"""回归测试：分镜图并发 / 特殊 token 清洗 / 快通道草稿提示。

对应 2026-11-05 用户反馈（陈年老窖白酒广告任务）：
  1. 9 格分镜串行生图实测 10 分 22 秒 —— 整条管线最慢的一段，必须并发；
  2. MiniMax-M3-W8A8 会在正文里漏出 `]<]minimax[>[` 这类控制符，
     污染 <<<FILE:>>> 块与 DSML 调用的解析，表现为"模型写了但 harness 说没产出"；
  3. 快通道收尾只写"任务完成"，用户点开产物发现没有 mp4 —— 文案失职。
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
from app.harness import runner as runner_mod
from app.harness import tools as tools_mod
from app.harness.runner import RunManager
from app.harness.tools import ToolContext
from app.llm.providers import sanitize_model_text
from app.models import Base, Event, Run
from app.store import StorageAdapter


class SanitizeModelTextTest(unittest.TestCase):
    def test_strips_minimax_tokens(self) -> None:
        self.assertEqual(sanitize_model_text("你好]<]minimax[>[世界"), "你好世界")
        self.assertEqual(sanitize_model_text("a]<]minimax[>b"), "ab")

    def test_strips_openai_style_tokens(self) -> None:
        self.assertEqual(sanitize_model_text("a<|endoftext|>b"), "ab")
        self.assertEqual(sanitize_model_text("x<|im_start|>y"), "xy")

    def test_plain_text_untouched(self) -> None:
        text = "正常文本，不动。<这里不是控制符>"
        self.assertEqual(sanitize_model_text(text), text)

    def test_collapses_blank_lines_left_behind(self) -> None:
        self.assertEqual(sanitize_model_text("行1]<]minimax[>[\n\n\n\n行2"), "行1\n\n行2")

    def test_empty_input(self) -> None:
        self.assertEqual(sanitize_model_text(""), "")


class _FakeStorage(StorageAdapter):
    """StorageAdapter + 只读探测，够 _generate_images 用。"""


class ImageConcurrencyTest(unittest.TestCase):
    """分镜图必须并发跑，且结果顺序、index 落盘都不能因并发而错。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = StorageAdapter(Path(self._tmp.name))
        self.run_id = "r1"
        self.inflight = 0
        self.max_inflight = 0
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self) -> ToolContext:
        return ToolContext(
            run_id=self.run_id, stage_id="storyboard", brief={}, storage=self.storage,
            # skill_id 避开 COMIC_STYLE_SKILLS，免得风格锚改写提示词干扰断言
            extra={"skill_id": "ppt-deck"},
        )

    def _fake_generate(self):
        async def fake(storage, run_id, prompt, path, ratio="9:16",
                       model_override=None, service_override=None, *,
                       params=None, subject_reference=None):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.calls.append(path)
            await asyncio.sleep(0.02)  # 模拟网络往返，让并发真的重叠
            self.inflight -= 1
            rel, size = storage.write_bytes(run_id, path, b"fake-image")
            return {"path": rel, "provider": "fake", "is_placeholder": False,
                    "size": size, "prompt_used": prompt}

        return fake

    def _run(self, panels: list[dict]) -> dict:
        async def go() -> dict:
            with (
                patch("app.media.image.generate_image", self._fake_generate()),
                patch.object(tools_mod, "_inspect_and_maybe_redraw", _passthrough_vision),
                patch.object(tools_mod, "_consistency_sampling",
                             _async_return({"checked": False})),
            ):
                return await tools_mod._generate_images(self._ctx(), {"panels": panels})

        return asyncio.run(go())

    def test_panels_run_concurrently(self) -> None:
        panels = [{"index": i, "prompt": f"画面 {i}"} for i in range(1, 10)]
        with patch.object(settings, "image_concurrency", 3):
            out = self._run(panels)
        self.assertGreater(self.max_inflight, 1, "9 格必须并发，不能还是串行")
        self.assertLessEqual(self.max_inflight, 3, "并发路数不能超过配置上限")
        self.assertEqual(len(out["created"]), 9, out)

    def test_created_order_follows_panel_order(self) -> None:
        # 故意打乱数组顺序：成片顺序以格号为准（_order_panels），并发不能把它搅乱
        panels = [
            {"index": 3, "prompt": "c"},
            {"index": 1, "prompt": "a"},
            {"index": 2, "prompt": "b"},
        ]
        out = self._run(panels)
        self.assertEqual(out["created"], ["panels/panel-01.svg",
                                          "panels/panel-02.svg",
                                          "panels/panel-03.svg"], out)

    def test_index_keeps_every_panel(self) -> None:
        panels = [{"index": i, "prompt": f"画面 {i}"} for i in range(1, 7)]
        self._run(panels)
        index = json.loads((Path(self._tmp.name) / self.run_id / "media/images.json").read_text())
        self.assertEqual(sorted(index, key=int), ["1", "2", "3", "4", "5", "6"], index)

    def test_one_panel_failure_does_not_kill_the_batch(self) -> None:
        async def fake(storage, run_id, prompt, path, ratio="9:16",
                       model_override=None, service_override=None, *,
                       params=None, subject_reference=None):
            if "panel-02" in path:
                raise RuntimeError("网关 500")
            rel, size = storage.write_bytes(run_id, path, b"fake-image")
            return {"path": rel, "provider": "fake", "is_placeholder": False,
                    "size": size, "prompt_used": prompt}

        panels = [{"index": i, "prompt": f"画面 {i}"} for i in range(1, 4)]

        async def go() -> dict:
            with (
                patch("app.media.image.generate_image", fake),
                patch.object(tools_mod, "_inspect_and_maybe_redraw", _passthrough_vision),
                patch.object(tools_mod, "_consistency_sampling",
                             _async_return({"checked": False})),
            ):
                return await tools_mod._generate_images(self._ctx(), {"panels": panels})

        out = asyncio.run(go())
        self.assertEqual(len(out["created"]), 2, out)
        self.assertTrue(any("第 2 格" in d for d in out["degraded"]), out["degraded"])


