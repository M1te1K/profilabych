import asyncio
import io
import json
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue, SecretStr, ValidationError
from test_tools import call

from profilabych.agent.core import AgentError
from profilabych.config import Settings
from profilabych.demo import demo_provider
from profilabych.interfaces.cli import run_cli
from profilabych.llm.fake import FakeLLM
from profilabych.llm.models import LLMError, Message, ToolChoice
from profilabych.logging import JsonFormatter
from profilabych.main import build_agent
from profilabych.storage.contracts import StorageError
from profilabych.storage.history import history_size, validate_turn
from profilabych.storage.sqlite import SQLiteStorage
from profilabych.tools.calculator import CalculatorArguments
from profilabych.tools.registry import Tool, ToolContext


def answer(text: str = "Готово") -> Message:
    return Message(role="assistant", content=text)


def tool_request(name: str, arguments: str = "{}", call_id: str = "call_1") -> Message:
    return Message(role="assistant", tool_calls=(call(name, arguments, call_id),))


async def test_plain_answer_and_preserved_context(tmp_path: Path) -> None:
    fake = FakeLLM([answer("Первый"), answer("Второй")])
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        assert await agent.handle(1, "Привет") == "Первый"
        assert await agent.handle(1, "Продолжи") == "Второй"
        history = await storage.load_history(1)
        assert [m.content for m in history] == ["Привет", "Первый", "Продолжи", "Второй"]
        assert fake.requests[1].messages[1:] == tuple(history[:-1])
        assert not agent._locks


@pytest.mark.parametrize(
    ("name", "arguments", "result_key"),
    [
        ("calculator", '{"expression":"15*4"}', "result"),
        ("current_time", "{}", "time"),
        ("save_note", '{"text":"моя заметка"}', "note_id"),
        ("list_notes", "{}", "notes"),
    ],
)
async def test_each_builtin_through_core(
    tmp_path: Path, name: str, arguments: str, result_key: str
) -> None:
    fake = FakeLLM([tool_request(name, arguments), answer()])
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        assert await agent.handle(1, "Сделай") == "Готово"
        result = json.loads(fake.requests[1].messages[-1].content)
        assert result["ok"] and result_key in result["data"]
        history = await storage.load_history(1)
        assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
        assert history[2].tool_call_id == history[1].tool_calls[0].id
        validate_turn(history)


async def test_calculate_save_note_demo_and_reset(tmp_path: Path) -> None:
    fake = demo_provider()
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        result = await agent.handle(42, "Посчитай 15 * 4 и сохрани результат как заметку")
        assert result == "15 * 4 = 60. Сохранено как заметка №1."
        assert [r.messages[-1].role for r in fake.requests] == ["user", "tool", "tool"]
        assert (await storage.list_notes(42))[0].text == "60"
        assert not await storage.list_notes(1)
        history = await storage.load_history(42)
        assert [m.role for m in history] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
        ]
        validate_turn(history)
        await agent.reset_history(42)
        assert not await storage.load_history(42)
        assert (await storage.list_notes(42))[0].text == "60"


async def test_multiple_calls_execute_in_order(tmp_path: Path) -> None:
    multi = Message(
        role="assistant",
        tool_calls=(
            call("save_note", '{"text":"first"}', "a"),
            call("list_notes", "{}", "b"),
        ),
    )
    fake = FakeLLM([multi, answer()])
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        await agent.handle(1, "Запиши и прочитай")
        results = [m for m in fake.requests[1].messages if m.role == "tool"]
        assert [m.tool_call_id for m in results] == ["a", "b"]
        assert json.loads(results[1].content)["data"]["notes"][0]["text"] == "first"
        validate_turn(await storage.load_history(1))


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("missing", "{}", "unknown_tool"),
        ("calculator", "bad json", "invalid_arguments"),
        ("calculator", '{"expression":"1/0"}', "division_by_zero"),
    ],
)
async def test_tool_failure_can_be_repaired(
    tmp_path: Path, name: str, arguments: str, code: str
) -> None:
    fake = FakeLLM(
        [
            tool_request(name, arguments, "bad"),
            tool_request("calculator", '{"expression":"2+2"}', "fixed"),
            answer("4"),
        ]
    )
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        assert (
            await build_agent(fake, storage, Settings(_env_file=None)).handle(1, "Посчитай") == "4"
        )
        assert json.loads(fake.requests[1].messages[-1].content)["error"]["code"] == code
        assert json.loads(fake.requests[2].messages[-1].content)["data"]["result"] == "4"
        validate_turn(await storage.load_history(1))


