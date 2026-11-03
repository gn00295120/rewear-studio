"""One process-wide admission budget shared by isolated hosted wardrobes."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class WorkCapacityError(RuntimeError):
    def __init__(self):
        super().__init__("服務處理佇列已滿，請稍後重試。")


class WorkBudget:
    """Admission is synchronous; callers must not await between checking and enqueueing.

    All managers live on the same event loop. Separate kind semaphores allow
    analysis and its supplemental work to progress without a nested-lock deadlock.
    Every admitted task, including one cancelled before starting, releases its slot.
    """

    def __init__(self, max_pending: int = 64, per_kind: int = 1):
        if type(max_pending) is not int or type(per_kind) is not int:
            raise ValueError("工作容量必須為正整數。")
        if max_pending < 1 or per_kind < 1:
            raise ValueError("工作容量必須為正整數。")
        self.max_pending = max_pending
        self._pending = 0
        self._semaphores = {
            kind: asyncio.Semaphore(per_kind) for kind in ("analysis", "reconstruction", "discovery")
        }

    @property
    def pending(self) -> int:
        return self._pending

    def has_capacity(self, count: int = 1) -> bool:
        return type(count) is int and count >= 0 and self._pending + count <= self.max_pending

    def semaphore(self, kind: str) -> asyncio.Semaphore:
        return self._semaphores[kind]

    def create_task(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        if not self.has_capacity():
            coroutine.close()
            raise WorkCapacityError()
        try:
            task = asyncio.create_task(coroutine, name=name)
        except BaseException:
            coroutine.close()
            raise
        self._pending += 1

        def release(_task):
            self._pending -= 1

        task.add_done_callback(release)
        return task
