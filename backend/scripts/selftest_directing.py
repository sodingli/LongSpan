#!/usr/bin/env python3
"""演出约束校验器的自测 —— 正样本必须通过，每个反样本必须**报对那一条**。

为什么要这个文件：
    一个只会说"不通过"的校验器，和没有校验器一样不可用；
    一个永远说"通过"的校验器更糟 —— 它会让整条产线误以为自己守住了规范。
    所以这里两侧都测：合规分镜过，逐条破坏只报对应那一类错。

零依赖、零成本、不连网：python scripts/selftest_directing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.harness.consistency import (  # noqa: E402
    extract_anchors,
    is_character_anchor,
    is_empty_shot,
    validate_framing,
    validate_scene_references,
    validate_shot_budget,
)
from app.harness.tools import _apply_empty_shot_guard  # noqa: E402

BUDGET = {
    "max_distinct_scenes": 3,
    "max_scene_cuts": 6,
    "primary_scene_ratio": 0.5,
    "aba_rule": True,
    "forbid_adjacent_same_shot": True,
    "one_action_per_panel": True,
    "require_motion": True,
}

# 合规样本：3 个场景、A-B-A 换场、景别不撞、每格一个动作。
SCENE_SEQ = ["S1", "S1", "S2", "S1", "S1", "S1", "S3", "S1", "S1", "S2", "S1", "S1"]
SHOT_SEQ = [
    "wide", "medium", "close", "medium", "wide", "extreme-close",
    "wide", "medium", "close", "wide", "medium", "close",
]
MOTIONS = ["static", "zoom-in", "pan-left", "zoom-out", "static", "zoom-in",
           "pan-right", "zoom-in", "static", "pan-left", "zoom-out", "static"]


def good_panels() -> dict:
    panels = []
    for i in range(12):
        panels.append({
            "index": i + 1,
            "scene_id": SCENE_SEQ[i],
            "shot_size": SHOT_SEQ[i],
            "action": f"机械师完成第 {i + 1} 个动作",
            "motion": MOTIONS[i],
            "prompt": "CHENMO_ANCHOR: anchor text ...",
        })
    return {"panels": panels}


def good_scenes() -> dict:
    return {"scenes": [
        {"id": "S1", "name": "废料场", "anchor": "vast scrap-metal wasteland, overcast sky"},
        {"id": "S2", "name": "维修间", "anchor": "cluttered repair shed, single hanging bulb"},
        {"id": "S3", "name": "门口", "anchor": "rusted gate under a broken streetlight"},
    ]}


def check(name: str, ok: bool, reasons: list[str], expect_fail_with: str | None) -> bool:
    """expect_fail_with=None 表示期望通过；否则期望失败且原因里含该关键词。"""
    hit = any(expect_fail_with in r for r in reasons) if expect_fail_with else False
    passed = ok if expect_fail_with is None else (not ok and hit)
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {name}")
    if not passed:
        print("       ok=", ok)
        for r in reasons:
            print("       -", r)
    return passed


def main() -> int:
    results: list[bool] = []

    # ① 正样本
    ok, reasons = validate_shot_budget(good_panels(), BUDGET)
    results.append(check("合规分镜应通过", ok, reasons, None))
    ok2, r2 = validate_scene_references(good_panels(), good_scenes())
    results.append(check("合规场景引用应通过", ok2, r2, None))

    # ② 场景超预算
    p = good_panels()
    p["panels"][3]["scene_id"] = "S9"
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("第 4 个场景应触发超预算", ok, reasons, "超出预算"))

    # ③ A→B→C 三连换场
    p = good_panels()
    for i, sid in enumerate(["S1", "S2", "S3"], start=0):
        p["panels"][i]["scene_id"] = sid
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("三连换场应被拦", ok, reasons, "必须改成 A-B-A"))

    # ④ 相邻同景别
    p = good_panels()
    p["panels"][1]["shot_size"] = p["panels"][0]["shot_size"]
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("相邻同景别应被拦", ok, reasons, "景别"))

    # ⑤ 单格多动作
    p = good_panels()
    p["panels"][4]["action"] = "他捡起管子，然后抬头望向远处"
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("单格多动作应被拦", ok, reasons, "多个动作"))

    # ⑥ 缺 scene_id
    p = good_panels()
    del p["panels"][0]["scene_id"]
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("缺 scene_id 应被拦", ok, reasons, "缺少 scene_id"))

    # ⑦ 缺 motion
    p = good_panels()
    del p["panels"][2]["motion"]
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("缺 motion 应被拦", ok, reasons, "缺少 motion"))

    # ⑧ 未知场景号（引用完整性）
    p = good_panels()
    p["panels"][0]["scene_id"] = "S8"
    ok, reasons = validate_scene_references(p, good_scenes())
    results.append(check("未知场景号应被拦", ok, reasons, "不存在的场景号"))

    # ⑨ 没有场景卡
    ok, reasons = validate_scene_references(good_panels(), None)
    results.append(check("缺 scenes.json 应被拦", ok, reasons, "未产出或其中没有 scenes"))

    # ⑩ 裸数组形态也必须能校验（宽容解析）
    ok, reasons = validate_shot_budget(good_panels()["panels"], BUDGET)
    results.append(check("裸数组形态应同样通过", ok, reasons, None))

    # ⑪ 主场景判定必须按"出现次数"，不能按"第一次出现的顺序"
    #    回归用例：首个出现的场景是 S3，但主场景其实是 S1（占 8 格）。
    p = good_panels()
    seq = ["S3", "S1", "S1", "S1", "S2", "S1", "S1", "S1", "S2", "S1", "S1", "S1"]
    for i, sid in enumerate(seq):
        p["panels"][i]["scene_id"] = sid
    ok, reasons = validate_shot_budget(p, BUDGET)
    results.append(check("主场景按出现次数判定（S1 占 9/12 应通过）", ok, reasons, None))

    # ⑫ 没有任何场景过半 → 必须报出来，且报的是真实占比
    p = good_panels()
    seq = ["S1", "S2", "S3", "S1", "S2", "S3", "S1", "S2", "S3", "S1", "S2", "S3"]
    for i, sid in enumerate(seq):
        p["panels"][i]["scene_id"] = sid
    ok, reasons = validate_shot_budget(p, {**BUDGET, "aba_rule": False, "max_scene_cuts": 12})
    results.append(check("无主场景应被拦且占比正确", ok, reasons, "只占 33%"))

    # ⑬ 景别别名匹配从长到短：extreme-close 不能被 'close' 子串抢先误归一
    p = good_panels()
    p["panels"][2]["shot_size"] = "close"           # 格 3
    p["panels"][3]["shot_size"] = "extreme-close"   # 格 4（大特写 ≠ 近景）
    ok, reasons = validate_shot_budget(p, BUDGET)
    bad = [r for r in reasons if "景别" in r]
    results.append(check("extreme-close 不被误归一成 close", ok or not bad, reasons, None))

    # ---- 锚点提取（2026-10-22：全局画风锚点混进角色表 → 无主定妆图事故）----

    # ⑭ 形态 C（裸代码块）里混着「全局画风锚点」：只有画风词、没有人物词，
    #    不得被编号成角色锚点（否则会生成无主定妆图 + 校验放水）。
    md_mixed = """# 角色设计

