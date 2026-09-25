"""提交单调的雪花 ID 生成器。

id = 41 位毫秒时间戳 | 10 位节点 | 12 位序列  → 单调、可排序、跨进程不撞。
调度层正确性硬前置:event_id 必须真单调(不能用 SEQUENCE CACHE),这里满足。
"""
from __future__ import annotations

import os
import threading
import time

_EPOCH_MS = 1_577_836_800_000  # 2020-01-01Z,缩短数值
_nodes_lock = threading.Lock()
_nodes: dict[int, _NodeState] = {}


class _NodeState:
    """同一进程中，同一个 node_id 的所有 Tracer 共用计数器。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_ms = -1
        self.seq = 0


def _state_for(node: int) -> _NodeState:
    with _nodes_lock:
        if node not in _nodes:
            _nodes[node] = _NodeState()
        return _nodes[node]


class Snowflake:
    def __init__(self, node_id: int | None = None):
        if node_id is None:
            # 默认用 PID 低 10 位;多机部署应显式配不同 node_id
            node_id = os.getpid() & 0x3FF
        if isinstance(node_id, bool) or not isinstance(node_id, int) or not 0 <= node_id <= 0x3FF:
            raise ValueError("node_id must be an integer from 0 to 1023")
        self.node = node_id
        self._state = _state_for(node_id)

    def next(self) -> int:
        state = self._state
        with state.lock:
            ms = max(int(time.time() * 1000), state.last_ms)
            if ms == state.last_ms:
                state.seq = (state.seq + 1) & 0xFFF
                if state.seq == 0:  # 同毫秒序列耗尽,自旋到下一毫秒
                    while ms <= state.last_ms:
                        ms = int(time.time() * 1000)
            else:
                state.seq = 0
            state.last_ms = ms
            return ((ms - _EPOCH_MS) << 22) | (self.node << 12) | state.seq
