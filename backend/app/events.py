"""事件总线：事件流是长任务的"心跳"。

两个职责：
1. 持久化 —— 每个事件写入 events 表（SSE 的 Last-Event-ID 续传依据）
2. 广播 —— 推送给订阅该 Run 的活跃 SSE 连接

设计要点：先落库再广播，保证"断线重连能补齐"。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from .models import Event

logger = logging.getLogger("longspan.events")

# 事件类型常量（前端按这些类型做渲染分支）
class EventType:
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_STATUS = "run.status"              # 状态变更（running/paused/...）
    RUN_PROGRESS = "run.progress"
    RUN_FINISHED = "run.finished"
    RUN_ERROR = "run.error"

    STAGE_STARTED = "stage.started"
    STAGE_CHECKPOINT = "stage.checkpoint"  # in_progress 检查点（"是活着不是卡住"）
    STAGE_COMPLETED = "stage.completed"
    STAGE_REVISION = "stage.revision"      # 重做
    STAGE_FAILED = "stage.failed"

    AGENT_THOUGHT = "agent.thought"
    AGENT_MESSAGE = "agent.message"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"

    ARTIFACT_CREATED = "artifact.created"
    DECISION_RECORDED = "decision.recorded"

    DRAFT_READY = "draft.ready"            # 首个可用成品落盘（TTFV 计量点）
    RUN_RESUMED = "run.resumed"            # 续跑/升级：复用已完成阶段，只补跑剩余

    GATE_OPENED = "gate.opened"            # 关卡开启，Run 挂起等待
    GATE_RESOLVED = "gate.resolved"

    QUOTA_WARN = "quota.warn"
    QUOTA_CAP = "quota.cap"


class EventBus:
    """进程内广播 + 数据库持久化。"""

    def __init__(self) -> None:
        # run_id -> 订阅者队列集合
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def emit(
        self,
        db: Session,
        run_id: str,
        type_: str,
        message: str = "",
        stage_id: str | None = None,
        payload: dict | None = None,
    ) -> Event:
        """先落库（获得自增 id），再广播。返回值带 id，可直接作为 SSE id。"""
        ev = Event(
            run_id=run_id,
            type=type_,
            stage_id=stage_id,
            message=message[:2000],
            payload=payload or {},
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)

        self._broadcast(ev)
        return ev

    def _broadcast(self, ev: Event) -> None:
        queues = self._subscribers.get(ev.run_id)
        if not queues:
            return
        data = ev.to_sse()
        for q in list(queues):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:  # pragma: no cover
                logger.warning("SSE 队列已满，丢弃事件 id=%s", ev.id)

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    async def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.setdefault(run_id, set()).add(q)
        return q

    async def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(run_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(run_id, None)

    def has_subscribers(self, run_id: str) -> bool:
        return bool(self._subscribers.get(run_id))


bus = EventBus()
