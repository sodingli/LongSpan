"""分镜骨架：由系统**生成并回填**，不靠模型照着提示词自觉。

为什么要有这个模块（2026-10-23 实测）：
- 提示词里给一张固定的 12 格表，模型仍会自己改场景序列（实测 run fa1bcce9 第 1 轮
  格 2→4 写成 S1→S2→S3，直接违反 A-B-A），也会把 scene_id / shot_size 整列漏掉
  （run 17e5d8e5 四轮全格缺 action/scene_id）。四集连测分镜一次通过率 0/4。
- 结论不是"再写清楚点"，而是：**结构是能机器保证的，就不该写成提示词约定**
  （MEMORY 铁律 + ROADMAP §六 方向 A）。

两条落点（缺一不可，和 directing 同一模子）：
1. `build()` + `render_table()`：按 spec 的真实格数生成骨架表，渲染进提示词 ——
   模型知道"每格该是什么景别、哪个场景"，不用自己发明结构；
2. `apply()`：模型交卷后**机器回填**结构字段（格号/scene_id/shot_size），
   偏离逐条记录成 decision，不静默 —— 模型写错结构不再触发整阶段重做，
   因为结构已经不由它决定了。

刻意**不**回填的：`action` / `prompt` / `subtitle` / `speaker` / `motion` / 时长。
前者是创作内容；`motion` 是演出决策（也是内容），只校验是否在枚举内。
"""

from __future__ import annotations

import json
from typing import Any

# 景别循环：相邻格不重复，且建立/推进/细节三种功能都有落点
_SHOT_CYCLE = ("wide", "medium", "close", "medium", "extreme-close")

_SHOT_LABEL = {
    "wide": "全景（建立空间）",
    "medium": "中景（人物与动作）",
    "close": "近景（表情/细节）",
    "extreme-close": "大特写（道具/局部，须写取景边界）",
}


def _seg_indices(seg: dict) -> list[int]:
    """从 rhythm_table 的一个分段里取格号列表，健壮兼容多种字段名与类型。

    实测 spec.json 是模型自由生成的，同一语义出现过两种写法：
    - `panel_indices: [1,2,3]`（格号列表，正确语义）
    - `panels: 3`（该分段的格数，是 int —— 迭代它就会报 'int' object is not iterable）
    也可能用 `panel_nums` / `panel_numbers` / `indices`。这里统一收敛：
    只有列表才迭代；int 直接无视（格数不能当格号用）。
    """
    for key in ("panel_indices", "panel_nums", "panel_numbers", "indices", "panels"):
        raw = seg.get(key)
        if isinstance(raw, list):
            out: list[int] = []
            for no in raw:
                try:
                    out.append(int(no))
                except (TypeError, ValueError):
                    continue
            return out
    return []


def _panel_count(spec: Any) -> int:
    """格数只认 spec 的 field，读不到才退回 12。

    容忍三种形状：`grid_count: 12` / `panel_count: 12` / `rhythm_table` 里
    各段 `panel_indices` 的上界。读不到不猜太大 —— 格数越多成本越高。
    """
    if not isinstance(spec, dict):
        return 12
    for key in ("grid_count", "panel_count", "panels"):
        raw = spec.get(key)
        if isinstance(raw, int) and 6 <= raw <= 40:
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            val = int(raw.strip())
            if 6 <= val <= 40:
                return val
    best = 0
    for seg in spec.get("rhythm_table") or []:
        if isinstance(seg, dict):
            for no in _seg_indices(seg):
                best = max(best, no)
    return best if 6 <= best <= 40 else 12


def _functions(spec: Any, total: int) -> dict[int, str]:
    """把 spec 的 rhythm_table 翻成「第几格负责什么」，让骨架表带剧情功能。

    这张表给的是**功能**（抛钩子/建立冲突/反转/收尾），不是画面内容 ——
    内容仍归模型，避免机器写死创作。
    """
    out: dict[int, str] = {}
    table = spec.get("rhythm_table") if isinstance(spec, dict) else None
    for seg in table or []:
        if not isinstance(seg, dict):
            continue
        # 分段名与功能描述：兼容 phase/narrative_function 与 segment/function 两套命名
        label = (str(seg.get("phase") or "") or str(seg.get("segment") or "")).strip()
        func = (str(seg.get("narrative_function") or "")
                or str(seg.get("function") or "")).strip()
        text = "—".join([x for x in (label, func) if x])[:60]
        for no in _seg_indices(seg):
            if 1 <= no <= total and text:
                out[no] = text
    return out


