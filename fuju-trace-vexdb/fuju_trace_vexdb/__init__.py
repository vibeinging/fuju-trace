"""VexDB storage and native search adapter for Fuju Trace events.

The adapter owns its tables. It keeps source events and folded search rows in one
transaction, while VexDB supplies BM25 and vector top-k. It is intentionally a
separate package so the Rust engine and the default SDK stay dependency-free.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

__all__ = ["VexDBTraceStore"]

_PREFIX = re.compile(r"^[a-z][a-z0-9_]{0,35}$")
_FIELDS = (
    "parent_span_id", "status", "duration_ns", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "session_id", "tenant_id",
    "external_trace_id", "external_span_id", "external_parent_span_id",
    "external_session_id", "span_name", "display_name", "agent_name",
    "tool_name", "model", "input_text", "output_text", "eval_score", "eval_label",
)
_FILTER_COLUMNS = {"trace_id": "trace_id", "agent_name": "agent_name", "status": "status"}
_ATTR_ALIASES = {"projectId": "project_id", "callSite": "call_site"}


_SQL_ROWS_PER_STATEMENT = 256  # Bound SQL text and bind count without changing caller batch semantics.


def _chunks(items: Sequence[Any], size: int = _SQL_ROWS_PER_STATEMENT) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def _pair_sql(count: int) -> str:
    return ",".join(["(%s,%s)"] * count)


def _pair_params(pairs: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(value for pair in pairs for value in pair)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event_id(ext_span_id: str, seq: int, event_type: int) -> str:
    # Same FNV-1a input and byte order as fuju_trace.event.event_id.
    data = ext_span_id.encode("utf-8") + seq.to_bytes(8, "little") + bytes([event_type])
    value = 0xCBF29CE484222325
    for byte in data:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def _vector_text(vector: Sequence[float], dim: int) -> str:
    if len(vector) != dim:
        raise ValueError(f"vector dimension {len(vector)} does not match configured {dim}")
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("vector values must be finite")
    if not any(value != 0 for value in values):
        raise ValueError("cosine vector must not be all zero")
    return "[" + ",".join(format(value, ".9g") for value in values) + "]"


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
    # display_name and span_name are intentionally excluded from BM25, as in the SDK contract.
    return "\n".join(str(value) for value in (
        span.get("input_text"), span.get("output_text"), *(span.get("logs") or []),
    ) if value) or " "


class VexDBTraceStore:
    """Store SDK events in VexDB and search with native BM25 and graph_index.

    A store is bound to one tenant. Use one instance per tenant; no request body
    may change its tenant. One connection is shared under a lock, so callers may
    use it from multiple threads. Processes should each open their own connection.
    """

    def __init__(
        self, dsn: str | None = None, *, tenant_id: str | int, vector_dim: int,
        table_prefix: str = "fuju_trace", connection_params: Mapping[str, Any] | None = None,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not _PREFIX.fullmatch(table_prefix):
            raise ValueError("table_prefix must be a short lowercase SQL identifier")
        if not isinstance(vector_dim, int) or isinstance(vector_dim, bool) or vector_dim < 1:
            raise ValueError("vector_dim must be a positive integer")
        if (dsn is None) == (connection_params is None):
            raise ValueError("provide either VexDB dsn or connection_params")
        if dsn is not None and not dsn:
            raise ValueError("VexDB dsn is required")
        if connection_params is not None and not connection_params:
            raise ValueError("VexDB connection_params must not be empty")
        if tenant_id is None or str(tenant_id) == "":
            raise ValueError("tenant_id is required")
        if connection_factory is None:
            try:
                import psycopg2
            except ImportError as exc:
                raise RuntimeError("Install psycopg2-binary or provide a compatible psycopg2 driver") from exc
            connection_factory = psycopg2.connect
        self._conn = (connection_factory(**dict(connection_params)) if connection_params is not None
                      else connection_factory(dsn))
        if hasattr(self._conn, "autocommit"):
            self._conn.autocommit = False
        self._lock = threading.RLock()
        self._tenant = str(tenant_id)
        self.vector_dim = vector_dim
        self.prefix = table_prefix
        self._config = f"{table_prefix}_config"
        self._events = f"{table_prefix}_events"
        self._spans = f"{table_prefix}_spans"
        self._attrs = f"{table_prefix}_attrs"
        self._closed = False

    @classmethod
    def open(cls, dsn: str | None = None, *, tenant_id: str | int, vector_dim: int, initialize: bool = False, **options: Any) -> "VexDBTraceStore":
        store = cls(dsn, tenant_id=tenant_id, vector_dim=vector_dim, **options)
        if initialize:
            try:
                store.initialize()
            except Exception:
                store.close()
                raise
        return store

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("VexDBTraceStore is closed")
            cursor = self._conn.cursor()
            try:
                yield cursor
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cursor.close()

    def _tenant_for(self, requested: str | int | None) -> str:
        if requested is not None and str(requested) != self._tenant:
            raise ValueError("tenant_id does not match this VexDBTraceStore")
        return self._tenant

    def initialize(self) -> None:
        """Create private tables and native indexes; call once before using a new prefix."""
        with self._tx() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {self._config} (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cur.execute(f"INSERT INTO {self._config} (name, value) VALUES ('schema_version', '1') ON CONFLICT (name) DO NOTHING")
            cur.execute(f"INSERT INTO {self._config} (name, value) VALUES ('vector_dim', %s) ON CONFLICT (name) DO NOTHING", (str(self.vector_dim),))
            cur.execute(f"SELECT name, value FROM {self._config} WHERE name IN ('schema_version', 'vector_dim')")
            settings = dict(cur.fetchall())
            if settings != {"schema_version": "1", "vector_dim": str(self.vector_dim)}:
                raise ValueError(f"existing {self.prefix} schema version or vector dimension differs")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {self._events} (
                event_id CHAR(16) NOT NULL, tenant_id TEXT NOT NULL,
                ext_span_id TEXT NOT NULL, event_type SMALLINT NOT NULL,
                trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
                seq BIGINT NOT NULL, ts BIGINT NOT NULL, event_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, ext_span_id, seq, event_type))""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {self._spans} (
                tenant_id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
                ts BIGINT NOT NULL, agent_name TEXT, status INTEGER,
                search_text TEXT NOT NULL, data TEXT NOT NULL,
                embedding floatvector({self.vector_dim}),
                PRIMARY KEY (tenant_id, trace_id, span_id))""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {self._attrs} (
                tenant_id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
                attr_key TEXT NOT NULL, value_hash CHAR(64) NOT NULL, value_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, trace_id, span_id, attr_key))""")
            self._create_index(cur, f"{self.prefix}_event_span_idx", self._events, "(tenant_id, trace_id, span_id, seq)")
            self._create_index(cur, f"{self.prefix}_attrs_lookup_idx", self._attrs, "(tenant_id, attr_key, value_hash)")
            self._create_index(cur, f"{self.prefix}_span_time_idx", self._spans, "(tenant_id, ts)")
            self._create_index(cur, f"{self.prefix}_text_idx", self._spans, "USING fulltext(search_text)")
            self._create_index(cur, f"{self.prefix}_vector_idx", self._spans, "USING graph_index(embedding floatvector_cosine_ops)")

    @staticmethod
    def _create_index(cur: Any, name: str, table: str, expression: str) -> None:
        # VexDB documents CREATE INDEX, not IF NOT EXISTS for these index methods.
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s", (name,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE INDEX {name} ON {table} {expression}")

    def ingest(self, events: Sequence[Mapping[str, Any]], *, tenant_id: str | int | None = None) -> dict[str, int]:
        tenant = self._tenant_for(tenant_id)
        prepared: list[tuple[str, str, int, str, str, int, int, str]] = []
        for raw in events:
            event = dict(raw)
            if event.get("tenant_id") is not None and str(event["tenant_id"]) != tenant:
                raise ValueError("event tenant_id does not match this VexDBTraceStore")
            kind, seq = int(event["event_type"]), int(event["seq"])
            if kind not in (1, 2, 3, 4, 5) or not 0 <= seq < 2**63:
                raise ValueError("invalid event_type or seq")
            ext_id = str(event["ext_span_id"])
            if not ext_id:
                raise ValueError("ext_span_id is required")
            eid = _event_id(ext_id, seq, kind)
            if event.get("event_id") is not None and int(event["event_id"]) != int(eid, 16):
                raise ValueError("event_id does not match ext_span_id, seq and event_type")
            trace_id, span_id = str(event["trace_id"]), str(event["span_id"])
            if not trace_id or not span_id:
                raise ValueError("trace_id and span_id are required")
            event["tenant_id"] = tenant
            prepared.append((eid, ext_id, kind, trace_id, span_id, seq, int(event["ts"]), _json(event)))
        if not prepared:
            return {"ingested": 0}
        groups = sorted({(trace_id, span_id) for _, _, _, trace_id, span_id, _, _, _ in prepared})
        inserted = 0
        with self._tx() as cur:
            # Insert placeholders in key order, then lock all requested spans before
            # touching events. Reprojection and source events stay in one transaction.
            for part in _chunks(groups):
                values_sql = ",".join(["(%s,%s,%s,0,' ','{}')"] * len(part))
                values = tuple(value for trace_id, span_id in part
                               for value in (tenant, trace_id, span_id))
                cur.execute(f"""INSERT INTO {self._spans}
                    (tenant_id, trace_id, span_id, ts, search_text, data)
                    VALUES {values_sql}
                    ON CONFLICT (tenant_id, trace_id, span_id) DO NOTHING""", values)
                cur.execute(f"""SELECT 1 FROM {self._spans}
                    WHERE tenant_id=%s AND (trace_id,span_id) IN ({_pair_sql(len(part))})
                    ORDER BY trace_id,span_id FOR UPDATE""", (tenant, *_pair_params(part)))
                cur.fetchall()

            # VexDB supports multi-row ON CONFLICT and reports the number inserted.
            # Do not infer it from submitted events: retries may be in the same call.
            for part in _chunks(prepared):
                values_sql = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(part))
                values = tuple(value for eid, ext_id, kind, trace_id, span_id, seq, ts, body in part
                               for value in (eid, tenant, ext_id, kind, trace_id, span_id, seq, ts, body))
                cur.execute(f"""INSERT INTO {self._events}
                    (event_id, tenant_id, ext_span_id, event_type, trace_id, span_id, seq, ts, event_json)
                    VALUES {values_sql}
                    ON CONFLICT (tenant_id, ext_span_id, seq, event_type) DO NOTHING""", values)
                if cur.rowcount < 0:
                    raise RuntimeError("VexDB did not report inserted event count")
                inserted += cur.rowcount

            if inserted == 0:
                # A retry with a wrong span ID must not leave its placeholder.
                for part in _chunks(groups):
                    cur.execute(f"""DELETE FROM {self._spans}
                        WHERE tenant_id=%s AND data='{{}}'
                        AND (trace_id,span_id) IN ({_pair_sql(len(part))})""",
                        (tenant, *_pair_params(part)))
                return {"ingested": 0}

            for part in _chunks(groups):
                # Read all source events for this bounded group in one round trip.
                cur.execute(f"""SELECT trace_id, span_id, event_json FROM {self._events}
                    WHERE tenant_id=%s AND (trace_id,span_id) IN ({_pair_sql(len(part))})
                    ORDER BY trace_id,span_id,seq,event_id""", (tenant, *_pair_params(part)))
                source_events: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in part}
                for trace_id, span_id, body in cur.fetchall():
                    source_events[(str(trace_id), str(span_id))].append(json.loads(body))
                projected: list[tuple[tuple[str, str], dict[str, Any]]] = []
                empty: list[tuple[str, str]] = []
                for key in part:
                    if source_events[key]:
                        projected.append((key, _fold(source_events[key])))
                    else:
                        empty.append(key)
                if empty:
                    cur.execute(f"""DELETE FROM {self._spans}
                        WHERE tenant_id=%s AND data='{{}}'
                        AND (trace_id,span_id) IN ({_pair_sql(len(empty))})""",
                        (tenant, *_pair_params(empty)))
                if not projected:
                    continue
                projection_rows = [
                    (trace_id, span_id, folded["ts"], folded.get("agent_name"),
                     folded.get("status"), _search_text(folded), _json(folded))
                    for (trace_id, span_id), folded in projected
                ]
                for update_part in _chunks(projection_rows):
                    values_sql = ",".join(
                        ["(%s,%s,%s::bigint,%s::text,%s::integer,%s::text,%s::text)"] * len(update_part))
                    values = tuple(value for row in update_part for value in row)
                    cur.execute(f"""UPDATE {self._spans} AS s
                        SET ts=v.ts, agent_name=v.agent_name, status=v.status,
                            search_text=v.search_text, data=v.data
                        FROM (VALUES {values_sql})
                            AS v(trace_id,span_id,ts,agent_name,status,search_text,data)
                        WHERE s.tenant_id=%s AND s.trace_id=v.trace_id AND s.span_id=v.span_id""",
                        (*values, tenant))
                    if cur.rowcount != len(update_part):
                        raise RuntimeError("VexDB did not update every folded span")
                projected_keys = [key for key, _ in projected]
                cur.execute(f"""DELETE FROM {self._attrs}
                    WHERE tenant_id=%s AND (trace_id,span_id) IN ({_pair_sql(len(projected_keys))})""",
                    (tenant, *_pair_params(projected_keys)))
                attrs: list[tuple[str, str, str, str, str, str]] = []
                for (trace_id, span_id), folded in projected:
                    indexed_attrs = dict(folded["attrs"])
                    if "__fuju_session_id" in indexed_attrs:
                        raise ValueError("__fuju_session_id is reserved for the trace store")
                    session_id = folded.get("external_session_id") or folded.get("session_id")
                    if session_id is not None:
                        indexed_attrs["__fuju_session_id"] = str(session_id)
                    for key, value in sorted(indexed_attrs.items()):
                        encoded = _json(value)
                        attrs.append((tenant, trace_id, span_id, str(key),
                                      hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded))
                for attr_part in _chunks(attrs):
                    values_sql = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(attr_part))
                    values = tuple(value for row in attr_part for value in row)
                    cur.execute(f"""INSERT INTO {self._attrs}
                        (tenant_id, trace_id, span_id, attr_key, value_hash, value_json)
                        VALUES {values_sql}""", values)
        return {"ingested": inserted}

    def set_embedding(self, trace_id: str | int, span_id: str | int, vector: Sequence[float], *, tenant_id: str | int | None = None) -> bool:
        tenant = self._tenant_for(tenant_id)
        encoded = _vector_text(vector, self.vector_dim)
        with self._tx() as cur:
            cur.execute(f"""UPDATE {self._spans} SET embedding=%s::floatvector
                WHERE tenant_id=%s AND trace_id=%s AND span_id=%s AND data <> '{{}}'""",
                (encoded, tenant, str(trace_id), str(span_id)))
            return cur.rowcount == 1

    def set_embeddings(
        self, embeddings: Sequence[tuple[str | int, str | int, Sequence[float]]],
        *, tenant_id: str | int | None = None,
    ) -> int:
        """Update existing span vectors in one transaction, returning rows changed.

        Validate the whole input before writing so a malformed later vector cannot
        leave earlier chunks committed. Repeated span keys are rejected because an
        UPDATE FROM VALUES would otherwise choose an unspecified source row.
        """
        tenant = self._tenant_for(tenant_id)
        rows: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for trace_id, span_id, vector in embeddings:
            key = (str(trace_id), str(span_id))
            if key in seen:
                raise ValueError("duplicate trace_id/span_id in embedding batch")
            seen.add(key)
            rows.append((*key, _vector_text(vector, self.vector_dim)))
        if not rows:
            return 0
        updated = 0
        with self._tx() as cur:
            for part in _chunks(rows):
                values_sql = ",".join(["(%s,%s,%s::floatvector)"] * len(part))
                values = tuple(value for row in part for value in row)
                cur.execute(f"""UPDATE {self._spans} AS s SET embedding=v.embedding
                    FROM (VALUES {values_sql}) AS v(trace_id,span_id,embedding)
                    WHERE s.tenant_id=%s AND s.trace_id=v.trace_id
                    AND s.span_id=v.span_id AND s.data <> '{{}}'""", (*values, tenant))
                if cur.rowcount < 0:
                    raise RuntimeError("VexDB did not report updated vector count")
                updated += cur.rowcount
        return updated

    def _where(self, tenant: str, filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["s.tenant_id = %s"]
        params: list[Any] = [tenant]
        attrs: dict[str, Any] = {}
        for key, value in filters.items():
            if key in {"tenant_id", "tenantId"}:
                raise ValueError("tenant cannot be set in search filter")
            if value is None and key != "attrs":
                continue
            if key in _FILTER_COLUMNS:
                clauses.append(f"s.{_FILTER_COLUMNS[key]} = %s")
                params.append(str(value) if key == "trace_id" else value)
            elif key in {"externalTraceId", "external_trace_id"}:
                clauses.append("s.trace_id = %s")
                params.append(str(value))
            elif key in {"externalSessionId", "external_session_id"}:
                attrs["__fuju_session_id"] = str(value)
            elif key in ("time_from", "time_to"):
                clauses.append(f"s.ts {'>=' if key == 'time_from' else '<='} %s")
                params.append(int(value))
            elif key == "attrs":
                if not isinstance(value, Mapping):
                    raise ValueError("filter.attrs must be an object")
                attrs.update(value)
            elif key in {"project_id", "projectId", "skill", "mode", "call_site", "callSite"}:
                attrs[_ATTR_ALIASES.get(key, key)] = value
            else:
                raise ValueError(f"unsupported VexDB search filter: {key}")
        for key, value in sorted(attrs.items()):
            encoded = _json(value)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            clauses.append(f"""EXISTS (SELECT 1 FROM {self._attrs} a
                WHERE a.tenant_id=s.tenant_id AND a.trace_id=s.trace_id AND a.span_id=s.span_id
                AND a.attr_key=%s AND a.value_hash=%s AND a.value_json=%s)""")
            params.extend((str(key), digest, encoded))
        return " AND ".join(clauses), params

    def _scan_scope(self, tenant: str, filters: Mapping[str, Any]) -> tuple[str, str, list[Any]]:
        """Join the exact session index so the planner can avoid a broad scan."""
        remaining = dict(filters)
        sessions = [str(remaining.pop(key)) for key in ("externalSessionId", "external_session_id")
                    if key in remaining and remaining[key] is not None]
        if len(set(sessions)) > 1:
            raise ValueError("conflicting session filters")
        where, params = self._where(tenant, remaining)
        if not sessions:
            return "", where, params
        encoded = _json(sessions[0])
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        join = (f"JOIN {self._attrs} session_index ON session_index.tenant_id=s.tenant_id "
                "AND session_index.trace_id=s.trace_id AND session_index.span_id=s.span_id")
        where += " AND session_index.attr_key=%s AND session_index.value_hash=%s AND session_index.value_json=%s"
        return join, where, [*params, "__fuju_session_id", digest, encoded]

    def list_spans(self, *, filters: Mapping[str, Any] | None = None,
                   limit: int = 100, cursor: int = 0,
                   tenant_id: str | int | None = None) -> dict[str, Any]:
        """Page folded spans by business identity without requiring text search."""
        tenant = self._tenant_for(tenant_id)
        if not 1 <= limit <= 1000 or cursor < 0:
            raise ValueError("limit must be 1..1000 and cursor must be nonnegative")
        join, where, params = self._scan_scope(tenant, filters or {})
        with self._tx() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._spans} s {join} WHERE {where} AND s.data <> '{{}}'", params)
            total = int(cur.fetchone()[0])
            cur.execute(f"""SELECT s.data FROM {self._spans} s {join}
                WHERE {where} AND s.data <> '{{}}'
                ORDER BY s.ts, s.trace_id, s.span_id LIMIT %s OFFSET %s""",
                (*params, limit, cursor))
            return {"items": [json.loads(row[0]) for row in cur.fetchall()], "total": total}

    def list_trace_ids(self, *, filters: Mapping[str, Any] | None = None,
                       limit: int = 20, cursor: int = 0,
                       tenant_id: str | int | None = None) -> dict[str, Any]:
        """Page trace IDs for session-scoped replay and export."""
        tenant = self._tenant_for(tenant_id)
        if not 1 <= limit <= 1000 or cursor < 0:
            raise ValueError("limit must be 1..1000 and cursor must be nonnegative")
        join, where, params = self._scan_scope(tenant, filters or {})
        with self._tx() as cur:
            cur.execute(f"""SELECT COUNT(DISTINCT s.trace_id) FROM {self._spans} s {join}
                WHERE {where} AND s.data <> '{{}}'""", params)
            total = int(cur.fetchone()[0])
            cur.execute(f"""SELECT s.trace_id FROM {self._spans} s {join}
                WHERE {where} AND s.data <> '{{}}'
                GROUP BY s.trace_id ORDER BY MIN(s.ts), s.trace_id LIMIT %s OFFSET %s""",
                (*params, limit, cursor))
            return {"items": [str(row[0]) for row in cur.fetchall()], "total": total}

    def ping(self) -> bool:
        """Check that this connection can read the expected trace schema."""
        with self._tx() as cur:
            cur.execute(f"SELECT value FROM {self._config} WHERE name='schema_version'")
            row = cur.fetchone()
            if row is None or row[0] != "1":
                raise RuntimeError("VexDB Trace schema is missing or incompatible")
            cur.execute(f"SELECT 1 FROM {self._events} LIMIT 0")
            cur.execute(f"SELECT 1 FROM {self._spans} LIMIT 0")
            cur.execute(f"SELECT 1 FROM {self._attrs} LIMIT 0")
            return True

    def prune_before(self, cutoff_ns: int, *, limit: int = 1000,
                     tenant_id: str | int | None = None) -> dict[str, int]:
        """Remove whole traces whose newest source event is older than a cutoff.

        Source events and both projections are removed in one transaction. The
        caller chooses the retention window; this method never reads a clock.
        """
        tenant = self._tenant_for(tenant_id)
        if cutoff_ns < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid cutoff or limit")
        with self._tx() as cur:
            cur.execute(f"""SELECT trace_id FROM {self._events}
                WHERE tenant_id=%s GROUP BY trace_id HAVING MAX(ts) < %s
                ORDER BY MAX(ts), trace_id LIMIT %s""", (tenant, cutoff_ns, limit))
            trace_ids = [str(row[0]) for row in cur.fetchall()]
            if not trace_ids:
                return {"traces": 0, "events": 0}
            placeholders = ",".join(["%s"] * len(trace_ids))
            cur.execute(f"DELETE FROM {self._attrs} WHERE tenant_id=%s AND trace_id IN ({placeholders})",
                        (tenant, *trace_ids))
            cur.execute(f"DELETE FROM {self._spans} WHERE tenant_id=%s AND trace_id IN ({placeholders})",
                        (tenant, *trace_ids))
            cur.execute(f"DELETE FROM {self._events} WHERE tenant_id=%s AND trace_id IN ({placeholders})",
                        (tenant, *trace_ids))
            return {"traces": len(trace_ids), "events": cur.rowcount}

    def search(self, query: Mapping[str, Any] | None = None, *, tenant_id: str | int | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        tenant = self._tenant_for(tenant_id)
        body = {**dict(query or {}), **kwargs}
        if "tenant_id" in body or "tenantId" in body:
            raise ValueError("tenant cannot be set in search body")
        unknown = set(body) - {"text", "vector", "k", "filter"}
        if unknown:
            raise ValueError(f"unsupported VexDB search fields: {sorted(unknown)}")
        text = body.get("text")
        if text and "@<PARAM:" in str(text).upper():
            raise ValueError("VexDB BM25 query parameters are not accepted in plain text search")
        vector = body.get("vector")
        if not text and vector is None:
            raise ValueError("search requires text or vector")
        k = int(body.get("k", 10))
        if not 1 <= k <= 1000:
            raise ValueError("k must be between 1 and 1000")
        filters = body.get("filter") or {}
        if not isinstance(filters, Mapping):
            raise ValueError("filter must be an object")
        where, params = self._where(tenant, filters)
        candidate_k = min(1000, max(k * 4, 50)) if text and vector is not None else k
        text_hits: list[tuple[str, str, float]] = []
        vector_hits: list[tuple[str, str, float]] = []
        with self._tx() as cur:
            if text:
                cur.execute(f"""SELECT s.trace_id, s.span_id, bm25_score() AS score
                    FROM {self._spans} s WHERE {where} AND s.search_text @~@ %s
                    ORDER BY score DESC NULLS LAST LIMIT %s""", (*params, str(text), candidate_k))
                text_hits = [(str(t), str(s), float(score)) for t, s, score in cur.fetchall() if score is not None]
            if vector is not None:
                encoded = _vector_text(vector, self.vector_dim)
                cur.execute(f"""SELECT s.trace_id, s.span_id,
                    s.embedding <=> %s::floatvector AS distance
                    FROM {self._spans} s WHERE {where} AND s.embedding IS NOT NULL
                    ORDER BY s.embedding <=> %s::floatvector LIMIT %s""",
                    (encoded, *params, encoded, candidate_k))
                vector_hits = [(str(t), str(s), float(distance)) for t, s, distance in cur.fetchall() if distance is not None]
            scores: dict[tuple[str, str], float] = {}
            if text and vector is not None:
                for hits in (text_hits, vector_hits):
                    for rank, (trace_id, span_id, _) in enumerate(hits, 1):
                        key = (trace_id, span_id)
                        scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
            elif text:
                scores = {(trace_id, span_id): score for trace_id, span_id, score in text_hits}
            else:
                scores = {(trace_id, span_id): 1.0 / (1.0 + max(0.0, distance)) for trace_id, span_id, distance in vector_hits}
            ranked = sorted(scores, key=lambda key: (-scores[key], key))[:k]
            # Hydrate ranked IDs in bounded groups, then restore score order in
            # Python. A Top 10 search now uses one data read instead of ten.
            rows: dict[tuple[str, str], dict[str, Any]] = {}
            for part in _chunks(ranked):
                cur.execute(f"""SELECT trace_id, span_id, data FROM {self._spans}
                    WHERE tenant_id=%s AND (trace_id,span_id) IN ({_pair_sql(len(part))})""",
                    (tenant, *_pair_params(part)))
                for trace_id, span_id, data in cur.fetchall():
                    rows[(str(trace_id), str(span_id))] = json.loads(data)
            result: list[dict[str, Any]] = []
            for key in ranked:
                if key in rows:
                    span = rows[key]
                    span["score"] = scores[key]
                    result.append(span)
            return result

    def trace(self, trace_id: str | int, *, tenant_id: str | int | None = None) -> list[dict[str, Any]]:
        tenant = self._tenant_for(tenant_id)
        with self._tx() as cur:
            cur.execute(f"SELECT data FROM {self._spans} WHERE tenant_id=%s AND trace_id=%s AND data <> '{{}}' ORDER BY ts, span_id", (tenant, str(trace_id)))
            return [json.loads(row[0]) for row in cur.fetchall()]

    def span(self, trace_id: str | int, span_id: str | int, *, tenant_id: str | int | None = None) -> dict[str, Any] | None:
        tenant = self._tenant_for(tenant_id)
        with self._tx() as cur:
            cur.execute(f"SELECT data FROM {self._spans} WHERE tenant_id=%s AND trace_id=%s AND span_id=%s AND data <> '{{}}'", (tenant, str(trace_id), str(span_id)))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None

    def capabilities(self) -> dict[str, Any]:
        """Describe the supported surface without claiming a planner guarantee."""
        return {
            "backend": "vexdb", "text": "native_bm25", "vector": "graph_index",
            "vector_dim": self.vector_dim, "distance": "cosine", "hybrid": "rrf",
            "attrs_filter": "exact_sql", "transactional_projection": True,
            "vector_batch_write": True, "ann_filter_execution": "planner_dependent",
        }

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "VexDBTraceStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
