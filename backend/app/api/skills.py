"""技能接口：列表、详情、参考知识（三层知识架构的 L2/L3 可见性）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..harness.skill import registry

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
def list_skills() -> dict:
    return {"skills": [s.to_dict(with_stages=False) for s in registry.all()]}


@router.get("/{skill_id}")
def get_skill(skill_id: str) -> dict:
    skill = registry.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    d = skill.to_dict(with_stages=True)
    d["references"] = registry.list_references(skill)
    d["quota"] = skill.quota()
    d["skill_md"] = skill.skill_md
    return d


@router.get("/{skill_id}/stages/{stage_id}/prompt")
def get_stage_prompt(skill_id: str, stage_id: str) -> dict:
    """阶段提示词可见 —— 让"技能可插拔"可被检验。"""
    skill = registry.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    stage = skill.stage(stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"阶段不存在：{stage_id}")
    return {
        "stage": stage.to_dict(),
        "prompt": registry.read_prompt(skill, stage),
    }


@router.post("/reload")
def reload_skills() -> dict:
    """热插拔：往 skills/ 放一个新目录后调它即可生效，无需重启。"""
    skills = registry.scan()
    return {"loaded": list(skills.keys())}
