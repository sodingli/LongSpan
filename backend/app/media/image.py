"""生图 Provider —— 分镜画面的"手"。

真实服务：OpenAI 兼容 `POST /v1/images/generations`
（dall-e-3 / gpt-image 系 / 各中转站的 image 模型均可，吃 b64_json 与 url 两种返回）。

本地兜底：**程序化电影感分镜卡**（SVG 矢量图）。
不是随机涂鸦 —— 按 prompt 哈希确定性地选色调与构图，同一格重跑结果一致；
画面上明确标注「占位图」，配合事件流告知（禁止静默降级，架构铁律 #2）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("longspan.media.image")

# 几套电影感调色板：天空渐变（上→下）、剪影色、点缀色
PALETTES: list[dict[str, str]] = [
    {"sky_top": "#ff9a5a", "sky_mid": "#e2653e", "sky_bot": "#2b1b3d", "silhouette": "#191026", "accent": "#ffd166"},  # 黄昏都市
    {"sky_top": "#141d4d", "sky_mid": "#0d1026", "sky_bot": "#05060f", "silhouette": "#02030a", "accent": "#5ce8ff"},  # 夜雨霓虹
    {"sky_top": "#dfeeff", "sky_mid": "#bcd8ee", "sky_bot": "#8fb3cf", "silhouette": "#33475c", "accent": "#ffffff"},  # 晨雾山野
    {"sky_top": "#d97b41", "sky_mid": "#a34d2c", "sky_bot": "#3d1d14", "silhouette": "#200d08", "accent": "#f2c17d"},  # 废土橙尘
    {"sky_top": "#aee3f5", "sky_mid": "#d8f0f2", "sky_bot": "#fdf6e3", "silhouette": "#5b7d8c", "accent": "#ff8a8a"},  # 日系清新
]

SIZE_TABLE = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1024, 1024),
}

# ---------------------------------------------------------------- 风格锚校验（FR-04 补充）
# 实测教训（2026-10-20 高山流水）：同一任务 6 格里 2 格提示词漏了「角色风格化强化句」，
# 生出的首帧往半写实漂（暖橙色调、写实脸、发型变形），视频跟着首帧跑，成片风格分裂。
# 所以在生图入口做字符串级校验：缺哪层锚补哪层，并留痕（铁律 #2，绝不静默）。
#
# 2026-11-02 事故复盘（用户反馈「成片 0:24 之后风格变了、人物也不一样了」）：
# 旧实现把 spec.json 的 style_anchor **改写**了（semi-realistic → semi-stylized anime），
# 又在最前面硬编码注入 `flat cel shading on skin` + `clean drawn lineart` ——
# 而同一段 prompt 里 spec 锚写着 `no flat cel-shading, no line-art comic style`。
# 一段提示词里两套互相打架的画风指令，模型只能按「哪条更具体」来选：
# 全景格（脸小）听厚涂条款 → 前 5 格是厚涂；近景/特写（脸大）听「anime face with
# large expressive eyes」→ 第 6 格起全变日系平涂。0:24 正好是格 5/6 的分界。
#
# 结论（现在的铁律）：**spec.json 的 style_anchor 是 C2 冻结的画风契约，生图层只准
# 原文复用，不准改写、不准加任何指定画种的条款**。生图层只负责一条与画种无关的
# 审核底线：这是画出来的，不是照片/真人。画风本身漂了，是 C2 写错了锚，回 C2 改。

COMIC_STYLE_SKILLS = {"comic-drama"}

# 审核底线条款：只声明「画出来的、不是照片」，**不含任何画种词**（平涂/线稿/厚涂/
# 赛璐璐…）。画种由 spec.json 的 style_anchor 决定 —— 这两件事必须分开，
# 否则生图层就又在替用户定画风了。
AUDIT_SAFETY_CLAUSE = (
    "Stylized illustration, clearly hand-drawn artwork rather than a photograph: "
    "NOT a photograph, NOT photorealistic, NOT live-action, NOT a 3D render, "
    "no realistic skin pores, no photographic depth of field"
)
_AUDIT_KEYWORD = "not photorealistic"

# 兼容旧名字：外部（tools.py / canvas.py）只 import COMIC_STYLE_SKILLS，
# 但保留别名可避免第三方脚本按旧名引用时直接 ImportError。
COMIC_STYLE_CLAUSES: list[tuple[str, str, str]] = [
    ("审核底线条款", _AUDIT_KEYWORD, AUDIT_SAFETY_CLAUSE),
]

# 正文里**只中和真正的"照片级写实"断言**（2026-11-02 收窄）：
# 旧表把 oil painting / brushstrokes / realistic illustration 这些**画种词**也一起
# 改写成 anime —— 那等于让生图层否决 C2 的画风契约，正是本次事故的直接原因。
# 现在只保留「照片/真人」语义的词，且**永不作用于 spec 锚原文**（见 _neutralize_body）。
_REALISM_PATTERNS = [
    (re.compile(r"(?<!no )(?<!not )(?<!non-)\bhyper[- ]?realistic\b", re.I), "stylized illustration"),
    (re.compile(r"(?<!no )(?<!not )(?<!non-)\bphotorealistic\b", re.I), "stylized illustration"),
    (re.compile(r"(?<!no )(?<!not )(?<!non-)\bphoto[- ]?realistic\b", re.I), "stylized illustration"),
    (re.compile(r"(?<!no )(?<!not )(?<!non-)\brealistic\s+skin\s+(?:texture|pores)\b", re.I), "simplified skin rendering"),
]

# 判断「本格是有意的局部特写」：与 harness.consistency._FRAMING_DECL_RE 同源判据的轻量版
# （media 层不反向依赖 harness，避免循环导入；两边都覆盖"no face/only … fills the frame"）。
_HEADLESS_HINT_RE = re.compile(
    r"(no\s+(?:face|head|torso|body|person)\b"
    r"|only\s+[^.,;]{0,80}?\bfills?\s+the\s+frame"
    r"|headless\s+composition"
    r"|不(?:出现|入画)(?:头部|面部|脸|躯干))",
    re.IGNORECASE,
)


def is_headless_framing(prompt: str) -> bool:
    """该提示词是否显式要求「画面里不出现脸/头」（有意的局部特写）。"""
    return bool(_HEADLESS_HINT_RE.search(prompt or ""))


def _neutralize_body(text: str) -> tuple[str, list[str]]:
    """中和**正文**里的照片级写实断言。返回 (新文本, 留痕列表)。

    调用方必须先把 spec 锚原文摘出去 —— 规格锚是契约，一个字符都不许改。
    """
    notes: list[str] = []
    for pat, repl in _REALISM_PATTERNS:
        text, n = pat.subn(repl, text)
        if n:
            notes.append(f"中和正文写实倾向描述 {n} 处（→ {repl}）")
    return text, notes


def _split_spec_anchor(prompt: str, spec: str) -> tuple[str, str]:
    """把 prompt 拆成 (规格锚原文, 其余正文)；规格锚不在时返回 ("", prompt)。"""
    if not spec:
        return "", prompt
    idx = prompt.lower().find(spec.lower())
    if idx < 0:
        return "", prompt
    return prompt[idx:idx + len(spec)], prompt[:idx] + prompt[idx + len(spec):]


def ensure_style_anchor(
    prompt: str, style_anchor: str | None = None
) -> tuple[str, str | None]:
    """漫剧类生图提示词的风格锚校验：规格锚原文必须在最前，缺了补上并留痕。

    style_anchor（2026-10-29 用户要求「生图提示词缺少风格锚，要严格按照 spec.json 里的
    风格来确定」）：C2 在 spec.json 里冻结的**规格风格锚**，是"这一集是什么画风"的
    唯一来源。

    2026-11-02 修正（事故：成片 0:24 起画风突变）：
      - **规格锚原文一字不改**。旧实现会把 semi-realistic 改写成 semi-stylized anime，
        再前置一段硬编码的平涂条款，与规格锚里的 `no flat cel-shading` 直接对冲；
      - 规格锚**统一提到最前**（模型写在中间也搬过来），保证 12 格拿到同一个前缀 ——
        前缀不一致本身就是风格漂移的来源（实测第 10 格走了无脸分支，前缀与其余 11 格不同）；
      - 只补一条**与画种无关**的审核底线（AUDIT_SAFETY_CLAUSE），不再指定平涂/线稿；
      - 正文里的照片级写实断言仍会中和，但**只在规格锚之外**做，绝不碰契约原文。

    返回 (修正后的提示词, 留痕说明)。提示词本来就齐 → 原样返回 (prompt, None)。
    """
    notes: list[str] = []
    spec = (style_anchor or "").strip()
    text = prompt or ""

    found, body = _split_spec_anchor(text, spec)
    body, body_notes = _neutralize_body(body)
    notes.extend(body_notes)
    if spec and not found:
        notes.append("生图提示词缺少规格风格锚（spec.json style_anchor），已在开头补齐")
        found = spec
    elif found and not text.startswith(found):
        notes.append("规格风格锚不在提示词开头，已统一提到最前（保证逐格前缀一致）")
    # 前缀顺序：规格锚 → 审核底线 → 正文。
    # 规格锚必须排第一（它是画风契约，也是逐格前缀一致的基准）；
    # 审核底线紧跟在它后面 —— 旧实现把审核条款前置到规格锚之前，
    # 等于又让「生图层自己写的条款」压过 C2 的画风契约。
    prefix = [found] if found else []
    if _AUDIT_KEYWORD not in f"{found} {body}".lower():
        prefix.append(AUDIT_SAFETY_CLAUSE)
        notes.append("生图提示词缺少审核底线条款（非照片/非真人），已自动补齐")
    parts = prefix + ([body.strip()] if body.strip() else [])
    text = ". ".join(p for p in parts if p)

    if not notes:
        return prompt, None
    return text, "；".join(notes)


def attach_note(out: dict[str, Any], note: str) -> None:
    """把补充说明挂到生图结果的 params_note 上（已有就追加，不覆盖）。"""
    if not note:
        return
    prev = str(out.get("params_note") or "").strip()
    out["params_note"] = f"{prev}；{note}" if prev else note


def _pick_palette(prompt: str) -> dict[str, str]:
    """按 prompt 确定性选调色板：同一格重跑结果一致（可复现是长任务的底线）。"""
    h = int(hashlib.md5(prompt.encode("utf-8")).hexdigest(), 16)
    return PALETTES[h % len(PALETTES)]


def _skyline(seed: int, w: int, horizon: int, color: str) -> str:
    """城市剪影：伪随机楼群（确定性）。"""
    rng = hashlib.md5(str(seed).encode()).digest()
    parts, x = [], int(w * 0.02)
    i = 0
    while x < w * 0.98:
        bw = 60 + rng[i % len(rng)] * 3
        bh = 180 + rng[(i + 3) % len(rng)] * 9
        x2 = min(x + bw, w)
        parts.append(
            f'<rect x="{x:.0f}" y="{horizon - bh:.0f}" width="{x2 - x:.0f}" height="{bh:.0f}" fill="{color}"/>'
        )
        # 点几个窗灯
        for k in range(3):
            wx = x + 12 + (rng[(i + k) % len(rng)] % max(1, int(bw) - 24))
            wy = horizon - bh + 30 + (rng[(i + k + 1) % len(rng)] % max(1, int(bh) - 60))
            parts.append(
                f'<rect x="{wx:.0f}" y="{wy:.0f}" width="9" height="13" fill="{PALETTES[0]["accent"]}" opacity="0.55"/>'
            )
        x = x2 + 8
        i += 1
    return "".join(parts)


def _mountains(seed: int, w: int, horizon: int, color: str) -> str:
    rng = hashlib.md5(str(seed).encode()).digest()
    n = 5
    step = w / n
    pts = [f"0,{horizon}"]
    for i in range(n + 1):
        peak = horizon - 220 - rng[i % len(rng)] * 4
        pts.append(f"{i * step:.0f},{peak:.0f}")
    pts.append(f"{w},{horizon}")
    return f'<polygon points="{" ".join(pts)}" fill="{color}"/>'


def _figure(cx: int, ground: int, scale: float, color: str, accent: str) -> str:
    """人物剪影：极简但有"存在感"（头 + 肩 + 身），加一点轮廓光。"""
    s = scale
    return f"""
  <g transform="translate({cx},{ground}) scale({s})">
    <circle cx="0" cy="-320" r="62" fill="{color}"/>
    <circle cx="0" cy="-320" r="66" fill="none" stroke="{accent}" stroke-width="4" opacity="0.5"/>
    <path d="M -95,-250 Q 0,-300 95,-250 L 70,-40 Q 0,-70 -70,-40 Z" fill="{color}"/>
    <path d="M -70,-40 Q 0,-70 70,-40 L 46,220 Q 0,200 -46,220 Z" fill="{color}"/>
  </g>"""


def _wrap_text(text: str, per_line: int = 16, max_lines: int = 3) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    lines = [text[i : i + per_line] for i in range(0, len(text), per_line)]
    return lines[:max_lines]


def storyboard_card_svg(prompt: str, ratio: str = "9:16") -> str:
    """程序化分镜卡：有构图、有光、有场景描述 —— 兜底但认真兜。"""
    w, h = SIZE_TABLE.get(ratio, SIZE_TABLE["9:16"])
    p = _pick_palette(prompt)
    seed = int(hashlib.md5((prompt + ratio).encode()).hexdigest()[:8], 16)
    horizon = int(h * 0.62)
    is_city = seed % 2 == 0

    layers = (
        _skyline(seed, w, horizon, p["silhouette"])
        if is_city
        else _mountains(seed, w, horizon, p["silhouette"])
    )
    sun_x = int(w * (0.2 + (seed % 60) / 100))
    lines = _wrap_text(prompt)
    text_y = int(h * 0.88)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{p["sky_top"]}"/>
      <stop offset="55%" stop-color="{p["sky_mid"]}"/>
      <stop offset="100%" stop-color="{p["sky_bot"]}"/>
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="{p["accent"]}" stop-opacity="0.85"/>
      <stop offset="100%" stop-color="{p["accent"]}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="{w}" height="{h}" fill="url(#sky)"/>
  <circle cx="{sun_x}" cy="{int(h * 0.34)}" r="{int(w * 0.16)}" fill="url(#glow)"/>
  <circle cx="{sun_x}" cy="{int(h * 0.34)}" r="{int(w * 0.055)}" fill="{p["accent"]}" opacity="0.9"/>
  {layers}
  <rect x="0" y="{horizon}" width="{w}" height="{h - horizon}" fill="{p["silhouette"]}"/>
  {_figure(int(w * 0.5), int(h * 0.78), w / 1080, p["silhouette"], p["accent"])}
  <rect width="{w}" height="{h}" fill="none" stroke="{p["accent"]}" stroke-opacity="0.25" stroke-width="6"/>
  <g font-family='"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif' text-anchor="middle">
    {''.join(f'<text x="{w/2}" y="{text_y + i * 54}" fill="rgba(255,255,255,0.92)" font-size="40">{_esc(l)}</text>' for i, l in enumerate(lines))}
    <text x="{w/2}" y="{h - 70}" fill="rgba(255,214,120,0.85)" font-size="30">占位图 · 未接真实生图 · 构图 {"城市" if is_city else "山野"}</text>
  </g>
</svg>"""
    return svg


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 参考图未生效时的统一措辞：generate_image 靠它判断「参考图到底有没有被用上」，
# 不能只看通道能力位 —— 能力位说支持、服务端不认 edits，同样是没生效。
_REF_UNUSED_NOTE = "参考图未生效"


