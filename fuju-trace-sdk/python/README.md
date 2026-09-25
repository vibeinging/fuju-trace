# Fuju Trace Python SDK

Python 3.8+ SDK for Agent traces. It emits deterministic events and writes them through an Exporter. The base package uses only the Python standard library.

## VexDB

```bash
pip install 'fuju-trace[vexdb]==0.1.9'
```

```python
import os
from fuju_trace import DbExporter, Tracer, connect

with connect(vexdb_dsn=os.environ["VEXDB_DSN"], tenant_id=1,
             vector_dim=3, initialize=True) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("risk review", tenant_id=1) as trace:
        with trace.span("investigate") as span:
            span.log("suspicious transaction")
    tracer.close()
    print(db.search(text="transaction", k=10))
```

The VexDB adapter also accepts `vexdb_params={...}`. `vector_dim` must match the embedding model; text search works without supplying vectors. For batching, use `BufferedDbExporter` and call `flush()` when reads need to see all queued writes. See [the adapter guide](../../fuju-trace-vexdb/README.md).

## Local embedded DB

```bash
python -m pip install -e ./fuju-trace-sdk/python -e ./fuju-trace-db-python
```

Use `connect(path="./trace-data", tenant_id=1)` with the same `DbExporter` and `Tracer` code. The separate `fuju-trace-db` wheel contains the Rust engine.

## Logging and custom sinks

`ConsoleExporter` prints JSON events for local debugging. `CollectingExporter` stores events in memory for tests. `BatchExporter` wraps a sink and forwards full batches. Implement `Exporter.export_batch()` to integrate another database.

```python
from fuju_trace import ConsoleExporter, Tracer

tracer = Tracer(exporter=ConsoleExporter(), node_id=1, agent_name="planner")
with tracer.trace("request", tenant_id=1) as trace:
    with trace.span("planner.route", display_name="Plan next step") as span:
        span.log("ready")
```

`name` becomes the stored `span_name`; `display_name` is optional. `agent_name` identifies the actor. `event_id` is derived from `ext_span_id`, `seq`, and `event_type` and matches the Rust and TypeScript implementations.

## Tests

```bash
python tests/test_sdk.py
python -m unittest discover -s tests
```
