#!/usr/bin/env python3
"""一致性抽检（W4，只告警不重画）：这一格的人，还是同一个人吗？

主判据：视觉模型**双图判定**（定妆图 + 该格画面 → 1~5 分 + 依据）。
辅助信号（`--geometric`，默认关）：人脸框裁切后的直方图 + SSIM —— 只写进报告
供参考，**不参与告警**。原因见 media/vision_check.py：框位置不可靠（同一张图
有时给归一化、有时给像素，偏到背景是常态），框一歪 SSIM 就崩到 0.00，
用它告警的误报率远超路线图允许的 30%。

为什么只告警不自动重画：
    度量再准也无法替人判断"这张脸好不好看、这个角度能不能用"。抽检的职责是
    **把该看的格子挑出来**并给出依据，重画由人点。

用法：
    python3 scripts/audit_consistency.py <run_id>
    python3 scripts/audit_consistency.py --all
    python3 scripts/audit_consistency.py <run_id> --threshold 0.5 --geometric

缓存：判定结果写工作区 vision_compare.json —— 每格只问模型一次，
重复跑脚本不重复计费（换定妆图/改图后删该文件即可）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.media.vision_check import compare_character  # noqa: E402
from app.store import storage  # noqa: E402

# 告警阈值：score = (same-1)/4，即 same ≤ 3 分（"可能是另一个人"及以下）才告警。
# 定得低是有意的：5=同一人、4=基本一致（可以是角度/光线差异），4 分不该打扰人。
DEFAULT_THRESHOLD = 0.5
CONCURRENCY = 3


def _ws(run_id: str) -> Path:
    return Path(storage.root) / run_id


def _load_cache(run_id: str) -> dict:
    p = _ws(run_id) / "vision_compare.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_cache(run_id: str, cache: dict) -> None:
    try:
        (_ws(run_id) / "vision_compare.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=1)
        )
    except Exception:  # noqa: BLE001
        pass


def _character_arts(run_id: str) -> list[str]:
    ws = _ws(run_id)
    chars = ws / "characters"
    if not chars.exists():
        return []
    arts = sorted(p for p in chars.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    return [str(p.relative_to(ws)) for p in arts]


def _panels(run_id: str) -> list[dict]:
    ws = _ws(run_id)
    doc = {}
    try:
        doc = json.loads((ws / "panels.json").read_text())
    except Exception:  # noqa: BLE001
        doc = {}
    prompt_by = {}
    for p in (doc.get("panels") if isinstance(doc, dict) else doc) or []:
        if isinstance(p, dict):
            prompt_by[str(p.get("index"))] = str(p.get("shot_size") or "")
    out = []
    for f in sorted((ws / "panels").glob("panel-*.png")):
        idx = f.stem.split("-")[-1]
        key = str(int(idx)) if idx.isdigit() else idx
        out.append({"index": key, "path": str(f.relative_to(ws)), "shot": prompt_by.get(key, "")})
    return out


def _geometric(run_id: str, ref_rel: str, panel_rel: str) -> dict | None:
    """辅助信号：人脸框 + 直方图/SSIM。框不可靠，只作参考，绝不参与告警。"""
    try:
        from app.media.face_similarity import compare_faces, crop_box, load_image

        faces = json.loads((_ws(run_id) / "vision_faces.json").read_text())
        rb = (faces.get(ref_rel) or {}).get("box")
        pb = (faces.get(panel_rel) or {}).get("box")
        if not rb or not pb:
            return None
        a = crop_box(load_image(_ws(run_id) / ref_rel), rb)
        b = crop_box(load_image(_ws(run_id) / panel_rel), pb)
        if a is None or b is None:
            return None
        return compare_faces(a, b)
    except Exception:  # noqa: BLE001
        return None


async def audit_run(run_id: str, threshold: float = DEFAULT_THRESHOLD, geometric: bool = False) -> dict:
    arts, panels = _character_arts(run_id), _panels(run_id)
    if not arts or not panels:
        return {"run_id": run_id, "skipped": "缺定妆图或分镜图", "rows": []}

    ref_rel = arts[0]  # 多角色任务只比主角（第一张定妆图），报告里注明
    cache = _load_cache(run_id)
    sem = asyncio.Semaphore(CONCURRENCY)
    rows = []

    async def one(p: dict) -> dict:
        rel = p["path"]
        hit = cache.get(rel)
        if not isinstance(hit, dict) or "checked" not in hit:
            async with sem:
                r = await compare_character(_ws(run_id) / ref_rel, _ws(run_id) / rel)
            hit = {k: r.get(k) for k in ("checked", "found", "same", "score", "reason")}
            cache[rel] = hit
        row = {"index": p["index"], "path": rel, "shot": p.get("shot"), **hit}
        if not hit.get("checked"):
            row["status"] = "unchecked"
        elif not hit.get("found"):
            row["status"] = "skip"          # 空镜/无人：不比对，不告警
        elif (hit.get("score") or 0) < threshold:
            row["status"] = "warn"
        else:
            row["status"] = "ok"
        if geometric and row["status"] in ("ok", "warn"):
            g = await asyncio.to_thread(_geometric, run_id, ref_rel, rel)
            if g:
                row["geometric"] = g
        return row

    rows = await asyncio.gather(*(one(p) for p in panels))
    rows = sorted(rows, key=lambda r: int(r["index"]) if str(r["index"]).isdigit() else 999)
    _save_cache(run_id, cache)

    warn = [r["index"] for r in rows if r["status"] == "warn"]
    report = {
        "run_id": run_id,
        "reference": ref_rel,
        "threshold": threshold,
        "compared": sum(1 for r in rows if r["status"] in ("ok", "warn")),
        "warn": warn,
        "rows": rows,
    }
    try:
        storage.write_json(run_id, "consistency_audit.json", report)
    except Exception:  # noqa: BLE001
        pass
    return report


async def main_async() -> None:
    argv = sys.argv[1:]
    threshold = DEFAULT_THRESHOLD
    if "--threshold" in argv:
        i = argv.index("--threshold")
        if i + 1 < len(argv):
            try:
                threshold = float(argv[i + 1])
            except ValueError:
                pass
    geometric = "--geometric" in argv
    skip = {"--threshold", "--all", "--geometric", str(threshold)}
    targets = [a for a in argv if not a.startswith("--") and a not in skip]
    if not targets or "--all" in argv:
        root = Path(storage.root)
        targets = [
            d.name for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            if d.is_dir() and (d / "panels").exists()
        ]

    for run_id in targets:
        rep = await audit_run(run_id, threshold, geometric)
        if rep.get("skipped"):
            print(f"- {run_id[:8]}  跳过：{rep['skipped']}")
            continue
        flag = "!" if rep["warn"] else "✓"
        print(f"{flag} {run_id[:8]}  参照 {rep['reference']}  比对 {rep['compared']} 格  "
              f"低于 {threshold} 的格：{rep['warn'] or '无'}")
        for r in rep["rows"]:
            if r["status"] == "warn":
                g = f"（几何 辅助 {r['geometric']['score']:.2f}）" if r.get("geometric") else ""
                print(f"    格 {r['index']:>2}: {r.get('same')} 分 {g} → {r.get('reason')}")
            elif r["status"] == "skip":
                print(f"    格 {r['index']:>2}: 无人/空镜 —— {r.get('reason')}")
            elif r["status"] == "unchecked":
                print(f"    格 {r['index']:>2}: 未判定 —— {r.get('reason')}")
    print("\n说明：只告警不重画。判定依据随报告落盘 consistency_audit.json。")


if __name__ == "__main__":
    asyncio.run(main_async())
