"""Agent 循环 —— 模型负责决策（What），Harness 负责安全执行（How）。

架构原则（v1.1 修正）：
    这里**没有编排器**。循环不知道"漫剧"或"PPT"是什么，
    它只做四件事：把技能文本+上下文交给模型 → 执行模型要的工具 →
    校验数据化的验收标准 → 落盘并打检查点。
    改流程 = 改 YAML/Markdown，不改这个文件。

两种产出通道（兼容不同模型的工具调用能力）：
  ① 原生 function calling（tool_calls）
  ② 文件块约定  <<<FILE:path>>> ... <<<END>>>
两条通道最终都走同一个 write_artifact 工具，保证"工具真的被执行"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..events import EventType
from ..llm import extract_json, get_provider_for
from .tools import ToolContext, registry

logger = logging.getLogger("longspan.agent")

MAX_ITERATIONS = 6

FILE_BLOCK_RE = re.compile(
    r"<{3,}\s*FILE:\s*(?P<path>[^\n>]+?)\s*>{3,}\s*\n(?P<body>.*?)\n<{3,}\s*END\s*>{3,}",
    re.DOTALL,
)

# 宽容模式：模型写了 <<<FILE:...>>> 却漏掉 <<<END>>>（实测真实模型会这样）。
# 取到下一个文件块开始标记或文本末尾为止 —— 先保住产物，质量交给验收标准判断。
OPEN_FILE_BLOCK_RE = re.compile(
    r"<{3,}\s*FILE:\s*(?P<path>[^\n>]+?)\s*>{3,}\s*\n(?P<body>.*?)(?=<{3,}\s*(?:FILE:|END)|$)",
    re.DOTALL,
)


@dataclass
class AgentResult:
    text: str = ""
    files: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    iterations: int = 0
    raw: str = ""


def parse_file_blocks(text: str, tolerant: bool = True) -> list[tuple[str, str]]:
    """解析文件块。严格匹配优先；模型漏写结束标记时用宽容模式兜住。"""
    strict = [(m.group("path").strip(), m.group("body")) for m in FILE_BLOCK_RE.finditer(text or "")]
    if strict:
        return strict
    if not tolerant:
        return []
    return [(m.group("path").strip(), m.group("body")) for m in OPEN_FILE_BLOCK_RE.finditer(text or "")]


def is_tolerant_parse(text: str) -> bool:
    """本次解析是否用到了宽容模式（严格匹配一个都没中，但确实有文件块）。"""
    raw = text or ""
    return not FILE_BLOCK_RE.search(raw) and bool(OPEN_FILE_BLOCK_RE.search(raw))


def strip_file_blocks(text: str) -> str:
    """去掉文件块，留下给用户看的正文（事件流里展示的是人话，不是协议标记）。"""
    out = FILE_BLOCK_RE.sub("", text or "")
    out = OPEN_FILE_BLOCK_RE.sub("", out)  # 顺带清理未闭合的块，别让正文被污染
    return out.strip()


class AgentRunner:
    def __init__(self, on_event: Callable[..., None]) -> None:
        self.on_event = on_event

    async def run(
        self,
        ctx: ToolContext,
        system: str,
        user: str,
        tools_whitelist: list[str] | None = None,
        demo: str | None = None,
        json_mode: bool = False,
        stage_id: str = "",
        model_override: str | None = None,
    ) -> AgentResult:
        provider = get_provider_for(model_override)
        visible = registry.whitelist(tools_whitelist or [])
        result = AgentResult()

        self.on_event(
            EventType.AGENT_THOUGHT,
            f"开始处理阶段「{stage_id}」，可见工具 {len(visible)} 个",
            stage_id=stage_id,
            payload={
                "tools": [t.name for t in visible],
                "provider": provider.name,
                "model": getattr(provider, "model", ""),
                "model_overridden": bool(model_override and getattr(provider, "model", "") == model_override),
            },
        )

        resp = await provider.chat(system=system, user=user, demo=demo, json_mode=json_mode)
        result.raw = resp.text
        result.tokens += resp.tokens
        result.cost_usd += resp.cost_usd
        result.iterations = 1

        # 主模型不可用而自动回退时，如实告知 —— 用户必须知道"这次的产物是谁写的"
        if getattr(resp, "fallback_from", ""):
            self.on_event(
                EventType.AGENT_THOUGHT,
                f"主模型 {resp.fallback_from} 不可用，已自动回退到备用模型 {resp.model} 完成本阶段",
                stage_id=stage_id,
                payload={
                    "fallback_from": resp.fallback_from,
                    "fallback_to": resp.model,
                    "reason": getattr(resp, "fallback_reason", ""),
                },
            )

        blocks = parse_file_blocks(resp.text)
        if blocks and is_tolerant_parse(resp.text):
            self.on_event(
                EventType.AGENT_THOUGHT,
                f"模型漏写了文件块结束标记，已按宽容模式解析并落盘 {len(blocks)} 个产物（请人工留意内容完整性）",
                stage_id=stage_id,
                payload={"tolerant_parse": True, "files": [p for p, _ in blocks]},
            )
        if blocks:
            for path, body in blocks:
                self.on_event(
                    EventType.TOOL_CALL,
                    f"写入产物 {path}",
                    stage_id=stage_id,
                    payload={"tool": "write_artifact", "path": path},
                )
                out = await registry.invoke(
                    "write_artifact", ctx, path=path, content=body, kind=_guess_kind(path)
                )
                if isinstance(out, dict) and out.get("error"):
                    result.observations.append(f"写入失败 {path}: {out['error']}")
                    self.on_event(
                        EventType.TOOL_RESULT,
                        f"写入失败：{out['error']}",
                        stage_id=stage_id,
                        payload=out,
                    )
                else:
                    result.files.append(path)
                    self.on_event(
                        EventType.ARTIFACT_CREATED,
                        f"产物已生成：{path}",
                        stage_id=stage_id,
                        payload={"path": path, "stage_id": stage_id},
                    )

        # 决策留痕：模型可能把 decisions 写在正文 JSON 里，也可能写在某个产物 JSON 内
        payloads: list[dict] = []
        top = extract_json(resp.text)
        if isinstance(top, dict):
            payloads.append(top)
        for _path, body in blocks:
            inner = extract_json(body)
            if isinstance(inner, dict):
                payloads.append(inner)

        seen_what: set[str] = set()
        for payload in payloads:
            for d in payload.get("decisions", []) or []:
                if not isinstance(d, dict) or not d.get("what"):
                    continue
                key = str(d["what"])[:80]
                if key in seen_what:
                    continue
                seen_what.add(key)
                result.decisions.append(d)
                await registry.invoke(
                    "record_decision",
                    ctx,
                    what=str(d.get("what", "")),
                    why=str(d.get("why", "")),
                    alternatives=d.get("alternatives") or [],
                )
                self.on_event(
                    EventType.DECISION_RECORDED,
                    f"决策：{str(d.get('what',''))[:60]}",
                    stage_id=stage_id,
                    payload=d,
                )

        vis = strip_file_blocks(resp.text)
        if vis:
            result.text = vis
            self.on_event(
                EventType.AGENT_MESSAGE,
                vis[:600],
                stage_id=stage_id,
                payload={"full": vis},
            )

        return result

    async def call_tool(self, name: str, ctx: ToolContext, **kwargs) -> Any:
        return await registry.invoke(name, ctx, **kwargs)


def _guess_kind(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "md": "markdown",
        "json": "json",
        "svg": "image",
        "png": "image",
        "jpg": "image",
        "mp3": "audio",
        "wav": "audio",
        "mp4": "video",
        "pptx": "pptx",
        "docx": "docx",
        "txt": "text",
    }.get(ext, "text")


# --------------------------------------------------------------------------- #
# 提示词装配
# --------------------------------------------------------------------------- #
TOOL_CONTRACT_HEADER = """
## 可用工具
{schemas}

