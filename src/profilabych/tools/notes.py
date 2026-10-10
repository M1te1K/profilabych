"""User-scoped note tools; ownership comes only from application context."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from profilabych.storage.contracts import NotesRepository
from profilabych.timezones import DEFAULT_TIMEZONE, display_datetime, get_timezone
from profilabych.tools.registry import ToolContext, ToolError


class SaveNoteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=2000)


class ListNotesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=1000000)


class NoteTools:
    def __init__(
        self,
        repository: NotesRepository,
        *,
        max_length: int = 2000,
        default_timezone: str = DEFAULT_TIMEZONE,
    ) -> None:
        self._repository = repository
        self._max_length = max_length
        self._zone = get_timezone(default_timezone)

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
        try:
            displayed_notes: list[JsonValue] = [
                {
                    "id": note.id,
                    "text": note.text,
                    "created_at": display_datetime(note.created_at, self._zone),
                }
                for note in notes
            ]
        except ValueError:
            raise ToolError("invalid_timestamp", "Дата создания заметки повреждена.") from None
        return {
            "notes": displayed_notes,
            "timezone": self._zone.key,
            "offset": arguments.offset,
            "limit": arguments.limit,
        }
