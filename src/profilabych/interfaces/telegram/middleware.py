"""Trusted event gating, process-local update deduplication and handler shutdown."""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update

logger = logging.getLogger(__name__)


def is_private_user_message(message: Message) -> bool:
    return (
        message.chat.type == "private"
        and message.from_user is not None
        and not message.from_user.is_bot
        and message.from_user.id > 0
    )


class UpdateGuard(BaseMiddleware):
    """Reserve IDs before awaiting; keep active IDs separate from the bounded cache."""

    def __init__(self, capacity: int = 10000) -> None:
        if capacity < 1:
            raise ValueError("Update cache capacity must be positive")
        self._capacity = capacity
        self._completed: OrderedDict[int, None] = OrderedDict()
        self._pending: set[int] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            self._closing
            or not isinstance(event, Update)
            or event.message is None
            or not is_private_user_message(event.message)
        ):
            return None
        update_id = event.update_id
        if update_id in self._pending or update_id in self._completed:
            logger.info("telegram_duplicate_update", extra={"context": {"update_id": update_id}})
            return None
        # Single event loop: this reservation has no await and cannot race.
        self._pending.add(update_id)
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self._tasks.discard(task)
            self._pending.discard(update_id)
            # Retain failed/cancelled deliveries too: tools may already have committed.
            self._completed[update_id] = None
            if len(self._completed) > self._capacity:
                self._completed.popitem(last=False)

    async def aclose(self) -> None:
        """Reject new work, then cancel and await handlers before resources are closed."""
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def error_handler_scope(self) -> AsyncIterator[bool]:
        """Aiogram invokes errors outside this middleware; track that delivery too."""
        if self._closing:
            yield False
            return
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            yield True
        finally:
            self._tasks.discard(task)
