"""每集三个数（ROADMAP-8周 §零.2）：返工率、结构违约数、TTFV 分段（机器/等人）。

设计立场：**不新增埋点**。这三个数全部从既有留痕算出来 ——
steps 的 attempt/时间戳、events 的 stage.revision（含逐条违约明细）、
gates 的创建→决策时刻、runs.ttfv_seconds。留痕已经在那里，缺的只是汇总。
这也是「留痕是资产」的直接兑现：换 skill、换题材，指标照算。

口径（写死在这里，不要在别处各算一套）：
- 返工率 = (分镜自动重做轮次 + 单格人工重画次数) / 格数。判定线 ≤1/12。
- 结构违约数 = Σ stage.revision 事件 payload.failures 的条数（每条带格号）。
- 机器耗时 = Σ 各 stage (finished_at - started_at)；等人 = Σ 各 gate (decided_at - created_at)
  （未决 gate 计到当前时刻）。等人占比 = 等人 / 总时长。判定线 ≤30%。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Event, Gate, Run, Step


def _sec(a: datetime | None, b: datetime | None) -> float:
    if not a or not b:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def _panel_count(run: Run) -> int | None:
    """从工作区 panels.json 读格数；读不到（草稿未到分镜）返回 None。"""
    try:
        from ..store import storage

        ws = storage.run_workspace(run.id)
        p = ws / "panels.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        items = data.get("panels") if isinstance(data, dict) else data
        if isinstance(items, list):
            return len(items)
    except Exception:  # noqa: BLE001 —— 指标计算绝不影响主流程
        return None
    return None


def compute_run_metrics(db: Session, run: Run) -> dict[str, Any]:
    """从既有留痕汇总一集的三个数。只读，不写库。"""
    events = (
        db.query(Event)
        .filter(Event.run_id == run.id)
        .order_by(Event.created_at)
        .all()
    )

    # --- 结构违约：stage.revision 事件逐条展开（每条自带格号，可直接改） ---
    violations: list[dict[str, Any]] = []
    rework_rounds = 0
    panel_redraws = 0
    for e in events:
        payload = e.payload if isinstance(e.payload, dict) else {}
        if e.type == "stage.revision":
            rework_rounds += 1
            for f in payload.get("failures") or []:
                violations.append({"stage": e.stage_id, "text": str(f)[:200]})
        elif e.type == "run.status" and payload.get("human_edit") == "regenerate":
            panel_redraws += 1

    # --- 机器耗时：各 stage 执行时段求和（阶段串行，无重叠） ---
    steps = db.query(Step).filter(Step.run_id == run.id).all()
    machine_seconds = sum(_sec(s.started_at, s.finished_at) for s in steps)

    # --- 等人：gate 创建 → 决策，与 run 活跃窗口求交（区间并集，重叠不双算） ---
    # 实测坑（run 233a8a68）：characters gate 挂了 7 小时才被批，但 run 早已结束 ——
    # 残留批准的 decided_at 晚于 run.finished_at，盲目累加会把"用户睡觉的时间"
    # 算进等人，等人占比飙到 819%。所以必须夹在 run 窗口内。
    now = datetime.now(timezone.utc)
    run_end = run.finished_at or now
    run_start = run.created_at or run.started_at or run_end
    windows: list[tuple[datetime, datetime]] = []
    for g in db.query(Gate).filter(Gate.run_id == run.id).all():
        start = max(g.created_at or run_start, run_start)
        end = min(g.decided_at or now, run_end)
        if end > start:
            windows.append((start, end))
    wait_seconds = 0.0
    if windows:
        windows.sort()
        merged_end = windows[0][0]
        cur_start, cur_end = windows[0]
        for s, e in windows[1:]:
            if s > cur_end:
                wait_seconds += (cur_end - cur_start).total_seconds()
                cur_start, cur_end = s, e
            else:
                cur_end = max(cur_end, e)
        wait_seconds += (cur_end - cur_start).total_seconds()
    pending_gates = sum(
        1 for g in db.query(Gate).filter(Gate.run_id == run.id).all()
        if not g.decided_at and (g.created_at or run_start) < run_end
    )

    panel_count = _panel_count(run)
    total_seconds = _sec(run.created_at, run.finished_at or now)
    rework_total = rework_rounds + panel_redraws

    return {
        "run_id": run.id,
        "title": run.title,
        "status": run.status,
        "panel_count": panel_count,
        # ① 返工率（判定线 ≤1/12 ≈ 0.083）
        "rework_rounds": rework_rounds,
        "panel_redraws": panel_redraws,
        "rework_rate": round(rework_total / panel_count, 4) if panel_count else None,
        # ② 结构违约数（判定线 = 0；明细每条带格号/阶段）
        "violation_count": len(violations),
        "violations": violations[:40],
        # ③ TTFV 分段（判定线：等人占比 ≤30%）
        "ttfv_seconds": round(run.ttfv_seconds, 2) if run.ttfv_seconds is not None else None,
        "machine_seconds": round(machine_seconds, 1),
        "wait_seconds": round(wait_seconds, 1),
        "total_seconds": round(total_seconds, 1),
        "human_share": round(wait_seconds / total_seconds, 4) if total_seconds else None,
        "pending_gates": pending_gates,
        # 判定线速览（MRD §0.6 三条线）
        "verdicts": {
            "rework": (rework_total / panel_count <= 1 / 12) if panel_count else None,
            "violations_zero": len(violations) == 0,
            "human_share_ok": (wait_seconds / total_seconds <= 0.30) if total_seconds else None,
        },
        "computed_at": now.isoformat(),
    }


def save_run_metrics(storage: Any, run_id: str, metrics: dict[str, Any]) -> str:
    """指标落盘到工作区（留痕可复核，换模型/换 skill 都能对齐比较）。"""
    storage.write_json(run_id, "metrics.json", metrics)
    return "metrics.json"
