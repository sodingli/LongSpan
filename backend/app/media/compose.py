"""分镜合成器 —— 把分镜图 + 配音 + 字幕渲染成竖屏 mp4。

分工（关键设计）：
- **时间轴在这里排**（business：格时长对齐、配音起点）——因为这是合成业务的一部分；
- **渲染在 Remotion 工程里**（`backend/media/remotion/`）——它是纯渲染器，
  换技能/换风格不改这里，加新形态视频 = 加一个 Composition。

实测踩过的坑（都在代码里处理了）：
1. 本机 HTTP_PROXY 代理会让 Remotion 内嵌服务被 Chrome 代理掉 → 渲染进程必须剥离代理环境变量；
2. Remotion 默认要下载 Chrome Headless Shell → 用 `--browser-executable` 复用本机 Chrome；
3. ffmpeg 无需另装 —— `@remotion/compositor-*/ffmpeg` 随 npm 包分发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger("longspan.media.compose")

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
REMOTION_DIR = BACKEND_DIR / "media" / "remotion"

# node 的 bin 目录候选：默认走 PATH；需要指定版本时设 LONGSPAN_NODE_BIN
NODE_BIN_CANDIDATES = [d for d in [os.environ.get("LONGSPAN_NODE_BIN", "")] if d]
CHROME_CANDIDATES = [c for c in [os.environ.get("LONGSPAN_CHROME", "")] if c] + [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

VALID_MOTIONS = {"zoom-in", "zoom-out", "pan-left", "pan-right", "pan-up", "pan-down", "static"}

# 格间 crossfade 重叠（秒）：下一格的 from 比本格结尾提前这么多，
# 渲染端在本重叠窗里把下一格淡入到本格上（Panel 不再淡出，接力给下一格的淡入）。
# 同时也是片头卡→格 1、最后格→片尾卡的叠化窗。
TRANSITION_SECONDS = 0.5


def _node_bin() -> str:
    for d in NODE_BIN_CANDIDATES:
        npx = Path(d) / "npx"
        if npx.exists():
            return str(npx)
    found = shutil.which("npx")
    if not found:
        raise RuntimeError("找不到 npx，无法驱动 Remotion 合成")
    return found


def _chrome_bin() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def _clean_env() -> dict[str, str]:
    """净化渲染子进程的环境。

    - 剥离代理变量：Chrome 会把 localhost 渲染服务也交给代理，直接失败；
    - 剥离 NODE_OPTIONS：宿主环境会往所有 node 进程注入 fs 代理 shim，
      会让 Remotion 的 webpack 临时目录 mkdir 报 EEXIST（实测）。
    """
    env = dict(os.environ)
    for k in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        "NODE_OPTIONS", "NODE_PATH", "NODE_REQUIRE",
    ):
        env.pop(k, None)
    # 不只删代理，还要显式豁免 localhost：Chrome 对 127.0.0.1 的渲染服务
    # 一旦走了任何代理路径就会 "Visited http://localhost:3000 but got no response"（实测）。
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["no_proxy"] = "localhost,127.0.0.1,::1"
    env["CI"] = "1"
    return env


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """校验并补全合成清单：排时间轴、兜底缺失素材、统一字段。

    输入来自模型（可能不完美），所以这里必须把好关 —— 但**只补结构，不替模型做创意决定**。
    """
    panels_in = spec.get("panels") or []
    if not panels_in:
        raise ValueError("合成清单里没有 panels（至少要一格）")

    fps = int(spec.get("fps") or 30)
    width = int(spec.get("width") or 1080)
    height = int(spec.get("height") or 1920)
    intro = spec.get("intro") or None
    outro = spec.get("outro") or None
    transition = max(0.0, min(float(spec.get("transitionSeconds") or TRANSITION_SECONDS), 2.0))
    degraded: list[str] = []

    # 起点：片头卡让出大部分时长，但提前 transition 入画 —— 格 1 与片头卡叠化。
    t = max(0.0, float(intro["durationInSeconds"]) - transition) if intro and intro.get("durationInSeconds") else 0.0
    panels: list[dict[str, Any]] = []
    unknown_motions: list[str] = []
    frame_notes: list[str] = []
    for i, p in enumerate(panels_in):
        dur = max(1.0, float(p.get("durationInSeconds") or 3.0))
        src = str(p.get("src") or "").strip()
        motion = str(p.get("motion") or "zoom-in").lower()
        if motion not in VALID_MOTIONS:
            # 上游模型爱写复合值（实测 "zoom-in+dissolve" / "static+fadeout"），
            # 渲染器只认单值枚举。这里必须**改并上报** —— 静默退回 zoom-in
            # 会让"我明明要求了淡出"变成一句没人知道的谎（架构铁律 #2）。
            unknown_motions.append(f"格 {i + 1} 的 motion「{motion}」不在支持枚举内，已退回 zoom-in")
            motion = "zoom-in"
        is_ph = bool(p.get("isPlaceholder"))

        # 多帧：按给定顺序去重、去掉空值。首帧缺省用 src 补齐（老数据兼容）。
        raw_frames = p.get("frames") if isinstance(p.get("frames"), list) else []
        frames: list[str] = []
        for f in raw_frames:
            fs = str(f or "").strip()
            if fs and fs not in frames:
                frames.append(fs)
        if src and src not in frames:
            frames.insert(0, src)
        if not frames:
            frame_notes.append(f"格 {i + 1} 没有任何画面，已跳过")
            continue

        video = str(p.get("video") or "").strip()
        # fromSeconds 显式给出（工具侧已按 crossfade 排好）→ 原样信任；
        # 缺失才按顺序排（带重叠）。混用时以显式值为准，顺序游标只增不减。
        explicit_from = p.get("fromSeconds")
        if isinstance(explicit_from, (int, float)) and explicit_from >= 0:
            t = max(t, float(explicit_from))
        panels.append(
            {
                "src": src or frames[0],
                "frames": frames,
                "video": video or None,
                "fromSeconds": round(t, 3),
                "durationInSeconds": round(dur, 3),
                "motion": motion,
                "subtitle": (p.get("subtitle") or "").strip() or None,
                "speaker": (p.get("speaker") or "").strip() or None,
                "label": p.get("label") or f"格 {i + 1}",
                "isPlaceholder": is_ph,
            }
        )
        t += dur - transition

    if outro and outro.get("durationInSeconds"):
        # 片尾卡提前 transition 入画，与最后一格叠化
        t += float(outro["durationInSeconds"]) - transition

    audio_in = spec.get("audio") or []
    audio = [
        {
            "src": str(a.get("src") or "").strip(),
            "from": round(float(a.get("from") or 0.0), 3),
            "volume": min(max(float(a.get("volume") or 1.0), 0.0), 1.5),
            "durationInSeconds": a.get("durationInSeconds"),
            "speaker": a.get("speaker"),
        }
        for a in audio_in
        if a.get("src")
    ]

    return {
        "title": str(spec.get("title") or "Longspan")[:60],
        "fps": fps,
        "width": width,
        "height": height,
        "transitionSeconds": transition,
        "durationInSeconds": round(t, 3),
        "intro": intro,
        "outro": outro,
        "panels": panels,
        "audio": audio,
        "bgm": spec.get("bgm") or None,
        "showWatermark": bool(spec.get("showWatermark", True)),
        "degraded": degraded + unknown_motions + frame_notes,
    }


def _missing_assets(spec: dict[str, Any], workspace: Path) -> list[str]:
    """找出清单里引用了但不存在的原件（模型可能编了不存在的路径）。"""
    missing = []
    for p in spec["panels"]:
        for f in p.get("frames") or []:
            if f and not (workspace / f).exists():
                missing.append(f)
        if p.get("video") and not (workspace / p["video"]).exists():
            missing.append(p["video"])
    for a in spec["audio"]:
        if a["src"] and not (workspace / a["src"]).exists():
            missing.append(a["src"])
    return missing


async def render_video(
    run_id: str,
    workspace: Path,
    spec: dict[str, Any],
    out_rel: str = "video/final.mp4",
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """渲染成片。workspace 即本次任务的产物目录（同时作为 Remotion 的 public dir）。

    返回 {path, duration_seconds, frames, degraded, missing→degraded 里}；
    失败抛 RuntimeError，错误里带 Remotion 的最后几行输出。
    """
    if not REMOTION_DIR.exists():
        raise RuntimeError(f"Remotion 工程不存在：{REMOTION_DIR}")

    spec = normalize_spec(spec)

    # 素材缺失不渲染假片：把缺失项从清单里剔除并标注（宁缺毋假）
    missing = _missing_assets(spec, workspace)
    if missing:
        kept_panels: list[dict[str, Any]] = []
        for p in spec["panels"]:
            frames = [f for f in (p.get("frames") or []) if (workspace / f).exists()]
            video = p.get("video") if (p.get("video") and (workspace / p["video"]).exists()) else None
            if not frames and not video:
                continue
            p["frames"] = frames or ([p["src"]] if p.get("src") and (workspace / p["src"]).exists() else [])
            p["src"] = p["frames"][0] if p["frames"] else p.get("src")
            p["video"] = video
            kept_panels.append(p)
        spec["panels"] = kept_panels
        spec["audio"] = [a for a in spec["audio"] if (workspace / a["src"]).exists()]
        if not spec["panels"]:
            raise RuntimeError(f"分镜图全部缺失，无法合成：{missing}")
        spec["degraded"].append(f"以下素材缺失已剔除：{missing}")

    # 重算时长（剔除后可能变短）；片尾卡与最后一格叠化，要减去重叠窗
    end = spec["panels"][-1]["fromSeconds"] + spec["panels"][-1]["durationInSeconds"]
    if spec.get("outro"):
        end += float(spec["outro"]["durationInSeconds"]) - spec.get("transitionSeconds", TRANSITION_SECONDS)
    spec["durationInSeconds"] = round(max(end, 1.0), 3)

    props_path = (workspace / "video" / "render_props.json").resolve()
    props_path.parent.mkdir(parents=True, exist_ok=True)
    props_path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    out_abs = workspace / out_rel
    out_abs.parent.mkdir(parents=True, exist_ok=True)
    # 先渲到临时文件，渲完再原子换名 —— final.mp4 只可能指向一份完整视频，
    # 不会出现"渲染中途失败、却把半成品留在成品路径上"的情况。
    tmp_abs = out_abs.with_name(out_abs.stem + ".rendering" + out_abs.suffix)

    frames = int(spec["durationInSeconds"] * spec["fps"])
    first_conc = max(1, int(settings.render_concurrency))
    # 并发降级链：本机 macOS < 15，多 tab 时部分 Chrome tab 会连不上本地渲染服务。
    # 先在配置并发上试，失败就降到 1 再试一次（只是跑得更保守，产物零降级）。
    concs = [first_conc] + ([1] if first_conc > 1 else [])
    chrome = _chrome_bin()
    last_tail = ""

    for i, conc in enumerate(concs):
        cmd = [
            _node_bin(), "remotion", "render", "src/index.ts", "ComicDrama", str(tmp_abs),
            f"--props={props_path}",
            f"--public-dir={workspace}",
            "--log=error",
            "--timeout=120000",
            f"--concurrency={conc}",
        ]
        if chrome:
            cmd.append(f"--browser-executable={chrome}")

        logger.info("开始合成 run=%s 帧=%s 并发=%s（第 %s 次）输出=%s",
                    run_id, frames, conc, i + 1, out_abs)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REMOTION_DIR),
            env=_clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as e:
            proc.kill()
            tmp_abs.unlink(missing_ok=True)
            raise RuntimeError(f"合成超时（>{timeout_seconds}s），已终止") from e

        last_tail = "\n".join(stdout.decode("utf-8", "replace").strip().splitlines()[-12:])
        if proc.returncode == 0 and tmp_abs.exists() and tmp_abs.stat().st_size > 0:
            break

        tmp_abs.unlink(missing_ok=True)
        if i + 1 < len(concs):
            logger.warning("并发=%s 渲染失败，降到 %s 重试：%s", conc, concs[i + 1], last_tail[-200:])
            continue
        raise RuntimeError(f"Remotion 渲染失败（exit={proc.returncode}，并发={conc}）：\n{last_tail}")

    final_conc = concs[min(i, len(concs) - 1)]
    if final_conc != first_conc:
        # 不静默：并发从配置值退到 1 这件事必须出现在事件流与成片说明里
        spec["degraded"].append(
            f"渲染并发从 {first_conc} 自动降到 {final_conc}（本机 Chrome 多 tab 连不上本地渲染服务），成片本身无降级"
        )

    tmp_abs.replace(out_abs)

    size = out_abs.stat().st_size
    logger.info("合成完成 run=%s out=%s size=%s 并发=%s", run_id, out_rel, size, final_conc)
    return {
        "path": out_rel,
        "duration_seconds": spec["durationInSeconds"],
        "frames": frames,
        "size": size,
        "degraded": spec["degraded"],
        "panels_count": len(spec["panels"]),
        "audio_count": len(spec["audio"]),
        "concurrency": final_conc,
        "retried": final_conc != first_conc,
    }
