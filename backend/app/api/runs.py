"""Run 接口：创建、查询、控制、门禁、产物。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import EventType, bus
from ..file_index import build_file_index, render_markdown
from ..harness.runner import build_draft_info, manager
from ..harness.skill import registry
from ..models import Artifact, Checkpoint, Decision, Gate, GateStatus, Run, RunStatus, Step, StepStatus
from ..schemas import GateDecisionIn, RunCreate
from ..store import storage

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _apply_input_defaults(skill, brief: dict, preset_id: str | None) -> dict:
    """快通道「零输入可跑」的关键：所有输入都有稳妥默认值，不做必填拦截。"""
    out = dict(brief or {})
    if preset_id:
        for p in skill.presets:
            if p.get("id") == preset_id:
                out.update(p.get("brief", {}) or {})
                break
    for inp in skill.entry_inputs:
        key = inp.get("key")
        if not key:
            continue
        if key not in out or out.get(key) in (None, ""):
            out[key] = inp.get("default", "")
    out.setdefault("_channel", "fast")
    return out


@router.post("")
async def create_run(payload: RunCreate, db: Session = Depends(get_db)) -> dict:
    """必须是 async：manager.start 内部用 asyncio.create_task，
    同步接口会被 FastAPI 丢到线程池执行，那里没有运行中的事件循环。"""
    skill = registry.get(payload.skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{payload.skill_id}")

    brief = _apply_input_defaults(skill, payload.brief, payload.preset_id)
    title = payload.title.strip()
    if not title and skill.entry_inputs:
        title = str(brief.get(skill.entry_inputs[0]["key"], "") or "").strip()
    if not title:
        title = f"{skill.name} · {datetime.now().strftime('%m-%d %H:%M')}"

    run = Run(
        skill_id=skill.id,
        skill_name=skill.name,
        title=title[:240],
        brief=brief,
        channel=payload.channel,
        origin_channel=payload.channel,
        status=RunStatus.PENDING,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    storage.run_workspace(run.id)
    bus.emit(db, run.id, EventType.RUN_CREATED, f"任务已创建：{run.title}",
             payload={"skill_id": skill.id, "channel": run.channel})

    if payload.autostart:
        manager.start(run.id)
    return run.to_dict()


@router.get("")
def list_runs(db: Session = Depends(get_db)) -> dict:
    rows = db.query(Run).order_by(Run.created_at.desc()).limit(50).all()
    return {"runs": [r.to_dict() for r in rows]}


@router.get("/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    steps = db.query(Step).filter(Step.run_id == run_id).order_by(Step.index).all()
    gates = db.query(Gate).filter(Gate.run_id == run_id).order_by(Gate.created_at).all()
    artifacts = db.query(Artifact).filter(Artifact.run_id == run_id).order_by(Artifact.created_at).all()
    decisions = db.query(Decision).filter(Decision.run_id == run_id).order_by(Decision.created_at).all()
    checkpoints = db.query(Checkpoint).filter(Checkpoint.run_id == run_id).order_by(Checkpoint.created_at).all()
    d = run.to_dict()
    d.update(
        {
            "steps": [s.to_dict() for s in steps],
            "gates": [g.to_dict() for g in gates],
            "artifacts": [a.to_dict() for a in artifacts],
            "decisions": [x.to_dict() for x in decisions],
            "checkpoints": [c.to_dict() for c in checkpoints],
            "files": storage.list_files(run_id),
            # 草稿诚实性：前端据此渲染「这是草稿 / 用什么换来的 / 还能怎么变好」
            "draft": build_draft_info(run, registry.get(run.skill_id), steps),
        }
    )
    return d


@router.post("/{run_id}/upgrade")
async def upgrade_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    """草稿 → 精修：原地续跑，不重做已完成的阶段。

    这是 D9「先可用性、后留存」的落地接口：用户看完草稿觉得方向对，
    一键在**同一份产物**上接着深化，而不是重新开一个任务从零跑。
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.channel == "slow":
        raise HTTPException(status_code=409, detail="该任务已在精修通道")
    if run.status in (RunStatus.RUNNING, RunStatus.AWAITING_GATE, RunStatus.PENDING):
        raise HTTPException(status_code=409, detail=f"任务正在执行中（{run.status}），请先等它结束")

    skill = registry.get(run.skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{run.skill_id}")

    # 已完成的阶段会被 runner 跳过并复用其产物，这里只负责把通道切过去
    run.channel = "slow"
    run.resumed_count += 1
    run.error = None
    run.status = RunStatus.PENDING
    run.finished_at = None
    db.commit()

    bus.emit(db, run_id, EventType.RUN_STATUS,
             "已升级为精修通道：保留现有产物，只补跑未完成的阶段",
             payload={"channel": "slow", "resumed_count": run.resumed_count})
    manager.start(run_id)
    return {"ok": True, "run_id": run_id, "channel": "slow",
            "pending_stages": [s.id for s in skill.stages if not s.fast_channel]}


@router.post("/{run_id}/resume")
async def resume_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    """续跑：接着把没跑完的阶段跑完，已完成阶段的产物原样复用。

    与 `/upgrade` 的分工：upgrade 是「草稿 → 精修」的**通道升级**；resume 是
    「中断/失败/取消之后**接着跑**」，不改通道。专治进程重启留下的中断任务——
    否则它会以 running 状态永远卡住，且 upgrade 还会以"任务正在执行中"拒绝它。
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.status in (RunStatus.RUNNING, RunStatus.AWAITING_GATE, RunStatus.PENDING):
        raise HTTPException(status_code=409, detail=f"任务正在执行中（{run.status}），无需续跑")

    skill = registry.get(run.skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{run.skill_id}")

    done = {
        s.stage_id
        for s in db.query(Step).filter(
            Step.run_id == run_id,
            Step.status.in_([StepStatus.COMPLETED, StepStatus.SKIPPED]),
        ).all()
    }
    target = skill.fast_stages() if run.channel == "fast" else [s for s in skill.stages if not s.optional]
    pending = [s.id for s in target if s.id not in done]

    run.status = RunStatus.PENDING
    run.error = None
    run.finished_at = None
    run.resumed_count += 1
    db.commit()

    bus.emit(db, run_id, EventType.RUN_STATUS,
             "手动续跑：保留已完成阶段的产物，只补跑未完成的阶段",
             payload={"channel": run.channel, "resumed_count": run.resumed_count,
                      "pending": pending, "trigger": "resume"})
    manager.start(run_id)
    return {"ok": True, "run_id": run_id, "channel": run.channel,
            "reuse": sorted(done), "pending_stages": pending}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    manager.cancel(run_id)
    return {"ok": True, "run_id": run_id}


@router.post("/{run_id}/gates/{gate_id}/resolve")
async def resolve_gate(
    run_id: str, gate_id: str, payload: GateDecisionIn, db: Session = Depends(get_db)
) -> dict:
    gate = db.get(Gate, gate_id)
    if gate is None or gate.run_id != run_id:
        raise HTTPException(status_code=404, detail="关卡不存在")
    if gate.status != GateStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"关卡已处理：{gate.status}")

    ok = manager.resolve_gate(run_id, gate_id, payload.status, payload.model_dump())
    if not ok:
        # 任务不在等待中（比如重启后），直接落库，避免"点了没反应"
        gate.status = payload.status
        gate.decision = payload.model_dump()
        gate.decided_at = datetime.now(timezone.utc)
        run = db.get(Run, run_id)
        if run and run.status == RunStatus.AWAITING_GATE:
            run.status = RunStatus.RUNNING
        db.commit()
        return {"ok": True, "revived": False}
    return {"ok": True, "revived": True}


@router.get("/{run_id}/files")
def list_files(run_id: str) -> dict:
    return {"files": storage.list_files(run_id)}


@router.get("/{run_id}/files/{rel_path:path}")
def read_file(run_id: str, rel_path: str):
    if not storage.exists(run_id, rel_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    p = storage.abs_path(run_id, rel_path)
    if p.suffix.lower() in {".svg", ".png", ".jpg", ".jpeg", ".webp"}:
        return FileResponse(p)
    if p.suffix.lower() in {".mp3", ".wav", ".mp4"}:
        return FileResponse(p)
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@router.get("/{run_id}/tree")
def run_tree(run_id: str, db: Session = Depends(get_db)) -> dict:
    """任务目录表：把工作区整理成一张分层清单。

    与 `/{run_id}/files`（只回一串路径）的区别：这里给出每个文件的**真实**大小与
    修改时间、**属于哪个阶段**（读 artifacts 表）、**谁写的**（读画布编辑留痕），
    以及这次成片实际用到了哪些（读渲染参数）。文件系统是事实来源，DB 只作补充。
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    artifacts = db.query(Artifact).filter(Artifact.run_id == run_id).all()
    return build_file_index(
        storage=storage,
        run_id=run_id,
        run=run,
        skill=registry.get(run.skill_id),
        artifacts=artifacts,
    )


@router.post("/{run_id}/tree/export")
def export_tree(
    run_id: str,
    group_by: str = "stage",
    save: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    """把目录表落盘成 `INDEX.md`（随任务一起打包下载），并记一笔事件留痕。"""
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if group_by not in ("stage", "dir"):
        raise HTTPException(status_code=400, detail="group_by 只能是 stage 或 dir")

    artifacts = db.query(Artifact).filter(Artifact.run_id == run_id).all()
    index = build_file_index(
        storage=storage,
        run_id=run_id,
        run=run,
        skill=registry.get(run.skill_id),
        artifacts=artifacts,
    )
    md = render_markdown(index, run, group_by=group_by)
    rel = ""
    size = 0
    if save:
        rel, size = storage.write_text(run_id, "INDEX.md", md)
        bus.emit(
            db, run_id, EventType.ARTIFACT_CREATED,
            f"已生成任务目录表 INDEX.md（{index['stats']['files']} 个文件）",
            payload={"path": rel, "bytes": size, "group_by": group_by},
        )
    return {"ok": True, "path": rel, "bytes": size, "markdown": md}


@router.get("/{run_id}/download")
def download_zip(run_id: str):
    """把整个 Run 工作区打包下载 —— 用户要的是"拿走成品"。"""
    import io
    import zipfile

    ws = storage.run_workspace(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in ws.rglob("*"):
            if p.is_file():
                zf.write(p, str(p.relative_to(ws)))
    buf.seek(0)
    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="longspan-{run_id}.zip"'},
    )


@router.get("/{run_id}/metrics")
def get_run_metrics(run_id: str, db: Session = Depends(get_db)) -> dict:
    """一集的三个数（ROADMAP §零.2）：返工率 / 结构违约数 / TTFV 分段。

    实时从既有留痕计算（steps / events / gates），与 metrics.json 落盘口径一致；
    不存在 cache 失效问题，改了历史（重置阶段重跑）后刷新页面即得新数。
    """
    from ..harness.metrics import compute_run_metrics

    run = db.query(Run).filter(Run.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return compute_run_metrics(db, run)


@router.get("/metrics/overview")
def metrics_overview(db: Session = Depends(get_db)) -> dict:
    """全部任务的指标总览（周报视图）：每个 run 一行三个数 + 判定线状态。"""
    from ..harness.metrics import compute_run_metrics

    rows = []
    for run in db.query(Run).order_by(Run.created_at.desc()).limit(50).all():
        try:
            m = compute_run_metrics(db, run)
        except Exception:  # noqa: BLE001
            continue
        rows.append({
            "run_id": m["run_id"],
            "title": m["title"],
            "status": m["status"],
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "panel_count": m["panel_count"],
            "rework_rate": m["rework_rate"],
            "violation_count": m["violation_count"],
            "human_share": m["human_share"],
            "ttfv_seconds": m["ttfv_seconds"],
            "verdicts": m["verdicts"],
        })
    return {"rows": rows}
