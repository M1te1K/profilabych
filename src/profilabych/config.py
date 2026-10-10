"""Explicit, validated environment configuration without import-time side effects."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from profilabych.timezones import DEFAULT_TIMEZONE, get_timezone

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Settings shared by adapters; credentials are optional for offline checks/demo."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: LogLevel = "INFO"
    database_path: Path = Path("data/profilabych.sqlite3")
    default_timezone: str = Field(default=DEFAULT_TIMEZONE, min_length=1, max_length=100)
    telegram_bot_token: SecretStr | None = Field(default=None, repr=False)
    openrouter_api_key: SecretStr | None = Field(default=None, repr=False)
    openrouter_base_url: Literal["https://openrouter.ai/api/v1"] = "https://openrouter.ai/api/v1"
    llm_model: str = "qwen/qwen3-coder:free"
    llm_timeout_seconds: float = Field(default=30, gt=0, le=300)
    llm_max_tokens: int = Field(default=1024, ge=1, le=32768)
    llm_max_retries: int = Field(default=2, ge=0, le=3)
    llm_retry_delay_seconds: float = Field(default=0.5, ge=0, le=5)
    agent_max_tool_calls: int = Field(default=5, ge=1, le=20)
    agent_max_tool_rounds: int = Field(default=5, ge=1, le=5)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=30)
    tool_result_max_chars: int = Field(default=8000, ge=256, le=12000)
    history_max_messages: int = Field(default=40, ge=2, le=200)
    history_max_chars: int = Field(default=64000, ge=2000, le=1000000)
    user_input_max_chars: int = Field(default=4000, ge=1, le=8000)
    note_max_length: int = Field(default=2000, ge=1, le=2000)

    @field_validator("telegram_bot_token", "openrouter_api_key", mode="before")
    @classmethod
    def normalize_optional_secret(cls, value: object) -> object:
        """Treat empty dotenv placeholders as absent credentials."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("database_path", mode="before")
    @classmethod
    def reject_empty_database_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("DATABASE_PATH must be a non-empty file path")
        return value

    @field_validator("database_path")
    @classmethod
    def validate_database_path(cls, value: Path) -> Path:
        if value.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise ValueError("DATABASE_PATH must have a SQLite file extension")
        return value

    @field_validator("llm_model")
    @classmethod
    def validate_free_model(cls, value: str) -> str:
        """Reject paid identifiers; the adapter also verifies pricing and capabilities."""
        value = value.strip()
        if not value.endswith(":free") or value.count("/") != 1:
            raise ValueError("LLM_MODEL must be an explicit provider/model:free identifier")
        provider, model = value.removesuffix(":free").split("/")
        if not provider or not model or any(char.isspace() for char in value):
            raise ValueError("LLM_MODEL must be an explicit provider/model:free identifier")
        return value

    @field_validator("default_timezone")
    @classmethod
    def validate_default_timezone(cls, value: str) -> str:
        value = value.strip()
        get_timezone(value)
        return value


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Read settings on demand; environment variables take precedence over dotenv."""
    return Settings(_env_file=env_file)
