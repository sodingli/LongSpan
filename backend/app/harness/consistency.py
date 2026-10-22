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


# --------------------------------------------------------------------------- #
# 演出约束（directing.shot_budget）：镜头预算与场景卡
#
# 背景（MRD v1.4 §0.2 + PRD-演出约束与一致性分层）：
#   跨格一致性最便宜的杠杆不是"更强的模型"，而是"别让模型每一格都重新建世界"。
#   传统动画用有限动画（limited animation）解决过同一个问题：少机位、同场景、
#   遮挡转场、单格单动作。这些是**可机器校验的结构约束**，因此放进这里。
#
# 设计原则（与锚点校验一致）：
#   - 只判结构，不判审美。"这一格好不好看"不归这里管。
#   - 缺字段 = 契约未满足，判失败并给出**第几格缺什么**（模型能在重做时照做）。
#   - 宽容解析：panels.json/scenes.json 是模型写的，形状不能当作前提。
# --------------------------------------------------------------------------- #

_PANEL_LIST_KEYS = ("panels", "items", "data", "list", "records")
_SCENE_LIST_KEYS = ("scenes", "items", "data", "list", "locations")

# 景别归一：模型可能写中文/英文/带修饰（"全景 · 跟拍推进"），全部映射到四档。
_SHOT_SIZE_ALIASES: dict[str, str] = {
    "wide": "wide", "远景": "wide", "大远景": "wide", "全景": "wide",
    "establishing": "wide", "long shot": "wide", "full shot": "wide",
    "medium": "medium", "中景": "medium", "中近景": "medium", "mid shot": "medium",
    "close": "close", "近景": "close", "特写": "close", "close-up": "close",
    "closeup": "close", "bust": "close",
    "extreme-close": "extreme-close", "大特写": "extreme-close",
    "extreme close": "extreme-close", "macro": "extreme-close",
}

# 一格只演一个动作：出现这些连接词 = 两个动作挤在一格里（模型只能挑一个或画中间态）。
_MULTI_ACTION_MARKERS = (
    "and then", "then he", "then she", "after that", "while also",
    "然后", "接着", "同时", "并且", "随后", "，又", "；", ";",
)


