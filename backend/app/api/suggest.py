"""大模型修改建议 —— 人工介入时的「第二双眼睛」。

设计原则：
  1. 建议是**只读**的：这个模块不改任何产物，改不改、怎么改永远是人的决定；
  2. 建议是**有依据**的：上下文带角色锚点、相邻格、该阶段的 review_focus，
     不让模型凭空发挥；
  3. 尊重阶段级模型覆盖：某个阶段单独配了模型，建议也用同一个模型，
     保证「你看到的产出」和「给你提建议的」是同一个脑子。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import canvas_store as cs
from ..db import get_db
from ..harness.skill import registry
from ..llm import extract_json, get_provider_for
from ..models import Run
from ..store import storage

logger = logging.getLogger("longspan.suggest.api")

router = APIRouter(prefix="/api/runs", tags=["suggest"])

# 单份产物送进上下文的上限：再长模型就开始"概括"而不是"审阅"了
MAX_ARTIFACT_CHARS = 12_000
# 相邻格上下文只带 scene/prompt/subtitle，够判断衔接即可
_NEIGHBOR_FIELDS = ("scene", "prompt", "subtitle", "speaker")

PANEL_EDITABLE_FIELDS = ("prompt", "scene", "motion", "subtitle", "speaker")


def _require_run(db: Session, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return run


def _real_provider(model_override: str = ""):
    """建议必须有真模型。演示模式的 Mock 只会输出节目台词，装不了评审。"""
    provider = get_provider_for(model_override)
    if getattr(provider, "kind", "") != "real":
        raise HTTPException(status_code=400, detail="当前是演示模式（未配置真实模型），无法生成修改建议")
    return provider


def _run_brief(run: Run) -> dict:
    brief = getattr(run, "brief", None) or {}
    if isinstance(brief, str):
        import json

        try:
            brief = json.loads(brief)
        except Exception:  # noqa: BLE001
            brief = {}
    return brief if isinstance(brief, dict) else {}


def _read_artifact(run_id: str, rel: str, limit: int = MAX_ARTIFACT_CHARS) -> str:
    try:
        if not storage.exists(run_id, rel):
            return ""
        text = storage.read_text(run_id, rel)
    except Exception:  # noqa: BLE001
        return ""
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"\n…（已截断，原文 {len(text)} 字）"
    return text


# --------------------------------------------------------------------- 分镜单格


def _panel_key(p: dict, fallback: int) -> str:
    for field in ("index", "panel", "no", "id"):
        val = p.get(field)
        if val is None or val == "":
            continue
        try:
            return str(int(val))
        except (TypeError, ValueError):
            return str(val)
    return str(fallback)


@router.post("/{run_id}/panels/{index}/suggest")
async def suggest_panel(
    run_id: str,
    index: int,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
) -> dict:
    """给某一格出修改建议：诊断 + 可直接采纳的字段 patch。"""
    run = _require_run(db, run_id)
    doc = cs.load_panels(storage, run_id)
    panels = [p for p in (doc.get("panels") or []) if isinstance(p, dict)]
    if not panels:
        raise HTTPException(status_code=404, detail="该任务还没有分镜清单（panels.json）")

    keys = [_panel_key(p, i + 1) for i, p in enumerate(panels)]
    want = str(index)
    if want not in keys:
        raise HTTPException(status_code=404, detail=f"没有第 {index} 格（现有格号：{', '.join(keys[:24])}）")
    pos = keys.index(want)
    target = panels[pos]

    neighbors: list[dict] = []
    for j in (pos - 1, pos + 1):
        if 0 <= j < len(panels):
            neighbors.append(
                {"格号": keys[j], **{k: str(panels[j].get(k) or "")[:220] for k in _NEIGHBOR_FIELDS}}
            )

    anchors = _read_artifact(run_id, "characters.md", limit=4_000)
    note = str(payload.get("note") or "").strip()

    provider = _real_provider()
    system = (
        "你是资深动态漫分镜师，负责在人工介入环节给出**具体、可执行**的修改建议。\n"
        "你要审一格分镜：判断画面描述是否可生成、是否与角色锚点冲突、与前后格衔接是否生硬、"
        "景别/运镜是否单调、字幕是否口语化。\n"
        "输出严格的 JSON（不要 markdown 代码块），结构：\n"
        '{"diagnosis": "一句话诊断", "patch": {"prompt": "改进后的完整画面描述", "motion": "运镜", "subtitle": "字幕"},'
        ' "why": {"prompt": "改动理由", "motion": "理由", "subtitle": "理由"}}\n'
        "规则：\n"
        "- patch 只允许这些键：" + ", ".join(PANEL_EDITABLE_FIELDS) + "；没有必要的键就不要出现，**不要全部都改**；\n"
        "- prompt 必须是完整可独立使用的英文生图提示词（含角色锚点特征），不是 diff；\n"
        "- 若用户给了 note，优先满足它；\n"
        "- 改动要克制：诊断出什么才改什么。"
    )
    import json as _json

    user = _json.dumps(
        {
            "本格": {k: target.get(k) for k in ("index", "scene", "prompt", "motion", "subtitle", "speaker")},
            "相邻格": neighbors,
            "角色锚点（节选）": anchors or "（本任务没有角色卡）",
            "用户要求": note or "（无，按你的判断来）",
        },
        ensure_ascii=False,
        indent=1,
    )
    resp = await provider.chat(system=system, user=user, json_mode=True)
    data = extract_json(resp.text)
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="模型没有返回结构化建议，请重试")
    patch = data.get("patch") or {}
    if not isinstance(patch, dict):
        patch = {}
    clean = {k: str(v) for k, v in patch.items() if k in PANEL_EDITABLE_FIELDS and str(v).strip()}
    if not clean:
        raise HTTPException(status_code=502, detail=f"模型没有给出可采纳的改动：{str(data)[:200]}")

    logger.info("单格建议 run=%s 格=%s 改动=%s model=%s", run_id, index, list(clean), resp.model)
    return {
        "ok": True,
        "index": index,
        "diagnosis": str(data.get("diagnosis") or ""),
        "patch": clean,
        "why": {k: str(v) for k, v in (data.get("why") or {}).items() if isinstance(v, (str, int, float))},
        "model": resp.model,
        "fallback_from": resp.fallback_from,
    }


# --------------------------------------------------------------------- 阶段级


@router.post("/{run_id}/stages/{stage_id}/suggest")
async def suggest_stage(
    run_id: str,
    stage_id: str,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
) -> dict:
    """给某个阶段的全部文本产物出修改建议（评审人视角，只读）。"""
    run = _require_run(db, run_id)
    skill = registry.get(run.skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{run.skill_id}")
    stage = next((s for s in skill.stages if s.id == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"阶段不存在：{stage_id}")

    artifacts: list[dict] = []
    for name in stage.produces or []:
        if not name.lower().endswith(tuple(cs.EDITABLE_TEXT_EXT)):
            continue
        text = _read_artifact(run_id, name)
        if text:
            artifacts.append({"file": name, "content": text})
    if not artifacts:
        raise HTTPException(status_code=409, detail=f"阶段「{stage.name}」还没有可评审的文本产物")

    brief = _run_brief(run)
    note = str(payload.get("note") or "").strip()
    override = (cs.load_overrides(storage, run_id).get(stage_id) or {}).get("llm_model") or ""
    provider = _real_provider(override)

    system = (
        f"你是「{skill.name}」项目的监制，正在人工确认环节评审阶段「{stage.name}」的产出。\n"
        f"阶段职责：{stage.description or ''}\n"
        "该阶段自带的评审要点（review_focus）：" + "；".join(stage.review_focus or []) + "\n"
        "请站在**观众体验**和**下一步可执行性**两个角度，给出最多 5 条修改建议。\n"
        "输出严格的 JSON（不要 markdown 代码块）：\n"
        '{"overall": "一段总评（两三句）", "suggestions": [{"target": "针对的文件或位置", '
        '"issue": "问题", "suggestion": "具体怎么改（可直接照做的程度）", "priority": "high|medium|low"}]}\n'
        "规则：建议必须落到这份产物本身，不要泛泛的'可以更好'；没有问题就直说没有，不硬凑。"
    )
    import json as _json

    user = _json.dumps(
        {
            "任务 brief": {k: brief.get(k) for k in ("idea", "duration", "style", "genre", "platform") if brief.get(k)},
            "产出文件": artifacts,
            "用户要求": note or "（无，按你的判断来）",
        },
        ensure_ascii=False,
        indent=1,
    )
    resp = await provider.chat(system=system, user=user, json_mode=True)
    data = extract_json(resp.text)
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="模型没有返回结构化建议，请重试")

    raw = data.get("suggestions") or []
    suggestions = []
    for s in raw[:5] if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        suggestions.append(
            {
                "target": str(s.get("target") or "")[:120],
                "issue": str(s.get("issue") or "")[:600],
                "suggestion": str(s.get("suggestion") or "")[:900],
                "priority": str(s.get("priority") or "medium").lower(),
            }
        )
    logger.info("阶段建议 run=%s stage=%s 条数=%s model=%s", run_id, stage_id, len(suggestions), resp.model)
    return {
        "ok": True,
        "stage_id": stage_id,
        "overall": str(data.get("overall") or ""),
        "suggestions": suggestions,
        "model": resp.model,
        "override_model": override or None,
        "fallback_from": resp.fallback_from,
    }
