"""事件导出。把 SDK 产生的 span 事件送出去（日志或数据库）。"""
from __future__ import annotations

import abc
import atexit
from collections import deque
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .event import SpanEvent


class Exporter(abc.ABC):
    @abc.abstractmethod
    def export(self, event: SpanEvent) -> None:
        ...

    def export_batch(self, events: list[SpanEvent]) -> None:
        """一次收一批。默认逐条转 `export`；批量 sink 可自行覆盖。"""
        for e in events:
            self.export(e)

    def request_flush(self) -> int | None:
        """请求后台 exporter 尽快写出已有数据，但不阻塞调用线程。"""

    def close(self) -> None:
        pass


class ConsoleExporter(Exporter):
    """打印成 JSON 行（开发/调试用）。"""

    def export(self, event: SpanEvent) -> None:
        print(json.dumps(event.to_wire(), ensure_ascii=False))


class CollectingExporter(Exporter):
    """收集到内存（测试用）。"""

    def __init__(self) -> None:
        self.events: list[SpanEvent] = []

    def export(self, event: SpanEvent) -> None:
        self.events.append(event)


class NoopExporter(Exporter):
    """静默丢弃事件。用于 fail-open，保证 trace 故障不影响主业务。"""

    def __init__(self) -> None:
        self._dropped = 0

    def export(self, event: SpanEvent) -> None:
        self._dropped += 1

    def export_batch(self, events: list[SpanEvent]) -> None:
        self._dropped += len(events)

    def dropped_count(self) -> int:
        return self._dropped

    def health(self) -> dict:
        return {
            "type": "noop",
            "queue": {"queued": 0, "max": 0},
            "sent": 0,
            "dropped": self.dropped_count(),
            "last_error": None,
        }


class DbExporter(Exporter):
    """把 SDK 事件直接写进 embedded FujuTraceDB。"""

    def __init__(self, db, *, tenant_id: int | str | None = None) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self._sent = 0

    def export(self, event: SpanEvent) -> None:
        self.export_batch([event])

    def export_batch(self, events: list[SpanEvent]) -> None:
        if not events:
            return
        self.db.ingest([event.to_wire() for event in events], tenant_id=self.tenant_id)
        self._sent += len(events)

    def sent_count(self) -> int:
        return self._sent

    def health(self) -> dict:
        return {
            "type": "db",
            "queue": {"queued": 0, "max": 0},
            "sent": self.sent_count(),
            "dropped": 0,
            "last_error": None,
        }


