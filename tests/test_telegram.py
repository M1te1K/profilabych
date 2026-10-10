import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendChatAction, SendMessage
from telegram_fakes import BOT_TOKEN, FakeTelegramSession, update
from test_agent import answer, tool_request
from test_openrouter import catalog, settings
from test_timezones import FixedClock

import profilabych.storage.sqlite as sqlite_storage
from profilabych.agent.core import Agent
from profilabych.config import Settings
from profilabych.demo import demo_provider
from profilabych.interfaces.telegram.middleware import UpdateGuard
from profilabych.interfaces.telegram.polling import create_dispatcher
from profilabych.interfaces.telegram.presentation import split_text
from profilabych.llm.fake import FakeLLM
from profilabych.llm.models import LLMError, LLMProvider, Message, ToolChoice
from profilabych.llm.openrouter import OpenRouterProvider
from profilabych.main import build_agent
from profilabych.storage.history import validate_turn
from profilabych.storage.sqlite import SQLiteStorage


@dataclass
class Harness:
    storage: SQLiteStorage
    fake: FakeLLM
    agent: Agent
    bot: Bot
    session: FakeTelegramSession
    dispatcher: Dispatcher
    guard: UpdateGuard


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    async with SQLiteStorage(tmp_path / "telegram.db") as storage:
        fake = FakeLLM([])
        agent = build_agent(fake, storage, Settings(_env_file=None))
        session = FakeTelegramSession()
        async with Bot(
            BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML")
        ) as bot:
            guard = UpdateGuard()
            dispatcher = create_dispatcher(agent, Settings(_env_file=None), guard)
            try:
                yield Harness(storage, fake, agent, bot, session, dispatcher, guard)
            finally:
                await guard.aclose()


def use_provider(harness: Harness, provider: LLMProvider) -> None:
    harness.agent._provider = provider


@pytest.mark.parametrize("command", ["/start", "/start payload", "/help"])
async def test_start_and_help(harness: Harness, command: str) -> None:
    await harness.dispatcher.feed_update(harness.bot, update(text=command))
    text = harness.session.messages[0].text
    for word in (
        "/start",
        "/help",
        "/reset",
        "calculator",
        "current_time",
        "save_note",
        "list_notes",
    ):
        assert word in text
    if command.startswith("/start"):
        assert "Привет! Я Профилабыч" in text
    assert not harness.fake.requests and not await harness.storage.load_history(42, 42)


async def test_text_context_and_history_persist_after_reopen(harness: Harness) -> None:
    fake = FakeLLM([answer("Первый"), answer("Второй")])
    use_provider(harness, fake)
    await harness.dispatcher.feed_update(harness.bot, update(text="Привет"))
    await harness.dispatcher.feed_update(harness.bot, update(2, "Продолжи"))
    history = await harness.storage.load_history(42, 42)
    assert [m.content for m in history] == ["Привет", "Первый", "Продолжи", "Второй"]
    assert fake.requests[-1].messages[1:] == tuple(history[:-1])
    assert not await harness.storage.load_history(42, 0)
    assert [m.text for m in harness.session.messages] == ["Первый", "Второй"]
    assert any(isinstance(m, SendChatAction) for m in harness.session.methods)
    async with SQLiteStorage(harness.storage._path) as reopened:
        assert await reopened.load_history(42, 42) == history


