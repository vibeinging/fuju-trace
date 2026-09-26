# Fuju Trace

Fuju Trace records AI Agent runs and lets applications query them directly. The SDK represents a run as traces, spans, and events; storage adapters can write to **VexDB**, local TraceDB, SQLite, DuckDB, or PostgreSQL. The current project does not expose a standalone HTTP/OTLP service or web console.

[中文说明](README.zh-CN.md) · [VexDB connection guide](fuju-trace-vexdb/README.md) · [Current state](docs/CURRENT_STATE.md) · [0.1.11 release status](docs/reports/2026-09-26_python-0.1.11-release.md)

**Installable PyPI release: 0.1.10.** The 0.1.11 source and tag include separately named SQL backend packages, but the PyPI rollout is still in progress. Use the 0.1.10 install commands below until the [release report](docs/reports/2026-09-26_python-0.1.11-release.md) confirms all six 0.1.11 packages are available.

## Why traces

Logs tell you what happened. A trace also shows **which step called which, in what order, how long it took, and what it returned**. Fuju Trace keeps the original start/log/end events and folds them into searchable spans. Use session and trace IDs to follow an execution path, then search text and attributes to find related runs.

Technical properties of this release:

- **Idempotent events:** `event_id` is derived from `ext_span_id`, `seq`, and event type. The Python, TypeScript, and Rust SDKs match the local engine byte-for-byte, so retries and recovery do not count an event twice.
- **Choice of storage:** The Python SDK writes through an `Exporter`. VexDB uses native BM25 and vector indexes; local TraceDB has its own WAL, Chinese BM25, and vector index. The general SQL adapters start with transactional writes and basic text queries.
- **Execution context:** Session, trace, and span relationships remain queryable. Tenant, time, agent, status, and attribute filters are available; exact text, vector, and hybrid features depend on the selected adapter.
- **Optional embeddings:** Text search works without writing vectors. VexDB still requires `vector_dim` at table creation and creates a vector column and index, even for a text-only workload.

## VexDB quick start

Requires Python 3.10 or later:

```bash
python -m pip install 'fuju-trace[vexdb]==0.1.10'
```

```python
import os
from fuju_trace import DbExporter, Tracer, connect

params = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

with connect(vexdb_params=params, tenant_id=1, vector_dim=384,
             initialize=True) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("risk review", session_id=1001, tenant_id=1) as trace:
        with trace.span("investigate transaction") as span:
            span.log("suspicious transaction; manual review needed")
    tracer.close()
    print(db.search(text="transaction", k=10))
```

Set `vector_dim` to your embedding model's actual dimension; `384` is a placeholder. `initialize=True` creates tables and indexes on first use and requires DDL permissions. Later opens may omit it. You can also pass a securely managed `vexdb_dsn`. See the [adapter guide](fuju-trace-vexdb/README.md) for connection parameters, session reads, and text-only use.

The extra installs `fuju-trace-vexdb` and the generic `psycopg2-binary>=2.9.5,<3` driver. Pip reuses a compatible installed version. If your application supplies a compatible `psycopg2` driver, install `fuju-trace-vexdb==0.1.10` separately alongside `fuju-trace==0.1.10`.

## Choose a write and query path

| Need | API | Behavior |
| --- | --- | --- |
| Confirm each write | `DbExporter(db)` | Synchronous and simple for initial integration. |
| Write many sessions continuously | `BufferedDbExporter(db, max_batch=128, drop_when_full=False)` | Batches in the background; call `flush()` before reading, then inspect `health()` for write errors and drops. A successful flush means the queue was processed, not that every event reached the database. |
| Search text only | `db.search(text="transaction", k=10)` | No embedding generation or vector writes needed. |
| Semantic or hybrid search | `db.set_embedding(...)`, then search with `vector` or both `text` and `vector` | Your application supplies the embeddings. |
| Store locally in-process | Build `fuju-trace-db` from source | For same-host applications; the native DB wheel is outside this PyPI release. |

