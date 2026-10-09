"""JSON logging to stdout with defensive secret redaction."""

import json
import logging
import math
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TextIO

from pydantic import SecretStr

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

_SENSITIVE_KEYS = ("token", "secret", "password", "api_key", "apikey", "authorization", "cookie")
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]+\b"),
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
)
_REQUEST_CONTEXT: ContextVar[Mapping[str, object] | None] = ContextVar(
    "request_context", default=None
)


@contextmanager
def request_logging(context: Mapping[str, object]) -> Iterator[None]:
    """Attach request metadata to nested provider logs without sharing user data."""
    token = _REQUEST_CONTEXT.set(context)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


class JsonFormatter(logging.Formatter):
    """Serialize a log record as one JSON line, masking keys and known values."""

    def __init__(self, secrets: Iterable[SecretStr] = ()) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted(
                {secret.get_secret_value() for secret in secrets if secret.get_secret_value()},
                key=len,
                reverse=True,
            )
        )

    def _redact(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        for pattern in _TOKEN_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value

    def _sanitize(
        self, value: object, depth: int = 0, budget: list[int] | None = None
    ) -> JsonValue:
        if budget is None:
            budget = [128]
        budget[0] -= 1
        if depth > 6 or budget[0] < 0:
            return "[TRUNCATED]"
        if isinstance(value, SecretStr):
            return "[REDACTED]"
        if isinstance(value, str):
            redacted = self._redact(value)
            return redacted[:1000] + "[TRUNCATED]" if len(redacted) > 1000 else redacted
        if isinstance(value, float) and not math.isfinite(value):
            return "[NON_FINITE]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Mapping):
            return {
                self._redact(str(key))[:100]: (
                    "[REDACTED]"
                    if any(part in str(key).lower().replace("-", "_") for part in _SENSITIVE_KEYS)
                    else self._sanitize(item, depth + 1, budget)
                )
                for key, item in list(value.items())[:20]
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item, depth + 1, budget) for item in value[:20]]
        return self._sanitize(str(value), depth + 1, budget)

    def format(self, record: logging.LogRecord) -> str:
        """Keep metadata stable and structured context in its own namespace."""
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        context = getattr(record, "context", None)
        inherited = dict(_REQUEST_CONTEXT.get() or {})
        if isinstance(context, Mapping):
            payload["context"] = inherited | dict(context)
        elif context is not None:
            payload["context"] = context
        elif inherited:
            payload["context"] = inherited
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(self._sanitize(payload), ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    *,
    secrets: Iterable[SecretStr] = (),
    stream: TextIO | None = None,
) -> None:
    """Configure once at the application boundary; repeated calls replace handlers."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # HTTP debug logs can expose request bodies, URLs and user content.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("pydantic_settings").setLevel(logging.WARNING)
