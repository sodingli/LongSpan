"""回归测试：C2 数字契约 + 规格风格锚 + C4 单角色重生成 + C5 样片门禁。

对应 2026-10-29 用户提出的四条优化：

  1. C2 生产规格必须按用户输入的时长制定，是后面所有阶段的数字契约；
     不输入集数就以用户输入的为准。
     实测事故：constraints.json 里模型把用户填的 episodes=-18 判成"无效输入"、
     自行改成单集 —— 用户输入与模型判断的边界不清，规格就成了"模型觉得合理"的规格。
  2. C4 角色设定后可以改提示词、可以点重新生成定妆图。
     旧实现幂等条件是"文件存在就复用"，改了锚点也不会重画。
  3. C5 先出样片、样片可编辑可重生成 → 必须有门禁，否则一路跑到配音结束。
  4. 生图提示词必须有风格锚，且以 spec.json 的 style_anchor 为准（不再由 media 层硬编码说了算）。
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
from app.harness.consistency import validate_spec_duration
from app.harness.skill import SkillRegistry
from app.harness.tools import ToolContext
from app.media.image import ensure_style_anchor
from app.store import StorageAdapter


class SpecDurationContractTest(unittest.TestCase):
    """C2：用户输入的时长/集数是硬数字，模型不得改判。"""

    def test_matching_spec_passes(self) -> None:
        spec = {
            "total_duration_seconds": 38,
            "grid_count": 8,
            "seconds_per_panel": 4.75,
            "tolerance": {"range_seconds": [34.2, 41.8]},
        }
        ok, reasons = validate_spec_duration(spec, {"duration": 38, "episodes": 1})
        self.assertTrue(ok, reasons)

    def test_duration_drift_fails(self) -> None:
        # 用户要 38 秒，模型自作主张做成 60 秒 —— 必须判失败
        spec = {"total_duration_seconds": 60, "grid_count": 12, "seconds_per_panel": 5}
        ok, reasons = validate_spec_duration(spec, {"duration": 38})
        self.assertFalse(ok)
        self.assertTrue(any("偏离用户输入" in r for r in reasons), reasons)

    def test_grid_count_must_be_derived(self) -> None:
        # 38s ÷ 5s ≈ 7.6 → 8 格；写成 12 格就是"拍脑袋"
        spec = {"total_duration_seconds": 38, "grid_count": 12, "seconds_per_panel": 5}
        ok, reasons = validate_spec_duration(spec, {"duration": 38})
        self.assertFalse(ok)
        self.assertTrue(any("格数" in r for r in reasons), reasons)

    def test_episodes_must_follow_user_input(self) -> None:
        spec = {"total_duration_seconds": 60, "grid_count": 12, "seconds_per_panel": 5,
                "episodes": 3}
        ok, reasons = validate_spec_duration(spec, {"duration": 60, "episodes": 1})
        self.assertFalse(ok)
        self.assertTrue(any("集数" in r for r in reasons), reasons)

    def test_missing_total_duration_fails(self) -> None:
        ok, reasons = validate_spec_duration({"grid_count": 12}, {"duration": 60})
        self.assertFalse(ok)
        self.assertTrue(any("total_duration_seconds" in r for r in reasons), reasons)

    def test_no_user_duration_only_checks_grid_math(self) -> None:
        spec = {"total_duration_seconds": 60, "grid_count": 12, "seconds_per_panel": 5}
        ok, reasons = validate_spec_duration(spec, {})
        self.assertTrue(ok, reasons)


class SpecStyleAnchorTest(unittest.TestCase):
    """风格锚以 spec.json 为准：原文复用、统一提到最前、只补与画种无关的审核底线。

    2026-11-02 事故回归：旧实现会把规格锚里的 semi-realistic 改写成 anime，
    并前置硬编码的 `flat cel shading on skin` —— 与规格锚里的 `no flat cel-shading`
    直接对冲，成片从第 6 格（近景开始）起整体变日系平涂。
    """

    def test_missing_spec_anchor_is_prepended(self) -> None:
        anchor = "Flat 2D Chinese comic cel-shading style, thick clean outlines"
        prompt, note = ensure_style_anchor("A man walking in the rain", anchor)
        self.assertTrue(prompt.startswith(anchor))
        self.assertIn("规格风格锚", note or "")

    def test_present_spec_anchor_is_kept_verbatim(self) -> None:
        anchor = "Flat 2D Chinese comic cel-shading style, thick clean outlines"
        prompt = f"{anchor}. A man walking in the rain, NOT a photograph"
        out, note = ensure_style_anchor(prompt, anchor)
        self.assertTrue(out.startswith(anchor))
        self.assertNotIn("规格风格锚", note or "")

    def test_impasto_spec_anchor_is_not_rewritten(self) -> None:
        # 厚涂写实规格锚：一个词都不许改，更不许注入平涂条款
        anchor = (
            "Thick impasto painterly semi-realistic illustration, visible oil brush strokes, "
            "no flat cel-shading, no line-art comic style"
        )
        out, _ = ensure_style_anchor("Close-up of a young woman's face", anchor)
        self.assertTrue(out.startswith(anchor), out[:200])
        self.assertIn("semi-realistic", out)
        low = out.lower()
        self.assertNotIn("flat cel shading on skin", low)
        self.assertNotIn("anime face with large expressive eyes", low)
        self.assertNotIn("stylized 2d anime", low)

    def test_anchor_is_canonicalized_to_front(self) -> None:
        # 模型把规格锚写在中间时，统一提到最前 —— 12 格前缀必须一致
        anchor = "Flat 2D cel-shading, thick clean outlines"
        prompt = f"A man walking. {anchor}. NOT a photograph"
        out, note = ensure_style_anchor(prompt, anchor)
        self.assertTrue(out.startswith(anchor))
        self.assertIn("统一提到最前", note or "")

    def test_headless_shot_gets_same_style_prefix(self) -> None:
        # 无脸特写不许走另一套风格前缀（旧实现第 10 格前缀与其余 11 格不同）
        anchor = "Thick impasto painterly semi-realistic illustration, no flat cel-shading"
        body = "Extreme close-up of two hands holding a paper cup, no face in frame"
        out, _ = ensure_style_anchor(body, anchor)
        self.assertTrue(out.startswith(anchor))
        self.assertNotIn("anime", out.lower())

    def test_audit_clause_is_style_neutral(self) -> None:
        out, _ = ensure_style_anchor("A man walking", None)
        low = out.lower()
        self.assertIn("not photorealistic", low)
        for word in ("flat cel shading", "lineart", "anime face", "impasto"):
            self.assertNotIn(word, low)

    def test_body_photoreal_claim_is_neutralized_but_anchor_kept(self) -> None:
        anchor = "Thick impasto painterly semi-realistic illustration"
        out, note = ensure_style_anchor(f"{anchor}. A photorealistic portrait of a man", anchor)
        self.assertTrue(out.startswith(anchor))
        self.assertNotIn("A photorealistic portrait", out)
        self.assertIn("中和正文写实倾向", note or "")


class ManifestContractTest(unittest.TestCase):
    """manifest 是流程本身：这两条声明缺了，上面的代码就等于没接上。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SkillRegistry(settings.skills_dir)
        cls.registry.scan()
        cls.skill = cls.registry.get("comic-drama")

    def _stage(self, sid: str):
        return next(s for s in self.skill.stages if s.id == sid)

    def test_c2_declares_spec_duration_check(self) -> None:
        kinds = [c.get("kind") for c in (self._stage("production_spec").consistency_checks or [])]
        self.assertIn("spec_duration", kinds)

    def test_c5_has_sample_gate(self) -> None:
        stage = self._stage("storyboard")
        self.assertTrue(
            stage.human_approval_default,
            "分镜阶段必须有门禁 —— 样片出来要停下来让用户改/重生成，否则一路跑到配音结束",
        )

    def test_c5_still_limits_fast_channel_to_sample(self) -> None:
        stage = self._stage("storyboard")
        limits = [a.get("fast_limit") for a in (stage.actions or []) if a.get("tool") == "generate_images"]
        self.assertEqual(limits, [3])


