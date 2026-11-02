from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.harness import agent as agent_mod
from app.harness.agent import AgentRunner, normalize_tool_arguments, parse_dsml_calls, parse_json_tool_calls, strip_tool_protocol
from app.harness.tools import ToolContext


DSML_LIST = (
    "<｜｜DSML｜｜ calls>\n"
    "<｜｜DSML｜｜ invoke name=\"list_artifacts\">\n\n"
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)

DSML_DECISION = (
    "<｜｜DSML｜｜ calls>\n"
    "<｜｜DSML｜｜ invoke name=\"record_decision\">\n"
    "<｜｜DSML｜｜ parameter name=\"what\" string=\"true\">使用 8 格</｜｜DSML｜｜ parameter>\n"
    "<｜｜DSML｜｜ parameter name=\"why\" string=\"true\">符合节奏要求</｜｜DSML｜｜ parameter>\n"
    "<｜｜DSML｜｜ parameter name=\"alternatives\" string=\"false\">"
    "[{\"option\":\"7 格\",\"confidence\":0.2}]</｜｜DSML｜｜ parameter>\n"
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


class ParseAgentProtocolTest(unittest.TestCase):
    def test_parse_dsml_without_arguments(self) -> None:
        self.assertEqual(
            parse_dsml_calls(DSML_LIST, {"list_artifacts"}),
            [{"name": "list_artifacts", "arguments": {}}],
        )

    def test_parse_dsml_with_json_parameter(self) -> None:
        calls = parse_dsml_calls(DSML_DECISION, {"record_decision"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "record_decision")
        self.assertEqual(calls[0]["arguments"]["what"], "使用 8 格")
        self.assertEqual(
            calls[0]["arguments"]["alternatives"],
            [{"option": "7 格", "confidence": 0.2}],
        )

    def test_parse_json_tool_call_tags(self) -> None:
        open_tag = "<" + "tool_call" + ">"
        close_tag = "</" + "tool_call" + ">"
        body = (
            '{"name": "record_decision", "arguments": '
            '{"what": "反派是倒计时", "why": "节省时长", "alternatives": []}}'
        )
        text = f"先说明思路。\n{open_tag}\n{body}\n{close_tag}\n"
        self.assertEqual(
            parse_json_tool_calls(text, {"record_decision"}),
            [{"name": "record_decision", "arguments": {
                "what": "反派是倒计时", "why": "节省时长", "alternatives": []
            }}],
        )

    def test_ordinary_json_is_not_tool_call(self) -> None:
        fence = "`" * 3
        self.assertEqual(parse_json_tool_calls(f'{fence}json\n{{"foo":"bar"}}\n{fence}', {"record_decision"}), [])

    def test_strip_tool_protocol_keeps_human_text(self) -> None:
        open_tag = "<" + "tool_call" + ">"
        close_tag = "</" + "tool_call" + ">"
        body = '{"name": "list_artifacts", "arguments": {}}'
        text = f"开始侦察。\n{open_tag}\n{body}\n{close_tag}"
        self.assertEqual(strip_tool_protocol(text, {"list_artifacts"}), "开始侦察。")
        self.assertEqual(strip_tool_protocol(DSML_LIST, {"list_artifacts"}), "")

    def test_normalize_integer_argument(self) -> None:
        class Spec:
            inputs = {
                "type": "object",
                "properties": {"max_chars": {"type": "integer"}, "ignored": {"type": "string"}},
            }

        self.assertEqual(
            normalize_tool_arguments(Spec(), {"max_chars": "8000", "ignored": "x", "extra": "y"}),
            {"max_chars": 8000, "ignored": "x"},
        )


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.users: list[str] = []

    async def chat(self, system: str, user: str, demo: str | None = None, json_mode: bool = False):
        from app.llm.providers import LLMResponse

        self.users.append(user)
        return LLMResponse(text=self.responses.pop(0), tokens=10, cost_usd=0.1, model=self.model)


class FakeStorage:
    def __init__(self) -> None:
        self.files = ["script.md"]

    def list_files(self, run_id: str) -> list[str]:
        return list(self.files)

    def write_text(self, run_id: str, path: str, content: str) -> tuple[str, int]:
        self.files.append(path)
        return path, len(content)


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_dsml_observation_is_fed_back_before_delivery(self) -> None:
        provider = FakeProvider([
            DSML_LIST,
            "分镜已完成。\n<<<FILE:storyboard.md>>>\n# 分镜\n<<<END>>>",
        ])
        storage = FakeStorage()
        ctx = ToolContext(
            run_id="test-run",
            stage_id="storyboard",
            brief={},
            storage=storage,
            extra={"declared_produces": {"storyboard.md": "storyboard"}},
        )
        runner = AgentRunner(on_event=lambda *args, **kwargs: None)

        with patch.object(agent_mod, "get_provider_for", return_value=provider):
            result = await runner.run(
                ctx,
                system="system",
                user="请生成分镜",
                tools_whitelist=["list_artifacts", "write_artifact"],
                stage_id="storyboard",
            )

        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.files, ["storyboard.md"])
        self.assertEqual(result.text, "分镜已完成。")
        self.assertIn("script.md", provider.users[1])
        self.assertIn("HARNESS TOOL OBSERVATIONS", provider.users[1])
        self.assertTrue(any("list_artifacts" in obs for obs in result.observations))

    async def test_undeclared_placeholder_file_is_ignored_and_loop_continues(self) -> None:
        provider = FakeProvider([
            "先侦察。\n<<<FILE:__placeholder__>>>\n<<<END>>>",
            "分镜已完成。\n<<<FILE:storyboard.md>>>\n# 分镜\n<<<END>>>",
        ])
        storage = FakeStorage()
        ctx = ToolContext(
            run_id="test-run",
            stage_id="storyboard",
            brief={},
            storage=storage,
            extra={"declared_produces": {"storyboard.md": "storyboard"}},
        )
        runner = AgentRunner(on_event=lambda *args, **kwargs: None)

        with patch.object(agent_mod, "get_provider_for", return_value=provider):
            result = await runner.run(
                ctx,
                system="system",
                user="请生成分镜",
                tools_whitelist=["write_artifact"],
                stage_id="storyboard",
            )

        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.files, ["storyboard.md"])
        self.assertNotIn("__placeholder__", storage.files)
        self.assertIn("不是本阶段声明的交付产物", provider.users[1])


if __name__ == "__main__":
    unittest.main()
