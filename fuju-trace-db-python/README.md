# fuju-trace-db

Embedded Fuju Trace DB for Python agents.

`fuju-trace-db` is the Python equivalent of `@fuju/trace-db`: it embeds the Rust
Fuju Trace engine in the Python process and calls `EngineJsonApi` in-process. It
does not parse Fuju Trace files in Python and does not send embedded calls through
a TCP socket. It can optionally expose the same DB through FastAPI or the
`fuju-trace-db serve` CLI when you want a local server.

## Install

For local development from this repository:

```bash
cd fuju-trace-db-python
python -m pip install -e .
```

Public wheels should be built with maturin per platform:

```bash
cd fuju-trace-db-python
python -m pip install maturin
python -m maturin build --release --interpreter "$(command -v python)"
```

Use `--interpreter` when the machine has multiple Python installs; otherwise
maturin may discover an old system Python instead of the environment you are
building for.

## Test

```bash
python -m pytest
```

From the repository root, run the package-mode eval when changing package
contracts, `connect(path=...)`, FastAPI router behavior, or server-mode docs:

```bash
./scripts/package_mode_eval.sh
```

## Usage

You can use it directly:

```python
from fuju_trace_db import FujuTraceDB, create_span_event_builder

db = FujuTraceDB.open("./data", tenant_id=1)

events = create_span_event_builder({
    "trace_id": "run-uuid",
    "session_id": "session-uuid",
    "attrs": {
        "project_id": "agentic-data",
        "skill": "review",
        "mode": "auto",
    },
})

events.start_span(span_id="span-uuid", name="risk review", input_text="疑似盗刷")
events.log("疑似盗刷", span_id="span-uuid")
events.end_span(span_id="span-uuid", status=0, duration_ns=12_000_000, output_text="needs review")
events.ingest(db)

hits = db.search({"text": "盗刷", "k": 10, "filter": {"attrs": {"project_id": "agentic-data"}}})
span = db.span("run-uuid", "span-uuid")

trajectories = db.trace_trajectories({
    "filter": {"projectId": "agentic-data", "taskFingerprint": "refund-v1"}
})
groups = db.trajectory_groups({
    "filter": {"projectId": "agentic-data", "taskFingerprint": "refund-v1"}
})
diff = db.trace_diff("run-a", "run-b")
loops = db.loops(projectId="agentic-data", taskFingerprint="refund-v1")
task_runs = db.task_traces("refund-v1", validationStatus="pass")

annotation = db.annotate(
    traceId="run-uuid",
    spanId="span-uuid",
    label="best_path",
    score=950,
    source="human",
    attrs={"project_id": "agentic-data", "skill": "review"},
)
db.update_annotation(annotation["annotationId"], status="resolved", reviewer="qa")
db.link_dataset_item(
    datasetId="agentic-regression",
    itemId="case-1",
    traceId="run-uuid",
    spanId="span-uuid",
    split="eval",
    label="pass",
)

plan = db.retention_plan(
    {
        "filter": {"projectId": "agentic-data"},
        "deleteBeforeTs": 100000,
        "protect": {"annotations": True, "datasetAssociations": True},
    }
)
result = db.apply_retention(
    {
        "filter": {"projectId": "agentic-data"},
        "deleteBeforeTs": 100000,
        "requestedBy": "nightly-retention",
    }
)
audits = db.retention_audits(source="nightly-retention")

db.close()
```

Use `with` to close safely:

```python
with FujuTraceDB.open("./data", tenant_id=1) as db:
    print(db.search(text="盗刷", k=10))
```

Use `db.lock_metrics()` when a service feels slow around embedded writes. It
returns whether embedded locking is enabled, lock acquire counts, wait counts,
active waiters, wait milliseconds, timeout counts, stale lock cleanup counts,
and reader pin counts.

Or through the user-facing `fuju-trace` package:

```bash
python -m pip install "fuju-trace[db]"
# Or install the two packages explicitly:
python -m pip install fuju-trace fuju-trace-db
```

```python
from fuju_trace import DbExporter, Tracer, connect

db = connect(path="./data", tenant_id=1)
tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
```

The `fuju-trace` package remains the pure-Python instrumentation SDK and
client facade. Use `fuju_trace` when you want one import for HTTP and local modes.
Use `fuju-trace-db` directly when a Python app needs the embedded DB handle.

## Server Mode

Install optional server dependencies:

```bash
python -m pip install "fuju-trace-db[server]"
```

Expose an embedded DB through FastAPI:

```python
from fastapi import FastAPI
from fuju_trace_db import FujuTraceDB
from fuju_trace_db.fastapi import create_fuju_trace_router

db = FujuTraceDB.open("./data", tenant_id=1)
app = FastAPI()
app.include_router(create_fuju_trace_router(db), prefix="/fuju_trace")
```

Or start the small CLI server:

```bash
fuju-trace-db serve --data-dir ./data --bind 0.0.0.0:7878
```

Embedded mode can be used by multiple local worker processes. Each worker may
call `FujuTraceDB.open("./data")`; the Rust engine serializes open/write paths
inside the data dir. Before each write it refreshes WAL, manifest, and metadata:
an unchanged WAL is skipped, an appended WAL is applied from its tail, and
derived indexes are rebuilt only when the manifest changes. Cross-process reader
pins stop `reclaim()` from physically deleting segment files while another
process still holds a snapshot. Do not share one data directory across machines
or unreliable network filesystems. For multi-host deployments, run one Fuju Trace
server process and send workers to it over HTTP.

The read-model helpers above are single-node implementations. Common filters
such as `project_id`, `skill`, `task_fingerprint`, `loop_id`,
`validation_status`, `tool_name`, and `model` use the attrs sidecar postings and
return `readPlan`. Postings are memory-budgeted: very wide values or total-entry
pressure disable only the affected postings, then queries fall back to the
sidecar rows and still return correct results. Persistent data dirs write a
disposable `filter_attrs.dat` segment cache; reopen loads it before replaying
the WAL tail, and stale or corrupt cache contents are rebuilt from the current snapshot. No-text
`trace_aggregate()` can use the in-memory aggregate
rollup (`readPlan.source == "aggregate_rollup"`). Persistent data dirs also
write a disposable `trace_rollup.dat` segment cache; reopen loads it before
replaying the WAL tail, and stale or corrupt cache contents are rebuilt from
the current snapshot. Deletes, retention apply, and segment upgrades rebuild
the cache as well. Trajectory, loop, and task helpers can return
`readPlan.source == "trajectory_rollup"` for no-text path summaries and reuse
the same `trace_rollup.dat` cache after reopen. When those helpers expand
complete traces after finding candidates, `readPlan.traceFetchSource` shows
whether that second step also used the rollup by trace id. Text filters still
use the normal folded read path. Disk sidecars and dedicated
trajectory-loop-task indexes can be added later without changing these method
names.

Annotation and dataset association use the same embedded metadata ledger as
Node/Rust. They keep review and regression-set links beside trace data without
copying large trace payloads.

Retention audit and policy records are stored in that same ledger. Retention is
always explicit: dry-run with `retention_plan()`, then call `apply_retention()`
or trigger saved policies with `run_retention_policies()`. Audit and policy
queries use the same in-memory metadata postings as annotations.
