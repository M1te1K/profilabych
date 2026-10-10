from pathlib import Path

import pytest
from pydantic import ValidationError

from profilabych.config import Settings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_defaults_need_no_credentials_or_database() -> None:
    settings = load_settings(env_file=None)
    assert settings.telegram_bot_token is None
    assert settings.openrouter_api_key is None
    assert settings.llm_model == "qwen/qwen3-coder:free"
    assert settings.log_level == "INFO"
    assert settings.default_timezone == "Europe/Moscow"
    assert not settings.database_path.exists()


def test_example_is_a_valid_complete_configuration() -> None:
    settings = load_settings(PROJECT_ROOT / ".env.example")
    assert settings.app_env == "development"
    assert settings.openrouter_api_key is None
    assert settings.telegram_bot_token is None


def test_environment_overrides_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LOG_LEVEL=WARNING\nLLM_MAX_TOKENS=512\n", encoding="utf-8")
    monkeypatch.setenv("LOG_LEVEL", " debug ")
    settings = load_settings(dotenv)
    assert settings.log_level == "DEBUG"
    assert settings.llm_max_tokens == 512


def test_default_dotenv_is_loaded(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=test\n", encoding="utf-8")
    assert load_settings().app_env == "test"


def test_explicit_none_skips_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=production\n", encoding="utf-8")
    assert load_settings(env_file=None).app_env == "development"


def test_secret_fields_are_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = "test-only-key-never-valid"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    settings = load_settings(env_file=None)
    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == fake_key
    assert fake_key not in repr(settings)
    assert fake_key not in settings.model_dump_json()


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_empty_credentials_are_optional(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", value)
    monkeypatch.setenv("OPENROUTER_API_KEY", value)
    settings = load_settings(env_file=None)
    assert settings.telegram_bot_token is None
    assert settings.openrouter_api_key is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_ENV", "unknown"),
        ("LOG_LEVEL", "TRACE"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "301"),
        ("LLM_MAX_TOKENS", "-1"),
        ("LLM_MAX_TOKENS", "bad-number"),
        ("AGENT_MAX_TOOL_CALLS", "0"),
        ("AGENT_MAX_TOOL_CALLS", "21"),
        ("HISTORY_MAX_MESSAGES", "1"),
        ("DATABASE_PATH", ""),
        ("DATABASE_PATH", "data"),
        ("LLM_MODEL", "qwen/qwen3-coder"),
        ("LLM_MODEL", "openrouter/free"),
        ("LLM_MODEL", "model:free"),
        ("LLM_MODEL", "/model:free"),
        ("LLM_MODEL", "qwen/:free"),
        ("LLM_MODEL", "qwen/a b:free"),
        ("OPENROUTER_BASE_URL", "http://openrouter.ai/api/v1"),
        ("OPENROUTER_BASE_URL", "https://example.com/api/v1"),
        ("DEFAULT_TIMEZONE", "Unknown/Planet"),
        ("DEFAULT_TIMEZONE", ""),
        ("DEFAULT_TIMEZONE", "   "),
    ],
)
def test_invalid_values_fail(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        load_settings(env_file=None)


def test_model_can_be_changed_without_code_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Synthetic identifier: the test checks configurability, not catalog availability.
    monkeypatch.setenv("LLM_MODEL", "deepseek/test-model:free")
    assert load_settings(env_file=None).llm_model == "deepseek/test-model:free"


def test_default_timezone_can_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_TIMEZONE", " Asia/Tokyo ")
    assert load_settings(env_file=None).default_timezone == "Asia/Tokyo"


def test_unknown_dotenv_field_is_rejected(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_MODLE=typo\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(dotenv)


def test_validation_text_hides_raw_input(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_input = "sensitive-invalid-input"
    monkeypatch.setenv("LLM_MAX_TOKENS", fake_input)
    with pytest.raises(ValidationError) as caught:
        load_settings(env_file=None)
    assert fake_input not in str(caught.value)


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.llm_model = "other/model:free"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_MAX_RETRIES", "4"),
        ("LLM_RETRY_DELAY_SECONDS", "6"),
        ("AGENT_MAX_TOOL_ROUNDS", "6"),
        ("TOOL_TIMEOUT_SECONDS", "0"),
        ("TOOL_RESULT_MAX_CHARS", "255"),
        ("HISTORY_MAX_CHARS", "1999"),
        ("USER_INPUT_MAX_CHARS", "8001"),
        ("NOTE_MAX_LENGTH", "2001"),
    ],
)
def test_stage_two_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        load_settings(env_file=None)