class BufferedDbExporter(Exporter):
    """服务端 embedded 默认写法：业务线程入队，后台单写线程串行写 FujuTraceDB。"""

    def __init__(
        self,
        db,
        *,
        tenant_id: int | str | None = None,
        max_batch: int = 256,
        flush_interval: float = 1.0,
        max_queue: int = 8192,
        drop_when_full: bool = True,
        max_retries: int = 3,
        retry_interval: float = 0.1,
        on_error: Callable[[BaseException, int], None] | None = None,
        register_atexit: bool = True,
    ) -> None:
        if max_batch <= 0:
            raise ValueError("max_batch must be > 0")
        if max_queue <= 0:
            raise ValueError("max_queue must be > 0")
        if flush_interval < 0:
            raise ValueError("flush_interval must be >= 0")
        self.db = db
        self.tenant_id = tenant_id
        self.max_batch = max_batch
        self.flush_interval = flush_interval
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.drop_when_full = drop_when_full
        self.on_error = on_error if on_error is not None else self._default_on_error
        self._queue = deque()
        self._max_queue = max_queue
        self._queue_changed = threading.Condition()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._submitted_seq = 0
        self._processed_seq = 0
        self._flush_target = 0
        self._sent = 0
        self._dropped = 0
        self._write_errors = 0
        self._last_error: str | None = None
        self._thread = threading.Thread(target=self._run, name="fuju-trace-db-writer", daemon=True)
        self._thread.start()
        if register_atexit:
            atexit.register(self.close)

    def export(self, event: SpanEvent) -> None:
        self.export_batch([event])

    def export_batch(self, events: list[SpanEvent]) -> None:
        if not events:
            return
        dropped = 0
        with self._queue_changed:
            for event in events:
                while (
                    not self.drop_when_full
                    and not self._closed.is_set()
                    and len(self._queue) >= self._max_queue
                ):
                    self._queue_changed.wait()
                if self._closed.is_set() or len(self._queue) >= self._max_queue:
                    dropped += 1
                    continue
                self._submitted_seq += 1
                self._queue.append((self._submitted_seq, time.monotonic(), event))
            self._queue_changed.notify_all()
        self._record_drop(dropped)

    def request_flush(self) -> int:
        """让 writer 立即结束当前收集窗口；调用方不等待数据库写完。"""
        with self._queue_changed:
            target = self._submitted_seq
            if target > self._flush_target:
                self._flush_target = target
            self._queue_changed.notify_all()
            return target

    def flush(self, timeout: float | None = None) -> bool:
        """等待调用前已提交的事件处理完；之后的新事件不影响本次等待。"""
        target = self.request_flush()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._queue_changed:
            while self._processed_seq < target:
                if deadline is None:
                    self._queue_changed.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue_changed.wait(remaining)
            return True

    def close(self, timeout: float | None = 5.0) -> None:
        with self._queue_changed:
            if self._closed.is_set():
                return
            self._closed.set()
            self._flush_target = max(self._flush_target, self._submitted_seq)
            self._queue_changed.notify_all()
        self.flush(timeout=timeout)
        self._thread.join(timeout=timeout)

    def sent_count(self) -> int:
        with self._lock:
            return self._sent

    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def write_error_count(self) -> int:
        with self._lock:
            return self._write_errors

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def queued_count(self) -> int:
        with self._queue_changed:
            return len(self._queue)

    def health(self) -> dict:
        return {
            "type": "buffered_db",
            "queue": {"queued": self.queued_count(), "max": self._max_queue},
            "max_batch": self.max_batch,
            "flush_interval": self.flush_interval,
            "sent": self.sent_count(),
            "dropped": self.dropped_count(),
            "write_errors": self.write_error_count(),
            "last_error": self.last_error(),
            "closed": self._closed.is_set(),
            "thread_alive": self._thread.is_alive(),
        }

    def _run(self) -> None:
        while True:
            queued_batch = self._take_batch()
            if not queued_batch:
                return
            batch = [item[2] for item in queued_batch]
            processed_through = queued_batch[-1][0]
            try:
                self._write_batch(batch)
            except BaseException as err:
                # native DB bindings can raise BaseException subclasses (for
                # example a PyO3 panic). A single bad write must not kill the
                # process-scoped writer and strand a flush barrier forever.
                self._record_write_error(err)
                self._record_drop(len(batch))
                self._report_error(err, len(batch))
                self._closed.wait(self.retry_interval)
            finally:
                with self._queue_changed:
                    self._processed_seq = processed_through
                    self._queue_changed.notify_all()

    def _take_batch(self) -> list[tuple[int, float, SpanEvent]]:
        with self._queue_changed:
            while not self._queue:
                if self._closed.is_set():
                    return []
                self._queue_changed.wait()

            deadline = self._queue[0][1] + self.flush_interval
            while (
                len(self._queue) < self.max_batch
                and not self._closed.is_set()
                and self._flush_target <= self._processed_seq
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._queue_changed.wait(remaining)

            batch_size = min(self.max_batch, len(self._queue))
            batch = [self._queue.popleft() for _ in range(batch_size)]
            self._queue_changed.notify_all()
            return batch

    def _write_batch(self, batch: list[SpanEvent]) -> None:
        attempts = 0
        while True:
            try:
                self.db.ingest([event.to_wire() for event in batch], tenant_id=self.tenant_id)
                with self._lock:
                    self._sent += len(batch)
                return
            except BaseException as err:
                attempts += 1
                self._record_write_error(err)
                if attempts > self.max_retries:
                    self._record_drop(len(batch))
                    self._report_error(err, len(batch))
                    return
                self._report_error(err, 0)
                backoff = self.retry_interval * (2 ** min(attempts - 1, 6))
                self._closed.wait(backoff)

    def _record_write_error(self, err: BaseException) -> None:
        with self._lock:
            self._write_errors += 1
            self._last_error = str(err)

    def _report_error(self, err: BaseException, dropped: int) -> None:
        try:
            self.on_error(err, dropped)
        except BaseException as callback_err:
            print(f"[fuju_trace] 写入错误回调失败: {callback_err}", file=sys.stderr)

    def _record_drop(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._dropped += n

    @staticmethod
    def _default_on_error(err: BaseException, dropped: int) -> None:
        msg = f"[fuju_trace] embedded 写入失败: {err}"
        if dropped:
            msg += f" (dropped={dropped})"
        print(msg, file=sys.stderr)


def _ensure_spool_dirs(spool_dir: Path) -> dict[str, Path]:
    dirs = {
        "root": spool_dir,
        "tmp": spool_dir / "tmp",
        "ready": spool_dir / "ready",
        "inflight": spool_dir / "inflight",
        "done": spool_dir / "done",
        "dead": spool_dir / "dead",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SpoolDbExporter(Exporter):
    """多 worker 本地写日志：worker 只写 spool 文件，不直接打开 FujuTraceDB。"""

    def __init__(
        self,
        spool_dir: str | Path,
        *,
        tenant_id: int | str | None = None,
        max_batch: int = 256,
        fsync: bool = True,
        on_error: Callable[[Exception, int], None] | None = None,
    ) -> None:
        if max_batch <= 0:
            raise ValueError("max_batch must be > 0")
        self.spool_dir = Path(spool_dir)
        self.tenant_id = tenant_id
        self.max_batch = max_batch
        self.fsync = fsync
        self.on_error = on_error if on_error is not None else self._default_on_error
        self._dirs = _ensure_spool_dirs(self.spool_dir)
        self._lock = threading.Lock()
        self._buf: list[SpanEvent] = []
        self._counter = 0
        self._written = 0
        self._dropped = 0

    def export(self, event: SpanEvent) -> None:
        with self._lock:
            self._buf.append(event)
            if len(self._buf) < self.max_batch:
                return
            batch, self._buf = self._buf, []
        self._write_batch(batch)

    def export_batch(self, events: list[SpanEvent]) -> None:
        if not events:
            return
        self._write_batch(events)

    def flush(self) -> None:
        with self._lock:
            if not self._buf:
                return
            batch, self._buf = self._buf, []
        self._write_batch(batch)

    def close(self) -> None:
        self.flush()

    def written_count(self) -> int:
        with self._lock:
            return self._written

    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def queued_count(self) -> int:
        with self._lock:
            return len(self._buf)

    def health(self) -> dict:
        return {
            "type": "spool_db",
            "spool_dir": str(self.spool_dir),
            "queue": {"queued": self.queued_count(), "max": self.max_batch},
            "written": self.written_count(),
            "dropped": self.dropped_count(),
            "last_error": None,
        }

    def _next_name(self) -> str:
        with self._lock:
            self._counter += 1
            counter = self._counter
        return f"{time.time_ns()}-{os.getpid()}-{counter}.json"

    def _write_batch(self, batch: list[SpanEvent]) -> None:
        if not batch:
            return
        name = self._next_name()
        tmp_path = self._dirs["tmp"] / f"{name}.tmp"
        ready_path = self._dirs["ready"] / name
        payload = {
            "version": 1,
            "created_ns": time.time_ns(),
            "pid": os.getpid(),
            "tenant_id": None if self.tenant_id is None else str(self.tenant_id),
            "events": [event.to_wire() for event in batch],
        }
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                f.write("\n")
                f.flush()
                if self.fsync:
                    os.fsync(f.fileno())
            os.replace(tmp_path, ready_path)
            if self.fsync:
                _fsync_dir(self._dirs["ready"])
            with self._lock:
                self._written += len(batch)
        except Exception as err:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            with self._lock:
                self._dropped += len(batch)
            self.on_error(err, len(batch))

    @staticmethod
    def _default_on_error(err: Exception, dropped: int) -> None:
        msg = f"[fuju_trace] spool 写入失败: {err}"
        if dropped:
            msg += f" (dropped={dropped})"
        print(msg, file=sys.stderr)


class SpoolConsumer:
    """唯一消费者：读取 spool ready 文件，串行写入 embedded FujuTraceDB。"""

    def __init__(
        self,
        db,
        spool_dir: str | Path,
        *,
        tenant_id: int | str | None = None,
        keep_done: bool = False,
        on_error: Callable[[Exception, Path], None] | None = None,
    ) -> None:
        self.db = db
        self.spool_dir = Path(spool_dir)
        self.tenant_id = tenant_id
        self.keep_done = keep_done
        self.on_error = on_error if on_error is not None else self._default_on_error
        self._dirs = _ensure_spool_dirs(self.spool_dir)
        self._consumed = 0
        self._dead = 0
        self._recover_inflight()

    def consume_once(self, *, limit: int | None = None) -> int:
        """消费一轮 ready 文件，返回成功写入 DB 的事件数。"""
        consumed = 0
        files = sorted(path for path in self._dirs["ready"].iterdir() if path.is_file())
        for ready_path in files:
            if limit is not None and consumed >= limit:
                break
            inflight_path = self._dirs["inflight"] / ready_path.name
            try:
                os.replace(ready_path, inflight_path)
            except FileNotFoundError:
                continue
            try:
                payload = self._read_payload(inflight_path)
            except Exception as err:
                self._move_to_dead(inflight_path)
                self.on_error(err, inflight_path)
                continue

            events = payload["events"]
            tenant_id = payload.get("tenant_id")
            if tenant_id is None:
                tenant_id = self.tenant_id
            try:
                self.db.ingest(events, tenant_id=tenant_id)
            except Exception as err:
                self._move_back_to_ready(inflight_path)
                self.on_error(err, inflight_path)
                break

            if self.keep_done:
                os.replace(inflight_path, self._dirs["done"] / inflight_path.name)
            else:
                inflight_path.unlink(missing_ok=True)
            consumed += len(events)
            self._consumed += len(events)
        return consumed

    def consumed_count(self) -> int:
        return self._consumed

    def dead_count(self) -> int:
        return self._dead

    def _read_payload(self, path: Path) -> dict:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError(f"bad spool payload version: {path}")
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError(f"bad spool payload events: {path}")
        return payload

    def _recover_inflight(self) -> None:
        for path in sorted(self._dirs["inflight"].iterdir()):
            if path.is_file():
                self._move_back_to_ready(path)

    def _move_back_to_ready(self, path: Path) -> None:
        os.replace(path, self._dirs["ready"] / path.name)

    def _move_to_dead(self, path: Path) -> None:
        os.replace(path, self._dirs["dead"] / path.name)
        self._dead += 1

    @staticmethod
    def _default_on_error(err: Exception, path: Path) -> None:
        print(f"[fuju_trace] spool 消费失败: {path}: {err}", file=sys.stderr)


class BatchExporter(Exporter):
    """攒够一批后交给下游 sink 的 `export_batch`。"""

    def __init__(self, sink: Exporter, max_batch: int = 256) -> None:
        self._sink = sink
        self._max = max_batch
        self._buf: list[SpanEvent] = []

    def export(self, event: SpanEvent) -> None:
        self._buf.append(event)
        if len(self._buf) >= self._max:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        self._sink.export_batch(batch)  # 整批一次交下游（sink 能批就批）

    def close(self) -> None:
        self.flush()
        self._sink.close()
