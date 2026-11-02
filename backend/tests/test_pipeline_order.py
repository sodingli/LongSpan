"""回归测试：角色设计必须排在分镜之前，且在快通道里就跑、并且有门禁。

2026-10-29 实测事故（run 0e7d2cb8…，steps 表时间戳）：
    快通道把 C4 角色设计标成精修专属，实际执行顺序变成
      C3 剧本 10:08 → C5 分镜 16:14 → C6 配音 16:17 → C4 角色 16:20
    分镜都画完了才回头定角色，8 格全部画在临时描述上 ——
    那一轮的 scenes.json 里留下了「上游 characters.md 未产出，本阶段拿不到冻结锚点」。
    顺序错不是省钱，是把后面每一格都画在错的锚点上。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.harness.skill import SkillRegistry
from app.config import settings


class ComicDramaPipelineOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SkillRegistry(settings.skills_dir)
        cls.registry.scan()
        cls.skill = cls.registry.get("comic-drama")
        cls.ids = [s.id for s in cls.skill.stages]

    def _stage(self, sid: str):
        return next(s for s in self.skill.stages if s.id == sid)

    def test_characters_runs_before_storyboard(self) -> None:
        self.assertLess(
            self.ids.index("characters"),
            self.ids.index("storyboard"),
            "角色设计必须排在分镜绘制之前 —— 否则分镜只能画在临时描述上",
        )

    def test_characters_is_in_fast_channel(self) -> None:
        # 快通道漏掉它，草稿就会跳过角色直接画分镜（旧 bug 的直接成因）
        self.assertIn("characters", [s.id for s in self.skill.fast_stages()])

    def test_characters_still_waits_for_human(self) -> None:
        # 用户要求：角色信息 + 定妆图出来之后停下来等确认
        self.assertTrue(self._stage("characters").human_approval_default)

    def test_characters_generates_reference_art(self) -> None:
        tools = [a.get("tool") for a in self._stage("characters").actions]
        self.assertIn("generate_character_art", tools)

    def test_anchor_check_is_not_skipped_in_draft(self) -> None:
        # characters.md 现在在分镜之前就有，锚点校验不该再对草稿放行
        checks = self._stage("storyboard").consistency_checks
        anchor = next(c for c in checks if c.get("kind") == "panel_anchor_consistency")
        self.assertFalse(anchor.get("slow_only"))

    def test_draft_gaps_do_not_claim_unlocked_characters(self) -> None:
        joined = "".join(self.skill.draft_gaps())
        self.assertNotIn("角色形象没有锁定", joined)


if __name__ == "__main__":
    unittest.main()
