"""openai-video provider（Sora 式 /v1/videos 协议）的回归测试。

背景：2026-11-05 用户给了一个中转网关 + 接入点名「MiniMax H3」，要求把视频生成切过去。
实测该网关 /v1/models 只有 10 个文本/向量模型，MiniMax H3 恒返
`503 model_not_found`。所以这里做两件事：

1. 把 openai-video 协议实现好（提交 / 轮询 / 取片），换对模型名即可用；
2. 加一道**零成本预检** —— 模型不在网关清单里就在第 0 秒拦住，
   而不是让 12 个镜头各吃一次 503、最后成片全是静态图。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.media import video as video_mod

PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
    )
).decode()
DATA_URL = f"data:image/png;base64,{PNG_1PX}"


class _Resp:
    def __init__(self, payload=None, status: int = 200, text: str | None = None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {}, ensure_ascii=False)
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Client:
    """记录请求的假 AsyncClient —— 只实现被测代码用到的那几个方法。"""

    def __init__(self, resp: _Resp, calls: list):
        self._resp = resp
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._resp

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._resp


def _fake(resp: _Resp, calls: list):
    return lambda **kw: _Client(resp, calls)


class BuildPayloadTest(unittest.TestCase):
    def test_routes_to_openai_video(self) -> None:
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_model", "MiniMax H3"),
            patch.object(settings, "video_duration", 5),
            patch.object(settings, "video_resolution", ""),
            patch.object(settings, "video_params_json", ""),
            patch.object(settings, "video_models_json", ""),
        ):
            payload = video_mod.build_payload(prompt="一只猫走过", duration=5, first_frame=DATA_URL)
        self.assertEqual(payload["model"], "MiniMax H3")
        self.assertEqual(payload["prompt"], "一只猫走过")
        self.assertEqual(payload["seconds"], "5")
        self.assertEqual(payload["_first_frame"], DATA_URL)
        self.assertNotIn("parameters", payload)

    def test_last_frame_and_reference_video_are_dropped_with_note(self) -> None:
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_model", "m"),
            patch.object(settings, "video_duration", 5),
            patch.object(settings, "video_params_json", ""),
            patch.object(settings, "video_models_json", ""),
        ):
            payload = video_mod.build_payload(
                prompt="p", duration=5, last_frame=DATA_URL, reference_video="https://x/y.mp4"
            )
        self.assertIn("尾帧已忽略", payload["_note"])
        self.assertIn("参考视频", payload["_note"])

    def test_duration_out_of_range_is_blocked_locally(self) -> None:
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_model", "m"),
            patch.object(settings, "video_params_json", ""),
            patch.object(settings, "video_models_json", ""),
        ):
            with self.assertRaises(video_mod.VideoConfigError):
                video_mod.build_payload(prompt="p", duration=99)


class SubmitTest(unittest.TestCase):
    def test_submit_posts_multipart_with_first_frame(self) -> None:
        calls: list = []
        resp = _Resp({"id": "vid_123", "status": "queued"})
        payload = {
            "model": "MiniMax H3",
            "prompt": "p",
            "seconds": "5",
            "_first_frame": DATA_URL,
            "_duration": 5,
            "_note": "",
        }
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            task_id = asyncio.run(video_mod.submit_task(payload))

        self.assertEqual(task_id, "vid_123")
        method, url, kw = calls[0]
        self.assertEqual((method, url), ("POST", "https://gw.example.com/v1/videos"))
        # 首帧必须是文件，不是 URL 字段
        self.assertIn("input_reference", kw["files"])
        self.assertNotIn("_first_frame", kw["data"])
        self.assertNotIn("_note", kw["data"])
        self.assertEqual(kw["headers"]["Authorization"], "Bearer sk-test")

    def test_submit_reports_model_not_found_clearly(self) -> None:
        calls: list = []
        resp = _Resp(
            {"error": {"code": "model_not_found", "message": "No available channel for model MiniMax H3"}},
            status=503,
        )
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(video_mod.submit_task({"model": "MiniMax H3", "prompt": "p", "seconds": "5"}))
        self.assertIn("503", str(ctx.exception))


class PollTest(unittest.TestCase):
    def test_completed_returns_content_endpoint_with_auth(self) -> None:
        calls: list = []
        resp = _Resp({"id": "vid_123", "status": "completed", "progress": 100})
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(settings, "video_poll_seconds", 3),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            result = asyncio.run(video_mod.poll_task("vid_123"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["video_url"], "https://gw.example.com/v1/videos/vid_123/content")
        self.assertTrue(result["needs_auth"])

    def test_failed_status_surfaces_reason(self) -> None:
        calls: list = []
        resp = _Resp({"id": "vid_1", "status": "failed", "error": {"message": "content policy"}})
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(settings, "video_poll_seconds", 3),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            result = asyncio.run(video_mod.poll_task("vid_1"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("content policy", result["error"])


class PreflightTest(unittest.TestCase):
    def test_missing_model_is_caught_before_any_shot(self) -> None:
        calls: list = []
        resp = _Resp({"data": [{"id": "DeepSeek-V4-Flash"}, {"id": "Qwen3.8-27b"}]})
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(settings, "video_model", "MiniMax H3"),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            ok, why = asyncio.run(video_mod.preflight())
        self.assertFalse(ok)
        self.assertIn("MiniMax H3", why)
        self.assertIn("DeepSeek-V4-Flash", why)
        # 预检只查清单，绝不提交任务
        self.assertEqual([c[0] for c in calls], ["GET"])

    def test_model_present_passes(self) -> None:
        calls: list = []
        resp = _Resp({"data": [{"id": "MiniMax H3"}]})
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(settings, "video_model", "MiniMax H3"),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            ok, why = asyncio.run(video_mod.preflight())
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_gateway_without_models_endpoint_is_not_blocked(self) -> None:
        calls: list = []
        resp = _Resp(None, status=404, text="not found")
        with (
            patch.object(settings, "video_provider", "openai-video"),
            patch.object(settings, "video_base_url", "https://gw.example.com/v1"),
            patch.object(settings, "video_api_key", "sk-test"),
            patch.object(settings, "video_model", "m"),
            patch.object(video_mod.httpx, "AsyncClient", _fake(resp, calls)),
        ):
            ok, _ = asyncio.run(video_mod.preflight())
        self.assertTrue(ok)

    def test_other_providers_skip_preflight(self) -> None:
        with patch.object(settings, "video_provider", "jdcloud"):
            ok, why = asyncio.run(video_mod.preflight())
        self.assertTrue(ok)
        self.assertEqual(why, "")


if __name__ == "__main__":
    unittest.main()
