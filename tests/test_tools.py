import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import JsonValue

from profilabych.config import Settings
from profilabych.llm.fake import FakeLLM
from profilabych.llm.models import FunctionCall, ToolCall
from profilabych.main import build_agent
from profilabych.storage.sqlite import SQLiteStorage
from profilabych.tools.calculator import CalculatorArguments, calculate, run_calculator
from profilabych.tools.registry import Tool, ToolContext, ToolError, ToolRegistry


def call(name: str, arguments: str = "{}", call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=arguments))


@pytest.mark.parametrize(
    ("expression", "result"),
    [
        ("15 * 4", "60"),
        ("(25 + 15) / 2", "20"),
        ("125.5 * 3", "376.5"),
        ("0.1 + 0.2", "0.3"),
        ("-2 + +5", "3"),
        ("5 - 7", "-2"),
        (" 3 / 2 ", "1.5"),
        ("1 / 3", "0.3333333333333333333333333333"),
        ("0e-99999999", "0"),
        ("1-1", "0"),
    ],
)
def test_safe_arithmetic(expression: str, result: str) -> None:
    assert calculate(expression) == result


@pytest.mark.parametrize(
    "expression",
    [
        "1/0",
        "(2+3",
        "__import__('os').system('touch forbidden')",
        "a+1",
        "2**3",
        "True+1",
        "[1,2]",
        "(1).__class__",
        "x := 5",
        "2//1",
        "1%2",
        "",
        "9" * 257,
        "+" * 30 + "1",
        "+".join(["1"] * 30),
        "1e300",
        "1e-100",
        "9j",
        "0x10",
        "1<<4",
    ],
)
def test_rejects_unsafe_invalid_and_complex_expressions(expression: str) -> None:
    with pytest.raises(ToolError):
        calculate(expression)
    assert not Path("forbidden").exists()


async def test_builtin_schemas_and_registration(tmp_path: Path) -> None:
    async with SQLiteStorage(tmp_path / "test.sqlite3") as storage:
        agent = build_agent(FakeLLM([]), storage, Settings(_env_file=None))
        registry = agent._registry
        assert [s["function"]["name"] for s in registry.schemas()] == [
            "calculator",
            "current_time",
            "save_note",
            "list_notes",
        ]
        for schema in registry.schemas():
            assert schema["function"]["parameters"]["additionalProperties"] is False
            assert "user_id" not in schema["function"]["parameters"]["properties"]
        assert registry.find("missing") is None
        with pytest.raises(ValueError, match="registered"):
            registry.register(Tool("calculator", "duplicate", CalculatorArguments, run_calculator))


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("missing", "{}", "unknown_tool"),
        ("calculator", "not-json", "invalid_arguments"),
        ("calculator", "[]", "invalid_arguments"),
        ("calculator", '{"expression": 123}', "invalid_arguments"),
        ("calculator", '{"expression":"1/0"}', "division_by_zero"),
        ("current_time", '{"timezone":"Unknown/Planet"}', "unknown_timezone"),
        ("save_note", '{"text":"  "}', "invalid_arguments"),
        ("save_note", '{"text":"x", "user_id":2}', "invalid_arguments"),
        ("list_notes", '{"user_id":2}', "invalid_arguments"),
        ("list_notes", '{"limit":true}', "invalid_arguments"),
        ("list_notes", '{"limit":21}', "invalid_arguments"),
        ("list_notes", '{"offset":-1}', "invalid_arguments"),
    ],
)
async def test_controlled_errors(tmp_path: Path, name: str, arguments: str, code: str) -> None:
    async with SQLiteStorage(tmp_path / "test.sqlite3") as storage:
        registry = build_agent(FakeLLM([]), storage, Settings(_env_file=None))._registry
        result = await registry.execute(call(name, arguments), ToolContext(1, 0, "request"))
        assert not result.ok and result.error["code"] == code
        assert not await storage.list_notes(1)
        assert not await storage.list_notes(2)


@pytest.mark.parametrize(
    ("arguments", "timezone"),
    [("{}", "Europe/Moscow"), ('{"timezone":"UTC"}', "UTC")],
)
async def test_current_time(tmp_path: Path, arguments: str, timezone: str) -> None:
    async with SQLiteStorage(tmp_path / "test.sqlite3") as storage:
        registry = build_agent(FakeLLM([]), storage, Settings(_env_file=None))._registry
        before = datetime.now(ZoneInfo(timezone)).strftime("%H:%M")
        result = await registry.execute(
            call("current_time", arguments), ToolContext(1, 0, "request")
        )
        assert result.ok
        after = datetime.now(ZoneInfo(timezone)).strftime("%H:%M")
        assert result.data["timezone"] == timezone
        assert result.data["time"] in {before, after}
        assert result.data["answer"].startswith("Сейчас ")


async def test_tool_timeout_and_external_cancellation() -> None:
    started = asyncio.Event()

    async def slow(params: CalculatorArguments, context: ToolContext) -> dict[str, JsonValue]:
        started.set()
        await asyncio.Event().wait()
        return {}

    registry = ToolRegistry(timeout_seconds=0.01)
    registry.register(Tool("slow", "slow", CalculatorArguments, slow))
    invocation = call("slow", '{"expression":"1"}')
    result = await registry.execute(invocation, ToolContext(1, 0, "r"))
    assert result.error["code"] == "tool_timeout"
    registry = ToolRegistry(timeout_seconds=30)
    registry.register(Tool("slow", "slow", CalculatorArguments, slow))
    started.clear()
    task = asyncio.create_task(registry.execute(invocation, ToolContext(1, 0, "r")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_large_note_result_is_bounded(tmp_path: Path) -> None:
    async with SQLiteStorage(tmp_path / "test.sqlite3") as storage:
        registry = build_agent(FakeLLM([]), storage, Settings(_env_file=None))._registry
        for _ in range(10):
            await storage.save_note(1, "x" * 2000)
        result = await registry.execute(call("list_notes"), ToolContext(1, 0, "r"))
        assert result.error["code"] == "result_too_large"
        result = await registry.execute(
            call("list_notes", '{"limit":1,"offset":2}'), ToolContext(1, 0, "r")
        )
        assert result.ok and len(result.data["notes"]) == 1
