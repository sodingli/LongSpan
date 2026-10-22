"""生图后的多模态自检（视觉后检）—— 唯一会「看图」的一层。

为什么需要它（2026-10-22 实测事故，run 2942b7ea 第 8 格）：
    提示词写 extreme close-up of an old fisherman's weathered hand touching the
    inner pocket of his shirt —— 结构校验全过（景别合法、单格单动作、锚点在位），
    但成图里**人物没有脑袋**：胸口占满画框，头被裁在画外，成片里一个悬空的衣领。

    根因是"这套校验全是文本级的"：它只能判断**提示词**合规，判断不了
    **模型实际画出来的东西**。文本校验能防"话没说对"，防不了"画没画对"。
    所以补这一层：用视觉语言模型逐格检查构图完整性，检出即修（补取景声明重画），
    修不好就如实标注，不许默默交付。

三条不可动摇的约束：
    1. **不阻断生产**：检查失败（网络/配额/解析）只记 checked=False + error，
       绝不让一次视觉检查的抖动毁掉整集渲染。
    2. **不做审美评判**：只判可判定的结构缺陷（人物头部是否被裁、主体是否残缺），
       不判"好不好看"（那没有客观标准，会变成噪声）。
    3. **结论必须留痕**：每格的判定结果写回 images.json（vision 字段），
       人工复核时能看到"这格为什么被重画过"。

协议：DashScope 原生 multimodal-generation（与生图同一中转，报文已验证可用）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("longspan.vision_check")

# 单次检查的提示词。原则：**只问可判定的结构问题**，并要求结构化输出。
# 明确写出"纯空景不算缺陷"，否则空镜（小船驶向雾中）会被判成"人物缺失"而误重画。
_PROMPT_STRICT = (
    "这是一张竖屏动画分镜图。请只输出一个 JSON 对象，不要解释、不要代码块：\n"
    '{"has_person": true/false, "person_cut": true/false, "issue": "一句话中文说明缺陷，无缺陷写空字符串"}\n'
    "判定规则：\n"
    "1. has_person：画面里是否能看到人物的身体（脸、头、躯干、四肢、衣着）——"
    "纯风景、空镜、只有物件或船只时填 false；\n"
    "2. person_cut：画面里出现了人物躯干/衣着/四肢，但**头部被整体裁掉或没有画出来**"
    "（例如只看到胸口和手，衣领上方是空的）→ 填 true；\n"
    "3. 有意为之的手部/物件特写（画面里只有手和物件，没有躯干衣着）不算缺陷，填 false；\n"
    "4. 人物完整、或纯空景，都填 false。\n"
    "只输出 JSON。"
)

# 已声明「局部特写」的格用这一版：**没有人头是预期结果**，不能反过来判缺陷。
# 否则每格补了边界声明的干净特写都会被判"头部被裁"，陷入「补声明 → 仍判错 → 再补」的死循环。
_PROMPT_ALLOW_HEADLESS = (
    "这是一张竖屏动画分镜图，本格是**有意的局部特写**（导演就只要手/物件/布料的局部，"
    "画面里没有人脸和头部是正确且预期的）。请只判断是否出现构图失误。只输出 JSON：\n"
    '{"has_person": true/false, "person_cut": true/false, "issue": "一句话中文说明缺陷，无缺陷写空字符串"}\n'
    "判定规则：\n"
    "1. 画面是干净的手部/物件/衣料局部（看不到敞开的衣领、没有悬空的躯干）→ person_cut 填 false；\n"
    "2. 画面里出现整件上衣或胸口占满画框、衣领上方空着，看起来像「被切掉头的人」→ 填 true；\n"
    "3. 画面里出现完整人物（有头）→ 填 false；\n"
    "4. 纯空景/物件 → 填 false。\n"
    "只输出 JSON。"
)


# 需要被判定为「构图残缺」的缺陷关键词（视觉模型偶尔把结论写在 issue 里而非字段）
_CUT_HINTS = ("没有头", "无头", "头部缺失", "头部被裁", "头被裁", "缺头", "headless",
              "head is missing", "head cut", "no head")


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON（容忍代码栅栏、前后废话、尾随逗号）。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE).strip()
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    s = re.sub(r",\s*([}\]])", r"\1", s)  # 尾随逗号
    try:
        data = json.loads(s)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _endpoint() -> tuple[str, str, str]:
    """返回 (url, api_key, model)。配置缺失时 url 为空。"""
    base = (settings.vision_base_url or settings.image_base_url or "").rstrip("/")
    key = settings.vision_api_key or settings.image_api_key or ""
    model = settings.vision_model or "qwen3-vl-plus"
    if not base or not key:
        return "", "", model
    return f"{base}/services/aigc/multimodal-generation/generation", key, model


async def inspect_image(
    image_path: Path, *, allow_headless: bool = False, timeout: float = 90.0
) -> dict[str, Any]:
    """检查一张分镜图是否「人物头部被裁」等构图残缺。

    allow_headless：本格是否**有意**的局部特写（prompt 里已声明 no face / only … fills
    the frame）。为 True 时"画面里没有头"是预期结果，不能判成缺陷 ——
    否则每格补了边界声明的干净特写都会被判"头部被裁"，陷入
    「补声明 → 仍判错 → 再补」的死循环（2026-10-22 实测踩到）。

    返回字段：
      checked  —— 是否真的做了检查（False 表示关闭/未配置/调用失败，原因在 reason）
      ok       —— 构图是否可接受（checked=False 时恒为 True，不阻断生产）
      issue    —— 缺陷描述（人话，可直接写进留痕）
      model / latency_ms / usage —— 计费与排障留痕
    """
    mode = str(getattr(settings, "vision_check_mode", "auto") or "auto").lower()
    if mode == "off":
        return {"checked": False, "ok": True, "reason": "视觉自检已关闭（vision_check_mode=off）"}

    url, key, model = _endpoint()
    if not url:
        return {
            "checked": False,
            "ok": True,
            "reason": "视觉自检未配置（缺 vision/image 的 base_url 或 api_key），本轮未检查画面内容",
        }

    try:
        raw = Path(image_path).read_bytes()
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "ok": True, "reason": f"读图失败：{exc}"}

    mime = "image/png" if str(image_path).lower().endswith(".png") else "image/jpeg"
    b64 = base64.b64encode(raw).decode()
    body = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [
            {"image": f"data:{mime};base64,{b64}"},
            {"text": _PROMPT_ALLOW_HEADLESS if allow_headless else _PROMPT_STRICT},
        ]}]},
        "parameters": {"result_format": "message", "temperature": 0},
    }

    loop = asyncio.get_running_loop()
    started = loop.time()

    def _post() -> httpx.Response:
        return httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )

    try:
        resp = await asyncio.to_thread(_post)
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "ok": True, "reason": f"视觉自检请求失败（不阻断）：{exc}"[:300]}

    latency = int((loop.time() - started) * 1000)
    if resp.status_code >= 400:
        return {
            "checked": False,
            "ok": True,
            "reason": f"视觉自检返回 {resp.status_code}（不阻断）：{resp.text[:200]}",
        }

    try:
        data = resp.json()
        content = data["output"]["choices"][0]["message"]["content"]
        text = "".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
        usage = data.get("usage") or {}
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "ok": True, "reason": f"视觉自检返回结构异常（不阻断）：{exc}"}

    verdict = _parse_json_loose(text)
    if verdict is None:
        # 判不出结论时不当作"有问题"（不阻断），但如实记录原文便于排查
        return {
            "checked": False,
            "ok": True,
            "reason": f"视觉自检输出无法解析（不阻断）：{text[:160]}",
            "model": model,
            "latency_ms": latency,
        }

    has_person = bool(verdict.get("has_person"))
    cut = bool(verdict.get("person_cut"))
    issue = str(verdict.get("issue") or "").strip()
    # 视觉模型偶尔结论写在 issue 里而字段填 false —— 两边任一指出就认
    if not cut and any(h in issue.lower() for h in _CUT_HINTS):
        cut = True
    if cut and not issue:
        issue = "人物头部被裁出画框（构图中出现躯干/衣着但没有头）"

    return {
        "checked": True,
        "ok": not cut,
        "has_person": has_person,
        "issue": issue,
        "model": model,
        "latency_ms": latency,
        "usage": usage,
    }


# 取景声明补丁：检出「无头半身」时，补一句确定性边界声明后重画一次。
# 为什么不改写整段提示词：只补一句最短的边界声明，其余原样 —— 改得越少，
# 风格与人物锚点越不容易被意外改动（可复现性优先）。
FRAMING_PATCH = (
    "only the hand and the fabric fill the frame, no face, no head, no torso in frame"
)
FRAMING_PATCH_GENERIC = (
    "the subject fills the frame, no face, no head, no torso in frame"
)


def patch_framing_prompt(prompt: str, shot: str | None = None) -> str:
    """给「无头半身」风险的提示词补上取景边界声明（幂等，已声明则原样返回）。"""
    from ..harness.consistency import _FRAMING_DECL_RE  # 与校验器共用一套判据

    if _FRAMING_DECL_RE.search(prompt or ""):
        return prompt
    patch = FRAMING_PATCH if re.search(r"\bhands?\b", prompt or "", re.IGNORECASE) else FRAMING_PATCH_GENERIC
    p = (prompt or "").rstrip()
    if p and not p.endswith("."):
        p += "."
    return f"{p} {patch}."
