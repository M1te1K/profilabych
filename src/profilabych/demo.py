"""Offline calculator -> note demonstration against real temporary SQLite."""

import json
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from profilabych.config import Settings
from profilabych.llm.fake import FakeLLM
from profilabych.llm.models import FunctionCall, Message, ToolCall
from profilabych.storage.sqlite import SQLiteStorage


def demo_provider() -> FakeLLM:
    def save_result(messages: Sequence[Message]) -> Message:
        result = json.loads(messages[-1].content or "")
        assert messages[-1].tool_call_id == "calculate_1" and result["ok"]
        return Message(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id="save_1",
                    function=FunctionCall(
                        name="save_note",
                        arguments=json.dumps({"text": result["data"]["result"]}),
                    ),
                ),
            ),
        )

    def answer(messages: Sequence[Message]) -> Message:
        result = json.loads(messages[-1].content or "")
        assert messages[-1].tool_call_id == "save_1" and result["ok"]
        return Message(
            role="assistant",
            content=f"15 * 4 = 60. Сохранено как заметка №{result['data']['note_id']}.",
        )

    return FakeLLM(
        [
            Message(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="calculate_1",
                        function=FunctionCall(
                            name="calculator", arguments='{"expression":"15 * 4"}'
                        ),
                    ),
                ),
            ),
            save_result,
            answer,
        ]
    )


async def run_demo() -> None:
    # Import the composition function here to avoid a main/demo import cycle.
    from profilabych.main import build_agent

    with TemporaryDirectory(prefix="profilabych-demo-") as directory:
        settings = Settings.model_construct(database_path=Path(directory) / "demo.sqlite3")
        provider = demo_provider()
        async with SQLiteStorage(settings.database_path) as storage:
            agent = build_agent(provider, storage, settings)
            query = "Посчитай 15 * 4 и сохрани результат как заметку"
            print(f"Пользователь: {query}")
            result = await agent.handle(1, query)
            history = await storage.load_history(1)
            for message in history:
                for call in message.tool_calls:
                    print(f"Fake LLM -> {call.function.name}: {call.function.arguments}")
                if message.role == "tool":
                    print(f"Инструмент [{message.tool_call_id}] -> {message.content}")
            print(f"Итог: {result}")
            print(f"SQLite: {await storage.list_notes(1)}")
            await agent.reset_history(1)
            assert not await storage.load_history(1)
            assert (await storage.list_notes(1))[0].text == "60"
            print("/reset: история пуста, заметка 60 сохранена. Сеть не использовалась.")
