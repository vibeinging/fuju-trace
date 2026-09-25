"""服务端接入 helper。"""
from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import connect
from .exporter import BufferedDbExporter, DbExporter, Exporter, NoopExporter, SpoolDbExporter
from .tracer import Tracer


@dataclass
class FujuTraceRuntime:
    """`init_fuju_trace` 返回的运行时句柄。"""

    tracer: Tracer
    exporter: Exporter
    db: Any | None = None
    enabled: bool = True
    error: Exception | None = None
    owns_db: bool = False
    mode: str = "unknown"
    data_dir: str | None = None
    spool_dir: str | None = None
    requested_mode: str | None = None

    def close(self, timeout: float | None = 5.0) -> None:
        try:
            close = getattr(self.exporter, "close")
            try:
                close(timeout=timeout)
            except TypeError:
                close()
        finally:
            if self.owns_db and self.db is not None:
                self.db.close()

    def health(self) -> dict[str, Any]:
        exporter = _exporter_health(self.exporter)
        lock = _db_lock_health(self.db)
        last_error = str(self.error) if self.error is not None else exporter.get("last_error")
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "data_dir": self.data_dir,
            "spool_dir": self.spool_dir,
            "queue": exporter.get("queue", {"queued": None, "max": None}),
            "sent": exporter.get("sent"),
            "written": exporter.get("written"),
            "dropped": exporter.get("dropped", 0),
            "write_errors": exporter.get("write_errors", 0),
            "last_error": last_error,
            "lock": lock,
            "exporter": exporter,
        }


def _path_text(value: str | Path | None) -> str | None:
    return None if value is None else str(value)


def _requested_mode(
    *,
    path: str | Path | None,
    data_dir: str | Path | None,
    spool_dir: str | Path | None,
    mode: str,
) -> str:
    normalized = mode.replace("-", "_")
    if normalized == "spool" or spool_dir is not None:
        return "spool"
    if path is not None or data_dir is not None:
        return normalized
    return normalized


def _exporter_health(exporter: Exporter) -> dict[str, Any]:
    health = getattr(exporter, "health", None)
    if callable(health):
        value = health()
        if isinstance(value, dict):
            return value
    state: dict[str, Any] = {"type": exporter.__class__.__name__, "queue": {"queued": None, "max": None}}
    for key, method_name in (
        ("sent", "sent_count"),
        ("dropped", "dropped_count"),
        ("written", "written_count"),
        ("write_errors", "write_error_count"),
        ("last_error", "last_error"),
    ):
        method = getattr(exporter, method_name, None)
        if callable(method):
            try:
                state[key] = method()
            except Exception as err:
                state.setdefault("last_error", str(err))
    queued = getattr(exporter, "queued_count", None)
    if callable(queued):
        try:
            state["queue"] = {"queued": queued(), "max": None}
        except Exception as err:
            state.setdefault("last_error", str(err))
    return state


def _db_lock_health(db: Any | None) -> dict[str, Any]:
    if db is None:
        return {"enabled": False}
    lock_metrics = getattr(db, "lock_metrics", None)
    if not callable(lock_metrics):
        return {"enabled": None}
    try:
        metrics = lock_metrics()
    except Exception as err:
        return {"enabled": True, "error": str(err)}
    if isinstance(metrics, dict):
        return metrics
    return {"enabled": True, "raw": metrics}


_runtime_lock = threading.Lock()
_runtime: FujuTraceRuntime | None = None


def init_fuju_trace(
    *,
    path: str | Path | None = None,
    data_dir: str | Path | None = None,
    spool_dir: str | Path | None = None,
    mode: str = "buffered",
    tenant_id: int | str | None = None,
    node_id: int | None = None,
    fail_open: bool = True,
    register_atexit: bool = True,
    **options: Any,
) -> FujuTraceRuntime:
    """初始化服务端 tracer。

    - `path=...` / `data_dir=...`：打开 embedded DB，默认使用 `BufferedDbExporter`。
    - `mode="spool"` + `spool_dir=...`：只写本地 spool，不打开 embedded DB。
    - `fail_open=True`：初始化失败时返回 `NoopExporter`，主服务继续启动。
    """

    global _runtime
    try:
        runtime = _init_fuju_trace_strict(
            path=path,
            data_dir=data_dir,
            spool_dir=spool_dir,
            mode=mode,
            tenant_id=tenant_id,
            node_id=node_id,
            register_atexit=register_atexit,
            **options,
        )
    except Exception as err:
        if not fail_open:
            raise
        exporter = NoopExporter()
        runtime = FujuTraceRuntime(
            tracer=Tracer(exporter=exporter, node_id=node_id),
            exporter=exporter,
            enabled=False,
            error=err,
            mode="noop",
            requested_mode=_requested_mode(
                path=path,
                data_dir=data_dir,
                spool_dir=spool_dir,
                mode=mode,
            ),
            data_dir=_path_text(data_dir if data_dir is not None else path),
            spool_dir=_path_text(spool_dir),
        )
    with _runtime_lock:
        _runtime = runtime
    if register_atexit:
        atexit.register(shutdown_fuju_trace)
    return runtime


def _init_fuju_trace_strict(
    *,
    path: str | Path | None,
    data_dir: str | Path | None,
    spool_dir: str | Path | None,
    mode: str,
    tenant_id: int | str | None,
    node_id: int | None,
    register_atexit: bool,
    **options: Any,
) -> FujuTraceRuntime:
    normalized_mode = mode.replace("-", "_")
    if normalized_mode == "spool":
        if spool_dir is None:
            raise ValueError('mode="spool" requires spool_dir')
        exporter = SpoolDbExporter(
            spool_dir,
            tenant_id=tenant_id,
            max_batch=options.pop("max_batch", 256),
            fsync=options.pop("fsync", True),
        )
        return FujuTraceRuntime(
            tracer=Tracer(exporter=exporter, node_id=node_id),
            exporter=exporter,
            mode="spool",
            requested_mode="spool",
            spool_dir=_path_text(spool_dir),
        )

    local_path = data_dir if data_dir is not None else path
    if local_path is None:
        raise ValueError("init_fuju_trace requires path/data_dir or mode='spool' with spool_dir")
    db = connect(path=local_path, tenant_id=tenant_id, **options.pop("connect_options", {}))
    if normalized_mode == "direct":
        exporter = DbExporter(db, tenant_id=tenant_id)
    elif normalized_mode == "buffered":
        exporter = BufferedDbExporter(
            db,
            tenant_id=tenant_id,
            max_batch=options.pop("max_batch", 256),
            flush_interval=options.pop("flush_interval", 1.0),
            max_queue=options.pop("max_queue", 8192),
            drop_when_full=options.pop("drop_when_full", True),
            max_retries=options.pop("max_retries", 3),
            retry_interval=options.pop("retry_interval", 0.1),
            register_atexit=register_atexit,
        )
    else:
        db.close()
        raise ValueError(f"unknown fuju_trace mode: {mode}")
    return FujuTraceRuntime(
        tracer=Tracer(exporter=exporter, node_id=node_id),
        exporter=exporter,
        db=db,
        owns_db=True,
        mode=normalized_mode,
        requested_mode=normalized_mode,
        data_dir=_path_text(local_path),
    )


def get_fuju_trace_runtime() -> FujuTraceRuntime | None:
    with _runtime_lock:
        return _runtime


def shutdown_fuju_trace(timeout: float | None = 5.0) -> None:
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        runtime.close(timeout=timeout)


__all__ = ["FujuTraceRuntime", "get_fuju_trace_runtime", "init_fuju_trace", "shutdown_fuju_trace"]