def build(spec: Any) -> dict:
    """生成骨架：格数 / 每格场景（建议值）/ 每格景别（回填值）/ 每格剧情功能。

    回填与建议的分界（2026-10-23 实测校准）：
    - **景别 shot_size** 是纯结构（相邻不重复、建立/推进/细节有落点），机器回填；
    - **格数 panel_count** 是纯结构，机器回填；
    - **场景 scene_id** 是创作内容（故事发生在哪几个空间、有几个），**只作建议**，
      交还模型决定，由 scene_reference / shot_budget.aba_rule 校验器兜底。
      实测：骨架硬编码 3 场景与模型只写 2 场景冲突，把格强塞不存在的 S3 导致阶段失败。
    """
    total = _panel_count(spec)
    funcs = _functions(spec, total)

    # 场景分配：主场景 S1 占大多数，S2/S3 各 2 格，且都夹在 S1 中间（A-B-A、A-C-A）。
    # 格数少时（<10）只留一个插曲场景，否则主场景占比压不住。
    pair = 2 if total >= 8 else 1
    s2_start = max(2, round(total * 0.34))
    s3_start = max(s2_start + pair + 2, round(total * 0.66))
    if s3_start + pair - 1 > total - 1:  # 别把收尾格占掉
        s3_start = total - 1 - pair

    scenes: list[str] = []
    for i in range(1, total + 1):
        if s2_start <= i < s2_start + pair:
            scenes.append("S2")
        elif total >= 10 and s3_start <= i < s3_start + pair:
            scenes.append("S3")
        else:
            scenes.append("S1")

    panels: list[dict] = []
    prev_shot = ""
    for i, sid in enumerate(scenes, 1):
        if i == 1:
            shot = "wide"
        elif i == total:
            shot = "medium"
        elif sid in ("S2", "S3"):
            shot = "medium" if i == s2_start or i == s3_start else "close"
        else:
            # 主场景里按循环取，跳过与上一格相同的景别
            idx = i % len(_SHOT_CYCLE)
            shot = _SHOT_CYCLE[idx]
            while shot == prev_shot:
                idx = (idx + 1) % len(_SHOT_CYCLE)
                shot = _SHOT_CYCLE[idx]
        prev_shot = shot
        panels.append({
            "index": i,
            "scene_id": sid,
            "shot_size": shot,
            "function": funcs.get(i) or ("建立空间：主场景全貌" if i == 1 else
                                        "结尾钩子：定格在情绪/道具上" if i == total else ""),
        })

    # 收尾修正：换场交接处（S2/S3 前后）仍可能撞出相邻同景别 —— 骨架自己违反自己的规则
    # 就会让"相邻格不同景别"这条校验变成冤枉模型。这里按循环换到下一个不冲突的景别。
    # 末格（结尾钩子）优先保留：冲突时改它的前一格，别把收尾镜改成大景别。
    for i in range(1, len(panels)):
        if panels[i]["shot_size"] != panels[i - 1]["shot_size"]:
            continue
        target = i - 1 if i == len(panels) - 1 else i
        neighbors = {panels[j]["shot_size"] for j in (target - 1, target + 1)
                     if 0 <= j < len(panels)}
        for cand in _SHOT_CYCLE:
            if cand not in neighbors:
                panels[target]["shot_size"] = cand
                break

    primary = sum(1 for p in panels if p["scene_id"] == "S1")
    return {
        "panel_count": total,
        "panels": panels,
        "primary_scene_ratio": round(primary / total, 3),
    }


