# Fuju Trace

**Searchable run replay for AI agents.** Fuju Trace turns an agent run into a connected record of model calls, tool calls, outputs, errors, and token use. Add a tracing SDK to replay what happened and find related runs; add embedded storage only when your app needs local queries.

[中文](README.zh-CN.md) · English · [MIT license](LICENSE)

> **Repository status:** Fuju Trace now has its own public source repository. The renamed Python and npm packages are configured but have not been published. The source quick start below works without a package registry. See [Current State](docs/CURRENT_STATE.md) for verified capabilities and release status.

![Fuju Trace replay console](docs/images/console-overview.png)

## Why Fuju Trace

A log records an event. To diagnose an agent run, you also need to know which steps belonged to that run, how they called one another, and where the result changed. Fuju Trace keeps that structure as traces and spans; each span can still contain logs.

- **Explain a result or failure:** Replay a session and follow nested model and tool calls, their inputs and outputs, errors, timing, and token use.
- **Find patterns across runs:** Search Chinese text with BM25, use filtered vector or hybrid search, and narrow results by tenant, agent, status, time, or supported attributes. Mark useful runs with annotations and dataset associations for later eval work.
- **Start small, then choose where data lives:** Instrument Python, TypeScript, or Rust, or send OTLP/HTTP JSON. Query a local HTTP service or embed the same Rust engine in Python, Node/Electron, or Rust.
- **Handle retries and restarts:** Deterministic event IDs let the engine recognize repeated events. The durable engine uses a WAL, snapshots, and recovery; its core has no third-party Rust dependencies.

Fuju Trace stores execution evidence. Prompt optimization and independent acceptance live in the separate `fuju-rsi` project.

## Try it from source

Run these commands from the repository root. You need Rust 1.80+ and Python 3.8+. The demo server includes sample traces and keeps data **in memory**; use an embedded DB or the Python DB server for durable storage.

**Terminal 1 — start the demo server:**

```bash
cargo run --offline --manifest-path fuju-trace-engine/Cargo.toml \
  -p fuju-trace-engine --example server
```

**Terminal 2 — send a trace with the Python SDK, then search for it:**

```bash
PYTHONPATH=fuju-trace-sdk/python python3 - <<'PY'
from fuju_trace import HttpExporter, Tracer

tracer = Tracer(
    exporter=HttpExporter("http://127.0.0.1:7878/v1/ingest", tenant_id=1),
    node_id=1,
)
with tracer.trace("风控复核", tenant_id=1) as trace:
    with trace.span("查询交易") as span:
        span.log("疑似盗刷")
tracer.close()
PY

curl -fsS http://127.0.0.1:7878/v1/search \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: 1' \
  -d '{"text":"盗刷","k":10}'
```

`tracer.close()` flushes buffered events. Use the same `X-Tenant-Id` on writes and reads. The full wire format, search filters, authentication, and OTLP endpoint are in the [API reference](docs/API_REFERENCE.md).

### Open the replay console

A fresh source checkout must build the frontend before the Rust binary can embed it. Run this from the repository root, **then restart** the demo server:

```bash
npm --prefix fuju-trace-console ci
VITE_API=http npm --prefix fuju-trace-console run build
python3 scripts/sync_console.py
```

Open [http://127.0.0.1:7878/](http://127.0.0.1:7878/). The console uses the same `/v1/*` API as other clients.

## Choose an integration

The names below are the **planned package names**, not currently published install targets. Use the linked source packages for local development.

| Your app | Package / source | Use it for |
|---|---|---|
| Python sends traces to a server | `fuju-trace` · [Python SDK](fuju-trace-sdk/python/README.md) | Lightweight, standard-library tracing |
| TypeScript sends traces to a server | `@fuju/trace-sdk` · [TypeScript SDK](fuju-trace-sdk/typescript/README.md) | Browser/Node tracing |
| Rust sends traces to a server | [Rust SDK](fuju-trace-sdk/rust/README.md) | Standard-library tracing |
| Python writes and queries locally | `fuju-trace` + `fuju-trace-db` · [Python DB](fuju-trace-db-python/README.md) | Embedded DB, optional FastAPI server |
| Node or Electron writes and queries locally | `@fuju/trace-db` · [Node DB](fuju-trace-node/README.md) | Embedded Rust engine through Node-API |
| Rust writes and queries locally | [Rust DB](fuju-trace-db-rs/README.md) | Embedded engine wrapper |
| Existing OpenTelemetry/OpenInference app | `POST /v1/traces` · [API reference](docs/API_REFERENCE.md) | OTLP/HTTP JSON ingestion |

For example, install the Python source packages from the repository root to use embedded storage:

```bash
python3 -m pip install ./fuju-trace-sdk/python ./fuju-trace-db-python
```

`fuju-trace-db` builds a native Rust extension. Once installed, open one DB handle per process and reuse it:

```python
from fuju_trace import DbExporter, Tracer, connect

with connect(path="./fuju-trace-data", tenant_id=1) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("风控复核", tenant_id=1) as trace:
        with trace.span("查询交易") as span:
            span.log("疑似盗刷")
    tracer.close()
    print(db.search(text="盗刷", k=10))
```

For FastAPI, ARQ, or Celery, initialize once when each process starts and close once when it exits; see the [Python DB guide](fuju-trace-db-python/README.md). For Node/Electron local tarballs, build the native package and run `npm run pack:verify` in [`fuju-trace-node/`](fuju-trace-node/README.md).

**Deployment boundary:** Embedded mode supports processes sharing one **local** data directory on the same machine. For multiple machines or hosts, run one service and connect over HTTP. Do not share an embedded data directory over a network filesystem.

## How it works

SDK and OTLP events enter through HTTP or the in-process `EngineJsonApi`. The engine writes them to a WAL and segments, then folds start, log, and end events into complete spans when reading. Search, replay, and the console read the same underlying traces. Event IDs are deterministic:

```text
event_id = hash(ext_span_id, seq, event_type)
```

This lets retries and WAL replay recognize the same event. For retrieval, the default engine combines Chinese word tokenization and BM25 with an on-disk graph vector index; hybrid search fuses the two result sets with RRF. The caller supplies embeddings when vector search is needed—the engine does not call an embedding model.

The core Rust engine uses only the standard library. Native language bindings and optional storage adapters live outside its workspace. See [Current State](docs/CURRENT_STATE.md) for tested behavior and remaining production limits.

## Develop and verify

Run from the repository root:

```bash
cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml
PYTHONPATH=fuju-trace-sdk/python python3 fuju-trace-sdk/python/tests/test_sdk.py
python3 scripts/check_release_versions.py
```

Package-level build, clean-consumer checks, upgrade tests, and release steps are documented in [AGENTS.md](AGENTS.md). Publishing this source repository does not publish the Python and npm packages.

## Project map

- [`fuju-trace-engine/`](fuju-trace-engine/) — Rust engine and HTTP examples
- [`fuju-trace-sdk/`](fuju-trace-sdk/) — Python, TypeScript, and Rust SDKs
- [`fuju-trace-console/`](fuju-trace-console/) — replay UI
- [`fuju-trace-db-python/`](fuju-trace-db-python/), [`fuju-trace-node/`](fuju-trace-node/), [`fuju-trace-db-rs/`](fuju-trace-db-rs/) — embedded DB packages
- [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) — HTTP and embedded JSON contract
- [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) — current implementation and limits

`fuju-rsi` is a separate project. Its optional telemetry plugin uses Fuju Trace when available and writes local telemetry logs when it is not; Fuju Trace has no runtime dependency on RSI.

## License

[MIT](LICENSE)
