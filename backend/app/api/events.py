"""SSE 事件流。

两个必须做对的地方（长任务的体验分水岭）：
  1. **断线续传** —— 刷新页面/网络抖动后，用 Last-Event-ID 补齐丢失的事件，
     而不是让用户面对一个"停止更新"的界面。
  2. **心跳** —— 模型思考可能静默数十秒，心跳让前端知道"是活着不是卡住"。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, Request
from sse_starlette.sse import EventSourceResponse

from ..db import SessionLocal
from ..events import bus
from ..models import Event, Run

router = APIRouter(prefix="/api/runs", tags=["events"])

PING_INTERVAL = 15


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    async def gen():
        q = await bus.subscribe(run_id)
        try:
            # ---- ① 补发历史事件（断线续传的核心）----
            since = 0
            if last_event_id:
                try:
                    since = int(last_event_id)
                except ValueError:
                    since = 0

            db = SessionLocal()
            try:
                hist = (
                    db.query(Event)
                    .filter(Event.run_id == run_id, Event.id > since)
                    .order_by(Event.id)
                    .all()
                )
                events = [e.to_sse() for e in hist]
            finally:
                db.close()

            for ev in events:
                yield {"event": "message", "id": str(ev["id"]), "data": json.dumps(ev, ensure_ascii=False)}

            # ---- ② 实时推送 ----
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ok": True})}
                    # 心跳时顺带告知当前状态，避免前端"以为卡住"
                    db = SessionLocal()
                    try:
                        run = db.get(Run, run_id)
                        if run and run.status in {"succeeded", "failed", "cancelled"}:
                            yield {
                                "event": "message",
                                "id": "0",
                                "data": json.dumps(
                                    {"id": 0, "type": "run.status", "payload": {"status": run.status}},
                                    ensure_ascii=False,
                                ),
                            }
                    finally:
                        db.close()
                    continue
                yield {"event": "message", "id": str(data.get("id", 0)), "data": json.dumps(data, ensure_ascii=False)}
        finally:
            await bus.unsubscribe(run_id, q)

    return EventSourceResponse(
        gen(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 关掉反向代理缓冲，否则 SSE 会被攒着一起发
        },
    )
