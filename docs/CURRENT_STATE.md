# Current state

Updated 2026-09-26. Fuju Trace is an in-process trace SDK and data adapter project. It offers VexDB, local embedded TraceDB, and separately installable SQLite, DuckDB, and PostgreSQL adapters. The HTTP service, OTLP ingestion endpoint, FastAPI/CLI server, and replay web console have been removed from the current source tree.

## Available paths

- Python: `fuju-trace` SDK, `fuju-trace[vexdb]` for VexDB, and a separately built `fuju-trace-db` wheel for embedded local TraceDB. `connect(vexdb_dsn=...)`, `connect(vexdb_params=...)`, and `connect(path=...)` are the connection forms.
- SQL adapters: `fuju-trace-sqlite`, `fuju-trace-duckdb`, and `fuju-trace-postgresql` are the direct install packages. All use `fuju-trace-sql` for `connect(sqlite_path=...)`, `connect(duckdb_path=...)`, and `connect(postgresql_dsn=.../postgresql_params=...)`. The shared implementation supports transactional event storage, folded span reads, exact filters, and substring text search. See `fuju-trace-sql/README.md`.
- TypeScript and Rust: instrumentation SDKs with exporter interfaces. Node/Electron uses `@fuju/trace-db`; Rust uses `fuju-trace-db` for embedded storage.
- Engine: WAL recovery, trace folding, Chinese BM25, disk-backed vector search, filtering, evaluation and retention functions. Embedded bindings call `EngineJsonApi` in-process. Its route strings are private dispatch keys, not network endpoints.
- VexDB: original event table plus folded span and attribute read models. VexDB's native BM25 and vector indexes handle retrieval; vectors are supplied by the caller. Text-only operation needs no embeddings.

## Limits and verification

- VexDB adapter does not implement every local TraceDB read and management method. See `fuju-trace-vexdb/README.md` for its exact surface and concurrency behavior.
- The new SQL adapters do not yet have native full-text or vector search. SQLite and DuckDB have real file database tests; PostgreSQL has passed a disposable local PostgreSQL 16 integration run, and CI is configured with a PostgreSQL service. Target deployment versions and production workloads still need separate checks. MySQL is outside the current support scope.
- Same-host processes may share the local TraceDB data directory. Cross-host or network filesystem sharing is unsupported.
- The source remains alpha. Test on the actual data shape and database version before production use.
- Core checks: `cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml`, `./scripts/package_mode_eval.sh`, VexDB unit and live smoke tests, and `./tests/crash_recovery_kill9.sh 3`.

Python SDK `fuju-trace==0.1.11`, VexDB adapter, shared SQL implementation, and the three separately named SQL backend packages are published to PyPI at version 0.1.11. The native `fuju-trace-db` wheel is not part of that release. See `docs/reports/2026-09-25_sql-adapter-real-tests.md` for SQL test scope.
