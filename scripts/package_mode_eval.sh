#!/usr/bin/env bash
# Fuju Trace package-mode eval.
#
# Covers the public package shapes that users install or embed:
#   - Python fuju_trace facade: connect(path/VexDB), DbExporter
#   - TypeScript tracing SDK
#   - Rust tracing SDK
#   - Python fuju-trace-db embedded DB
#   - Rust fuju-trace-db embedded crate
#   - Node @fuju/trace-db embedded package

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_NODE=1
RUN_PYTHON_DB=1
RUN_RUST_DB=1
RUN_SDK=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-node)
      RUN_NODE=0
      shift
      ;;
    --skip-python-db)
      RUN_PYTHON_DB=0
      shift
      ;;
    --skip-rust-db)
      RUN_RUST_DB=0
      shift
      ;;
    --skip-sdk)
      RUN_SDK=0
      shift
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

run() {
  echo
  echo "==> $*"
  "$@"
}

need_dir() {
  if [[ ! -d "$1" ]]; then
    echo "缺少必需目录: $1（只可用对应 --skip-* 参数显式跳过）" >&2
    exit 1
  fi
  return 0
}

if [[ "$RUN_SDK" -eq 1 ]]; then
  if need_dir "$ROOT_DIR/fuju-trace-sdk/python"; then
    run python "$ROOT_DIR/fuju-trace-sdk/python/tests/test_sdk.py"
    run python "$ROOT_DIR/scripts/verify_python_sdk_consumer.py"
  fi

  if need_dir "$ROOT_DIR/fuju-trace-sdk/typescript"; then
    pushd "$ROOT_DIR/fuju-trace-sdk/typescript" >/dev/null
    run npm test
    popd >/dev/null
  fi

  if need_dir "$ROOT_DIR/fuju-trace-sdk/rust"; then
    run cargo test --offline --manifest-path "$ROOT_DIR/fuju-trace-sdk/rust/Cargo.toml"
  fi
fi

if [[ "$RUN_PYTHON_DB" -eq 1 ]]; then
  if need_dir "$ROOT_DIR/fuju-trace-db-python"; then
    pushd "$ROOT_DIR/fuju-trace-db-python" >/dev/null
    run python -m pytest
    popd >/dev/null
  fi
fi

if [[ "$RUN_RUST_DB" -eq 1 ]]; then
  if need_dir "$ROOT_DIR/fuju-trace-db-rs"; then
    run cargo test --offline --manifest-path "$ROOT_DIR/fuju-trace-db-rs/Cargo.toml"
  fi
fi

if [[ "$RUN_NODE" -eq 1 ]]; then
  if need_dir "$ROOT_DIR/fuju-trace-node"; then
    pushd "$ROOT_DIR/fuju-trace-node" >/dev/null
    run npm run build
    run npm test
    popd >/dev/null
  fi
fi

echo
echo "package-mode eval 全部通过"
