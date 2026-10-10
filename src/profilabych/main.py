"""Composition root for Telegram, CLI, offline checks and the Fake LLM demo."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from functools import partial

import httpx
from aiogram import Bot
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from profilabych.agent.core import Agent
from profilabych.config import Settings, load_settings
from profilabych.interfaces.cli import run_cli
from profilabych.interfaces.telegram.polling import run_polling
from profilabych.llm.models import LLMError, LLMProvider
from profilabych.llm.openrouter import OpenRouterProvider
from profilabych.logging import configure_logging
from profilabych.storage.contracts import StorageError
from profilabych.storage.sqlite import SQLiteStorage
from profilabych.tools.calculator import CalculatorArguments, run_calculator
from profilabych.tools.current_time import TimeArguments, run_current_time
from profilabych.tools.notes import ListNotesArguments, NoteTools, SaveNoteArguments
from profilabych.tools.registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)


def build_agent(provider: LLMProvider, storage: SQLiteStorage, settings: Settings) -> Agent:
    """Wire dependencies once; interfaces contain no business logic."""
    registry = ToolRegistry(
        timeout_seconds=settings.tool_timeout_seconds,
        result_max_chars=settings.tool_result_max_chars,
    )
    notes = NoteTools(
        storage, max_length=settings.note_max_length, default_timezone=settings.default_timezone
    )
    registry.register(
        Tool(
            "calculator",
            "Вычисляет выражения с + - * / и скобками.",
            CalculatorArguments,
            run_calculator,
        )
    )
    registry.register(
        Tool(
            "current_time",
            "Возвращает текущее время и готовую фразу answer для пользователя. "
            f"По умолчанию {settings.default_timezone}; не передавай timezone без явного "
            "указания города/пояса в текущем запросе. Для указанного города выбери IANA timezone.",
            TimeArguments,
            partial(run_current_time, default_timezone=settings.default_timezone),
        )
    )
    registry.register(
        Tool(
            "save_note",
            "Сохраняет заметку текущего пользователя.",
            SaveNoteArguments,
            notes.save_note,
        )
    )
    registry.register(
        Tool(
            "list_notes",
            "Читает заметки текущего пользователя по времени создания; limit/offset для страниц. "
            f"created_at уже отображается в {settings.default_timezone}, формат ДД.ММ.ГГГГ ЧЧ:ММ. "
            "Покажи дату создания каждой заметки, без повторного преобразования времени.",
            ListNotesArguments,
            notes.list_notes,
        )
    )
    return Agent(provider, registry, storage, settings)


async def _run_online(
    settings: Settings, user_id: int, chat_id: int, *, telegram: bool = False
) -> None:
    async with (
        httpx.AsyncClient(trust_env=False) as client,
        OpenRouterProvider(client, settings) as provider,
    ):
        # Validate free model before creating state or accepting any user input.
        await provider.verify_model()
        async with SQLiteStorage(
            settings.database_path,
            max_messages=settings.history_max_messages,
            max_chars=settings.history_max_chars,
            note_max_length=settings.note_max_length,
        ) as storage:
            agent = build_agent(provider, storage, settings)
            if telegram:
                assert settings.telegram_bot_token is not None
                async with Bot(token=settings.telegram_bot_token.get_secret_value()) as bot:
                    await run_polling(bot, agent, settings)
            else:
                await run_cli(agent, user_id=user_id, chat_id=chat_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Профилабыч: Telegram и CLI AI-агента")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Проверить конфигурацию без сети и БД")
    modes.add_argument(
        "--demo", action="store_true", help="Fake LLM + временная SQLite, без сети и ключей"
    )
    modes.add_argument("--telegram", action="store_true", help="Запустить Telegram polling (сеть)")
    parser.add_argument(
        "--user-id", type=int, default=1, help="Локальный пользователь CLI (не аутентификация)"
    )
    parser.add_argument("--chat-id", type=int, default=0)
    args = parser.parse_args(argv)
    if args.user_id <= 0:
        parser.error("--user-id должен быть положительным")
    configure_logging(stream=sys.stderr)
    try:
        # Demo deliberately ignores developer credentials and local dotenv.
        settings = Settings.model_construct() if args.demo else load_settings()
    except ValidationError as error:
        fields = sorted(
            {
                str(item["loc"][0]) if item["loc"][0] in Settings.model_fields else "unknown_field"
                for item in error.errors()
                if item["loc"]
            }
        )
        logger.error("configuration_invalid", extra={"context": {"fields": fields}})
        return 1
    except (SettingsError, OSError, UnicodeError):
        logger.error("configuration_unreadable")
        return 1
    secrets = tuple(
        secret
        for secret in (settings.telegram_bot_token, settings.openrouter_api_key)
        if secret is not None
    )
    configure_logging(
        settings.log_level, secrets=secrets, stream=sys.stdout if args.check else sys.stderr
    )
    if args.check:
        logger.info(
            "infrastructure_ready",
            extra={
                "context": {
                    "stage": 3,
                    "mode": "offline",
                    "app_env": settings.app_env,
                    "llm_model": settings.llm_model,
                }
            },
        )
        return 0
    if not args.demo:
        missing = []
        if settings.openrouter_api_key is None:
            missing.append("OPENROUTER_API_KEY")
        if args.telegram and settings.telegram_bot_token is None:
            missing.append("TELEGRAM_BOT_TOKEN")
        if missing:
            print(
                "Добавьте " + ", ".join(missing) + " в окружение или локальный .env. "
                "Офлайн: --demo или --check.",
                file=sys.stderr,
            )
            return 1
        if args.telegram:
            assert settings.telegram_bot_token is not None
            try:
                validate_token(settings.telegram_bot_token.get_secret_value())
            except TokenValidationError:
                print("Некорректный формат TELEGRAM_BOT_TOKEN.", file=sys.stderr)
                return 1
    try:
        if args.demo:
            from profilabych.demo import run_demo

            asyncio.run(run_demo())
        else:
            asyncio.run(_run_online(settings, args.user_id, args.chat_id, telegram=args.telegram))
    except (LLMError, StorageError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (TelegramAPIError, ClientDecodeError) as error:
        logger.error("telegram_startup_failed", extra={"context": {"error": type(error).__name__}})
        print(
            "Не удалось запустить Telegram. Проверьте токен и доступность сервиса.", file=sys.stderr
        )
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