def _extract_items(payload: Any, keys: tuple[str, ...]) -> list[dict]:
    """宽容解析清单：接受 {key:[...]} / {items|data|list|records:[...]} / 裸数组。"""
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def normalize_shot_size(raw: Any) -> str | None:
    """把景别描述归一成 wide / medium / close / extreme-close；认不出返回 None。

    匹配按别名**从长到短**：'extreme-close' 必须先于 'close' 判定，
    否则 'extreme-close' 里的子串 'close' 会抢先命中，把大特写误归成近景
    （实测冤枉了合规分镜：close/extreme-close/close 被报成三连 close）。
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    for alias, canon in sorted(_SHOT_SIZE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in text:
            return canon
    return None


def _panel_no(p: dict, i: int) -> str:
    return str(p.get("index") or p.get("panel") or p.get("id") or i)


def _panel_scene_id(p: dict) -> str:
    """场景号只认显式字段。

    绝不回退到 `scene` 字段：那是**画面描述**（"全景 · 跟拍推进"），
    拿它当场景号会让每一格都成了"新场景"—— 与"格号≠句号"同类的错误。
    """
    return str(p.get("scene_id") or p.get("sceneId") or p.get("scene_key") or "").strip()


def _panel_action(p: dict) -> str:
    return str(p.get("action") or p.get("beat") or "").strip()


def _panel_motion(p: dict) -> str:
    return str(p.get("motion") or "").strip().lower()


def _scene_payload(scenes_json: Any) -> dict[str, dict]:
    """scenes.json → {scene_id: {anchor, name}}，宽容解析。"""
    out: dict[str, dict] = {}
    for item in _extract_items(scenes_json, _SCENE_LIST_KEYS):
        sid = str(item.get("id") or item.get("scene_id") or "").strip()
        if not sid:
            continue
        out[sid] = {
            "anchor": str(item.get("anchor") or item.get("description") or "").strip(),
            "name": str(item.get("name") or "").strip(),
        }
    return out


def validate_shot_budget(
    panels_json: Any, budget: dict | None = None
) -> tuple[bool, list[str]]:
    """镜头预算硬校验：场景收敛 / 禁三连换场 / 景别节奏 / 单格单动作 / 运动白名单。

    返回 (是否通过, 逐条原因)。原因里必须带**格号**——"不合格"三个字没法指导重做。
    """
    b = dict(budget or {})
    max_scenes = int(b.get("max_distinct_scenes") or 0)
    max_cuts = int(b.get("max_scene_cuts") or 0)
    forbid_adjacent_same = bool(b.get("forbid_adjacent_same_shot", False))
    aba_rule = bool(b.get("aba_rule", False))
    one_action = bool(b.get("one_action_per_panel", False))
    primary_ratio = float(b.get("primary_scene_ratio") or 0)
    motion_whitelist = {
        "zoom-in", "zoom-out", "pan-left", "pan-right", "pan-up", "pan-down",
        "tilt-up", "tilt-down", "static",
    }

    panels = _extract_items(panels_json, _PANEL_LIST_KEYS)
    if not panels:
        return False, ["panels.json 里没有可校验的 panels 数组"]

    failures: list[str] = []
    scenes: list[str] = []
    shots: list[str | None] = []
    missing_scene: list[str] = []
    missing_action: list[str] = []
    missing_motion: list[str] = []

    for i, p in enumerate(panels, 1):
        no = _panel_no(p, i)
        sid = _panel_scene_id(p)
        if not sid:
            missing_scene.append(no)
        scenes.append(sid)
        shots.append(normalize_shot_size(p.get("shot_size") or p.get("scene")))

        if one_action:
            act = _panel_action(p)
            if not act:
                missing_action.append(no)
            else:
                low = act.lower()
                hit = next((m for m in _MULTI_ACTION_MARKERS if m in low), "")
                if hit:
                    failures.append(
                        f"格 {no} 的 action 含多个动作（命中「{hit}」）：一格只演一个动作，拆成两格"
                    )

        motion = _panel_motion(p)
        if motion:
            if motion not in motion_whitelist:
                failures.append(
                    f"格 {no} 的 motion「{motion}」不在白名单（{', '.join(sorted(motion_whitelist))}）"
                )
        elif b.get("require_motion"):
            missing_motion.append(no)

    if missing_action:
        failures.append(
            "以下格缺少 action 字段：" + "、".join(missing_action[:12])
            + "（单格单动作：每格必须写明只演哪一个动作，否则模型会自己塞两个）"
        )

    if missing_motion:
        failures.append(
            "以下格缺少 motion 字段：" + "、".join(missing_motion[:12])
            + "（镜头怎么动必须显式声明，静止也是一种决策）"
        )

    if missing_scene:
        failures.append(
            "以下格缺少 scene_id：" + "、".join(missing_scene[:12])
            + "（必须引用 scenes.json 里的场景号，否则每格都会重建一次世界）"
        )

    valid_scenes = [s for s in scenes if s]
    if valid_scenes:
        distinct: list[str] = []
        for s in valid_scenes:
            if s not in distinct:
                distinct.append(s)
        if max_scenes and len(distinct) > max_scenes:
            failures.append(
                f"用了 {len(distinct)} 个场景（{', '.join(distinct)}），超出预算 {max_scenes} 个"
                "（场景越多，模型重建世界的次数越多，一致性越差）"
            )

        cuts = sum(1 for a, b2 in zip(scenes, scenes[1:]) if a and b2 and a != b2)
        if max_cuts and cuts > max_cuts:
            failures.append(f"场景切换 {cuts} 次，超出预算 {max_cuts} 次")

        if aba_rule:
            for i in range(len(scenes) - 2):
                tri = scenes[i:i + 3]
                if all(tri) and len(set(tri)) == 3:
                    failures.append(
                        f"格 {i + 1}→{i + 3} 连续三次换场（{tri[0]}→{tri[1]}→{tri[2]}），"
                        "必须改成 A-B-A（回到上一个场景），否则一致性误差会一路累积"
                    )

        if primary_ratio > 0:
            # 必须按「在整集里出现的次数」取主场景。
            # 注意：distinct 里每个场景只出现一次，拿 distinct.count 当 key 会让所有场景
            # 计数都是 1 → max 退化成"取第一个"→ 报出的主场景是错的（实测踩过：
            # 明明 S1 占 8 格，却报"S2 只占 8%"）。计数要在 scenes 里数。
            main = max(distinct, key=lambda s: scenes.count(s))
            ratio = scenes.count(main) / max(len(scenes), 1)
            if ratio < primary_ratio:
                counts = {s: scenes.count(s) for s in distinct}
                failures.append(
                    f"主场景 {main} 只占 {ratio:.0%}（要求 ≥ {primary_ratio:.0%}）："
                    "一集应把大部分镜头留在同一个场景里；当前分布 "
                    + "、".join(f"{k}×{v}" for k, v in counts.items())
                )

    if forbid_adjacent_same:
        for i in range(len(shots) - 1):
            if shots[i] and shots[i] == shots[i + 1]:
                failures.append(
                    f"格 {i + 1} 与格 {i + 2} 同为「{shots[i]}」景别：相邻格必须换景别或合并成一格"
                )

    return (not failures), failures


def validate_scene_references(
    panels_json: Any, scenes_json: Any
) -> tuple[bool, list[str]]:
    """场景引用完整性：每格的 scene_id 必须在 scenes.json 里真实存在。

    场景锚点本身**不由模型在 prompt 里复用**，而是由生成工具确定性地拼进去
    （见 tools._scene_anchor_for）—— 能机器保证的事，不交给模型自觉。
    所以这里只校验"引用是否指向一个真实场景"，不做文本比对（避免误伤）。
    """
    scene_map = _scene_payload(scenes_json)
    if not scene_map:
        return False, [
            "scenes.json 未产出或其中没有 scenes 数组（没有场景卡，跨格背景必然每次重建）"
        ]

    panels = _extract_items(panels_json, _PANEL_LIST_KEYS)
    if not panels:
        return False, ["panels.json 里没有可校验的 panels 数组"]

    failures: list[str] = []
    unknown: list[str] = []
    no_anchor: list[str] = []
    for i, p in enumerate(panels, 1):
        no = _panel_no(p, i)
        sid = _panel_scene_id(p)
        if not sid:
            continue  # 缺 scene_id 由 shot_budget 报，避免同一问题两条噪声
        if sid not in scene_map:
            unknown.append(f"{no}→{sid}")
        elif not scene_map[sid]["anchor"]:
            no_anchor.append(sid)

    if unknown:
        failures.append(
            "以下格引用了不存在的场景号：" + "、".join(unknown[:12])
            + f"（scenes.json 里只有 {', '.join(scene_map)}）"
        )
    if no_anchor:
        failures.append(
            "以下场景卡缺少 anchor（冻结背景描述）：" + "、".join(sorted(set(no_anchor)))
            + "，这样生图时无法注入同一段背景"
        )
    return (not failures), failures
