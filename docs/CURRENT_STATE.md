# Current state

Updated 2026-09-25. Fuju Trace is an in-process trace SDK and data adapter project. It offers VexDB and local embedded TraceDB backends. The HTTP service, OTLP ingestion endpoint, FastAPI/CLI server, and replay web console have been removed from the current source tree.

## Available paths

- Python: `fuju-trace` SDK, `fuju-trace[vexdb]` for VexDB, and a separately built `fuju-trace-db` wheel for embedded local TraceDB. `connect(vexdb_dsn=...)`, `connect(vexdb_params=...)`, and `connect(path=...)` are the connection forms.
- TypeScript and Rust: instrumentation SDKs with exporter interfaces. Node/Electron uses `@fuju/trace-db`; Rust uses `fuju-trace-db` for embedded storage.
- Engine: WAL recovery, trace folding, Chinese BM25, disk-backed vector search, filtering, evaluation and retention functions. Embedded bindings call `EngineJsonApi` in-process. Its route strings are private dispatch keys, not network endpoints.
- VexDB: original event table plus folded span and attribute read models. VexDB's native BM25 and vector indexes handle retrieval; vectors are supplied by the caller. Text-only operation needs no embeddings.

## Limits and verification

- VexDB adapter does not implement every local TraceDB read and management method. See `fuju-trace-vexdb/README.md` for its exact surface and concurrency behavior.
- Same-host processes may share the local TraceDB data directory. Cross-host or network filesystem sharing is unsupported.
- The source remains alpha. Test on the actual data shape and database version before production use.
- Core checks: `cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml`, `./scripts/package_mode_eval.sh`, VexDB unit and live smoke tests, and `./tests/crash_recovery_kill9.sh 3`.

Python SDK `fuju-trace==0.1.9` and VexDB adapter `fuju-trace-vexdb==0.1.9` are published to PyPI. The native `fuju-trace-db` wheel is built and tested locally but is not part of this PyPI release. See `docs/reports/2026-09-25_serverless-python-release.md`.
