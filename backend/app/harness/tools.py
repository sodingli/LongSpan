"""工具注册表 —— Harness 提供给模型的"手"。

工具契约必须模型友好（对应 MRD §7.3.3）：
    每个工具必须声明 when_to_use 与 failure_modes 的替代方案 ——
    没有替代方案，模型遇到失败只能干等或把问题丢给用户。

M0 提供最小工具集（读输入 / 写产物 / 记决策 / 占位图像）；
M1 追加 TTS、Remotion 合成、FFmpeg、真图像生成。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("longspan.tools")

# 成品二进制后缀：一律只允许工具产出，禁止模型用 write_artifact 写文本顶替。
BINARY_DELIVERABLE_EXT = {
    ".mp4", ".mov", ".webm", ".mkv", ".m4v",
    ".m4a", ".mp3", ".wav", ".aac", ".flac",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".zip",
}


@dataclass
class ToolContext:
    """工具执行上下文。runner 注入，工具不直接碰数据库。"""

    run_id: str
    stage_id: str
    brief: dict
    storage: Any
    extra: dict = field(default_factory=dict)

    def notify_decision(self, what: str, why: str, alternatives: list) -> None:
        """决策落库由 runner 注入的 sink 完成，工具只负责上报。"""
        sink = self.extra.get("decision_sink")
        if callable(sink):
            sink(what, why, alternatives)


@dataclass
class ToolSpec:
    name: str
    description: str
    when_to_use: str
    inputs: dict = field(default_factory=dict)
    outputs: str = ""
    cost_hint: str = "free"
    failure_modes: list[dict] = field(default_factory=list)
    handler: Callable[..., Awaitable[Any]] | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "cost_hint": self.cost_hint,
            "failure_modes": self.failure_modes,
        }

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.description}\n何时使用：{self.when_to_use}",
                "parameters": self.inputs
                or {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.handler is None:
            raise ValueError(f"工具 {spec.name} 缺少 handler")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def whitelist(self, names: list[str]) -> list[ToolSpec]:
        """按技能 manifest 的 tools_available 收窄可见工具。空列表 = 全部可见。"""
        if not names:
            return self.all()
        return [t for t in self._tools.values() if t.name in names]

    async def invoke(self, name: str, ctx: ToolContext, **kwargs) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            return {"error": f"未知工具 {name}，可用：{list(self._tools)}"}
        try:
            assert spec.handler is not None
            return await spec.handler(ctx, **kwargs)
        except Exception as exc:  # 工具失败不致命：返回结构化错误供模型改道
            logger.exception("工具执行失败 %s", name)
            return {
                "error": str(exc),
                "failure_modes": spec.failure_modes,
                "hint": "请按 failure_modes 中的替代方案改道，不要重试同一路径。",
            }


registry = ToolRegistry()


# --------------------------------------------------------------------------- #
# 内置工具
# --------------------------------------------------------------------------- #
async def _read_brief(ctx: ToolContext, key: str | None = None) -> dict:
    if key:
        return {"key": key, "value": ctx.brief.get(key)}
    return {"brief": ctx.brief}


# 工具名集合：模型偶尔会把"调用工具"误写成"写一个同名文件"。
# 血泪实例（2026-10-16）：产物目录里出现了一个 36 字节、名为 `list_artifacts` 的文件，
# 内容是"（调用工具列出文件列表）" —— 它冒充了一次工具调用，还污染了目录表。
_TOOL_NAME_LIKE = frozenset({
    "read_brief", "write_artifact", "read_artifact", "list_artifacts",
    "record_decision", "placeholder_image", "generate_images",
    "synthesize_lines", "render_video",
})


def _reject_tool_name_as_file(path: str) -> None:
    """拦掉「把工具调用写成文件」的全部已知变体。

    血泪实例（2026-10-16，任务 db0874408b）：
      - 16:03 出现 36 字节的 `list_artifacts`，内容"（调用工具列出文件列表）"；
      - 18:04 续跑后又出现 4 个带冒号前缀的 `read_artifact:panels.json` /
        `read_artifact:lines.json` / `read_artifact:spec.json` / `read_artifact:media/images.json`，
        内容是"（已读取，内容见用户上下文）"。
    模型想"记一笔我调用过工具"，但产物目录是**交付清单**，不是它的笔记本。
    """
    rel = str(path).strip().lstrip("./")
    if ":" in rel:
        raise ValueError(
            f"{path} 的路径里带了冒号 —— 这几乎一定是把「工具调用」写成了文件名"
            "（形如 read_artifact:panels.json）。工具不是产物：要读内容请直接调用该工具，"
            "不要在产物目录里留一张『我已读过』的纸条（它会污染交付清单）。"
        )
    for seg in rel.split("/"):
        if seg.lower() in _TOOL_NAME_LIKE:
            raise ValueError(
                f"{path} 看起来是把**工具名**当成了文件名 —— 工具不是产物。"
                "如果你确实要调用它，请直接在回复里调用该工具；"
                "不要再写一个同名文件假装调用过了（这会污染交付清单）。"
            )


async def _write_artifact(
    ctx: ToolContext, path: str, content: str, kind: str = "text"
) -> dict:
    rel_norm = str(path).strip().lstrip("./")
    _reject_tool_name_as_file(rel_norm)
    if rel_norm in TOOL_OWNED_REL:
        raise ValueError(
            f"{path} 是**工具自己的记账文件**，不是你的产物。它记录着"
            "「哪几格图/哪几句配音已经生成过」，写它会让幂等复用失效"
            "（要么谎报已完成、要么让已画好的图被重新生成并重复计费）。"
            "要看内容用 read_artifact；要改画面请改 panels.json 里对应的 prompt，"
            "再调 generate_images 重画。"
        )
    owners: dict = ctx.extra.get("declared_produces") or {}
    owner = owners.get(rel_norm)
    if owner and owner != ctx.stage_id:
        # 血泪实例（2026-10-16，任务 db0874408b）：合成阶段的模型看不到上游 panels.json
        # 的内容，于是"自己重建一份"并 write_artifact 覆盖了原文件（还写成裸数组），
        # 把分镜阶段辛苦写下的提示词永久冲掉，整轮任务 5 次重做全部崩在同一行。
        # 上游清单是下游的**输入契约**，任何阶段都不能改写别人声明要产的文件。
        raise ValueError(
            f"{path} 是阶段「{owner}」声明的产物，当前阶段「{ctx.stage_id}」不能覆写它。"
            "上游清单是下游的输入契约，改写它会让你自己和后面的阶段读到错误数据。"
            f"如果你认为 {path} 有问题：请用 read_artifact 读它、用 record_decision 写清"
            "问题与依据，而把你的结论写进本阶段自己的产物里。"
        )
    suffix = ("." + str(path).rsplit(".", 1)[-1]).lower() if "." in str(path) else ""
    if suffix in BINARY_DELIVERABLE_EXT:
        # 二进制成品只能由确定性工具产出，模型不能拿一段文字冒充。
        # 血泪实例：compose 阶段模型把「（此处为二进制视频文件，已通过渲染工具生成…）」
        # 写进了 video/final.mp4（156 字节），而验收标准只查"文件存在且非空"，
        # 于是 render_video 的真实失败被这个假文件盖过去、阶段还报了 completed。
        raise ValueError(
            f"{path} 是二进制成品（{suffix}），write_artifact 只能写文本/JSON。"
            "图片请用 generate_images、配音请用 synthesize_lines、成片请用 render_video 产出；"
            "工具失败时请如实上报并写清失败原因，不要写占位文字顶替成品。"
        )
    rel, size = ctx.storage.write_text(ctx.run_id, path, content)
    return {"path": rel, "size": size, "kind": kind}


async def _list_artifacts(ctx: ToolContext) -> dict:
    return {"files": ctx.storage.list_files(ctx.run_id)}


async def _read_artifact(ctx: ToolContext, path: str, max_chars: int = 8000) -> dict:
    """读回一份已产出的产物（上游清单 / 剧本 / 规格 / 分镜脚本）。

    为什么必须补这只手：在此之前模型只有 list_artifacts（**只有文件名、没有内容**），
    看不到上游清单内容时它会"自己重建一份"并覆盖原文件 —— 这正是 2026-10-16
    任务 db0874408b 失败的触发点。给它一只能读的手，比让它猜更便宜也更安全。
    """
    rel = str(path).strip().lstrip("./")
    if not ctx.storage.exists(ctx.run_id, rel):
        return {
            "error": f"文件不存在：{rel}",
            "available": ctx.storage.list_files(ctx.run_id)[:80],
            "hint": "先用 list_artifacts 确认真实路径。**不要凭猜测重建上游文件**，"
                    "读不到就如实上报，让验收标准判上游未完成。",
        }
    try:
        txt = ctx.storage.read_text(ctx.run_id, rel)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{rel} 不是可读文本（{type(exc).__name__}）",
            "hint": "二进制产物（图片/音频/视频）不能当文本读，用 list_artifacts 看列表即可。",
        }
    return {
        "path": rel,
        "content": txt[:max_chars],
        "size_chars": len(txt),
        "truncated": len(txt) > max_chars,
    }


async def _record_decision(
    ctx: ToolContext, what: str, why: str, alternatives: list | None = None
) -> dict:
    """决策留痕（MRD §7.3.4）。工具只上报，落库交给 runner 注入的 sink。"""
    alts = alternatives or []
    ctx.notify_decision(what, why, alts)
    return {
        "_decision": True,
        "what": what,
        "why": why,
        "alternatives": alts,
    }


async def _placeholder_image(
    ctx: ToolContext, path: str, caption: str = "", ratio: str = "9:16"
) -> dict:
    """M0 占位图像：生成 SVG 分镜框，保证链路可跑、产物可见。

    替代方案（failure_modes）见注册处：真实生图不可用时降级为线框，
    但**绝不静默降级** —— 事件流会明确标注这是占位图。
    """
    w, h = (1080, 1920) if ratio == "9:16" else (1920, 1080)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#1b2440"/>
      <stop offset="100%" stop-color="#0e1424"/>
    </linearGradient>
  </defs>
  <rect width="{w}" height="{h}" fill="url(#g)"/>
  <rect x="60" y="60" width="{w-120}" height="{h-120}" fill="none" stroke="#4f6bff" stroke-width="4" stroke-dasharray="24 18" rx="24"/>
  <text x="{w/2}" y="{h/2}" fill="#dbe4ff" font-size="64" font-family="sans-serif" text-anchor="middle">分镜占位</text>
  <text x="{w/2}" y="{h/2+96}" fill="#7e8bb5" font-size="40" font-family="sans-serif" text-anchor="middle">{caption[:24]}</text>
  <text x="{w/2}" y="{h-120}" fill="#5a6district" font-size="32" font-family="sans-serif" text-anchor="middle">M0 placeholder · {ratio}</text>
</svg>"""
    rel, size = ctx.storage.write_text(ctx.run_id, path, svg)
    return {"path": rel, "size": size, "kind": "image", "is_placeholder": True}


