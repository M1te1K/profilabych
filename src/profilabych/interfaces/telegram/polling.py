"""Aiogram polling lifecycle with explicit ownership of active update handlers."""

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher

from profilabych.agent.core import Agent
from profilabych.config import Settings
from profilabych.interfaces.telegram.handlers import create_router
from profilabych.interfaces.telegram.middleware import UpdateGuard

logger = logging.getLogger(__name__)


def create_dispatcher(agent: Agent, settings: Settings, guard: UpdateGuard) -> Dispatcher:
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(guard)
    dispatcher.include_router(create_router(agent, settings, guard))
    dispatcher.shutdown.register(guard.aclose)
    return dispatcher


async def run_polling(bot: Bot, agent: Agent, settings: Settings) -> None:
    """The caller closes the Bot session after all handlers finish cancellation."""
    guard = UpdateGuard()
    dispatcher = create_dispatcher(agent, settings, guard)
    ready = asyncio.Event()

    async def startup_ready() -> None:
        ready.set()

    dispatcher.startup.register(startup_ready)
    logger.info("telegram_polling_starting")
    task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            allowed_updates=["message"],
            close_bot_session=False,
            tasks_concurrency_limit=16,
        )
    )
    try:
        # Let aiogram stop its polling workers normally even on external cancellation.
        # Direct cancellation of start_polling can otherwise leave a getUpdates task alive.
        await asyncio.shield(task)
    except asyncio.CancelledError:
        ready_task = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait((task, ready_task), return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                await dispatcher.stop_polling()
            await task
        finally:
            ready_task.cancel()
            with suppress(asyncio.CancelledError):
                await ready_task
        raise
    finally:
        # Also covers startup failure, before aiogram reaches its shutdown hook.
        await guard.aclose()
        logger.info("telegram_polling_stopped")
