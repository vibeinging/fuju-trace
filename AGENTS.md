# Fuju Trace development notes

Fuju Trace provides tracing SDKs, a VexDB adapter, and an embedded local TraceDB. Applications write traces directly to a database. The standalone HTTP/OTLP service and replay console are retired.

## Source layout

- `fuju-trace-engine/`: standard-library-only Rust engine, WAL, folding, text/vector search.
- `fuju-trace-sdk/python/`, `typescript/`, `rust/`: instrumentation SDKs.
- `fuju-trace-vexdb/`: optional VexDB adapter, using a compatible `psycopg2` driver.
- `fuju-trace-db-python/`, `fuju-trace-node/`, `fuju-trace-db-rs/`: embedded bindings.
- `docs/CURRENT_STATE.md`: current features and limits.

## Invariants

- Keep the core Rust engine independent of third-party crates and make `cargo test --offline` pass. Heavy native dependencies belong in separate crates or bindings.
- `event_id` is derived from `ext_span_id`, `seq`, and `event_type`; it must match Python, TypeScript, and Rust byte-for-byte. Preserve deduplication through WAL replay and retries.
- Preserve the trait boundaries for tokenizer, vector index, segment storage, and BM25.
- Keep the embedded data path in-process. `EngineJsonApi` uses method/path strings as internal dispatch keys; it must not open a socket.
- For display names, `span_name` is the operation name and `display_name` is only a presentation override. Empty display names fall back to `span_name`; display text does not change event identity or indexing.
- Use a unique `node_id` for each writer process/host. Same-host processes may share a local data directory; cross-host sharing is unsupported.

## Verification

```bash
cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml
python fuju-trace-sdk/python/tests/test_sdk.py
python -m unittest discover -s fuju-trace-vexdb/tests -p 'test_*.py'
./scripts/package_mode_eval.sh
./tests/crash_recovery_kill9.sh 3
```

Use release mode for benchmarks. Run Node, Python DB, and Rust DB package tests after touching their bindings. Confirm packaged wheels and tarballs from a clean consumer before release.

## Git and docs

Use plain language. Name new branches `feature/...` or `hotfix/...`; do not use `codex/...`. Do not commit or push without a clear instruction. Commit messages should describe the change without AI tool names.

Save important analysis, plans, reports, and design notes under `docs/` in the relevant category, updating an existing same-topic document when possible. Use `YYYY-MM-DD_short-description.md` names. For requirements and design specs under `docs/specs/`, follow the specs workflow when that tool is available.
