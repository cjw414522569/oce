"""队列健康查询：主队列长度 / 在飞数 / DB 待办数。

queue 为 None（个人模式或 worker 关闭）时返回 enabled=False 的零值快照。
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.messages import Query
from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory


@dataclass(frozen=True)
class QueueStatusQuery(Query):
    pass


@dataclass(frozen=True)
class QueueStatusResult:
    enabled: bool
    main_size: int
    inflight: int
    db_pending: int


class QueueStatusQueryHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        queue: Queue | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._queue = queue

    async def handle(self, _query: QueueStatusQuery) -> QueueStatusResult:
        if self._queue is None:
            return QueueStatusResult(
                enabled=False, main_size=0, inflight=0, db_pending=0
            )
        async with self._uow_factory() as uow:
            db_pending = len(await uow.blobs.list_pending_names())
        return QueueStatusResult(
            enabled=True,
            main_size=await self._queue.size(),
            inflight=len(await self._queue.inflight_set()),
            db_pending=db_pending,
        )


# 吞吐视图窗口：名称 → 秒
THROUGHPUT_WINDOWS: dict[str, int] = {
    "last_1m": 60,
    "last_1h": 3600,
    "last_24h": 86400,
    "last_7d": 604800,
    "last_30d": 2592000,
}


@dataclass(frozen=True)
class QueueThroughputQuery(Query):
    pass


@dataclass(frozen=True)
class QueueThroughputResult:
    counts: dict[str, int]


class QueueThroughputQueryHandler:
    """运维吞吐视图：各时间窗完成的 blob 数（completed_at 打点）。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, _query: QueueThroughputQuery) -> QueueThroughputResult:
        async with self._uow_factory() as uow:
            counts = await uow.blobs.count_completed_windows(THROUGHPUT_WINDOWS)
        return QueueThroughputResult(counts=counts)