async def _decode_image_payload(data: dict) -> tuple[bytes, str]:
    """解析 OpenAI 兼容生图返回：b64_json 或 url 两种形状。"""
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), "png"
    if item.get("url"):
        async with httpx.AsyncClient(timeout=120) as cli:
            img = await cli.get(item["url"])
        if img.status_code >= 400:
            raise RuntimeError(f"下载生图结果失败：HTTP {img.status_code}")
        ct = img.headers.get("content-type", "")
        ext = "png" if "png" in ct else ("jpg" if ("jpeg" in ct or "jpg" in ct) else "png")
        return img.content, ext
    raise RuntimeError(f"生图返回里既没有 b64_json 也没有 url：{str(data)[:200]}")


async def _request_openai_edit(
    prompt: str,
    model: str | None,
    base: str,
    key: str,
    size: str,
    extra: dict | None,
    subject_refs: list[str],
) -> tuple[bytes, str]:
    """走 OpenAI 兼容 `/images/edits`（图生图），把定妆图当参考图送进去。

    为什么必须走这个端点（2026-10-29 实测）：
    主通道是 `openai-compat`（gpt-image-2），`/images/generations` 只吃文字，
    参考图能力位是 False —— 于是「用定妆图锁脸」在配置层面就是空转：
    定妆图生成得再准，逐格生图也从来没看过它一眼。

    报文形状是**实测**出来的，不要按 OpenAI 官方文档想当然（三个坑都踩过）：
      1. 不是 multipart —— 京东云网关会把 multipart 当 JSON 解析，
         报 `invalid character '-' in numeric literal`；
      2. `image` 不是字符串 —— 传字符串报
         `cannot unmarshal string into Go struct field GetImageEditRequest.Image of type []string`；
      3. 正确形状是 JSON + `"image": ["data:image/png;base64,..."]`（数组），
         实测 gpt-image-2 返回 b64_json，约 30 秒。

    2026-11-02：`image` 本来就是数组，多角色同框格可以一次送多张定妆图
    （旧实现只送 1 张 → 同框格里的配角没有参考，每格重画一张脸）。
    """
    body: dict = {
        "model": model or settings.image_model,
        "prompt": prompt,
        "image": list(subject_refs),
        "n": 1,
        "size": size,
    }
    if extra:
        body.update(extra)
    async with httpx.AsyncClient(timeout=240) as cli:
        resp = await cli.post(
            base + "/images/edits",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"图生图接口返回 {resp.status_code}：{resp.text[:300]}")
    return await _decode_image_payload(resp.json())


