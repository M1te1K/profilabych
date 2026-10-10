"""A small Markdown subset rendered as explicit, validated Telegram entities."""

import re

from aiogram.types import MessageEntity

_MARKUP = re.compile(
    r"(?P<fenced>```.*?(?:```|\Z))"
    r"|(?<![`\\])`(?P<code>[^`\n]+)`(?!`)"
    r"|(?<![*\\])\*\*(?!\*)(?P<bold>\S(?:.*?\S)?)(?<![*\\])\*\*(?!\*)",
    re.DOTALL,
)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def format_text(text: str) -> tuple[str, list[MessageEntity]]:
    """Render paired bold/inline code; keep HTML, links and malformed markup literal."""
    parts: list[str] = []
    entities: list[MessageEntity] = []
    cursor = offset = 0
    for match in _MARKUP.finditer(text):
        plain = text[cursor : match.start()]
        parts.append(plain)
        offset += utf16_length(plain)
        if match.group("fenced") is not None:
            content = match.group(0)
        else:
            kind = "bold" if match.group("bold") is not None else "code"
            content = match.group(kind)
            entities.append(MessageEntity(type=kind, offset=offset, length=utf16_length(content)))
        parts.append(content)
        offset += utf16_length(content)
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts), entities


def chunk_entities(entities: list[MessageEntity], offset: int, length: int) -> list[MessageEntity]:
    """Clip entities to one UTF-16 chunk without reordering or crossing its boundary."""
    result: list[MessageEntity] = []
    for entity in entities:
        start = max(offset, entity.offset)
        end = min(offset + length, entity.offset + entity.length)
        if start < end:
            result.append(
                entity.model_copy(update={"offset": start - offset, "length": end - start})
            )
    return result
