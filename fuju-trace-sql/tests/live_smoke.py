"""Integration checks against a disposable PostgreSQL database.

Creates and removes four tables under a random fuju_smoke_* prefix.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "fuju-trace-sdk" / "python"))

from fuju_trace import DbExporter, Tracer, connect


def _event(trace_id: str, span_id: str, ext_span_id: str, seq: int,
           event_type: int, **fields: object) -> dict[str, object]:
    return {
        "trace_id": trace_id, "span_id": span_id, "ext_span_id": ext_span_id,
        "seq": seq, "event_type": event_type, "ts": 100 + seq,
        "tenant_id": 1, **fields,
    }


def main() -> int:
    prefix = "fuju_smoke_" + uuid.uuid4().hex[:12]
    dsn = os.environ.get("POSTGRESQL_DSN")
    if not dsn:
        raise SystemExit("POSTGRESQL_DSN is required")
    db = None
    try:
        db = connect(postgresql_dsn=dsn, tenant_id=1, initialize=True,
                     table_prefix=prefix)
        assert db.ping()

        trace_id = "live-" + uuid.uuid4().hex
        span_id = "span-1"
        source_id = "source-" + uuid.uuid4().hex
        events = [
            _event(trace_id, span_id, source_id, 0, 1, span_name="review",
                   session_id=42, attrs={"project_id": "sql-live"}),
            _event(trace_id, span_id, source_id, 1, 4, logs=["疑似盗刷"]),
            _event(trace_id, span_id, source_id, 2, 2, status=1),
        ]
        assert db.ingest(events[1:]) == {"ingested": 2}
        assert db.ingest(events) == {"ingested": 1}
        assert db.ingest(events) == {"ingested": 0}
        span = db.span(trace_id, span_id)
        assert span is not None and span["event_count"] == 3
        assert span["span_name"] == "review" and span["logs"] == ["疑似盗刷"]
        assert db.trace(trace_id) == [span]
        assert len(db.search(text="盗刷", filter={"project_id": "sql-live", "status": 1})) == 1
        assert db.list_trace_ids(filters={"externalSessionId": 42})["items"] == [trace_id]

        try:
            db.ingest([{**events[0], "span_id": "wrong-span"}])
        except ValueError:
            pass
        else:
            raise AssertionError("conflicting event identity was accepted")
        assert db.span(trace_id, "wrong-span") is None
        assert db.span(trace_id, span_id)["event_count"] == 3

        with connect(postgresql_dsn=dsn, tenant_id=2, table_prefix=prefix) as other_tenant:
            assert other_tenant.trace(trace_id) == []
            assert other_tenant.search(text="盗刷") == []

        concurrent_span = "span-2"
        concurrent_source = "source-" + uuid.uuid4().hex
        db.ingest([_event(trace_id, concurrent_span, concurrent_source, 0, 1,
                          span_name="parallel")])
        barrier = threading.Barrier(2)
        failures: list[Exception] = []

        def write_one(event: dict[str, object]) -> None:
            try:
                with connect(postgresql_dsn=dsn, tenant_id=1, table_prefix=prefix) as writer:
                    barrier.wait(timeout=10)
                    writer.ingest([event])
            except Exception as exc:
                failures.append(exc)

        writers = [
            threading.Thread(target=write_one, args=(
                _event(trace_id, concurrent_span, concurrent_source, 1, 4,
                       logs=["parallel log"]),)),
            threading.Thread(target=write_one, args=(
                _event(trace_id, concurrent_span, concurrent_source, 2, 2,
                       status=0),)),
        ]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=15)
        assert not any(writer.is_alive() for writer in writers), "parallel writers timed out"
        assert not failures, failures
        folded = db.span(trace_id, concurrent_span)
        assert folded is not None and folded["event_count"] == 3
        assert folded["logs"] == ["parallel log"] and folded["has_end"]

        tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
        with tracer.trace("smoke", session_id=42, tenant_id=1) as trace:
            with trace.span("write") as span:
                span.log("fuju sql live smoke")
        tracer.close()
        assert len(db.search(text="fuju sql live smoke")) == 1
        db.close()
        db = None
        with connect(postgresql_dsn=dsn, tenant_id=1, table_prefix=prefix) as reopened:
            assert reopened.ping()
            assert reopened.span(trace_id, span_id)["event_count"] == 3
            assert reopened.span(trace_id, concurrent_span)["event_count"] == 3
        print("postgresql: real write, retry, rollback, tenant, concurrent writers, search, reopen OK")
    finally:
        if db is not None:
            db.close()
        # These names were generated here and validated by the store.
        import psycopg2
        with closing(psycopg2.connect(dsn)) as cleanup:
            with cleanup:
                with cleanup.cursor() as cur:
                    for suffix in ("attrs", "spans", "events", "config"):
                        cur.execute(f"DROP TABLE IF EXISTS {prefix}_{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
