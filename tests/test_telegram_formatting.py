import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from telegram_fakes import BOT_TOKEN, FakeTelegramSession

from profilabych.interfaces.telegram.formatting import format_text
from profilabych.interfaces.telegram.presentation import send_text


def test_bold_and_code_entities_use_utf16_offsets() -> None:
    text, entities = format_text("😺 **ID:** 7; `a < b & c`.")
    assert text == "😺 ID: 7; a < b & c."
    assert [(e.type, e.offset, e.length) for e in entities] == [
        ("bold", 3, 3),
        ("code", 10, 9),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "**незакрыто",
        "** пробелы **",
        "***неподдерживаемо***",
        r"\**экранировано**",
        "<b>текст & данные</b>",
        "[ссылка](https://example.com/?a=1&b=2)",
        "```\n**ID:** literal\n```",
        "```\n**ID:** unclosed block",
    ],
)
def test_unsupported_or_incomplete_markup_stays_literal(text: str) -> None:
    rendered, entities = format_text(text)
    assert rendered == text
    assert not entities


def test_markdown_inside_inline_code_is_literal() -> None:
    text, entities = format_text("`**ID:**` и **текст**")
    assert text == "**ID:** и текст"
    assert [(entity.type, entity.offset, entity.length) for entity in entities] == [
        ("code", 0, 7),
        ("bold", 10, 5),
    ]


@pytest.mark.parametrize(
    "text", ["😺 **" + "😀" * 5000 + "** конец", "x" * 4096 + " " * 5000 + "**ID:**"]
)
async def test_entities_remain_valid_across_long_chunks_and_skipped_whitespace(text: str) -> None:
    session = FakeTelegramSession()
    async with Bot(
        BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML")
    ) as bot:
        assert await send_text(bot, 42, text)
    assert len(session.messages) > 1
    formatted_spans = []
    for message in session.messages:
        assert message.parse_mode is None
        assert message.link_preview_options.is_disabled
        assert message.allow_paid_broadcast is False
        encoded = message.text.encode("utf-16-le")
        units = len(encoded) // 2
        assert 0 < units <= 4096
        for entity in message.entities or []:
            assert entity.type == "bold" and entity.length > 0
            assert 0 <= entity.offset < entity.offset + entity.length <= units
            formatted_spans.append(
                encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
            )
    assert "".join(formatted_spans) == ("😀" * 5000 if text.startswith("😺") else "ID:")


async def test_html_is_never_parsed_while_bold_is_formatted() -> None:
    session = FakeTelegramSession()
    async with Bot(BOT_TOKEN, session=session) as bot:
        assert await send_text(bot, 42, "**ID:** <b>1 & 2</b> _x_ [y](z)")
    message = session.messages[0]
    assert message.text == "ID: <b>1 & 2</b> _x_ [y](z)"
    assert message.parse_mode is None
    assert [(entity.type, entity.offset, entity.length) for entity in message.entities] == [
        ("bold", 0, 3)
    ]


async def test_code_edge_whitespace_does_not_shift_bold_offsets() -> None:
    session = FakeTelegramSession()
    async with Bot(BOT_TOKEN, session=session) as bot:
        assert await send_text(bot, 42, "` x ` **ID:**")
    message = session.messages[0]
    assert message.text == " x  ID:"
    assert [(entity.type, entity.offset, entity.length) for entity in message.entities] == [
        ("code", 0, 3),
        ("bold", 4, 3),
    ]


async def test_whitespace_only_code_uses_unformatted_fallback() -> None:
    session = FakeTelegramSession()
    async with Bot(BOT_TOKEN, session=session) as bot:
        assert await send_text(bot, 42, "`   `")
    assert session.messages[0].text == "Ответ пуст. Попробуйте переформулировать запрос."
    assert session.messages[0].entities is None
