# fuju-trace Rust SDK

A standard-library-only instrumentation SDK for Agent traces. The application supplies an `Exporter` to store events. For an embedded local TraceDB, use `fuju-trace-db`.

```rust
use fuju_trace::{ConsoleExporter, TraceOptions, Tracer};

fn main() -> fuju_trace::Result<()> {
    let mut tracer = Tracer::with_exporter(ConsoleExporter, 1);
    tracer.trace_with_result("risk review", TraceOptions::default().tenant_id(1), |trace| {
        trace.span_result("investigate", |span| {
            span.log("suspicious transaction")?;
            Ok(())
        })
    })?;
    tracer.close()
}
```

`event_id = hash(ext_span_id, seq, event_type)` matches the Python and TypeScript SDKs and Rust engine byte-for-byte. Nested spans inherit context and preserve parent IDs. A failed span writes `SpanEnd` with error status.

```bash
cargo test --offline
```
