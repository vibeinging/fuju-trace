#!/usr/bin/env python3
"""Check that every Fuju Trace release artifact uses one version."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    import tomllib
except ImportError as err:  # pragma: no cover - release runners use Python 3.12
    raise SystemExit("check_release_versions.py requires Python 3.11+") from err


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def cargo_package_version(path: Path, package_name: str) -> str:
    lock = read_toml(path)
    matches = [pkg["version"] for pkg in lock.get("package", []) if pkg.get("name") == package_name]
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one {package_name!r} package, found {len(matches)}")
    return matches[0]


def expect_name(actual: object, expected: object, source: str) -> None:
    if actual != expected:
        raise SystemExit(f"{source}: expected name {expected!r}, found {actual!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    expected_group = parser.add_mutually_exclusive_group()
    expected_group.add_argument("--expected", help="expected release version; defaults to @fuju/trace-db")
    expected_group.add_argument("--tag", help="release tag, for example v0.1.3 or v0.1.3-only-node-db")
    args = parser.parse_args()

    node_package = read_json(ROOT / "fuju-trace-node/package.json")
    ts_package = read_json(ROOT / "fuju-trace-sdk/typescript/package.json")
    python_sdk_package = read_toml(ROOT / "fuju-trace-sdk/python/pyproject.toml")["project"]
    python_db_package = read_toml(ROOT / "fuju-trace-db-python/pyproject.toml")["project"]
    vexdb_package = read_toml(ROOT / "fuju-trace-vexdb/pyproject.toml")["project"]
    sql_package = read_toml(ROOT / "fuju-trace-sql/pyproject.toml")["project"]
    sql_backend_packages = {
        backend: read_toml(ROOT / f"fuju-trace-{backend}/pyproject.toml")["project"]
        for backend in ("sqlite", "duckdb", "postgresql")
    }
    rust_sdk_package = read_toml(ROOT / "fuju-trace-sdk/rust/Cargo.toml")["package"]
    rust_db_package = read_toml(ROOT / "fuju-trace-db-rs/Cargo.toml")["package"]
    expect_name(node_package["name"], "@fuju/trace-db", "Node DB package")
    expect_name(ts_package["name"], "@fuju/trace-sdk", "TypeScript SDK package")
    expect_name(python_sdk_package["name"], "fuju-trace", "Python SDK package")
    expect_name(python_db_package["name"], "fuju-trace-db", "Python DB package")
    expect_name(vexdb_package["name"], "fuju-trace-vexdb", "VexDB adapter package")
    expect_name(sql_package["name"], "fuju-trace-sql", "SQL adapter package")
    for backend, package in sql_backend_packages.items():
        expect_name(package["name"], f"fuju-trace-{backend}", f"{backend} adapter package")
    expect_name(rust_sdk_package["name"], "fuju-trace", "Rust SDK crate")
    expect_name(rust_db_package["name"], "fuju-trace-db", "Rust DB crate")
    expect_name(read_toml(ROOT / "fuju-trace-node/Cargo.toml")["package"]["name"], "fuju-trace-db-node", "Node native crate")
    expect_name(read_toml(ROOT / "fuju-trace-db-python/Cargo.toml")["package"]["name"], "fuju-trace-db-python", "Python native crate")
    expect_name(node_package["napi"]["binaryName"], "fuju-trace-db", "Node native binary")
    expect_name(python_sdk_package["scripts"], {"fuju-trace": "fuju_trace.cli:main"}, "Python SDK CLI")
    tag_version = None
    if args.tag:
        if not args.tag.startswith("v"):
            raise SystemExit(f"release tag must start with v: {args.tag}")
        tag_version = args.tag[1:].split("-only-", 1)[0]
    expected = args.expected or tag_version or node_package["version"]
    versions: dict[str, str] = {
        "@fuju/trace-db": node_package["version"],
        "fuju-trace-db-node crate": read_toml(ROOT / "fuju-trace-node/Cargo.toml")["package"]["version"],
        "fuju-trace-db-node Cargo.lock": cargo_package_version(
            ROOT / "fuju-trace-node/Cargo.lock", "fuju-trace-db-node"
        ),
        "@fuju/trace-sdk": ts_package["version"],
        "TypeScript SDK package-lock": read_json(
            ROOT / "fuju-trace-sdk/typescript/package-lock.json"
        )["packages"][""]["version"],
        "Python fuju-trace": python_sdk_package["version"],
        "Python fuju-trace-db": python_db_package["version"],
        "Python fuju-trace-vexdb": vexdb_package["version"],
        "Python fuju-trace-sql": sql_package["version"],
        **{f"Python fuju-trace-{backend}": package["version"]
           for backend, package in sql_backend_packages.items()},
        "Python native crate": read_toml(ROOT / "fuju-trace-db-python/Cargo.toml")["package"]["version"],
        "Python native Cargo.lock": cargo_package_version(
            ROOT / "fuju-trace-db-python/Cargo.lock", "fuju-trace-db-python"
        ),
        "Rust fuju-trace SDK": rust_sdk_package["version"],
        "Rust fuju-trace SDK Cargo.lock": cargo_package_version(
            ROOT / "fuju-trace-sdk/rust/Cargo.lock", "fuju-trace"
        ),
        "Rust fuju-trace-db": rust_db_package["version"],
        "Rust fuju-trace-db Cargo.lock": cargo_package_version(
            ROOT / "fuju-trace-db-rs/Cargo.lock", "fuju-trace-db"
        ),
    }

    node_lock = read_json(ROOT / "fuju-trace-node/package-lock.json")
    ts_lock = read_json(ROOT / "fuju-trace-sdk/typescript/package-lock.json")
    expect_name(ts_lock["name"], "@fuju/trace-sdk", "TypeScript package-lock")
    expect_name(ts_lock["packages"][""]["name"], "@fuju/trace-sdk", "TypeScript package-lock root")
    expect_name(node_lock["name"], "@fuju/trace-db", "Node package-lock")
    expect_name(node_lock["packages"][""]["name"], "@fuju/trace-db", "Node package-lock root")
    versions["@fuju/trace-db package-lock"] = node_lock["packages"][""]["version"]

    platform_packages = sorted((ROOT / "fuju-trace-node/npm").glob("*/package.json"))
    expected_optional_names = set()
    for path in platform_packages:
        package = read_json(path)
        expect_name(package["name"], f"@fuju/trace-db-{path.parent.name}", str(path))
        versions[package["name"]] = package["version"]
        expected_optional_names.add(package["name"])

    optional = node_package.get("optionalDependencies", {})
    if set(optional) != expected_optional_names:
        missing = sorted(expected_optional_names - set(optional))
        extra = sorted(set(optional) - expected_optional_names)
        raise SystemExit(f"optional package list mismatch: missing={missing}, extra={extra}")
    for name, version in optional.items():
        versions[f"{name} optionalDependency"] = version
        lock_entry = node_lock["packages"].get(f"node_modules/{name}")
        if not lock_entry:
            raise SystemExit(f"package-lock is missing optional package {name}")
        versions[f"{name} package-lock"] = lock_entry["version"]

    vexdb_extra = python_sdk_package.get("optional-dependencies", {}).get("vexdb", [])
    expected_vexdb_requirement = f"fuju-trace-vexdb[driver]=={expected}"
    if vexdb_extra != [expected_vexdb_requirement]:
        raise SystemExit(
            f"Python SDK vexdb extra must be [{expected_vexdb_requirement!r}], found {vexdb_extra!r}"
        )
    if vexdb_package.get("dependencies") != [f"fuju-trace=={expected}"]:
        raise SystemExit("VexDB adapter must depend on the same Python SDK version")
    if sql_package.get("dependencies") != [f"fuju-trace=={expected}"]:
        raise SystemExit("SQL adapter must depend on the same Python SDK version")
    sql_extras = {
        backend: f"fuju-trace-{backend}=={expected}"
        for backend in sql_backend_packages
    }
    for extra, requirement in sql_extras.items():
        found = python_sdk_package.get("optional-dependencies", {}).get(extra, [])
        if found != [requirement]:
            raise SystemExit(f"Python SDK {extra} extra must be [{requirement!r}], found {found!r}")
    backend_drivers = {
        "sqlite": [],
        "duckdb": ["duckdb>=1.0,<3"],
        "postgresql": ["psycopg2-binary>=2.9.5,<3"],
    }
    for backend, package in sql_backend_packages.items():
        expected_dependencies = [f"fuju-trace-sql=={expected}", *backend_drivers[backend]]
        if package.get("dependencies") != expected_dependencies:
            raise SystemExit(f"{backend} adapter must depend on {expected_dependencies!r}")

    mismatches = [(name, version) for name, version in versions.items() if version != expected]
    if mismatches:
        print(f"release version mismatch; expected {expected}", file=sys.stderr)
        for name, version in mismatches:
            print(f"- {name}: {version}", file=sys.stderr)
        return 1

    print(f"Fuju Trace release versions are consistent: {expected} ({len(versions)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
