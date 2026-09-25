# Fuju Trace

Fuju Trace is an AI Agent tracing SDK with an embeddable Trace data layer. An application creates traces and spans in-process, then writes events directly to VexDB or the local TraceDB. Reads use the same database adapter. No separate Trace service is needed.

[中文说明](README.zh-CN.md) · [VexDB setup](fuju-trace-vexdb/README.md)

## VexDB quick start

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

Set `vector_dim` to your embedding model's dimension. Text search works without embeddings. Supply vectors through `db.set_embedding(...)` if you need vector or hybrid search. `initialize=True` creates the tables and indexes on first use. Pass either `vexdb_dsn` or `vexdb_params`; keep credentials outside the repository. The adapter uses the standard `psycopg2` protocol and can reuse an existing compatible `psycopg2-binary` installation.

Use `BufferedDbExporter` for batched writes, with an explicit `flush()` when reads must see the data. Use `DbExporter` when each write must complete synchronously. Sessions in one process may share a VexDB store. Assign distinct `node_id` values across processes or hosts.

## Local embedded TraceDB

From a source checkout, build the native Python binding:

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

The Rust engine provides WAL recovery, span folding, Chinese BM25, a vector index, and filtered search. Processes on the same host may share a local data directory; sharing it between hosts is unsupported. Node/Electron bindings live in `@fuju/trace-db`, and the Rust binding is `fuju-trace-db`.

## Technical properties

- Deterministic `event_id = hash(ext_span_id, seq, event_type)` matches the Python, TypeScript, and Rust SDKs and the engine byte-for-byte. Repeated events are counted once.
- Start, log, and end events fold into spans. The local DB recovers from its WAL and immutable segments.
- Text, vector, and hybrid search support tenant, trace, time, agent, status, and attribute filters. VexDB uses native BM25 and vector indexes; the local engine has its own indexes.
- Exporter and database adapters keep instrumentation separate from storage. The base Python SDK has no database runtime dependency; install the `db` or `vexdb` extra for the chosen backend.

## Repository

- `fuju-trace-sdk/`: Python, TypeScript, and Rust instrumentation SDKs.
- `fuju-trace-vexdb/`: VexDB event store, folded span model, and search adapter.
- `fuju-trace-engine/`: local Rust TraceDB engine.
- `fuju-trace-db-python/`, `fuju-trace-node/`, `fuju-trace-db-rs/`: in-process database bindings.
- `docs/CURRENT_STATE.md`: implementation status and limits.

## Verify

```bash
cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml
./scripts/package_mode_eval.sh
./tests/crash_recovery_kill9.sh 3
```

MIT licensed. This is an alpha release; validate query plans, recall, and write latency with your own workload before production use.