# --------------------------------------------------------------------------- #
# 媒体工具（M1）：生图 / 配音 / 合成
#
# 这三个工具**不由模型在对话里调用**（当前 provider 没有 function-calling 通道），
# 而是由 manifest 的 `stages[].actions` 在模型交出结构化清单后**确定性执行**：
#     模型负责 What（每格画什么、每句怎么念）→ 工具负责 How（调服务、编码、渲染）。
# 于是 v1.1 架构铁律继续成立：流程与执行都在数据里，Python 里没有流程逻辑。
#
# 三个工具都**幂等**：产物已存在则复用，只补缺失的。
# 这条性质很关键 —— 「草稿 → 继续优化」时上游阶段不重跑，靠它把没铺的格子补齐。
# --------------------------------------------------------------------------- #
IMAGE_INDEX_REL = "media/images.json"
AUDIO_INDEX_REL = "media/audio.json"

# 工具自有的**记账文件**：模型既不该写、也不该"帮忙写"。
# 它们是幂等复用的唯一依据 —— 改了 media/images.json 就等于谎报"这张图已经有了"，
# 或者反过来让已经画好的 12 张图被重新生成、重复计费。
# 血泪实例（2026-10-16，任务 db0874408b 续跑）：模型把"我读过索引"写成了
# `read_artifact:media/images.json`，正是想碰这类文件。
TOOL_OWNED_REL = frozenset({
    IMAGE_INDEX_REL, AUDIO_INDEX_REL, "video/render_props.json",
})


def _read_json_file(ctx: ToolContext, rel: str) -> dict | list | None:
    if not ctx.storage.exists(ctx.run_id, rel):
        return None
    try:
        return json.loads(ctx.storage.read_text(ctx.run_id, rel))
    except Exception as exc:  # noqa: BLE001
        logger.warning("清单文件解析失败 %s: %s", rel, exc)
        return None


def _write_index(ctx: ToolContext, rel: str, obj: Any) -> None:
    ctx.storage.write_json(ctx.run_id, rel, obj)


def _item_index(item: dict, fallback: int) -> str:
    # id 放在最后兜底：panels.json 常写成 {"id": 1}，没有 index/panel 字段。
    # 不认 id 就只能靠枚举序号，一旦清单顺序变了就会错位。
    v = item.get("index", item.get("panel", item.get("no", item.get("id"))))
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return str(fallback)


def _line_key(item: dict, ordinal: int) -> str:
    """**句级身份** —— 音轨索引、配音文件、时间轴对齐都以它为准。

    血泪实例（2026-10-16，任务 db0874408b 续跑后验收）：
    lines.json 的 `panel` 是**分组字段**（一格可以有多句台词），旧实现却拿
    `_item_index()` 当句号用 —— 它优先读 `panel`，于是同一格的 2、3 句算出**同一个键**，
    第 2 句开始全被判成"已存在，复用"直接跳过：12 格 17 句，只有每格首句有配音，
    **6 句台词全程静音、却照样有字幕**（成片看起来"正常"，听感却缺词）。
    格号与句号是两种身份，必须分开取。
    """
    v = item.get("index", item.get("line_no", item.get("lineNo", item.get("no", item.get("id")))))
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return str(ordinal)


def _panel_key(item: dict, fallback: int = 0) -> str:
    """台词**所属格号**（只用于分组/定位，不用于身份）。

    与 `_line_key` 分家的原因同上：`_item_index` 优先读 `index`，
    一旦 lines.json 同时给出 `index`（句号）与 `panel`（格号），
    拿 `_item_index` 查格就会错位。
    """
    v = item.get("panel", item.get("panel_index", item.get("panelIndex")))
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return _item_index(item, fallback)


def _plan_items(plan: Any, key: str) -> tuple[list, str | None]:
    """宽容解析阶段清单，返回 (条目列表, 形状纠正说明)。

    为什么必须宽容（2026-10-16 血泪实例，任务 db0874408b 因此整轮失败）：
    上游模型可能把清单写成**裸数组**（`[{...}, {...}]`），
    而旧实现写的是 `(plan or {}).get(key)` —— 遇到 list 直接抛
    `'list' object has no attribute 'get'`。一个 Python 属性错误让合成阶段
    连续 5 次重做都在同一行崩掉，最终阶段硬失败、成片永远出不来。

    清单文件是人写的、也是模型写的，"格式刚好符合预期"不能当作前提。
    这里同时接受三种形态：`{key: [...]}`、`{items|data|list: [...]}`、裸数组。
    note 非空表示发生过形状纠正，**必须如实上报**（禁止静默降级，架构铁律 #2）。
    """
    if isinstance(plan, dict):
        v = plan.get(key)
        if isinstance(v, list):
            return v, None
        for alt in ("items", "data", "list", "records"):
            v = plan.get(alt)
            if isinstance(v, list):
                return v, f"清单没有 {key} 键，已按 {alt} 键解析"
        return [], None
    if isinstance(plan, list):
        return plan, f"清单是裸数组，已按 {key} 列表解析（上游建议写成 {{\"{key}\": [...]}}）"
    return [], None


# --------------------------------------------------------------------------- #
# 场景卡（PRD FR-02）：场景锚点由工具确定性注入，不靠模型自觉
#
# 为什么是工具注入而不是让模型在 prompt 里复用：
#   角色锚点必须由模型写（它要接本格动作），所以只能"校验"；
#   场景锚点是**整段冻结的背景描述**，与动作无关，可以由工具无脑拼在末尾。
#   能机器保证的事，绝不写成提示词约定 —— 这是 MRD §0.2 的直接执行。
# --------------------------------------------------------------------------- #
SCENE_CARDS_REL = "scenes.json"


def _read_scene_map(ctx: ToolContext) -> dict[str, dict]:
    """读 scenes.json → {scene_id: {anchor, name}}；读不到返回空字典（由校验器负责报错）。"""
    from .consistency import _scene_payload

    raw = _read_json_file(ctx, SCENE_CARDS_REL)
    return _scene_payload(raw) if raw is not None else {}


def _apply_scene_anchor(
    panel: dict, prompt: str, scene_map: dict[str, dict], key: str
) -> tuple[str, str | None]:
    """把该格所属场景的冻结背景描述拼到提示词末尾。返回 (新提示词, 留痕说明)。"""
    sid = str(panel.get("scene_id") or panel.get("sceneId") or "").strip()
    if not sid:
        return prompt, None
    anchor = str((scene_map.get(sid) or {}).get("anchor") or "").strip()
    if not anchor:
        return prompt, f"第 {key} 格引用的场景「{sid}」在 scenes.json 里不存在或缺少 anchor，未注入场景描述"
    if anchor[:40] in prompt:
        return prompt, None
    return f"{prompt}. Scene (fixed backdrop, do not change): {anchor}", None


# --------------------------------------------------------------------------- #
# 人物一致性（FR-03/FR-04）：定妆图 + 参考图映射
#
# 为什么需要这里：跨格换脸根因是「生图只吃文字 prompt，没有参考图」。
# 京东云 image-01 支持 subject_reference（人物主体参考，传单人人脸照片做图生图）。
# 定妆图 → 参考图 → 逐格人脸一致，这是从根上解决换脸的路径。
# --------------------------------------------------------------------------- #
_ANCHOR_TAG_RE = re.compile(r"\b([A-Za-z0-9_\-]+)_ANCHOR\b")


