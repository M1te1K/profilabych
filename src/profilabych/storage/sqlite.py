"""One managed SQLite connection with serialized, atomic transactions."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import aiosqlite
from pydantic import TypeAdapter, ValidationError

from profilabych.llm.models import Message
from profilabych.storage.contracts import Note, StorageError, validate_identity
from profilabych.storage.history import history_size, validate_turn

_TURN_ADAPTER = TypeAdapter(list[Message])


class SQLiteStorage:
    def __init__(
        self,
        path: Path,
        *,
        max_messages: int = 40,
        max_chars: int = 64000,
        note_max_length: int = 2000,
    ) -> None:
        if max_messages < 2 or max_chars < 2000 or not 1 <= note_max_length <= 2000:
            raise ValueError("Invalid storage limits")
        self._path = path
        self._max_messages = max_messages
        self._max_chars = max_chars
        self._note_max_length = note_max_length
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def open(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            try:
                await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
                self._connection = await aiosqlite.connect(self._path, timeout=5)
                await self._connection.executescript("""
                    PRAGMA journal_mode=WAL;
                    PRAGMA busy_timeout=5000;
                    CREATE TABLE IF NOT EXISTS history_turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        messages TEXT NOT NULL,
                        message_count INTEGER NOT NULL,
                        char_count INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS history_owner
                        ON history_turns(user_id, chat_id, id);
                    CREATE TABLE IF NOT EXISTS notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 2000),
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS notes_owner
                        ON notes(user_id, created_at, id);
                """)
                await self._connection.commit()
            except (sqlite3.Error, OSError):
                await self._close_unlocked()
                raise StorageError("Не удалось открыть базу данных.") from None
            except asyncio.CancelledError:
                await self._close_unlocked()
                raise

    async def _close_unlocked(self) -> None:
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await connection.close()

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            connection = self._connection
            if connection is None:
                raise StorageError("База данных закрыта.")
            try:
                await connection.execute("BEGIN IMMEDIATE")
                yield connection
                await connection.commit()
            except sqlite3.Error:
                await connection.rollback()
                raise StorageError("Ошибка операции с базой данных.") from None
            except (asyncio.CancelledError, ValueError, StorageError):
                await connection.rollback()
                raise

    async def load_history(self, user_id: int, chat_id: int = 0) -> list[Message]:
        validate_identity(user_id, chat_id)
        async with (
            self._transaction() as connection,
            connection.execute(
                "SELECT messages FROM history_turns WHERE user_id=? AND chat_id=? ORDER BY id",
                (user_id, chat_id),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        try:
            turns = [_TURN_ADAPTER.validate_json(row[0]) for row in rows]
            for turn in turns:
                validate_turn(turn)
            return [message for turn in turns for message in turn]
        except (ValidationError, ValueError):
            raise StorageError("Сохранённая история повреждена.") from None

    async def append_turn(
        self, user_id: int, messages: Sequence[Message], chat_id: int = 0
    ) -> None:
        validate_identity(user_id, chat_id)
        validate_turn(messages)
        size = history_size(messages)
        if len(messages) > self._max_messages or size > self._max_chars:
            raise StorageError("Ход диалога превышает лимит истории.")
        payload = json.dumps([message.model_dump(mode="json") for message in messages])
        async with self._transaction() as connection:
            await connection.execute(
                "INSERT INTO history_turns(user_id,chat_id,messages,message_count,char_count) "
                "VALUES(?,?,?,?,?)",
                (user_id, chat_id, payload, len(messages), size),
            )
            async with connection.execute(
                "SELECT id,message_count,char_count FROM history_turns "
                "WHERE user_id=? AND chat_id=? ORDER BY id DESC",
                (user_id, chat_id),
            ) as cursor:
                rows = await cursor.fetchall()
            count = chars = 0
            for row in rows:
                count += row[1]
                chars += row[2]
                if count > self._max_messages or chars > self._max_chars:
                    await connection.execute(
                        "DELETE FROM history_turns WHERE user_id=? AND chat_id=? AND id<=?",
                        (user_id, chat_id, row[0]),
                    )
                    break

    async def reset_history(self, user_id: int, chat_id: int | None = None) -> None:
        validate_identity(user_id, chat_id)
        async with self._transaction() as connection:
            if chat_id is None:
                await connection.execute("DELETE FROM history_turns WHERE user_id=?", (user_id,))
            else:
                await connection.execute(
                    "DELETE FROM history_turns WHERE user_id=? AND chat_id=?", (user_id, chat_id)
                )

    async def save_note(self, user_id: int, text: str) -> int:
        validate_identity(user_id)
        if not text.strip() or len(text) > self._note_max_length:
            raise ValueError("Invalid note length")
        async with (
            self._transaction() as connection,
            connection.execute(
                "INSERT INTO notes(user_id,text,created_at) VALUES(?,?,?)",
                (user_id, text, datetime.now(UTC).isoformat()),
            ) as cursor,
        ):
            note_id = cursor.lastrowid
            if note_id is None:
                raise StorageError("Не удалось получить идентификатор заметки.")
            return note_id

    async def list_notes(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[Note]:
        validate_identity(user_id)
        if not 1 <= limit <= 20 or offset < 0:
            raise ValueError("Invalid pagination")
        async with (
            self._transaction() as connection,
            connection.execute(
                "SELECT id,text,created_at FROM notes WHERE user_id=? "
                "ORDER BY created_at,id LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [Note(id=row[0], text=row[1], created_at=row[2]) for row in rows]
