"""Longspan 后端入口。

启动即完成：建表 → 扫描技能目录（热插拔）→ 挂载 API。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import SessionLocal, init_db
from .api import canvas, edit, events, runs, settings as settings_api, skills
from .harness.runner import recover_orphaned_runs
from .harness.skill import registry
from .llm import llm_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("longspan")

app = FastAPI(title=f"{settings.app_name} API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    loaded = registry.scan()
    logger.info("启动完成：已加载技能 %s", list(loaded.keys()))
    # 上次进程留下的"半死不活"的任务：如实标成中断（可续跑），不让它永远转圈。
    # 见 harness/runner.recover_orphaned_runs —— 内存任务丢失是唯一的真相来源
    # 就是"这个进程里没有它的 asyncio 任务"，DB 里的 running 只是残影。
    try:
        db = SessionLocal()
        try:
            recovered = recover_orphaned_runs(db)
        finally:
            db.close()
        if recovered:
            logger.warning("已回收 %d 个中断任务：%s", len(recovered), recovered)
    except Exception:  # noqa: BLE001
        logger.exception("回收中断任务失败（不影响启动）")


app.include_router(skills.router)
app.include_router(runs.router)
app.include_router(events.router)
app.include_router(settings_api.router)
app.include_router(canvas.router)
app.include_router(edit.router)


@app.get("/api/health")
def health() -> dict:
    st = llm_status()
    return {
        "ok": True,
        "app": settings.app_name,
        "skills": [s.id for s in registry.all()],
        "llm_provider": st["provider"],
        "llm_mode": st["mode"],
        "db": settings.database_url.split("///")[-1],
    }


@app.get("/api/meta")
def meta() -> dict:
    """给前端的元信息：模型模式、配额、双通道说明。"""
    st = llm_status()
    return {
        "llm_mode": st["mode"],
        "model": st["model"],
        "llm": st,
        "channels": {
            "fast": {"label": "3 分钟草稿", "desc": "零输入可跑，先看效果"},
            "slow": {"label": "精修通道", "desc": "全阶段 + 评审 + 人工关卡"},
        },
        "quota": {
            "max_revisions_per_stage": settings.max_revisions_per_stage,
            "max_send_backs": settings.max_send_backs,
            "max_wall_time_minutes": settings.max_wall_time_minutes,
            "budget_default_usd": settings.budget_default_usd,
        },
    }