Sessions in one process may share a VexDB store; its single database connection serializes use. Open a separate connection per process and assign distinct `node_id` values (0–1023) to writer processes. The VexDB adapter supports ingest, text/vector/hybrid search, trace/span point reads, and session filters. It does not implement every local TraceDB method.

## Local TraceDB

The Rust engine provides WAL recovery, immutable segments, span folding, Chinese BM25, and a vector index. Build the Python binding from a source checkout:

```bash
python -m pip install -e ./fuju-trace-sdk/python -e ./fuju-trace-db-python
```

Replace the VexDB `connect(...)` call above with `connect(path="./trace-data", tenant_id=1)`; the `Tracer` and `DbExporter` usage stays the same. Processes on one host may share a local data directory. Sharing it across hosts or over a network filesystem is unsupported. The repository also retains Node/Electron and Rust embedded bindings.

## SQLite, DuckDB, and PostgreSQL adapters

The current PyPI release uses the shared `fuju-trace-sql` package for all three databases. Install the matching extra:

```bash
python -m pip install 'fuju-trace[sqlite]==0.1.10'
python -m pip install 'fuju-trace[duckdb]==0.1.10'
python -m pip install 'fuju-trace[postgresql]==0.1.10'
```

The 0.1.11 source adds a direct package for each database. `fuju-trace-sql` remains the shared implementation and a compatible installation path for existing users; the direct packages depend on it. Once the 0.1.11 PyPI rollout is complete, users can install `fuju-trace-sqlite`, `fuju-trace-duckdb`, or `fuju-trace-postgresql` directly, or use the matching `fuju-trace[...]` extra. DuckDB adds the `duckdb` driver; PostgreSQL adds `psycopg2-binary`; SQLite uses Python's standard library. All three share idempotent events, transactional span folding, tenant isolation, session/trace reads, and exact attribute filters. Text search currently uses a `LIKE` substring scan; there is **no BM25 or vector search**. Measure query time on your data before using it at scale. Choose VexDB when native BM25 and vector search are required.

| Database | Direct package in 0.1.11 | Connection form | Typical use |
| --- | --- | --- | --- |
| SQLite | `fuju-trace-sqlite` | `connect(sqlite_path="./trace.sqlite", tenant_id=1, initialize=True)` | Single-host file database |
| DuckDB | `fuju-trace-duckdb` | `connect(duckdb_path="./trace.duckdb", tenant_id=1, initialize=True)` | Local analytics with one writer process |
| PostgreSQL | `fuju-trace-postgresql` | `connect(postgresql_dsn="...", tenant_id=1, initialize=True)` | Services with an existing PostgreSQL instance |

See the [SQL adapter guide](fuju-trace-sql/README.md) for source installation and drivers. SQLite and DuckDB have real file database tests. PostgreSQL has been tested with a temporary local PostgreSQL 16 instance; CI is configured to repeat it against a disposable service database. MySQL is outside the current support scope.

## Release and verification

Version 0.1.10 is the latest **complete** PyPI release. The 0.1.11 code and CI are ready, but PyPI has accepted only `fuju-trace-sqlite` 0.1.11 so far; the DuckDB and PostgreSQL package names are waiting for PyPI's new-project quota to reset. The base SDK, VexDB adapter, and shared SQL implementation are still at 0.1.10 on PyPI. See the [0.1.11 release report](docs/reports/2026-09-26_python-0.1.11-release.md) before installing the new version. The [SQL real database test report](docs/reports/2026-09-25_sql-adapter-real-tests.md) records the storage verification scope.

[Python SDK](fuju-trace-sdk/python/README.md) · [VexDB adapter](fuju-trace-vexdb/README.md) · [Development notes](AGENTS.md) · [MIT license](LICENSE)

`fuju-rsi` is a separate project. The projects are intended to integrate through an optional plugin; Fuju Trace has no runtime dependency on RSI.
