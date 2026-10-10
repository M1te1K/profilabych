import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from test_openrouter import catalog

import profilabych.main as entrypoint
from profilabych.config import Settings
from profilabych.interfaces.cli import run_cli
from profilabych.llm.fake import FakeLLM
from profilabych.main import build_agent
from profilabych.storage.contracts import StorageError
from profilabych.storage.sqlite import SQLiteStorage


@pytest.mark.parametrize("flag", ["--demo", "--check", "--telegram", None])
def test_local_modes_without_keys(tmp_path: Path, flag: str | None) -> None:
    command = [sys.executable, "-m", "profilabych"]
    if flag:
        command.append(flag)
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert not list(tmp_path.iterdir())
    if flag is None or flag == "--telegram":
        assert result.returncode == 1 and "OPENROUTER_API_KEY" in result.stderr
        if flag == "--telegram":
            assert "TELEGRAM_BOT_TOKEN" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        if flag == "--demo":
            assert '"result":"60"' in result.stdout
            assert "save_note" in result.stdout and "заметка 60 сохранена" in result.stdout
        else:
            assert json.loads(result.stdout)["context"]["stage"] == 3


def test_demo_runs_with_network_connections_forbidden(tmp_path: Path) -> None:
    source = (
        "import sys\n"
        "def forbid(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Network forbidden')\n"
        "sys.addaudithook(forbid)\n"
        "from profilabych.main import main\n"
        "raise SystemExit(main(['--demo']))\n"
    )
    environment = os.environ | {"OPENROUTER_API_KEY": "fake-secret-must-not-be-used"}
    result = subprocess.run(
        [sys.executable, "-c", source],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "fake-secret" not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


async def test_cli_eof_and_controlled_core_errors(tmp_path: Path) -> None:
    output = []
    texts = iter(["message", "/reset", "/exit"])
    storage = SQLiteStorage(tmp_path / "test.db")
    await run_cli(
        build_agent(FakeLLM([]), storage, Settings(_env_file=None)),
        read=lambda prompt: next(texts),
        write=output.append,
    )
    assert "Ошибка базы" in output[1] and "очистить" in output[2]

    def eof(prompt: str) -> str:
        raise EOFError

    await run_cli(
        build_agent(FakeLLM([]), storage, Settings(_env_file=None)), read=eof, write=output.append
    )


async def test_composition_closes_clients_and_database_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=catalog()))
    )
    instances = []

    class ObservedStorage(SQLiteStorage):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(entrypoint.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(entrypoint, "SQLiteStorage", ObservedStorage)

    async def cancel_cli(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(entrypoint, "run_cli", cancel_cli)
    config = Settings(
        _env_file=None, openrouter_api_key=SecretStr("fake-key"), database_path=tmp_path / "test.db"
    )
    with pytest.raises(asyncio.CancelledError):
        await entrypoint._run_online(config, 1, 0)
    assert client.is_closed and len(instances) == 1
    with pytest.raises(StorageError, match="закрыта"):
        await instances[0].list_notes(1)