async def _passthrough_vision(_ctx, out, *_a, **_k):
    """视觉后检直通：本测试只关心并发与顺序，不关心看图。"""
    return {"out": out, "notes": [], "detail": None}


def _async_return(value):
    async def _fn(*_a, **_k):
        return value

    return _fn


class FastChannelDraftNoteTest(unittest.TestCase):
    """快通道收尾必须明说「这是草稿，没有成片」。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, future=True)()
        self.mgr = RunManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = StorageAdapter(Path(self._tmp.name))

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self._tmp.cleanup()

    def _run_with(self, channel: str, has_final: bool) -> Run:
        run = Run(id="r1", skill_id="comic-drama", skill_name="漫剧",
                  channel=channel, status="running", brief={}, progress=0.5)
        self.db.add(run)
        self.db.commit()
        if has_final:
            self.storage.write_bytes("r1", "video/final.mp4", b"mp4")
        return run

    def _finish(self, run: Run) -> str:
        with (
            patch.object(runner_mod, "storage", self.storage),
            patch.object(self.mgr, "_register_cast", lambda *a, **k: None),
        ):
            self.mgr._finish(self.db, run, status="succeeded")
        row = (
            self.db.query(Event)
            .filter(Event.run_id == "r1", Event.type == "run.finished")
            .first()
        )
        return row.message

    def test_fast_channel_without_final_says_draft(self) -> None:
        msg = self._finish(self._run_with("fast", has_final=False))
        self.assertIn("草稿", msg)
        self.assertIn("没有成片", msg)

    def test_fast_channel_with_final_does_not_claim_draft(self) -> None:
        msg = self._finish(self._run_with("fast", has_final=True))
        self.assertNotIn("没有成片", msg)

    def test_slow_channel_untouched(self) -> None:
        msg = self._finish(self._run_with("slow", has_final=False))
        self.assertNotIn("草稿", msg)


if __name__ == "__main__":
    unittest.main()
