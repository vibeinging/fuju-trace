# Fuju Trace SQL adapters

This package holds the shared database implementation for three separately
installable Fuju Trace packages: `fuju-trace-sqlite`, `fuju-trace-duckdb`, and
`fuju-trace-postgresql`. It remains directly installable for 0.1.10 users.
The adapters have the same event ingestion
and basic read API as the VexDB adapter, but their current text search is a
portable **substring scan**. They do not provide BM25, ANN, vector writes, or
hybrid search. Use VexDB or the local TraceDB when those search features matter.

Install a backend directly, or through a Fuju Trace extra:

```bash
python -m pip install 'fuju-trace-sqlite==0.1.11'
python -m pip install 'fuju-trace-duckdb==0.1.11'
python -m pip install 'fuju-trace-postgresql==0.1.11'

python -m pip install 'fuju-trace[sqlite]==0.1.11'
python -m pip install 'fuju-trace[duckdb]==0.1.11'
python -m pip install 'fuju-trace[postgresql]==0.1.11'
```

## Install from this checkout

```bash
python -m pip install -e ./fuju-trace-sdk/python -e ./fuju-trace-sql
# Add one or more optional drivers when needed:
python -m pip install 'duckdb>=1.0,<3' 'psycopg2-binary>=2.9.5,<3'
```

SQLite uses Python's standard library. The DuckDB and PostgreSQL packages
install their respective database drivers. Existing users can still install
`fuju-trace-sql[duckdb]==0.1.11` or
`fuju-trace-sql[postgresql]==0.1.11` directly.

## Connect

```python
from fuju_trace import DbExporter, Tracer, connect

with connect(sqlite_path="./trace.sqlite", tenant_id=1, initialize=True) as db:
    tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
    with tracer.trace("request", session_id=42, tenant_id=1) as trace:
        with trace.span("tool call") as span:
            span.log("query failed")
    tracer.close()
    print(db.search(text="failed", k=10))
    print(db.list_spans(filters={"externalSessionId": 42}))
```

Other connection forms:

```python
connect(duckdb_path="./trace.duckdb", tenant_id=1, initialize=True)
connect(postgresql_dsn="dbname=app user=trace", tenant_id=1, initialize=True)
connect(postgresql_params={"host": "localhost", "dbname": "app",
                           "user": "trace"}, tenant_id=1, initialize=True)
```

Supply credentials from your secret manager or environment, never from source
control. Set `initialize=True` only for first setup; it creates private
`fuju_trace_*` tables and indexes. Use `table_prefix` to isolate installations.
Opening a database without the schema requires initialization first.

## API and limits

- `ingest(events)` is transactional and deduplicates
  `(ext_span_id, seq, event_type)` within the bound tenant. The source event
  and folded span are committed together.
- `trace(id)`, `span(trace_id, span_id)`, `list_spans(filters=...)`, and
  `list_trace_ids(filters=...)` read the folded model.
- `search(text=..., k=..., filter=...)` searches log and input/output text.
  Supported filters include trace ID, session ID, time, agent, status, and
  exact attributes. `%` and `_` in a query are treated literally.
- `capabilities()` reports `text="substring_scan"` and `vector=None`.
  Passing a vector raises `NotImplementedError`.
- Each store binds one tenant and serializes one connection across threads.
  Give each process its own connection and each writer process a distinct
  `node_id` (0–1023).
- SQLite enables WAL for file databases; concurrent writes still serialize.
  DuckDB is best used by one writer process for a database file. PostgreSQL
  uses row locks when folding spans.
- The substring search has no text index and can scan many spans. It is a
  correctness-first baseline, not a large-scale search benchmark result.

For DuckDB's file concurrency behavior and FTS index refresh requirement, see
its [concurrency](https://duckdb.org/docs/stable/connect/concurrency.html) and
[full-text search](https://duckdb.org/docs/current/core_extensions/full_text_search)
documentation.

## Verification

```bash
python -m unittest discover -s fuju-trace-sql/tests -p 'test_*.py'
```

SQLite and DuckDB tests use real database files. They cover writes, retries,
filters, tenant isolation, and reopen. The PostgreSQL test below uses a real
server and covers the same path plus two connections writing one span at the
same time. No MySQL server is needed or supported.

To start a temporary local PostgreSQL instance, run the following from the
repository root. It listens only on a private Unix socket and deletes its data
directory after the test. `initdb`, `pg_ctl`, `createdb`, and a Python with
`psycopg2` must be installed:

```bash
FUJU_SQL_PYTHON=python3 ./fuju-trace-sql/tests/run_local_postgresql.sh
```

For an existing **disposable test database** with table create/drop permission,
set `POSTGRESQL_DSN` and run `python fuju-trace-sql/tests/live_smoke.py`.
The test creates and removes only four tables under its own random
`fuju_smoke_*` prefix. CI runs it against a PostgreSQL 16 service container.
These checks verify storage behavior; they do not benchmark search performance
or exercise VexDB-Lite vector extensions.
The [real database test report](../docs/reports/2026-09-25_sql-adapter-real-tests.md)
records the versions and results from the local run.
