import io
import json
import logging
from pathlib import Path

from pydantic import SecretStr

from profilabych.logging import JsonFormatter, configure_logging


def make_logger(stream: io.StringIO, *secrets: SecretStr) -> logging.Logger:
    logger = logging.Logger("test.profilabych", level=logging.DEBUG)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(secrets))
    logger.addHandler(handler)
    return logger


def test_logs_are_single_json_lines_with_structured_context() -> None:
    stream = io.StringIO()
    logger = make_logger(stream)
    logger.info("готово\nлокально", extra={"context": {"stage": 1, "path": Path("data")}})
    assert len(stream.getvalue().splitlines()) == 1
    event = json.loads(stream.getvalue())
    assert event["level"] == "INFO"
    assert event["logger"] == "test.profilabych"
    assert event["event"] == "готово\nлокально"
    assert event["context"] == {"stage": 1, "path": "data"}
    assert event["timestamp"].endswith("+00:00")


def test_secrets_are_redacted_in_messages_context_and_exceptions() -> None:
    fake_key = "fake-configured-key"
    stream = io.StringIO()
    logger = make_logger(stream, SecretStr(fake_key))
    try:
        raise ValueError(fake_key)
    except ValueError:
        logger.exception(
            "failed: %s",
            fake_key,
            extra={
                "context": {
                    "api_key": "unknown-key",
                    "nested": {"Authorization": "unknown-header", "value": fake_key},
                    "values": [fake_key, SecretStr("another-key")],
                }
            },
        )
    output = stream.getvalue()
    assert fake_key not in output
    assert "unknown-key" not in output
    assert "unknown-header" not in output
    assert "another-key" not in output
    event = json.loads(output)
    assert "ValueError" in event["exception"]
    assert event["context"]["values"] == ["[REDACTED]", "[REDACTED]"]


def test_common_token_patterns_are_redacted_without_registration() -> None:
    stream = io.StringIO()
    logger = make_logger(stream)
    logger.warning(
        "sk-or-v1-fakeToken https://api.telegram.org/bot123456789:%s Bearer abc.xyz", "A" * 35
    )
    output = stream.getvalue()
    assert "fakeToken" not in output
    assert "A" * 35 not in output
    assert "abc.xyz" not in output
    assert json.loads(output)["level"] == "WARNING"


def test_logging_configuration_is_repeatable_and_respects_level() -> None:
    root = logging.getLogger()
    original_handlers, original_level = root.handlers[:], root.level
    noisy_loggers = [
        logging.getLogger("httpx"),
        logging.getLogger("httpcore"),
        logging.getLogger("pydantic_settings"),
    ]
    original_levels = [logger.level for logger in noisy_loggers]
    first, second = io.StringIO(), io.StringIO()
    try:
        configure_logging("INFO", stream=first)
        configure_logging("WARNING", stream=second)
        logger = logging.getLogger("test.configured")
        logger.info("hidden")
        logger.warning("visible")
        assert not first.getvalue()
        assert len(second.getvalue().splitlines()) == 1
        assert json.loads(second.getvalue())["event"] == "visible"
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)
        for logger, level in zip(noisy_loggers, original_levels, strict=True):
            logger.setLevel(level)


def test_redaction_precedes_truncation_and_nested_values_are_bounded() -> None:
    secret = "x" * 1200 + "private-suffix"
    stream = io.StringIO()
    logger = make_logger(stream, SecretStr(secret))
    logger.info(
        secret, extra={"context": {"values": ["v" * 5000] * 100, "password": "private-password"}}
    )
    output = stream.getvalue()
    assert "private-suffix" not in output and "private-password" not in output
    result = json.loads(output)
    assert result["event"] == "[REDACTED]"
    assert len(result["context"]["values"]) == 20
    assert all(len(value) < 1100 for value in result["context"]["values"])


async def test_request_context_isolated_between_async_tasks() -> None:
    import asyncio

    from profilabych.logging import request_logging

    stream = io.StringIO()
    logger = make_logger(stream)

    async def log_for(user_id: int) -> None:
        with request_logging({"user_id": user_id, "request_id": str(user_id)}):
            await asyncio.sleep(0)
            logger.info("nested_provider_event", extra={"context": {"attempt": 1}})

    await asyncio.gather(log_for(1), log_for(2))
    logger.info("outside")
    logs = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["context"]["user_id"] for line in logs[:2]] == [1, 2]
    assert [line["context"]["request_id"] for line in logs[:2]] == ["1", "2"]
    assert "context" not in logs[-1]