def render_table(skel: dict) -> str:
    """把骨架渲染成 Markdown 表（表格比 JSON 更容易被模型逐行照抄）。"""
    lines = [
        f"共 **{skel['panel_count']} 格**（格数由 spec 决定，不是 12 就照抄 12）。"
        "下表由系统生成，**格号 / shot_size 两列必须原样照抄**；"
        "scene_id 是你决定的内容（场景有几个、叫什么，写进 scenes.json），"
        "只需保证换场不三连（A-B-A）、场景总数 ≤3。"
        "只填 action / prompt / subtitle / speaker / motion / duration_seconds：",
        "",
        "| 格 | scene_id（建议） | shot_size（必照抄） | 剧情功能（按此写内容，不要改结构） |",
        "| --- | --- | --- | --- |",
    ]
    for p in skel["panels"]:
        lines.append(
            f"| {p['index']} | {p['scene_id']} | {p['shot_size']} | "
            f"{p['function'] or _SHOT_LABEL.get(p['shot_size'], '')} |"
        )
    lines.append("")
    lines.append(
        "相邻格景别已排开，不要自行升/降景别；scene_id 是建议值，"
        "你可以按剧情改成别的场景号，但要写进 scenes.json，且换场遵守 A-B-A（不三连）。"
    )
    return "\n".join(lines)


def _items(payload: Any) -> tuple[list[dict] | None, str]:
    """宽容取 panels 数组：{panels|items|data|list:[...]} 或裸数组。"""
    if isinstance(payload, dict):
        for key in ("panels", "items", "data", "list", "records"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)], key
        return None, ""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], ""
    return None, ""


def apply(payload: Any, skel: dict) -> tuple[Any, list[str], list[str]]:
    """把骨架回填进 panels 清单。

    返回 (新 payload, 偏离说明, 无法修复的说明)。偏离不静默：调用方必须写进事件流。
    格数与骨架不一致时**不丢内容**：多出来的格保留（原样追加，标记超纲），
    少的格不补 —— 补一格等于机器编剧情，宁可让内容校验去报"格数不符"。
    """
    items, key = _items(payload)
    if items is None:
        return payload, [], ["panels.json 不是可识别的清单形状，骨架无从回填"]

    deviations: list[str] = []
    unfixable: list[str] = []

    by_index: dict[int, dict] = {}
    for pos, item in enumerate(items, 1):
        raw = item.get("index", item.get("panel", item.get("no", pos)))
        try:
            no = int(raw)
        except (TypeError, ValueError):
            no = pos
            unfixable.append(f"第 {pos} 项的 index 不是数字（{raw!r}），按位置回填")
        if no in by_index:
            unfixable.append(f"格号 {no} 重复，只回填第一次出现的项")
            continue
        by_index[no] = item

    extra = sorted(n for n in by_index if n > skel["panel_count"])
    if extra:
        unfixable.append(
            f"清单里有超出骨架的格号 {extra}（骨架共 {skel['panel_count']} 格），已保留但未回填结构"
        )

    for p in skel["panels"]:
        no = p["index"]
        item = by_index.get(no)
        if item is None:
            unfixable.append(f"缺少第 {no} 格（骨架要求 {skel['panel_count']} 格）")
            continue
        if item.get("index") in (None, ""):
            item["index"] = no
        # 只回填 shot_size（景别，纯结构）；scene_id 是场景创作，交还模型决定，
        # 其正确性由 scene_reference / shot_budget.aba_rule 校验器强制。
        # 2026-10-23 实测：骨架硬编码 S1/S2/S3 三场景，与模型实际写的 2 个场景
        # （scenes.json 只有 S1/S2）冲突，把格 8/9 强塞 S3 触发"引用不存在场景"失败。
        want = p["shot_size"]
        have = item.get("shot_size")
        if have in (None, ""):
            item["shot_size"] = want
            deviations.append(f"格 {no} 缺 shot_size，已按骨架回填 {want}")
        elif str(have).strip().lower() != str(want).lower():
            item["_declared_shot_size"] = have  # 保留模型原值，便于复盘
            item["shot_size"] = want
            deviations.append(f"格 {no} 的 shot_size={have} 偏离骨架，已改回 {want}")

    if key:
        payload[key] = items
    else:
        payload = items
    return payload, deviations, unfixable


def load_spec(workspace_paths: list[str]) -> dict:
    """按顺序找到第一份可解析的 spec.json（沙箱里也可能叫 production_spec.json）。"""
    for path in workspace_paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict):
                return obj
        except (OSError, json.JSONDecodeError):
            continue
    return {}
