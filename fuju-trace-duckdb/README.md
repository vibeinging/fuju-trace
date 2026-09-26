# Fuju Trace DuckDB

The 0.1.11 PyPI rollout is in progress. Check the [release status](../docs/reports/2026-09-26_python-0.1.11-release.md) before using the install command: this package also needs `fuju-trace-sql==0.1.11`.

DuckDB storage adapter for Fuju Trace. Install this package directly or use
the base SDK extra:

```bash
python -m pip install 'fuju-trace-duckdb==0.1.11'
# equivalent: python -m pip install 'fuju-trace[duckdb]==0.1.11'
```

```python
from fuju_trace import connect

with connect(duckdb_path="./trace.duckdb", tenant_id=1, initialize=True) as db:
    print(db.capabilities())
```

The package re-exports `DuckDBTraceStore` from the shared `fuju-trace-sql`
implementation and installs the DuckDB driver. Use one writer process per
database file. Text search is currently a substring scan; BM25 and vector
search are not available in this adapter. See the
[SQL adapter guide](https://github.com/vibeinging/fuju-trace/blob/main/fuju-trace-sql/README.md) for writing traces and filters.
