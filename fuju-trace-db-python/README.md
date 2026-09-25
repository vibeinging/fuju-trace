# fuju-trace-db

Embedded Fuju TraceDB for Python. This wheel runs the Rust engine in the Python process. It does not start a network service.

```bash
python -m pip install -e ./fuju-trace-sdk/python -e ./fuju-trace-db-python
```

```python
from fuju_trace import DbExporter, Tracer, connect

with connect(path="./trace-data", tenant_id=1) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("request", tenant_id=1) as trace:
        with trace.span("tool call") as span:
            span.log("done")
    tracer.close()
    print(db.search(text="done", k=10))
```

The underlying `FujuTraceDB` also provides `trace()`, `span()`, session queries, filtered search, annotations, retention, and read plans. Multiple processes on one host may share a local data directory; sharing it between hosts or over a network filesystem is unsupported. See [the root README](../README.md) for architecture and [current state](../docs/CURRENT_STATE.md) for limits.

```bash
python -m pytest
```
