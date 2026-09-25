import sys
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "fuju-trace-sdk" / "python"))

from fuju_trace import DbExporter, Tracer, connect
from fuju_trace_sql import (
    DuckDBTraceStore, PostgreSQLTraceStore, SQLiteTraceStore,
)


def _events():
    return [
        {"trace_id": "trace-1", "span_id": "span-1", "ext_span_id": "source-1",
         "event_type": 1, "seq": 0, "ts": 10, "tenant_id": 1,
         "span_name": "plan", "session_id": 42, "agent_name": "planner",
         "attrs": {"project_id": "demo", "retry": True}},
        {"trace_id": "trace-1", "span_id": "span-1", "ext_span_id": "source-1",
         "event_type": 4, "seq": 1, "ts": 11, "tenant_id": 1,
         "logs": ["疑似盗刷"], "attrs": {"step": 2}},
        {"trace_id": "trace-1", "span_id": "span-1", "ext_span_id": "source-1",
         "event_type": 2, "seq": 2, "ts": 12, "tenant_id": 1, "status": 1},
    ]


class SQLiteTests(unittest.TestCase):
    def test_sdk_round_trip_retry_filters_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "trace.sqlite"
            with connect(sqlite_path=db_path, tenant_id=1, initialize=True) as db:
                self.assertTrue(db.ping())
                self.assertEqual(db.ingest(_events()[1:]), {"ingested": 2})
                self.assertEqual(db.ingest(_events()), {"ingested": 1})
                self.assertEqual(db.ingest(_events()), {"ingested": 0})
                span = db.span("trace-1", "span-1")
                self.assertEqual(span["span_name"], "plan")
                self.assertEqual(span["logs"], ["疑似盗刷"])
                self.assertEqual(span["event_count"], 3)
                self.assertEqual(db.trace("trace-1"), [span])
                query = {"text": "盗刷", "filter": {
                    "externalSessionId": 42, "agent_name": "planner",
                    "status": 1, "attrs": {"project_id": "demo", "retry": True},
                }}
                self.assertEqual(len(db.search(query)), 1)
                self.assertEqual(db.search(text="盗%", k=10), [])
                self.assertEqual(db.list_spans(filters={"externalSessionId": 42})["total"], 1)
                self.assertEqual(db.list_trace_ids(filters={"externalSessionId": 42})["items"], ["trace-1"])
                with self.assertRaises(ValueError):
                    db.search(text="盗刷", filter={"tenant_id": 2})
                with self.assertRaises(ValueError):
                    db.span("trace-1", "span-1", tenant_id=2)
                with self.assertRaises(NotImplementedError):
                    db.search(vector=[1.0, 0.0])
                self.assertEqual(db.capabilities()["vector"], None)
            with SQLiteTraceStore.open(db_path, tenant_id=1, initialize=True) as reopened:
                self.assertEqual(reopened.span("trace-1", "span-1")["event_count"], 3)
            with SQLiteTraceStore.open(db_path, tenant_id=2) as other:
                self.assertEqual(other.trace("trace-1"), [])

    def test_transaction_rollback_and_conflicting_retry(self):
        with SQLiteTraceStore.open(":memory:", tenant_id=1, initialize=True) as db:
            with self.assertRaises(ValueError):
                db.ingest([_events()[0], {**_events()[1], "tenant_id": 2}])
            self.assertEqual(db.list_spans()["total"], 0)
            with self.assertRaises(TypeError):
                db.ingest([{**_events()[0], "logs": [["invalid log"]]}])
            self.assertEqual(db.list_spans()["total"], 0)
            db.ingest([_events()[0]])
            with self.assertRaises(ValueError):
                db.ingest([{**_events()[0], "span_id": "another"}])
            self.assertEqual(db.list_spans()["total"], 1)
            self.assertIsNone(db.span("trace-1", "another"))

    def test_sdk_exporter_and_concurrent_sessions(self):
        with SQLiteTraceStore.open(":memory:", tenant_id=1, initialize=True) as db:
            failures = []
            def write(node):
                try:
                    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=node)
                    with tracer.trace(f"request-{node}", session_id=node, tenant_id=1) as trace:
                        with trace.span("work") as span:
                            span.log(f"log-{node}")
                    tracer.close()
                except Exception as exc:
                    failures.append(exc)
            threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(failures)
            self.assertEqual(db.list_spans()["total"], 4)

    def test_validation_and_no_silent_vector_claim(self):
        with SQLiteTraceStore.open(":memory:", tenant_id=1, initialize=True) as db:
            event = _events()[0]
            with self.assertRaises(ValueError):
                db.ingest([{**event, "event_id": 42}])
            with self.assertRaises(ValueError):
                db.ingest([{**event, "ext_span_id": None}])
            self.assertEqual(db.list_spans()["total"], 0)
        with self.assertRaises(ValueError):
            connect(sqlite_path=":memory:", duckdb_path=":memory:", tenant_id=1)
        with self.assertRaises(ValueError):
            connect(sqlite_path=":memory:", tenant_id=None)


