"""Run only with a disposable writable VexDB database and VEXDB_DSN set."""
import os
from concurrent.futures import ThreadPoolExecutor
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "fuju-trace-sdk" / "python"))

from fuju_trace import BufferedDbExporter, DbExporter, Tracer, connect
from fuju_trace_vexdb import VexDBTraceStore


def main():
    dsn = os.environ["VEXDB_DSN"]
    prefix = "ft_smoke_" + uuid.uuid4().hex[:12]
    store = None
    try:
        store = VexDBTraceStore.open(dsn, tenant_id=1, vector_dim=3, table_prefix=prefix, initialize=True)
        events = [
            {"trace_id": 101, "span_id": 201, "ts": 1000, "seq": 0, "event_type": 1,
             "ext_span_id": prefix, "span_name": "风控", "tenant_id": 1},
            {"trace_id": 101, "span_id": 201, "ts": 2000, "seq": 1, "event_type": 4,
             "ext_span_id": prefix, "logs": ["疑似盗刷，人工复核"],
             "attrs": {"project_id": "smoke", "active": True}, "tenant_id": 1},
            {"trace_id": 101, "span_id": 201, "ts": 3000, "seq": 2, "event_type": 2,
             "ext_span_id": prefix, "status": 0, "tenant_id": 1},
        ]
        assert store.ingest(events)["ingested"] == 3
        assert store.ingest(events)["ingested"] == 0
        assert store.ingest([{**events[0], "span_id": 999}])["ingested"] == 0
        assert store.span(101, 999) is None
        assert store.span(101, 201)["event_count"] == 3
        assert len(store.trace(101)) == 1
        assert store.search(text="盗刷", filter={"attrs": {"active": True}})
        assert store.set_embedding(101, 201, [0.1, 0.2, 0.3])
        assert store.search(vector=[0.1, 0.2, 0.3])
        assert store.search(vector=[0.1, 0.2, 0.3], filter={"attrs": {"project_id": "smoke"}})
        assert store.search(vector=[0.1, 0.2, 0.3], filter={"attrs": {"project_id": "missing"}}) == []
        assert store.search(text="盗刷", vector=[0.1, 0.2, 0.3])
        other_tenant = VexDBTraceStore.open(dsn, tenant_id=2, vector_dim=3, table_prefix=prefix)
        try:
            assert other_tenant.trace(101) == []
            assert other_tenant.search(text="盗刷") == []
            tenant_two_events = [{**event, "tenant_id": 2} for event in events]
            assert other_tenant.ingest(tenant_two_events)["ingested"] == 3
            assert other_tenant.span(101, 201)["event_count"] == 3
            assert store.span(101, 201)["event_count"] == 3
        finally:
            other_tenant.close()

        def concurrent_log(seq):
            writer = VexDBTraceStore.open(dsn, tenant_id=1, vector_dim=3, table_prefix=prefix)
            try:
                return writer.ingest([{"trace_id": 102, "span_id": 202,
                    "ts": 5000 + seq, "seq": seq, "event_type": 4,
                    "ext_span_id": prefix + "-concurrent", "logs": [f"log-{seq}"],
                    "tenant_id": 1}])
            finally:
                writer.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert [result["ingested"] for result in pool.map(concurrent_log, (0, 1))] == [1, 1]
        concurrent = store.span(102, 202)
        assert concurrent["event_count"] == 2
        assert concurrent["logs"] == ["log-0", "log-1"]
        assert store.set_embeddings([
            (101, 201, [0.1, 0.2, 0.3]),
            (102, 202, [0.3, 0.2, 0.1]),
            (999, 999, [0.2, 0.2, 0.2]),
        ]) == 2
        assert store.span(999, 999) is None
        assert store.search(vector=[0.3, 0.2, 0.1])
        # Use the public SDK path as well as raw wire events.
        sdk_store = connect(vexdb_dsn=dsn, tenant_id=1, vector_dim=3, table_prefix=prefix)
        try:
            tracer = Tracer(exporter=DbExporter(sdk_store, tenant_id=1), node_id=1)
            with tracer.trace("联调", tenant_id=1) as trace:
                with trace.span("分析") as span:
                    span.log("适配检验")
            tracer.close()
            assert sdk_store.search(text="适配检验")
        finally:
            sdk_store.close()
        buffered = BufferedDbExporter(store, tenant_id=1, max_batch=8,
                                      flush_interval=0.05, drop_when_full=False,
                                      register_atexit=False)
        tracer = Tracer(exporter=buffered, node_id=2)
        with tracer.trace("缓冲联调", tenant_id=1) as trace:
            with trace.span("写入") as span:
                span.log("缓冲写入检验")
        tracer.close()
        health = buffered.health()
        assert health["sent"] > 0 and health["dropped"] == 0 and health["write_errors"] == 0
        assert store.search(text="缓冲写入检验")
        try:
            store.search(text="盗刷", tenant_id=2)
        except ValueError:
            pass
        else:
            raise AssertionError("tenant override was accepted")
        print("VexDB live smoke passed")
    finally:
        if store is not None:
            store.close()
        import psycopg2
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                for table in ("attrs", "events", "spans", "config"):
                    cur.execute(f"DROP TABLE IF EXISTS {prefix}_{table}")
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    main()