async def _request_real_image(
    prompt: str,
    ratio: str,
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    size: str | None = None,
    negative_prompt: str = "",
    seed: int = 0,
    extra: dict | None = None,
    subject_refs: list[str] | None = None,
) -> tuple[bytes, str, list[str]]:
    """调 OpenAI 兼容生图接口，返回 (图片字节, 扩展名, 未能生效的参数说明)。

    三个可选参数（size / negative_prompt / seed）不是所有服务商都支持。
    **不支持时要如实回传"没生效"**，而不是照发了事 —— 用户以为加了负向提示词、
    实际没加，属于典型的静默劣化。

    subject_refs 非空时先走 `/images/edits`（图生图，定妆图当参考图，可多张）；
    该端点不可用（404/405/400…）才退回纯文生图，并在返回值里写明"参考图未生效"。
    """
    ignored: list[str] = []
    if ratio == "9:16":
        default_size = "1024x1792"
    elif ratio == "16:9":
        default_size = "1792x1024"
    else:
        default_size = "1024x1024"
    final_size = (size or "").strip() or default_size
    base = (base_url or settings.image_base_url).rstrip("/")
    key = api_key or settings.image_api_key

    if subject_refs:
        try:
            data, ext = await _request_openai_edit(
                prompt, model, base, key, final_size, extra, subject_refs
            )
            return data, ext, ignored
        except Exception as exc:  # noqa: BLE001 —— 端点不支持就退回文生图，但必须留痕
            ignored.append(f"{_REF_UNUSED_NOTE}：{str(exc)[:200]}（已退化为纯文生图）")

    body: dict = {
        "model": model or settings.image_model,
        "prompt": prompt,
        "size": final_size,
        "n": 1,
        "response_format": "b64_json",
    }
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
        ignored.append("negative_prompt（若服务商不认该字段会被忽略）")
    if seed:
        body["seed"] = int(seed)
        ignored.append("seed（若服务商不认该字段会被忽略）")
    if extra:
        body.update(extra)
    async with httpx.AsyncClient(timeout=180) as cli:
        resp = await cli.post(
            base + "/images/generations",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"生图接口返回 {resp.status_code}：{resp.text[:300]}")
    data, ext = await _decode_image_payload(resp.json())
    return data, ext, ignored