async def test_llm_failure_mid_turn_preserves_completed_action(tmp_path: Path) -> None:
    fake = FakeLLM(
        [
            tool_request("save_note", '{"text":"keep"}'),
            LLMError("unavailable", "LLM недоступна"),
            answer("Продолжение"),
        ]
    )
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        assert "Не удалось" in await agent.handle(1, "Сохрани")
        assert (await storage.list_notes(1))[0].text == "keep"
        history = await storage.load_history(1)
        validate_turn(history)
        assert history[2].tool_call_id == "call_1"
        assert await agent.handle(1, "Продолжи") == "Продолжение"
        assert fake.requests[-1].messages[1:-1] == tuple(history)


async def test_limits_rounds_total_calls_and_final_response(tmp_path: Path) -> None:
    requests = [tool_request("calculator", '{"expression":"1"}', str(i)) for i in range(6)]
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        fake = FakeLLM(requests)
        agent = build_agent(fake, storage, Settings(_env_file=None, agent_max_tool_calls=20))
        assert "лимит" in await agent.handle(1, "Зацикливание")
        assert len(fake.requests) == 6 and fake.requests[-1].tool_choice == "none"
        history = await storage.load_history(1)
        assert len([m for m in history if m.role == "tool"]) == 5
        validate_turn(history)
        await agent.reset_history(1)
        fake = FakeLLM([*requests[:5], answer("Финал после пяти раундов")])
        assert (
            await build_agent(fake, storage, Settings(_env_file=None)).handle(1, "Пять действий")
            == "Финал после пяти раундов"
        )
        await agent.reset_history(1)
        fake = FakeLLM(
            [
                Message(
                    role="assistant",
                    tool_calls=tuple(call("save_note", '{"text":"no"}', str(i)) for i in range(3)),
                )
            ]
        )
        agent = build_agent(fake, storage, Settings(_env_file=None, agent_max_tool_calls=2))
        assert "лимит" in await agent.handle(1, "Слишком много вызовов")
        assert not await storage.list_notes(1)
        validate_turn(await storage.load_history(1))


