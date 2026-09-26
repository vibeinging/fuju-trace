#!/usr/bin/env python3
"""Install one built backend package in a clean environment and exercise it."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import venv


def one_wheel(directory: Path, pattern: str) -> Path:
    wheels = sorted(directory.glob(pattern))
    if len(wheels) != 1:
        raise SystemExit(f"expected one {pattern} wheel in {directory}, found {len(wheels)}")
    return wheels[0].resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("sqlite", "duckdb", "postgresql"))
    parser.add_argument("--sdk-wheel-dir", required=True, type=Path)
    parser.add_argument("--sql-wheel-dir", required=True, type=Path)
    parser.add_argument("--backend-wheel-dir", required=True, type=Path)
    args = parser.parse_args()

    sdk = one_wheel(args.sdk_wheel_dir, "fuju_trace-*.whl")
    one_wheel(args.sql_wheel_dir, "fuju_trace_sql-*.whl")
    backend_wheel = one_wheel(args.backend_wheel_dir, f"fuju_trace_{args.backend}-*.whl")
    links = [
        item for directory in (args.sdk_wheel_dir, args.sql_wheel_dir, args.backend_wheel_dir)
        for item in ("--find-links", str(directory.resolve()))
    ]

    with tempfile.TemporaryDirectory(prefix=f"fuju-trace-{args.backend}-consumer-") as tmp:
        env_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # Check both the direct distribution name and the base SDK extra.
        subprocess.run([str(python), "-m", "pip", "install", *links,
                        str(backend_wheel)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", *links,
                        f"{sdk}[{args.backend}]"], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        code = textwrap.dedent(f"""
            from importlib import import_module
            from importlib.metadata import version
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from fuju_trace import DbExporter, Tracer, connect
            from fuju_trace_sql import SQLiteTraceStore, DuckDBTraceStore, PostgreSQLTraceStore

            backend = {args.backend!r}
            assert version('fuju-trace') == version('fuju-trace-sql') == version('fuju-trace-' + backend)
            adapter = import_module('fuju_trace_' + backend)
            expected = {{
                'sqlite': SQLiteTraceStore,
                'duckdb': DuckDBTraceStore,
                'postgresql': PostgreSQLTraceStore,
            }}[backend]
            assert getattr(adapter, expected.__name__) is expected
            if backend == 'postgresql':
                import psycopg2
            else:
                if backend == 'duckdb':
                    import duckdb
                with TemporaryDirectory() as temp:
                    key = backend + '_path'
                    path = Path(temp) / ('trace.' + backend)
                    with connect(**{{key: path}}, tenant_id=1, initialize=True) as db:
                        tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
                        with tracer.trace('package smoke', tenant_id=1) as trace:
                            with trace.span('work') as span:
                                span.log('wheel smoke')
                        tracer.close()
                        assert len(db.search(text='wheel smoke')) == 1
                    with connect(**{{key: path}}, tenant_id=1) as db:
                        assert len(db.search(text='wheel smoke')) == 1
            print('Verified direct package and SDK extra', backend, version('fuju-trace'))
        """)
        subprocess.run([str(python), "-c", code], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
