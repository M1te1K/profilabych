"""Current time in the configured default or an explicitly requested IANA zone."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from profilabych.timezones import DEFAULT_TIMEZONE, current_time_answer, get_timezone
from profilabych.tools.registry import ToolContext, ToolError


class TimeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="IANA timezone только если пользователь явно указал город или часовой пояс.",
    )


async def run_current_time(
    arguments: TimeArguments, context: ToolContext, *, default_timezone: str = DEFAULT_TIMEZONE
) -> dict[str, JsonValue]:
    timezone = arguments.timezone if arguments.timezone is not None else default_timezone
    try:
        zone = get_timezone(timezone)
    except ValueError:
        raise ToolError(
            "unknown_timezone", "Неизвестный часовой пояс. Используйте UTC или IANA timezone."
        ) from None
    moment = datetime.now(zone)
    return {
        "time": moment.strftime("%H:%M"),
        "timezone": zone.key,
        "answer": current_time_answer(moment, zone),
    }
