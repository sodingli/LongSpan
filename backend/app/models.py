"""Longspan 数据模型（对应 MRD §10）。

保真约束：只用 SQLAlchemy 通用类型（String/Text/Integer/Float/Boolean/JSON/DateTime），
不写 Postgres 专有特性（无 JSONB、无 ARRAY、无 UUID 原生类型），
以便开发期 SQLite 与生产 Postgres 之间"只改连接串"。

核心表：
  runs        一次长任务（Run）
  steps       Run 内的阶段执行记录（Step）
  events      事件流（SSE 的数据源，id 自增可用于 Last-Event-ID 续传）
  artifacts   产物（文件）
  checkpoints 检查点（阶段级落盘，门禁硬校验的依据）
  gates       人工关卡（HITL）
  decisions   决策留痕（模型每个自主决定的记录，可推翻）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Run 状态常量（状态机，见 harness/runner.py）
# --------------------------------------------------------------------------- #
class RunStatus:
    PENDING = "pending"              # 已创建，未启动
    RUNNING = "running"              # 执行中
    AWAITING_GATE = "awaiting_gate"  # 挂在人工关卡上（可恢复）
    PAUSED = "paused"                # 用户主动暂停
    SUCCEEDED = "succeeded"          # 全部阶段完成
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"      # 进程重启导致执行中断（可续跑，已完成阶段复用）


class StepStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"      # 半途被进程重启打断（续跑时会重跑本阶段）


class CheckpointStatus:
    IN_PROGRESS = "in_progress"      # 进阶段先写，告知"是活着不是卡住"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"        # 被取代后归档，永不销毁


class GateStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISED = "revised"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(String(64), index=True)
    skill_name: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    brief: Mapped[dict] = mapped_column(JSON, default=dict)      # 用户输入（硬约束/软偏好）
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="fast")  # fast | slow（§5.1 双通道）

    # 首个「可用成品」落盘的耗时（秒）—— 北极星指标：Time to First Value。
    # 草稿答不答应"3 分钟出成品"，靠这个数说话，不靠感觉。
    ttfv_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 从哪个通道起步（fast 起步 = 这份产物天然是草稿，需如实标注）
    origin_channel: Mapped[str] = mapped_column(String(16), default="fast")
    resumed_count: Mapped[int] = mapped_column(Integer, default=0)  # 续跑/升级次数

    current_stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    send_backs: Mapped[int] = mapped_column(Integer, default=0)   # 打回次数（配额校验）

    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace: Mapped[str] = mapped_column(Text, default="")          # 该 Run 的产物工作目录

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    steps: Mapped[list["Step"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Step.index"
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    gates: Mapped[list["Gate"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "title": self.title,
            "brief": self.brief or {},
            "status": self.status,
            "channel": self.channel,
            "origin_channel": self.origin_channel,
            "ttfv_seconds": round(self.ttfv_seconds, 2) if self.ttfv_seconds is not None else None,
            "resumed_count": self.resumed_count,
            "current_stage_id": self.current_stage_id,
            "progress": round(self.progress or 0.0, 4),
            "send_backs": self.send_backs,
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd or 0.0, 4),
            "error": self.error,
            "workspace": self.workspace,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage_id: Mapped[str] = mapped_column(String(64))
    stage_name: Mapped[str] = mapped_column(String(128), default="")
    index: Mapped[int] = mapped_column(Integer, default=0)
    is_sub_stage: Mapped[bool] = mapped_column(Boolean, default=False)   # sample 子阶段等
    parent_stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=StepStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=0)             # 重做次数（配额校验）
    summary: Mapped[str] = mapped_column(Text, default="")               # 该阶段结论一句话
    detail: Mapped[dict] = mapped_column(JSON, default=dict)             # 校验结果/工具调用等

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="steps")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "stage_name": self.stage_name,
            "index": self.index,
            "is_sub_stage": self.is_sub_stage,
            "parent_stage_id": self.parent_stage_id,
            "status": self.status,
            "attempt": self.attempt,
            "summary": self.summary,
            "detail": self.detail or {},
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class Event(Base):
    """事件流。id 用自增整数 —— SSE 的 Last-Event-ID 断线续传直接用它。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def to_sse(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "type": self.type,
            "stage_id": self.stage_id,
            "message": self.message,
            "payload": self.payload or {},
            "at": self.created_at.isoformat() if self.created_at else None,
        }


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="file")   # md/json/image/audio/video/pptx...
    name: Mapped[str] = mapped_column(String(256), default="")
    rel_path: Mapped[str] = mapped_column(Text, default="")          # 相对 Run workspace
    size: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)   # 快通道产物标记为草稿
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)  # sample 子阶段产物
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    run: Mapped["Run"] = relationship(back_populates="artifacts")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "kind": self.kind,
            "name": self.name,
            "rel_path": self.rel_path,
            "size": self.size,
            "mime": self.mime,
            "version": self.version,
            "is_draft": self.is_draft,
            "is_sample": self.is_sample,
            "meta": self.meta or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Checkpoint(Base):
    """检查点。门禁硬校验：没有 human_approved 就不能写 completed。"""

    __tablename__ = "checkpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    stage_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=CheckpointStatus.IN_PROGRESS)
    human_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    partial_progress: Mapped[dict] = mapped_column(JSON, default=dict)  # 从失败点精确恢复
    archive_reason: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "status": self.status,
            "human_approved": self.human_approved,
            "payload": self.payload or {},
            "partial_progress": self.partial_progress or {},
            "archive_reason": self.archive_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Gate(Base):
    """人工关卡。门禁不是 UI 按钮，是落盘校验。"""

    __tablename__ = "gates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="approval")  # approval | input
    status: Mapped[str] = mapped_column(String(32), default=GateStatus.PENDING, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    proposed: Mapped[dict] = mapped_column(JSON, default=dict)   # 待确认内容
    decision: Mapped[dict] = mapped_column(JSON, default=dict)   # 用户决定（含修改意见）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="gates")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "kind": self.kind,
            "status": self.status,
            "title": self.title,
            "prompt": self.prompt,
            "proposed": self.proposed or {},
            "decision": self.decision or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


class Decision(Base):
    """决策留痕：用户只提约束，但不失去控制（MRD §7.3.4）。"""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(16), default="model")   # model | user
    what: Mapped[str] = mapped_column(Text, default="")               # 决定了什么
    why: Mapped[str] = mapped_column(Text, default="")                # 依据
    alternatives: Mapped[list] = mapped_column(JSON, default=list)    # 备选方案 + 置信度
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)   # 是否可推翻
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "actor": self.actor,
            "what": self.what,
            "why": self.why,
            "alternatives": self.alternatives or [],
            "reversible": self.reversible,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Character(Base):
    """角色资产：跨 Run 复用的角色档案（定妆图 + 冻结锚点 + 完整角色卡）。

    为什么单独成表而不是继续放在每个 Run 的 characters/ 目录里：
    漫剧是**系列**产品，系列的资产是角色。放在 Run 内等于每集重新铸造一次，
    第二集想用第一集的主角只能重新生图、重新赌一致性 —— 这是产品缺陷，
    不是工程细节。角色的归属应该是「工作台」而不是「某一次任务」。

    复用时把 art + card 物化回目标 Run 的 workspace（characters/<slug>.<ext>
    与 characters.md 里的角色卡块），下游参考图查找链路无需改动即可吃到，
    且定妆图是同一个文件，跨集一致性是 100% 而不是「概率上接近」。
    """

    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)        # 展示名（中文）
    slug: Mapped[str] = mapped_column(String(64), unique=True)        # 文件标识 characters/<slug>.<ext>
    anchor: Mapped[str] = mapped_column(Text, default="")             # 冻结锚点原文
    card: Mapped[str] = mapped_column(Text, default="")               # 完整角色卡 markdown（复用时原样注入）
    style: Mapped[str] = mapped_column(String(64), default="")        # 画风标签
    skill_id: Mapped[str] = mapped_column(String(64), default="", index=True)

    art_rel: Mapped[str] = mapped_column(Text, default="")            # 源 Run 内相对路径
    source_run_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    art_provider: Mapped[str] = mapped_column(String(64), default="") # 哪条生图通道产出的

    usage_count: Mapped[int] = mapped_column(Integer, default=0)      # 被复用次数（含首次）
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)            # 可变/禁止变化/视觉区分度等

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def to_dict(self, *, with_card: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "anchor": self.anchor,
            "style": self.style,
            "skill_id": self.skill_id,
            "art_rel": self.art_rel,
            "source_run_id": self.source_run_id,
            "art_provider": self.art_provider,
            "usage_count": self.usage_count,
            "meta": self.meta or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if with_card:
            d["card"] = self.card
        return d


class CharacterUsage(Base):
    """角色出场记录：哪个角色出现在哪一集（系列进度看板与「出场集数」的数据源）。"""

    __tablename__ = "character_usages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    character_id: Mapped[str] = mapped_column(String(32), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    reused: Mapped[bool] = mapped_column(Boolean, default=False)  # True=复用；False=本集新铸造
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