# 京东云 image-01（MiniMax）支持的宽高比（超出则回退 9:16，不赌）
_JD_RATIOS = {"1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"}


async def _request_jdcloud_image(
    prompt: str,
    ratio: str,
    model: str | None = None,
    service: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    subject_reference: str | None = None,
    seed: int = 0,
    extra: dict | None = None,
    extra_refs: int = 0,
) -> tuple[bytes, str, list[str]]:
    """京东云 modelservice「预置服务」生图，返回 (图片字节, 扩展名, 参数说明)。

    与 OpenAI 兼容协议的三点差异（实测确认，勿混用）：
    1. 端点是 POST {base}/images/{预置服务标识}/generations —— 服务标识在京东云控制台创建，
       不在 /v1/models 列表里（该网关 /v1/models 返回 Account invalid）；
    2. 比例用 aspect_ratio（9:16 等），不是 OpenAI 的 size；
    3. 同步返回 data.image_urls[]（带签名的临时 URL，须立即下载），不是 b64_json。
    实测模型 image-01（MiniMax 海螺生图）出 720x1280 竖屏图，数秒级返回。

    人物一致性（FR-04）：image-01 支持 subject_reference（人物主体参考），
    传单人人脸照片做图生图，实现跨格人脸一致。subject_reference 是
    [{type:"character", image_file:"<data URL 或公网 URL>"}]，当前仅支持 1 个元素。
    不传 = 纯文生图（沿用旧行为）。
    """
    service = (service or settings.image_service or "").strip()
    if not service:
        raise RuntimeError("京东云生图缺少预置服务标识（LONGSPAN_IMAGE_SERVICE 为空）")
    notes: list[str] = []
    body: dict = {
        "model": (model or settings.image_model or "image-01"),
        "prompt": prompt[:1500],  # 该接口 prompt 上限 1500 字符
        "aspect_ratio": ratio if ratio in _JD_RATIOS else "9:16",
        "response_format": "url",
        "n": 1,  # 必填，取值 [1,9]，缺省会报「n 取值范围 [1,9]」
    }
    if subject_reference:
        body["subject_reference"] = [{"type": "character", "image_file": subject_reference}]
        notes.append("已携带人物参考图（subject_reference）")
    if extra_refs:
        notes.append(
            f"该通道 subject_reference 仅支持 1 张，同框格其余 {extra_refs} 张定妆图未送入"
            "（多角色一致性由提示词锚点承担）"
        )
    if seed:
        body["seed"] = seed
        notes.append(f"已固定随机种子 seed={seed}")
    if extra:
        body.update(extra)
        notes.append(f"已合并高级参数：{', '.join(sorted(extra))}")
    async with httpx.AsyncClient(timeout=240) as cli:
        resp = await cli.post(
            (base_url or settings.image_base_url).rstrip("/") + f"/images/{service}/generations",
            headers={"Authorization": f"Bearer {api_key or settings.image_api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"京东云生图返回 {resp.status_code}：{resp.text[:300]}")
    urls = ((resp.json().get("data") or {}).get("image_urls")) or []
    if not urls:
        raise RuntimeError(f"京东云生图返回里没有 image_urls：{resp.text[:200]}")
    async with httpx.AsyncClient(timeout=120) as cli:
        img = await cli.get(urls[0])
    if img.status_code >= 400:
        raise RuntimeError(f"下载生图结果失败：HTTP {img.status_code}")
    ct = img.headers.get("content-type", "")
    ext = "png" if "png" in ct else ("jpg" if ("jpeg" in ct or "jpg" in ct) else "jpg")
    return img.content, ext, notes


# 万相 text2image 支持的尺寸由「尺寸组」限定（官方文档：纵横比 1:1 / 16:9 / 9:16 三组），
# 这里按分镜比例映射到最接近的合法尺寸；用户显式填了 size 就原样透传（x 或 * 分隔都接受）
_WAN_SIZES = {
    "9:16": "720*1280",
    "16:9": "1280*720",
    "1:1": "1024*1024",
}

# wan2.2/2.5 单边像素上限 1440、总像素上限约 200 万（官方文档），拦住明显超限的尺寸
_WAN_MAX_SIDE = 1440


def _wan_size(size: str, ratio: str) -> str:
    """把「比例或显式尺寸」归一成万相认的 W*H 字符串；非法尺寸直接报错而不是赌服务端。"""
    raw = (size or "").strip()
    if raw:
        w, _, h = raw.lower().replace("×", "*").replace("x", "*").partition("*")
        try:
            w_i, h_i = int(w), int(h)
        except ValueError as exc:
            raise ValueError(f"生图尺寸格式不合法：{raw!r}（应为 720*1280 这类 W*H）") from exc
        if not (512 <= w_i <= _WAN_MAX_SIDE and 512 <= h_i <= _WAN_MAX_SIDE):
            raise ValueError(f"生图尺寸 {w_i}*{h_i} 超出万相允许范围（单边 512~{_WAN_MAX_SIDE}）")
        return f"{w_i}*{h_i}"
    return _WAN_SIZES.get(ratio, "720*1280")


async def _request_dashscope_image(
    prompt: str,
    ratio: str,
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    size: str | None = None,
    negative_prompt: str = "",
    seed: int = 0,
    extra: dict | None = None,
    poll_timeout: float = 240.0,
) -> tuple[bytes, str, list[str]]:
    """万相（通义）文生图，异步任务制：提交 → 轮询 → 下载。返回 (图片字节, 扩展名, 参数说明)。

    与同步接口不同，这是 DashScope 协议：
      POST {base}/services/aigc/text2image/image-synthesis（头 X-DashScope-Async: enable）
      GET  {base}/tasks/{task_id}                          （轮询，SUCCEEDED 才有 results[].url）
    已实测（2026-10-22，万相中转）：wan2.2-t2i-plus / wan2.2-t2i-flash /
    wanx2.1-t2i-plus / wanx2.1-t2i-turbo 均可出图，数秒到十几秒级；
    wan2.5 系列与 OpenAI 兼容 /images/generations 在该网关不可用。

    negative_prompt / seed 走 parameters，万相明确支持 —— 不像 image-01 那样只能"如实告知未生效"。
    """
    base = (base_url or settings.image_base_url).rstrip("/")
    key = api_key or settings.image_api_key
    notes: list[str] = []
    body: dict = {
        "model": (model or settings.image_model or "wan2.2-t2i-plus"),
        "input": {"prompt": prompt[:2000]},
        "parameters": {"size": _wan_size(size, ratio), "n": 1},
    }
    if negative_prompt:
        body["parameters"]["negative_prompt"] = negative_prompt
        notes.append("已携带负向提示词（万相原生支持）")
    if seed:
        body["parameters"]["seed"] = int(seed)
        notes.append(f"已固定随机种子 seed={seed}")
    if extra:
        body["parameters"].update(extra)
        notes.append(f"已合并高级参数：{', '.join(sorted(extra))}")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    async with httpx.AsyncClient(timeout=120) as cli:
        resp = await cli.post(base + "/services/aigc/text2image/image-synthesis", headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"万相生图提交失败 {resp.status_code}：{resp.text[:300]}")
        task_id = ((resp.json().get("output") or {}).get("task_id")) or ""
        if not task_id:
            raise RuntimeError(f"万相生图提交返回里没有 task_id：{resp.text[:200]}")
        # 轮询：PENDING/RUNNING 都要等；FAILED/UNKNOWN 才是真失败。超时如实报错。
        import time as _time

        deadline = _time.time() + poll_timeout
        while True:
            await asyncio.sleep(2.5)
            poll = await cli.get(f"{base}/tasks/{task_id}", headers={"Authorization": f"Bearer {key}"})
            if poll.status_code >= 400:
                raise RuntimeError(f"万相任务查询失败 {poll.status_code}：{poll.text[:300]}")
            out = (poll.json().get("output") or {})
            status = (out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                url = ((out.get("results") or [{}])[0]).get("url") or ""
                if not url:
                    raise RuntimeError(f"万相任务成功但返回里没有图片 URL：{poll.text[:200]}")
                break
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise RuntimeError(f"万相生图任务失败（{status}）：{out.get('message') or out.get('code') or poll.text[:200]}")
            if _time.time() > deadline:
                raise RuntimeError(f"万相生图任务超时（>{poll_timeout:.0f}s，task={task_id} 最后状态 {status}）")
        img = await cli.get(url)
    if img.status_code >= 400:
        raise RuntimeError(f"下载生图结果失败：HTTP {img.status_code}")
    ct = img.headers.get("content-type", "")
    ext = "png" if "png" in ct else ("jpg" if ("jpeg" in ct or "jpg" in ct) else "png")
    return img.content, ext, notes


# --------------------------------------------------------------------------- #
# 通道（channel）：能力矩阵 + 通道链
#
# 背景（D15 决策 + 审核三级处理，2026-10-22）：
#   - 单一 provider 是全局二选一，人物格要参考图就得整集换通道 → 风格分裂；
#   - 单一 provider 还会被内容审核"一票否决"（实测 wan2.2 把"空车内"判成敏感内容），
#     之前只能降级占位图 —— 应该先软化重试、再换通道，全败才占位。
# 因此引入通道链：主通道 + LONGSPAN_IMAGE_FALLBACKS 备用链，每个通道带能力声明。
# 铁律：全败时 fallback_reason 必须列出**每个通道**的失败原因，不许只报最后一个。
# --------------------------------------------------------------------------- #


@dataclass
class ImageChannel:
    """一条生图通道 = 协议 + 凭据 + 能力声明（能力是路由依据，必须如实声明）。"""

    kind: str                      # dashscope-mm / dashscope / jdcloud / openai-compat
    base_url: str
    api_key: str
    model: str
    service: str = ""              # 仅 jdcloud 用（预置服务标识）
    name: str = ""                 # 留痕用通道名，空则按 kind/model 生成
    supports_reference: bool = False
    supports_negative: bool = False
    supports_seed: bool = False

    def label(self) -> str:
        return self.name or f"{self.kind}/{self.model or 'default'}"


# 各协议的能力矩阵（实测结论，勿凭想象改）：
#   dashscope-mm：multimodal-generation 同步；实测 qwen-image-3.0-pro 支持
#     参考图（data URL 多图输入）+ negative_prompt + seed + 720*1280；
#   dashscope：text2image 异步任务；negative/seed 支持，无参考图；
#   jdcloud：image-01 同步；subject_reference 支持（1 张），negative 不支持；
#   openai-compat：/images/generations；negative/seed 视服务商而定（如实标注）。
_CAPS = {
    "dashscope-mm": (True, True, True),
    "dashscope": (False, True, True),
    "jdcloud": (True, False, True),
    # openai-compat：2026-10-29 起支持参考图 —— 走 `/images/edits`（multipart 图生图）。
    # 端点不可用时会在 _request_real_image 里退回文生图并写明"参考图未生效"。
    "openai-compat": (True, True, True),
}

_KNOWN_KINDS = frozenset(_CAPS)


def _channel_from_parts(kind: str, base_url: str, api_key: str, model: str,
                        service: str = "", name: str = "") -> ImageChannel | None:
    kind = (kind or "").strip().lower()
    if kind not in _KNOWN_KINDS or not api_key or not base_url:
        return None
    ref, neg, seed = _CAPS[kind]
    return ImageChannel(
        kind=kind, base_url=base_url.rstrip("/"), api_key=api_key, model=(model or "").strip(),
        service=(service or "").strip(), name=(name or "").strip(),
        supports_reference=ref, supports_negative=neg, supports_seed=seed,
    )


def parse_channels() -> list[ImageChannel]:
    """主通道（LONGSPAN_IMAGE_*）+ 备用链（LONGSPAN_IMAGE_FALLBACKS）→ 通道列表。

    解析规则（对配置宽容，对矛盾报错）：
      - fallbacks 是 JSON 数组；单个对象也接受（自动包成数组）；
      - 缺 provider/api_key/base_url 的项跳过并在控制台记日志（配置半残不该炸整轮）；
      - 未知 provider 跳过 —— 别拿未知协议去赌线上。
    """
    channels: list[ImageChannel] = []
    main = _channel_from_parts(
        settings.image_provider if settings.image_provider not in ("", "auto") else "openai-compat",
        settings.image_base_url, settings.image_api_key,
        settings.image_model, settings.image_service, name="main",
    )
    if main:
        channels.append(main)
    raw = (settings.image_fallbacks or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("IMAGE_FALLBACKS 不是合法 JSON，忽略备用通道：%s", exc)
            obj = None
        items = obj if isinstance(obj, list) else ([obj] if isinstance(obj, dict) else [])
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            ch = _channel_from_parts(
                str(it.get("provider") or ""), str(it.get("base_url") or ""),
                str(it.get("api_key") or ""), str(it.get("model") or ""),
                str(it.get("service") or ""), str(it.get("name") or f"fallback-{i + 1}"),
            )
            if ch:
                channels.append(ch)
            else:
                logger.warning("备用通道 #%d 配置不完整（缺 provider/api_key/base_url 或未知协议），已跳过", i + 1)
    return channels


# 内容审核拦截的特征（实测：万相 "Output data may contain inappropriate content"、
# 京东云 InputImageSensitiveContentDetected「疑似真人」、DashScope DataInspectionFailed 等）
_MODERATION_MARKERS = (
    "inappropriate content", "sensitivecontent", "sensitive content", "content_policy",
    "content policy", "datainspection", "moderation", "censor",
    "审核", "敏感内容", "涉政", "风控", "违规内容",
)


def is_moderation_error(err_text: str) -> bool:
    """判断生图失败是否为内容审核拦截（决定走软化重试还是直接换通道）。"""
    low = (err_text or "").lower()
    return any(m in low for m in _MODERATION_MARKERS)


# 软化替换（审核误杀自救）：语义降噪，不是合规豁免 —— 真违规内容软化后照样该拦。
# L1 轻度：替换明显高敏词；L2 重度：再压情绪/冲突词。
_SOFTEN_L1: list[tuple[str, str]] = [
    (r"\bblood(y|stained|ied)?\b", "red-toned"),
    (r"\bblood\b", "red marks"),
    (r"\bcorpse[s]?\b", "debris"),
    (r"\bdead (body|bodies)\b", "still figures"),
    (r"\bdead\b", "still"),
    (r"\bdeath\b", "stillness"),
    (r"\bgun(s)?\b", "metal tools"),
    (r"\brifle(s)?\b", "metal tools"),
    (r"\bknife(s)?\b", "metal tools"),
    (r"\bwound(ed)?\b", "tired"),
    (r"\bkill(ed|ing|s)?\b", "confronted"),
    (r"\bnaked|nude\b", "plain-clothed"),
    (r"\bskull(s)?\b", "shell fragments"),
    (r"血迹|血污|流血", "红色痕迹"),
    (r"尸体", "杂物"),
    (r"死亡|死去|死了", "静止"),
    (r"枪", "金属物"),
    (r"刀", "工具"),
    (r"伤口", "伤痕纹样"),
]
_SOFTEN_L2: list[tuple[str, str]] = [
    (r"\bhorror\b", "dim"),
    (r"\beerie|creepy\b", "quiet"),
    (r"\bgloomy\b", "dim"),
    (r"\bfear|frightening\b", "unease"),
    (r"\bviolent|brutal\b", "intense"),
    (r"\bcorpse|cadaver\b", "debris"),
    (r"恐怖|阴森|诡异", "幽暗"),
    (r"血腥|暴力", "激烈"),
]

_SOFTEN_SUFFIX_L1 = "wholesome, family-friendly illustration, no violence, no gore"
# 软化后缀不许带画种词（旧值 "gentle flat illustration" 会把厚涂/水墨任务
# 在审核重试时悄悄改成平涂 —— 与 spec 锚对冲，属于同一类静默改画风）。
_SOFTEN_SUFFIX_L2 = "gentle illustration, safe for all audiences, soft lighting"


def soften_prompt(prompt: str, level: int) -> str:
    """按级软化提示词。level=1 轻度、2 重度（累积），其余返回原样。

    从**原始 prompt**出发生成每一级（不要在软化结果上叠加，避免重复拼接）。
    """
    if level < 1:
        return prompt
    text = prompt
    for pat, rep in _SOFTEN_L1:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    if level >= 2:
        for pat, rep in _SOFTEN_L2:
            text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    suffix = _SOFTEN_SUFFIX_L2 if level >= 2 else _SOFTEN_SUFFIX_L1
    return f"{text}, {suffix}"


async def _request_dashscope_mm_image(
    prompt: str,
    ratio: str,
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    size: str | None = None,
    negative_prompt: str = "",
    seed: int = 0,
    extra: dict | None = None,
    reference_images: list[str] | None = None,
    timeout: float = 240.0,
) -> tuple[bytes, str, list[str]]:
    """DashScope 多模态同步生图（qwen-image / wan2.7-image 等），**支持参考图**。

    协议（实测 2026-10-22，万相中转）：
      POST {base}/services/aigc/multimodal-generation/generation（同步返回，无任务轮询）
      input.messages[0].content = [{image: <data URL>}, ..., {text: prompt}]
      parameters.size = "720*1280"（W*H），negative_prompt / seed 原生支持。
    实测 qwen-image-3.0-pro：参考图跨场景保持人物一致（发型/五官/服装/胸牌全保留）；
    「空车内无人」这类被 wan2.2 审核误杀的描述在本模型直接过审。

    reference_images：data URL / 公网 URL 列表（最多 3 张，官方上限），顺序即输入顺序。
    """
    base = (base_url or settings.image_base_url).rstrip("/")
    key = api_key or settings.image_api_key
    notes: list[str] = []
    content: list[dict] = []
    refs = [r for r in (reference_images or []) if r][:3]
    for r in refs:
        content.append({"image": r})
    content.append({"text": prompt[:2000]})
    params: dict = {"size": _wan_size(size, ratio), "n": 1}
    if negative_prompt:
        params["negative_prompt"] = negative_prompt
    if seed:
        params["seed"] = int(seed)
    if extra:
        params.update(extra)
    body = {
        "model": (model or settings.image_model or "qwen-image-3.0-pro"),
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": params,
    }
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(
            base + "/services/aigc/multimodal-generation/generation",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"multimodal 生图返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        if data.get("code"):
            raise RuntimeError(f"multimodal 生图失败（{data.get('code')}）：{data.get('message') or resp.text[:200]}")
        content_out = ((data.get("output") or {}).get("choices") or [{}])[0].get("message", {}).get("content") or []
        url = next((c.get("image") or c.get("image_url") for c in content_out if isinstance(c, dict)), "")
        if not url:
            raise RuntimeError(f"multimodal 生图返回里没有图片：{resp.text[:200]}")
        if refs:
            notes.append(f"已携带人物参考图（{len(refs)} 张，多图输入）")
        if negative_prompt:
            notes.append("已携带负向提示词（原生支持）")
        if seed:
            notes.append(f"已固定随机种子 seed={seed}")
        img = await cli.get(url)
    if img.status_code >= 400:
        raise RuntimeError(f"下载生图结果失败：HTTP {img.status_code}")
    ct = img.headers.get("content-type", "")
    ext = "png" if "png" in ct else ("jpg" if ("jpeg" in ct or "jpg" in ct) else "png")
    return img.content, ext, notes


def _parse_extra(raw: str, kind: str) -> dict:
    """高级参数是 JSON 字符串；写错就在生图前直接报错，不带着坏参数去打接口。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{kind} 高级参数不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{kind} 高级参数必须是 JSON 对象")
    return obj


def _image_to_data_url(path: str) -> str:
    """把本地图片转成 base64 data URL（供 subject_reference 的 image_file 使用）。

    京东云 subject_reference 的 image_file 接受两种形式：
      - data URL（data:image/jpeg;base64,{data}）
      - 公网 URL
    本地定妆图只能走 data URL 形式。mime 按扩展名推断，缺省按 jpeg。
    超限（10M）时抛错 —— 参考图一般远小于此，但仍要挡住极端情况。
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"参考图不存在：{path}")
    size = p.stat().st_size
    if size > 10 * 1024 * 1024:
        raise ValueError(f"参考图 {p.name} 超过 10M 上限（{size / 1024 / 1024:.1f}MB）")
    ext = p.suffix.lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


async def generate_image(
    storage: Any,
    run_id: str,
    prompt: str,
    path: str,
    ratio: str = "9:16",
    model_override: str | None = None,
    service_override: str | None = None,
    *,
    params: dict | None = None,
    subject_reference: str | list[str] | None = None,
) -> dict[str, Any]:
    """生成一张分镜图。返回 {path, provider, is_placeholder, fallback_reason?, params_note?, channel_trace?}。

    model_override / service_override 来自「阶段级模型覆盖」——
    只影响这一次调用，不动全局配置（一个任务的实验不该污染整站）。
    params 覆盖全局的 negative_prompt / size / seed / 额外字段，
    优先级：调用方传入 > 全局配置。未生效的参数会回传 params_note，不静默吞掉。

    subject_reference（FR-04，人物一致性）：本地参考图路径或 data URL / 公网 URL。
    支持传列表（多角色同框格）—— 通道能吃几张就送几张，吃不下的如实留痕。
    路由（image_routing）：auto 时带参考图的格**优先走支持参考图的通道**（保持相对顺序）；
    main 时永远主通道优先。参考图传到不支持的通道时如实回传 params_note，绝不静默吞掉。

    审核三级处理（D15 伴生，2026-10-22）：
      1. 内容审核拦截 → 同通道软化提示词重试（Lv1 轻度 / Lv2 重度）；
      2. 软化仍拦 → 切换下一通道（fallbacks 链）；
      3. 全部通道失败 → 占位分镜卡 + fallback_reason **逐通道列出失败原因**。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    params = params or {}
    negative = str(params.get("negative_prompt") or settings.image_negative_prompt or "").strip()
    size = str(params.get("size") or settings.image_size or "").strip()
    try:
        seed = int(params.get("seed") or settings.image_seed or 0)
    except (TypeError, ValueError):
        seed = 0
    extra = _parse_extra(str(params.get("params_json") or settings.image_params_json or ""), "生图")

    # 参考图归一化：本地路径 → data URL；已是 data URL / http URL 则原样使用。
    # 多角色同框格可以传列表（2026-11-02：旧实现只送 1 张，配角每格重画一张脸）。
    subject_refs: list[str] = []
    if subject_reference:
        raw_refs = [subject_reference] if isinstance(subject_reference, str) else list(subject_reference)
        for item in raw_refs:
            sr = str(item or "").strip()
            if not sr:
                continue
            if sr.startswith("data:") or re.match(r"^https?://", sr):
                subject_refs.append(sr)
            else:
                subject_refs.append(_image_to_data_url(sr))

    channels = parse_channels()
    if model_override and channels:
        # 阶段级模型覆盖只作用于主通道（备用通道的模型在 fallbacks 里各自声明）
        channels[0] = dataclasses.replace(channels[0], model=model_override)

    routing = (settings.image_routing or "auto").strip().lower()
    if subject_refs and routing == "auto" and len(channels) > 1:
        # 参考图能力优先：稳定排序，不打破同能力通道的主备顺序
        channels = sorted(channels, key=lambda c: 0 if c.supports_reference else 1)

    # 文件名按扩展名修正：真实生图是位图，兜底是 SVG
    fallback_reasons: list[str] = []
    trace: list[str] = []
    data = ext = None
    notes: list[str] = []
    used_channel: ImageChannel | None = None
    used_prompt = prompt

    def _real_ready(ch: ImageChannel) -> bool:
        return bool(ch.api_key and ch.base_url)

    for ch in channels:
        if not _real_ready(ch):
            fallback_reasons.append(f"{ch.label()}: 未配置 api_key/base_url，跳过")
            continue
        attempt_prompt = prompt
        soft_level = 0
        for attempt in range(3):  # 原始 + Lv1 软化 + Lv2 软化
            try:
                data, ext, notes = await _request_via_channel(
                    ch, attempt_prompt, ratio,
                    model_override if ch is channels[0] else None,
                    service_override if ch is channels[0] else None,
                    size=size, negative_prompt=negative if ch.supports_negative else "",
                    seed=seed, extra=extra or None,
                    subject_refs=subject_refs,
                )
                used_channel = ch
                used_prompt = attempt_prompt
                break
            except Exception as e:  # noqa: BLE001
                err = str(e)[:300]
                if is_moderation_error(err) and soft_level < 2:
                    soft_level += 1
                    attempt_prompt = soften_prompt(prompt, soft_level)
                    trace.append(f"{ch.label()}：审核拦截，软化 Lv{soft_level} 重试")
                    continue
                fallback_reasons.append(f"{ch.label()}: {err}")
                trace.append(f"{ch.label()}：失败，{'换下一通道' if ch is not channels[-1] else '无备用通道'}")
                break  # 非审核类失败（网络/配置/协议）换通道才有意义
        if used_channel is not None:
            break

    if used_channel is not None and data is not None:
        p = re.sub(r"\.(svg|png|jpe?g)$", f".{ext}", path, flags=re.IGNORECASE)
        rel, size_bytes = storage.write_bytes(run_id, p, data)
        logger.info("真实生图成功 run=%s path=%s channel=%s bytes=%s", run_id, rel, used_channel.label(), size_bytes)
        out: dict[str, Any] = {
            "path": rel,
            "provider": f"{used_channel.kind}/{used_channel.model or 'default'}",
            "is_placeholder": False,
            "size": size_bytes,
            "channel": used_channel.label(),
        }
        if trace:
            out["channel_trace"] = trace
        # 未生效参数如实回传（铁律 #2）
        ineffective: list[str] = list(notes)
        if negative and not used_channel.supports_negative:
            ineffective.append("负面提示词不被该通道协议支持，已忽略；防写实请写进提示词的反写实条款")
        if subject_refs and not used_channel.supports_reference:
            ineffective.append("参考图（subject_reference）未生效：该通道不支持人物参考图，已退化为纯文生图")
        if ineffective:
            out["params_note"] = "；".join(ineffective)
        # 能力位说支持 ≠ 真的用上了：服务端不认 edits 时会退回文生图并留下
        # "参考图未生效"的说明 —— 那种情况不能标成已用参考图（否则一致性无从追溯）。
        ref_actually_used = bool(
            subject_refs
            and used_channel.supports_reference
            and not any(_REF_UNUSED_NOTE in n for n in notes)
        )
        if ref_actually_used:
            out["used_subject_reference"] = True
        out["prompt_used"] = used_prompt[:1200]
        return out

    # 全通道失败 / 未配置 → 占位分镜卡（逐通道留痕，绝不只报最后一个错）
    reason = "；".join(fallback_reasons) if fallback_reasons else "未配置生图服务（IMAGE_API_KEY 为空）"
    logger.warning("生图全通道失败，降级占位卡 run=%s：%s", run_id, reason[:400])
    svg = storyboard_card_svg(prompt, ratio)
    p = re.sub(r"\.(png|jpe?g)$", ".svg", path, flags=re.IGNORECASE)
    rel, size_bytes = storage.write_text(run_id, p, svg)
    out = {
        "path": rel,
        "provider": "local-svg",
        "is_placeholder": True,
        "size": size_bytes,
        "fallback_reason": reason,
    }
    if trace:
        out["channel_trace"] = trace
    if subject_refs:
        out["params_note"] = "参考图（subject_reference）未生效：本地兜底分镜卡不支持人物参考图"
    return out


async def _request_via_channel(
    ch: ImageChannel,
    prompt: str,
    ratio: str,
    model_override: str | None,
    service_override: str | None,
    *,
    size: str,
    negative_prompt: str,
    seed: int,
    extra: dict | None,
    subject_refs: list[str] | None,
) -> tuple[bytes, str, list[str]]:
    """按通道协议分发请求。model_override / service_override 仅主通道消费。"""
    refs = [r for r in (subject_refs or []) if r]
    if ch.kind == "dashscope-mm":
        return await _request_dashscope_mm_image(
            prompt, ratio, model=model_override or ch.model or None,
            base_url=ch.base_url, api_key=ch.api_key,
            size=size or None, negative_prompt=negative_prompt, seed=seed,
            extra=extra, reference_images=refs or None,
        )
    if ch.kind == "dashscope":
        return await _request_dashscope_image(
            prompt, ratio, model=model_override or ch.model or None,
            base_url=ch.base_url, api_key=ch.api_key,
            size=size or None, negative_prompt=negative_prompt, seed=seed, extra=extra,
        )
    if ch.kind == "jdcloud":
        return await _request_jdcloud_image(
            prompt, ratio, model=model_override or ch.model or None,
            service=service_override or ch.service or None,
            base_url=ch.base_url, api_key=ch.api_key,
            subject_reference=refs[0] if refs else None, seed=seed, extra=extra,
            extra_refs=max(0, len(refs) - 1),
        )
    return await _request_real_image(
        prompt, ratio, model=model_override or ch.model or None,
        base_url=ch.base_url, api_key=ch.api_key,
        size=size or None, negative_prompt=negative_prompt, seed=seed, extra=extra,
        subject_refs=refs or None,
    )
