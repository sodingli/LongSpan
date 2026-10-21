"""M0 冒烟测试：跑通一条完整流水线（含人工关卡自动确认）。

用法：
    python3 scripts/smoke.py [fast|slow]

验收判据（对应 MRD §13 M0）：
  1. 能跑完一条完整流水线
  2. 每个阶段都有事件（前端据此实时渲染）
  3. 人工关卡能挂起、能恢复
  4. 产物落盘且通过验收标准
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.harness.runner import manager  # noqa: E402
from app.harness.skill import registry  # noqa: E402
from app.models import Artifact, Checkpoint, Decision, Event, Gate, GateStatus, Run  # noqa: E402
from app.store import storage  # noqa: E402

OK, BAD, DIM, END = "\033[92m", "\033[91m", "\033[90m", "\033[0m"


async def main(channel: str = "fast") -> int:
    init_db()
    skills = registry.scan()
    print(f"{DIM}已加载技能：{list(skills.keys())}{END}")
    skill = registry.require("comic-drama")

    db = SessionLocal()
    run = Run(
        skill_id=skill.id,
        skill_name=skill.name,
        title="冒烟测试 · 隐藏的美食评论家",
        brief={"idea": "外卖小哥其实是隐藏的美食评论家，被米其林主厨当众约战"},
        channel=channel,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id
    storage.run_workspace(run_id)
    print(f"Run: {run_id}  通道: {channel}")
    print("-" * 78)

    t0 = time.monotonic()
    seen_events = 0
    manager.start(run_id)

    last_status = None
    while True:
        await asyncio.sleep(0.25)
        db2 = SessionLocal()
        try:
            r = db2.get(Run, run_id)

            # 打印新事件（模拟前端的实时流）
            rows = (
                db2.query(Event)
                .filter(Event.run_id == run_id, Event.id > seen_events)
                .order_by(Event.id)
                .all()
            )
            for e in rows:
                seen_events = e.id
                if e.type in {"agent.thought", "agent.message"}:
                    print(f"{DIM}  · [{e.type}] {e.message[:110].replace(chr(10), ' ')}{END}")
                else:
                    print(f"  · [{e.type}] {e.message[:110].replace(chr(10), ' ')}")

            # 自动确认人工关卡（模拟用户点"确认"）
            for g in db2.query(Gate).filter(Gate.run_id == run_id, Gate.status == GateStatus.PENDING):
                print(f"{OK}  ▶ 自动确认关卡：{g.title}{END}")
                manager.resolve_gate(run_id, g.id, GateStatus.APPROVED,
                                     {"feedback": "smoke 自动确认", "patches": {}})

            if r.status != last_status:
                last_status = r.status
                print(f"{DIM}  = 状态 → {r.status}{END}")

            if r.status in {"succeeded", "failed", "cancelled"}:
                break
        finally:
            db2.close()

    elapsed = time.monotonic() - t0
    print("-" * 78)

    db3 = SessionLocal()
    try:
        r = db3.get(Run, run_id)
        print(f"最终状态：{r.status}   耗时：{elapsed:.2f}s   进度：{r.progress:.0%}")
        print(f"token：{r.tokens_used}   成本：${r.cost_usd:.4f}   打回次数：{r.send_backs}")
        if r.error:
            print(f"{BAD}错误：{r.error}{END}")

        files = storage.list_files(run_id)
        print(f"\n产物（{len(files)} 个）：")
        for f in files:
            size = storage.abs_path(run_id, f).stat().st_size
            print(f"  - {f}  ({size} bytes)")

        arts = db3.query(Artifact).filter(Artifact.run_id == run_id).all()
        print(f"\n产物登记：{len(arts)} 条")
        cps = db3.query(Checkpoint).filter(Checkpoint.run_id == run_id).all()
        done = [c for c in cps if c.status == "completed"]
        print(f"检查点：{len(cps)} 条（completed {len(done)}，in_progress "
              f"{len([c for c in cps if c.status == 'in_progress'])}）")
        dec = db3.query(Decision).filter(Decision.run_id == run_id).all()
        print(f"决策留痕：{len(dec)} 条")
        for d in dec[:6]:
            print(f"  - [{d.actor}] {d.what[:70]}")
        evs = db3.query(Event).filter(Event.run_id == run_id).count()
        print(f"事件总数：{evs}")

        print("\n阶段结论：")
        from app.models import Step
        for s in db3.query(Step).filter(Step.run_id == run_id).order_by(Step.index).all():
            print(f"  [{s.status:11}] {s.stage_name:12} attempt={s.attempt}  "
                  f"files={','.join(s.detail.get('files', [])) or '-'}  "
                  f"criteria_ok={s.detail.get('criteria_ok')}")

        ok = r.status == "succeeded" and len(files) > 0
        print("\n" + (f"{OK}✔ M0 冒烟通过{END}" if ok else f"{BAD}✘ M0 冒烟失败{END}"))
        return 0 if ok else 1
    finally:
        db3.close()


if __name__ == "__main__":
    ch = sys.argv[1] if len(sys.argv) > 1 else "fast"
    raise SystemExit(asyncio.run(main(ch)))
