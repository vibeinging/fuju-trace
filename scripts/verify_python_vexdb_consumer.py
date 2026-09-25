#!/usr/bin/env python3
"""Install the exact built SDK wheel with its VexDB extra in a clean venv."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-wheel-dir", type=Path, required=True)
    parser.add_argument("--vexdb-wheel-dir", type=Path, required=True)
    args = parser.parse_args()
    sdk_wheels = sorted(args.sdk_wheel_dir.glob("fuju_trace-*.whl"))
    adapter_wheels = sorted(args.vexdb_wheel_dir.glob("fuju_trace_vexdb-*.whl"))
    if len(sdk_wheels) != 1 or len(adapter_wheels) != 1:
        raise SystemExit("expected one SDK wheel and one VexDB adapter wheel")

    with tempfile.TemporaryDirectory(prefix="fuju-trace-vexdb-consumer-") as tmp:
        env_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        package = f"{sdk_wheels[0].resolve()}[vexdb]"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--find-links", str(args.vexdb_wheel_dir.resolve()), package],
            check=True,
        )
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        subprocess.run(
            [str(python), "-c", "import fuju_trace, fuju_trace_vexdb, psycopg2; "
             "from importlib.metadata import version; "
             "assert version('fuju-trace') == version('fuju-trace-vexdb'); "
             "print('Verified fuju-trace[vexdb]', version('fuju-trace'))"],
            check=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
