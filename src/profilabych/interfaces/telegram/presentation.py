"""Safe entity delivery and bounded, cancellable typing notifications."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiogram import Bot
from aiogram.exceptions import ClientDecodeError, TelegramAPIError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from profilabych.interfaces.telegram.formatting import chunk_entities, format_text, utf16_length

logger = logging.getLogger(__name__)
_MESSAGE_LIMIT = 4096
_MAX_RETRY_DELAY = 5
_TYPING_INTERVAL = 4.0


def split_text(text: str) -> list[str]:
    """Split on whitespace where possible, conservatively counting UTF-16 units."""
    return [chunk for chunk, _ in _split_parts(text.strip())]


def _split_parts(text: str) -> list[tuple[str, int]]:
    """Keep source offsets even when skipping chunks containing only whitespace."""
    chunks: list[tuple[str, int]] = []
    offset = 0
    while text:
        units = end = 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > _MESSAGE_LIMIT:
                break
            units += width
            end += 1
        if end < len(text):
            boundary = max(text.rfind("\n", 0, end), text.rfind(" ", 0, end))
            if boundary >= end // 2 and text[: boundary + 1].strip():
                end = boundary + 1
        chunk, text = text[:end], text[end:]
        if chunk.strip():
            chunks.append((chunk, offset))
        offset += utf16_length(chunk)
    return chunks


async def send_text(bot: Bot, chat_id: int, text: str) -> bool:
    """Deliver in order; retry only an explicit short flood-control rejection once."""
    rendered, entities = format_text(text.strip())
    chunks = _split_parts(rendered)
    if not chunks:
        chunks = [("Ответ пуст. Попробуйте переформулировать запрос.", 0)]
        entities = []
    for chunk, offset in chunks:
        formatting = chunk_entities(entities, offset, utf16_length(chunk))
        for attempt in range(2):
            try:
                await bot.send_message(
                    chat_id,
                    chunk,
                    parse_mode=None,
                    entities=formatting or None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    allow_paid_broadcast=False,
                    request_timeout=15,
                )
                break
            except TelegramRetryAfter as error:
                if attempt == 0 and 0 <= error.retry_after <= _MAX_RETRY_DELAY:
                    await asyncio.sleep(error.retry_after)
                    continue
                logger.warning(
                    "telegram_delivery_rate_limit", extra={"context": {"chat_id": chat_id}}
                )
                return False
            except (TelegramAPIError, ClientDecodeError) as error:
                logger.warning(
                    "telegram_delivery_failed",
                    extra={"context": {"chat_id": chat_id, "error": type(error).__name__}},
                )
                return False
    return True


async def _send_typing(bot: Bot, chat_id: int) -> bool:
    try:
        await bot.send_chat_action(chat_id, "typing", request_timeout=10)
    except (TelegramAPIError, ClientDecodeError) as error:
        logger.warning(
            "telegram_typing_failed",
            extra={"context": {"chat_id": chat_id, "error": type(error).__name__}},
        )
        return False
    return True


async def _repeat_typing(bot: Bot, chat_id: int) -> None:
    while True:
        await asyncio.sleep(_TYPING_INTERVAL)
        if not await _send_typing(bot, chat_id):
            return


@asynccontextmanager
async def typing(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """Stop and await the background task even when the request is cancelled."""
    task = (
        asyncio.create_task(_repeat_typing(bot, chat_id))
        if await _send_typing(bot, chat_id)
        else None
    )
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
