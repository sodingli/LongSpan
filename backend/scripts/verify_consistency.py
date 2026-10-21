"""真实验证脚本：跑一条精修通道（slow）的双主角故事，验证人物一致性整改效果。

与 smoke.py 的区别：
- 题材换成双主角（两个视觉区分度明显的角色），验证多角色各自跨格一致性
- 固定 slow 通道（精修通道才跑 C4 定妆图 + C5 锚点硬校验 + 参考图生图）
- 跑完额外输出「参考图通道是否被触发」的判定依据，供验证报告引用

用法：
    python3 scripts/verify_consistency.py

真实调用 LLM + 生图 + 视频，耗时数分钟、有真实费用。
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
from app.models import Artifact, Gate, GateStatus, Run  # noqa: E402
from app.store import storage  # noqa: E402

OK, BAD, DIM, END = "\033[92m", "\033[91m", "\033[90m", "\033[0m"


async def main() -> int:
    init_db()
    skills = registry.scan()
    print(f"{DIM}已加载技能：{list(skills.keys())}{END}")
    skill = registry.require("comic-drama")

    db = SessionLocal()
    run = Run(
        skill_id=skill.id,
        skill_name=skill.name,
        title="真实验证 · 双主角一致性",
        brief={
            "idea": (
                "失忆的年轻女调香师与沉默寡言的老钟表匠，在一座海边小城相遇，"
                "两人各自藏着一段未愈合的往事，最终在一只停摆的怀表里找到彼此缺失的记忆。"
                "双主角故事：女调香师（年轻、长发、浅色系服装）与老钟表匠（白发、戴单边眼镜、深色围裙）。"
            )
        },
        channel="slow",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id
    storage.run_workspace(run_id)
    print(f"Run: {run_id}  通道: slow（精修，含定妆图 + 锚点硬校验 + 参考图生图）")
    print("-" * 78)

    t0 = time.monotonic()
    manager.start(run_id)

    last_status = None
    while True:
        await asyncio.sleep(0.5)
        db2 = SessionLocal()
        try:
            r = db2.get(Run, run_id)

            for g in db2.query(Gate).filter(Gate.run_id == run_id, Gate.status == GateStatus.PENDING):
                print(f"{OK}  ▶ 自动确认关卡：{g.title}{END}")
                manager.resolve_gate(run_id, g.id, GateStatus.APPROVED,
                                     {"feedback": "verify 自动确认", "patches": {}})

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
        char_art = []
        panels = []
        video_clips = []
        for f in files:
            size = storage.abs_path(run_id, f).stat().st_size
            print(f"  - {f}  ({size} bytes)")
            if f.startswith("characters/") and f.endswith((".jpg", ".png", ".svg")):
                char_art.append(f)
            if f.startswith("panels/") and f.endswith((".jpg", ".png")):
                panels.append(f)
            if f.startswith("panels/") and f.endswith(".mp4"):
                video_clips.append(f)

        print(f"\n=== 一致性整改生效判定 ===")
        print(f"定妆图数量（characters/）: {len(char_art)}")
        for c in char_art:
            print(f"    · {c}")
        print(f"真实分镜图数量（panels/*.jpg|png）: {len(panels)}")
        has_ref = any(not c.endswith('.svg') for c in char_art)
        print(f"定妆图为真实生图（非 SVG 兜底）: {has_ref}")

        print(f"\n=== 图生视频（关键格）生效判定 ===")
        print(f"视频片段数量（panels/*.mp4）: {len(video_clips)}")
        for v in video_clips:
            print(f"    · {v}")

        # 输出角色定妆图和分镜图清单，供后续人工看图比对一致性
        print(f"\n=== 供人工比对一致性的图片路径 ===")
        print("RUN_ID=" + run_id)
        for c in char_art:
            print("CHARACTER_ART: " + c)
        for p in panels:
            print("PANEL: " + p)
        for v in video_clips:
            print("VIDEO_CLIP: " + v)

        print("\n阶段结论：")
        from app.models import Step
        for s in db3.query(Step).filter(Step.run_id == run_id).order_by(Step.index).all():
            print(f"  [{s.status:11}] {s.stage_name:12} attempt={s.attempt}  "
                  f"files={','.join(s.detail.get('files', [])) or '-'}  "
                  f"criteria_ok={s.detail.get('criteria_ok')}")

        ok = r.status == "succeeded" and len(files) > 0
        print("\n" + (f"{OK}✔ 真实验证完成{END}" if ok else f"{BAD}✘ 真实验证失败{END}"))
        return 0 if ok else 1
    finally:
        db3.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
