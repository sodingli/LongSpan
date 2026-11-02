"""回归测试：画风契约与多角色参考图（2026-11-02 事故）。

事故现象（用户反馈）：成片 0:24 之后画风突变、人物也跟前面不是一个人。

根因（已用产物逐格比对确认）：
  1. `ensure_style_anchor` 把 spec.json 的 style_anchor **改写**了
     （semi-realistic → semi-stylized anime），又在最前面硬编码注入
     `flat cel shading on skin` + `clean drawn lineart`；
     而同一段 prompt 里规格锚写着 `no flat cel-shading, no line-art comic style`。
     一段提示词两套画风指令，模型按「哪条更具体」选：全景格（脸小）听厚涂条款，
     近景/特写格（脸大）听「anime face with large expressive eyes」——
     0:24 正好是格 5/6 的分界，第 6 格起全变日系平涂。
  2. 定妆图走的是另一套硬编码平涂条款（CHARACTER_ART_STYLE），
     参考图与分镜提示词画风分叉，近景格被参考图继续往平涂拽。
  3. 12 格里 8 格是双人同框，但参考图只送 1 张 → 配角每格重画一张脸。
  4. 视觉自检触发的重画漏传参考图 → 越"修"越不像。

这里守住四条：规格锚原文不许改、逐格前缀必须一致、同框格必须带齐参考图、
重画必须带同一批参考图。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.harness import tools as tools_mod
from app.harness.consistency import validate_style_anchor
from app.harness.skill import SkillRegistry
from app.harness.tools import ToolContext
from app.media.image import ensure_style_anchor
from app.store import StorageAdapter

IMPASTO_ANCHOR = (
    "Thick impasto painterly semi-realistic illustration, visible oil brush strokes with "
    "heavy paint buildup, strong warm-versus-cool color contrast, NOT a photograph, "
    "NOT photorealistic, no flat cel-shading, no line-art comic style"
)


def _panel(index: int, prompt: str) -> dict:
    return {"index": index, "prompt": prompt}


class StyleAnchorContractTest(unittest.TestCase):
    """C5 画风契约：每格 prompt 必须原文复用 spec.json 的 style_anchor。"""

    def test_all_panels_reusing_anchor_pass(self) -> None:
        spec = {"style_anchor": IMPASTO_ANCHOR}
        panels = {"panels": [_panel(i, f"{IMPASTO_ANCHOR}. Shot {i}") for i in range(1, 4)]}
        ok, reasons = validate_style_anchor(panels, spec)
        self.assertTrue(ok, reasons)

    def test_missing_anchor_in_one_panel_fails(self) -> None:
        spec = {"style_anchor": IMPASTO_ANCHOR}
        panels = {"panels": [
            _panel(1, f"{IMPASTO_ANCHOR}. Shot 1"),
            _panel(2, "A stylized anime close-up of a young woman"),
        ]}
        ok, reasons = validate_style_anchor(panels, spec)
        self.assertFalse(ok)
        self.assertTrue(any("第 2 格" in r and "style_anchor" in r for r in reasons), reasons)

    def test_contradicting_style_term_fails(self) -> None:
        # 规格锚明确否定 flat cel-shading，正文却要求它 —— 一段提示词两套画风
        spec = {"style_anchor": IMPASTO_ANCHOR}
        panels = {"panels": [
            _panel(1, f"{IMPASTO_ANCHOR}. Flat cel shading on skin, clean lineart"),
        ]}
        ok, reasons = validate_style_anchor(panels, spec)
        self.assertFalse(ok)
        self.assertTrue(any("明确否定" in r for r in reasons), reasons)

    def test_repeating_the_negation_is_not_a_conflict(self) -> None:
        # 规格锚自己的 "no flat cel-shading" 不算"正文要求了平涂"
        spec = {"style_anchor": IMPASTO_ANCHOR}
        panels = {"panels": [_panel(1, f"{IMPASTO_ANCHOR}. A woman waits at a crossing")]}
        ok, reasons = validate_style_anchor(panels, spec)
        self.assertTrue(ok, reasons)

    def test_missing_spec_anchor_fails(self) -> None:
        ok, reasons = validate_style_anchor({"panels": [_panel(1, "x")]}, {})
        self.assertFalse(ok)
        self.assertTrue(any("style_anchor" in r for r in reasons), reasons)


class StyleAnchorInjectionTest(unittest.TestCase):
    """生图层只准原文复用规格锚，不准改写、不准加指定画种的条款。"""

    def test_no_art_style_clause_is_injected(self) -> None:
        out, _ = ensure_style_anchor("Close-up of a woman's face", IMPASTO_ANCHOR)
        low = out.lower()
        for forbidden in (
            "flat cel shading on skin",
            "anime face with large expressive eyes",
            "stylized 2d anime",
            "tv anime screenshot",
        ):
            self.assertNotIn(forbidden, low, out[:300])

    def test_prefix_is_identical_for_face_and_headless_shots(self) -> None:
        a, _ = ensure_style_anchor("Close-up of a woman's face", IMPASTO_ANCHOR)
        b, _ = ensure_style_anchor(
            "Extreme close-up of two hands holding a cup, no face in frame", IMPASTO_ANCHOR
        )
        self.assertEqual(a[: len(IMPASTO_ANCHOR)], b[: len(IMPASTO_ANCHOR)])


CHARACTERS_MD = """# 角色设计

