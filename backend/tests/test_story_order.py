"""回归测试：成片格序必须等于剧本格序（2026-10-29 实测事故）。

事故（run 7e5a1588，60 秒 12 格）：
    剧本 script.md 的格序是「修车 1-8 → 点单 9 → 回店 10-12」，
    分镜 panels.json 却把「点单」搬到第 4-5 格、修车推到 6-9 格。
    分镜自己在 storyboard.md 里写明了原因：
      「格序按系统骨架重排，点单从第 9 格提前到第 4 格……要同时满足
        场景 ≤3 / 换场 A-B-A / S1 占比 ≥50%」
    —— 为了满足**场景预算**去搬动**剧情节拍**，成片顺序就和剧本对不上。

排查已确认渲染侧无辜：时间轴严格 1→12 单调、12 段视频首帧与各自分镜图
SSIM 0.85+ 一一对应。所以修复落在两处：
  ① 分镜骨架不再按格位预分配 scene_id（那正是"重排"的诱因）；
  ② 新增 story_order 机器校验 + 渲染前按格号稳定排序（防另一种同类故障：
     数组顺序 ≠ 格号）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.harness.consistency import parse_script_beats, validate_story_order
from app.harness.tools import _line_key, _order_panels, _prompt_fingerprint

SCRIPT = """# 热奶茶 · 剧本（第 1 集）

### 第 1 格 · 4.5 秒 · 冬夜，谁在骑
**画面**：路口。一辆蓝色自行车滑进光里。
**台词**：无。

### 第 2 格 · 4.5 秒 · 链条断了
**画面**：链条整个脱下来。
**台词**：林小满「啊——别啊。」

### 第 3 格 · 5.0 秒 · 一束灯从背后照过来
**画面**：电动车灯推过来。
**台词**：林小满「链子掉了……」／陈默「我看看。」

### 第 4 格 · 5.0 秒 · 他把手套摘了
**画面**：他蹲下，把右手棉手套扯下来。
**台词**：陈默「手电给我。」／林小满「你不是……在送餐吗？」

### 第 5 格 · 5.0 秒 · 还有三分钟
**画面**：她的视线越过他的肩。
**台词**：陈默「还有三分钟。」——停半拍——「来得及。」

### 第 6 格 · 5.5 秒 · 手
**画面**：大特写。冻红的手指。
**台词**：无。**这一格必须没有台词。**

### 第 7 格 · 5.5 秒 · 第二次响
**画面**：手机亮起来。
**台词**：无。只有提示音。

### 第 8 格 · 5.5 秒 · 好了
**画面**：他把链条挂上。
**台词**：林小满「我转你钱吧！」／陈默「不用。」

### 第 9 格 · 5.0 秒 · 两杯
**画面**：奶茶店柜台前。
**台词**：店员「一杯？」／林小满「两杯热的。另一杯……备注：谢谢刚才修车的师傅。」

### 第 10 格 · 5.0 秒 · 门铃响了
**画面**：她提着两杯奶茶回头。
**台词**：无。

### 第 11 格 · 5.0 秒 · 是你
**画面**：她抬头。
**台词**：林小满「……是你啊。」／陈默「您的奶茶。」

