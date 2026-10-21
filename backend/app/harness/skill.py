"""技能加载器 —— "技能可插拔"的落地点。

核心原则（对应 MRD §8 与 v1.1 架构修正）：
    编排逻辑不写在 Python 里，而是写在技能的 manifest.yaml 与 SKILL.md 里。
    新增任务类型 = 新增一个目录，不动主程序。

manifest schema 采用参考项目（OpenMontage）已验证的格式：
    orchestration{mode,budget_default_usd,max_revisions_per_stage,max_send_backs,max_wall_time_minutes}
    stages[]{id,name,prompt,tools_available,produces,checkpoint_required,
             human_approval_default,review_focus[],success_criteria[],sub_stages[],demo}

「什么算合格、要不要停下来问人、最多重做几次」全部是**数据**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import settings

logger = logging.getLogger("longspan.skill")


class SkillError(Exception):
    pass


@dataclass
class StageDef:
    id: str
    name: str
    description: str = ""
    prompt: str | None = None              # 相对技能目录的 prompt 模板路径
    tools_available: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)      # 预期产物相对路径
    checkpoint_required: bool = True
    human_approval_default: bool = False   # ← 门禁的数据来源
    review_focus: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    sub_stages: list["StageDef"] = field(default_factory=list)
    demo: str = ""                         # 无模型 Key 时的确定性演示产物（内联）
    demo_file: str = ""                    # 或：指向 fixtures/ 下的文件（内容长时用）
    optional: bool = False
    fast_channel: bool = True              # 是否属于快通道（≤3 分钟草稿）
    model_hint: str = ""
    icon: str = ""
    # 阶段收尾的**确定性动作**：模型交完结构化清单后，由 Harness 调工具执行。
    # 例：{tool: generate_images, plan: panels.json, fast_limit: 3}
    # 这里只有数据（调哪个工具、读哪份清单），没有一行流程代码。
    actions: list[dict] = field(default_factory=list)
    # 阶段级**确定性校验钩子**（数据驱动，不写死在 runner 里）。
    # 例：{kind: panel_anchor_consistency, plan: panels.json, anchors: characters.md}
    #   kind 决定用哪个校验器；plan/anchors 是校验器的输入文件。
    # 「什么算一致」是技能的知识，runner 只负责「读声明 → 调校验器 → 判失败重做」。
    consistency_checks: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "StageDef":
        return cls(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            description=raw.get("description", ""),
            prompt=raw.get("prompt"),
            tools_available=list(raw.get("tools_available", []) or []),
            produces=list(raw.get("produces", []) or []),
            checkpoint_required=bool(raw.get("checkpoint_required", True)),
            human_approval_default=bool(raw.get("human_approval_default", False)),
            review_focus=list(raw.get("review_focus", []) or []),
            success_criteria=list(raw.get("success_criteria", []) or []),
            sub_stages=[cls.from_dict(s) for s in raw.get("sub_stages", []) or []],
            demo=raw.get("demo", "") or "",
            demo_file=raw.get("demo_file", "") or "",
            optional=bool(raw.get("optional", False)),
            fast_channel=bool(raw.get("fast_channel", True)),
            model_hint=raw.get("model_hint", ""),
            icon=raw.get("icon", ""),
            actions=[dict(a) for a in (raw.get("actions", []) or [])],
            consistency_checks=[dict(c) for c in (raw.get("consistency_checks", []) or [])],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "produces": self.produces,
            "tools_available": self.tools_available,
            "checkpoint_required": self.checkpoint_required,
            "human_approval_default": self.human_approval_default,
            "review_focus": self.review_focus,
            "success_criteria": self.success_criteria,
            "fast_channel": self.fast_channel,
            "optional": self.optional,
            "icon": self.icon,
            "sub_stages": [s.to_dict() for s in self.sub_stages],
            "actions": self.actions,
            "consistency_checks": self.consistency_checks,
        }


@dataclass
class SkillDef:
    id: str
    name: str
    version: str
    description: str
    dir: Path
    icon: str = "🧩"
    tags: list[str] = field(default_factory=list)
    entry_inputs: list[dict] = field(default_factory=list)
    presets: list[dict] = field(default_factory=list)
    orchestration: dict = field(default_factory=dict)
    stages: list[StageDef] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    skill_md: str = ""

    # ---- 派生 ----
    def stage(self, stage_id: str) -> StageDef | None:
        for s in self.stages:
            if s.id == stage_id:
                return s
            for sub in s.sub_stages:
                if sub.id == stage_id:
                    return sub
        return None

    def fast_stages(self) -> list[StageDef]:
        """快通道阶段序列（§5.1：≤3 分钟出草稿）。"""
        return [s for s in self.stages if s.fast_channel]

    def quota(self) -> dict:
        o = self.orchestration or {}
        return {
            "max_revisions_per_stage": o.get(
                "max_revisions_per_stage", settings.max_revisions_per_stage
            ),
            "max_send_backs": o.get("max_send_backs", settings.max_send_backs),
            "max_wall_time_minutes": o.get(
                "max_wall_time_minutes", settings.max_wall_time_minutes
            ),
            "budget_default_usd": o.get("budget_default_usd", settings.budget_default_usd),
        }

    def draft_gaps(self) -> list[str]:
        """快通道相比精修「省略了什么」—— 由技能声明，不是代码里写死。

        草稿诚实性的数据来源：宁可提前说清哪里是粗的，也不让用户以为拿到了成品。
        """
        o = self.orchestration or {}
        return [str(x) for x in (o.get("draft_gaps") or [])]

    def slow_only_stages(self) -> list["StageDef"]:
        """精修通道才跑的阶段（= 快通道没做的部分）。"""
        return [s for s in self.stages if not s.fast_channel and not s.optional]

    def to_dict(self, with_stages: bool = True) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "icon": self.icon,
            "tags": self.tags,
            "entry_inputs": self.entry_inputs,
            "presets": self.presets,
            "outputs": self.outputs,
            "stage_count": len(self.stages),
            "orchestration": self.orchestration,
        }
        if with_stages:
            d["stages"] = [s.to_dict() for s in self.stages]
        return d


class SkillRegistry:
    """扫描 skills/ 目录并注册技能。支持热插拔：新增目录即新增能力。"""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, SkillDef] = {}

    def scan(self) -> dict[str, SkillDef]:
        self._skills.clear()
        if not self.skills_dir.exists():
            logger.warning("技能目录不存在：%s", self.skills_dir)
            return self._skills

        for manifest in sorted(self.skills_dir.glob("*/manifest.yaml")):
            try:
                skill = self._load(manifest.parent)
                self._skills[skill.id] = skill
                logger.info("已加载技能 %s（%d 阶段）", skill.id, len(skill.stages))
            except Exception as exc:  # 单个技能坏了不影响其他
                logger.exception("加载技能失败 %s: %s", manifest, exc)
        return self._skills

    def _load(self, skill_dir: Path) -> SkillDef:
        manifest_path = skill_dir / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if "id" not in raw:
            raise SkillError(f"{manifest_path} 缺少 id 字段")

        skill_md_path = skill_dir / "SKILL.md"
        skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.exists() else ""

        entry = raw.get("entry", {}) or {}
        return SkillDef(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            version=str(raw.get("version", "0.1.0")),
            description=raw.get("description", ""),
            dir=skill_dir,
            icon=raw.get("icon", "🧩"),
            tags=list(raw.get("tags", []) or []),
            entry_inputs=list(entry.get("inputs", []) or []),
            presets=list(entry.get("presets", []) or []),
            orchestration=raw.get("orchestration", {}) or {},
            stages=[StageDef.from_dict(s) for s in raw.get("stages", []) or []],
            outputs=list(raw.get("outputs", []) or []),
            skill_md=skill_md,
        )

    # ---- 查询 ----
    def all(self) -> list[SkillDef]:
        return list(self._skills.values())

    def get(self, skill_id: str) -> SkillDef | None:
        return self._skills.get(skill_id)

    def require(self, skill_id: str) -> SkillDef:
        s = self.get(skill_id)
        if s is None:
            raise SkillError(f"技能不存在：{skill_id}")
        return s

    # ---- 提示词与知识 ----
    def read_prompt(self, skill: SkillDef, stage: StageDef) -> str:
        if not stage.prompt:
            return ""
        p = (skill.dir / stage.prompt).resolve()
        if not p.exists():
            logger.warning("提示词文件缺失：%s", p)
            return ""
        return p.read_text(encoding="utf-8")

    def read_reference(self, skill: SkillDef, rel: str) -> str:
        """L3 外部技术知识包按需加载（对应 MRD §8 三层知识架构）。"""
        p = (skill.dir / rel).resolve()
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")

    def demo_content(self, skill: SkillDef, stage: StageDef) -> str | None:
        """取阶段的无模型演示产物：优先 demo_file，其次内联 demo。

        这让 M0/M0.5 在没有模型额度时也能真实跑出可看的成品（北极星指标的可验收前提）。
        """
        if stage.demo_file:
            p = (skill.dir / stage.demo_file).resolve()
            if p.exists():
                return p.read_text(encoding="utf-8")
            logger.warning("demo_file 缺失：%s", p)
        return stage.demo or None

    def list_references(self, skill: SkillDef) -> list[str]:
        refs_dir = skill.dir / "references"
        if not refs_dir.exists():
            return []
        return sorted(str(p.relative_to(skill.dir)) for p in refs_dir.rglob("*.md"))


registry = SkillRegistry(settings.skills_dir)
