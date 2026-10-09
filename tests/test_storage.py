import asyncio
from pathlib import Path

import pytest

from profilabych.llm.models import FunctionCall, Message, ToolCall
from profilabych.storage.contracts import StorageError
from profilabych.storage.history import validate_turn
from profilabych.storage.sqlite import SQLiteStorage


def turn(text: str = "hello") -> list[Message]:
    return [Message(role="user", content=text), Message(role="assistant", content="answer")]


async def test_persistence_ownership_order_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "test.sqlite3"
    async with SQLiteStorage(path) as storage:
        first = await storage.save_note(1, "first")
        second = await storage.save_note(1, "second")
        await storage.save_note(2, "private")
        await storage.append_turn(1, turn("a"), 10)
        await storage.append_turn(1, turn("b"), 20)
        await storage.append_turn(2, turn("c"), 10)
        assert [n.id for n in await storage.list_notes(1)] == [first, second]
        assert [n.text for n in await storage.list_notes(2)] == ["private"]
        assert (await storage.load_history(1, 10))[0].content == "a"
        assert (await storage.load_history(1, 20))[0].content == "b"
        await storage.reset_history(1, 10)
        assert not await storage.load_history(1, 10)
        assert await storage.load_history(1, 20)
        await storage.reset_history(1)
        assert not await storage.load_history(1, 20)
        assert await storage.load_history(2, 10)
        assert len(await storage.list_notes(1)) == 2
    async with SQLiteStorage(path) as reopened:
        assert len(await reopened.list_notes(1)) == 2
        assert await reopened.load_history(2, 10)
    with pytest.raises(StorageError, match="закрыта"):
        await reopened.list_notes(1)
    await reopened.aclose()


async def test_sql_injection_is_stored_as_text(tmp_path: Path) -> None:
    text = "'); DELETE FROM notes; DROP TABLE history_turns; --"
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        await storage.save_note(1, text)
        await storage.save_note(2, "other")
        await storage.append_turn(1, turn(text), -100)
        assert (await storage.list_notes(1))[0].text == text
        assert (await storage.list_notes(2))[0].text == "other"
        assert (await storage.load_history(1, -100))[0].content == text


async def test_whole_turn_retention(tmp_path: Path) -> None:
    calls = Message(
        role="assistant",
        tool_calls=(ToolCall(id="call", function=FunctionCall(name="calculator", arguments="{}")),),
    )
    tool_turn = [
        Message(role="user", content="calculate"),
        calls,
        Message(role="tool", content="{}", tool_call_id="call"),
        Message(role="assistant", content="done"),
    ]
    async with SQLiteStorage(tmp_path / "test.db", max_messages=5) as storage:
        await storage.append_turn(1, tool_turn)
        validate_turn(await storage.load_history(1))
        await storage.append_turn(1, turn("new"))
        assert await storage.load_history(1) == turn("new")


async def test_character_retention_and_oversized_turn(tmp_path: Path) -> None:
    async with SQLiteStorage(tmp_path / "test.db", max_chars=2000) as storage:
        await storage.append_turn(1, turn("a" * 1000))
        await storage.append_turn(1, turn("b" * 1000))
        assert (await storage.load_history(1))[0].content == "b" * 1000
        with pytest.raises(StorageError):
            await storage.append_turn(1, turn("c" * 2000))
        assert (await storage.load_history(1))[0].content == "b" * 1000


async def test_invalid_turns_do_not_overwrite_history(tmp_path: Path) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        await storage.append_turn(1, turn())
        with pytest.raises(ValueError):
            await storage.append_turn(1, [Message(role="user", content="incomplete")])
        with pytest.raises(ValueError):
            await storage.append_turn(
                1, [*turn(), Message(role="tool", content="{}", tool_call_id="unknown")]
            )
        assert await storage.load_history(1) == turn()


async def test_sqlite_failure_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    async with SQLiteStorage(path) as storage:
        await storage.save_note(1, "keep")
        async with storage._transaction() as connection:
            await connection.execute(
                "CREATE TRIGGER reject_notes BEFORE INSERT ON notes "
                "BEGIN SELECT RAISE(ABORT, 'private DB detail'); END"
            )
        with pytest.raises(StorageError) as caught:
            await storage.save_note(1, "fail")
        assert "private DB detail" not in str(caught.value)
        assert [n.text for n in await storage.list_notes(1)] == ["keep"]


async def test_cancelled_transaction_rolls_back(tmp_path: Path) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        with pytest.raises(asyncio.CancelledError):
            async with storage._transaction() as connection:
                await connection.execute(
                    "INSERT INTO notes(user_id,text,created_at) VALUES(?,?,?)",
                    (1, "rollback", "now"),
                )
                raise asyncio.CancelledError
        assert not await storage.list_notes(1)
        await storage.save_note(1, "works")


async def test_database_open_failure_and_corrupt_history(tmp_path: Path) -> None:
    path = tmp_path / "directory.db"
    path.mkdir()
    with pytest.raises(StorageError):
        async with SQLiteStorage(path):
            pass
    async with SQLiteStorage(tmp_path / "valid.db") as storage:
        async with storage._transaction() as connection:
            await connection.execute(
                "INSERT INTO history_turns(user_id,chat_id,messages,message_count,char_count) "
                "VALUES(?,?,?,?,?)",
                (1, 0, "bad json", 2, 10),
            )
        with pytest.raises(StorageError, match="повреждена"):
            await storage.load_history(1)


@pytest.mark.parametrize("user_id", [0, -1, True, "1"])
async def test_requires_trusted_integer_user(tmp_path: Path, user_id: object) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        with pytest.raises(ValueError):
            await storage.list_notes(user_id)
        with pytest.raises(ValueError):
            await storage.reset_history(user_id)


@pytest.mark.parametrize("text", ["", "  ", "x" * 2001])
async def test_note_length_limit(tmp_path: Path, text: str) -> None:
    async with SQLiteStorage(tmp_path / "test.db") as storage:
        with pytest.raises(ValueError):
            await storage.save_note(1, text)
