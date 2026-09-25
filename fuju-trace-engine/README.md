# Fuju Trace Engine

An embeddable Rust TraceDB engine built on the standard library. Applications call it in-process through the Python, Node, or Rust bindings. The engine stores original events, folds them into spans, and provides filtered text, vector, and hybrid search.

Core storage uses a WAL, immutable segments, manifest snapshots, and deterministic event IDs. Derived indexes can be rebuilt from original events. The local engine includes Chinese BM25 and a disk-backed vector index. VexDB is a separate Python adapter and uses VexDB's native indexes.

```bash
cargo test --offline
cargo run -p fuju-trace-engine --example demo --offline
cargo run -p fuju-trace-engine --example bench_qps --release
```

The `EngineJsonApi` route dispatcher is an in-process boundary used by embedded bindings; it does not listen on a socket. Recovery behavior is checked by `../tests/crash_recovery_kill9.sh`.
