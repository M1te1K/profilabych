"""Keep complete conversation turns and matched tool results."""

from collections.abc import Sequence

from profilabych.llm.models import Message


def validate_turn(messages: Sequence[Message]) -> None:
    if len(messages) < 2 or messages[0].role != "user":
        raise ValueError("A turn starts with a user message")
    pending: list[str] = []
    seen: set[str] = set()
    for index, message in enumerate(messages[1:], 1):
        if pending:
            if message.role != "tool" or message.tool_call_id != pending.pop(0):
                raise ValueError("Tool results must match call order and IDs")
        elif message.role != "assistant":
            raise ValueError("Expected an assistant message")
        elif not message.tool_calls and index != len(messages) - 1:
            raise ValueError("Final assistant text must end the turn")
        if message.tool_calls:
            pending = [call.id for call in message.tool_calls]
            if seen.intersection(pending):
                raise ValueError("Duplicate tool call IDs in a turn")
            seen.update(pending)
    if pending or messages[-1].role != "assistant" or messages[-1].tool_calls:
        raise ValueError("A turn ends with an assistant answer")


def history_size(messages: Sequence[Message]) -> int:
    return sum(len(message.model_dump_json()) for message in messages)


def trim_history(
    messages: Sequence[Message], *, max_messages: int, max_chars: int
) -> list[Message]:
    """Evict oldest entire turns, preserving tool-call groups."""
    result = list(messages)
    while result and (len(result) > max_messages or history_size(result) > max_chars):
        next_turn = next(
            (i for i, message in enumerate(result[1:], 1) if message.role == "user"),
            len(result),
        )
        del result[:next_turn]
    return result
