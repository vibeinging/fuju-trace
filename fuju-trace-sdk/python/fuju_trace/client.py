"""Open an embedded Fuju Trace store or VexDB adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

TenantId = str | int


def connect(
    target: str | Path | None = None,
    *,
    path: str | Path | None = None,
    data_dir: str | Path | None = None,
    vexdb_dsn: str | None = None,
    vexdb_params: Mapping[str, Any] | None = None,
    tenant_id: TenantId | None = None,
    **options: Any,
) -> Any:
    """Open a Fuju Trace connection.

    Use ``connect(path="./data")`` for an embedded DB when ``fuju-trace-db`` is
    installed, or ``connect(vexdb_params={...}, tenant_id=..., vector_dim=...)``
    when the optional ``fuju-trace-vexdb`` package is installed. A VexDB DSN
    can also be supplied with ``vexdb_dsn``.
    """

    if vexdb_dsn is not None or vexdb_params is not None:
        if target is not None or path is not None or data_dir is not None:
            raise ValueError("connect accepts VexDB or path, not a combination")
        if vexdb_dsn is not None and vexdb_params is not None:
            raise ValueError("connect accepts vexdb_dsn or vexdb_params, not both")
        if tenant_id is None or "vector_dim" not in options:
            raise ValueError("connect(VexDB) requires tenant_id and vector_dim")
        try:
            from fuju_trace_vexdb import VexDBTraceStore
        except ImportError as err:
            raise RuntimeError("connect(VexDB) requires fuju-trace-vexdb; install with pip install 'fuju-trace[vexdb]'") from err
        return VexDBTraceStore.open(vexdb_dsn, tenant_id=tenant_id,
                                    connection_params=vexdb_params, **options)

    if target is not None:
        if path is not None or data_dir is not None:
            raise ValueError("connect received both target and path/data_dir")
        path = target
        if str(path).startswith(("http://", "https://")):
            raise ValueError("HTTP connections are no longer supported; use path or VexDB")
    local_path = data_dir if data_dir is not None else path
    if local_path is None:
        raise ValueError("connect requires path or VexDB connection parameters")
    try:
        from fuju_trace_db import FujuTraceDB
    except ImportError as err:
        raise RuntimeError(
            "connect(path=...) requires the embedded DB package. "
            "Install it with: pip install fuju-trace-db. "
        ) from err
    return FujuTraceDB.open(local_path, tenant_id=tenant_id, **options)


__all__ = ["connect"]
