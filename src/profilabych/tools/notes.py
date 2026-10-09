"""User-scoped note tools; ownership comes only from application context."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from profilabych.storage.contracts import NotesRepository
from profilabych.tools.registry import ToolContext, ToolError


class SaveNoteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=2000)


class ListNotesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=1000000)


class NoteTools:
    def __init__(self, repository: NotesRepository, *, max_length: int = 2000) -> None:
        self._repository = repository
        self._max_length = max_length

    async def save_note(
        self, arguments: SaveNoteArguments, context: ToolContext
    ) -> dict[str, JsonValue]:
        if not arguments.text.strip() or len(arguments.text) > self._max_length:
            raise ToolError("invalid_arguments", "Заметка пустая или превышает максимальную длину.")
        return {"note_id": await self._repository.save_note(context.user_id, arguments.text)}

    async def list_notes(
        self, arguments: ListNotesArguments, context: ToolContext
    ) -> dict[str, JsonValue]:
        notes = await self._repository.list_notes(
            context.user_id, limit=arguments.limit, offset=arguments.offset
        )
        return {
            "notes": [
                {"id": note.id, "text": note.text, "created_at": note.created_at} for note in notes
            ],
            "offset": arguments.offset,
            "limit": arguments.limit,
        }