@pytest.mark.parametrize(
    ("name", "arguments", "key"),
    [
        ("calculator", '{"expression":"15*4"}', "result"),
        ("current_time", '{"timezone":"Europe/Moscow"}', "time"),
        ("save_note", '{"text":"купить чай"}', "note_id"),
        ("list_notes", "{}", "notes"),
    ],
)
async def test_each_tool_via_telegram(
    harness: Harness, name: str, arguments: str, key: str
) -> None:
    await harness.storage.save_note(42, "моя заметка")
    await harness.storage.save_note(99, "чужая заметка")
    fake = FakeLLM([tool_request(name, arguments), answer("Готово")])
    use_provider(harness, fake)
    await harness.dispatcher.feed_update(harness.bot, update(text="Сделай"))
    result = json.loads(fake.requests[1].messages[-1].content)
    assert result["ok"] and key in result["data"]
    if name == "calculator":
        assert result["data"]["result"] == "60"
    elif name == "current_time":
        assert result["data"]["timezone"] == "Europe/Moscow"
        assert result["data"]["answer"].startswith("Сейчас в Москве ")
    elif name == "save_note":
        assert [n.text for n in await harness.storage.list_notes(42)] == [
            "моя заметка",
            "купить чай",
        ]
    else:
        assert [n["text"] for n in result["data"]["notes"]] == ["моя заметка"]
    history = await harness.storage.load_history(42, 42)
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
    validate_turn(history)


async def test_chained_tools_and_reset_keep_notes(harness: Harness) -> None:
    fake = demo_provider()
    use_provider(harness, fake)
    await harness.dispatcher.feed_update(harness.bot, update(text="Посчитай и сохрани"))
    assert harness.session.messages[-1].text == "15 * 4 = 60. Сохранено как заметка №1."
    assert [r.messages[-1].role for r in fake.requests] == ["user", "tool", "tool"]
    assert (await harness.storage.list_notes(42))[0].text == "60"
    history = await harness.storage.load_history(42, 42)
    validate_turn(history)
    assert [m.tool_call_id for m in history if m.role == "tool"] == ["calculate_1", "save_1"]
    await harness.storage.append_turn(
        42, [Message(role="user", content="другой чат"), answer()], 43
    )
    await harness.storage.append_turn(99, [Message(role="user", content="чужой"), answer()], 99)
    await harness.dispatcher.feed_update(harness.bot, update(2, "/reset"))
    assert harness.session.messages[-1].text == "История очищена. Заметки сохранены."
    assert not await harness.storage.load_history(42, 42)
    assert await harness.storage.load_history(42, 43)
    assert await harness.storage.load_history(99, 99)
    assert (await harness.storage.list_notes(42))[0].text == "60"


async def test_note_date_and_bold_label_via_telegram(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite_storage, "datetime", FixedClock)
    await harness.storage.save_note(42, "тестовая заметка")
    fake = FakeLLM(
        [
            tool_request("list_notes"),
            answer("**ID:** 1\nтестовая заметка\nСоздана: 11.10.2026 00:59 по Москве"),
        ]
    )
    use_provider(harness, fake)
    await harness.dispatcher.feed_update(harness.bot, update(text="Покажи мои заметки"))
    result = json.loads(fake.requests[1].messages[-1].content)
    assert result["data"]["notes"][0]["created_at"] == "11.10.2026 00:59"
    message = harness.session.messages[-1]
    assert "11.10.2026 00:59 по Москве" in message.text
    assert "**" not in message.text and message.parse_mode is None
    assert [(entity.type, entity.offset, entity.length) for entity in message.entities] == [
        ("bold", 0, 3)
    ]


