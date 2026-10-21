"""媒体配置的连通性自检。

为什么单独一个文件：三类能力的「验证方式」本质不同，混在各自的生成函数里会让
生成路径带上测试分支（没人愿意在生产路径里维护一条只为按钮服务的代码）。

三条原则（对应项目铁律）：

1. **能证明才算通。** 只看"字段都填了"会把 503 的模型判成可用。图片/配音必须真发一次
   最小请求；成本极小但确实会计费，所以 API 层要求显式 `confirm_cost`。
2. **测试绝不触发按次计费的长任务。** 视频不为测试提交真实任务 —— 那是"用用户的钱
   验证配置"，一次几毛到几块。改为查一个不存在的 task_id：401 = Key 不对，
   其它响应 = 鉴权已通过。
3. **失败原因要能直接照做。** 返回里带 hint，说明该改哪个字段。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("longspan.media.probe")


def _pick(values: dict, key: str, default: Any, cast=None) -> Any:
    val = values.get(key)
    if val in (None, ""):
        val = default
    if cast and val is not None:
        try:
            return cast(val)
        except (TypeError, ValueError):
            return default
    return val


def _effective(kind: str, values: dict) -> dict:
    """请求里带了就用请求的（还没保存也能测），没带就用当前生效配置。"""
    prefix = {"image": "image_", "tts": "tts_", "video": "video_"}[kind]
    api_key = values.get("api_key")
    if api_key in (None, ""):
        api_key = getattr(settings, prefix + "api_key", "")
    if kind == "video":
        prov = str(values.get("provider") or getattr(settings, "video_provider", "")).lower()
        if not api_key and prov == "jdcloud":
            api_key = settings.image_api_key  # 京东云同一网关，允许复用图片 Key
            # dashscope 是独立网关，绝不复用 —— 用错 Key 的报错会指向生图，排查方向就歪了
    return {
        "provider": str(_pick(values, "provider", getattr(settings, prefix + "provider", ""))).lower(),
        "base_url": str(_pick(values, "base_url", getattr(settings, prefix + "base_url", ""))).rstrip("/"),
        "api_key": str(api_key or ""),
        "model": str(_pick(values, "model", getattr(settings, prefix + "model", ""))),
        "service": str(_pick(values, "service", getattr(settings, "image_service", ""))),
        "voice": str(_pick(values, "voice", getattr(settings, "tts_voice", ""))),
    }


async def probe(kind: str, *, values: dict, confirm_cost: bool = False) -> dict:
    if kind == "image":
        return await _probe_image(_effective("image", values), confirm_cost)
    if kind == "tts":
        return await _probe_tts(_effective("tts", values), confirm_cost)
    if kind == "video":
        return await _probe_video(_effective("video", values))
    raise ValueError(f"未知的媒体类别：{kind}")


# --------------------------------------------------------------------- 图片


async def _probe_image(cfg: dict, confirm_cost: bool) -> dict:
    started = time.time()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {
            "ok": False,
            "stage": "config",
            "error": "缺少 API Key 或接口地址",
            "hint": "填好 Key 与 Base URL 再测；不填也能跑，但出的是本地 SVG 分镜卡。",
        }
    if cfg["provider"] == "jdcloud" and not cfg["service"]:
        return {
            "ok": False,
            "stage": "config",
            "error": "京东云生图缺少「预置服务标识」",
            "hint": "在京东云控制台创建生图预置服务（如 minimax_image01），把标识填到 service。",
        }
    if not confirm_cost:
        # 不做任何请求就给出「未验证」的诚实结论，而不是伪造一个成功
        return {
            "ok": False,
            "stage": "need_confirm",
            "error": "测试会真实生成一张 1:1 小图（会产生一次调用费用）",
            "hint": "确认后再次点击「测试连接」即可。",
        }

    prompt = "配置自检：一个纯色圆点，极简"
    try:
        if cfg["provider"] == "jdcloud":
            from .image import _request_jdcloud_image

            data, ext, _notes = await _request_jdcloud_image(
                prompt, "1:1", model=cfg["model"], service=cfg["service"],
                base_url=cfg["base_url"], api_key=cfg["api_key"],
            )
        else:
            from .image import _request_real_image

            data, ext, _notes = await _request_real_image(
                prompt, "1:1", model=cfg["model"],
                base_url=cfg["base_url"], api_key=cfg["api_key"],
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "stage": "request",
            "latency_ms": int((time.time() - started) * 1000),
            "error": str(exc)[:400],
            "hint": "看错误里的 HTTP 状态码：401 是 Key，404 是路径，400/422 多半是模型名或参数。",
        }
    return {
        "ok": bool(data),
        "stage": "done",
        "latency_ms": int((time.time() - started) * 1000),
        "bytes": len(data),
        "format": ext,
        "provider": cfg["provider"],
        "model": cfg["model"] or ("image-01" if cfg["provider"] == "jdcloud" else ""),
        "preview": "data:image/" + ("jpeg" if ext == "jpg" else ext) + ";base64," + base64.b64encode(data).decode(),
        "message": "生图接口连通，返回了一张真实图片",
    }


# --------------------------------------------------------------------- 配音


async def _probe_tts(cfg: dict, confirm_cost: bool) -> dict:
    started = time.time()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {
            "ok": False,
            "stage": "config",
            "error": "缺少 API Key 或接口地址",
            "hint": "不填也能跑，但走本机语音（音色单一）。",
        }
    if not confirm_cost:
        return {
            "ok": False,
            "stage": "need_confirm",
            "error": "测试会真实合成一小段语音（会产生一次调用费用）",
            "hint": "确认后再次点击「测试连接」即可。",
        }
    try:
        from .tts import _request_real_speech

        audio = await _request_real_speech(
            "配置自检",
            cfg.get("voice") or settings.tts_voice,
            1.0,
            model=cfg["model"] or None,
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "stage": "request",
            "latency_ms": int((time.time() - started) * 1000),
            "error": str(exc)[:400],
            "hint": "OpenAI 兼容接口是 POST /v1/audio/speech；返回必须是 audio/*。",
        }
    return {
        "ok": bool(audio),
        "stage": "done",
        "latency_ms": int((time.time() - started) * 1000),
        "bytes": len(audio),
        "model": cfg["model"],
        "message": "配音接口连通，返回了真实音频",
    }


# --------------------------------------------------------------------- 视频


async def _probe_video(cfg: dict) -> dict:
    """绝不提交真实任务：查一个不存在的 task_id，用鉴权结果判断连通性。"""
    started = time.time()
    if cfg["provider"] in ("", "off"):
        return {
            "ok": False,
            "stage": "config",
            "error": "视频生成未启用",
            "hint": "启用后成片会优先使用视频片段；不启用则走静态图 + 运镜（零成本）。",
        }
    if not cfg["api_key"] or not cfg["base_url"]:
        return {
            "ok": False,
            "stage": "config",
            "error": "缺少 API Key 或接口地址",
            "hint": "京东云是 {base_url}/task/submit；万相(dashscope)是 {base_url}/services/aigc/video-generation/video-synthesis。",
        }
    if cfg["provider"] == "dashscope":
        url = f"{cfg['base_url']}/tasks/__longspan_probe__"
    else:
        url = f"{cfg['base_url']}/task/__longspan_probe__"
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            resp = await cli.get(url, headers={"Authorization": f"Bearer {cfg['api_key']}"})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "network", "error": str(exc)[:300], "hint": "检查网络或代理设置。"}

    body = resp.text[:300]
    latency = int((time.time() - started) * 1000)
    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "stage": "auth",
            "http_status": resp.status_code,
            "latency_ms": latency,
            "error": body,
            "hint": "鉴权失败：Key 不对，或这个 Key 没开通该模型服务。",
        }
    # 404 / 400 都说明"请求到达了业务层"，即鉴权通过
    return {
        "ok": True,
        "stage": "done",
        "http_status": resp.status_code,
        "latency_ms": latency,
        "model": cfg["model"],
        "message": "鉴权通过（本次未提交任何生成任务，不产生费用）",
        "detail": body,
    }