async def test_rejects_repeated_call_ids_and_wrong_role(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Message(role="assistant", tool_calls=(call("list_notes"), call("list_notes")))
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        fake = FakeLLM(
            [
                tool_request("save_note", '{"text":"once"}'),
                tool_request("save_note", '{"text":"twice"}'),
            ]
        )
        agent = build_agent(fake, storage, Settings(_env_file=None))
        assert "tool_call_id" in await agent.handle(1, "save")
        assert [n.text for n in await storage.list_notes(1)] == ["once"]
        validate_turn(await storage.load_history(1))
        fake = FakeLLM([Message(role="user", content="wrong")])
        assert "роль" in await build_agent(fake, storage, Settings(_env_file=None)).handle(
            2, "hello"
        )
        validate_turn(await storage.load_history(2))


async def test_isolation_and_concurrent_same_user_requests(tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class SlowFake(FakeLLM):
        async def complete(
            self,
            messages: Sequence[Message],
            tools: Sequence[dict[str, object]],
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Message:
            if not self.requests:
                started.set()
                await release.wait()
            return await super().complete(messages, tools, tool_choice=tool_choice)

    fake = SlowFake([answer("a"), answer("b"), answer("c")])
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        first = asyncio.create_task(agent.handle(1, "first", chat_id=10))
        await started.wait()
        second = asyncio.create_task(agent.handle(1, "second", chat_id=10))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == ["a", "b"]
        assert [m.content for m in fake.requests[1].messages[1:]] == ["first", "a", "second"]
        await agent.handle(2, "private", chat_id=10)
        assert [m.content for m in fake.requests[2].messages[1:]] == ["private"]
        assert [m.content for m in await storage.load_history(1, 10)] == [
            "first",
            "a",
            "second",
            "b",
        ]
        assert not agent._locks


async def test_history_limits_preserve_current_turn(tmp_path: Path) -> None:
    config = Settings(_env_file=None, history_max_messages=4)
    fake = FakeLLM([answer("a"), answer("b"), answer("c")])
    async with SQLiteStorage(tmp_path / "test.db", max_messages=4) as storage:
        agent = build_agent(fake, storage, config)
        for text in ("first", "second", "third"):
            await agent.handle(1, text)
        assert len(await storage.load_history(1)) == 4
        assert len(fake.requests[-1].messages) <= 5  # system prompt is additional
        assert "first" not in [m.content for m in fake.requests[-1].messages]
    async with SQLiteStorage(tmp_path / "chars.db", max_chars=2000) as storage:
        fake = FakeLLM([answer("a"), answer("b"), tool_request("save_note", '{"text":"no"}')])
        agent = build_agent(fake, storage, Settings(_env_file=None, history_max_chars=2000))
        await agent.handle(1, "x" * 1000)
        await agent.handle(1, "y" * 1000)
        assert history_size(await storage.load_history(1)) <= 2000
        assert "лимит" in await agent.handle(1, "save")
        assert not await storage.list_notes(1)
        history = await storage.load_history(1)
        validate_turn(history[-2:])


async def test_storage_failures_are_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(FakeLLM([answer()]), storage, Settings(_env_file=None))

        async def fail(*args: object, **kwargs: object) -> None:
            raise StorageError("private SQL")

        monkeypatch.setattr(storage, "append_turn", fail)
        with pytest.raises(AgentError) as caught:
            await agent.handle(1, "hello")
        assert caught.value.code == "storage_error" and "SQL" not in str(caught.value)
        assert not await storage.load_history(1)
    with pytest.raises(AgentError):
        await agent.handle(1, "closed")
    with pytest.raises(AgentError):
        await agent.reset_history(1)


async def test_sqlite_tool_error_is_returned_to_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:

        async def fail(user_id: int, text: str) -> int:
            raise StorageError("private")

        monkeypatch.setattr(storage, "save_note", fail)
        fake = FakeLLM(
            [tool_request("save_note", '{"text":"hello"}'), answer("Сохранить не удалось")]
        )
        result = await build_agent(fake, storage, Settings(_env_file=None)).handle(1, "save")
        assert result == "Сохранить не удалось"
        assert json.loads(fake.requests[1].messages[-1].content)["error"]["code"] == "storage_error"
        validate_turn(await storage.load_history(1))


async def test_cancel_mid_tool_group_persists_valid_history(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def blocked(args: CalculatorArguments, context: ToolContext) -> dict[str, JsonValue]:
        started.set()
        await asyncio.Event().wait()
        return {}

    fake = FakeLLM(
        [
            Message(
                role="assistant",
                tool_calls=(
                    call("save_note", '{"text":"keep"}', "a"),
                    call("blocked", '{"expression":"1"}', "b"),
                    call("list_notes", "{}", "c"),
                ),
            )
        ]
    )
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        agent._registry.register(Tool("blocked", "blocked", CalculatorArguments, blocked))
        task = asyncio.create_task(agent.handle(1, "start"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        history = await storage.load_history(1)
        validate_turn(history)
        assert [m.tool_call_id for m in history if m.role == "tool"] == ["a", "b", "c"]
        assert (await storage.list_notes(1))[0].text == "keep"
        assert not agent._locks


async def test_cli_commands_use_core_and_keep_notes(tmp_path: Path) -> None:
    inputs = iter(["", "Посчитай и сохрани", "/reset", "/exit"])
    output = []
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(demo_provider(), storage, Settings(_env_file=None))
        await run_cli(agent, read=lambda prompt: next(inputs), write=output.append)
        assert "заметка №1" in output[1]
        assert "История очищена" in output[2]
        assert not await storage.load_history(1)
        assert (await storage.list_notes(1))[0].text == "60"


async def test_log_secrets_size_and_user_isolation(tmp_path: Path) -> None:
    secret = "fake-registered-secret"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter([SecretStr(secret)]))
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        async with SQLiteStorage(tmp_path / "test.db") as storage:
            await storage.save_note(2, "other-user-private-note")
            fake = FakeLLM([tool_request("list_notes"), answer(secret)])
            await build_agent(fake, storage, Settings(_env_file=None)).handle(
                1, secret + "x" * 2000
            )
        logs = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert secret not in stream.getvalue()
        assert "other-user-private-note" not in stream.getvalue()
        assert all(row["context"]["user_id"] == 1 for row in logs)
        assert len({row["context"]["request_id"] for row in logs}) == 1
        assert len(logs[0]["context"]["query"]) < 1100
        assert all(row["timestamp"].endswith("+00:00") for row in logs)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


@pytest.mark.parametrize("text", ["", " ", "x" * 4001])
async def test_invalid_user_input_does_not_mutate_history(tmp_path: Path, text: str) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        fake = FakeLLM([])
        agent = build_agent(fake, storage, Settings(_env_file=None))
        with pytest.raises(AgentError):
            await agent.handle(1, text)
        assert not fake.requests and not await storage.load_history(1)


async def test_reset_waits_for_active_request_and_clears_its_history(tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class PausedFake(FakeLLM):
        async def complete(
            self,
            messages: Sequence[Message],
            tools: Sequence[dict[str, object]],
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Message:
            if not self.requests:
                started.set()
                await release.wait()
            return await super().complete(messages, tools, tool_choice=tool_choice)

    fake = PausedFake([answer("first answer"), answer("after reset")])
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        await storage.save_note(1, "keep")
        request = asyncio.create_task(agent.handle(1, "first"))
        await started.wait()
        reset = asyncio.create_task(agent.reset_history(1))
        await asyncio.sleep(0)
        assert not reset.done()
        release.set()
        await request
        await reset
        assert not await storage.load_history(1)
        await agent.handle(1, "new")
        assert [m.content for m in fake.requests[-1].messages[1:]] == ["new"]
        assert (await storage.list_notes(1))[0].text == "keep"


async def test_cancelled_queued_request_does_not_leak_lock(tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class PausedFake(FakeLLM):
        async def complete(
            self,
            messages: Sequence[Message],
            tools: Sequence[dict[str, object]],
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Message:
            started.set()
            await release.wait()
            return await super().complete(messages, tools, tool_choice=tool_choice)

    fake = PausedFake([answer()])
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        active = asyncio.create_task(agent.handle(1, "active"))
        await started.wait()
        queued = asyncio.create_task(agent.handle(1, "cancelled"))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await active
        assert not agent._locks
        assert len(fake.requests) == 1
        assert [m.content for m in await storage.load_history(1)] == ["active", "Готово"]


async def test_different_users_can_wait_for_llm_concurrently(tmp_path: Path) -> None:
    both_started = asyncio.Event()
    count = 0

    class ConcurrentFake(FakeLLM):
        async def complete(
            self,
            messages: Sequence[Message],
            tools: Sequence[dict[str, object]],
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Message:
            nonlocal count
            count += 1
            if count == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return Message(role="assistant", content=f"Ответ: {messages[-1].content}")

    async with SQLiteStorage(tmp_path / "test.db") as storage:
        agent = build_agent(ConcurrentFake([]), storage, Settings(_env_file=None))
        assert await asyncio.gather(agent.handle(1, "один"), agent.handle(2, "два")) == [
            "Ответ: один",
            "Ответ: два",
        ]
        assert [m.content for m in await storage.load_history(1)] == ["один", "Ответ: один"]
        assert [m.content for m in await storage.load_history(2)] == ["два", "Ответ: два"]


async def test_cancel_after_commit_does_not_duplicate_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        append = storage.append_turn

        async def commit_then_cancel(
            user_id: int, messages: Sequence[Message], chat_id: int = 0
        ) -> None:
            await append(user_id, messages, chat_id)
            raise asyncio.CancelledError

        monkeypatch.setattr(storage, "append_turn", commit_then_cancel)
        agent = build_agent(FakeLLM([answer()]), storage, Settings(_env_file=None))
        with pytest.raises(asyncio.CancelledError):
            await agent.handle(1, "hello")
        history = await storage.load_history(1)
        assert len(history) == 2
        validate_turn(history)
        assert not agent._locks
