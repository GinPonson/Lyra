"""DuckDB 单写协调器。

DuckDB 的账本文件**同一时刻只允许一个进程打开**（已实测，见设计文档附录 A），
进程内则必须保证写操作串行。本类只保证"写任务不并发"，事务的开启、提交与
回滚仍由仓储层负责。

一个任务失败不会污染队列：后续已排队的任务仍按顺序执行。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class WriteCoordinator:
    """在单个进程内串行执行所有 DuckDB 写操作。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._accepting = True
        self._pending = 0

    @property
    def accepting(self) -> bool:
        """是否仍接受新任务。"""
        return self._accepting

    @property
    def pending_operations(self) -> int:
        """当前正在执行及等待执行的任务数。"""
        return self._pending

    async def enqueue(self, operation: Callable[[], Awaitable[T]]) -> T:
        """把一个写任务追加到单写队列。

        @param operation: 轮到时执行的异步写操作。
        @returns ``operation`` 的原始结果。
        @raises RuntimeError: 协调器已开始关闭。
        """
        if not self._accepting:
            raise RuntimeError("写协调器正在关闭，不能接受新任务")
        self._pending += 1
        try:
            async with self._lock:
                return await operation()
        finally:
            self._pending -= 1

    async def close(self) -> None:
        """停止接受新任务，并等待已排队任务完成。"""
        self._accepting = False
        async with self._lock:
            return
