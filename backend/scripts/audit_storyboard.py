#!/usr/bin/env python3
"""分镜规范自检 —— 不开任务、不花一分钱，审计一集分镜是否守住了"演出约束"。

为什么需要它（MRD v1.4 §0.2）：
    约束如果只能靠"跑一整集、看成品、凭感觉"，那它就不是规范，是期望。
    这个脚本把 `directing.shot_budget` 的判据搬到命令行，让"这集分镜结构有没有问题"
    变成一个 0 成本的确定性回答 —— 而且能在**生图之前**就回答。

用法：
    python scripts/audit_storyboard.py 233a8a686373471bb3cbce6d2ef24de0
    python scripts/audit_storyboard.py .data/artifacts/<run_id> --skill comic-drama
    python scripts/audit_storyboard.py <dir> --budget max_distinct_scenes=4

输出：场景序列、景别序列、逐条违规原因、退出码（0 通过 / 1 不通过 / 2 读不到产物）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.harness.consistency import (  # noqa: E402
    normalize_shot_size,
    validate_scene_references,
    validate_shot_budget,
)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"！{path.name} 解析失败：{exc}")
        return None


def resolve_dir(target: str) -> Path:
    p = Path(target)
    if p.is_dir():
        return p
    candidate = BACKEND.parent / ".data" / "artifacts" / target
    if candidate.is_dir():
        return candidate
    raise SystemExit(f"找不到任务产物目录：{target}")


def budget_from_manifest(skill: str, stage_id: str = "storyboard") -> dict:
    """从 manifest 读真实预算 —— 自检必须用**线上同一份**数据，否则校验没有意义。"""
    if yaml is None:
        return {}
    manifest = BACKEND / "skills" / skill / "manifest.yaml"
    if not manifest.exists():
        return {}
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    for stage in raw.get("stages", []) or []:
        if stage.get("id") == stage_id:
            return dict((stage.get("directing") or {}).get("shot_budget") or {})
    return {}


def panels_of(payload) -> list[dict]:
    if isinstance(payload, dict):
        for k in ("panels", "items", "data", "list"):
            if isinstance(payload.get(k), list):
                return [x for x in payload[k] if isinstance(x, dict)]
        return []
    return [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="run_id 或产物目录")
    ap.add_argument("--skill", default="comic-drama")
    ap.add_argument("--stage", default="storyboard")
    ap.add_argument("--budget", nargs="*", default=[], help="覆盖预算，如 max_distinct_scenes=4")
    args = ap.parse_args()

    run_dir = resolve_dir(args.target)
    plan = load_json(run_dir / "panels.json")
    if plan is None:
        print(f"！{run_dir} 下没有 panels.json，无法自检")
        return 2
    scenes = load_json(run_dir / "scenes.json")

    budget = budget_from_manifest(args.skill, args.stage)
    for item in args.budget:
        if "=" in item:
            k, v = item.split("=", 1)
            try:
                budget[k.strip()] = int(v)
            except ValueError:
                budget[k.strip()] = v.strip().lower() in ("1", "true", "yes")

    panels = panels_of(plan)
    seq = [str(p.get("scene_id") or "?").strip() for p in panels]
    shots = [normalize_shot_size(p.get("shot_size") or p.get("scene")) or "?" for p in panels]

    print(f"任务目录：{run_dir}")
    print(f"格数：{len(panels)}")
    print(f"场景序列：{' '.join(seq)}")
    print(f"景别序列：{' '.join(shots)}")
    if scenes is None:
        print("场景卡：缺失（scenes.json 不存在）")
    else:
        print(f"场景卡：{len((scenes.get('scenes') if isinstance(scenes, dict) else scenes) or [])} 个")

    ok_budget, reasons_budget = validate_shot_budget(plan, budget)
    ok_scene, reasons_scene = validate_scene_references(plan, scenes)

    violations = reasons_budget + reasons_scene
    print()
    if violations:
        print(f"不通过（{len(violations)} 条）：")
        for r in violations:
            print(f"  - {r}")
    else:
        print("通过：分镜结构与场景卡引用均符合 directing 声明")

    ok = ok_budget and ok_scene
    print()
    print("结论：" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
