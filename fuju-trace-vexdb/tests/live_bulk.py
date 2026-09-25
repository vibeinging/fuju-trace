"""Check VexDB multi-row SQL on both sides of the 256-row statement bound."""
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fuju_trace_vexdb import VexDBTraceStore


def main() -> None:
    dsn = os.environ["VEXDB_DSN"]
    prefix = "ft_bulk_" + uuid.uuid4().hex[:12]
    store = None
    try:
        store = VexDBTraceStore.open(dsn, tenant_id=1, vector_dim=3,
                                      table_prefix=prefix, initialize=True)
        events = [
            {"trace_id": 1, "span_id": i + 1, "ext_span_id": f"{prefix}-{i}",
             "ts": 1000 + i, "seq": 0, "event_type": 1,
             "input_text": f"盗刷排查 {i}",
             "attrs": {"project_id": "bulk", "ordinal": i}}
            for i in range(260)
        ]
        assert store.ingest(events) == {"ingested": 260}
        assert store.ingest(events) == {"ingested": 0}
        assert len(store.trace(1)) == 260
        assert store.span(1, 260)["attrs"]["ordinal"] == 259
        assert store.search(text="盗刷", k=10, filter={"attrs": {"project_id": "bulk"}})
        changed = store.set_embeddings([(1, i + 1, [1.0, float(i + 1), 1.0])
                                        for i in range(260)])
        assert changed == 260
        assert store.search(vector=[1.0, 260.0, 1.0], k=10)
        assert store.search(text="盗刷", vector=[1.0, 260.0, 1.0], k=10)
        print("VexDB live bulk boundary passed")
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
