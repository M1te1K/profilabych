"""Bounded asynchronous OpenRouter Chat Completions adapter."""

import asyncio
import json
import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Self

import httpx
from pydantic import ValidationError

from profilabych.config import Settings
from profilabych.llm.models import LLMError, Message, ToolChoice

logger = logging.getLogger(__name__)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RESPONSE_BYTES = 1_000_000


class OpenRouterProvider:
    """Own the injected client; close it on exit, including failed preflight."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        if settings.openrouter_api_key is None:
            raise LLMError("missing_key", "Для OpenRouter нужен OPENROUTER_API_KEY.")
        self._client = client
        self._settings = settings
        self._verified = False
        self._verification_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        delay = min(5.0, self._settings.llm_retry_delay_seconds * 2**attempt)
        if response is not None and (value := response.headers.get("Retry-After")):
            try:
                delay = float(value)
            except ValueError:
                with suppress(ValueError, TypeError, OverflowError):
                    delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        # Even malicious/very large Retry-After cannot cause an unbounded wait.
        return float(max(0.0, min(5.0, delay)))

    async def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        key = self._settings.openrouter_api_key
        assert key is not None
        for attempt in range(self._settings.llm_max_retries + 1):
            response: httpx.Response | None = None
            failure = LLMError("unavailable", "OpenRouter временно недоступен.")
            try:
                async with asyncio.timeout(self._settings.llm_timeout_seconds):
                    async with self._client.stream(
                        method,
                        f"{self._settings.openrouter_base_url}{path}",
                        headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                        json=payload,
                        timeout=self._settings.llm_timeout_seconds,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in _RETRY_STATUSES:
                            if response.status_code == 429:
                                failure = LLMError(
                                    "rate_limit", "Лимит OpenRouter исчерпан. Повторите позже."
                                )
                        elif response.is_error or response.is_redirect:
                            raise LLMError(
                                "http_error", "OpenRouter отклонил запрос. Проверьте ключ и модель."
                            )
                        else:
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > (
                                    _MAX_RESPONSE_BYTES * 8
                                    if path == "/models"
                                    else _MAX_RESPONSE_BYTES
                                ):
                                    raise LLMError(
                                        "invalid_response", "Ответ OpenRouter слишком большой."
                                    )
                            try:
                                return json.loads(data)
                            except (ValueError, UnicodeError, RecursionError):
                                raise LLMError(
                                    "invalid_response", "OpenRouter вернул некорректный JSON."
                                ) from None
            except (httpx.TransportError, TimeoutError):
                failure = LLMError("unavailable", "OpenRouter недоступен или не ответил вовремя.")
            if attempt == self._settings.llm_max_retries:
                raise failure
            logger.warning(
                "llm_retry", extra={"context": {"attempt": attempt + 1, "error": failure.code}}
            )
            await asyncio.sleep(self._retry_delay(response, attempt))
        raise AssertionError("Unreachable retry state")

    async def verify_model(self) -> None:
        """Fail closed: catalog must confirm tools and zero pricing."""
        async with self._verification_lock:
            if self._verified:
                return
            payload = await self._request("GET", "/models")
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise LLMError("invalid_response", "Некорректный каталог моделей OpenRouter.")
            model = next(
                (
                    item
                    for item in payload["data"]
                    if isinstance(item, dict) and item.get("id") == self._settings.llm_model
                ),
                None,
            )
            if model is None:
                raise LLMError("model_unavailable", "Выбранная бесплатная модель недоступна.")
            pricing = model.get("pricing")
            parameters = model.get("supported_parameters")
            if (
                not isinstance(pricing, dict)
                or not isinstance(parameters, list)
                or not all(isinstance(parameter, str) for parameter in parameters)
            ):
                raise LLMError(
                    "model_unavailable", "Не удалось подтвердить тариф и возможности модели."
                )
            try:
                free = all(Decimal(str(pricing[name])) == 0 for name in ("prompt", "completion"))
                free = free and Decimal(str(pricing.get("request", "0"))) == 0
            except (KeyError, InvalidOperation, ValueError):
                free = False
            if not free or not {"tools", "tool_choice"}.issubset(parameters):
                raise LLMError(
                    "model_unavailable",
                    "Модель должна быть бесплатной и поддерживать tools/tool_choice.",
                )
            self._verified = True

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, object]],
        *,
        tool_choice: ToolChoice = "auto",
    ) -> Message:
        await self.verify_model()
        payload: dict[str, object] = {
            "model": self._settings.llm_model,
            "messages": [message.to_wire() for message in messages],
            "max_tokens": self._settings.llm_max_tokens,
            "stream": False,
            "provider": {
                "require_parameters": True,
                "allow_fallbacks": False,
                "max_price": {"prompt": 0, "completion": 0, "request": 0},
            },
        }
        if tools:
            payload.update(tools=list(tools), tool_choice=tool_choice)
        response = await self._request("POST", "/chat/completions", payload)
        try:
            if not isinstance(response, dict) or response.get("error"):
                raise ValueError("Provider error")
            choices = response["choices"]
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
            ):
                raise ValueError("Invalid choices")
            choice = choices[0]
            if choice.get("error") or choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ValueError("Incomplete completion")
            # JSON-mode validation normalizes arrays to immutable tuples.
            raw_message = choice["message"]
            if not isinstance(raw_message, dict):
                raise ValueError("Invalid message")
            raw_calls = raw_message.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                raise ValueError("Invalid tool calls")
            # Some endpoints include a transport index even in non-streaming replies.
            # Preserve the list order and IDs; the index is not part of the core contract.
            tool_calls = [
                {key: value for key, value in call.items() if key != "index"}
                if isinstance(call, dict)
                else call
                for call in raw_calls or []
            ]
            message = Message.model_validate_json(
                json.dumps(
                    {
                        "role": raw_message.get("role"),
                        "content": raw_message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
            )
            if message.role != "assistant" or bool(message.tool_calls) != (
                choice["finish_reason"] == "tool_calls"
            ):
                raise ValueError("Invalid assistant completion")
            return message
        except (KeyError, TypeError, ValueError, ValidationError, RecursionError):
            raise LLMError(
                "invalid_response", "OpenRouter вернул некорректный или незавершённый ответ."
            ) from None
