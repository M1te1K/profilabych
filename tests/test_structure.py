import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "profilabych.agent",
        "profilabych.llm",
        "profilabych.tools",
        "profilabych.storage",
        "profilabych.interfaces.telegram",
        "aiogram",
        "httpx",
        "aiosqlite",
    ],
)
def test_packages_import_without_credentials(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_offline_entrypoint_creates_no_state(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "profilabych", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    event = json.loads(completed.stdout)
    assert event["event"] == "infrastructure_ready"
    assert event["context"]["mode"] == "offline"
    assert not list(tmp_path.iterdir())


def test_cli_reports_invalid_config_without_exposing_input(tmp_path: Path) -> None:
    environment = os.environ | {"LLM_MAX_TOKENS": "secret-invalid-value"}
    completed = subprocess.run(
        [sys.executable, "-m", "profilabych"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 1
    assert "secret-invalid-value" not in completed.stdout + completed.stderr
    event = json.loads(completed.stderr)
    assert event["event"] == "configuration_invalid"
    assert event["context"]["fields"] == ["llm_max_tokens"]


def test_imports_do_not_access_network_or_create_files(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "def block_network(event, args):\n"
            "    if event.startswith('socket.'):\n"
            "        raise AssertionError('network access: ' + event)\n"
            "sys.addaudithook(block_network)\n"
            "import profilabych.config, profilabych.logging, profilabych.main\n"
            "import profilabych.agent, profilabych.llm, profilabych.tools\n"
            "import profilabych.storage, profilabych.interfaces.telegram\n"
            "raise SystemExit(profilabych.main.main(['--check']))",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert not list(tmp_path.iterdir())
