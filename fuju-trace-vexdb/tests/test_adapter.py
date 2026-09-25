import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "fuju-trace-sdk" / "python"))

from fuju_trace import connect
from fuju_trace.event import EventType, event_id
from fuju_trace_vexdb import VexDBTraceStore
from fuju_trace_vexdb import _event_id, _fold, _search_text, _vector_text


class FakeConnection:
    def __init__(self):
        self.sql = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.affected = 0

    def execute(self, sql, params=None):
        self.connection.sql.append((sql, params))
        if "bm25_score()" in sql:
            self.rows = [("7", "1", 9.0), ("7", "2", 8.0)]
        elif "AS distance" in sql:
            self.rows = [("7", "2", 0.01), ("7", "3", 0.1)]
        elif sql.lstrip().startswith("UPDATE ") and "SET embedding=v.embedding" in sql:
            self.rows = []
            self.affected = (len(params) - 1) // 3
        elif sql.lstrip().startswith("SELECT trace_id, span_id, data"):
            pairs = list(zip(params[1::2], params[2::2]))
            # Return reverse SQL row order so the adapter must restore rank order.
            self.rows = [(trace, span, json.dumps({"trace_id": int(trace), "span_id": int(span)}))
                         for trace, span in reversed(pairs)]
        else:
            raise AssertionError(sql)

    @property
    def rowcount(self):
        return self.affected

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class SQLiteCursor:
    def __init__(self, cursor, connection):
        self.cursor = cursor
        self.connection = connection
        self._reported_rowcount = None

    def execute(self, sql, params=None):
        self.connection.sql_calls += 1
        self._reported_rowcount = None
        if sql.lstrip().startswith("UPDATE fuju_trace_spans AS s"):
            # SQLite does not accept the VexDB UPDATE FROM VALUES syntax.
            # Translate it only in this transactional DML test double.
            values, tenant = params[:-1], params[-1]
            changed = 0
            for offset in range(0, len(values), 7):
                trace, span, ts, agent, status, search_text, data = values[offset:offset + 7]
                self.cursor.execute("""UPDATE fuju_trace_spans
                    SET ts=?,agent_name=?,status=?,search_text=?,data=?
                    WHERE tenant_id=? AND trace_id=? AND span_id=?""",
                    (ts, agent, status, search_text, data, tenant, trace, span))
                changed += self.cursor.rowcount
            self._reported_rowcount = changed
            return
        sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        self.cursor.execute(sql, params or ())

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    @property
    def rowcount(self):
        return self._reported_rowcount if self._reported_rowcount is not None else self.cursor.rowcount

    def close(self):
        self.cursor.close()


