#!/usr/bin/env python3
"""批量视觉体检（FR-08 一致性抽检的雏形）—— 让脚本替你「看一遍片」。

对指定任务（或全部历史任务）的分镜图逐格跑 qwen3-vl-plus 构图检查，
输出：哪一格有缺陷、依据是什么、建议怎么改。报告落盘到工作区 vision_audit.json。

用法：
    python3 scripts/audit_vision.py <run_id>          # 单集体检（只读）
    python3 scripts/audit_vision.py --all             # 全部有分镜图的任务
    python3 scripts/audit_vision.py <run_id> --fix    # 问题格自动补取景声明并重画

设计约束（与 vision_check.py 同源）：
    - 只判可判定的结构缺陷，不做审美评判；
    - 检查失败不当作"有问题"（宁漏报不误报，误报会引发无意义重画）；
    - prompt 已声明局部特写的格用 allow_headless 口径（没有头是预期）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.harness.consistency import _FRAMING_DECL_RE  # noqa: E402
from app.media.vision_check import inspect_image, patch_framing_prompt  # noqa: E402
from app.store import storage  # noqa: E402

BACKEND = "http://127.0.0.1:8000"
CONCURRENCY = 3


def _runs_with_panels() -> list[str]:
    root = Path(storage.root)
    out = []
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir() and (d / "panels.json").exists() and list(d.glob("panels/panel-*.png")):
            out.append(d.name)
    return out


def _panels_of(run_id: str) -> list[dict]:
    data = json.loads((Path(storage.root) / run_id / "panels.json").read_text())
    items = data.get("panels") if isinstance(data, dict) else data
    return items or []


def _prompt_of(p: dict) -> str:
    return str(p.get("prompt") or p.get("image_prompt") or p.get("scene") or p.get("description") or "")


async def audit_run(run_id: str) -> dict:
    """体检一个 run 的全部分镜图。返回报告 dict（可直接落盘）。"""
    panels = _panels_of(run_id)
    ws = Path(storage.root) / run_id
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(p: dict) -> dict:
        idx = str(p.get("index") or "")
        rel = f"panels/panel-{int(idx):02d}.png"
        path = ws / rel
        if not path.exists():
            return {"index": idx, "checked": False, "ok": True, "issue": "图不存在", "path": rel}
        prompt = _prompt_of(p)
        allow = bool(_FRAMING_DECL_RE.search(prompt))
        async with sem:
            r = await inspect_image(path, allow_headless=allow)
        r.update({"index": idx, "path": rel, "allow_headless": allow})
        return r

    results = await asyncio.gather(*(one(p) for p in panels))
    results = sorted(results, key=lambda r: int(r["index"]) if str(r["index"]).isdigit() else 999)
    bad = [r for r in results if r.get("checked") and not r.get("ok")]
    unchecked = [r for r in results if not r.get("checked")]
    return {
        "run_id": run_id,
        "total": len(results),
        "checked": len(results) - len(unchecked),
        "defects": len(bad),
        "results": results,
        "bad": [r["index"] for r in bad],
        "unchecked": [r["index"] for r in unchecked],
    }


async def fix_run(run_id: str, bad_indexes: list[str]) -> list[dict]:
    """对缺陷格：补取景声明 → PUT panels → POST regenerate（走管线全部既有校验）。"""
    import httpx

    panels = _panels_of(run_id)
    by = {str(p.get("index")): p for p in panels}
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=300) as client:
        for idx in bad_indexes:
            p = by.get(idx)
            if not p:
                continue
            new_prompt = patch_framing_prompt(_prompt_of(p))
            r1 = await client.put(
                f"{BACKEND}/api/runs/{run_id}/panels",
                json={"panels": [{"index": int(idx), "prompt": new_prompt,
                                  "shot_size": p.get("shot_size")}]},
            )
            r2 = await client.post(
                f"{BACKEND}/api/runs/{run_id}/panels/{idx}/regenerate", json={}
            )
            ok = r1.status_code < 400 and r2.status_code < 400
            detail = {}
            try:
                detail = r2.json()
            except Exception:  # noqa: BLE001
                pass
            out.append({
                "index": idx,
                "ok": ok,
                "new_prompt_tail": new_prompt[-120:],
                "resp": str(detail)[:200],
            })
    return out


async def main_async() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_fix = "--fix" in sys.argv
    targets = args if args else _runs_with_panels()
    if not do_fix and "--all" not in sys.argv and args:
        targets = args

    all_bad: dict[str, list[str]] = {}
    for run_id in targets:
        report = await audit_run(run_id)
        # 留痕：写进工作区，人工复核与后续统计都能用
        try:
            storage.write_json(run_id, "vision_audit.json", report)
        except Exception as exc:  # noqa: BLE001
            print(f"  （报告落盘失败：{exc}）")
        flag = "✗" if report["defects"] else ("✓" if report["checked"] else "-")
        print(f"{flag} {run_id[:8]}  检查 {report['checked']}/{report['total']} 格，"
              f"缺陷 {report['defects']} 格 {report['bad'] or ''}"
              f"{'（未检查: ' + ','.join(report['unchecked']) + '）' if report['unchecked'] else ''}")
        for r in report["results"]:
            if r.get("checked") and not r.get("ok"):
                print(f"    格 {r['index']}: {r.get('issue')}")
        if report["defects"]:
            all_bad[run_id] = report["bad"]

    if do_fix and all_bad:
        print("\n--fix：对缺陷格补取景声明并重画（走管线全部既有校验与视觉后检）…")
        for run_id, bad in all_bad.items():
            for r in await fix_run(run_id, bad):
                print(f"  格 {r['index']}: {'OK' if r['ok'] else 'FAIL'}  {r['resp'][:80]}")
        print("重画完成。建议对同一 run 再跑一次本脚本复核。")
    elif all_bad:
        print("\n提示：加 --fix 可自动补取景声明并重画缺陷格。")


if __name__ == "__main__":
    asyncio.run(main_async())
