# @fuju/trace-sdk

TypeScript instrumentation SDK for Agent traces. It creates deterministic events and sends them to an `Exporter` supplied by the application.

```ts
import { ConsoleExporter, Tracer } from "@fuju/trace-sdk";

const tracer = new Tracer(new ConsoleExporter(), 1, "planner");
tracer.trace("request", (trace) => {
  trace.span("planner.route", { displayName: "Plan next step" }, (span) => {
    span.log("ready");
  });
});
await tracer.close();
```

`ConsoleExporter` prints events for debugging; `CollectingExporter` stores them in memory. Implement `Exporter.exportBatch()` to write batches into your application's database adapter. `BatchExporter` batches events for that sink. `Tracer.close()` awaits an asynchronous sink's final write.

The SDK uses `BigInt` for 64-bit IDs and serializes them as strings in `toWire()`. Its `eventId(extSpanId, seq, eventType)` matches the Python SDK and Rust engine byte-for-byte.

```bash
npm run build
npm test
```
