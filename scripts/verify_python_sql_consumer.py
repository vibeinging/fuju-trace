#!/usr/bin/env python3
"""Install built SDK and SQL wheels in a clean environment and use real local databases."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import venv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-wheel-dir", type=Path, required=True)
    parser.add_argument("--sql-wheel-dir", type=Path, required=True)
    args = parser.parse_args()
    sdk_wheels = sorted(args.sdk_wheel_dir.glob("fuju_trace-*.whl"))
    sql_wheels = sorted(args.sql_wheel_dir.glob("fuju_trace_sql-*.whl"))
    if len(sdk_wheels) != 1 or len(sql_wheels) != 1:
        raise SystemExit("expected one SDK wheel and one SQL adapter wheel")

    with tempfile.TemporaryDirectory(prefix="fuju-trace-sql-consumer-") as tmp:
        env_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        package = f"{sql_wheels[0].resolve()}[duckdb,postgresql]"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--find-links",
             str(args.sdk_wheel_dir.resolve()), package],
            check=True,
        )
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        code = textwrap.dedent("""
            from importlib.metadata import metadata, version
            from pathlib import Path
            from tempfile import TemporaryDirectory
            import duckdb
            import psycopg2
            import fuju_trace_sql
            from fuju_trace import DbExporter, Tracer, connect

            assert version('fuju-trace') == version('fuju-trace-sql')
            extras = set(metadata('fuju-trace').get_all('Provides-Extra') or [])
            assert {'sqlite', 'duckdb', 'postgresql'} <= extras
            assert 'mysql' not in extras
            with TemporaryDirectory() as temp:
                for backend, path in (
                    ('sqlite_path', Path(temp) / 'trace.sqlite'),
                    ('duckdb_path', Path(temp) / 'trace.duckdb'),
                ):
                    with connect(**{backend: path}, tenant_id=1, initialize=True) as db:
                        tracer = Tracer(exporter=DbExporter(db, tenant_id=1), node_id=1)
                        with tracer.trace('consumer', session_id=42, tenant_id=1) as trace:
                            with trace.span('work') as span:
                                span.log('wheel smoke')
                        tracer.close()
                        assert len(db.search(text='wheel smoke')) == 1
                    with connect(**{backend: path}, tenant_id=1) as reopened:
                        assert len(reopened.search(text='wheel smoke')) == 1
            print('Verified fuju-trace SQL extras', version('fuju-trace'))
        """)
        subprocess.run([str(python), "-c", code], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
