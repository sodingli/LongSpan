"""回归测试：视觉自检协议适配 + 视频尾帧降级 + 片段复用。

对应 2026-10-28 实测事故（任务 0e7d2cb8…，73 条降级、成片永远出不来）：
  1. 视觉自检写死 DashScope 原生协议 + qwen3-vl-plus，而 base_url 指向京东云
     → 每格 400，8 格全部 checked=false，视觉这层等于不存在；
  2. 京东云 Seedance 对 last_frame 一律 500，代码死磕同一报文重试 3 次
     → 8 格里 7 格退回静态运镜，整集没有一段真视频；
  3. 最后一格天然没有尾帧（chained=false），复用判据却要求 chained=true
     → 每轮重跑都重新生成一次（重复计费）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.harness import tools as tools_mod
from app.harness.runner import RunManager
from app.harness.tools import ToolContext
from app.media import vision_check as vc
from app.models import Base, Event


class VisionProtocolTest(unittest.TestCase):
    def test_protocol_inferred_from_base_url(self) -> None:
        with patch.object(settings, "vision_provider", "auto"):
            self.assertEqual(vc._protocol("https://dashscope.aliyuncs.com/api/v1"), "dashscope")
            self.assertEqual(vc._protocol("https://modelservice.jdcloud.com/v1"), "openai-compat")
            self.assertEqual(vc._protocol("https://example.com/v1"), "openai-compat")

    def test_explicit_provider_wins_over_inference(self) -> None:
        with patch.object(settings, "vision_provider", "dashscope"):
            self.assertEqual(vc._protocol("https://modelservice.jdcloud.com/v1"), "dashscope")
        with patch.object(settings, "vision_provider", "openai-compat"):
            self.assertEqual(vc._protocol("https://dashscope.aliyuncs.com/api/v1"), "openai-compat")

    def test_endpoint_uses_chat_completions_for_openai_compat(self) -> None:
        with (
            patch.object(settings, "vision_provider", "auto"),
            patch.object(settings, "vision_base_url", "https://modelservice.jdcloud.com/v1"),
            patch.object(settings, "vision_api_key", "k"),
            patch.object(settings, "vision_model", "GPT-5.5"),
        ):
            url, key, model, proto = vc._endpoint()
        self.assertEqual(url, "https://modelservice.jdcloud.com/v1/chat/completions")
        self.assertEqual((key, model, proto), ("k", "GPT-5.5", "openai-compat"))

    def test_endpoint_keeps_dashscope_native_path(self) -> None:
        with (
            patch.object(settings, "vision_provider", "auto"),
            patch.object(settings, "vision_base_url", "https://dashscope.aliyuncs.com/api/v1"),
            patch.object(settings, "vision_api_key", "k"),
            patch.object(settings, "vision_model", ""),
        ):
            url, _key, model, proto = vc._endpoint()
        self.assertEqual(
            url, "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        )
        self.assertEqual(model, "qwen3-vl-plus")
        self.assertEqual(proto, "dashscope")

    def test_openai_body_omits_temperature(self) -> None:
        # GPT-5.5 只接受默认 temperature，传 0 直接 400 —— 自检是"不阻断"的一层，
        # 一次 400 就等于这一格没检查。
        body = vc._build_body("openai-compat", "m", ["data:image/png;base64,AA"], "P")
        self.assertNotIn("temperature", json.dumps(body))
        self.assertIn("max_tokens", body)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertEqual(content[-1], {"type": "text", "text": "P"})

    def test_dashscope_body_keeps_native_shape(self) -> None:
        body = vc._build_body("dashscope", "m", ["data:image/png;base64,AA"], "P")
        self.assertEqual(body["parameters"]["result_format"], "message")
        content = body["input"]["messages"][0]["content"]
        self.assertEqual(content[0]["image"], "data:image/png;base64,AA")
        self.assertEqual(content[-1]["text"], "P")

    def test_extract_text_both_shapes(self) -> None:
        oa = {"choices": [{"message": {"content": "{\"ok\":1}"}}]}
        self.assertEqual(vc._extract_text(oa, "openai-compat"), "{\"ok\":1}")
        ds = {"output": {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}}
        self.assertEqual(vc._extract_text(ds, "dashscope"), "ab")

    def test_extract_text_accepts_content_parts(self) -> None:
        oa = {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]}
        self.assertEqual(vc._extract_text(oa, "openai-compat"), "hi")


class _FakeStorage:
    """最小可用存储：只实现 _generate_panel_videos 用到的四个方法。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run_workspace(self, run_id: str) -> Path:
        return self.root

    def exists(self, run_id: str, rel: str) -> bool:
        return (self.root / rel).exists()

    def read_text(self, run_id: str, rel: str) -> str:
        return (self.root / rel).read_text()

    def write_json(self, run_id: str, rel: str, obj) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False))


async def _noop_sleep(*_a, **_k) -> None:
    return None


_JD_500 = (
    '视频任务提交失败 HTTP 500：{"error":{"cause":"","code":500,'
    '"message":"任务信息落库失败,modelTaskId=","status":"INTERNAL"}}'
)


class PanelVideoFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        tools_mod._LAST_FRAME_STATE["broken_models"].clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "panels").mkdir(parents=True)
        for i in (1, 2):
            (self.root / f"panels/panel-{i:02d}.png").write_bytes(b"png")
        images = {
            "1": {"path": "panels/panel-01.png"},
            "2": {"path": "panels/panel-02.png"},
        }
        (self.root / "media").mkdir()
        (self.root / "media/images.json").write_text(json.dumps(images))
        self.ctx = ToolContext(
            run_id="r1", stage_id="compose", brief={}, storage=_FakeStorage(self.root)
        )
        self.plan = {"panels": [{"index": 1, "prompt": "a"}, {"index": 2, "prompt": "b"}]}
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        tools_mod._LAST_FRAME_STATE["broken_models"].clear()
        self._tmp.cleanup()

    def _fake_generate(self):
        async def _gen(workspace, *, prompt, first_frame, last_frame, reference_video,
                       duration, mode, out_rel, model):
            self.calls.append({"last_frame": last_frame, "out_rel": out_rel})
            if last_frame:
                raise RuntimeError(_JD_500)
            dest = Path(workspace) / out_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"mp4")
            return {"path": out_rel, "provider": "jdcloud"}

        return _gen

    def _run(self):
        with (
            patch("app.media.video.video_ready", lambda: (True, "")),
            patch("app.media.video.generate_clip", self._fake_generate()),
            patch("asyncio.sleep", _noop_sleep),
        ):
            return asyncio.run(tools_mod._generate_panel_videos(self.ctx, self.plan))

    def test_last_frame_reject_is_detected(self) -> None:
        self.assertTrue(tools_mod._looks_like_last_frame_reject(RuntimeError(_JD_500)))
        self.assertFalse(tools_mod._looks_like_last_frame_reject(RuntimeError("HTTP 429 限流")))

    def test_falls_back_to_single_first_frame(self) -> None:
        out = self._run()
        self.assertEqual(sorted(out["generated"]), ["1", "2"])
        # 第 1 格：带尾帧失败 2 次 → 单首帧成功；第 2 格：熔断后直接单首帧
        self.assertEqual(len([c for c in self.calls if c["last_frame"]]), 2)
        self.assertEqual(len([c for c in self.calls if not c["last_frame"]]), 2)
        self.assertTrue(any("不支持尾帧" in n for n in out["degraded"]))

        images = json.loads((self.root / "media/images.json").read_text())
        self.assertFalse(images["1"]["video"]["chained"])
        self.assertTrue(images["1"]["video"]["chain_fallback"])
        self.assertFalse(images["2"]["video"]["chained"])
        self.assertNotIn("chain_fallback", images["2"]["video"])

    def test_ready_clips_are_reused_instead_of_regenerated(self) -> None:
        self._run()
        self.calls.clear()
        out = self._run()
        self.assertEqual(self.calls, [])
        self.assertEqual(sorted(out["reused"]), ["1", "2"])

    def test_last_panel_without_chain_is_reusable(self) -> None:
        # 最后一格天然 chained=false；旧判据要求 chained=true，导致每轮重跑都重画一次
        self._run()  # 先正常生成一遍，拿到带来源指纹的片段
        images = json.loads((self.root / "media/images.json").read_text())
        clip = images["2"]["video"]
        self.assertFalse(clip["chained"])
        # 2026-10-30：复用判据新增「来源指纹」—— 只有能证明"片段来自当前这张图"
        # 才允许复用，否则分镜重画后会出现新图配旧视频。
        self.assertTrue(clip.get("source_fingerprint"))

        self.calls.clear()
        out = self._run()
        self.assertIn("2", out["reused"])
        self.assertNotIn("2", out["generated"])

    def test_stale_fingerprint_forces_regeneration(self) -> None:
        """分镜重画后，旧片段必须重做 —— 否则成片是新图配旧视频。"""
        self._run()
        images = json.loads((self.root / "media/images.json").read_text())
        images["1"]["video"]["source_fingerprint"] = "deadbeefdeadbeef"
        (self.root / "media/images.json").write_text(json.dumps(images))

        self.calls.clear()
        out = self._run()
        self.assertIn("1", out["generated"])
        self.assertNotIn("1", out["reused"])
        self.assertTrue(any("旧视频片段不再复用" in n for n in out["degraded"]), out["degraded"])

    def test_clip_without_fingerprint_is_regenerated(self) -> None:
        """历史片段没有指纹 = 无法证明与当前分镜一致 → 重做，不静默复用。"""
        self._run()
        images = json.loads((self.root / "media/images.json").read_text())
        images["1"]["video"].pop("source_fingerprint", None)
        (self.root / "media/images.json").write_text(json.dumps(images))

        self.calls.clear()
        out = self._run()
        self.assertIn("1", out["generated"])
        self.assertTrue(any("没有来源指纹" in n for n in out["degraded"]), out["degraded"])


class DegradedEventDedupTest(unittest.TestCase):
    """同一个降级说明在阶段重做时不该被反复 emit。

    实测：一个任务 82 条降级日志，其中 49 条是同一句"第 N 格视频生成失败"——
    阶段验收不过会自动重做，每次重做把同样的降级再报一遍，真正的信号被淹了。
    """

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, future=True)()
        self.mgr = RunManager()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_same_note_emitted_once_per_stage(self) -> None:
        note = "第 3 格视频生成失败（重试 3 次），退回静态运镜：HTTP 500"
        for _ in range(5):
            self.mgr._emit_degraded_once(self.db, "r1", "compose", "generate_panel_videos", note)
        rows = self.db.query(Event).filter(Event.run_id == "r1").all()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].message.startswith("降级说明："))

    def test_different_notes_are_kept(self) -> None:
        for note in ("第 1 格失败", "第 2 格失败"):
            self.mgr._emit_degraded_once(self.db, "r1", "compose", "t", note)
        self.assertEqual(self.db.query(Event).filter(Event.run_id == "r1").count(), 2)

    def test_same_note_in_another_stage_is_kept(self) -> None:
        self.mgr._emit_degraded_once(self.db, "r1", "compose", "t", "同一句话")
        self.mgr._emit_degraded_once(self.db, "r1", "storyboard", "t", "同一句话")
        self.assertEqual(self.db.query(Event).filter(Event.run_id == "r1").count(), 2)


if __name__ == "__main__":
    unittest.main()
