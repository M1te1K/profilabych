"""Provider-independent messages and completion contract."""

from collections.abc import Sequence
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type ToolChoice = Literal["auto", "none", "required"]


class FunctionCall(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    arguments: str = Field(max_length=8192)


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"] = "function"
    function: FunctionCall


class Message(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=16000)
    tool_calls: tuple[ToolCall, ...] = Field(default=(), max_length=20)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_role(self) -> Self:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("Only assistants can call tools")
        if self.role == "tool":
            if not self.tool_call_id or self.content is None:
                raise ValueError("Tool results require content and tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("Only tool results have tool_call_id")
        if not self.tool_calls and (self.content is None or not self.content.strip()):
            raise ValueError("A message must have text or tool calls")
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate tool_call_id")
        return self

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [call.model_dump(mode="json") for call in self.tool_calls]
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        return result


class LLMError(Exception):
    """Safe public error; never includes response bodies or credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, object]],
        *,
        tool_choice: ToolChoice = "auto",
    ) -> Message: ...