class SQLiteConnection:
    """Exercise the adapter's transactional DML on a real SQL engine."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.sql_calls = 0
        self.db.executescript("""
            CREATE TABLE fuju_trace_config (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO fuju_trace_config (name,value) VALUES ('schema_version','1');
            CREATE TABLE fuju_trace_spans (
                tenant_id TEXT, trace_id TEXT, span_id TEXT, ts INTEGER,
                agent_name TEXT, status INTEGER, search_text TEXT, data TEXT,
                PRIMARY KEY (tenant_id, trace_id, span_id));
            CREATE TABLE fuju_trace_events (
                event_id TEXT, tenant_id TEXT, ext_span_id TEXT, event_type INTEGER,
                trace_id TEXT, span_id TEXT, seq INTEGER, ts INTEGER, event_json TEXT,
                PRIMARY KEY (tenant_id, ext_span_id, seq, event_type));
            CREATE TABLE fuju_trace_attrs (
                tenant_id TEXT, trace_id TEXT, span_id TEXT, attr_key TEXT,
                value_hash TEXT, value_json TEXT,
                PRIMARY KEY (tenant_id, trace_id, span_id, attr_key));
        """)

    def cursor(self):
        return SQLiteCursor(self.db.cursor(), self)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        self.db.close()


class AdapterTests(unittest.TestCase):
    def test_event_id_matches_sdk(self):
        self.assertEqual(int(_event_id("span-测试", 5, 4), 16), event_id("span-测试", 5, EventType.LOG))

    def test_fold_is_ordered_and_preserves_start_name(self):
        events = [
            {"trace_id": 1, "span_id": 2, "ts": 30, "seq": 2, "event_type": 2, "status": 0, "logs": ["相同", "结束"], "attrs": {"n": 1}},
            {"trace_id": 1, "span_id": 2, "ts": 10, "seq": 0, "event_type": 1, "span_name": "开始", "logs": ["相同"], "attrs": {"ok": True}},
            {"trace_id": 1, "span_id": 2, "ts": 20, "seq": 1, "event_type": 4, "span_name": "不能覆盖", "input_text": "盗刷", "logs": ["分析"]},
        ]
        span = _fold(events)
        self.assertEqual(span["span_name"], "开始")
        self.assertEqual(span["logs"], ["相同", "分析", "结束"])
        self.assertEqual(span["attrs"], {"ok": True, "n": 1})
        self.assertEqual(span["ts"], 10)
        self.assertEqual(span["event_count"], 3)
        self.assertTrue(span["has_start"] and span["has_end"])
        self.assertIn("盗刷", _search_text(span))
        self.assertNotIn("开始", _search_text(span))

    def test_vector_validation(self):
        self.assertEqual(_vector_text([0.1, 0.2], 2), "[0.1,0.2]")
        for value in ([0, 0], [float("nan"), 1], [1]):
            with self.assertRaises(ValueError):
                _vector_text(value, 2)

    def test_batch_vectors_validate_before_write_and_bind_tenant(self):
        connection = FakeConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        self.assertEqual(store.set_embeddings([]), 0)
        with self.assertRaises(ValueError):
            store.set_embeddings([(7, 1, [1, 2]), (7, 1, [2, 1])])
        with self.assertRaises(ValueError):
            store.set_embeddings([(7, 1, [1, 2]), (7, 2, [1])])
        with self.assertRaises(ValueError):
            store.set_embeddings([(7, 1, [1, 2])], tenant_id=2)
        self.assertEqual(connection.sql, [])
        self.assertEqual(store.set_embeddings([(7, 1, [1, 2]), (7, 2, [2, 1])]), 2)
        self.assertEqual(len(connection.sql), 1)
        sql, params = connection.sql[0]
        self.assertIn("UPDATE", sql)
        self.assertIn("FROM (VALUES", sql)
        self.assertEqual(params[-1], "1")
        self.assertNotIn("7", sql)
        connection.sql.clear()
        many = [(8, i, [1, 2]) for i in range(257)]
        self.assertEqual(store.set_embeddings(many), 257)
        self.assertEqual(len(connection.sql), 2)
        store.close()

    def test_transactional_ingest_retry_and_out_of_order_event(self):
        connection = SQLiteConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        events = [
            {"trace_id": 7, "span_id": 2, "ts": 30, "seq": 2,
             "event_type": 2, "ext_span_id": "ext-2", "status": 1,
             "attrs": {"active": True}},
            {"trace_id": 7, "span_id": 2, "ts": 10, "seq": 0,
             "event_type": 1, "ext_span_id": "ext-2", "span_name": "分析"},
            {"trace_id": 7, "span_id": 2, "ts": 20, "seq": 1,
             "event_type": 4, "ext_span_id": "ext-2", "logs": ["盗刷"]},
        ]
        self.assertEqual(store.ingest(events), {"ingested": 3})
        self.assertEqual(store.ingest(events), {"ingested": 0})
        span = store.span(7, 2)
        self.assertEqual(span["span_name"], "分析")
        self.assertEqual(span["logs"], ["盗刷"])
        self.assertEqual(span["status"], 1)
        self.assertEqual(span["event_count"], 3)
        self.assertEqual(store.trace(7), [span])
        # Same event identity with a wrong span ID must not leave an empty span row.
        self.assertEqual(store.ingest([{**events[0], "span_id": 3}]), {"ingested": 0})
        self.assertIsNone(store.span(7, 3))
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_spans").fetchone()[0], 1)
        row = connection.db.execute("SELECT value_json FROM fuju_trace_attrs WHERE attr_key='active'").fetchone()
        self.assertEqual(row, ("true",))
        with self.assertRaises(ValueError):
            store.ingest([{**events[0], "tenant_id": 2}])
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_events").fetchone()[0], 3)
        store.close()

    def test_session_scoped_trace_pages_and_tenant_boundary(self):
        connection = SQLiteConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        events = [
            {"trace_id": "task-a", "span_id": "api", "session_id": "task-a",
             "ext_span_id": "task-a-api", "ts": 10, "seq": 1, "event_type": 1,
             "span_name": "query.api.accept", "attrs": {"project_id": "p"}},
            {"trace_id": "task-a", "span_id": "worker", "session_id": "task-a",
             "ext_span_id": "task-a-worker", "ts": 20, "seq": 1, "event_type": 1,
             "span_name": "database_agent", "attrs": {"project_id": "p"}},
            {"trace_id": "task-b", "span_id": "api", "session_id": "task-b",
             "ext_span_id": "task-b-api", "ts": 30, "seq": 1, "event_type": 1,
             "span_name": "query.api.accept", "attrs": {"project_id": "p"}},
        ]
        self.assertEqual(store.ingest(events), {"ingested": 3})
        self.assertTrue(store.ping())
        filtered = {"externalSessionId": "task-a", "projectId": "p"}
        first = store.list_spans(filters=filtered, limit=1)
        second = store.list_spans(filters=filtered, limit=1, cursor=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual([first["items"][0]["span_id"], second["items"][0]["span_id"]],
                         ["api", "worker"])
        self.assertEqual(store.list_trace_ids(filters=filtered), {"items": ["task-a"], "total": 1})
        store.ingest([{"trace_id": "task-a-2", "span_id": "api", "session_id": "task-a",
                       "ext_span_id": "task-a-2-api", "ts": 25, "seq": 1,
                       "event_type": 1, "attrs": {"project_id": "p"}}])
        self.assertEqual(store.list_trace_ids(filters=filtered),
                         {"items": ["task-a", "task-a-2"], "total": 2})
        self.assertEqual(store.list_spans(filters={"externalTraceId": "task-b"})["total"], 1)
        with self.assertRaises(ValueError):
            store.list_spans(filters=filtered, tenant_id=2)
        store.close()

    def test_retention_removes_only_whole_expired_traces(self):
        connection = SQLiteConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        events = [
            {"trace_id": "old", "span_id": "a", "ext_span_id": "old-a",
             "ts": 10, "seq": 1, "event_type": 1},
            {"trace_id": "old", "span_id": "a", "ext_span_id": "old-a",
             "ts": 20, "seq": 2, "event_type": 2},
            {"trace_id": "active", "span_id": "a", "ext_span_id": "active-a",
             "ts": 10, "seq": 1, "event_type": 1},
            {"trace_id": "active", "span_id": "b", "ext_span_id": "active-b",
             "ts": 100, "seq": 1, "event_type": 1},
        ]
        store.ingest(events)
        self.assertEqual(store.prune_before(50), {"traces": 1, "events": 2})
        self.assertEqual(store.trace("old"), [])
        self.assertEqual(len(store.trace("active")), 2)
        self.assertEqual(store.prune_before(50), {"traces": 0, "events": 0})
        store.close()

    def test_many_spans_bulk_write_and_mixed_retry(self):
        connection = SQLiteConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        events = []
        for i in range(40):
            base = {"trace_id": i // 10 + 1, "span_id": i % 10 + 1,
                    "ext_span_id": f"span-{i}", "ts": i * 10}
            events.extend([
                {**base, "seq": 0, "event_type": 1, "span_name": f"step-{i}",
                 "attrs": {"project_id": "a" if i % 2 == 0 else "b"}},
                {**base, "seq": 1, "event_type": 4, "logs": [f"log-{i}"]},
                {**base, "seq": 2, "event_type": 2, "status": i % 2},
            ])
        self.assertEqual(store.ingest(events), {"ingested": 120})
        self.assertLessEqual(connection.sql_calls, 10)  # 40 spans in one bounded SQL batch
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_spans").fetchone()[0], 40)
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_attrs").fetchone()[0], 40)
        self.assertEqual(store.span(4, 10)["logs"], ["log-39"])
        # A mixed call contains one new event and a duplicate with a wrong span ID.
        changed = {"trace_id": 4, "span_id": 10, "ext_span_id": "span-39",
                   "ts": 400, "seq": 3, "event_type": 4,
                   "logs": ["new"], "attrs": {"project_id": "c"}}
        wrong = {**events[0], "span_id": 99}
        self.assertEqual(store.ingest([changed, wrong]), {"ingested": 1})
        self.assertIsNone(store.span(1, 99))
        self.assertEqual(store.span(4, 10)["event_count"], 4)
        self.assertEqual(store.span(4, 10)["attrs"], {"project_id": "c"})
        self.assertEqual(store.ingest(events), {"ingested": 0})
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_spans").fetchone()[0], 40)
        store.close()

    def test_bulk_ingest_crosses_statement_boundary(self):
        connection = SQLiteConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        events = [
            {"trace_id": 1, "span_id": i + 1, "ext_span_id": f"many-{i}",
             "ts": i, "seq": 0, "event_type": 1,
             "attrs": {"project_id": "a", "ordinal": i}}
            for i in range(260)
        ]
        self.assertEqual(store.ingest(events), {"ingested": 260})
        self.assertLess(connection.sql_calls, 25)
        self.assertEqual(store.ingest(events), {"ingested": 0})
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_events").fetchone()[0], 260)
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_spans").fetchone()[0], 260)
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_attrs").fetchone()[0], 520)
        self.assertEqual(store.span(1, 260)["attrs"]["ordinal"], 259)
        store.close()

    def test_bulk_ingest_rolls_back_events_and_projection(self):
        connection = SQLiteConnection()
        connection.db.execute("""CREATE TRIGGER reject_attr BEFORE INSERT ON fuju_trace_attrs
            WHEN NEW.attr_key = 'reject' BEGIN SELECT RAISE(ABORT, 'reject'); END""")
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2,
                                connection_factory=lambda _: connection)
        event = {"trace_id": 1, "span_id": 1, "ext_span_id": "rollback",
                 "ts": 1, "seq": 0, "event_type": 1, "attrs": {"reject": True}}
        with self.assertRaises(sqlite3.IntegrityError):
            store.ingest([event])
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_events").fetchone()[0], 0)
        self.assertEqual(connection.db.execute("SELECT count(*) FROM fuju_trace_spans").fetchone()[0], 0)
        store.close()

    def test_sdk_connect_routes_to_optional_adapter(self):
        connection = FakeConnection()
        store = connect(vexdb_dsn="unused", tenant_id=1, vector_dim=2,
                        connection_factory=lambda _: connection)
        self.assertIsInstance(store, VexDBTraceStore)
        self.assertEqual(store.capabilities()["vector_dim"], 2)
        with self.assertRaises(ValueError):
            connect(vexdb_dsn="unused", path="./data", tenant_id=1, vector_dim=2)
        with self.assertRaises(ValueError):
            connect(vexdb_dsn="unused", tenant_id=1)
        store.close()

    def test_sdk_connect_accepts_keyword_database_parameters(self):
        connection = FakeConnection()
        received = {}

        def factory(**params):
            received.update(params)
            return connection

        params = {"host": "localhost", "port": 5432, "dbname": "traces",
                  "user": "tester", "password": "secret@value"}
        store = connect(vexdb_params=params, tenant_id=1, vector_dim=3,
                        connection_factory=factory)
        self.assertIsInstance(store, VexDBTraceStore)
        self.assertEqual(received, params)
        store.close()
        with self.assertRaises(ValueError):
            connect(vexdb_params=params, vexdb_dsn="unused", tenant_id=1, vector_dim=3)
        with self.assertRaises(ValueError):
            connect(vexdb_params=params, path="./data", tenant_id=1, vector_dim=3)

    def test_native_queries_rrf_and_exact_filter(self):
        connection = FakeConnection()
        store = VexDBTraceStore("unused", tenant_id=1, vector_dim=2, connection_factory=lambda _: connection)
        hits = store.search(text="盗刷", vector=[1, 2], k=3,
                            filter={"attrs": {"project_id": "a", "active": True}})
        self.assertEqual([hit["span_id"] for hit in hits], [2, 1, 3])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(len(connection.sql), 3)  # text, vector, one hydration query
        sql = "\n".join(statement for statement, _ in connection.sql)
        self.assertIn("@~@ %s", sql)
        self.assertIn("bm25_score()", sql)
        self.assertIn("<=> %s::floatvector", sql)
        self.assertIn("a.value_hash=%s AND a.value_json=%s", sql)
        text_params = connection.sql[0][1]
        self.assertEqual(text_params[0], "1")
        self.assertNotIn("盗刷", connection.sql[0][0])
        self.assertIn('"a"', text_params)
        self.assertIn("true", text_params)
        with self.assertRaises(ValueError):
            store.search(text="x", tenant_id=2)
        with self.assertRaises(ValueError):
            store.search(text="x", filter={"tenant_id": 2})
        with self.assertRaises(ValueError):
            store.search(text="x", filter={"unknown": 1})
        with self.assertRaises(ValueError):
            store.search(text="x", unknown=1)


if __name__ == "__main__":
    unittest.main()
