"""人物/风格一致性校验器 —— 「一致性优先于华丽」的机器化落点。

背景（见 PRD-人物一致性与风格统一整改.md）：
    跨格画风/人物漂移几乎全部来自「模型每次重新描述角色」。SKILL.md 与 C5 prompt
    里写满了「锚点原文复用、一个字不改」的铁律，但那是**写给模型的软约束**——
    模型不遵守时，没有任何机器拦住。本模块把这条铁律变成**硬校验**：

    1. extract_anchors：从 characters.md 提取每个角色的「一致性锚点」冻结字符串；
    2. validate_panel_anchors：逐格检查 panels.json 里含角色的格，其 prompt 是否
       包含至少一个锚点原文子串；不含 → 判失败，触发自动重做。

设计原则：
    - 只做「结构/字符串」校验，不做「好看/像不像」的判断（那是多模态后检的活，P1）。
    - 校验失败给出**具体格号与原因**，不是一句「不合格」。
    - 宽容解析：characters.md 是模型写的，格式不能当作前提；锚点提取要能容忍
      代码块标记、语言前缀（如 `CHENMO_ANCHOR:`）等噪声。
"""

from __future__ import annotations

import re
from typing import Any


# --------------------------------------------------------------------------- #
# 锚点提取
# --------------------------------------------------------------------------- #

# 锚点可能写在这几种形态里，全部要能识别：
#   形态 A（冒号）：CHENMO_ANCHOR: A 30-year-old Chinese man, ...
#   形态 B（等号+引号）：CHENMO_ANCHOR = "young Chinese man, ..."
# 两种都出现在真实产物里（不同模型/不同版本写法不一），必须都兼容。
_ANCHOR_COLON_RE = re.compile(
    r"^\s*`{0,3}\s*([A-Za-z0-9_\-]+_ANCHOR)\s*[:：]\s*`{0,3}\s*(.*?)\s*`{0,3}\s*$",
    re.MULTILINE,
)
_ANCHOR_EQ_RE = re.compile(
    r"([A-Za-z0-9_\-]+_ANCHOR)\s*=\s*[\"“](.*?)[\"”]",
)

# 一致性锚点段落标题：### 一致性锚点（英文生图描述词，冻结字符串）
_ANCHOR_HEADING_RE = re.compile(r"#+\s*一致性锚点")


def _clean_anchor(text: str) -> str:
    """把锚点文本清理成「可直接当子串比对」的规范化形式。

    只做最低限度的清理：去首尾空白、压缩多余空白、去掉代码块反引号与引号残留。
    不做任何会改变原文语义的改写 —— 锚点的价值就在于「逐字复用」。
    """
    t = text.strip()
    t = t.strip("`").strip()
    # 去掉等号格式遗留的包裹引号（形态 B 的引号已在正则里剥掉，这里兜底）
    t = t.strip("\"'\"“”")
    t = re.sub(r"\s+", " ", t)
    return t


def extract_anchors(characters_md: str) -> dict[str, str]:
    """从 characters.md 提取「角色标识 → 锚点原文」映射。

    返回 {角色标识: 锚点原文}。角色标识取 `XXX_ANCHOR` 里的 XXX（CHENMO_ANCHOR → CHENMO）。
    兼容两种锚点写法：`XXX_ANCHOR: ...`（冒号）与 `XXX_ANCHOR = "..."`（等号+引号）。

    锚点原文经过 `_clean_anchor` 规范化（去空白、去引号、去反引号），但**保留原文词汇**，
    以便后续用「子串包含」校验 panels.json 里的 prompt。
    """
    text = characters_md or ""
    anchors: dict[str, str] = {}
    seen: set[str] = set()

    def _add(key: str, val: str) -> None:
        """登记一个锚点。key 是原始标签（CHENMO_ANCHOR），角色名 = 去掉 _ANCHOR 后缀。"""
        val = _clean_anchor(val)
        if not val or len(val) < 20:
            return
        role = re.sub(r"_ANCHOR$", "", key, flags=re.IGNORECASE)
        if not role or role in seen:
            return
        anchors[role] = val
        seen.add(role)
        seen.add(key)

    # 形态 B（等号+引号）优先：它能拿到精确的角色标签，且不会把前缀混进锚点内容
    for m in _ANCHOR_EQ_RE.finditer(text):
        _add(m.group(1), m.group(2))

    # 形态 A（冒号）
    for m in _ANCHOR_COLON_RE.finditer(text):
        _add(m.group(1), m.group(2))

    # 形态 C：无标签前缀的裸代码块，跟在「一致性锚点」标题后（最后兜底）
    if not anchors:
        # 注意 (?:[a-zA-Z]+\s*)? ：剥离代码块语言标记（```text / ```markdown 等）。
        # 教训（2026-10-21 真实验证）：模型常写 ```text ... ```，语言标记 "text" 会被
        # 连带抓进锚点指纹开头 → 指纹与 prompt 永远对不上 → 全量误伤、空耗重做轮次。
        blocks = re.findall(r"```(?:[a-zA-Z]+\s*)?(.*?)```", text, re.DOTALL)
        idx = 0
        for block in blocks:
            val = _clean_anchor(block)
            if len(val) < 40:
                continue
            idx += 1
            anchors[f"anchor_{idx}"] = val

    return anchors


