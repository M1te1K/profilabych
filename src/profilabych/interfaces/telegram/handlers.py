"""Commands and input validation; all dialogue and tool execution stays in Agent."""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, Message

from profilabych.agent.core import Agent, AgentError
from profilabych.config import Settings
from profilabych.interfaces.telegram.middleware import UpdateGuard, is_private_user_message
from profilabych.interfaces.telegram.presentation import send_text, typing

logger = logging.getLogger(__name__)
_UNAVAILABLE = "Сервис временно недоступен. Попробуйте позже."
_HELP = (
    "Команды:\n"
    "/start — знакомство\n"
    "/help — команды и возможности\n"
    "/reset — очистить историю диалога; заметки сохраняются\n\n"
    "Напишите вопрос обычным текстом. Я могу выполнить вычисление (calculator), "
    "узнать время (current_time), сохранить заметку (save_note) "
    "и прочитать ваши заметки (list_notes).\n"
    "Например: «Посчитай 15 * 4 и сохрани результат в заметку».\n"
    "Работаю только с текстом в личном чате."
)


def create_router(agent: Agent, settings: Settings, guard: UpdateGuard) -> Router:
    """Attach handlers to one application Agent, without creating infrastructure."""
    router = Router(name="profilabych.telegram")

    @router.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        await send_text(bot, message.chat.id, "Привет! Я Профилабыч.\n\n" + _HELP)

    @router.message(Command("help"))
    async def help_command(message: Message, bot: Bot) -> None:
        await send_text(bot, message.chat.id, _HELP)

    @router.message(Command("reset"))
    async def reset(message: Message, bot: Bot) -> None:
        assert message.from_user is not None
        try:
            async with typing(bot, message.chat.id):
                await agent.reset_history(message.from_user.id, chat_id=message.chat.id)
        except AgentError as error:
            logger.warning("telegram_reset_failed", extra={"context": {"error": error.code}})
            await send_text(bot, message.chat.id, "Не удалось очистить историю. Попробуйте позже.")
            return
        await send_text(bot, message.chat.id, "История очищена. Заметки сохранены.")

    @router.message(F.text)
    async def text_message(message: Message, bot: Bot) -> None:
        assert message.from_user is not None and message.text is not None
        text = message.text
        if not text.strip():
            await send_text(bot, message.chat.id, "Отправьте непустое текстовое сообщение.")
            return
        if len(text) > settings.user_input_max_chars:
            await send_text(
                bot,
                message.chat.id,
                f"Сообщение слишком длинное. Максимум {settings.user_input_max_chars} символов.",
            )
            return
        if text.lstrip().startswith("/"):
            await send_text(bot, message.chat.id, "Неизвестная команда. Список команд: /help.")
            return
        try:
            async with typing(bot, message.chat.id):
                answer = await agent.handle(message.from_user.id, text, chat_id=message.chat.id)
        except AgentError as error:
            logger.warning("telegram_agent_failed", extra={"context": {"error": error.code}})
            answer = _UNAVAILABLE
        await send_text(bot, message.chat.id, answer)

    @router.message()
    async def unsupported_message(message: Message, bot: Bot) -> None:
        await send_text(bot, message.chat.id, "Поддерживаются только текстовые сообщения. /help")

    @router.errors()
    async def unexpected_error(event: ErrorEvent, bot: Bot) -> bool:
        # Framework error boundary: isolate a faulty update, never serialize its exception.
        async with guard.error_handler_scope() as active:
            logger.error(
                "telegram_handler_error",
                extra={
                    "context": {
                        "update_id": event.update.update_id,
                        "error": type(event.exception).__name__,
                    }
                },
            )
            message = event.update.message
            if active and message is not None and is_private_user_message(message):
                await send_text(bot, message.chat.id, _UNAVAILABLE)
        return True

    return router
