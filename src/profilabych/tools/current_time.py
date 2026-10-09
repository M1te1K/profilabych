"""Timezone-aware ISO 8601 time using the standard library."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from profilabych.tools.registry import ToolContext, ToolError


class TimeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


async def run_current_time(arguments: TimeArguments, context: ToolContext) -> dict[str, JsonValue]:
    try:
        zone = ZoneInfo(arguments.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError(
            "unknown_timezone", "Неизвестный часовой пояс. Используйте UTC или IANA timezone."
        ) from None
    return {"time": datetime.now(zone).isoformat(), "timezone": arguments.timezone}
