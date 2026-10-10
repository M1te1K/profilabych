"""In-memory Telegram transport; real aiogram dispatch runs without network access."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetUpdates, SendChatAction, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Message, Update, User

BOT_TOKEN = "123456789:fake-test-token"


class FakeTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod[Any]] = []
        self.errors: dict[str, list[Exception]] = {}
        self.updates: asyncio.Queue[list[Update]] = asyncio.Queue()
        self.closed = False
        self.typing_sent = asyncio.Event()

    @property
    def messages(self) -> list[SendMessage]:
        return [method for method in self.methods if isinstance(method, SendMessage)]

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 -- aiogram contract
    ) -> TelegramType:
        assert not self.closed, "Telegram session used after shutdown"
        self.methods.append(method)
        errors = self.errors.get(type(method).__name__)
        if errors:
            raise errors.pop(0)
        if isinstance(method, GetMe):
            result: object = User(
                id=123456789, is_bot=True, first_name="Профилабыч", username="profilabych_test_bot"
            )
        elif isinstance(method, GetUpdates):
            result = await self.updates.get()
        elif isinstance(method, SendChatAction):
            self.typing_sent.set()
            result = True
        elif isinstance(method, SendMessage):
            result = Message.model_validate(
                {
                    "message_id": len(self.methods),
                    "date": 1700000000,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                }
            )
        else:
            raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")
        return cast(TelegramType, result)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 -- aiogram contract
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise AssertionError("Media downloads are forbidden")
        yield b""  # pragma: no cover


def update(
    update_id: int = 1,
    text: str | None = "Привет",
    *,
    user_id: int = 42,
    chat_id: int = 42,
    chat_type: str = "private",
    is_bot: bool = False,
    sender: bool = True,
    event_type: str = "message",
    **content: object,
) -> Update:
    message: dict[str, object] = {
        "message_id": update_id,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": chat_type},
        **content,
    }
    if sender:
        message["from"] = {"id": user_id, "is_bot": is_bot, "first_name": "Тест"}
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
    return Update.model_validate({"update_id": update_id, event_type: message})
