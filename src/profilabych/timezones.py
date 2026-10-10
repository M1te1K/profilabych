"""Timezone validation and human-readable time presentation."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Moscow"
_CITY_LOCATIONS = {
    "Europe/Moscow": "в Москве",
    "Europe/London": "в Лондоне",
    "Europe/Paris": "в Париже",
    "Europe/Berlin": "в Берлине",
    "Asia/Tokyo": "в Токио",
    "Asia/Dubai": "в Дубае",
    "Asia/Yekaterinburg": "в Екатеринбурге",
    "America/New_York": "в Нью-Йорке",
    "UTC": "по UTC",
}


def get_timezone(name: str) -> ZoneInfo:
    """Reject unavailable IANA zones without depending on the host timezone."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown IANA timezone") from None


def current_time_answer(moment: datetime, zone: ZoneInfo) -> str:
    location = _CITY_LOCATIONS.get(zone.key, f"в часовом поясе {zone.key}")
    return f"Сейчас {location} {moment.astimezone(zone):%H:%M}"


def display_datetime(timestamp: str, zone: ZoneInfo) -> str:
    """Convert an aware stored timestamp for display; never alter persisted UTC."""
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Stored timestamp must have a timezone")
    return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M")
