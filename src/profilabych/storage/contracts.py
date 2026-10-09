"""Storage contracts used by the core and tools."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from profilabych.llm.models import Message


class StorageError(Exception):
    """Database operation failed without exposing SQL or user content."""


def validate_identity(user_id: int, chat_id: int | None = None) -> None:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A trusted positive integer user_id is required")
    if chat_id is not None and type(chat_id) is not int:
        raise ValueError("A trusted integer chat_id is required")


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    created_at: str


class HistoryRepository(Protocol):
    async def load_history(self, user_id: int, chat_id: int = 0) -> list[Message]: ...

    async def append_turn(
        self, user_id: int, messages: Sequence[Message], chat_id: int = 0
    ) -> None: ...

    async def reset_history(self, user_id: int, chat_id: int | None = None) -> None: ...


class NotesRepository(Protocol):
    async def save_note(self, user_id: int, text: str) -> int: ...

    async def list_notes(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[Note]: ...
