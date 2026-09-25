#!/usr/bin/env python3
"""Verify the Python fuju_trace SDK from a clean consumer environment."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SDK_DIR = ROOT / "fuju-trace-sdk" / "python"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print(f"\n==> {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def venv_python(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def venv_script(env_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / f"{name}.exe"
    return env_dir / "bin" / name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, help="Install the exact built wheel instead of the source tree")
    args = parser.parse_args()
    if args.wheel_dir:
        wheels = sorted(args.wheel_dir.glob("fuju_trace-*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected exactly one fuju-trace wheel in {args.wheel_dir}, found {len(wheels)}")
        package = wheels[0].resolve()
    else:
        if not SDK_DIR.exists():
            raise SystemExit(f"missing Python SDK directory: {SDK_DIR}")
        package = SDK_DIR
    work = Path(tempfile.mkdtemp(prefix="fuju-trace-python-sdk-consumer-"))
    try:
        env_dir = work / ".venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        py = venv_python(env_dir)
        run([str(py), "-m", "pip", "install", "--no-deps", str(package)])
        run([str(venv_script(env_dir, "fuju-trace")), "--help"])
        script = work / "verify.py"
        script.write_text(
            textwrap.dedent(
                """
                from fuju_trace import CollectingExporter, NoopExporter, Tracer, connect, init_fuju_trace, shutdown_fuju_trace

                exporter = CollectingExporter()
                tracer = Tracer(exporter=exporter, node_id=1)
                with tracer.trace("clean consumer") as trace:
                    with trace.span("span") as span:
                        span.log("疑似盗刷")
                assert [event.event_type.value for event in exporter.events] == [1, 4, 2]

                try:
                    connect(path="./data")
                except RuntimeError as err:
                    message = str(err)
                    assert "pip install fuju-trace-db" in message
                else:
                    raise AssertionError("connect(path=...) must explain the missing fuju-trace-db package")

                runtime = init_fuju_trace(path="./data", fail_open=True, register_atexit=False)
                assert runtime.enabled is False
                assert isinstance(runtime.exporter, NoopExporter)
                with runtime.tracer.trace("fail open") as trace:
                    with trace.span("span") as span:
                        span.log("ignored")
                runtime.close()
                assert runtime.exporter.dropped_count() == 3
                shutdown_fuju_trace()
                """
            ),
            encoding="utf-8",
        )
        run([str(py), str(script)], cwd=work)
        print(f"\nVerified Python fuju_trace SDK in clean consumer from {package}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