# --------------------------------------------------------------------------- #
# panels.json 锚点校验
# --------------------------------------------------------------------------- #

# 一个 prompt 里若出现这些「无角色」标记，视为纯场景格，不强制含锚点。
# 但默认策略是：**能判定含角色就必须含锚点**，判定不了才放过。
_NO_CHARACTER_HINTS = (
    "no character", "no human", "no people", "empty scene",
    "landscape", "establishing shot", "empty room", "无人", "空镜", "远景空",
)


def _looks_characterless(prompt: str) -> bool:
    low = prompt.lower()
    return any(h in low for h in _NO_CHARACTER_HINTS)


# 正向人物特征词：出现才算「含角色」，才强制锚点。
# 教训（2026-10-21 真实验证）：旧版对「含角色」没有正向判定，纯物件特写
# （如怀表空镜）也被要求带锚点 → 误伤 → 模型空耗 3 轮重做仍改不对。
_PERSON_HINTS_RE = re.compile(
    r"\b(man|woman|girl|boy|person|people|figure|character|faces?|hair|eyes?|"
    r"smil(?:es?|ing)|standing|sitting|walking|wearing|dressed)\b"
    r"|男人|女人|女孩|男孩|少年|老人|人物|角色|他|她",
    re.IGNORECASE,
)


def _mentions_person(prompt: str) -> bool:
    """prompt 里是否有正向的人物特征（物件特写/纯风景不算）。"""
    return bool(_PERSON_HINTS_RE.search(prompt or ""))


def _anchor_substring(a: str) -> str:
    """锚点用于「子串包含」校验：取锚点前 80 字符作为指纹。

    为什么不全串比对：模型在 prompt 里复用锚点时，可能紧跟动作描述，中间没有
    明确分隔；全串比会因「锚点被动作描述截断」而误伤。取锚点开头一段足够长的
    指纹（前 80 字符），既保留「逐字复用」的约束力，又容忍正常的拼接。
    """
    return a[:80]


def validate_panel_anchors(
    panels_json: Any, anchors: dict[str, str]
) -> tuple[bool, list[str]]:
    """逐格校验 panels.json 的 prompt 是否复用了角色锚点。

    返回 (是否通过, 失败原因列表)。

    规则：
      - 没有锚点可查（characters.md 未产出）→ 视为「上游未完成」，返回失败并说明。
      - 每格 prompt 非空时，检查是否包含任一锚点指纹子串；
      - 若该格明确标注为「无角色/空镜」，豁免锚点要求（但记入 reasons 说明）；
      - 含角色却未含任何锚点 → 记失败，并给出格号与「缺哪个锚点」的提示。

    校验逻辑放在这里而非 runner，是为了保持「流程在数据里」的架构边界：
    runner 只负责在验收处调用它，不关心「什么算一致」。
    """
    reasons: list[str] = []

    if not anchors:
        return False, ["characters.md 未产出任何一致性锚点（上游角色设计未完成），无法校验分镜一致性"]

    # 宽容解析 panels.json 的形状（复用 tools.py 的容错思路：可能是 {panels:[...]} 或裸数组）
    panels: list[dict] = []
    if isinstance(panels_json, dict):
        for key in ("panels", "items", "data", "list", "records"):
            v = panels_json.get(key)
            if isinstance(v, list):
                panels = [p for p in v if isinstance(p, dict)]
                break
    elif isinstance(panels_json, list):
        panels = [p for p in panels_json if isinstance(p, dict)]

    if not panels:
        return False, ["panels.json 里没有可校验的 panels 数组"]

    fingerprints = {role: _anchor_substring(a) for role, a in anchors.items()}
    failures: list[str] = []
    for i, p in enumerate(panels, 1):
        prompt = str(
            p.get("prompt") or p.get("image_prompt") or p.get("scene") or p.get("description") or ""
        ).strip()
        if not prompt:
            # 缺 prompt 的格由 generate_images 另行判定「跳过」，这里不重复报
            continue
        if _looks_characterless(prompt):
            reasons.append(f"格 {i} 标注为无角色/空镜，豁免锚点校验")
            continue
        if not _mentions_person(prompt):
            # 无正向人物特征（物件特写/纯风景）→ 不强制锚点
            reasons.append(f"格 {i} 未检测到人物特征，按空镜豁免锚点校验")
            continue
        hit = next((role for role, fp in fingerprints.items() if fp in prompt), None)
        if hit:
            reasons.append(f"格 {i} 复用角色锚点「{hit}」")
            continue
        failures.append(f"格 {i} 的 prompt 未复用任何角色锚点（含角色却重新描述了角色，跨格会换脸）")

    if failures:
        return False, failures
    return True, reasons


def validate_panel_anchors_reason(panels_json: Any, anchors: dict[str, str]) -> str:
    """给 runner 的验收失败文本：把失败原因拼成一句话。"""
    ok, reasons = validate_panel_anchors(panels_json, anchors)
    if ok:
        return ""
    return "；".join(reasons)
