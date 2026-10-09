"""Typed tool registration, argument validation and bounded execution."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from profilabych.llm.models import ToolCall
from profilabych.storage.contracts import StorageError, validate_identity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    """Application-supplied identity; never part of the tool JSON schema."""

    user_id: int
    chat_id: int
    request_id: str

    def __post_init__(self) -> None:
        validate_identity(self.user_id, self.chat_id)


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    data: dict[str, JsonValue] | None = None
    error: dict[str, str] | None = None

    @classmethod
    def failure(cls, code: str, message: str) -> "ToolResult":
        return cls(ok=False, error={"code": code, "message": message})


class RegisteredTool(Protocol):
    @property
    def name(self) -> str: ...

    def schema(self) -> dict[str, object]: ...

    async def execute(self, arguments: str, context: ToolContext) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class Tool[Arguments: BaseModel]:
    name: str
    description: str
    parameters: type[Arguments]
    handler: Callable[[Arguments, ToolContext], Awaitable[dict[str, JsonValue]]]

    def schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters.model_json_schema(),
            },
        }

    async def execute(self, arguments: str, context: ToolContext) -> dict[str, JsonValue]:
        params = self.parameters.model_validate_json(arguments, strict=True)
        return await self.handler(params, context)


class ToolRegistry:
    def __init__(self, *, timeout_seconds: float = 5, result_max_chars: int = 8000) -> None:
        if not 0 < timeout_seconds <= 30 or not 256 <= result_max_chars <= 12000:
            raise ValueError("Invalid tool limits")
        self._tools: dict[str, RegisteredTool] = {}
        self._timeout_seconds = timeout_seconds
        self._result_max_chars = result_max_chars

    @property
    def result_max_chars(self) -> int:
        return self._result_max_chars

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        if (
            not tool.name
            or len(tool.name) > 64
            or not all(char.isascii() and (char.isalnum() or char in "_-") for char in tool.name)
        ):
            raise ValueError("Invalid tool name")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, object]]:
        return [tool.schema() for tool in self._tools.values()]

    def find(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        started = perf_counter()
        metadata: dict[str, object] = {
            "user_id": context.user_id,
            "chat_id": context.chat_id,
            "request_id": context.request_id,
            "tool": call.function.name,
            "tool_call_id": call.id,
        }
        try:
            metadata["arguments"] = json.loads(call.function.arguments)
        except (ValueError, RecursionError):
            metadata["arguments"] = "[INVALID_JSON]"
        logger.info("tool_started", extra={"context": metadata})
        tool = self.find(call.function.name)
        if tool is None:
            result = ToolResult.failure("unknown_tool", "Неизвестный инструмент.")
        else:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    data = await tool.execute(call.function.arguments, context)
                result = ToolResult(ok=True, data=data)
            except ValidationError:
                result = ToolResult.failure(
                    "invalid_arguments", "Аргументы не соответствуют схеме инструмента."
                )
            except ToolError as error:
                result = ToolResult.failure(error.code, str(error))
            except StorageError:
                result = ToolResult.failure(
                    "storage_error", "Не удалось выполнить операцию с заметками."
                )
            except TimeoutError:
                result = ToolResult.failure(
                    "tool_timeout",
                    "Инструмент не ответил вовремя; состояние операции проверьте отдельно.",
                )
        if len(result.model_dump_json()) > self._result_max_chars:
            result = ToolResult.failure(
                "result_too_large",
                "Результат слишком большой. Уменьшите limit или используйте offset.",
            )
        logger.info(
            "tool_finished",
            extra={
                "context": metadata
                | {
                    "result": json.loads(result.model_dump_json()),
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                }
            },
        )
        return result
