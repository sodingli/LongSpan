"""配音 Provider —— 让分镜"开口说话"。

真实服务：OpenAI 兼容 `POST /v1/audio/speech`（tts 系模型，返回 mp3 等音频字节）。

本地兜底：**macOS 内置神经语音合成（`say`）**。
这不是假音频 —— Tingting 等是真声学模型合成的中文人声，可直接听。
限制（要如实告知）：本机中文音色有限，角色区分靠语速与停顿，男声效果一般；
接真实 TTS 服务（面板里填 base_url / key / model）即可获得多音色。

音频格式统一转 m4a/AAC：`say` 一步直出，Chrome（Remotion 渲染内核）可播，
时长用系统 `afinfo` 读取 —— 合成器排时间轴必须有真实时长，不能猜。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("longspan.media.tts")

# 本地音色映射：角色性别/旁白 → macOS 语音名（当前机器实测定）
LOCAL_VOICES: dict[str, str] = {
    "female": "Tingting",   # 普通话女声
    "male": "Tingting",     # 本机无普通话男声，用同一音色降速区分（如实标注）
    "narrator": "Tingting",
}


def _local_say_available() -> bool:
    return bool(shutil.which("say")) and Path("/usr/bin/say").exists()


def audio_duration_seconds(path: str | Path) -> float:
    """用系统 afinfo 读音频真实时长（mp3/m4a/aiff/wav 都支持）。"""
    try:
        out = subprocess.run(
            ["afinfo", str(path)], capture_output=True, text=True, timeout=15
        )
        m = re.search(r"estimated duration:\s*([\d.]+)\s*sec", out.stdout)
        if m:
            return float(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


async def _request_real_speech(
    text: str,
    voice: str,
    speed: float,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    extra: dict | None = None,
) -> bytes:
    body: dict = {
        "model": model or settings.tts_model,
        "input": text,
        "voice": voice or settings.tts_voice,
        "response_format": "mp3",
        "speed": min(max(speed, 0.5), 2.0),
    }
    if extra:
        body.update(extra)
    async with httpx.AsyncClient(timeout=180) as cli:
        resp = await cli.post(
            (base_url or settings.tts_base_url).rstrip("/") + "/audio/speech",
            headers={"Authorization": f"Bearer {api_key or settings.tts_api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"TTS 接口返回 {resp.status_code}：{resp.text[:300]}")
    ct = resp.headers.get("content-type", "")
    if "audio" not in ct and "octet-stream" not in ct:
        raise RuntimeError(f"TTS 返回不是音频（{ct}）：{resp.text[:150]}")
    return resp.content


async def _local_say(
    text: str, out_path: Path, voice: str, rate: int
) -> None:
    """macOS say 直出 m4a/AAC（Chrome 可直接解码，无需 ffmpeg）。"""
    proc = await asyncio.create_subprocess_exec(
        "say",
        "-v", voice,
        "-r", str(rate),
        "-o", str(out_path),
        "--file-format=m4af",
        "--data-format=aac",
        text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"say 合成失败：{stderr.decode('utf-8', 'replace')[:300]}")


def _sanitize(text: str) -> str:
    """配音文本只保留可读字符：say 对某些符号会逐字念出来。"""
    text = re.sub(r"[*_`#>|]", "", text or "")
    return text.strip()


async def synthesize_speech(
    storage: Any,
    run_id: str,
    text: str,
    path: str,
    role: str = "female",
    rate_wpm: int = 190,
    *,
    voice_override: str | None = None,
    speed_override: float | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """合成一句配音。

    返回 {path, provider, voice, duration_seconds, is_fallback, fallback_reason?}。
    voice_override 来自「阶段级音色覆盖」或画布上给单句指定的音色；
    speed_override / params 覆盖全局语速与高级参数。
    """
    text = _sanitize(text)
    if not text:
        raise ValueError("配音文本不能为空")
    params = params or {}
    voice = str(voice_override or settings.tts_voice or "").strip()
    base_speed = float(speed_override if speed_override is not None else settings.tts_speed or 1.0)
    try:
        extra = json.loads(settings.tts_params_json) if (settings.tts_params_json or "").strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"配音高级参数不是合法 JSON：{exc}") from exc
    if extra and not isinstance(extra, dict):
        raise ValueError("配音高级参数必须是 JSON 对象")

    real_ready = settings.tts_provider != "local" and settings.tts_api_key and settings.tts_base_url
    if real_ready:
        try:
            speed = (rate_wpm / 190.0) * base_speed
            data = await _request_real_speech(text, voice, speed, extra=extra or None)
            p = re.sub(r"\.(m4a|mp3|wav|aac)$", ".mp3", path, flags=re.IGNORECASE)
            rel, size = storage.write_bytes(run_id, p, data)
            dur = audio_duration_seconds(storage.abs_path(run_id, rel))
            logger.info("真实 TTS 成功 run=%s path=%s dur=%.2fs", run_id, rel, dur)
            return {
                "path": rel, "provider": settings.tts_model, "voice": voice,
                "duration_seconds": round(dur, 2), "is_fallback": False, "size": size,
                "speed": round(speed, 3),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("真实 TTS 失败，降级为本地 say：%s", e)
            fallback_reason = str(e)[:300]
    else:
        fallback_reason = "未配置 TTS 服务（TTS_API_KEY 为空），使用本机语音合成"

    if not _local_say_available():
        raise RuntimeError("TTS 服务不可用，且本机没有 say 命令（非 macOS？），无法合成配音")

    local_voice = LOCAL_VOICES.get(role, LOCAL_VOICES["female"])
    # 男声没有独立音色：降速 15% 与女声区分，并如实标注
    adjusted_rate = int(rate_wpm * (0.85 if role == "male" else 1.0) * base_speed)
    p = re.sub(r"\.(mp3|wav|aac)$", ".m4a", path, flags=re.IGNORECASE)
    abs_out = storage.abs_path(run_id, p)
    abs_out.parent.mkdir(parents=True, exist_ok=True)
    await _local_say(text, abs_out, local_voice, adjusted_rate)
    dur = audio_duration_seconds(abs_out)
    rel, size = p, abs_out.stat().st_size
    return {
        "path": rel, "provider": "macos-say", "voice": local_voice,
        "duration_seconds": round(dur, 2), "is_fallback": True,
        "fallback_reason": fallback_reason + (
            "；本机无普通话男声，男角色为同音色降速模拟" if role == "male" else ""
        ),
        "size": size,
    }
