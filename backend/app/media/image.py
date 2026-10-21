"""生图 Provider —— 分镜画面的"手"。

真实服务：OpenAI 兼容 `POST /v1/images/generations`
（dall-e-3 / gpt-image 系 / 各中转站的 image 模型均可，吃 b64_json 与 url 两种返回）。

本地兜底：**程序化电影感分镜卡**（SVG 矢量图）。
不是随机涂鸦 —— 按 prompt 哈希确定性地选色调与构图，同一格重跑结果一致；
画面上明确标注「占位图」，配合事件流告知（禁止静默降级，架构铁律 #2）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
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

COMIC_STYLE_SKILLS = {"comic-drama"}

# 两层锚的「缺失判定关键字」与补齐用的完整条款
COMIC_STYLE_CLAUSES: list[tuple[str, str, str]] = [
    # (key, 判定关键字（小写）, 补齐条款)
    (
        "反写实条款",
        "not photorealistic",
        "NOT a photograph, NOT photorealistic, NOT live-action, NOT 3D render",
    ),
    (
        "角色风格化强化句",
        "stylized 2d anime",
        "Stylized 2D anime character design, anime face with large expressive eyes, "
        "clean drawn lineart, flat cel shading on skin, no realistic skin texture, no photography",
    ),
    # 2026-10-21 实测：即便有上面两条，模型在句首写 "thick paint realistic
    # illustration" 仍会产出半写实厚涂人脸 —— 这类图过不了视频模型的内容审核
    # （火山 InputImageSensitiveContentDetected.PrivacyInformation「疑似真人」，
    # 12 格里 10 格被拦，重试也救不回来）。平涂赛璐璐 TV 动画画风是实测能
    # 稳定过审的风格，同时让全片风格统一。
    (
        "平涂赛璐璐条款",
        "tv anime",
        "Flat 2D TV anime screenshot style, thick clean outlines, uniform flat colors, "
        "simplified facial shading, absolutely no realistic skin texture, no painterly brushwork, no oil painting texture",
    ),
]

# 模型爱在句首写写实倾向的画种描述，与风格锚直接对冲 —— 出现即中和并留痕
_REALISM_PATTERNS = [
    (re.compile(r"\bthick\s+paint\s+realistic\s+illustration\b", re.I), "stylized anime illustration"),
    (re.compile(r"\brealistic\s+illustration\b", re.I), "stylized anime illustration"),
    (re.compile(r"\bhyper[- ]?realistic\b", re.I), "stylized anime"),
    (re.compile(r"\bphotorealistic\b(?!.*not\b)", re.I), "stylized anime"),
]


def ensure_style_anchor(prompt: str) -> tuple[str, str | None]:
    """漫剧类生图提示词的风格锚校验：缺「反写实 / 角色风格化」条款就在开头补齐。

    另有一层**写实倾向中和**：模型句首写 "realistic illustration" 这类画种描述
    时，补再多锚也会被它带偏（实测半写实人脸过不了视频内容审核），直接替换并留痕。

    返回 (修正后的提示词, 留痕说明)。提示词本来就齐 → 原样返回 (prompt, None)。
    校验是字符串级的 —— 模型写出的提示词再"看起来对"，缺了关键字就是缺了，
    不做语义层猜测（可计量、可复核）。
    """
    notes: list[str] = []
    for pat, repl in _REALISM_PATTERNS:
        prompt, n = pat.subn(repl, prompt)
        if n:
            notes.append(f"中和写实倾向描述 {n} 处（→ {repl}）")
    low = (prompt or "").lower()
    missing = [(key, clause) for key, keyword, clause in COMIC_STYLE_CLAUSES if keyword not in low]
    if not missing and not notes:
        return prompt, None
    if missing:
        prompt = ", ".join(clause for _, clause in missing) + ". " + prompt
        notes.append("生图提示词缺少风格锚（" + "、".join(k for k, _ in missing) + "），已在开头自动补齐")
    return prompt, "；".join(notes)


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
) -> tuple[bytes, str, list[str]]:
    """调 OpenAI 兼容生图接口，返回 (图片字节, 扩展名, 未能生效的参数说明)。

    三个可选参数（size / negative_prompt / seed）不是所有服务商都支持。
    **不支持时要如实回传"没生效"**，而不是照发了事 —— 用户以为加了负向提示词、
    实际没加，属于典型的静默劣化。
    """
    ignored: list[str] = []
    if ratio == "9:16":
        default_size = "1024x1792"
    elif ratio == "16:9":
        default_size = "1792x1024"
    else:
        default_size = "1024x1024"
    body: dict = {
        "model": model or settings.image_model,
        "prompt": prompt,
        "size": (size or "").strip() or default_size,
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
            (base_url or settings.image_base_url).rstrip("/") + "/images/generations",
            headers={"Authorization": f"Bearer {api_key or settings.image_api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"生图接口返回 {resp.status_code}：{resp.text[:300]}")
    data = resp.json()
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), "png", ignored
    if item.get("url"):
        async with httpx.AsyncClient(timeout=120) as cli:
            img = await cli.get(item["url"])
        if img.status_code >= 400:
            raise RuntimeError(f"下载生图结果失败：HTTP {img.status_code}")
        ct = img.headers.get("content-type", "")
        ext = "png" if "png" in ct else ("jpg" if ("jpeg" in ct or "jpg" in ct) else "png")
        return img.content, ext, ignored
    raise RuntimeError(f"生图返回里既没有 b64_json 也没有 url：{str(data)[:200]}")


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
    subject_reference: str | None = None,
) -> dict[str, Any]:
    """生成一张分镜图。返回 {path, provider, is_placeholder, fallback_reason?, params_note?}。

    model_override / service_override 来自「阶段级模型覆盖」——
    只影响这一次调用，不动全局配置（一个任务的实验不该污染整站）。
    params 覆盖全局的 negative_prompt / size / seed / 额外字段，
    优先级：调用方传入 > 全局配置。未生效的参数会回传 params_note，不静默吞掉。

    subject_reference（FR-04，人物一致性）：可选，本地参考图路径或 data URL / 公网 URL。
    传入本地路径会自动转 data URL。仅京东云 image 接口支持（subject_reference 字段），
    OpenAI 兼容接口与本地兜底不支持 —— 不支持的 provider 会如实回传 params_note，
    绝不静默吞掉（架构铁律 #2）。
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

    # 参考图归一化：本地路径 → data URL；已是 data URL / http URL 则原样使用
    subject_ref: str | None = None
    if subject_reference:
        sr = subject_reference.strip()
        if sr.startswith("data:") or re.match(r"^https?://", sr):
            subject_ref = sr
        else:
            subject_ref = _image_to_data_url(sr)

    # 文件名按扩展名修正：真实生图是位图，兜底是 SVG
    provider_kind = (settings.image_provider or "auto").strip().lower()
    real_ready = provider_kind != "local" and settings.image_api_key and settings.image_base_url
    if real_ready:
        try:
            if provider_kind == "jdcloud":
                data, ext, notes = await _request_jdcloud_image(
                    prompt, ratio, model=model_override, service=service_override,
                    subject_reference=subject_ref, seed=seed, extra=extra or None,
                )
                if negative:
                    # image-01 协议没有 negative_prompt 字段（官方文档确认），
                    # 不支持就明说，绝不静默丢弃 —— 防写实靠提示词里的反写实条款。
                    notes = list(notes) + [
                        "负面提示词不被 image-01 协议支持，已忽略；防写实请写进提示词的反写实条款"
                    ]
                provider_label = f"jdcloud/{model_override or settings.image_model or 'image-01'}"
            else:
                # OpenAI 兼容接口不支持 subject_reference，如实回传未生效
                data, ext, notes = await _request_real_image(
                    prompt, ratio, model=model_override,
                    size=size or None, negative_prompt=negative, seed=seed, extra=extra or None,
                )
                if subject_ref:
                    notes = list(notes) + [
                        "参考图（subject_reference）未生效：当前 provider 不支持人物参考图，已退化为纯文生图"
                    ]
                provider_label = model_override or settings.image_model or "openai-compat"
            p = re.sub(r"\.(svg|png|jpe?g)$", f".{ext}", path, flags=re.IGNORECASE)
            rel, size_bytes = storage.write_bytes(run_id, p, data)
            logger.info("真实生图成功 run=%s path=%s bytes=%s", run_id, rel, size_bytes)
            out: dict[str, Any] = {
                "path": rel, "provider": provider_label, "is_placeholder": False, "size": size_bytes,
            }
            if notes:
                out["params_note"] = "；".join(notes)
            if subject_ref:
                out["used_subject_reference"] = True
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("真实生图失败，降级为分镜卡：%s", e)
            fallback_reason = str(e)[:300]
    else:
        fallback_reason = "未配置生图服务（IMAGE_API_KEY 为空）"

    svg = storyboard_card_svg(prompt, ratio)
    p = re.sub(r"\.(png|jpe?g)$", ".svg", path, flags=re.IGNORECASE)
    rel, size_bytes = storage.write_text(run_id, p, svg)
    out = {
        "path": rel,
        "provider": "local-svg",
        "is_placeholder": True,
        "size": size_bytes,
        "fallback_reason": fallback_reason,
    }
    if subject_ref:
        out["params_note"] = "参考图（subject_reference）未生效：本地兜底分镜卡不支持人物参考图"
    return out
