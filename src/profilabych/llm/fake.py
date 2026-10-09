"""Scripted offline provider for tests and the reproducible demo."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from profilabych.llm.models import LLMError, Message, ToolChoice

type FakeReply = Message | LLMError | Callable[[Sequence[Message]], Message]


@dataclass(frozen=True)
class FakeRequest:
    messages: tuple[Message, ...]
    tools: tuple[dict[str, object], ...]
    tool_choice: ToolChoice


class FakeLLM:
    """Explicit scripted responses; never selected as a production fallback."""

    def __init__(self, replies: Sequence[FakeReply]) -> None:
        self._replies = iter(replies)
        self.requests: list[FakeRequest] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, object]],
        *,
        tool_choice: ToolChoice = "auto",
    ) -> Message:
        self.requests.append(FakeRequest(tuple(messages), tuple(tools), tool_choice))
        reply = next(self._replies, LLMError("fake_exhausted", "Сценарий Fake LLM закончился."))
        if isinstance(reply, LLMError):
            raise reply
        return reply(messages) if callable(reply) else reply