### 老渔民

#### 一致性锚点（冻结字符串）
```
Stylized 2D anime cel-shaded illustration. A 65-year-old Chinese fisherman, grey-white short hair, deep wrinkles, calm tired eyes, faded blue cloth shirt. NOT a photograph, NOT photorealistic.
```

### 全局画风锚点（供场景生图复用）
```
2D anime cel-shaded illustration, Chinese donghua style, clean ink lines, flat cel colors, anime screenshot frame, NOT a photograph, NOT photorealistic, NOT live-action, NOT 3D render.
```
"""
    anchors = extract_anchors(md_mixed)
    results.append(check(
        "裸代码块里的全局画风锚点不进角色表",
        set(anchors) == {"anchor_1"} and "fisherman" in anchors.get("anchor_1", ""),
        [f"提取结果: {list(anchors)}"],
        None,
    ))

    # ⑮ is_character_anchor 的边界：有具体人物特征 → 是角色；纯画风/场景 → 不是
    results.append(check(
        "is_character_anchor：含人物特征判为角色",
        is_character_anchor("A 65-year-old Chinese fisherman, grey-white short hair, calm tired eyes"),
        [], None,
    ))
    results.append(check(
        "is_character_anchor：纯画风描述判为非角色",
        not is_character_anchor("2D anime cel-shaded illustration, clean ink lines, flat cel colors, anime screenshot frame"),
        [], None,
    ))
    results.append(check(
        "is_character_anchor：只有服装没有人物也判非角色",
        not is_character_anchor("a faded blue cloth shirt on a wooden bench, flat cel colors"),
        [], None,
    ))

    # ---- 取景完整性（2026-10-22 实测事故：无头半身人）----

    # ⑯ 反样本：大特写拍手 + 衬衫内袋，不提头在哪 → 模型只能把头顶裁掉
    bad = {"panels": [{
        "index": 8, "shot_size": "extreme-close",
        "prompt": ("Extreme close-up of an old fisherman's weathered hand touching the inner "
                   "pocket of his faded blue cloth shirt, morning light"),
    }]}
    ok, reasons = validate_framing(bad)
    results.append(check("无头半身（手+衣着但不提头）应被拦", ok, reasons, "没有脑袋"))

    # ⑰ 修法一：补取景边界声明 → 通过
    fixed = {"panels": [{
        "index": 8, "shot_size": "extreme-close",
        "prompt": ("Extreme close-up of an old fisherman's weathered hand touching the inner "
                   "pocket of his shirt, only the hand and the fabric fill the frame, "
                   "no face, no head, no torso in frame"),
    }]}
    ok, reasons = validate_framing(fixed)
    results.append(check("补取景声明后应通过", ok, reasons, None))

    # ⑱ 修法二：升一档景别（medium 人物完整入画）→ 通过
    lifted = {"panels": [{
        "index": 8, "shot_size": "medium",
        "prompt": ("Medium shot of an old fisherman's weathered hand touching the inner pocket "
                   "of his shirt"),
    }]}
    ok, reasons = validate_framing(lifted)
    results.append(check("升景别到 medium 后应通过", ok, reasons, None))

    # ⑲ 有头部词 → 不报（画面里有脸，头不会被裁）
    with_head = {"panels": [{
        "index": 8, "shot_size": "close",
        "prompt": ("Close-up of an old fisherman's face and hand, his hand resting on the "
                   "pocket of his shirt"),
    }]}
    ok, reasons = validate_framing(with_head)
    results.append(check("含 face/hand 的近景不误伤", ok, reasons, None))

    # ⑳ 纯手部特写（画框里没有躯干衣着）→ 不报
    hand_only = {"panels": [{
        "index": 2, "shot_size": "extreme-close",
        "prompt": "Extreme close-up of a weathered hand holding a paper envelope, blurred dock behind",
    }]}
    ok, reasons = validate_framing(hand_only)
    results.append(check("纯手部特写不误伤", ok, reasons, None))

    # ㉑ 空镜（没有人物）→ 不报
    empty = {"panels": [{
        "index": 10, "shot_size": "wide",
        "prompt": "Wide shot of a small fishing boat sailing into the morning mist",
    }]}
    ok, reasons = validate_framing(empty)
    results.append(check("空镜不误伤", ok, reasons, None))

    # ---- 空镜判定（2026-10-22 真缺陷：空镜被画出陌生人，见 W4 一致性抽检） ----
    # 正样本：物件/风景描述本身就是空镜
    for p in (
        "Medium shot of a small fishing boat sailing into the mist at dawn, calm sea",
        "船舱内部，木箱和渔网堆在角落，晨光从舷窗照进来",
        "Empty street at night, neon reflections on wet asphalt, no people",
    ):
        results.append(check(f"空镜判定命中：{p[:24]}", is_empty_shot(p), [], None))
    # 反样本：中英文「有人物」的表述都必须判非空镜（误判会注入 no-people，灾难）
    for p in (
        "A 65-year-old fisherman with grey-white hair mending a net on the deck",
        "Close-up of a weathered hand slipping a letter into a shirt pocket",
        "他站在码头尽头，望着海面",
        "少女蹲在废墟里，手里握着一株幼苗",
    ):
        results.append(check(f"非空镜判定命中：{p[:24]}", not is_empty_shot(p), [], None))

    # 注入幂等：已声明的 prompt 不得重复追加
    injected, note = _apply_empty_shot_guard("A wooden boat drifting in the fog")
    again, note2 = _apply_empty_shot_guard(injected)
    results.append(check("空镜声明注入幂等", bool(note) and note2 is None and again == injected, [], None))
    _, note3 = _apply_empty_shot_guard("他站在码头尽头，望着海面")
    results.append(check("有人物的格不注入 no-people", note3 is None, [], None))

    print()
    total, passed = len(results), sum(results)
    print(f"{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
