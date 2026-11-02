"""配额与收尾自检的回归测试（2026-11-02 龟兔赛跑事故）。

事故现场：一个 12 格任务全程 55 分钟，其中 33 分钟在等人工确认；40 分钟的墙钟配额
被"等人"吃光，分镜刚确认就被判超时 —— 配音、合成两个阶段从未执行，任务却标成
`succeeded`，界面显示"全部完成"，用户拿不到任何视频。

两条不变量：
  1. 墙钟配额只统计**执行时间**，门禁等待不计入；
  2. `succeeded` 必须意味着目标阶段**全部完成**，否则是 `interrupted`（可续跑）。
"""

from __future__ import annotations

import unittest

from app.harness.runner import _elapsed_exec_minutes, _unfinished_stage_ids
from app.models import StepStatus


class ElapsedExecMinutesTest(unittest.TestCase):
    def test_gate_wait_is_excluded(self) -> None:
        # 事故现场：墙钟 55 分钟，等人 33 分钟 → 有效执行 22 分钟
        self.assertAlmostEqual(
            _elapsed_exec_minutes(0.0, gate_wait_seconds=33 * 60, now=55 * 60), 22.0
        )

    def test_no_wait(self) -> None:
        self.assertAlmostEqual(_elapsed_exec_minutes(0.0, 0.0, now=10 * 60), 10.0)

    def test_never_negative(self) -> None:
        # 等人时长不可能超过墙钟，但真出现异常数据时也不能算出负时长
        self.assertEqual(_elapsed_exec_minutes(0.0, gate_wait_seconds=100 * 60, now=10 * 60), 0.0)

    def test_regression_cap_not_triggered_by_waiting(self) -> None:
        # 回归点：55 分钟墙钟 / 33 分钟等人 / 40 分钟配额 → 不该触发超时
        self.assertLess(
            _elapsed_exec_minutes(0.0, gate_wait_seconds=33 * 60, now=55 * 60), 40
        )


class UnfinishedStagesTest(unittest.TestCase):
    def test_all_completed(self) -> None:
        statuses = {s: StepStatus.COMPLETED for s in ("a", "b", "c")}
        self.assertEqual(_unfinished_stage_ids(["a", "b", "c"], statuses), [])

    def test_pending_is_detected(self) -> None:
        statuses = {
            "constraint_parse": StepStatus.COMPLETED,
            "storyboard": StepStatus.COMPLETED,
            "voice": StepStatus.PENDING,
            "compose": StepStatus.PENDING,
        }
        self.assertEqual(
            _unfinished_stage_ids(list(statuses), statuses), ["voice", "compose"]
        )

    def test_skipped_counts_as_done(self) -> None:
        statuses = {"a": StepStatus.SKIPPED, "b": StepStatus.COMPLETED}
        self.assertEqual(_unfinished_stage_ids(["a", "b"], statuses), [])

    def test_missing_step_counts_as_unfinished(self) -> None:
        # 没有 Step 记录 = 从没跑过，绝不能算完成
        self.assertEqual(_unfinished_stage_ids(["a"], {}), ["a"])


if __name__ == "__main__":
    unittest.main()