CHARACTERS_MD = """# 角色设计

## 陈默（主角）

### 一致性锚点（英文生图描述词，冻结字符串）

CHENMO_ANCHOR: A 30-year-old man with short black hair, thin-rimmed glasses, a loose beige trench coat, carrying a worn leather satchel.

## 林苑舟（女主）

### 一致性锚点（英文生图描述词，冻结字符串）

LIN_ANCHOR: A 26-year-old woman with long dark wavy hair, a white knit sweater, silver hoop earrings, holding a paper cup.
"""


class CharacterArtRegenerateTest(unittest.TestCase):
    """C4：单角色改提示词 + 重新生成定妆图。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = StorageAdapter(Path(self.tmp.name))
        self.run_id = "r1"
        self.storage.write_text(self.run_id, "characters.md", CHARACTERS_MD)
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

    def test_first_run_creates_then_reuses(self) -> None:
        with patch("app.media.image.generate_image", self._fake_generate()):
            first = asyncio.run(tools_mod._generate_character_art(self._ctx()))
            self.assertEqual(len(first["created"]), 2, first)
            second = asyncio.run(tools_mod._generate_character_art(self._ctx()))
            self.assertEqual(len(second["reused"]), 2, second)
        self.assertEqual(len(self.calls), 2, "第二次不该重复生图（幂等）")

    def test_force_regenerates_only_named_role(self) -> None:
        with patch("app.media.image.generate_image", self._fake_generate()):
            asyncio.run(tools_mod._generate_character_art(self._ctx()))
            self.calls.clear()
            out = asyncio.run(
                tools_mod._generate_character_art(self._ctx(), force=True, roles=["chenmo"])
            )
        self.assertEqual(len(out["created"]), 1, out)
        self.assertEqual(len(self.calls), 1, "只该重画被点名的角色")
        self.assertIn("chenmo", self.calls[0]["path"])

    def test_prompt_override_takes_effect(self) -> None:
        from app import canvas_store as cs

        cs.save_character_art_prompt(
            self.storage, self.run_id, "chenmo",
            "A calm man in a grey hoodie, short black hair, thin-rimmed glasses",
        )
        with patch("app.media.image.generate_image", self._fake_generate()):
            asyncio.run(
                tools_mod._generate_character_art(self._ctx(), force=True, roles=["chenmo"])
            )
        self.assertIn("grey hoodie", self.calls[0]["prompt"])
        # 锚点原文只作兜底 —— 被覆盖后不该再出现在提示词里
        self.assertNotIn("beige trench coat", self.calls[0]["prompt"])

    def test_unknown_role_is_reported(self) -> None:
        out = asyncio.run(
            tools_mod._generate_character_art(self._ctx(), force=True, roles=["nobody"])
        )
        self.assertIn("error", out)


REAL_WORLD_CHARACTERS_MD = """# 《两分钟》· C4 角色设计（定稿）