## 产出通道（必须严格遵守）
交付产物**必须**使用文件块，且开始标记与结束标记**必须成对出现**：
<<<FILE:相对路径>>>
文件内容
<<<END>>>

铁律：
- 漏写 `<<<END>>>` 会导致产物无法保存、本阶段直接判为失败并重做，白白浪费一次机会。
- 一次回复可以包含多个文件块，但每一个都必须完整闭合。
- 文件内容直接写在块内，不要把解释性文字混进块内；正文说明写在文件块之外。
- **不要在块内重复同一段内容**；一旦发现自己开始重复，立即停止并收尾。
- JSON 产物必须是**合法 JSON**：key 用双引号、不要重复写同一个 key（如 `"reason": "reason": …`）、不要尾随逗号。
- 若你替用户做了选择，调用 record_decision 记录依据，或在一个 JSON 产物里给出
  {{"decisions":[{{"what":"...","why":"...","alternatives":[...]}}]}}。

## 二进制成品不是你的活
视频（.mp4/.mov）、音频（.m4a/.mp3/.wav）、图片（.png/.jpg）、压缩包（.zip）**只能由工具产出**，
write_artifact 只写文本/JSON/Markdown。这些成品由阶段动作调用工具生成，你负责的是
"写好驱动工具的清单（如 panels.json / lines.json）与说明文件（如 edit_plan.md）"。
**绝对不要**用 write_artifact 往 video/final.mp4、panels/*.jpg 这类路径写一句"此处为二进制文件"——
那等于伪造成品，会直接被拒绝并判为失败。
工具执行失败时，如实说明失败原因，不要用文字占位顶替。

## 工具调用不写进产物目录
调用一个工具就在回复里直接调用它 —— **不要**用 write_artifact 写一个"记录我调用过工具"的文件。
血泪实例（2026-10-16，任务 db0874408b）：产物目录里先后出现了 36 字节的 `list_artifacts`
（内容"（调用工具列出文件列表）"）和 4 个 `read_artifact:panels.json`（内容"（已读取，内容见用户上下文）"）
—— 把**工具名当成了文件名**。产物目录是**交付清单**，不是你的笔记本；这些东西会被拒绝，
也会污染用户的目录表。

## 上游产物是输入契约，不是你可以重写的东西
上游阶段声明的产物（如 storyboard 的 panels.json、voice 的 lines.json）是**下游的输入契约**。
- 需要它就用 **read_artifact 读内容**；不要凭"我没看到它"就自己重建一份。
- **不允许覆写别的阶段声明的产物** —— write_artifact 会直接拒绝并告诉你归哪个阶段所有。
- 确实认为上游有错：用 record_decision 写清"错在哪、依据是什么"，然后**让上游重做**，
  或把你的修正结论写进本阶段自己的产物里。
- 你只能提交「本阶段要交付的文件」；清单文件缺字段（如每格缺 prompt）宁可如实上报，
  也不要自己补一份格式不同的版本 —— 那会让确定性执行工具读到错误数据。
""".strip()


def build_system_prompt(skill_md: str, stage_name: str, stage_desc: str,
                         review_focus: list[str], success_criteria: list[str],
                         tools_whitelist: list[str] | None) -> str:
    parts = [skill_md.strip()] if skill_md.strip() else []
    parts.append(f"\n\n# 当前阶段：{stage_name}\n{stage_desc}".strip())
    if success_criteria:
        parts.append(
            "\n\n## 本阶段验收标准（不满足会被自动打回重做）\n"
            + "\n".join(f"- {c}" for c in success_criteria)
        )
    if review_focus:
        parts.append("\n\n## 自检要点\n" + "\n".join(f"- {c}" for c in review_focus))

    schemas = "\n".join(
        json.dumps(t.to_openai_schema()["function"], ensure_ascii=False)
        for t in registry.whitelist(tools_whitelist or [])
    )
    parts.append("\n\n" + TOOL_CONTRACT_HEADER.format(schemas=schemas or "（无）"))
    return "".join(parts)
