"""Minimal terminal adapter sharing the same core as the future Telegram interface."""

import asyncio
from collections.abc import Callable

from profilabych.agent.core import Agent, AgentError


async def run_cli(
    agent: Agent,
    *,
    user_id: int = 1,
    chat_id: int = 0,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> None:
    write("Профилабыч: /reset — очистить историю, /exit — выйти.")
    while True:
        try:
            text = await asyncio.to_thread(read, "Вы: ")
        except EOFError:
            return
        text = text.strip()
        if text == "/exit":
            return
        if not text:
            continue
        try:
            if text == "/reset":
                await agent.reset_history(user_id, chat_id=chat_id)
                write("История очищена. Заметки сохранены.")
            else:
                write(await agent.handle(user_id, text, chat_id=chat_id))
        except AgentError as error:
            write(str(error))
