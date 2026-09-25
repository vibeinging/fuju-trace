"""Direct SQL stores for Fuju Trace.

Each store owns private tables under a validated prefix. Source events and the
folded span projection change in one transaction. Text search is a portable
substring query; native FTS and vector indexes are separate future capabilities.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from fuju_trace.event import EventType, event_id

__all__ = [
    "SQLiteTraceStore", "DuckDBTraceStore", "PostgreSQLTraceStore",
]

_PREFIX = re.compile(r"^[a-z][a-z0-9_]{0,35}$")
_FIELDS = (
    "parent_span_id", "status", "duration_ns", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "session_id", "tenant_id",
    "external_trace_id", "external_span_id", "external_parent_span_id",
    "external_session_id", "span_name", "display_name", "agent_name",
    "tool_name", "model", "input_text", "output_text", "eval_score", "eval_label",
)
_ALIASES = {"projectId": "project_id", "callSite": "call_site"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _key(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _fold(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(events, key=lambda e: (int(e["seq"]), int(e["event_type"])))
    first = ordered[0]
    span: dict[str, Any] = {
        "trace_id": first["trace_id"], "span_id": first["span_id"],
        "has_start": False, "has_end": False, "event_count": len(ordered),
        "ts": min(int(e["ts"]) for e in ordered), "logs": [], "attrs": {},
    }
    seen_logs: set[str] = set()
    for event in ordered:
        kind = int(event["event_type"])
        span["has_start"] |= kind == 1
        span["has_end"] |= kind == 2
        for field in _FIELDS:
            value = event.get(field)
            if value is not None and (field not in {"span_name", "display_name"} or kind == 1):
                span[field] = value
        for log in event.get("logs") or []:
            if log not in seen_logs:
                seen_logs.add(log)
                span["logs"].append(log)
        span["attrs"].update(event.get("attrs") or {})
    return span


def _search_text(span: Mapping[str, Any]) -> str:
    # Display fields must not silently become search terms.
    return "\n".join(str(value) for value in (
        span.get("input_text"), span.get("output_text"), *(span.get("logs") or []),
    ) if value) or " "


def _like(value: str) -> str:
    return "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


class SQLTraceStore:
    """One tenant, one DB-API connection, guarded by a thread lock."""

    dialect = ""

    def __init__(self, connection: Any, *, tenant_id: str | int,
                 table_prefix: str = "fuju_trace") -> None:
        if not _PREFIX.fullmatch(table_prefix):
            raise ValueError("table_prefix must be a short lowercase SQL identifier")
        if tenant_id is None or str(tenant_id) == "":
            raise ValueError("tenant_id is required")
        self._conn = connection
        self._lock = threading.RLock()
        self._closed = False
        self._tenant = str(tenant_id)
        self._tenant_key = _key(self._tenant)
        self.prefix = table_prefix
        self._config = f"{table_prefix}_config"
        self._events = f"{table_prefix}_events"
        self._spans = f"{table_prefix}_spans"
        self._attrs = f"{table_prefix}_attrs"

    @property
    def _bind(self) -> str:
        return "?" if self.dialect in ("sqlite", "duckdb") else "%s"

    def _execute(self, cur: Any, sql: str, params: Sequence[Any] = ()) -> Any:
        return cur.execute(sql.replace("?", self._bind), tuple(params))

    @contextmanager
    def _tx(self, *, write: bool = False) -> Iterator[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("trace store is closed")
            # DuckDB cursor() creates another connection. An unnamed in-memory
            # database would then appear empty to that connection.
            cur = self._conn if self.dialect == "duckdb" else self._conn.cursor()
            try:
                if write and self.dialect == "sqlite":
                    cur.execute("BEGIN IMMEDIATE")
                elif write and self.dialect == "duckdb":
                    cur.execute("BEGIN TRANSACTION")
                yield cur
                if self.dialect != "duckdb" or write:
                    self._conn.commit()
            except Exception:
                if self.dialect != "duckdb" or write:
                    self._conn.rollback()
                raise
            finally:
                if cur is not self._conn:
                    cur.close()

    def _tenant_for(self, requested: str | int | None) -> str:
        if requested is not None and str(requested) != self._tenant:
            raise ValueError("tenant_id does not match this store")
        return self._tenant_key

    def initialize(self) -> None:
        """Create a private, versioned schema; never mutate another prefix."""
        with self._tx(write=True) as cur:
            self._execute(cur, f"CREATE TABLE IF NOT EXISTS {self._config} (name TEXT PRIMARY KEY, value VARCHAR(255) NOT NULL)")
            self._execute(cur, f"SELECT value FROM {self._config} WHERE name='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._execute(cur, f"INSERT INTO {self._config} (name,value) VALUES ('schema_version','1')")
            elif row[0] != "1":
                raise ValueError(f"unsupported {self.prefix} schema version")
            self._execute(cur, f"""CREATE TABLE IF NOT EXISTS {self._events} (
                tenant_key CHAR(64) NOT NULL, event_key CHAR(64) NOT NULL,
                trace_key CHAR(64) NOT NULL, span_key CHAR(64) NOT NULL,
                seq BIGINT NOT NULL, ts BIGINT NOT NULL, event_json TEXT NOT NULL,
                PRIMARY KEY (tenant_key,event_key))""")
            self._execute(cur, f"""CREATE TABLE IF NOT EXISTS {self._spans} (
                tenant_key CHAR(64) NOT NULL, trace_key CHAR(64) NOT NULL,
                span_key CHAR(64) NOT NULL, trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL, ts BIGINT NOT NULL,
                agent_name TEXT, status INTEGER, session_key CHAR(64),
                search_text TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (tenant_key,trace_key,span_key))""")
            self._execute(cur, f"""CREATE TABLE IF NOT EXISTS {self._attrs} (
                tenant_key CHAR(64) NOT NULL, trace_key CHAR(64) NOT NULL,
                span_key CHAR(64) NOT NULL, attr_key_hash CHAR(64) NOT NULL,
                value_hash CHAR(64) NOT NULL, attr_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                PRIMARY KEY (tenant_key,trace_key,span_key,attr_key_hash))""")
            self._index(cur, f"{self.prefix}_event_span_idx", self._events,
                        "(tenant_key,trace_key,span_key,seq)")
            self._index(cur, f"{self.prefix}_span_time_idx", self._spans,
                        "(tenant_key,ts)")
            self._index(cur, f"{self.prefix}_span_session_idx", self._spans,
                        "(tenant_key,session_key,ts)")
            self._index(cur, f"{self.prefix}_attrs_lookup_idx", self._attrs,
                        "(tenant_key,attr_key_hash,value_hash)")

    def _index(self, cur: Any, name: str, table: str, columns: str) -> None:
        self._execute(cur, f"CREATE INDEX IF NOT EXISTS {name} ON {table} {columns}")

    def ingest(self, events: Sequence[Mapping[str, Any]], *,
               tenant_id: str | int | None = None) -> dict[str, int]:
        tenant = self._tenant_for(tenant_id)
        prepared: list[tuple[str, str, str, str, str, int, int, str]] = []
        for raw in events:
            event = dict(raw)
            if event.get("tenant_id") is not None and str(event["tenant_id"]) != self._tenant:
                raise ValueError("event tenant_id does not match this store")
            kind, seq = int(event["event_type"]), int(event["seq"])
            if kind not in (1, 2, 3, 4, 5) or not 0 <= seq < 2**63:
                raise ValueError("invalid event_type or seq")
            ext_id = str(event["ext_span_id"]) if event.get("ext_span_id") is not None else ""
            if not ext_id:
                raise ValueError("ext_span_id is required")
            expected_id = event_id(ext_id, seq, EventType(kind))
            if event.get("event_id") is not None and int(event["event_id"]) != expected_id:
                raise ValueError("event_id does not match ext_span_id, seq and event_type")
            trace_id = str(event["trace_id"]) if event.get("trace_id") is not None else ""
            span_id = str(event["span_id"]) if event.get("span_id") is not None else ""
            if not trace_id or not span_id:
                raise ValueError("trace_id and span_id are required")
            event["tenant_id"] = self._tenant
            identity = _key(_json([ext_id, seq, kind]))
            prepared.append((identity, _key(trace_id), _key(span_id),
                             trace_id, span_id, seq, int(event["ts"]), _json(event)))
        if not prepared:
            return {"ingested": 0}
        group_ids = {(trace_key, span_key): (trace_id, span_id)
                     for _, trace_key, span_key, trace_id, span_id, _, _, _ in prepared}
        groups = sorted(group_ids)
        inserted = 0
        with self._tx(write=True) as cur:
            for trace_key, span_key in groups:
                trace_id, span_id = group_ids[(trace_key, span_key)]
                self._execute(cur, f"""INSERT INTO {self._spans}
                    (tenant_key,trace_key,span_key,trace_id,span_id,ts,search_text,data)
                    VALUES (?,?,?,?,?,0,' ','{{}}') ON CONFLICT DO NOTHING""",
                    (tenant, trace_key, span_key, trace_id, span_id))
                lock = " FOR UPDATE" if self.dialect == "postgresql" else ""
                self._execute(cur, f"""SELECT 1 FROM {self._spans}
                    WHERE tenant_key=? AND trace_key=? AND span_key=?{lock}""",
                    (tenant, trace_key, span_key))
                cur.fetchone()
            for identity, trace_key, span_key, _trace_id, _span_id, seq, ts, body in prepared:
                self._execute(cur, f"""SELECT trace_key,span_key FROM {self._events}
                    WHERE tenant_key=? AND event_key=?""", (tenant, identity))
                existing = cur.fetchone()
                if existing is not None:
                    if (str(existing[0]), str(existing[1])) != (trace_key, span_key):
                        raise ValueError("duplicate event identity belongs to another span")
                    continue
                self._execute(cur, f"""INSERT INTO {self._events}
                    (tenant_key,event_key,trace_key,span_key,seq,ts,event_json)
                    VALUES (?,?,?,?,?,?,?)""",
                    (tenant, identity, trace_key, span_key, seq, ts, body))
                inserted += 1
            for trace_key, span_key in groups:
                self._execute(cur, f"""SELECT event_json FROM {self._events}
                    WHERE tenant_key=? AND trace_key=? AND span_key=?
                    ORDER BY seq,event_key""", (tenant, trace_key, span_key))
                sources = [json.loads(row[0]) for row in cur.fetchall()]
                if not sources:
                    self._execute(cur, f"""DELETE FROM {self._spans}
                        WHERE tenant_key=? AND trace_key=? AND span_key=? AND data='{{}}'""",
                        (tenant, trace_key, span_key))
                    continue
                span = _fold(sources)
                session = span.get("external_session_id") or span.get("session_id")
                self._execute(cur, f"""UPDATE {self._spans} SET
                    ts=?,agent_name=?,status=?,session_key=?,search_text=?,data=?
                    WHERE tenant_key=? AND trace_key=? AND span_key=?""",
                    (span["ts"], span.get("agent_name"), span.get("status"),
                     _key(session) if session is not None else None,
                     _search_text(span), _json(span), tenant, trace_key, span_key))
                self._execute(cur, f"""DELETE FROM {self._attrs}
                    WHERE tenant_key=? AND trace_key=? AND span_key=?""",
                    (tenant, trace_key, span_key))
                for attr_key, value in sorted((span.get("attrs") or {}).items()):
                    encoded = _json(value)
                    self._execute(cur, f"""INSERT INTO {self._attrs}
                        (tenant_key,trace_key,span_key,attr_key_hash,value_hash,attr_key,value_json)
                        VALUES (?,?,?,?,?,?,?)""",
                        (tenant, trace_key, span_key, _key(attr_key), _key(encoded),
                         str(attr_key), encoded))
        return {"ingested": inserted}

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["s.tenant_key=?", "s.data<>'{}'"]
        params: list[Any] = [self._tenant_key]
        attrs: dict[str, Any] = {}
        for key, value in filters.items():
            if key in {"tenant_id", "tenantId"}:
                raise ValueError("tenant cannot be set in a filter")
            if value is None and key != "attrs":
                continue
            if key in {"trace_id", "externalTraceId", "external_trace_id"}:
                clauses.append("s.trace_key=?")
                params.append(_key(value))
            elif key in {"externalSessionId", "external_session_id"}:
                clauses.append("s.session_key=?")
                params.append(_key(value))
            elif key == "agent_name":
                clauses.append("s.agent_name=?")
                params.append(value)
            elif key == "status":
                clauses.append("s.status=?")
                params.append(value)
            elif key in {"time_from", "time_to"}:
                clauses.append(f"s.ts {'>=' if key == 'time_from' else '<='}?")
                params.append(int(value))
            elif key == "attrs":
                if not isinstance(value, Mapping):
                    raise ValueError("filter.attrs must be an object")
                attrs.update(value)
            elif key in {"project_id", "projectId", "skill", "mode", "call_site", "callSite"}:
                attrs[_ALIASES.get(key, key)] = value
            else:
                raise ValueError(f"unsupported search filter: {key}")
        for key, value in sorted(attrs.items()):
            encoded = _json(value)
            clauses.append(f"""EXISTS (SELECT 1 FROM {self._attrs} a
                WHERE a.tenant_key=s.tenant_key AND a.trace_key=s.trace_key
                AND a.span_key=s.span_key AND a.attr_key_hash=?
                AND a.value_hash=? AND a.attr_key=? AND a.value_json=?)""")
            params.extend((_key(key), _key(encoded), str(key), encoded))
        return " AND ".join(clauses), params

    def search(self, query: Mapping[str, Any] | None = None, *,
               tenant_id: str | int | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        self._tenant_for(tenant_id)
        body = {**dict(query or {}), **kwargs}
        if set(body) - {"text", "vector", "k", "filter"}:
            raise ValueError("unsupported search field")
        if body.get("vector") is not None:
            raise NotImplementedError(f"{self.dialect} vector search is not configured")
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("search requires non-empty text")
        k = int(body.get("k", 10))
        if not 1 <= k <= 1000:
            raise ValueError("k must be between 1 and 1000")
        filters = body.get("filter") or {}
        if not isinstance(filters, Mapping):
            raise ValueError("filter must be an object")
        where, params = self._where(filters)
        with self._tx() as cur:
            self._execute(cur, f"""SELECT s.data FROM {self._spans} s
                WHERE {where} AND s.search_text LIKE ? ESCAPE '!'
                ORDER BY s.ts DESC,s.trace_key,s.span_key LIMIT ?""",
                (*params, _like(text), k))
            return [dict(json.loads(row[0]), score=1.0) for row in cur.fetchall()]

    def trace(self, trace_id: str | int, *, tenant_id: str | int | None = None) -> list[dict[str, Any]]:
        tenant = self._tenant_for(tenant_id)
        with self._tx() as cur:
            self._execute(cur, f"""SELECT data FROM {self._spans}
                WHERE tenant_key=? AND trace_key=? AND data<>'{{}}'
                ORDER BY ts,span_key""", (tenant, _key(trace_id)))
            return [json.loads(row[0]) for row in cur.fetchall()]

    def span(self, trace_id: str | int, span_id: str | int, *,
             tenant_id: str | int | None = None) -> dict[str, Any] | None:
        tenant = self._tenant_for(tenant_id)
        with self._tx() as cur:
            self._execute(cur, f"""SELECT data FROM {self._spans}
                WHERE tenant_key=? AND trace_key=? AND span_key=? AND data<>'{{}}'""",
                (tenant, _key(trace_id), _key(span_id)))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None

    def list_spans(self, *, filters: Mapping[str, Any] | None = None,
                   limit: int = 100, cursor: int = 0,
                   tenant_id: str | int | None = None) -> dict[str, Any]:
        self._tenant_for(tenant_id)
        if not 1 <= limit <= 1000 or cursor < 0:
            raise ValueError("limit must be 1..1000 and cursor nonnegative")
        where, params = self._where(filters or {})
        with self._tx() as cur:
            self._execute(cur, f"SELECT COUNT(*) FROM {self._spans} s WHERE {where}", params)
            total = int(cur.fetchone()[0])
            self._execute(cur, f"""SELECT s.data FROM {self._spans} s WHERE {where}
                ORDER BY s.ts,s.trace_key,s.span_key LIMIT ? OFFSET ?""",
                (*params, limit, cursor))
            return {"items": [json.loads(row[0]) for row in cur.fetchall()], "total": total}

    def list_trace_ids(self, *, filters: Mapping[str, Any] | None = None,
                       limit: int = 20, cursor: int = 0,
                       tenant_id: str | int | None = None) -> dict[str, Any]:
        self._tenant_for(tenant_id)
        if not 1 <= limit <= 1000 or cursor < 0:
            raise ValueError("limit must be 1..1000 and cursor nonnegative")
        where, params = self._where(filters or {})
        with self._tx() as cur:
            self._execute(cur, f"SELECT COUNT(DISTINCT s.trace_key) FROM {self._spans} s WHERE {where}", params)
            total = int(cur.fetchone()[0])
            self._execute(cur, f"""SELECT MIN(s.trace_id) FROM {self._spans} s WHERE {where}
                GROUP BY s.trace_key ORDER BY MIN(s.ts),s.trace_key LIMIT ? OFFSET ?""",
                (*params, limit, cursor))
            return {"items": [str(row[0]) for row in cur.fetchall()], "total": total}

    def capabilities(self) -> dict[str, Any]:
        return {"backend": self.dialect, "text": "substring_scan",
                "vector": None, "hybrid": None, "attrs_filter": "exact_sql",
                "transactional_projection": True, "session_filter": True}

    def ping(self) -> bool:
        with self._tx() as cur:
            self._execute(cur, f"SELECT value FROM {self._config} WHERE name='schema_version'")
            row = cur.fetchone()
            if row is None or row[0] != "1":
                return False
            for table in (self._events, self._spans, self._attrs):
                self._execute(cur, f"SELECT 1 FROM {table} LIMIT 0")
            return True

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> SQLTraceStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class SQLiteTraceStore(SQLTraceStore):
    dialect = "sqlite"

    @classmethod
    def open(cls, path: str | Path, *, tenant_id: str | int,
             initialize: bool = False, table_prefix: str = "fuju_trace") -> SQLiteTraceStore:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        store = cls(conn, tenant_id=tenant_id, table_prefix=table_prefix)
        try:
            if initialize:
                store.initialize()
            return store
        except Exception:
            store.close()
            raise


class DuckDBTraceStore(SQLTraceStore):
    dialect = "duckdb"

    @classmethod
    def open(cls, path: str | Path, *, tenant_id: str | int,
             initialize: bool = False, table_prefix: str = "fuju_trace") -> DuckDBTraceStore:
        try:
            import duckdb
        except ImportError as exc:
            raise RuntimeError("install fuju-trace-sql[duckdb]") from exc
        store = cls(duckdb.connect(str(path)), tenant_id=tenant_id, table_prefix=table_prefix)
        try:
            if initialize:
                store.initialize()
            return store
        except Exception:
            store.close()
            raise


class PostgreSQLTraceStore(SQLTraceStore):
    dialect = "postgresql"

    @classmethod
    def open(cls, dsn: str | None = None, *, tenant_id: str | int,
             connection_params: Mapping[str, Any] | None = None,
             initialize: bool = False, table_prefix: str = "fuju_trace",
             connection_factory: Callable[..., Any] | None = None) -> PostgreSQLTraceStore:
        if (dsn is None) == (connection_params is None):
            raise ValueError("provide PostgreSQL dsn or connection_params")
        if dsn == "" or connection_params == {}:
            raise ValueError("PostgreSQL connection target must not be empty")
        if connection_factory is None:
            try:
                import psycopg2
            except ImportError as exc:
                raise RuntimeError("install fuju-trace-sql[postgresql]") from exc
            connection_factory = psycopg2.connect
        conn = (connection_factory(dsn) if dsn is not None
                else connection_factory(**dict(connection_params)))
        if hasattr(conn, "autocommit"):
            conn.autocommit = False
        store = cls(conn, tenant_id=tenant_id, table_prefix=table_prefix)
        try:
            if initialize:
                store.initialize()
            return store
        except Exception:
            store.close()
            raise
