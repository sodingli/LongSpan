"""骨架生成与回填的自测（正样本必过 + 每条规则配反样本）。

为什么要单独测：回填是**机器改模型产物**，写错一个字段就会让整集分镜错位，
而且错得很安静（结构看着对、内容对错了格）。所以每条规则都要有一个反样本。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.harness import skeleton  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")


def no_adjacent_same(panels: list[dict]) -> bool:
    return all(
        panels[i]["shot_size"] != panels[i - 1]["shot_size"] for i in range(1, len(panels))
    )


# ---------- 1. 格数来自 spec ----------
for n in (12, 16):
    skel = skeleton.build({"grid_count": n})
    check(f"grid_count={n} → 生成 {n} 格", skel["panel_count"] == n, str(skel["panel_count"]))
    check(f"{n} 格 · 相邻景别不重复", no_adjacent_same(skel["panels"]))
    check(f"{n} 格 · 主场景占比 ≥0.6", skel["primary_scene_ratio"] >= 0.6,
          str(skel["primary_scene_ratio"]))
    check(f"{n} 格 · 首格 wide", skel["panels"][0]["shot_size"] == "wide")
    check(f"{n} 格 · 末格 medium", skel["panels"][-1]["shot_size"] == "medium")

# ---------- 2. A-B-A：S2 / S3 都必须夹在主场景之间 ----------
skel16 = skeleton.build({"grid_count": 16})
seq = [p["scene_id"] for p in skel16["panels"]]
check("16 格 · 场景序列含 S2 与 S3", "S2" in seq and "S3" in seq, str(seq))
check("16 格 · S2 前后都是 S1（A-B-A）",
      seq[seq.index("S2") - 1] == "S1" and seq[seq.index("S2") + 2] == "S1", str(seq))
check("16 格 · 无三连换场",
      all(not (seq[i] != seq[i - 1] and seq[i + 1] != seq[i] and seq[i + 1] != seq[i - 1])
          for i in range(1, len(seq) - 1)), str(seq))

# ---------- 3. spec 缺失/形状怪：退默认而不是崩 ----------
check("无 spec → 默认 12 格", skeleton.build({})["panel_count"] == 12)
check("panel_count 字符串也能读", skeleton.build({"panel_count": "14"})["panel_count"] == 14)
check("只有 rhythm_table 也能推格数",
      skeleton.build({"rhythm_table": [{"panels": [1, 2, 3]}, {"panels": [4, 5, 6, 7, 8, 9, 10, 11, 12]}]})["panel_count"] == 12)
check("越界格数（999）退默认", skeleton.build({"grid_count": 999})["panel_count"] == 12)

# ---------- 3.5 真实 spec.json 形状（2026-10-23 实测，反样本） ----------
# 模型自由生成的 spec 里，rhythm_table 每段是 `panels: <格数 int>` + `panel_indices: <格号列表>`
# 而非我们早先假设的 `panels: [格号列表]`。`panels` 是 int 时若直接 for 迭代会抛
# `'int' object is not iterable`（run 分镜阶段启动即失败）。下面三条必须全过：
check("panels 是 int 不崩、退默认 12 格",
      skeleton.build({"rhythm_table": [{"panels": 3}, {"panels": 4}]})["panel_count"] == 12)
check("panel_indices 推格数（真实字段名）",
      skeleton.build({"rhythm_table": [
          {"panel_indices": [1, 2, 3]},
          {"panel_indices": [4, 5, 6, 7, 8, 9, 10, 11, 12]},
      ]})["panel_count"] == 12)
check("phase/narrative_function 正确映射（真实字段名）",
      all("抛钩子" in p["function"] for p in skeleton.build({
          "grid_count": 12,
          "rhythm_table": [{"phase": "开场", "narrative_function": "抛钩子", "panels": 3,
                            "panel_indices": [1, 2, 3]}],
      })["panels"][:3]))

# ---------- 4. rhythm_table 的剧情功能要带进骨架 ----------
skel_fn = skeleton.build({
    "grid_count": 12,
    "rhythm_table": [{"segment": "开场", "function": "抛钩子", "panels": [1, 2, 3]}],
})
check("剧情功能映射到前 3 格",
      all("抛钩子" in p["function"] for p in skel_fn["panels"][:3]),
      str([p["function"] for p in skel_fn["panels"][:3]]))
check("表格渲染含格号与 scene_id",
      "| 1 | S1 |" in skeleton.render_table(skel_fn))

# ---------- 5. 回填：缺列补上 ----------
skel = skeleton.build({"grid_count": 12})
payload = {"panels": [{"index": i, "action": f"动作{i}", "prompt": "x"} for i in range(1, 13)]}
patched, devs, bad = skeleton.apply(json.loads(json.dumps(payload)), skel)
check("缺 shot_size 全部补上（scene_id 不强制）",
      all(p.get("shot_size") for p in patched["panels"]), str(bad))
check("补字段逐条留痕（12 条：12 格 × 1 列 shot_size）", len(devs) == 12, str(len(devs)))
check("无不可修复项", bad == [], str(bad))

# ---------- 6. 回填：错 shot_size 改回，错 scene_id 不碰（交还模型） ----------
wrong = {"panels": [{"index": 1, "scene_id": "S9", "shot_size": "close", "action": "a"}]}
wrong["panels"] += [{"index": i, "scene_id": "S1", "shot_size": "medium", "action": "a"}
                    for i in range(2, 13)]
patched2, devs2, _ = skeleton.apply(json.loads(json.dumps(wrong)), skel)
check("错 shot_size 被改回骨架值", patched2["panels"][0]["shot_size"] == skel["panels"][0]["shot_size"])
check("错 scene_id 不被回填（场景交还模型）", patched2["panels"][0]["scene_id"] == "S9")
check("模型原值被保留在 _declared_shot_size 字段",
      patched2["panels"][0].get("_declared_shot_size") == "close")
check("偏离被记录", any("shot_size=close" in d for d in devs2), str(devs2[:2]))
check("scene_id 偏离不产生回填留痕", not any("scene_id" in d for d in devs2), str(devs2[:2]))

# ---------- 7. 幂等：回填过的再跑一遍不该有偏离 ----------
patched3, devs3, bad3 = skeleton.apply(json.loads(json.dumps(patched2)), skel)
check("回填幂等（再跑无偏离）", devs3 == [] and bad3 == [], f"{devs3[:2]} / {bad3[:2]}")

# ---------- 8. 超纲格与缺格：不丢内容、不编剧情 ----------
extra = {"panels": [{"index": i, "action": "a"} for i in range(1, 15)]}  # 14 格 vs 骨架 12
_, devs4, bad4 = skeleton.apply(json.loads(json.dumps(extra)), skel)
check("超出骨架的格号被上报", any("超出骨架" in b for b in bad4), str(bad4))
short = {"panels": [{"index": i, "action": "a"} for i in range(1, 10)]}   # 缺 10-12
_, devs5, bad5 = skeleton.apply(json.loads(json.dumps(short)), skel)
check("缺格被上报且不补内容", sum("缺少第" in b for b in bad5) == 3, str(bad5))

# ---------- 9. 裸数组 / 其他键名也能回填 shot_size ----------
bare = [{"index": i, "action": "a"} for i in range(1, 13)]
patched6, devs6, _ = skeleton.apply(json.loads(json.dumps(bare)), skel)
check("裸数组形态可回填 shot_size", isinstance(patched6, list) and patched6[0].get("shot_size") == "wide")
alt = {"items": [{"index": i, "action": "a"} for i in range(1, 13)]}
patched7, _, _ = skeleton.apply(json.loads(json.dumps(alt)), skel)
check("items 键名可回填 shot_size", patched7["items"][0].get("shot_size") == "wide")

# ---------- 10. 不可识别形状：如实报，不假装修好了 ----------
_, _, bad8 = skeleton.apply({"foo": "bar"}, skel)
check("不可识别形状被上报", bool(bad8), str(bad8))

print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