def _anchor_tags(prompt: str) -> list[str]:
    """从 prompt 里识别锚点标签（CHENMO_ANCHOR / L7_ANCHOR …）。

    panels.json 的 prompt 约定以 `XXX_ANCHOR: ...` 标注该格用了哪些角色。
    这里提取标签，用于：① 映射角色定妆图做参考图；② 清理 prompt 里的标签前缀。
    """
    return [m.group(1).upper() for m in _ANCHOR_TAG_RE.finditer(prompt or "")]


def _strip_anchor_tags(prompt: str) -> str:
    """去掉 prompt 里的 `XXX_ANCHOR:` 标签前缀。

    血泪点：真实产物里 prompt 写成了 `CHENMO_ANCHOR: A 30-year-old man ...`，
    这个标签文字会直接喂给生图模型，被当成画面内容的一部分（实测会让生成图
    出现莫名其妙的英文残字或构图偏移）。锚点标签是**给机器看的路标**，不是
    画面描述，必须在送生图前剥掉。
    """
    return re.sub(r"\b[A-Za-z0-9_\-]+_ANCHOR\s*[:：]\s*", "", prompt or "").strip()


def _character_art_rel(role: str) -> str:
    """角色的定妆图产物相对路径。role 用锚点标签的小写形式（如 chenmo → characters/chenmo.jpg）。"""
    safe = re.sub(r"[^a-z0-9_\-]", "", (role or "").lower())
    return f"characters/{safe or 'unknown'}.jpg"


# 定妆图的固定风格强化条款（comic 技能下注入，2026-10-22）。
# 定妆图是全片 subject_reference 的源头：源头半写实，逐格参考只会把写实带进每一格。
# 与 image.ensure_style_anchor 的通用锚互补 —— 那层是「缺了才补」的校验，
# 这层是定妆场景下**无条件**注入的强约束（老人/皱纹/深肤色等描述写实拉力强，必须压住）。
CHARACTER_ART_STYLE = (
    "Flat 2D anime character design, thick clean outlines, uniform flat cel shading, "
    "simplified anime face with large expressive eyes, no realistic skin texture, no pores, "
    "no watercolor rendering, no painterly brushwork, no photorealistic face, "
    "stylized 2D illustration only"
)


def _read_character_art(storage: Any, run_id: str, role: str) -> str | None:
    """找某个角色的定妆图绝对路径；不存在返回 None。"""
    rel = _character_art_rel(role)
    if not storage.exists(run_id, rel):
        return None
    return str(storage.abs_path(run_id, rel))


def _find_reference_art(ctx: Any, prompt: str) -> tuple[str | None, str | None, list[str]]:
    """为某一格找到参考图（定妆图）。返回 (绝对路径 | None, 角色标识 | None, 降级说明)。

    三级查找（2026-10-21 真实验证教训：只有第 1 级时参考图会静默失联）：
    1. 按锚点标签猜文件名（LINYUANZHOU → characters/linyuanzhou.jpg）；
    2. **内容匹配**：C4 提取锚点时角色名可能是中文（苏晚/林远舟），文件名只能落成
       anchor_1.jpg / anchor_2.jpg；而 C5 prompt 里是模型自造的拼音标签
       `LINYUANZHOU_ANCHOR: <锚点原文>`。两者对不上时，用「prompt 里的锚点原文」
       与 characters.md 的锚点值做相似度匹配，定位到 anchor_N.jpg。
    3. 兜底：任意一张已有定妆图（总比没有强），并如实记录降级。
    """
    notes: list[str] = []
    tags = _anchor_tags(prompt)

    # 第 1 级：标签 → 文件名
    for tag in tags:
        art = _read_character_art(ctx.storage, ctx.run_id, tag)
        if art:
            return art, tag, notes

    # 第 2 级：内容匹配（锚点原文出现在 prompt 里 → 找到对应 anchor_N 定妆图）。
    # 注意：**不依赖标签** —— 真实验证发现模型可能完全不写标签、直接把锚点原文
    # 抄进 prompt 开头，此时内容匹配是唯一的定位手段。
    chars_md = ""
    try:
        if ctx.storage.exists(ctx.run_id, "characters.md"):
            chars_md = ctx.storage.read_text(ctx.run_id, "characters.md") or ""
    except Exception:  # noqa: BLE001
        chars_md = ""
    if chars_md:
        from .consistency import extract_anchors

        anchors = extract_anchors(chars_md)
        # 取最后一个标签之后的文本段（该段即锚点原文 + 动作描述）；无标签时用全文
        if tags:
            seg = re.split(r"\b[A-Za-z0-9_\-]+_ANCHOR\s*[:：]\s*", prompt)[-1]
        else:
            seg = prompt
        best_key, best_score = None, 0.0
        for key, val in anchors.items():
            if not val:
                continue
            # 探针匹配：锚点可能被画风锚挤到 prompt 后半段（前缀相似度会落空），
            # 改用「锚点前几个逗号子句」在 prompt 里找子串。锚点原文逐字复用时必然命中。
            chunks = [c.strip() for c in val.split(",") if c.strip()]
            score = 0.0
            for take in (3, 2, 1):
                probe = ", ".join(chunks[:take])
                if len(probe) >= 20 and probe in seg:
                    # score 用探针绝对长度：探针 ≥20 字符且逐字命中，已是「逐字复用」的
                    # 强证据；不能按探针/锚点全长的比例设阈值（长锚点会被稀释成假阴性）。
                    score = float(len(probe))
                    break
            if score > best_score:
                best_key, best_score = key, score
        if best_key and best_score >= 20:
            art = _read_character_art(ctx.storage, ctx.run_id, best_key)
            if art:
                if tags and best_score < 0.95:
                    notes.append(f"参考图按内容匹配（相似度 {best_score:.2f}）定位到 {best_key}")
                elif not tags:
                    notes.append(f"prompt 未写锚点标签，参考图按锚点内容匹配定位到 {best_key}")
                return art, best_key, notes

    # 没有任何标签且内容也匹配不上 → 视为空镜，不带参考图
    if not tags:
        return None, None, notes

    # 第 3 级：兜底 —— 任意一张已有定妆图
    try:
        files = ctx.storage.list_files(ctx.run_id)
    except Exception:  # noqa: BLE001
        files = []
    arts = sorted(f for f in files if f.startswith("characters/") and f.endswith((".jpg", ".png")))
    if arts:
        notes.append(
            f"参考图未按标签（{','.join(tags)}）或内容匹配定位，已兜底使用 {arts[0]}"
        )
        return str(ctx.storage.abs_path(ctx.run_id, arts[0])), arts[0], notes

    notes.append(f"识别到角色标签（{','.join(tags)}）但没有任何定妆图可用，本格未带参考图")
    return None, None, notes


async def _generate_images(
    ctx: ToolContext, plan: dict | None = None, fast_limit: int | None = None
) -> dict:
    """按 panels.json 逐格生成分镜图（真实生图不可用则降级为程序化分镜卡，并如实标注）。"""
    from ..media.image import COMIC_STYLE_SKILLS, ensure_style_anchor, generate_image

    panels, shape_note = _plan_items(plan, "panels")
    if not panels:
        return {
            "error": "清单里没有 panels 数组",
            "expected": '{"panels":[{"index":1,"prompt":"画面提示词","ratio":"9:16"}]}',
            "hint": "上游必须先产出 panels.json；缺它说明本阶段未完成，应由验收标准判失败并重做。",
        }
    if fast_limit and ctx.extra.get("channel") == "fast":
        panels = panels[: int(fast_limit)]

    index: dict[str, dict] = _read_json_file(ctx, IMAGE_INDEX_REL) or {}
    scene_map = _read_scene_map(ctx)
    created: list[str] = []
    reused: list[str] = []
    degraded: list[str] = []
    if shape_note:
        degraded.append(f"panels.json {shape_note}")

    # 阶段级模型覆盖（画布上单独指定）—— 只影响本次执行，不动全局配置
    img_cfg = ctx.extra.get("image_config") or {} if isinstance(ctx.extra, dict) else {}
    model_ov = str(img_cfg.get("model") or "").strip() or None
    service_ov = str(img_cfg.get("service") or "").strip() or None

    for i, p in enumerate(panels, 1):
        if not isinstance(p, dict):
            continue
        key = _item_index(p, i)
        prompt = str(
            p.get("prompt") or p.get("image_prompt") or p.get("scene") or p.get("description") or ""
        ).strip()
        ratio = str(p.get("ratio") or "9:16")
        prev = index.get(key) or {}
        if prev.get("path") and ctx.storage.exists(ctx.run_id, prev["path"]):
            reused.append(prev["path"])
            continue
        if not prompt:
            degraded.append(f"第 {key} 格缺少画面提示词，已跳过（不算完成）")
            continue

        # 人物一致性（FR-04）：识别该格用到的角色锚点标签，映射定妆图做参考图。
        # 三级查找（标签文件名 → 锚点内容匹配 → 任意定妆图兜底），见 _find_reference_art。
        # 京东云 subject_reference 当前只支持 1 张参考图 → 多角色格取第一个命中的角色。
        subject_ref, ref_role, ref_notes = _find_reference_art(ctx, prompt)
        degraded.extend(ref_notes)
        # 锚点标签是机器路标，不是画面内容 —— 送生图前剥掉，避免污染生成结果
        clean_prompt = _strip_anchor_tags(prompt) or prompt

        # 风格锚校验（FR-04 补充）：漫剧类技能的生图提示词缺「反写实 / 角色风格化」
        # 条款就在开头补齐并留痕 —— 模型漏写不等于允许写实漂移（2026-10-20 实测教训）。
        style_note = None
        if str(ctx.extra.get("skill_id") or "") in COMIC_STYLE_SKILLS:
            clean_prompt, style_note = ensure_style_anchor(clean_prompt)
            if style_note:
                degraded.append(f"第 {key} 格：{style_note}")

        # 场景锚点：确定性注入（PRD FR-02）。不注入 = 每格重新描述背景 = 世界漂移。
        clean_prompt, scene_note = _apply_scene_anchor(p, clean_prompt, scene_map, key)
        if scene_note:
            degraded.append(scene_note)

        out = await generate_image(
            ctx.storage, ctx.run_id, clean_prompt, f"panels/panel-{i:02d}.svg", ratio,
            model_override=model_ov, service_override=service_ov,
            subject_reference=subject_ref,
        )
        out["index"] = key
        # 留痕：记录**实际送进模型**的提示词（含自动补齐的风格锚 + 场景锚，
        # 以及审核拦截后的软化版本）。generate_image 已写入它时不覆盖 ——
        # 软化后的 prompt 才是"这格为什么长这样"的真相；没有它就只能靠猜。
        out.setdefault("prompt_used", clean_prompt[:1200])
        if scene_note:
            out["scene_note"] = scene_note
        if ref_role:
            out["subject_reference_role"] = ref_role
        # 关键：**合并**进已有条目，不能整体覆盖 —— 条目里可能已经有人工补的
        # 中间帧 / 尾帧 / 视频片段，直接 index[key]=out 会把它们静默抹掉。
        entry = dict(index.get(key) or {})
        frames = dict(entry.get("frames") or {}) if isinstance(entry.get("frames"), dict) else {}
        frames["first"] = out
        entry.update(out)
        entry["frames"] = frames
        index[key] = entry
        created.append(out["path"])
        if out.get("is_placeholder"):
            degraded.append(
                f"{out['path']} 为占位分镜卡（{out.get('fallback_reason', '')[:120]}）"
            )

    _write_index(ctx, IMAGE_INDEX_REL, index)
    return {
        "created": created,
        "reused": reused,
        "total_indexed": len(index),
        "placeholders": [v["path"] for v in index.values() if v.get("is_placeholder")],
        "degraded": degraded,
        "summary": f"分镜图：新生成 {len(created)} 张、复用 {len(reused)} 张",
    }


