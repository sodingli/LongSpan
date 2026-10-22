#!/usr/bin/env python3
"""周报视图：全部任务的三个数（ROADMAP §零.2）。

用法：
    python3 scripts/report_metrics.py            # 最近 10 集
    python3 scripts/report_metrics.py --all      # 全部
    python3 scripts/report_metrics.py <run_id>   # 单集明细（含违约逐条）

判定线（MRD §0.6）：返工率 ≤1/12 ≈ 0.083；结构违约 = 0；等人占比 ≤30%。
数不改善的周，下周不许加新功能（ROADMAP §零.2）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402
from app.harness.metrics import compute_run_metrics  # noqa: E402
from app.models import Run  # noqa: E402


def _fmt(v, pct: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.1f}%" if pct else f"{v:g}"


def _verdict(m: dict) -> str:
    v = m["verdicts"]
    marks = []
    for key, label in (("rework", "返工"), ("violations_zero", "违约"), ("human_share_ok", "等人")):
        val = v.get(key)
        marks.append("✓" if val else ("✗" if val is not None else "-"))
    return "".join(marks)


def main() -> None:
    db = SessionLocal()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv

    if args:
        run = db.query(Run).filter(Run.id == args[0]).first()
        if run is None:
            print(f"任务不存在：{args[0]}")
            return
        m = compute_run_metrics(db, run)
        print(f"任务：{m['title']}（{m['status']}，{m['panel_count'] or '?'} 格）")
        print(f"① 返工率：{m['rework_rate']}（自动重做 {m['rework_rounds']} 轮 + 单格重画 {m['panel_redraws']} 次）")
        print(f"② 结构违约：{m['violation_count']} 条")
        for v in m["violations"]:
            print(f"   - [{v['stage']}] {v['text']}")
        print(f"③ TTFV：ttfv={_fmt(m['ttfv_seconds'])}s 机器={_fmt(m['machine_seconds'])}s "
              f"等人={_fmt(m['wait_seconds'])}s（占比 {_fmt(m['human_share'], pct=True)}）")
        print(f"判定线：{_verdict(m)}  （返工/违约/等人）")
        return

    q = db.query(Run).order_by(Run.created_at.desc())
    runs = q.all() if show_all else q.limit(10).all()
    print(f"{'判定':4s} {'违约':>4s} {'返工率':>7s} {'等人':>7s} {'机器':>8s}  {'状态':10s} 标题")
    print("-" * 84)
    for run in runs:
        try:
            m = compute_run_metrics(db, run)
        except Exception:  # noqa: BLE001
            continue
        print(f"{_verdict(m):4s} {m['violation_count']:4d} "
              f"{_fmt(m['rework_rate']):>7s} {_fmt(m['human_share'], pct=True):>7s} "
              f"{_fmt(m['machine_seconds']) + 's':>8s}  {m['status']:10s} {m['title'][:24]}")
    print("-" * 84)
    print("判定线：返工 ≤0.083 · 违约 = 0 · 等人 ≤30%   ✓=达标 ✗=未达标 -=无法判定")


if __name__ == "__main__":
    main()
