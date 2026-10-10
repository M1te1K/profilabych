import json
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from test_agent import answer, tool_request
from test_tools import call

import profilabych.tools.current_time as current_time
from profilabych.config import Settings
from profilabych.llm.fake import FakeLLM
from profilabych.main import build_agent
from profilabych.storage.sqlite import SQLiteStorage
from profilabych.timezones import display_datetime
from profilabych.tools.registry import ToolContext

NOW = datetime(2026, 10, 10, 21, 59, 59, 123456, tzinfo=UTC)


class FixedClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("arguments", "default_zone", "expected_zone", "expected_time", "expected_answer"),
    [
        ("{}", "Europe/Moscow", "Europe/Moscow", "00:59", "Сейчас в Москве 00:59"),
        ('{"timezone":null}', "Europe/Moscow", "Europe/Moscow", "00:59", "Сейчас в Москве 00:59"),
        ('{"timezone":"UTC"}', "Europe/Moscow", "UTC", "21:59", "Сейчас по UTC 21:59"),
        (
            '{"timezone":"Asia/Tokyo"}',
            "Europe/Moscow",
            "Asia/Tokyo",
            "06:59",
            "Сейчас в Токио 06:59",
        ),
        ("{}", "Asia/Tokyo", "Asia/Tokyo", "06:59", "Сейчас в Токио 06:59"),
        (
            '{"timezone":"Europe/London"}',
            "Asia/Tokyo",
            "Europe/London",
            "22:59",
            "Сейчас в Лондоне 22:59",
        ),
    ],
)
async def test_clock_uses_default_or_explicit_zone_and_human_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
    default_zone: str,
    expected_zone: str,
    expected_time: str,
    expected_answer: str,
) -> None:
    monkeypatch.setattr(current_time, "datetime", FixedClock)
    async with SQLiteStorage(tmp_path / "clock.db") as storage:
        registry = build_agent(
            FakeLLM([]), storage, Settings(_env_file=None, default_timezone=default_zone)
        )._registry
        result = await registry.execute(
            call("current_time", arguments), ToolContext(42, 42, "time")
        )
        assert result.ok
        assert result.data == {
            "time": expected_time,
            "timezone": expected_zone,
            "answer": expected_answer,
        }


@pytest.mark.parametrize("query", ["Сколько сейчас времени?", "Который час?", "Че по времени?"])
async def test_time_phrases_receive_moscow_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setattr(current_time, "datetime", FixedClock)
    fake = FakeLLM([tool_request("current_time"), answer("Сейчас в Москве 00:59")])
    async with SQLiteStorage(tmp_path / "phrases.db") as storage:
        agent = build_agent(fake, storage, Settings(_env_file=None))
        assert await agent.handle(42, query, chat_id=42) == "Сейчас в Москве 00:59"
        result = json.loads(fake.requests[1].messages[-1].content)
        assert result["data"]["timezone"] == "Europe/Moscow"
        assert result["data"]["answer"] == "Сейчас в Москве 00:59"


@pytest.mark.parametrize(
    ("timestamp", "zone", "expected"),
    [
        ("2026-10-10T21:59:59.123456+00:00", "Europe/Moscow", "11.10.2026 00:59"),
        ("2026-10-10T12:12:00Z", "Europe/Moscow", "10.10.2026 15:12"),
        ("2026-01-10T12:12:00+00:00", "Europe/London", "10.01.2026 12:12"),
        ("2026-07-10T12:12:00+00:00", "Europe/London", "10.07.2026 13:12"),
    ],
)
def test_display_timestamp_converts_utc_including_date_and_dst(
    timestamp: str, zone: str, expected: str
) -> None:
    assert display_datetime(timestamp, ZoneInfo(zone)) == expected


@pytest.mark.parametrize(
    ("zone", "expected"),
    [("Europe/Moscow", "11.10.2026 00:59"), ("Asia/Tokyo", "11.10.2026 06:59")],
)
async def test_note_dates_are_local_for_display_and_utc_in_storage(
    tmp_path: Path, zone: str, expected: str
) -> None:
    path = tmp_path / "notes.db"
    async with SQLiteStorage(path) as storage:
        note_id = await storage.save_note(42, "тестовая заметка")
        stored = (await storage.list_notes(42))[0]
        assert datetime.fromisoformat(stored.created_at).utcoffset().total_seconds() == 0
        timestamp = NOW.isoformat()
        async with aiosqlite.connect(path) as connection:
            await connection.execute(
                "UPDATE notes SET created_at=? WHERE id=? AND user_id=?", (timestamp, note_id, 42)
            )
            await connection.commit()
        agent = build_agent(FakeLLM([]), storage, Settings(_env_file=None, default_timezone=zone))
        result = await agent._registry.execute(call("list_notes"), ToolContext(42, 42, "notes"))
        assert result.ok
        assert result.data["notes"] == [
            {"id": note_id, "text": "тестовая заметка", "created_at": expected}
        ]
        assert result.data["timezone"] == zone
        assert (await storage.list_notes(42))[0].created_at == timestamp


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-10-10T12:12:00"])
def test_invalid_or_naive_stored_timestamp_is_rejected(timestamp: str) -> None:
    with pytest.raises(ValueError):
        display_datetime(timestamp, ZoneInfo("Europe/Moscow"))
