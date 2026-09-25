#!/usr/bin/env python3
"""Install a built fuju-trace-db wheel in a clean environment and use every public entrypoint."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    print("==>", " ".join(command), flush=True)
    return subprocess.run(command, check=True, text=True, **kwargs)


def venv_command(root: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return root / directory / f"{name}{suffix}"


def choose_wheel(wheel_dir: Path) -> Path:
    wheels = sorted(wheel_dir.glob("fuju_trace_db-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one fuju-trace-db wheel in {wheel_dir}, found {len(wheels)}")
    return wheels[0].resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel-dir", type=Path, required=True)
    args = parser.parse_args()
    wheel = choose_wheel(args.wheel_dir)

    work = Path(tempfile.mkdtemp(prefix="fuju-trace-python-db-wheel-"))
    try:
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = venv_command(environment, "python")
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)])

        consumer = work / "consumer.py"
        consumer.write_text(
            """
from pathlib import Path
import tempfile

from fuju_trace_db import FujuTraceDB, create_span_event_builder

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp)
    with FujuTraceDB.open(path, tenant_id=42) as db:
        builder = create_span_event_builder({
            "trace_id": "wheel-run",
            "session_id": "wheel-session",
            "attrs": {"project_id": "wheel-project", "skill": "release"},
        })
        builder.start_span(span_id="wheel-span", name="wheel smoke", input_text="疑似盗刷")
        builder.log("疑似盗刷", span_id="wheel-span")
        builder.end_span(span_id="wheel-span", status=0, duration_ns=7)
        db.ingest(builder.events(), tenant_id=42)
        assert len(db.search(text="盗刷", k=10)) == 1

    with FujuTraceDB.open(path, tenant_id=42) as db:
        hits = db.search(text="盗刷", k=10)
        assert len(hits) == 1
        assert hits[0]["external_trace_id"] == "wheel-run"
""".lstrip(),
            encoding="utf-8",
        )
        run([str(python), str(consumer)], cwd=work)

        print(f"Verified fuju-trace-db wheel in clean consumer: {wheel.name}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