## 0. 本版与上游的关系（等价性声明 · 必读）

上游 storyboard.md 临时用中文视觉锚点出图，标签为 `LINXIAOMAN_ANCHOR:` / `CHENMO_ANCHOR:`。

## 1. 林小满 · 女生 · 21

### 一致性锚点（冻结字符串 · 逐字复用，含标点）

LINXIAOMAN_ANCHOR: young Chinese woman, 21 years old, soft round face, off-white knit beanie.

## 2. 陈默 · 外卖骑手 · 26

### 一致性锚点（冻结字符串 · 逐字复用，含标点）

CHENMO_ANCHOR: young Chinese man, 26 years old, lean angular face, orange windbreaker jacket.

## 4. 视觉区分度（9:16 窄画幅 · 涂黑也要能分清）

| 角色 | 主色 |
|---|---|

## 5. 定妆图清单（给 `generate_character_art`）

**定妆图 prompt 模板**：`{STYLE_ANCHOR} {CHARACTER_ANCHOR}. Character reference sheet`
"""


class CharacterRoleParsingTest(unittest.TestCase):
    """画布上的角色列表必须只包含真角色。

    2026-10-29 实测：旧实现把所有 `## ` 二级标题都当角色，于是
    `## 0. 本版与上游的关系` / `## 4. 视觉区分度` / `## 5. 定妆图清单`
    全成了"角色"，画布上 4 个角色里 3 个没有定妆图，用户不知道该点哪个。
    """

    def test_only_anchor_sections_are_roles(self) -> None:
        from app.canvas_store import _role_pairs

        pairs = _role_pairs(REAL_WORLD_CHARACTERS_MD)
        self.assertEqual(pairs, [("林小满", "linxiaoman"), ("陈默", "chenmo")])

    def test_characters_state_exposes_art_per_role(self) -> None:
        from app import canvas_store as cs

        with tempfile.TemporaryDirectory() as tmp:
            storage = StorageAdapter(Path(tmp))
            storage.write_text("r1", "characters.md", REAL_WORLD_CHARACTERS_MD)
            storage.write_bytes("r1", "characters/chenmo.png", b"img")
            state = cs.characters_state(storage, "r1")
        self.assertEqual(state["roles"], ["林小满", "陈默"])
        self.assertEqual(state["role_tags"], ["linxiaoman", "chenmo"])
        self.assertTrue(state["art"]["陈默"]["has_art"])
        self.assertEqual(state["art"]["陈默"]["art_path"], "characters/chenmo.png")
        self.assertFalse(state["art"]["林小满"]["has_art"])


class OpenAIImageEditTest(unittest.TestCase):
    """参考图必须真的送进生图接口。

    2026-10-29 实测：主通道 openai-compat（gpt-image-2）的 `/images/generations`
    只吃文字，参考图能力位是 False —— 「用定妆图锁脸」在配置层面就是空转。
    图生图入口是 `/images/edits`，报文形状是试出来的：
      - multipart → 网关报 `invalid character '-' in numeric literal`
      - `"image": "data:..."` → 报 `cannot unmarshal string ... of type []string`
      - `"image": ["data:..."]` → 正常返回 b64_json
    """

    def setUp(self) -> None:
        from app.media import image as image_mod

        self.mod = image_mod
        self.captured: list[dict] = []
        self.calls = self.captured

    def _patch_client(self, status: int = 200, payload: dict | None = None):
        captured = self.captured
        body = payload or {"data": [{"b64_json": "aW1n"}]}

        class _Resp:
            def __init__(self, st, data):
                self.status_code = st
                self._data = data
                self.text = str(data)[:200]

            def json(self):
                return self._data

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                captured.append({"url": url, "json": json, "files": files})
                if len(captured) == 1 and status >= 400:
                    return _Resp(status, {"error": "unsupported"})
                return _Resp(200, body)

        return patch.object(self.mod.httpx, "AsyncClient", _Client)

    def test_edit_endpoint_gets_image_array(self) -> None:
        ref = "data:image/png;base64,AAAA"
        with self._patch_client():
            data, ext, notes = asyncio.run(
                self.mod._request_real_image(
                    "a prompt", "9:16", base_url="https://x/v1", api_key="k",
                    subject_refs=[ref],
                )
            )
        self.assertEqual(data, b"img")
        self.assertEqual(ext, "png")
        self.assertEqual(notes, [])
        self.assertTrue(self.captured[0]["url"].endswith("/images/edits"))
        self.assertEqual(self.captured[0]["json"]["image"], [ref])
        self.assertIsNone(self.captured[0]["files"], "必须是 JSON 报文，不是 multipart")

    def test_edit_endpoint_accepts_multiple_references(self) -> None:
        # 同框格（2026-11-02）：两张定妆图必须一起送，只送一张 = 配角每格重画一张脸
        refs = ["data:image/png;base64,AAAA", "data:image/png;base64,BBBB"]
        with self._patch_client():
            asyncio.run(
                self.mod._request_real_image(
                    "a prompt", "9:16", base_url="https://x/v1", api_key="k",
                    subject_refs=refs,
                )
            )
        self.assertEqual(self.captured[0]["json"]["image"], refs)

    def test_falls_back_to_generations_and_says_reference_unused(self) -> None:
        ref = "data:image/png;base64,AAAA"
        with self._patch_client(status=404):
            data, _ext, notes = asyncio.run(
                self.mod._request_real_image(
                    "a prompt", "9:16", base_url="https://x/v1", api_key="k",
                    subject_refs=[ref],
                )
            )
        self.assertEqual(data, b"img")
        self.assertTrue(self.captured[1]["url"].endswith("/images/generations"))
        self.assertTrue(any("参考图未生效" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