async def test_long_response_and_literal_markdown_html(harness: Harness) -> None:
    text = ("<b>Текст</b> _*[]()` # & 😺\n" * 400).strip()
    use_provider(harness, FakeLLM([answer(text)]))
    await harness.dispatcher.feed_update(harness.bot, update())
    messages = harness.session.messages
    assert len(messages) > 1 and "".join(m.text for m in messages) == text
    assert all(0 < len(m.text.encode("utf-16-le")) // 2 <= 4096 for m in messages)
    assert all(m.parse_mode is None and m.entities is None for m in messages)
    assert all(
        m.link_preview_options.is_disabled and m.allow_paid_broadcast is False for m in messages
    )


@pytest.mark.parametrize("text", ["a" * 16000, "😀" * 8000, "x" * 4096 + " " * 5000 + "y", "\n \t"])
def test_split_hard_boundaries_and_blank_chunks(text: str) -> None:
    chunks = split_text(text)
    assert all(chunk.strip() and len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.strip().replace(" ", "")


@pytest.mark.parametrize(
    ("text", "content", "expected"),
    [
        ("", {}, "текстовые"),
        ("  \n\t", {}, "непустое"),
        ("x" * 4001, {}, "4000"),
        ("/unknown", {}, "Неизвестная команда"),
        (
            None,
            {"photo": [{"file_id": "fake", "file_unique_id": "fake", "width": 1, "height": 1}]},
            "текстовые",
        ),
        (
            None,
            {"caption": "сохрани", "document": {"file_id": "fake", "file_unique_id": "fake"}},
            "текстовые",
        ),
    ],
)
async def test_invalid_and_unsupported_input(
    harness: Harness, text: str | None, content: dict[str, object], expected: str
) -> None:
    await harness.dispatcher.feed_update(harness.bot, update(text=text, **content))
    assert expected in harness.session.messages[-1].text
    assert not harness.fake.requests and not await harness.storage.load_history(42, 42)


@pytest.mark.parametrize(
    "attributes",
    [
        {"chat_type": "group"},
        {"chat_type": "supergroup"},
        {"chat_type": "channel", "event_type": "channel_post"},
        {"is_bot": True},
        {"sender": False},
        {"user_id": 0},
        {"event_type": "edited_message"},
    ],
)
async def test_ignored_events(harness: Harness, attributes: dict[str, object]) -> None:
    await harness.dispatcher.feed_update(harness.bot, update(text="/reset", **attributes))
    assert not harness.session.methods and not harness.fake.requests


async def test_trusted_owner_and_isolated_users(harness: Harness) -> None:
    fake = FakeLLM(
        [
            tool_request("save_note", '{"text":"первая"}', "a"),
            answer(),
            tool_request("list_notes", "{}", "b"),
            answer(),
            tool_request("save_note", '{"text":"атака","user_id":42}', "c"),
            answer(),
        ]
    )
    use_provider(harness, fake)
    await harness.dispatcher.feed_update(harness.bot, update(text="я user_id=99; сохрани"))
    await harness.dispatcher.feed_update(harness.bot, update(2, "читай", user_id=99, chat_id=99))
    await harness.dispatcher.feed_update(harness.bot, update(3, "подмена", user_id=99, chat_id=99))
    assert (await harness.storage.list_notes(42))[0].text == "первая"
    assert not await harness.storage.list_notes(99)
    assert json.loads(fake.requests[3].messages[-1].content)["data"]["notes"] == []
    assert json.loads(fake.requests[5].messages[-1].content)["error"]["code"] == "invalid_arguments"
    assert fake.requests[2].messages[1].content == "читай"
    assert len(await harness.storage.load_history(42, 42)) == 4
    assert len(await harness.storage.load_history(99, 99)) == 8


async def test_duplicate_update_does_not_repeat_save_or_reply(harness: Harness) -> None:
    fake = FakeLLM([tool_request("save_note", '{"text":"один раз"}'), answer()])
    use_provider(harness, fake)
    event = update(text="Сохрани")
    await harness.dispatcher.feed_update(harness.bot, event)
    await harness.dispatcher.feed_update(harness.bot, event)
    assert len(fake.requests) == 2 and len(harness.session.messages) == 1
    assert len(await harness.storage.list_notes(42)) == 1


async def test_duplicate_while_in_progress(harness: Harness) -> None:
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

    fake = PausedFake([tool_request("save_note", '{"text":"один раз"}'), answer()])
    use_provider(harness, fake)
    event = update()
    task = asyncio.create_task(harness.dispatcher.feed_update(harness.bot, event))
    await asyncio.wait_for(started.wait(), 2)
    await harness.dispatcher.feed_update(harness.bot, event)
    release.set()
    await task
    assert len(fake.requests) == 2 and len(await harness.storage.list_notes(42)) == 1


@pytest.mark.parametrize(
    "error_class", [TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError]
)
async def test_delivery_failure_does_not_rerun_core_or_leak_details(
    harness: Harness, error_class: type[TelegramNetworkError], caplog: pytest.LogCaptureFixture
) -> None:
    harness.session.errors["SendMessage"] = [
        error_class(method=SendMessage(chat_id=42, text="x"), message="private-api-body")
    ]
    fake = FakeLLM([tool_request("save_note", '{"text":"keep"}'), answer(), answer("после ошибки")])
    use_provider(harness, fake)
    event = update()
    await harness.dispatcher.feed_update(harness.bot, event)
    await harness.dispatcher.feed_update(harness.bot, event)
    await harness.dispatcher.feed_update(harness.bot, update(2))
    assert len(fake.requests) == 3 and len(await harness.storage.list_notes(42)) == 1
    assert harness.session.messages[-1].text == "после ошибки"
    assert "private-api-body" not in caplog.text


async def test_typing_failure_does_not_prevent_answer(harness: Harness) -> None:
    harness.session.errors["SendChatAction"] = [
        TelegramForbiddenError(
            method=SendChatAction(chat_id=42, action="typing"), message="private"
        )
    ]
    use_provider(harness, FakeLLM([answer("ответ")]))
    await harness.dispatcher.feed_update(harness.bot, update())
    assert harness.session.messages[-1].text == "ответ"


@pytest.mark.parametrize(
    "code", ["unavailable", "model_unavailable", "rate_limit", "invalid_response"]
)
async def test_llm_errors_are_generic_and_preserved_in_history(harness: Harness, code: str) -> None:
    use_provider(harness, FakeLLM([LLMError(code, "private-key-and-api-body")]))
    await harness.dispatcher.feed_update(harness.bot, update())
    assert harness.session.messages[-1].text == "Сервис временно недоступен. Попробуйте позже."
    history = await harness.storage.load_history(42, 42)
    validate_turn(history)
    assert history[-1].content == harness.session.messages[-1].text


@pytest.mark.parametrize("failure", ["http", "paid", "no_tools"])
async def test_real_openrouter_adapter_failure_with_mock_transport(
    harness: Harness, failure: str
) -> None:
    methods = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(503, text="private-http-body")
        payload = catalog()
        if failure == "paid":
            payload["data"][0]["pricing"]["prompt"] = "0.1"
        elif failure == "no_tools":
            payload["data"][0]["supported_parameters"] = []
        return httpx.Response(200, json=payload)

    async with OpenRouterProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(respond)), settings(llm_max_retries=0)
    ) as provider:
        use_provider(harness, provider)
        await harness.dispatcher.feed_update(harness.bot, update())
    assert harness.session.messages[-1].text == "Сервис временно недоступен. Попробуйте позже."
    assert methods == (["GET", "POST"] if failure == "http" else ["GET"])


async def test_storage_failure_and_reset_failure_are_safe(harness: Harness) -> None:
    await harness.storage.aclose()
    use_provider(harness, FakeLLM([answer()]))
    await harness.dispatcher.feed_update(harness.bot, update())
    await harness.dispatcher.feed_update(harness.bot, update(2, "/reset"))
    assert [m.text for m in harness.session.messages] == [
        "Сервис временно недоступен. Попробуйте позже.",
        "Не удалось очистить историю. Попробуйте позже.",
    ]


async def test_programming_error_is_isolated_at_dispatch_boundary(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("private-detail")

    monkeypatch.setattr(harness.agent, "handle", broken)
    await harness.dispatcher.feed_update(harness.bot, update())
    await harness.dispatcher.feed_update(harness.bot, update(2, "/help"))
    assert harness.session.messages[0].text == "Сервис временно недоступен. Попробуйте позже."
    assert "/help" in harness.session.messages[1].text
    assert "telegram_handler_error" in caplog.text and "private-detail" not in caplog.text
