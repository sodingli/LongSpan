"""Run 状态机 —— 长任务主干。

它做的是"长任务产品"特有的、**开源内核不提供**的那部分（MRD §9.1 职责边界）：
  · Run/Step 状态机（含 Gate 挂起与恢复）
  · 检查点协议（进阶段先写 in_progress；门禁硬校验；被取代的归档不销毁）
  · 配额治理（重做/打回/墙钟/预算四重上限）
  · 快慢双通道调度（§5.1）

它**不**做编排：阶段的顺序、验收标准、是否要人确认，全部读自技能 manifest。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..events import EventType, bus
from ..models import (
    Artifact,
    Checkpoint,
    CheckpointStatus,
    Decision,
    Gate,
    GateStatus,
    Run,
    RunStatus,
    Step,
    StepStatus,
)
from ..store import storage
from ..canvas_store import effective_image_config, effective_llm_model
from .agent import AgentRunner, build_system_prompt
from .consistency import extract_anchors, validate_panel_anchors
from .skill import SkillDef, StageDef, registry
from .tools import ToolContext
from .tools import registry as tool_registry

logger = logging.getLogger("longspan.runner")

# 送进"上游上下文"时必须跳过的二进制后缀：它们读不出文本，
# 混进去只会浪费一次异常，还可能把关键清单挤出上下文预算。
_UPSTREAM_SKIP_EXT = (
    ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    ".mp3", ".m4a", ".wav", ".aac", ".flac",
    ".mp4", ".mov", ".webm", ".mkv",
    ".zip", ".pdf", ".pptx", ".docx",
)


# --------------------------------------------------------------------------- #
# 验收标准校验（数据化，来自 manifest 的 success_criteria）
# --------------------------------------------------------------------------- #
_RE_KW = re.compile(r"(?:包含|含)\s*[:：]?\s*[「\"“']?([^」\"”'\n，,。]{2,30})[」\"”']?")
_RE_LEN = re.compile(r"(?:至少|不少于|>=|≥)\s*(\d+)\s*字")
_RE_FILE = re.compile(r"文件\s*([\w./\-]+)\s*(?:存在|已生成|非空)")


def evaluate_criteria(
    criteria: list[str], file_map: dict[str, str]
) -> tuple[bool, list[str]]:
    """把自然语言的验收标准做可机器校验的解读。

    支持三类硬规则（manifest 里按此写）：
      · 文件 X 存在（非空）
      · 包含"关键词"
      · 至少 N 字
    未命中任何规则的条目按"任选其一即通过"处理，避免误伤。
    """
    failures: list[str] = []
    checked_any = False
    joined = "\n".join(file_map.values())

    for c in criteria:
        hit = False

        m = _RE_FILE.search(c)
        if m:
            checked_any = True
            hit = m.group(1) in file_map and len(file_map[m.group(1)].strip()) > 0
            if not hit:
                failures.append(f"未生成或为空：{m.group(1)}")
            continue

        m = _RE_LEN.search(c)
        if m:
            checked_any = True
            hit = len(joined) >= int(m.group(1))
            if not hit:
                failures.append(f"内容长度不足（要求 ≥ {m.group(1)} 字，实际 {len(joined)} 字）")
            continue

        m = _RE_KW.search(c)
        if m:
            checked_any = True
            kw = m.group(1).strip()
            hit = kw in joined
            if not hit:
                failures.append(f"缺少必需信息：「{kw}」")
            continue

    # 什么都没识别出来 → 退化为"至少要有非空产物"
    if not checked_any:
        ok = bool(joined.strip())
        return ok, ([] if ok else ["未产出任何内容"])

    return (not failures), failures


def _read_json_artifact(run_id: str, rel: str) -> Any | None:
    """读取一份 JSON 产物（阶段动作的输入清单）。读不到就返回 None，由调用方决定怎么办。"""
    if not storage.exists(run_id, rel):
        return None
    try:
        return json.loads(storage.read_text(run_id, rel))
    except Exception as exc:  # noqa: BLE001
        logger.warning("清单解析失败 %s：%s", rel, exc)
        return None


def _aware(dt: datetime | None) -> datetime:
    """SQLite 取回的 DateTime 是 naive 的，统一补上 UTC 再参与计算。"""
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_draft_info(run: Run, skill: SkillDef | None, steps: list[Step]) -> dict:
    """草稿卡片的数据来源。

    设计立场：**草稿可以粗，但不可以不诚实**。
    与其让用户以为拿到了成品，不如提前把"差在哪、下一步能怎么变好"摊开。
    """
    target_s = settings.fast_channel_target_seconds
    done = {s.stage_id for s in steps if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)}

    unrun: list[dict] = []
    if skill:
        for s in skill.stages:
            if s.optional or s.id in done:
                continue
            unrun.append({
                "id": s.id,
                "name": s.name,
                "icon": s.icon,
                "reason": "快通道未覆盖" if not s.fast_channel else "尚未执行",
            })

    ttfv = run.ttfv_seconds
    return {
        "is_draft": run.channel == "fast",
        "origin_channel": run.origin_channel,
        "ttfv_seconds": round(ttfv, 2) if ttfv is not None else None,
        "target_seconds": target_s,
        "within_target": (ttfv <= target_s) if ttfv is not None else None,
        # 技能声明的「快通道省略了什么」——数据，不是代码里写死的文案
        "gaps": skill.draft_gaps() if skill else [],
        "unrun_stages": unrun,
        # 草稿跑完才谈得上继续优化（这也是留存动线的入口）
        "can_upgrade": run.channel == "fast" and run.status == RunStatus.SUCCEEDED,
        "resume_count": run.resumed_count or 0,
    }


# --------------------------------------------------------------------------- #
# Run 管理器
# --------------------------------------------------------------------------- #
class RunManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._gate_waiters: dict[str, asyncio.Event] = {}
        self._gate_results: dict[str, dict] = {}
        self._cancelled: set[str] = set()

    # ---------------- 对外控制 ----------------
    def start(self, run_id: str) -> None:
        if run_id in self._tasks and not self._tasks[run_id].done():
            return
        self._tasks[run_id] = asyncio.create_task(self._execute(run_id))

    def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        # 唤醒可能挂起的门禁等待，让循环有机会退出
        ev = self._gate_waiters.get(run_id)
        if ev:
            self._gate_results[run_id] = {"status": GateStatus.REJECTED, "decision": {"reason": "cancelled"}}
            ev.set()

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled

    def resolve_gate(self, run_id: str, gate_id: str, status: str, decision: dict) -> bool:
        ev = self._gate_waiters.get(run_id)
        if not ev:
            return False
        self._gate_results[run_id] = {"gate_id": gate_id, "status": status, "decision": decision}
        ev.set()
        return True

    async def _wait_gate(self, run_id: str, timeout_minutes: int) -> dict:
        ev = asyncio.Event()
        self._gate_waiters[run_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_minutes * 60)
        except asyncio.TimeoutError:
            self._gate_results[run_id] = {
                "status": GateStatus.REJECTED,
                "decision": {"reason": "timeout"},
            }
        finally:
            self._gate_waiters.pop(run_id, None)
        return self._gate_results.pop(run_id, {"status": GateStatus.REJECTED, "decision": {}})

    # ---------------- 主流程 ----------------
    async def _execute(self, run_id: str) -> None:
        db = SessionLocal()
        started = time.monotonic()
        try:
            run = db.get(Run, run_id)
            if run is None:
                return
            skill = registry.get(run.skill_id)
            if skill is None:
                self._fail(db, run, f"技能不存在：{run.skill_id}")
                return

            quota = skill.quota()
            all_stages = [s for s in skill.stages if not s.optional] or skill.stages
            target = skill.fast_stages() if run.channel == "fast" else all_stages
            target = [s for s in target if not s.optional] or all_stages

            # 预先建 Step 记录，前端一开始就能看到完整进度树
            self._prepare_steps(db, run, target)

            # 续跑语义：已完成的阶段不重跑，其产物直接作为下游上下文。
            # 「草稿 → 继续优化」与断点续跑共用这一条路径（不另写一套逻辑）。
            done_ids = {
                s.stage_id
                for s in db.query(Step)
                .filter(
                    Step.run_id == run.id,
                    Step.status.in_([StepStatus.COMPLETED, StepStatus.SKIPPED]),
                )
                .all()
            }
            pending = [s for s in target if s.id not in done_ids]

            run.status = RunStatus.RUNNING
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.workspace = str(storage.run_workspace(run.id))
            if done_ids and target:
                # 续跑时进度条不能停在上一轮的数值：被复用跳过的阶段也要算进分子，
                # 否则界面会出现"100% 却还在跑"的假象（用户以为卡住了）。
                run.progress = min(len(done_ids) / len(target), 0.99)
            db.commit()

            if done_ids:
                bus.emit(db, run.id, EventType.RUN_RESUMED,
                         f"续跑：复用已完成 {len(done_ids)} 个阶段的产物，只补跑剩余 {len(pending)} 个阶段",
                         payload={"reuse": sorted(done_ids), "pending": [s.id for s in pending]})
            bus.emit(db, run.id, EventType.RUN_STARTED,
                     f"任务启动：{skill.name}（{'快通道·草稿' if run.channel == 'fast' else '精修通道'}）",
                     payload={"skill_id": skill.id, "stages": [s.id for s in target]})
            self._emit_status(db, run)

            for idx, stage in enumerate(target):
                if self.is_cancelled(run.id):
                    self._cancel(db, run)
                    return

                # 已完成阶段：跳过（产物已在上游上下文里）
                if stage.id in done_ids:
                    continue

                # 配额：墙钟
                elapsed_min = (time.monotonic() - started) / 60
                if elapsed_min > quota["max_wall_time_minutes"]:
                    bus.emit(db, run.id, EventType.QUOTA_CAP,
                             f"已达墙钟上限 {quota['max_wall_time_minutes']} 分钟，安全收尾",
                             stage_id=stage.id)
                    self._finish(db, run, status=RunStatus.SUCCEEDED,
                                 note="达到时间上限，输出当前已完成部分")
                    return
                # 配额：预算
                if run.cost_usd > quota["budget_default_usd"]:
                    bus.emit(db, run.id, EventType.QUOTA_CAP,
                             f"已达预算上限 ${quota['budget_default_usd']}，安全收尾", stage_id=stage.id)
                    self._finish(db, run, status=RunStatus.SUCCEEDED, note="达到预算上限")
                    return

                gate_decision = await self._run_stage(db, run, skill, stage, quota)

                # 阶段没产出成品（声明的媒体动作失败且重做次数用尽）：
                # 立刻如实收尾，不带着假成品往下走。已完成阶段的产物保留，可续跑。
                if gate_decision and gate_decision.get("status") == "_stage_failed":
                    self._fail(
                        db, run,
                        f"阶段「{stage.name}」未产出成品："
                        + "；".join(gate_decision.get("failures") or [])[:400],
                    )
                    return

                if gate_decision and gate_decision.get("status") == GateStatus.REVISED:
                    # 用户要求修改 → 带反馈重跑本阶段（消耗一次打回配额）
                    run.send_backs += 1
                    if run.send_backs > quota["max_send_backs"]:
                        bus.emit(db, run.id, EventType.QUOTA_CAP, "打回次数超限，改为记录意见后继续",
                                 stage_id=stage.id)
                    else:
                        await self._run_stage(
                            db, run, skill, stage, quota,
                            feedback=str(gate_decision.get("decision", {}).get("feedback", "")),
                        )

                run.current_stage_id = stage.id
                run.progress = (idx + 1) / len(target)
                db.commit()
                bus.emit(db, run.id, EventType.RUN_PROGRESS,
                         f"进度 {idx + 1}/{len(target)} · {stage.name}",
                         stage_id=stage.id, payload={"progress": run.progress,
                                                     "index": idx + 1, "total": len(target)})

            self._finish(db, run, status=RunStatus.SUCCEEDED)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Run 执行异常 %s", run_id)
            db.rollback()
            run = db.get(Run, run_id)
            if run:
                self._fail(db, run, str(exc))
        finally:
            db.close()

    # ---------------- 单阶段 ----------------
    async def _run_stage(
        self,
        db: Session,
        run: Run,
        skill: SkillDef,
        stage: StageDef,
        quota: dict,
        feedback: str = "",
    ) -> dict | None:
        before_files = set(storage.list_files(run.id))
        step = self._get_step(db, run.id, stage.id)
        if step is None:
            step = Step(run_id=run.id, stage_id=stage.id, stage_name=stage.name)
            db.add(step)
            db.commit()

        # ① 进阶段先写 in_progress 检查点 —— 告知用户"是活着不是卡住"
        if stage.checkpoint_required:
            db.add(Checkpoint(run_id=run.id, stage_id=stage.id,
                              status=CheckpointStatus.IN_PROGRESS,
                              payload={"stage_name": stage.name}))
            db.commit()
        bus.emit(db, run.id, EventType.STAGE_STARTED, f"进入阶段：{stage.name}",
                 stage_id=stage.id, payload={"stage_name": stage.name, "icon": stage.icon})
        bus.emit(db, run.id, EventType.STAGE_CHECKPOINT,
                 f"检查点已写入（in_progress）：{stage.name}", stage_id=stage.id,
                 payload={"status": CheckpointStatus.IN_PROGRESS})

        step.status = StepStatus.IN_PROGRESS
        step.started_at = datetime.now(timezone.utc)
        step.attempt += 1
        db.commit()

        # ② 装配上下文并执行 Agent
        def _decision_sink(what: str, why: str, alternatives: list) -> None:
            """模型的自主决策落库（MRD §7.3.4：用户不写步骤，但必须看得到判断）。"""
            db.add(Decision(
                run_id=run.id, stage_id=stage.id, actor="model",
                what=str(what), why=str(why), alternatives=alternatives or [],
                reversible=True,
            ))
            db.commit()

        # 阶段级模型覆盖（画布上单独指定）—— 只影响本阶段，不动全局配置。
        # 「不静默」：用了覆盖就在事件流里说明，用户得知道这一阶段是谁跑的。
        image_cfg = effective_image_config(storage, run.id, stage.id)
        llm_override = effective_llm_model(storage, run.id, stage.id)
        if llm_override:
            bus.emit(db, run.id, EventType.AGENT_THOUGHT,
                     f"本阶段使用单独指定的模型：{llm_override}（画布设置）",
                     stage_id=stage.id, payload={"stage_override": {"llm_model": llm_override}})
        if image_cfg.get("overridden"):
            bus.emit(db, run.id, EventType.AGENT_THOUGHT,
                     f"本阶段使用单独指定的生图模型：{image_cfg.get('model')}（服务 {image_cfg.get('service')}）",
                     stage_id=stage.id, payload={"stage_override": image_cfg})

        ctx = ToolContext(
            run_id=run.id, stage_id=stage.id, brief=dict(run.brief or {}), storage=storage,
            extra={
                "decision_sink": _decision_sink,
                "channel": run.channel,
                "skill_id": skill.id,
                "image_config": image_cfg,
                "llm_model": llm_override,
                # 「谁声明的产物归谁」——write_artifact 靠它拦住跨阶段覆写。
                # 上游清单是下游的输入契约，被改写会让整条流水线读到错误数据
                # （2026-10-16 实测：合成阶段覆写 panels.json 导致整轮失败）。
                "declared_produces": {
                    str(rel).strip().lstrip("./"): st.id
                    for st in skill.stages for rel in (st.produces or [])
                },
            },
        )
        prompt_text = registry.read_prompt(skill, stage)
        system = build_system_prompt(
            skill_md=skill.skill_md,
            stage_name=stage.name,
            stage_desc=stage.description,
            review_focus=stage.review_focus,
            success_criteria=stage.success_criteria,
            tools_whitelist=stage.tools_available,
        )
        user = self._build_user_prompt(run, skill, stage, prompt_text, feedback)

        agent = AgentRunner(on_event=self._evt(db, run.id))
        result = await agent.run(
            ctx, system=system, user=user,
            tools_whitelist=stage.tools_available,
            demo=registry.demo_content(skill, stage),
            stage_id=stage.id,
            model_override=llm_override,
        )

        run.tokens_used += result.tokens
        run.cost_usd += result.cost_usd
        db.commit()

        # ③ 阶段动作：模型交完结构化清单后，由工具做确定性执行（生图 / 配音 / 渲染）。
        #    这是「数据驱动执行」的落点 —— runner 只知道「读哪份清单、调哪个工具」，
        #    完全不知道"漫剧"是什么。加新形态视频 = 改 manifest，不改这个文件。
        action_results = await self._run_actions(db, run, stage, ctx)

        # ④ 校验验收标准（此时工具产出已落盘，二进制产物也一并纳入校验视野）
        file_map = self._read_produced(run.id, stage)
        ok, failures = evaluate_criteria(stage.success_criteria, file_map)

        # ④.0 一致性硬校验（数据驱动：manifest 声明 consistency_checks，runner 只读声明调校验器）。
        #     这是「一致性优先于华丽」的机器化落点 —— 把 SKILL.md 里的软铁律变成硬闸。
        #     校验失败 = 阶段未达标，走与文本验收失败相同的「自动重做」路径。
        consistency_failures = self._run_consistency_checks(run.id, run.channel, stage)
        if consistency_failures:
            ok = False
            failures = list(failures) + consistency_failures

        # ④.1 声明的动作失败 = 成品根本没做出来，不是"标准略低"。
        #      必须并入验收失败，否则会出现"渲染失败 + 成片是假的 + 阶段 completed"。
        action_failures = [
            f"阶段动作 {a.get('tool')} 未成功：{str(a.get('error') or '')[:200]}"
            for a in action_results
            if not a.get("ok")
        ]
        if action_failures:
            ok = False
            failures = list(failures) + action_failures

        if not ok and step.attempt <= quota["max_revisions_per_stage"]:
            bus.emit(db, run.id, EventType.STAGE_REVISION,
                     f"验收未通过（第 {step.attempt} 次），自动重做：{'；'.join(failures)[:200]}",
                     stage_id=stage.id, payload={"failures": failures, "attempt": step.attempt})
            step.status = StepStatus.PENDING
            db.commit()
            return await self._run_stage(db, run, skill, stage, quota,
                                         feedback="上一轮未通过验收：" + "；".join(failures))

        # ④.2 重做次数用尽、媒体动作仍然没成 → 硬失败（禁止"带病标完成"）。
        #      文本类验收未达标仍按现状交付（已在 step.detail 标注），
        #      但"成品没产出"没有可交付的东西，必须如实判失败、让用户续跑。
        if action_failures:
            self._mark_stage_failed(db, step, stage, action_results, failures)
            return {"status": "_stage_failed", "failures": failures}

        # ⑤ 登记产物（用阶段前后的文件快照做 diff，这样 panels/、audio/、video/ 也一并登记）
        new_files = sorted(set(storage.list_files(run.id)) - before_files)
        for rel in new_files:
            self._register_artifact(db, run, stage, rel)

        # ⑥ 需要人工确认 → 开门禁并挂起
        decision: dict = {}
        if stage.human_approval_default:
            decision = await self._open_gate(db, run, skill, stage)
            if decision.get("status") == GateStatus.REVISED:
                return decision
            if decision.get("status") == GateStatus.REJECTED:
                bus.emit(db, run.id, EventType.GATE_RESOLVED, f"关卡未通过：{stage.name}",
                         stage_id=stage.id, payload=decision)
                # 用户拒绝 → 记录后继续（M0 不硬失败，避免演示中断）

        # ⑦ 写 completed 检查点（门禁硬校验：需 human_approved 时已在上一步拿到）
        if stage.checkpoint_required:
            approved = bool(decision.get("status") == GateStatus.APPROVED) if stage.human_approval_default else False
            self._archive_previous(db, run.id, stage.id)
            # 进阶段时写的 in_progress 检查点，此时归档（保留历史，不销毁）
            for cp in (
                db.query(Checkpoint)
                .filter(Checkpoint.run_id == run.id, Checkpoint.stage_id == stage.id,
                        Checkpoint.status == CheckpointStatus.IN_PROGRESS)
                .all()
            ):
                cp.status = CheckpointStatus.SUPERSEDED
                cp.archive_reason = "replaced_by_completed"
            db.commit()
            db.add(Checkpoint(
                run_id=run.id, stage_id=stage.id, status=CheckpointStatus.COMPLETED,
                human_approved=approved,
                payload={"stage_name": stage.name, "files": list(file_map.keys())},
                partial_progress={"attempts": step.attempt},
            ))
            db.commit()

        step.status = StepStatus.COMPLETED
        step.finished_at = datetime.now(timezone.utc)
        step.summary = (result.text or "")[:400]
        step.detail = {
            "attempts": step.attempt,
            "files": list(file_map.keys()),
            "criteria_ok": ok,
            "failures": failures,
            "actions": action_results,
        }
        db.commit()

        bus.emit(db, run.id, EventType.STAGE_COMPLETED,
                 f"阶段完成：{stage.name}", stage_id=stage.id,
                 payload={"files": list(file_map.keys()), "ok": ok})
        return None

    # ---------------- 一致性硬校验（数据驱动） ----------------
    def _run_consistency_checks(self, run_id: str, channel: str, stage: StageDef) -> list[str]:
        """执行阶段声明的确定性一致性校验（如分镜锚点复用）。

        与 stage.actions 同理：runner 不知道「什么算一致」，它只读 manifest 声明的
        consistency_checks，逐条派发给对应校验器。新增校验类型 = 在 consistency.py
        加一个 kind 分支 + 在 manifest 里声明，不动 runner 的流程骨架。

        slow_only 语义：与 actions 的 slow_only 一致 —— 快通道（草稿）下跳过。
        草稿的定位是「3 分钟出方向」，角色一致性是精修通道的事；manifest 的
        draft_gaps 已声明「角色形象没有锁定」。快通道下角色设计阶段根本没跑，
        characters.md 不存在，强行校验只会让草稿永远失败。

        返回失败原因列表；空列表 = 全部通过（或本阶段未声明任何一致性校验）。
        """
        failures: list[str] = []
        for check in stage.consistency_checks or []:
            if check.get("slow_only") and channel == "fast":
                continue
            kind = str(check.get("kind") or "").strip()
            if kind == "panel_anchor_consistency":
                failures.extend(self._check_panel_anchors(run_id, check))
            elif kind:
                # 未识别的一致性校验类型：不静默忽略，如实上报（架构铁律 #2）。
                failures.append(f"一致性校验类型未识别：{kind}（manifest 声明了 runner 不支持的类型）")
        return failures

    def _check_panel_anchors(self, run_id: str, check: dict) -> list[str]:
        """FR-02：分镜格必须复用角色锚点原文（跨格一致性）。

        读 manifest 声明的两个输入：anchors（characters.md，锚点来源）与
        plan（panels.json，被校验的分镜清单）。读不到任一文件 = 上游未完成，
        判失败并说明缺什么，让模型去补，而不是放过。
        """
        plan_rel = str(check.get("plan") or "panels.json")
        anchors_rel = str(check.get("anchors") or "characters.md")

        if not storage.exists(run_id, anchors_rel):
            return [f"一致性校验缺少锚点来源 {anchors_rel}（角色设计未完成，先补角色锚点）"]
        if not storage.exists(run_id, plan_rel):
            return [f"一致性校验缺少分镜清单 {plan_rel}（分镜未产出）"]

        try:
            anchors_md = storage.read_text(run_id, anchors_rel)
        except Exception as exc:  # noqa: BLE001
            return [f"读取锚点来源 {anchors_rel} 失败：{exc}"]

        try:
            import json as _json
            panels = _json.loads(storage.read_text(run_id, plan_rel))
        except Exception as exc:  # noqa: BLE001
            return [f"解析分镜清单 {plan_rel} 失败：{exc}"]

        anchors = extract_anchors(anchors_md)
        ok, reasons = validate_panel_anchors(panels, anchors)
        if ok:
            return []
        # 失败原因里带具体格号，模型据此精准修正，而不是整段重写
        return [f"分镜一致性校验未通过：{r}" for r in reasons]

    # ---------------- 阶段动作（确定性媒体执行） ----------------
    async def _run_actions(
        self, db: Session, run: Run, stage: StageDef, ctx: ToolContext
    ) -> list[dict]:
        """执行 manifest 声明的阶段动作。

        runner 在这里只做三件事：读声明 → 调工具 → 上报事件。
        「调哪个工具、读哪份清单、快通道是否限量」全是数据，写在这个文件之外的 manifest 里。
        """
        results: list[dict] = []
        for act in stage.actions or []:
            act = dict(act or {})
            tool = str(act.get("tool") or "").strip()
            if not tool:
                continue

            if act.get("slow_only") and run.channel == "fast":
                bus.emit(db, run.id, EventType.AGENT_THOUGHT,
                         f"动作 {tool} 只在精修通道执行，草稿阶段跳过（草稿差距清单已声明）",
                         stage_id=stage.id, payload={"tool": tool, "skip": "fast_channel"})
                results.append({"tool": tool, "ok": True, "skipped": "fast_channel"})
                continue

            plan_rel = str(act.get("plan") or "").strip()
            plan = None
            if plan_rel:
                plan = _read_json_artifact(run.id, plan_rel)
                if plan is None:
                    bus.emit(db, run.id, EventType.TOOL_RESULT,
                             f"动作 {tool} 未执行：清单 {plan_rel} 缺失（本阶段按未完成处理）",
                             stage_id=stage.id,
                             payload={"tool": tool, "missing_plan": plan_rel})
                    results.append({"tool": tool, "ok": False, "error": f"缺少清单 {plan_rel}"})
                    continue

            params: dict = {
                k: v for k, v in act.items() if k not in ("tool", "plan", "slow_only")
            }
            if plan is not None:
                params["plan"] = plan

            bus.emit(db, run.id, EventType.TOOL_CALL, f"执行动作：{tool}",
                     stage_id=stage.id,
                     payload={"tool": tool,
                              "inputs": {k: v for k, v in act.items() if k != "plan"}})

            out = await tool_registry.invoke(tool, ctx, **params)
            ok = not (isinstance(out, dict) and out.get("error"))
            if ok and isinstance(out, dict):
                bus.emit(db, run.id, EventType.TOOL_RESULT,
                         str(out.get("summary") or f"动作 {tool} 完成"),
                         stage_id=stage.id,
                         payload={k: v for k, v in out.items() if k not in ("created", "reused")})
                for note in (out.get("degraded") or []):
                    bus.emit(db, run.id, EventType.AGENT_THOUGHT, f"降级说明：{str(note)[:200]}",
                             stage_id=stage.id, payload={"tool": tool, "degraded": note})
            else:
                err = str((out or {}).get("error") if isinstance(out, dict) else out)[:400]
                bus.emit(db, run.id, EventType.TOOL_RESULT,
                         f"动作 {tool} 未成功：{err}", stage_id=stage.id,
                         payload={"tool": tool, "error": err})

            results.append({
                "tool": tool,
                "ok": ok,
                "summary": (out or {}).get("summary") if isinstance(out, dict) else "",
                "error": None if ok else err,
            })
        return results

    # ---------------- 门禁 ----------------
    async def _open_gate(self, db: Session, run: Run, skill: SkillDef, stage: StageDef) -> dict:
        gate = Gate(
            run_id=run.id, stage_id=stage.id, kind="approval",
            status=GateStatus.PENDING,
            title=f"请确认：{stage.name}",
            prompt=stage.description or f"「{stage.name}」已产出，确认后继续，也可以直接给修改意见。",
            proposed={
                "files": stage.produces,
                "review_focus": stage.review_focus,
                "success_criteria": stage.success_criteria,
            },
        )
        db.add(gate)
        run.status = RunStatus.AWAITING_GATE
        db.commit()
        db.refresh(gate)

        bus.emit(db, run.id, EventType.GATE_OPENED, f"等待你确认：{stage.name}",
                 stage_id=stage.id, payload=gate.to_dict())
        self._emit_status(db, run)

        result = await self._wait_gate(run.id, timeout_minutes=settings.max_wall_time_minutes)

        gate.status = result.get("status", GateStatus.APPROVED)
        gate.decision = result.get("decision", {}) or {}
        gate.decided_at = datetime.now(timezone.utc)
        run.status = RunStatus.RUNNING
        db.commit()

        # 用户决定也记入决策留痕
        db.add(Decision(
            run_id=run.id, stage_id=stage.id, actor="user",
            what=f"关卡「{stage.name}」决定：{gate.status}",
            why=str(gate.decision.get("feedback", "")) or "用户确认",
            alternatives=[], reversible=True,
        ))
        db.commit()

        bus.emit(db, run.id, EventType.GATE_RESOLVED,
                 f"关卡已处理：{stage.name} → {gate.status}", stage_id=stage.id,
                 payload=gate.to_dict())
        self._emit_status(db, run)
        return {"gate_id": gate.id, "status": gate.status, "decision": gate.decision}

    # ---------------- 辅助 ----------------
    def _evt(self, db: Session, run_id: str):
        def _emit(type_: str, message: str, stage_id: str = "", payload: dict | None = None):
            bus.emit(db, run_id, type_, message, stage_id=stage_id or None, payload=payload)
        return _emit

    def _build_user_prompt(
        self, run: Run, skill: SkillDef, stage: StageDef, prompt_text: str, feedback: str
    ) -> str:
        parts = [
            f"# 任务\n标题：{run.title}",
            f"技能：{skill.name}",
            f"\n# 用户输入（硬约束/软偏好）\n{self._fmt_brief(run.brief or {})}",
        ]
        channel_ctx = self._channel_context(run, skill)
        if channel_ctx:
            parts.append(f"\n# 本次产出通道\n{channel_ctx}")
        upstream = self._upstream_context(run.id, stage)
        if upstream:
            parts.append(f"\n# 上游产物\n{upstream}")
        if prompt_text:
            parts.append(f"\n# 阶段作业指导\n{prompt_text}")
        if feedback:
            parts.append(f"\n# 上一轮反馈（必须据此修正）\n{feedback}")
        parts.append(
            f"\n# 本阶段要交付的文件\n"
            + ("\n".join(f"- {p}" for p in stage.produces) if stage.produces
               else "- （按你判断产出必要的文件）")
        )
        return "\n".join(parts)

    @staticmethod
    def _channel_context(run: Run, skill: SkillDef) -> str:
        """告诉模型它现在是在交草稿还是交成品 —— 草稿诚实性的第一道闸。

        模型不必假装完美：快通道下它被明确允许"粗一点"，但简化必须写进决策留痕。
        """
        if run.channel == "fast":
            lines = [
                f"这是**快通道草稿**（目标：{settings.fast_channel_target_seconds} 秒内交出可判断方向的最小成品）。",
                "- 优先把「能让人一眼看出方向对不对」的东西做出来，细节可以粗。",
                "- 你做的每一处简化或不精确，必须用 record_decision 写清「简化了什么、为什么、后续怎么补」，不要默默省略。",
            ]
            gaps = skill.draft_gaps()
            if gaps:
                lines.append("- 已知本通道不覆盖（用户稍后会看到这份清单，无需你重复声明）：")
                lines.extend(f"  · {g}" for g in gaps)
            return "\n".join(lines)
        return (
            "这是**精修通道**（目标是可直接交付）。上游草稿阶段的产物已存在，"
            "你应该在上游结论的基础上深化，而不是推翻重来；确需推翻时必须记录决策依据。"
        )

    @staticmethod
    def _fmt_brief(brief: dict) -> str:
        return "\n".join(f"- {k}：{v}" for k, v in brief.items() if str(v).strip()) or "-（未填写，请自行采用稳妥默认值）"

    def _upstream_context(self, run_id: str, stage: StageDef) -> str:
        """把已产出的文件作为上游上下文（长任务必须让下游看到上游结论）。

        实测教训（2026-10-16，任务 db0874408b 整轮失败的直接原因之一）：
        旧实现是 `parts[-4:]` —— 只保留**字母序最后 4 个**文件。而 panels.json /
        lines.json 排在 panels/ 目录与 script.md 之前，被整段丢掉；合成阶段的模型
        因此认为"上游没给清单"，自己重建了一份裸数组并覆盖原文件，把流水线带崩。

        结论：**JSON 清单是数据契约，必须完整送达**，不能和说明文档一起按"最近几个"截断。
        现在的策略：JSON 清单优先全带（逐份截 6k，共用 60k 字符预算），
        说明文档只带最近 3 份；真超预算时从头截断并显式标注。
        """
        manifests: list[str] = []   # .json 数据契约：优先级最高，缺一份就可能带崩下游
        docs: list[str] = []        # .md/.txt 说明文档：只带最近几份
        for rel in storage.list_files(run_id):
            if rel in stage.produces:
                continue
            if rel.lower().endswith(_UPSTREAM_SKIP_EXT):
                continue
            try:
                txt = storage.read_text(run_id, rel)
            except Exception:  # noqa: BLE001  二进制/编码问题：跳过，不算上游文本
                continue
            if len(txt) > 6000:
                txt = txt[:6000] + "\n…（已截断）"
            block = f"## {rel}\n{txt}"
            (manifests if rel.lower().endswith(".json") else docs).append(block)

        selected = manifests + docs[-3:]
        if not selected:
            return ""
        joined = "\n\n".join(selected)
        if len(joined) > 60000:
            joined = "…（上游内容过长，较早的部分未随上下文送达；如需请用 read_artifact 读取）\n\n" + joined[-60000:]
        return joined

    def _read_produced(self, run_id: str, stage: StageDef) -> dict[str, str]:
        out: dict[str, str] = {}

        def _load(rel: str) -> str | None:
            if not storage.exists(run_id, rel):
                return None
            try:
                return storage.read_text(run_id, rel)
            except Exception:  # noqa: BLE001
                # 二进制产物（mp4/mp3/png）读不出文本：用大小占位，
                # 这样 manifest 里的「文件 video/final.mp4 存在且非空」才真的可校验。
                try:
                    size = storage.abs_path(run_id, rel).stat().st_size
                except Exception:  # noqa: BLE001
                    size = 0
                return f"<binary {rel} {size} bytes>"

        for rel in stage.produces:
            txt = _load(rel)
            if txt is not None:
                out[rel] = txt
        # 模型可能产出了 produces 之外的补充文件，一并纳入校验视野
        for rel in storage.list_files(run_id):
            if rel not in out and rel.endswith((".md", ".json", ".txt")):
                txt = _load(rel)
                if txt is not None:
                    out[rel] = txt
        return out

    def _register_artifact(self, db: Session, run: Run, stage: StageDef, rel: str) -> None:
        p = storage.abs_path(run.id, rel)
        kind = "file"
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        kind = {"md": "markdown", "json": "json", "svg": "image", "png": "image",
                "mp3": "audio", "wav": "audio", "mp4": "video", "pptx": "pptx",
                "docx": "docx"}.get(ext, "file")
        db.add(Artifact(
            run_id=run.id, stage_id=stage.id, kind=kind, name=rel.rsplit("/", 1)[-1],
            rel_path=rel, size=p.stat().st_size if p.exists() else 0, mime="",
            is_draft=(run.channel == "fast"), is_sample=("sample" in stage.id),
        ))
        db.commit()

        # 北极星指标计量点：第一个可用成品落盘的那一刻（MRD §5.1 / D9）。
        # 实测它，而不是宣称它 —— 指标不可计量就等于没有指标。
        if run.ttfv_seconds is None:
            base = _aware(run.started_at or run.created_at)
            run.ttfv_seconds = max(0.0, (datetime.now(timezone.utc) - base).total_seconds())
            db.commit()
            target_s = settings.fast_channel_target_seconds
            ok = run.ttfv_seconds <= target_s
            bus.emit(db, run.id, EventType.DRAFT_READY,
                     f"首个成品已就绪：{rel}（用时 {run.ttfv_seconds:.1f} 秒"
                     f"{'，达标' if ok else f'，超过 {target_s} 秒目标'}）",
                     stage_id=stage.id,
                     payload={"ttfv_seconds": round(run.ttfv_seconds, 2),
                              "target_seconds": target_s, "within_target": ok,
                              "file": rel})

    def _archive_previous(self, db: Session, run_id: str, stage_id: str) -> None:
        """被取代的检查点归档，永不销毁（保留可回溯的完整历史）。"""
        rows = (
            db.query(Checkpoint)
            .filter(Checkpoint.run_id == run_id, Checkpoint.stage_id == stage_id,
                    Checkpoint.status == CheckpointStatus.COMPLETED)
            .all()
        )
        for r in rows:
            r.status = CheckpointStatus.SUPERSEDED
            r.archive_reason = "superseded_by_rerun"
        db.commit()

    def _prepare_steps(self, db: Session, run: Run, stages: list[StageDef]) -> None:
        """按当前阶段序列建立/校准 Step 记录。

        每次都以传入序列为准重排 index —— 草稿升级为精修时阶段集合会变，
        不重排会出现两个 Step 撞同一个 index，进度树顺序就乱了。
        """
        existing = {s.stage_id: s for s in db.query(Step).filter(Step.run_id == run.id).all()}
        for i, s in enumerate(stages):
            row = existing.get(s.id)
            if row is None:
                db.add(Step(run_id=run.id, stage_id=s.id, stage_name=s.name, index=i))
            else:
                row.index = i
                row.stage_name = s.name or row.stage_name
        db.commit()

    def _get_step(self, db: Session, run_id: str, stage_id: str) -> Step | None:
        return (
            db.query(Step)
            .filter(Step.run_id == run_id, Step.stage_id == stage_id)
            .first()
        )

    def _emit_status(self, db: Session, run: Run) -> None:
        bus.emit(db, run.id, EventType.RUN_STATUS, f"状态：{run.status}",
                 payload={"status": run.status})

    def _finish(self, db: Session, run: Run, status: str, note: str = "") -> None:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        run.progress = 1.0 if status == RunStatus.SUCCEEDED else run.progress
        db.commit()
        bus.emit(db, run.id, EventType.RUN_FINISHED,
                 note or ("任务完成" if status == RunStatus.SUCCEEDED else "任务结束"),
                 payload={"status": status})
        self._emit_status(db, run)

    def _mark_stage_failed(
        self,
        db: Session,
        step: Step,
        stage: StageDef,
        action_results: list[dict],
        failures: list[str],
    ) -> None:
        """阶段确实没产出成品：如实标失败并把原因留在 step.detail 与事件流里。"""
        step.status = StepStatus.FAILED
        step.finished_at = datetime.now(timezone.utc)
        step.detail = {
            "attempts": step.attempt,
            "criteria_ok": False,
            "failures": failures,
            "actions": action_results,
        }
        db.commit()
        bus.emit(db, step.run_id, EventType.STAGE_FAILED,
                 f"阶段失败：{stage.name} —— {'；'.join(failures)[:240]}",
                 stage_id=stage.id, payload={"failures": failures, "actions": action_results})

    def _fail(self, db: Session, run: Run, error: str) -> None:
        run.status = RunStatus.FAILED
        run.error = error[:1000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        bus.emit(db, run.id, EventType.RUN_ERROR, f"执行失败：{error[:300]}",
                 payload={"error": error})
        self._emit_status(db, run)

    def _cancel(self, db: Session, run: Run) -> None:
        run.status = RunStatus.CANCELLED
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        bus.emit(db, run.id, EventType.RUN_FINISHED, "任务已被取消", payload={"status": "cancelled"})
        self._emit_status(db, run)


manager = RunManager()


# --------------------------------------------------------------------------- #
# 启动时回收"孤儿任务"
# --------------------------------------------------------------------------- #
# 为什么需要它：RunManager 的任务只活在进程内存里（asyncio.Task）。后端一旦重启
# ——包括 `uvicorn --reload` 因为我们自己改了一行代码而自动重启——正在执行的 Run
# 会瞬间变成"没人管的 running"：事件流停在半路、进度条不动、界面上永远转圈，
# 而 /upgrade 这类接口又会以"任务正在执行中"为由拒绝它。这就是一次真实的卡死事故。
#
# 恢复策略遵守项目的铁律：**不静默**。启动时把这些 Run 如实标成 interrupted、
# 写清原因、在事件流留痕，已完成阶段的产物原样保留，用户可一键续跑。
LIVE_STATUSES = (RunStatus.RUNNING, RunStatus.AWAITING_GATE, RunStatus.PENDING)


def recover_orphaned_runs(db: Session) -> list[str]:
    """把"内存里已无任务、DB 里还写着活着"的 Run 标记为中断。返回被恢复的 run_id 列表。"""
    orphans = db.query(Run).filter(Run.status.in_(LIVE_STATUSES)).all()
    recovered: list[str] = []
    for run in orphans:
        task = manager._tasks.get(run.id)
        if task is not None and not task.done():
            continue  # 本进程里确实还活着（同进程重启不会出现，留作保险）

        now = datetime.now(timezone.utc)
        steps = (
            db.query(Step)
            .filter(Step.run_id == run.id, Step.status == StepStatus.IN_PROGRESS)
            .all()
        )
        stage_label = (steps[0].stage_name or steps[0].stage_id) if steps else (run.current_stage_id or "")
        for s in steps:
            s.status = StepStatus.INTERRUPTED
            s.finished_at = now
            if not s.summary:
                s.summary = "该阶段执行中被进程重启打断，续跑时会重跑"

        run.status = RunStatus.INTERRUPTED
        run.finished_at = now
        run.error = (
            "后端进程重启，正在执行的阶段被中断"
            + (f"（停在「{stage_label}」）" if stage_label else "")
            + "。已完成阶段的产物已保留，可直接续跑。"
        )
        db.commit()

        bus.emit(
            db, run.id, EventType.RUN_ERROR,
            f"任务中断：后端进程重启，阶段「{stage_label}」未跑完（已完成阶段的产物保留）",
            stage_id=steps[0].stage_id if steps else None,
            payload={"status": RunStatus.INTERRUPTED, "reason": "process_restart",
                     "stage_id": steps[0].stage_id if steps else None, "resumable": True},
        )
        bus.emit(db, run.id, EventType.RUN_STATUS, f"状态：{RunStatus.INTERRUPTED}",
                 payload={"status": RunStatus.INTERRUPTED, "resumable": True})
        logger.warning("回收中断任务 %s（停在 %s）", run.id, stage_label or "未开始阶段")
        recovered.append(run.id)
    return recovered