### 第 12 格 · 4.5 秒 · 热气
**画面**：两杯奶茶的热气。
**台词**：陈默「链子……别蹬太猛。」
"""


def _panels(subtitles: list[str]) -> dict:
    return {"panels": [
        {"index": i, "subtitle": s} for i, s in enumerate(subtitles, 1)
    ]}


class ParseScriptBeatsTest(unittest.TestCase):
    def test_extracts_all_beats_and_lines(self) -> None:
        beats = parse_script_beats(SCRIPT)
        self.assertEqual([no for no, _ in beats], list(range(1, 13)))
        self.assertEqual(beats[1][1], ["啊——别啊。"])
        self.assertEqual(beats[2][1], ["链子掉了……", "我看看。"])
        # 无台词的格必须是空列表，不能把"无"当成台词
        self.assertEqual(beats[5][1], [])
        self.assertEqual(beats[6][1], [])

    def test_no_beat_structure_returns_empty(self) -> None:
        self.assertEqual(parse_script_beats("# 随便一份没有逐格结构的文本"), [])


class StoryOrderTest(unittest.TestCase):
    def test_in_order_passes(self) -> None:
        panels = _panels([
            "", "啊——别啊。", "链子掉了…… / 我看看。", "手电给我。 / 你不是……在送餐吗？",
            "还有三分钟。 / 来得及。", "", "", "我转你钱吧！ / 不用。",
            "两杯热的。另一杯……备注：谢谢刚才修车的师傅。", "", "……是你啊。 / 您的奶茶。",
            "链子……别蹬太猛。",
        ])
        ok, reasons = validate_story_order(panels, SCRIPT)
        self.assertTrue(ok, reasons)

    def test_reordered_beats_fail(self) -> None:
        """复现实测事故：点单从第 9 格被搬到第 4 格。"""
        panels = _panels([
            "", "啊——别啊。", "链子掉了…… / 我看看。",
            "两杯热的。另一杯……备注：谢谢刚才修车的师傅。",   # ← 剧本第 9 格
            "两杯热的。另一杯……备注：谢谢刚才修车的师傅。",
            "我转你钱吧！ / 不用。",                            # ← 剧本第 8 格
            "", "", "", "", "……是你啊。 / 您的奶茶。", "链子……别蹬太猛。",
        ])
        ok, reasons = validate_story_order(panels, SCRIPT)
        self.assertFalse(ok)
        joined = "；".join(reasons)
        self.assertIn("第 4 格", joined)
        self.assertIn("剧本第 9 格", joined)
        self.assertIn("格序", joined)

    def test_monotonic_guard_when_counts_differ(self) -> None:
        """格数不等时不走一一对应，但顺序倒退仍要拦。"""
        panels = _panels(["", "啊——别啊。", "我转你钱吧！", "链子掉了…… / 我看看。"])
        ok, reasons = validate_story_order(panels, SCRIPT)
        self.assertFalse(ok)
        self.assertTrue(any("之后" in r or "重排" in r for r in reasons), reasons)

    def test_short_lines_do_not_false_positive(self) -> None:
        # "不用。" 只有 2 字，不参与匹配，不能因此误判
        panels = _panels(["", "", "", "", "", "", "", "不用。", "", "", "", ""])
        ok, _ = validate_story_order(panels, SCRIPT)
        self.assertTrue(ok)

    def test_no_script_structure_is_not_blocking(self) -> None:
        ok, reasons = validate_story_order(_panels(["随便一句台词"]), "没有逐格结构")
        self.assertTrue(ok)
        self.assertEqual(reasons, [])


class PanelOrderTest(unittest.TestCase):
    """数组顺序 ≠ 格号时，时间轴必须按格号走（防同类故障）。"""

    def test_sorts_by_index_and_leaves_trace(self) -> None:
        panels = [
            {"index": 3, "prompt": "c"},
            {"index": 1, "prompt": "a"},
            {"index": 2, "prompt": "b"},
        ]
        notes: list[str] = []
        ordered = _order_panels(panels, notes)
        self.assertEqual([p["index"] for p in ordered], [1, 2, 3])
        self.assertEqual(len(notes), 1)
        self.assertIn("按格号升序重排", notes[0])

    def test_already_ordered_is_silent(self) -> None:
        panels = [{"index": 1}, {"index": 2}]
        notes: list[str] = []
        self.assertEqual(_order_panels(panels, notes), panels)
        self.assertEqual(notes, [])

    def test_missing_index_falls_back_to_position(self) -> None:
        panels = [{"prompt": "a"}, {"index": 2, "prompt": "b"}]
        notes: list[str] = []
        ordered = _order_panels(panels, notes)
        self.assertEqual([p["prompt"] for p in ordered], ["a", "b"])
        self.assertEqual(notes, [])


class PromptFingerprintTest(unittest.TestCase):
    """幂等的前提是"输入没变"，不是"文件在"。

    实测（2026-10-30）：分镜重写后第 4~9 格提示词全变了，但 media/images.json
    只按格号记账 —— 旧实现看到"格 4 有图"就复用，重跑等于没跑，成片还是旧画面。
    """

    def test_deterministic(self) -> None:
        self.assertEqual(_prompt_fingerprint("a", "b"), _prompt_fingerprint("a", "b"))

    def test_sensitive_to_every_part(self) -> None:
        base = _prompt_fingerprint("prompt", "role")
        self.assertNotEqual(base, _prompt_fingerprint("prompt2", "role"))
        self.assertNotEqual(base, _prompt_fingerprint("prompt", "role2"))

    def test_part_boundary_is_not_ambiguous(self) -> None:
        # ("ab","c") 与 ("a","bc") 必须不同，否则拼接式指纹会撞车
        self.assertNotEqual(_prompt_fingerprint("ab", "c"), _prompt_fingerprint("a", "bc"))

    def test_empty_parts_are_safe(self) -> None:
        self.assertEqual(len(_prompt_fingerprint("", "")), 16)


class LineKeyTest(unittest.TestCase):
    """句号 ≠ 格号（2026-10-30 实测事故）。

    C6 提示词规定 lines.json 的 `index` 是「这句台词所属的格号」，
    而 `_line_key` 旧实现把 `index` 当句号用 —— 同一格两句台词算出同一个键，
    第 2 句被判成"重复"直接丢弃：11 句只剩 8 条音轨，第 3/5/8 格的第二句全丢，
    音轨还错配到别的格（成片里"字幕有字、声音念的是别人的词"）。
    """

    def test_two_lines_in_same_panel_get_distinct_keys(self) -> None:
        lines = [{"index": 3, "text": "链子掉了。"}, {"index": 3, "text": "我看看。"}]
        keys = [_line_key(ln, i) for i, ln in enumerate(lines, 1)]
        self.assertEqual(keys, ["1", "2"])

    def test_index_is_panel_number_not_line_identity(self) -> None:
        # index=9 是格号；句号应退回数组序号
        self.assertEqual(_line_key({"index": 9}, 4), "4")

    def test_explicit_line_no_wins(self) -> None:
        self.assertEqual(_line_key({"line_no": 7, "index": 3}, 1), "7")

    def test_all_lines_unique_across_panels(self) -> None:
        lines = [{"index": n} for n in (2, 3, 3, 4, 5, 5, 8, 8, 9, 11, 12)]
        keys = [_line_key(ln, i) for i, ln in enumerate(lines, 1)]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
