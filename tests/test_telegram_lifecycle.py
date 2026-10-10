import asyncio
import io
import json
import logging
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import GetMe, SendChatAction, SendMessage
from pydantic import SecretStr
from telegram_fakes import BOT_TOKEN, FakeTelegramSession, update
from test_agent import answer, tool_request
from test_openrouter import catalog, completion

import profilabych.interfaces.telegram.polling as polling
import profilabych.interfaces.telegram.presentation as presentation
import profilabych.main as entrypoint
from profilabych.agent.core import Agent
from profilabych.config import Settings
from profilabych.interfaces.telegram.middleware import UpdateGuard
from profilabych.interfaces.telegram.polling import create_dispatcher
from profilabych.interfaces.telegram.presentation import send_text, typing
from profilabych.llm.fake import FakeLLM
from profilabych.llm.models import LLMError, Message, ToolChoice
from profilabych.logging import JsonFormatter
from profilabych.main import build_agent
from profilabych.storage.contracts import StorageError
from profilabych.storage.history import validate_turn
from profilabych.storage.sqlite import SQLiteStorage


@pytest.mark.parametrize(
    ("delays", "expected", "calls"),
    [([0], True, 2), ([0, 0], False, 2), ([6], False, 1)],
)
async def test_flood_control_retries_only_short_rejection_once(
    delays: list[int], expected: bool, calls: int
) -> None:
    session = FakeTelegramSession()
    session.errors["SendMessage"] = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=42, text="x"), message="private", retry_after=delay
        )
        for delay in delays
    ]
    async with Bot(BOT_TOKEN, session=session) as bot:
        assert await send_text(bot, 42, "ответ") is expected
    assert len(session.messages) == calls


async def test_failed_long_response_stops_after_failed_chunk() -> None:
    session = FakeTelegramSession()
    session.errors["SendMessage"] = [
        TelegramBadRequest(method=SendMessage(chat_id=42, text="x"), message="private")
    ]
    async with Bot(BOT_TOKEN, session=session) as bot:
        assert not await send_text(bot, 42, "x" * 9000)
    assert len(session.messages) == 1


async def test_blank_output_has_safe_fallback() -> None:
    session = FakeTelegramSession()
    async with Bot(BOT_TOKEN, session=session) as bot:
        assert await send_text(bot, 42, "  \n")
    assert session.messages[0].text == "Ответ пуст. Попробуйте переформулировать запрос."


async def test_typing_repeats_then_stops_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presentation, "_TYPING_INTERVAL", 0.01)
    session = FakeTelegramSession()
    started = asyncio.Event()

    async def wait_with_typing(bot: Bot) -> None:
        async with typing(bot, 42):
            started.set()
            await asyncio.Event().wait()

    async with Bot(BOT_TOKEN, session=session) as bot:
        task = asyncio.create_task(wait_with_typing(bot))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.03)
        assert len(session.methods) >= 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        count = len(session.methods)
        await asyncio.sleep(0.03)
        assert len(session.methods) == count


async def test_typing_background_api_error_is_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presentation, "_TYPING_INTERVAL", 0.01)
    session = FakeTelegramSession()
    async with Bot(BOT_TOKEN, session=session) as bot:
        async with typing(bot, 42):
            session.errors["SendChatAction"] = [
                TelegramBadRequest(
                    method=SendChatAction(chat_id=42, action="typing"), message="private"
                )
            ]
            await asyncio.sleep(0.03)
        assert len(session.methods) == 2


async def test_bounded_cache_retains_inflight_updates() -> None:
    guard = UpdateGuard(capacity=1)
    started, release = asyncio.Event(), asyncio.Event()
    processed = []

    async def handler(event: object, data: dict[str, object]) -> None:
        processed.append(event.update_id)
        if event.update_id == 1:
            started.set()
            await release.wait()

    first = asyncio.create_task(guard(handler, update(1), {}))
    await asyncio.wait_for(started.wait(), 2)
    await guard(handler, update(2), {})
    await guard(handler, update(3), {})
    await guard(handler, update(1), {})
    assert processed == [1, 2, 3]
    release.set()
    await first
    assert len(guard._completed) == 1 and not guard._pending and not guard._tasks
    await guard(handler, update(1), {})
    assert processed == [1, 2, 3]
    await guard.aclose()
    await guard(handler, update(4), {})
    assert processed == [1, 2, 3]


