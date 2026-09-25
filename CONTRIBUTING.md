# Contributing to Fuju Trace

The source tree contains Python, TypeScript, and Rust tracing SDKs; the VexDB adapter; and the local embedded Rust TraceDB with Python, Node, and Rust bindings. Start with [the current state](docs/CURRENT_STATE.md) and [the development notes](AGENTS.md).

Keep core engine changes compatible with `cargo test --offline`. Preserve deterministic event IDs across the three SDK languages and the engine. Add meaningful tests for WAL recovery, deduplication, indexing, and query behavior when changing those paths.

```bash
cargo test --offline --manifest-path fuju-trace-engine/Cargo.toml
./scripts/package_mode_eval.sh
python -m unittest discover -s fuju-trace-vexdb/tests -p 'test_*.py'
```

For release builds, run `scripts/check_release_versions.py`, package each target through `scripts/package_release_artifacts.sh`, and verify wheels/tarballs in a clean consumer. Benchmark in release mode and state the data shape, batch size, and machine with every performance number.

Use `feature/...` or `hotfix/...` branch names and plain commit messages. Keep unrelated local changes out of commits.