async def _generate_panel_videos(ctx: ToolContext, plan: dict | None = None) -> dict:
    """FR-06：**全格视频化 + 链式首尾帧** —— 每一格都生成真实的模型视频段。

    效果优先策略（2026-10-21 用户拍板「先不考虑成本」）：

    - 全格视频化：不再只做首/中/尾三个关键格。9 格静态 Ken Burns 夹 3 段真视频
      的「真动假动交替」是成片不连贯的最大来源（实测归因）。
    - 链式首尾帧：第 i 格的片段以**本格分镜图**为 first_frame、
      **下一格分镜图**为 last_frame —— 片段结尾恰好是下一格的开场画面，
      播到下一格时画面无缝续上，从根上消除格间跳变。
      两端锚点都是静态分镜图，所以 12 段**互相独立、可并行**（限流 3 路），
      不需要「等上一段渲完抽尾帧」的串行 24 分钟。
    - 提示词带上下文：本格动作 + 下一格场景写进同一段提示词，
      并按官方要求在文字里「指名」首帧/尾帧素材（光传不提，模型不会真参考）。
    - 幂等：只复用带 `chained` 标记且文件在的片段 —— 旧的关键格片段没有尾帧锚点，
      复用会让链断在半路，宁可重新生成。
    - 降级诚实：某格失败退回静态运镜并如实上报；断口由渲染端 crossfade 兜底，
      不让一格失败拖死整条链。
    """
    from ..canvas_store import set_panel_video
    from ..media.video import generate_clip, video_ready

    panels, shape_note = _plan_items(plan, "panels")
    if not panels:
        return {"error": "清单里没有 panels 数组，无法视频化", "hint": "依赖 storyboard 的 panels.json。"}

    ok, why = video_ready()
    if not ok:
        return {"degraded": [f"视频模型不可用，全部格退回静态运镜：{why}"], "generated": 0, "reused": 0}

    images: dict[str, dict] = _read_json_file(ctx, IMAGE_INDEX_REL) or {}
    workspace = ctx.storage.run_workspace(ctx.run_id)

    # 按清单顺序取格号；只有带分镜图的格才进链条（链的锚点就是分镜图）
    ordered: list[tuple[str, dict]] = []
    for i, p in enumerate(panels, 1):
        if not isinstance(p, dict):
            continue
        key = _item_index(p, i)
        img = images.get(key) or {}
        frame_path = str(img.get("path") or "").strip()
        if not frame_path or not ctx.storage.exists(ctx.run_id, frame_path):
            continue
        ordered.append((key, {"panel": p, "frame": frame_path}))

    if not ordered:
        return {"degraded": ["没有任何格带分镜图，无法视频化"], "generated": 0, "reused": 0}

    chain_enabled = not (isinstance(plan, dict) and plan.get("chain_frames") is False)

    generated: list[str] = []
    reused: list[str] = []
    degraded: list[str] = []
    chained_flags: dict[str, bool] = {}  # 本轮每格是否带尾帧锚（从生成结果记，不读过期快照）
    if shape_note:
        degraded.append(f"panels.json {shape_note}")

    async def _one(pos: int) -> None:
        key, item = ordered[pos]
        p = item["panel"]
        img = images.get(key) or {}
        clip = img.get("video") if isinstance(img.get("video"), dict) else {}
        # 复用条件：ready + 文件在 + （链式模式下）片段本身是带尾帧锚点生成的。
        # 旧的关键格片段没有 chained 标记 —— 它的结尾不是下一格开场，复用必跳变。
        chained_ok = (not chain_enabled) or bool(clip.get("chained"))
        if (
            str(clip.get("status") or "") == "ready"
            and clip.get("path")
            and chained_ok
            and ctx.storage.exists(ctx.run_id, clip["path"])
        ):
            reused.append(key)
            chained_flags[key] = bool(clip.get("chained"))
            return

        base_prompt = str(
            p.get("prompt") or p.get("image_prompt") or p.get("scene") or ""
        ).strip() or "让画面产生自然的镜头运动，保持人物与画风一致"

        # 链式尾帧：本格片段必须收在下一格的开场画面上
        last_frame: str | None = None
        if chain_enabled and pos + 1 < len(ordered):
            next_key, next_item = ordered[pos + 1]
            next_scene = str(
                next_item["panel"].get("scene") or next_item["panel"].get("prompt") or ""
            ).strip()
            last_frame = next_item["frame"]
            prompt = (
                f"{base_prompt}\n\n"
                "首帧是本镜头的开场画面。镜头完成上述动作后，"
                f"结尾平滑过渡并定格到尾帧画面（下一镜头：{next_scene[:120]}）；"
                "首帧与尾帧是同一连贯镜头的起点与落点，人物身份、服装与画风全程一致。"
            )[:1200]
        else:
            prompt = f"{base_prompt}\n\n画面从首帧开始，动作自然完整，保持人物与画风一致。"[:1200]

        # 审核/网关有随机性：同一张图此前过审、这次被拦的实例真实存在
        # （火山内容审核对半写实人脸的判定不稳定）。每格最多 3 次尝试，
        # 全失败才退回静态 —— 重试是廉价的，静默放弃一格动效才是贵的。
        out: dict[str, Any] | None = None
        last_err: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2.0 * attempt)
            try:
                out = await generate_clip(
                    workspace,
                    prompt=prompt,
                    first_frame=item["frame"],
                    last_frame=last_frame,
                    reference_video=None,
                    duration=5,
                    mode=None,
                    out_rel=f"panels/panel-{key}.mp4",
                    model=None,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("格 %s 视频化第 %s 次失败 run=%s: %s", key, attempt + 1, ctx.run_id, exc)
        if out is not None:
            out.update({"status": "ready", "prompt": prompt[:300], "chained": bool(last_frame)})
            set_panel_video(ctx.storage, ctx.run_id, key, out)
            generated.append(key)
            chained_flags[key] = bool(last_frame)
        else:
            degraded.append(f"第 {key} 格视频生成失败（重试 3 次），退回静态运镜：{str(last_err)[:200]}")

    # 限流并行：每段独立提交（锚点都是静态分镜图，无依赖），3 路并发
    # 是对网关限流与下游排队的安全余量。
    sem = asyncio.Semaphore(3)

    async def _worker(pos: int) -> None:
        async with sem:
            await _one(pos)

    await asyncio.gather(*(_worker(i) for i in range(len(ordered))))

    chained_count = sum(1 for v in chained_flags.values() if v)
    return {
        "panels": [k for k, _ in ordered],
        "generated": generated,
        "reused": reused,
        "degraded": degraded,
        "chain": {
            "enabled": chain_enabled,
            "chained_clips": chained_count,
            "static_fallback": len(ordered) - len(generated) - len(reused),
        },
        "summary": (
            f"全格视频化（链式首尾帧）：新生成 {len(generated)} 段、复用 {len(reused)} 段、"
            f"静态兜底 {len(ordered) - len(generated) - len(reused)} 格（共 {len(ordered)} 格）"
        ),
    }


async def _generate_character_art(ctx: ToolContext, plan: dict | None = None) -> dict:
    """FR-03：为每个角色生成定妆图（人物参考图），落盘 characters/{角色}.jpg。

    定妆图是后续 C5 逐格生图的**参考图来源**（subject_reference），从根上解决换脸。
    - 输入：characters.md（含各角色的「一致性锚点」冻结字符串）
    - 每个角色用其锚点原文作为 prompt 生一张定妆图（正面、半身、画风锚可选）
    - 幂等：定妆图已存在则复用，不重复生（也不重复计费）
    - 降级诚实：真实生图不可用 → 程序化分镜卡 + 标注，参考图通道随之降级为「仅锚点文字」

    plan 参数预留（当前从 characters.md 直接提取锚点，不依赖额外清单）。
    """
    from ..media.image import COMIC_STYLE_SKILLS, ensure_style_anchor, generate_image
    from .consistency import extract_anchors, is_character_anchor

    anchors_rel = "characters.md"
    if not ctx.storage.exists(ctx.run_id, anchors_rel):
        return {
            "error": "缺少 characters.md（角色设计未完成），无法生成定妆图",
            "hint": "上游角色设计阶段必须先产出 characters.md 与一致性锚点。",
        }
    try:
        characters_md = ctx.storage.read_text(ctx.run_id, anchors_rel)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取 characters.md 失败：{exc}"}

    anchors = extract_anchors(characters_md)
    # 双保险：非角色锚点（全局画风锚点/纯场景描述）混进来会生成「无主定妆图」
    # —— 模型自由发挥画出与故事无关的人物（2026-10-22 实测：画风锚点被画成
    # 机能风少女），照常计费、污染角色库，还会被参考图兜底逻辑误用。留痕跳过。
    skipped_noncharacter: list[str] = []
    for key in [k for k in anchors if not is_character_anchor(anchors[k])]:
        skipped_noncharacter.append(f"{key}（锚点不含人物特征，疑似全局画风锚点）")
        del anchors[key]
    if not anchors:
        return {
            "error": (
                "characters.md 里没有可生成定妆图的角色锚点"
                + (f"（{len(skipped_noncharacter)} 个锚点因不含人物特征被跳过）" if skipped_noncharacter else "")
            ),
            "hint": "角色设计阶段必须为每个角色给出「一致性锚点」冻结字符串（见 C4 prompt）。",
            "skipped": skipped_noncharacter,
        }

    img_cfg = ctx.extra.get("image_config") or {} if isinstance(ctx.extra, dict) else {}
    model_ov = str(img_cfg.get("model") or "").strip() or None
    service_ov = str(img_cfg.get("service") or "").strip() or None

    created: list[str] = []
    reused: list[str] = []
    degraded: list[str] = []
    details: list[dict] = []
    if skipped_noncharacter:
        degraded.append(
            "跳过非角色锚点（不生成无主定妆图）：" + "、".join(skipped_noncharacter)
        )
    style_gate = str(ctx.extra.get("skill_id") or "") in COMIC_STYLE_SKILLS
    for role, anchor in anchors.items():
        rel = _character_art_rel(role)
        if ctx.storage.exists(ctx.run_id, rel):
            reused.append(rel)
            continue
        # 定妆图 prompt：锚点原文 + 「正面半身、单人人像、干净背景」的定妆指令，
        # 保证参考图质量（subject_reference 建议单人人脸正面照）。
        prompt = (
            f"{anchor}. Character design sheet, front-facing bust portrait of this single "
            "character, neutral clean background, centered composition, consistent style."
        )
        if style_gate:
            # 定妆图固定注入强化平涂条款（2026-10-22 补，不只「缺了才补」）：
            # 定妆图是全片 subject_reference 的源头，源头一写实，人物一致性全盘漂。
            # 「65岁老人+深皱纹」这类描述对模型的写实拉力极强，靠锚点校验补齐的
            # 通用锚压不住（实测老渔夫定妆图生成半写实人像）—— 必须在定妆指令里
            # 直接写死平涂赛璐璐 + 反纹理渲染的强约束。
            prompt = (
                f"{CHARACTER_ART_STYLE}. {prompt}"
            )
            # 风格锚门禁（与分镜生图同款，2026-10-22 补）：锚点原文可能带写实画种词
            # （实测 "Thick oil painting style, realistic texture" 生出半写实人像定妆图，
            # 与平涂分镜图风格分裂、还过不了视频审核）—— 必须过同一个中和 + 补齐逻辑。
            style_note = None
            prompt, style_note = ensure_style_anchor(prompt)
            if style_note:
                degraded.append(f"{role} 定妆图：{style_note}")
        out = await generate_image(
            ctx.storage, ctx.run_id, prompt, rel, ratio="9:16",
            model_override=model_ov, service_override=service_ov,
        )
        created.append(out["path"])
        # 留痕：定妆图实际用的 prompt（含强化条款 + 风格锚补齐）——
        # 「这张参考图为什么长这样」必须可复核，不能只靠猜。
        details.append({
            "role": role,
            "path": out["path"],
            "prompt_used": str(out.get("prompt_used") or prompt)[:1200],
        })
        if out.get("is_placeholder"):
            degraded.append(
                f"{out['path']} 为占位定妆图（{out.get('fallback_reason', '')[:120]}），"
                "参考图通道已降级为「仅锚点文字」"
            )

    result = {
        "created": created,
        "reused": reused,
        "degraded": degraded,
        "roles": list(anchors.keys()),
        "summary": f"角色定妆图：新生成 {len(created)} 张、复用 {len(reused)} 张（共 {len(anchors)} 个角色）",
    }
    if details:
        result["details"] = details
    return result


async def _synthesize_lines(ctx: ToolContext, plan: dict | None = None) -> dict:
    """按 lines.json 逐句合成配音（无真实 TTS 则用本机语音，并如实标注）。"""
    from ..media.tts import synthesize_speech

    lines, shape_note = _plan_items(plan, "lines")
    if not lines:
        return {
            "error": "清单里没有 lines 数组",
            "expected": '{"lines":[{"index":1,"text":"台词","role":"female","rate_wpm":190}]}',
            "hint": "上游必须先产出 lines.json；缺它说明本阶段未完成。",
        }

    # 旧的 media/audio.json 是**格级**键（键=格号），与新的句级键（键=句号）会在
    # '2'..'12' 这一段重合 —— 若直接按句级键去取，第 2 句会捞到第 2 格首句的音，
    # 变成"听起来有声音、念的却是别人的词"，这类错最难发现。
    # 所以只复用带 `__line_scope__` 标记的条目（即本次修复后写下的索引）；
    # 历史格级条目一律不认，按句重建一份。
    prev_index: dict[str, dict] = _read_json_file(ctx, AUDIO_INDEX_REL) or {}
    reusable = {
        k: v for k, v in prev_index.items()
        if isinstance(v, dict) and v.get("__line_scope__")
    }
    legacy_count = len(prev_index) - len(reusable)

    index: dict[str, dict] = {}
    created: list[str] = []
    reused: list[str] = []
    degraded: list[str] = []
    if shape_note:
        degraded.append(f"lines.json {shape_note}")
    if legacy_count:
        degraded.append(
            f"media/audio.json 旧索引是格级键（{legacy_count} 条，只覆盖每格首句），"
            "已按句级键重建：同格多句此前只有首句有声音，其余句静音。"
        )

    for i, ln in enumerate(lines, 1):
        if not isinstance(ln, dict):
            continue
        key = _line_key(ln, i)
        text = str(ln.get("text") or ln.get("line") or ln.get("content") or "").strip()
        role = str(ln.get("role") or ln.get("speaker_role") or "female").lower()
        try:
            rate = int(ln.get("rate_wpm") or ln.get("rate") or 190)
        except (TypeError, ValueError):
            rate = 190
        if key in index:
            # 同一个句号出现两次 —— 不猜，如实上报，让上游修清单。
            degraded.append(f"第 {key} 句在 lines.json 里出现多次，后一条已忽略")
            continue
        prev = reusable.get(key) or {}
        if prev.get("path") and ctx.storage.exists(ctx.run_id, prev["path"]):
            index[key] = prev
            reused.append(prev["path"])
            continue
        if not text:
            degraded.append(f"第 {key} 句缺少台词文本，已跳过（不算完成）")
            continue
        out = await synthesize_speech(
            ctx.storage, ctx.run_id, text, f"audio/line-{i:02d}.m4a",
            role=role, rate_wpm=rate,
        )
        out["index"] = key
        out["__line_scope__"] = True  # 标记：本条目是**句级**键，可与历史格级键区分
        index[key] = out
        created.append(out["path"])
        if out.get("is_fallback"):
            degraded.append(f"{out['path']} 由本机语音合成（{out.get('fallback_reason', '')[:120]}）")

    _write_index(ctx, AUDIO_INDEX_REL, index)
    return {
        "created": created,
        "reused": reused,
        "total_indexed": len(index),
        "total_duration_seconds": round(
            sum(float(v.get("duration_seconds") or 0) for v in index.values()), 2
        ),
        "degraded": degraded,
        "summary": f"配音：新合成 {len(created)} 句、复用 {len(reused)} 句",
    }


def _norm_text(s: Any) -> str:
    """去空白与标点后的归一化文本，用于台词↔字幕的互含匹配。"""
    return re.sub(r"[\s，。！？…、,.!?；;：:\"'“”‘’（）()\[\]【】《》—\-·]+", "", str(s or ""))


def _match_lines_to_panels(
    lines: list, panels: list
) -> tuple[dict[str, list[tuple[str, dict]]], list[str]]:
    """把台词句映射到所属格。返回 ({格号: [(句键, 句对象)]}, 匹配过程说明)。

    三级策略（从可信到兜底）：
    1. 句里显式写了 panel 字段 → 直接用；
    2. 台词文本与 panels.json 的 subtitle 互含（去标点后）→ 按文本匹配；
    3. 都没有 → **插空**：按叙事顺序落进「已锚定句」之间的空格里
       （文本完全匹配上的句是锚点；未匹配句只会分给它前后锚点之间的格，
       绝不从第一个格重新数起 —— 否则中间一句没匹配上会整体前移错位）。

    为什么绝不做「句号 = 格号」：lines.json 的 index 是**句号**不是格号，
    模型不写 panel 时拿 index 当格号会把台词整体错位（实测 19eaa 任务
    9 句 index 从 3 起，按 index 映射全偏）。身份键分离是这里的地基。
    """
    notes: list[str] = []

    # 面板顺序位置表（格号 → 在播放顺序里的下标）
    pos_of: dict[str, int] = {}
    sub_titles: list[tuple[str, int, str]] = []  # (格号, 位置, 归一化字幕)
    for i, p in enumerate(panels, 1):
        if not isinstance(p, dict):
            continue
        pk = _item_index(p, i)
        pos_of.setdefault(pk, len(pos_of))
        t = _norm_text(p.get("subtitle") or p.get("line") or p.get("dialogue"))
        if t:
            sub_titles.append((pk, pos_of[pk], t))

    # 第一遍：显式格号 / 文本互含 → 锚点；其余进 unmatched（保持句序）
    assigned: list[list] = []  # [句序, 句键, 句对象, 格号或 None]
    n_unmatched = 0
    for i, ln in enumerate(lines, 1):
        if not isinstance(ln, dict):
            continue
        lk = _line_key(ln, i)
        row = [i, lk, ln, None]
        pv = ln.get("panel", ln.get("panel_index", ln.get("panelIndex")))
        try:
            row[3] = str(int(pv))
            assigned.append(row)
            continue
        except (TypeError, ValueError):
            pass
        nt = _norm_text(ln.get("text") or ln.get("line") or ln.get("content"))
        if len(nt) >= 4:
            for pk, _pos, st in sub_titles:
                if nt in st or st in nt:
                    row[3] = pk
                    break
        if row[3] is None:
            n_unmatched += 1
        assigned.append(row)

    n_explicit = sum(1 for r in assigned if r[3] and r[3] and _has_panel_field(r[2]))
    n_text = sum(1 for r in assigned if r[3]) - n_explicit

    # 第二遍：未锚定的句按叙事顺序插进前后锚点之间的空格
    if n_unmatched:
        if not sub_titles:
            # 连字幕都没有：均匀摊到所有格（保底，不丢句）
            keys = list(pos_of.keys())
            if keys:
                j = 0
                for r in assigned:
                    if r[3] is None:
                        r[3] = keys[min(j, len(keys) - 1)]
                        j += 1
                notes.append(f"{n_unmatched} 句无任何可匹配锚点，已均匀摊到 {len(keys)} 个格")
        else:
            used_in_gap: set[int] = set()  # 插空已占用的面板位置（按区间重置）
            prev_pos: int | None = None    # 前一个锚点的面板位置
            filled = 0
            for r in assigned:
                if r[3] is not None:
                    prev_pos = pos_of.get(r[3], prev_pos)
                    used_in_gap = set()
                    continue
                nxt_pos = next(
                    (pos_of[r2[3]] for r2 in assigned if r2[0] > r[0] and r2[3]), None
                )
                lo = (prev_pos + 1) if prev_pos is not None else 0
                hi = nxt_pos if nxt_pos is not None else len(pos_of)
                cand = [
                    (pk, pos) for pk, pos, _ in sub_titles
                    if lo <= pos < hi or (nxt_pos is None and pos == hi - 1)
                ]
                cand = [(pk, pos) for pk, pos in cand if pos not in used_in_gap] or cand
                if cand:
                    pk = cand[0][0]
                    used_in_gap.add(cand[0][1])
                    r[3] = pk
                    filled += 1
                else:
                    # 锚点之间没有空格可用：贴到前一个锚点的格上（宁可同格多句，不丢句）
                    if prev_pos is not None:
                        back = next(pk for pk, pos, _ in sub_titles if pos == prev_pos)
                    elif nxt_pos is not None:
                        back = next(
                            r2[3] for r2 in assigned if r2[0] > r[0] and r2[3]
                        )
                    else:
                        back = sub_titles[0][0]
                    r[3] = back
                    filled += 1
            notes.append(
                f"{n_unmatched} 句无显式格号且文本未完全匹配，已按叙事顺序插到锚点之间的格"
                if filled else "全部台词均按显式格号/文本匹配定位"
            )

    mapping: dict[str, list[tuple[str, dict]]] = {}
    for _seq, lk, ln, pk in assigned:
        if pk:
            mapping.setdefault(pk, []).append((lk, ln))
    if n_explicit:
        notes.append(f"{n_explicit} 句按显式 panel 字段映射，{n_text} 句按字幕文本匹配")
    return mapping, notes


def _has_panel_field(ln: dict) -> bool:
    v = ln.get("panel", ln.get("panel_index", ln.get("panelIndex")))
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


async def _render_video(ctx: ToolContext, plan: dict | None = None) -> dict:
    """把分镜图 + 配音 + 字幕排成时间轴，渲染竖屏 mp4。

    时间轴由这里排（合成业务的一部分），渲染交给 Remotion 工程（纯渲染器）。
    素材缺失的格**直接剔除并标注**，绝不渲染"假片"（宁缺毋假）。
    """
    from ..media.compose import render_video as _render_impl

    panels, shape_note = _plan_items(plan, "panels")
    if not panels:
        return {
            "error": "清单里没有 panels 数组，无法合成",
            "hint": "合成阶段依赖 storyboard 阶段的 panels.json。",
        }

    images: dict[str, dict] = _read_json_file(ctx, IMAGE_INDEX_REL) or {}
    audio_index: dict[str, dict] = _read_json_file(ctx, AUDIO_INDEX_REL) or {}
    lines, _ = _plan_items(_read_json_file(ctx, "lines.json"), "lines")

    # 台词→格 映射：三级匹配（显式 panel 字段 → 字幕文本互含 → 顺序分配）。
    # 旧实现 `_panel_key(ln, 0)` 在句里没写 panel 时会退回「句号当格号」，
    # 台词整体错位一格 —— 配音听起来"对得上但总是慢半拍"就是这么来的。
    mapping, mapping_notes = _match_lines_to_panels(lines, panels)
    line_to_panel: dict[str, str] = {
        lk: pk for pk, items in mapping.items() for lk, _ in items
    }
    # 字幕兜底：panels.json 若没带 subtitle 字段，就从映射到的台词里取。
    line_by_panel: dict[str, str] = {}
    for pk, items in mapping.items():
        for _, ln in items:
            txt = str(ln.get("text") or ln.get("line") or ln.get("content") or "").strip()
            if not txt:
                continue
            # 同一格的多句要全部保留（用 " / " 连接），不能再只取首句 ——
            # 否则同格第 2 句起"有声音没字幕/有字幕没声音"，字幕与配音对不上。
            line_by_panel[pk] = f"{line_by_panel[pk]} / {txt}" if pk in line_by_panel else txt

    title = str(
        (plan.get("title") if isinstance(plan, dict) else None)
        or ctx.brief.get("idea") or "Longspan 漫剧"
    ).strip()[:60]

    spec_panels: list[dict] = []
    skipped: list[str] = []
    freeze_notes: list[str] = []
    frame_stats = {"multi": 0, "single": 0, "video": 0}
    clip_retimed: list[str] = []

    # ---- 内容驱动时间轴 ----
    # 旧逻辑：12 格 × 5s 均分（panels.json 的规划值），配音时长完全不参与排版，
    # 声画"对得上但贴不紧"。现在：每格时长 = max(视频片段长, 配音实际需求)，
    # 片段长优先 —— 链式首尾帧的接缝要求片段完整播完。
    from ..media.compose import TRANSITION_SECONDS

    OVERLAP = TRANSITION_SECONDS  # 格间 crossfade 重叠：下一格提前入画，叠化接力
    LEAD = 0.35  # 每格内第一句在格起点之后 0.35s 开口，别贴脸
    GAP = 0.15   # 句与句之间的呼吸
    TAIL = 0.55  # 台词说完后画面再留的呼吸

    # 每格的配音实际需求时长（LEAD + 各句时长 + 句间 GAP + TAIL）
    audio_need: dict[str, float] = {}
    for pk, items in mapping.items():
        durs = []
        for lk, _ in items:
            c = audio_index.get(lk) or {}
            if c.get("path") and ctx.storage.exists(ctx.run_id, c["path"]):
                try:
                    durs.append(float(c.get("duration_seconds") or 1.5))
                except (TypeError, ValueError):
                    durs.append(1.5)
        if durs:
            audio_need[pk] = LEAD + sum(durs) + GAP * (len(durs) - 1) + TAIL

    # 时间轴起点：片头卡让出大部分时长，但提前 OVERLAP 入画，
    # 让格 1 的画面在片头卡淡出的同时叠化进来（intro 卡自己带淡出）。
    intro_dur = 2.0
    t = max(0.0, intro_dur - OVERLAP)
    starts: dict[str, float] = {}
    panel_dur: dict[str, float] = {}
    for i, p in enumerate(panels, 1):
        if not isinstance(p, dict):
            continue
        key = _item_index(p, i)
        img = images.get(key) or {}
        if not img.get("path") or not ctx.storage.exists(ctx.run_id, img["path"]):
            skipped.append(f"第 {key} 格无分镜图")
            continue
        try:
            dur = float(
                p.get("duration_seconds") or p.get("durationInSeconds") or p.get("duration") or 3.0
            )
        except (TypeError, ValueError):
            dur = 3.0
        dur = max(1.0, dur)

        # 多帧：按 首→中→尾 的顺序带给渲染器。缺失的帧直接丢掉（不占位、不猜）。
        frame_paths: list[str] = []
        raw_frames = img.get("frames") if isinstance(img.get("frames"), dict) else {}
        for role in ("first", "mid", "last"):
            item = raw_frames.get(role) if isinstance(raw_frames.get(role), dict) else {}
            fp = str(item.get("path") or "").strip()
            if not fp and role == "first":
                fp = str(img.get("path") or "")
            if fp and ctx.storage.exists(ctx.run_id, fp) and fp not in frame_paths:
                frame_paths.append(fp)
        if not frame_paths:
            frame_paths = [str(img["path"])]
        if len(frame_paths) > 1:
            frame_stats["multi"] += 1
        else:
            frame_stats["single"] += 1

        # 视频片段：只有真的下载完成（status=ready）且文件在，才用它。
        # 生成中的任务不能进成片 —— 否则渲染器拿到不存在的路径会整片失败。
        clip = img.get("video") if isinstance(img.get("video"), dict) else {}
        clip_rel = str(clip.get("path") or "").strip()
        video_rel = ""
        if str(clip.get("status") or "") == "ready" and clip_rel and ctx.storage.exists(ctx.run_id, clip_rel):
            video_rel = clip_rel
            frame_stats["video"] += 1
            # 片段长度由模型决定（5s / 10s）。链式接缝要求片段**完整播完**
            # （结尾定格在下一格开场画面），所以有视频时以片段长为基准。
            try:
                clip_dur = float(clip.get("duration") or 0)
            except (TypeError, ValueError):
                clip_dur = 0.0
            if clip_dur:
                if abs(clip_dur - dur) > 0.6:
                    clip_retimed.append(f"第 {key} 格 {dur:.1f}s → {clip_dur:.1f}s（跟随视频片段）")
                dur = clip_dur
        else:
            clip_dur = 0.0

        # 配音需求参与排版：说没说完不由规划值决定，由音频真实时长决定。
        need = audio_need.get(key, 0.0)
        if video_rel:
            if need > dur + 0.05:
                # 配音比片段长：格子延长，片段播完后定格在尾帧（= 下一格开场画面，
                # 视觉上仍是连续的），留痕说明而不是静默拉长。
                dur = need
                freeze_notes.append(f"第 {key} 格配音 {need:.1f}s 超出片段 {clip_dur:.1f}s，尾部定格")
        elif need > dur:
            dur = need
        dur = round(dur, 3)

        starts[key] = round(t, 3)
        panel_dur[key] = dur
        spec_panels.append({
            "src": img["path"],
            "frames": frame_paths,
            "video": video_rel or None,
            "fromSeconds": starts[key],
            "durationInSeconds": dur,
            "motion": str(p.get("motion") or p.get("camera") or "zoom-in").lower(),
            "subtitle": str(
                p.get("subtitle") or p.get("line") or p.get("dialogue")
                or line_by_panel.get(key) or ""
            ).strip() or None,
            "speaker": str(p.get("speaker") or "").strip() or None,
            "label": f"格 {key}",
            "isPlaceholder": bool(img.get("is_placeholder")),
        })
        # 下一格提前 OVERLAP 入画：本格尾与下格头重叠叠化（crossfade 接力）。
        t += dur - OVERLAP

    if not spec_panels:
        return {"error": "全部格都没有分镜图，无法合成", "hint": "先生成分镜图（generate_images）。"}

    # 配音对齐：同一格里有多句台词时，第 n 句**接在第 n-1 句之后**，不再同时开口。
    # 血泪实例（2026-10-16，任务 db0874408b）：格级键导致同格多句共用一条音轨、
    # 又都从格起点同时起播 —— 听感上第 2 句起"有字幕没声音"。
    spec_audio: list[dict] = []
    audio_missing: list[str] = []
    audio_overflow: list[str] = []
    cursor: dict[str, float] = {}
    for i, ln in enumerate(lines, 1):
        if not isinstance(ln, dict):
            continue
        key = _line_key(ln, i)
        clip = audio_index.get(key) or {}
        if not clip.get("path") or not ctx.storage.exists(ctx.run_id, clip["path"]):
            # 以前这里直接 continue —— 台词凭空消失且无人知晓。现在必须留痕。
            audio_missing.append(f"第 {key} 句")
            continue
        pk = line_to_panel.get(key)
        if pk is None:
            audio_missing.append(f"第 {key} 句（三级匹配都没能定位所属格）")
            continue
        base = starts.get(pk)
        if base is None:
            audio_missing.append(f"第 {key} 句（找不到所属格 {pk}，该格可能缺图被剔除）")
            continue
        offset = cursor.get(pk, LEAD)
        dur_panel = panel_dur.get(pk, 0.0)
        try:
            clip_dur = float(clip.get("duration_seconds") or 1.5)
        except (TypeError, ValueError):
            clip_dur = 1.5
        if dur_panel and offset + clip_dur > dur_panel + 0.05:
            # 句长超出本格时长：夹住起点，让最后一句落在格内（宁可挤，不越格串到下一画面）
            offset = max(LEAD, dur_panel - clip_dur - 0.05)
            audio_overflow.append(f"第 {key} 句（格 {pk} 装不下，已贴格尾对齐）")
        spec_audio.append({
            "src": clip["path"],
            "from": round(base + offset, 3),
            "volume": 1.0,
            # 音轨真实时长带给渲染器：Sequence 精确收口，不再靠"自然长度"漂移
            "durationInSeconds": round(clip_dur, 3),
            "speaker": str(ln.get("role") or "").strip() or None,
        })
        cursor[pk] = offset + clip_dur + GAP

    spec = {
        "title": title,
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "transitionSeconds": OVERLAP,
        "intro": {"durationInSeconds": intro_dur, "title": title, "subtitle": "动态漫 · Longspan 合成"},
        "panels": spec_panels,
        "audio": spec_audio,
        "bgm": None,
        "showWatermark": True,
        "outro": {"durationInSeconds": 2.4, "text": "第一集 · 完", "hint": "由 Longspan 合成"},
    }

    out = await _render_impl(ctx.run_id, ctx.storage.run_workspace(ctx.run_id), spec)
    degraded = list(out.get("degraded") or [])
    degraded.extend(f"{s}（已从成片中剔除，未用假素材顶替）" for s in skipped)
    if audio_missing:
        degraded.append(
            "以下台词没有配音音轨，成片里这几句是静音的（未用占位音顶替）："
            + "、".join(audio_missing)
        )
    if audio_overflow:
        degraded.append("以下台词因所在格时长不足已贴格尾对齐：" + "、".join(audio_overflow))
    if freeze_notes:
        degraded.append("以下格的配音长于视频片段，片段播完后定格到尾帧：" + "、".join(freeze_notes))
    degraded.extend(f"台词映射：{n}" for n in mapping_notes)
    if shape_note:
        degraded.append(f"panels.json {shape_note}")
    if clip_retimed:
        degraded.append("以下格的时长按视频片段长度调整：" + "、".join(clip_retimed))
    subtitle_count = sum(1 for p in spec_panels if p.get("subtitle"))
    if subtitle_count == 0:
        degraded.append("成片无任何字幕：panels.json 与 lines.json 都没有可用的台词文本")
    return {
        **out,
        "degraded": degraded,
        "skipped_panels": skipped,
        "audio_clips": len(spec_audio),
        "subtitle_panels": subtitle_count,
        "media_usage": frame_stats,
        "summary": (
            f"成片已渲染：{out['path']}（{out['duration_seconds']}s / {out['frames']} 帧 / "
            f"{out['size'] / 1024 / 1024:.1f}MB / 字幕 {subtitle_count} 格 / "
            f"多帧 {frame_stats['multi']} 格 / 视频 {frame_stats['video']} 格）"
        ),
    }


def register_builtin_tools() -> None:
    registry.register(
        ToolSpec(
            name="read_brief",
            description="读取用户本次任务的输入（硬约束与软偏好）。",
            when_to_use="阶段开始时先了解用户到底要什么；不确定某项约束时用它复核。",
            inputs={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "可选，只取某个键"}},
            },
            outputs="brief 对象",
            cost_hint="free",
            handler=_read_brief,
        )
    )
    registry.register(
        ToolSpec(
            name="write_artifact",
            description="把一个产物写入本次任务的工作目录。",
            when_to_use="产出了确定的文本/结构产物（剧本、大纲、分镜脚本等）后必须调用它落盘。",
            inputs={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 script.md"},
                    "content": {"type": "string"},
                    "kind": {"type": "string", "description": "md/json/script 等"},
                },
                "required": ["path", "content"],
            },
            outputs="{path,size}",
            cost_hint="free",
            failure_modes=[
                {
                    "case": "路径冲突或非法字符",
                    "fallback": "改写为安全文件名后重试一次；仍失败则把内容放入事件流并标注未落盘",
                }
            ],
            handler=_write_artifact,
        )
    )
    registry.register(
        ToolSpec(
            name="list_artifacts",
            description="列出当前任务已产出的所有文件。",
            when_to_use="需要复用自己的早期产物、或检查上游是否齐全时。",
            cost_hint="free",
            handler=_list_artifacts,
        )
    )
    registry.register(
        ToolSpec(
            name="read_artifact",
            description="读回一份已产出产物的**内容**（上游清单 JSON / 剧本 / 规格等文本）。",
            when_to_use=(
                "需要复用上游结论时（例如合成阶段要读 panels.json、lines.json、spec.json）。"
                "**看不到内容就不要动手写** —— 先读，读不到就如实上报上游缺失。"
            ),
            inputs={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "产物相对路径，如 panels.json"},
                    "max_chars": {"type": "integer", "description": "最多读取字符数，默认 8000"},
                },
                "required": ["path"],
            },
            outputs="{path,content,size_chars,truncated}",
            cost_hint="free",
            failure_modes=[
                {
                    "when": "文件不存在",
                    "fallback": "用 list_artifacts 确认真实路径；仍无则如实上报上游未完成，"
                                "不要自己重建一份覆盖它（会被拒绝写入）",
                },
                {"when": "是二进制产物", "fallback": "只能看列表，不能读内容"},
            ],
            handler=_read_artifact,
        )
    )
    registry.register(
        ToolSpec(
            name="record_decision",
            description="记录一个自主决策（做了什么决定、依据是什么、备选是什么）。",
            when_to_use="当你替用户在多个可行方案中做了选择时必须调用；用户不写步骤，但必须看得到你的判断。",
            inputs={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "决定了什么"},
                    "why": {"type": "string", "description": "依据"},
                    "alternatives": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "备选方案与置信度",
                    },
                },
                "required": ["what", "why"],
            },
            outputs="决策记录",
            cost_hint="free",
            handler=_record_decision,
        )
    )
    registry.register(
        ToolSpec(
            name="placeholder_image",
            description="生成一张分镜占位图（M0 为 SVG 线框，M1 换成真实 AI 生图）。",
            when_to_use="需要分镜画面但真实生图不可用、或处于草稿/抽样阶段时。",
            inputs={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "caption": {"type": "string"},
                    "ratio": {"type": "string", "description": "9:16 或 16:9"},
                },
                "required": ["path"],
            },
            outputs="{path,is_placeholder}",
            cost_hint="free",
            failure_modes=[
                {
                    "case": "真实生图被内容审核拦截",
                    "fallback": "自动改写提示词重试；连续失败则退回 placeholder_image 并明确标注，禁止静默降级",
                },
                {
                    "case": "生图服务额度不足",
                    "fallback": "降级为抽样模式（只生成前 3 格），并在决策留痕中说明",
                },
            ],
            handler=_placeholder_image,
        )
    )
    # ---- 媒体工具（M1）：由 stages[].actions 确定性调用，不由模型在对话里调用 ----
    registry.register(
        ToolSpec(
            name="generate_character_art",
            description="按 characters.md 为每个角色生成定妆图（人物参考图），落盘 characters/{角色}.jpg。定妆图是后续逐格生图的参考图来源，用于保证人物跨格一致。",
            when_to_use="阶段动作：characters 阶段在模型交出 characters.md 之后执行。幂等，已生成的定妆图会复用。",
            inputs={
                "type": "object",
                "properties": {"plan": {"type": "object", "description": "预留，当前从 characters.md 直接提取锚点"}},
            },
            outputs="{created[],reused[],roles[],degraded[]}",
            cost_hint="按角色数计费（真实生图）；本地兜底免费",
            failure_modes=[
                {
                    "case": "未配置生图服务或接口报错",
                    "fallback": "降级为程序化分镜卡并标注「占位」，参考图通道随之降级为「仅锚点文字」—— 禁止静默降级",
                },
                {
                    "case": "characters.md 缺锚点",
                    "fallback": "返回明确错误，让角色设计阶段补锚点（上游未完成）",
                },
            ],
            handler=_generate_character_art,
        )
    )
    registry.register(
        ToolSpec(
            name="generate_images",
            description="按 panels.json 逐格生成分镜图（竖屏）。真实生图不可用时降级为程序化分镜卡并明确标注。",
            when_to_use="阶段动作：storyboard / compose 阶段在模型交出 panels.json 之后执行。幂等，已生成的格会复用。",
            inputs={
                "type": "object",
                "properties": {
                    "plan": {"type": "object", "description": "panels.json 的内容"},
                    "fast_limit": {
                        "type": "integer",
                        "description": "快通道下只生成前 N 格（草稿只出样片），精修通道忽略此项",
                    },
                },
            },
            outputs="{created[],reused[],placeholders[],degraded[]}",
            cost_hint="按张计费（真实生图）；本地兜底免费",
            failure_modes=[
                {
                    "case": "未配置生图服务或接口报错",
                    "fallback": "降级为程序化电影感分镜卡（SVG），画面上标注「占位图」，并写入 degraded 列表 —— 禁止静默降级",
                },
                {
                    "case": "某格缺少画面提示词",
                    "fallback": "跳过该格并记入 degraded（跳过 ≠ 完成，交付时会被如实告知）",
                },
            ],
            handler=_generate_images,
        )
    )
    registry.register(
        ToolSpec(
            name="generate_panel_videos",
            description="全格视频化 + 链式首尾帧：每一格都调用图生视频模型；第 i 格片段以本格分镜图为 first_frame、下一格分镜图为 last_frame，片段结尾恰好是下一格开场，格间无缝衔接。写入 images.json 的 video 字段（带 chained 标记）供 render_video 消费。",
            when_to_use="阶段动作：compose 阶段在 render_video 之前执行。幂等：只复用带 chained 标记的 ready 片段（旧关键格片段无尾帧锚点，会重新生成）。",
            inputs={
                "type": "object",
                "properties": {"plan": {"type": "object", "description": "panels.json 的内容"}},
            },
            outputs="{panels[],generated[],reused[],chain{enabled,chained_clips,static_fallback},degraded[]}",
            cost_hint="按段计费（每格一段图生视频，约数元/段，全格化后费用 ≈ 格数 × 单价）；视频模型不可用或失败时该格退回静态运镜",
            failure_modes=[
                {
                    "case": "视频模型未配置或接口报错",
                    "fallback": "该格退回静态 Ken Burns 运镜，并写入 degraded 列表 —— 禁止静默降级",
                },
                {
                    "case": "某格缺分镜图",
                    "fallback": "跳过该格视频化，记入 degraded；链条在断口由渲染端 crossfade 兜底",
                },
            ],
            handler=_generate_panel_videos,
        )
    )
    registry.register(
        ToolSpec(
            name="synthesize_lines",
            description="按 lines.json 逐句合成配音音频。无真实 TTS 时用本机语音合成并明确标注。",
            when_to_use="阶段动作：voice / compose 阶段在模型交出 lines.json 之后执行。幂等。",
            inputs={
                "type": "object",
                "properties": {"plan": {"type": "object", "description": "lines.json 的内容"}},
            },
            outputs="{created[],reused[],total_duration_seconds,degraded[]}",
            cost_hint="按字符计费（真实 TTS）；本机语音免费",
            failure_modes=[
                {
                    "case": "未配置 TTS 服务",
                    "fallback": "使用本机神经语音合成（macOS say），产物照常可听，但如实标注音色有限",
                },
                {
                    "case": "本机语音也不可用（非 macOS）",
                    "fallback": "抛出明确错误；合成阶段改为无配音成片并标注「本版无配音」，不静默出哑片",
                },
            ],
            handler=_synthesize_lines,
        )
    )
    registry.register(
        ToolSpec(
            name="render_video",
            description="把分镜图 + 配音 + 字幕排成时间轴并渲染竖屏 mp4（Remotion）。",
            when_to_use="阶段动作：compose 阶段最后一步执行。素材缺失的格会被剔除并在 degraded 中说明。",
            inputs={
                "type": "object",
                "properties": {"plan": {"type": "object", "description": "panels.json 的内容"}},
            },
            outputs="{path,duration_seconds,frames,size,degraded[]}",
            cost_hint="本地渲染，仅耗机器时间",
            failure_modes=[
                {
                    "case": "Remotion 渲染失败或超时",
                    "fallback": "抛出带渲染日志尾部的错误；保留 edit_plan.md，本阶段判为未完成并给出可重试路径",
                },
                {
                    "case": "部分格缺素材",
                    "fallback": "剔除缺失格后渲染（宁缺毋假），并把剔除项写入 degraded",
                },
            ],
            handler=_render_video,
        )
    )


register_builtin_tools()