async def test_serialization_reset_and_other_user_concurrency(tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class PausedFake(FakeLLM):
        async def complete(
            self,
            messages: Sequence[Message],
            tools: Sequence[dict[str, object]],
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Message:
            if messages[-1].content == "первый":
                started.set()
                await release.wait()
            return await super().complete(messages, tools, tool_choice=tool_choice)

    fake = PausedFake([answer("другой пользователь"), answer("первый ответ")])
    session = FakeTelegramSession()
    config = Settings(_env_file=None)
    async with (
        SQLiteStorage(tmp_path / "test.db") as storage,
        Bot(BOT_TOKEN, session=session) as bot,
    ):
        await storage.save_note(42, "keep")
        guard = UpdateGuard()
        agent = build_agent(fake, storage, config)
        dispatcher = create_dispatcher(agent, config, guard)
        first = asyncio.create_task(dispatcher.feed_update(bot, update(1, "первый")))
        await asyncio.wait_for(started.wait(), 2)
        reset = asyncio.create_task(dispatcher.feed_update(bot, update(2, "/reset")))
        await asyncio.sleep(0)
        await asyncio.wait_for(
            dispatcher.feed_update(bot, update(3, "другой", user_id=99, chat_id=99)), 2
        )
        assert not first.done() and not reset.done()
        release.set()
        await asyncio.gather(first, reset)
        assert not await storage.load_history(42, 42)
        assert (await storage.list_notes(42))[0].text == "keep"
        assert (await storage.load_history(99, 99))[0].content == "другой"
        assert not agent._locks
        await guard.aclose()


@pytest.mark.parametrize(
    "exit_mode", ["stop", "cancel", "getme_error", "preflight_error", "storage_error"]
)
async def test_composition_resource_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_mode: str
) -> None:
    started = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={} if exit_mode == "preflight_error" else catalog())
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json=completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    session = FakeTelegramSession()
    bot = Bot(BOT_TOKEN, session=session)
    if exit_mode == "getme_error":
        session.errors["GetMe"] = [TelegramUnauthorizedError(method=GetMe(), message="private")]
    session.updates.put_nowait([update()])
    storages: list[SQLiteStorage] = []
    dispatchers: list[Dispatcher] = []
    guards: list[UpdateGuard] = []

    class ObservedStorage(SQLiteStorage):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            storages.append(self)

    def observe_dispatcher(agent: Agent, config: Settings, guard: UpdateGuard) -> Dispatcher:
        dispatcher = create_dispatcher(agent, config, guard)
        dispatchers.append(dispatcher)
        guards.append(guard)
        return dispatcher

    monkeypatch.setattr(entrypoint.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(entrypoint, "Bot", lambda **kwargs: bot)
    monkeypatch.setattr(entrypoint, "SQLiteStorage", ObservedStorage)
    monkeypatch.setattr(polling, "create_dispatcher", observe_dispatcher)
    path = tmp_path / "test.db"
    if exit_mode == "storage_error":
        path.mkdir()
    config = Settings(
        _env_file=None,
        telegram_bot_token=SecretStr(BOT_TOKEN),
        openrouter_api_key=SecretStr("fake-key"),
        database_path=path,
        llm_max_retries=0,
    )
    task = asyncio.create_task(entrypoint._run_online(config, 1, 0, telegram=True))
    if exit_mode in {"stop", "cancel"}:
        await asyncio.wait_for(started.wait(), 2)
        if exit_mode == "stop":
            await asyncio.wait_for(dispatchers[0].stop_polling(), 2)
            await task
        else:
            # External cancellation must stop getUpdates and drain active handlers.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert session.closed and not guards[0]._tasks and not guards[0]._pending
        assert not any(
            task.get_coro().__qualname__ == "Dispatcher._polling" and not task.done()
            for task in asyncio.all_tasks()
        )
    else:
        exception = {
            "getme_error": TelegramUnauthorizedError,
            "preflight_error": LLMError,
            "storage_error": StorageError,
        }[exit_mode]
        with pytest.raises(exception):
            await task
        assert session.closed is (exit_mode == "getme_error")
        if not session.closed:
            # The injected test bot was never constructed by the composition root.
            await session.close()
    assert client.is_closed
    for storage in storages:
        with pytest.raises(StorageError, match="закрыта"):
            await storage.list_notes(42)
    if exit_mode in {"stop", "cancel"}:
        async with SQLiteStorage(path) as reopened:
            history = await reopened.load_history(42, 42)
            validate_turn(history)
            assert history[-1].content == "Запрос прерван. Выполненные действия могли сохраниться."


async def test_shutdown_waits_for_committed_note_and_interrupted_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    class BlockAfterNote(FakeLLM):
        async def complete(
            self,
            messages: Sequence[Message],
            tools: Sequence[dict[str, object]],
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Message:
            if self.requests:
                started.set()
                await asyncio.Event().wait()
            return await super().complete(messages, tools, tool_choice=tool_choice)

    fake = BlockAfterNote([tool_request("save_note", '{"text":"keep"}')])
    path = tmp_path / "test.db"
    config = Settings(_env_file=None)
    session = FakeTelegramSession()
    session.updates.put_nowait([update()])
    captured: list[Dispatcher] = []

    def observe(agent: Agent, settings: Settings, guard: UpdateGuard) -> Dispatcher:
        dispatcher = create_dispatcher(agent, settings, guard)
        captured.append(dispatcher)
        return dispatcher

    monkeypatch.setattr(polling, "create_dispatcher", observe)
    async with SQLiteStorage(path) as storage, Bot(BOT_TOKEN, session=session) as bot:
        agent = build_agent(fake, storage, config)
        task = asyncio.create_task(polling.run_polling(bot, agent, config))
        await asyncio.wait_for(started.wait(), 2)
        await captured[0].stop_polling()
        await task
        assert not agent._locks
        assert (await storage.list_notes(42))[0].text == "keep"
        history = await storage.load_history(42, 42)
        validate_turn(history)
        assert history[2].tool_call_id == "call_1"
    assert session.closed
    async with SQLiteStorage(path) as reopened:
        assert (await reopened.list_notes(42))[0].text == "keep"
        assert await reopened.load_history(42, 42) == history


def test_invalid_token_and_missing_credentials_fail_before_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-private-key")
    assert entrypoint.main(["--telegram"]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-invalid-token")
    assert entrypoint.main(["--telegram"]) == 1
    output = capsys.readouterr().err
    assert "Некорректный формат" in output
    assert "fake-invalid-token" not in output and "fake-private-key" not in output


def test_aiogram_framework_logs_never_serialize_raw_bodies_or_tracebacks() -> None:
    stream = io.StringIO()
    logger = logging.Logger("aiogram.dispatcher")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    try:
        raise ValueError("private-api-body")
    except ValueError:
        logger.exception("Provider said: %s", "private-key", extra={"context": {"body": "private"}})
    event = json.loads(stream.getvalue())
    assert event["event"] == "telegram_framework_event"
    assert "private" not in stream.getvalue() and "exception" not in event


async def test_shutdown_also_waits_for_framework_error_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    session = FakeTelegramSession()
    original_request = session.make_request

    async def block_error_reply(bot: Bot, method: object, timeout: int | None = None) -> object:  # noqa: ASYNC109 -- aiogram contract
        if isinstance(method, SendMessage):
            started.set()
            await asyncio.Event().wait()
        return await original_request(bot, method, timeout)

    async def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("private-detail")

    monkeypatch.setattr(session, "make_request", block_error_reply)
    async with (
        SQLiteStorage(tmp_path / "test.db") as storage,
        Bot(BOT_TOKEN, session=session) as bot,
    ):
        config = Settings(_env_file=None)
        agent = build_agent(FakeLLM([]), storage, config)
        monkeypatch.setattr(agent, "handle", broken)
        guard = UpdateGuard()
        dispatcher = create_dispatcher(agent, config, guard)
        task = asyncio.create_task(dispatcher.feed_update(bot, update()))
        await asyncio.wait_for(started.wait(), 2)
        await guard.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not guard._tasks
    assert session.closed
