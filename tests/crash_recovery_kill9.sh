#!/usr/bin/env bash
# 真 kill -9 崩溃恢复：进程内写入已确认后立即杀进程，再重开核对累计数据和检索。

set -euo pipefail

ROUNDS="${1:-20}"
ENGINE_DIR="fuju-trace-engine"
BIN="${FUJU_TRACE_CRASH_WORKER_BIN:-${CARGO_TARGET_DIR:-$ENGINE_DIR/target}/release/examples/crash_worker}"
DATA_DIR="$(mktemp -d)"
WORKER_PID=""

cleanup() {
  if [[ -n "$WORKER_PID" ]]; then
    kill -9 "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

if [[ ! -x "$BIN" ]]; then
  echo "找不到 $BIN；先构建 crash_worker example" >&2
  exit 2
fi

echo "=== 进程内 kill -9 崩溃恢复（$ROUNDS 轮）==="
for round in $(seq 1 "$ROUNDS"); do
  "$BIN" write "$DATA_DIR" "$round" >"$DATA_DIR/worker.log" 2>&1 &
  WORKER_PID=$!
  ready=0
  for _ in $(seq 1 100); do
    if grep -q '^READY$' "$DATA_DIR/worker.log"; then
      ready=1
      break
    fi
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
      break
    fi
    sleep 0.05
  done
  if [[ "$ready" -ne 1 ]]; then
    cat "$DATA_DIR/worker.log" >&2
    echo "轮 $round：写入进程未确认" >&2
    exit 1
  fi

  kill -9 "$WORKER_PID"
  wait "$WORKER_PID" 2>/dev/null || true
  WORKER_PID=""
  "$BIN" verify "$DATA_DIR" "$round"
done
echo "全部 $ROUNDS 轮通过"
