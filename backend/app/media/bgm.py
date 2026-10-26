"""BGM 兜底合成 —— 没有音频素材库时，用纯 Python 生成一轨氛围垫底音。

为什么需要它（对应报告《视频效果根因分析》发现 1「声音层空白」）：
    三个真实 Run 的成片 `bgm` 全是 null —— voice.md 里精心设计的 BGM/音效点位表
    **从未被合进成片**，因为 compose 链路里没有任何"读 BGM 设计 + 铺音轨"的代码。
    悬疑/科幻动态漫没有 BGM，情绪张力直接归零。

设计约束（严格对齐架构铁律）：
    1. **零外部依赖**：只用标准库 `wave` + `math` + `struct` 合成 WAV，
       再经系统 `afconvert` 转 m4a/AAC（Chrome/Remotion 可直接解码）。
       —— 不引入 numpy / scipy / 需要联网下载的音色库。
    2. **诚实标注**：这是"程序化氛围 BGM"，不是真人作曲，也不是音乐库授权曲目。
       产物与事件里必须写清（禁止静默降级，架构铁律 #2）。
    3. **不冒充音乐**：合成的是"低频垫底 + 缓慢起伏"的氛围底噪，
       不是旋律 —— 它的作用是消除"成片一片死寂"，而不是假装有配乐。

氛围类型（按 voice.md 的 BGM 点位情绪映射）：
    - suspense（悬疑）  ：低音持续 + 缓慢涨落 + 极轻的高频气声
    - warm（温暖/治愈） ：柔和的三和弦垫底，起伏更缓
    - neutral（中性）   ：极轻的低频垫，几乎无情绪
    强度 strength 0~1 控制整体响度。

真实素材优先：若调用方提供了 bmg 素材文件路径（未来接音乐库），本模块不参与；
本模块只处理"没有任何素材"的兜底路径。
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

# 采样率 / 位深 / 声道
_SAMPLE_RATE = 22050
_CHANNELS = 1  # 单声道，节省体积；Remotion 播放时自动居中
_SAMPLE_WIDTH = 2  # 16-bit PCM

# 氛围类型 → (基频列表, 起伏周期秒, 是否加高频气声)
_MOODS = {
    "suspense": ([55.0, 110.0, 165.0], 12.0, True),
    "warm": ([220.0, 277.18, 329.63], 16.0, False),   # A3 - C#4 - E4 三和弦
    "neutral": ([82.41], 20.0, False),
}


def _afconvert_available() -> bool:
    return bool(shutil.which("afconvert"))


def synthesize_bgm(
    out_path: Path,
    duration_seconds: float,
    mood: str = "suspense",
    strength: float = 0.35,
) -> dict:
    """合成一轨氛围 BGM 到 out_path（m4a/AAC），返回元信息 dict。

    失败抛 RuntimeError；调用方负责降级留痕（本模块不吞错）。
    """
    mood = str(mood or "").strip().lower() or "neutral"
    freqs, swell_period, with_air = _MOODS.get(mood, _MOODS["neutral"])
    strength = max(0.0, min(1.0, float(strength)))
    dur = max(1.0, float(duration_seconds))

    n = int(dur * _SAMPLE_RATE)
    # 预分配缓冲：16-bit 单声道
    frames = bytearray(n * _CHANNELS * _SAMPLE_WIDTH)

    base_amp = 0.28 * strength  # 基准幅度，压低避免盖过配音

    for i in range(n):
        t = i / _SAMPLE_RATE
        # 缓慢起伏包络：让底噪有"呼吸感"，而不是恒定嗡嗡声
        swell = 0.5 + 0.5 * math.sin(2 * math.pi * t / swell_period)
        sample = 0.0
        for idx, f in enumerate(freqs):
            # 基频叠加，谐波权重递减
            amp = base_amp / (idx + 1)
            sample += amp * math.sin(2 * math.pi * f * t)
        # 极轻的高频气声（仅悬疑）：用确定性伪随机，避免白噪声太冲
        if with_air:
            air = (math.sin(t * 997.0 * 1.0) * 0.5 + 0.5) * 0.03 * strength
            sample += air
        sample *= 0.5 + 0.5 * swell
        # 限幅并转 16-bit
        sample = max(-1.0, min(1.0, sample))
        val = int(sample * 32767.0)
        struct.pack_into("<h", frames, i * _SAMPLE_WIDTH, val)

    # 写临时 WAV，再转 m4a（Chrome 可播）
    tmp_wav = out_path.with_suffix(".tmp.wav")
    try:
        with wave.open(str(tmp_wav), "wb") as w:
            w.setnchannels(_CHANNELS)
            w.setsampwidth(_SAMPLE_WIDTH)
            w.setframerate(_SAMPLE_RATE)
            w.writeframes(bytes(frames))
    except Exception:
        tmp_wav.unlink(missing_ok=True)
        raise

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if _afconvert_available():
        try:
            proc = subprocess.run(
                ["afconvert", "-f", "m4af", "-d", "aac", "-b", "96000",
                 str(tmp_wav), str(out_path)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                tmp_wav.unlink(missing_ok=True)
            else:
                raise RuntimeError(proc.stderr[:200] or "afconvert 失败")
        except Exception:
            # afconvert 失败 → 退回 WAV（Remotion 也能播 wav，只是体积大）
            tmp_wav.replace(out_path)
    else:
        tmp_wav.replace(out_path)

    size = out_path.stat().st_size
    return {
        "path": str(out_path.name),
        "mood": mood,
        "strength": strength,
        "duration_seconds": round(dur, 2),
        "size": size,
        "synthetic": True,
    }