## 陈默（男主）

### 一致性锚点（冻结字符串）

CHENMO_ANCHOR: A 30-year-old man with short black hair, a mustard-yellow delivery jacket, a grey helmet with a shark sticker on the left side.

## 林小满（女主）

### 一致性锚点（冻结字符串）

LINXIAOMAN_ANCHOR: A 20-year-old woman with dark brown hair in a low ponytail, a cream-white knit beanie, a camel-colored long wool scarf.
"""


class MultiReferenceTest(unittest.TestCase):
    """同框格必须把每个出场角色的定妆图都带上（旧实现只送 1 张）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = StorageAdapter(Path(self.tmp.name))
        self.run_id = "r1"
        self.storage.write_text(self.run_id, "characters.md", CHARACTERS_MD)
        for role in ("chenmo", "linxiaoman"):
            self.storage.write_bytes(self.run_id, f"characters/{role}.png", b"fake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ctx(self) -> ToolContext:
        return ToolContext(
            run_id=self.run_id, stage_id="storyboard", brief={}, storage=self.storage,
            extra={"skill_id": "comic-drama"},
        )

    def test_two_character_panel_gets_two_references(self) -> None:
        prompt = (
            "CHENMO_ANCHOR: A 30-year-old man with short black hair, a mustard-yellow delivery "
            "jacket, a grey helmet with a shark sticker on the left side. "
            "LINXIAOMAN_ANCHOR: A 20-year-old woman with dark brown hair in a low ponytail, "
            "a cream-white knit beanie, a camel-colored long wool scarf."
        )
        refs, roles, notes = tools_mod._find_reference_arts(self._ctx(), prompt)
        self.assertEqual(sorted(roles), ["CHENMO", "LINXIAOMAN"], notes)
        self.assertEqual(len(refs), 2, notes)

    def test_single_character_panel_gets_one_reference(self) -> None:
        prompt = (
            "LINXIAOMAN_ANCHOR: A 20-year-old woman with dark brown hair in a low ponytail, "
            "a cream-white knit beanie, a camel-colored long wool scarf."
        )
        refs, roles, _ = tools_mod._find_reference_arts(self._ctx(), prompt)
        self.assertEqual(roles, ["LINXIAOMAN"])
        self.assertEqual(len(refs), 1)

    def test_empty_shot_gets_no_reference(self) -> None:
        refs, roles, notes = tools_mod._find_reference_arts(
            self._ctx(), "Empty street at night, absolutely no people in frame"
        )
        self.assertEqual(refs, [])
        self.assertEqual(roles, [])


class CharacterArtStyleTest(unittest.TestCase):
    """定妆图与分镜图必须同一套画风：定妆图不许写死平涂/动漫条款。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = StorageAdapter(Path(self.tmp.name))
        self.run_id = "r1"
        self.storage.write_text(self.run_id, "characters.md", CHARACTERS_MD)
        self.storage.write_text(
            self.run_id, "spec.json", '{"style_anchor": "%s"}' % IMPASTO_ANCHOR
        )
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ctx(self) -> ToolContext:
        return ToolContext(
            run_id=self.run_id, stage_id="characters", brief={}, storage=self.storage,
            extra={"skill_id": "comic-drama"},
        )

    def _fake_generate(self):
        calls = self.calls

        async def fake(storage, run_id, prompt, path, ratio="9:16",
                       model_override=None, service_override=None, *,
                       params=None, subject_reference=None):
            calls.append({"prompt": prompt, "path": path})
            rel, size = storage.write_bytes(run_id, path, b"fake-image")
            return {"path": rel, "provider": "fake", "is_placeholder": False,
                    "size": size, "prompt_used": prompt}

        return fake

    def test_character_art_uses_spec_anchor_not_flat_anime(self) -> None:
        with patch("app.media.image.generate_image", self._fake_generate()):
            asyncio.run(tools_mod._generate_character_art(self._ctx()))
        self.assertTrue(self.calls)
        for call in self.calls:
            prompt = call["prompt"]
            self.assertTrue(prompt.startswith(IMPASTO_ANCHOR), prompt[:200])
            low = prompt.lower()
            self.assertNotIn("flat 2d anime", low)
            self.assertNotIn("flat cel shading", low)
            self.assertNotIn("thick clean outlines", low)


class RedrawKeepsReferenceTest(unittest.TestCase):
    """视觉自检触发的重画必须带同一批参考图（旧实现漏传 = 换脸重画）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = StorageAdapter(Path(self.tmp.name))
        self.run_id = "r1"
        self.storage.write_bytes(self.run_id, "panels/panel-01.png", b"first")
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_redraw_passes_subject_reference(self) -> None:
        calls = self.calls

        async def fake_generate(storage, run_id, prompt, path, ratio="9:16",
                                model_override=None, service_override=None, *,
                                params=None, subject_reference=None):
            calls.append({"prompt": prompt, "subject_reference": subject_reference})
            rel, size = storage.write_bytes(run_id, path, b"redrawn")
            return {"path": rel, "provider": "fake", "is_placeholder": False, "size": size}

        async def fake_inspect(path, allow_headless=False):
            # 第一次判不合格（触发重画），第二次通过
            if len(calls) == 0:
                return {"checked": True, "ok": False, "issue": "头部被裁"}
            return {"checked": True, "ok": True, "issue": ""}

        ctx = ToolContext(
            run_id=self.run_id, stage_id="storyboard", brief={}, storage=self.storage,
            extra={"skill_id": "comic-drama"},
        )
        out = {"path": "panels/panel-01.png", "is_placeholder": False, "index": "1"}
        with patch("app.media.image.generate_image", fake_generate), \
             patch("app.media.vision_check.inspect_image", fake_inspect), \
             patch.object(settings, "vision_retry_limit", 1):
            asyncio.run(
                tools_mod._inspect_and_maybe_redraw(
                    ctx, out, "A woman standing at a crossing", "1", "9:16", None, None,
                    ["/tmp/chenmo.png"],
                )
            )
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["subject_reference"], ["/tmp/chenmo.png"])


class ManifestStyleCheckTest(unittest.TestCase):
    """manifest 是流程本身：这条校验没声明，上面的代码就等于没接上。"""

    def test_c5_declares_style_anchor_check(self) -> None:
        registry = SkillRegistry(settings.skills_dir)
        registry.scan()
        skill = registry.get("comic-drama")
        stage = next(s for s in skill.stages if s.id == "storyboard")
        kinds = [c.get("kind") for c in (stage.consistency_checks or [])]
        self.assertIn("style_anchor_consistency", kinds)


if __name__ == "__main__":
    unittest.main()