class DriverRoutingTests(unittest.TestCase):
    def test_postgresql_dml_through_dbapi_compatibility_layer(self):
        # SQLite executes equivalent SQL here; real server smoke is separate.
        class Cursor:
            def __init__(self, inner):
                self.inner = inner
            def execute(self, sql, params=()):
                sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
                return self.inner.execute(sql, params)
            def fetchone(self):
                return self.inner.fetchone()
            def fetchall(self):
                return self.inner.fetchall()
            def close(self):
                self.inner.close()
        class Connection:
            def __init__(self, inner):
                self.inner = inner
            def cursor(self):
                return Cursor(self.inner.cursor())
            def commit(self):
                self.inner.commit()
            def rollback(self):
                self.inner.rollback()
            def close(self):
                self.inner.close()
        raw = sqlite3.connect(":memory:")
        SQLiteTraceStore(raw, tenant_id=1).initialize()
        wrapped = Connection(raw)
        with PostgreSQLTraceStore.open(
            "dbname=unused", tenant_id=1,
            connection_factory=lambda _: wrapped,
        ) as db:
            self.assertEqual(db.ingest(_events()), {"ingested": 3})
            self.assertEqual(db.ingest(_events()), {"ingested": 0})
            self.assertEqual(db.list_trace_ids()["items"], ["trace-1"])
            self.assertEqual(len(db.search(text="盗刷", filter={"project_id": "demo"})), 1)

    def test_postgresql_driver_routing_without_connecting(self):
        class Connection:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
        pg = Connection()
        captured = []
        with PostgreSQLTraceStore.open("dbname=trace", tenant_id=1,
                                       connection_factory=lambda dsn: (captured.append(dsn), pg)[1]) as db:
            self.assertEqual(db.capabilities()["backend"], "postgresql")
        self.assertTrue(pg.closed)
        self.assertEqual(captured[0], "dbname=trace")

    def test_duckdb_round_trip_if_installed(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb optional driver is not installed")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.duckdb"
            with connect(duckdb_path=path, tenant_id=1, initialize=True) as db:
                self.assertEqual(db.ingest(_events()[1:]), {"ingested": 2})
                self.assertEqual(db.ingest(_events()), {"ingested": 1})
                self.assertEqual(db.ingest(_events()), {"ingested": 0})
                self.assertEqual(len(db.search(text="盗刷", filter={"externalSessionId": 42})), 1)
                self.assertEqual(db.span("trace-1", "span-1")["event_count"], 3)
                with self.assertRaises(ValueError):
                    db.ingest([{**_events()[0], "span_id": "wrong-span"}])
                self.assertIsNone(db.span("trace-1", "wrong-span"))
            with DuckDBTraceStore.open(path, tenant_id=1) as db:
                self.assertEqual(db.list_trace_ids()["items"], ["trace-1"])
                self.assertEqual(db.span("trace-1", "span-1")["logs"], ["疑似盗刷"])
            with DuckDBTraceStore.open(path, tenant_id=2) as other:
                self.assertEqual(other.trace("trace-1"), [])


if __name__ == "__main__":
    unittest.main()
