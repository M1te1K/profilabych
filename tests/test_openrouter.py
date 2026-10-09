import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr

from profilabych.config import Settings
from profilabych.llm.models import LLMError, Message
from profilabych.llm.openrouter import OpenRouterProvider


def settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        openrouter_api_key=SecretStr("fake-test-key"),
        llm_retry_delay_seconds=0,
        **overrides,
    )


def catalog() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "qwen/qwen3-coder:free",
                "pricing": {
                    "prompt": "0",
                    "completion": "0",
                    "request": "0",
                },
                "supported_parameters": ["tools", "tool_choice"],
            }
        ]
    }


def completion(content: str = "Привет") -> dict[str, object]:
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]
    }


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


async def test_request_serialization_cost_guard_and_client_close() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer fake-test-key"
        assert payload["model"] == "qwen/qwen3-coder:free"
        assert payload["messages"] == [{"role": "user", "content": "Привет"}]
        assert payload["provider"] == {
            "max_price": {"prompt": 0, "completion": 0, "request": 0},
            "require_parameters": True,
            "allow_fallbacks": False,
        }
        assert payload["max_tokens"] == 1024 and payload["stream"] is False
        assert payload["tool_choice"] == "required" and payload["tools"] == [{"type": "function"}]
        assert "models" not in payload
        assert request.extensions["timeout"]["read"] == 30
        return httpx.Response(200, json=completion())

    client = client_for(handler)
    async with OpenRouterProvider(client, settings()) as provider:
        response = await provider.complete(
            [Message(role="user", content="Привет")], [{"type": "function"}], tool_choice="required"
        )
        assert response.content == "Привет"
        await provider.complete(
            [Message(role="user", content="Привет")], [{"type": "function"}], tool_choice="required"
        )
        assert len(requests) == 3  # catalog is cached, per-request max_price is always present
    assert client.is_closed


async def test_multiple_tool_calls_and_round_trip_ids() -> None:
    tool_calls = [
        {
            "id": name,
            "type": "function",
            "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'},
        }
        for name in ("a", "b")
    ]
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                    "reasoning": "provider-specific",
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=catalog() if request.url.path.endswith("/models") else response
        )

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        answer = await provider.complete([Message(role="user", content="calculate")], [])
        assert [c.id for c in answer.tool_calls] == ["a", "b"]
        assert answer.to_wire()["tool_calls"] == tool_calls


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retry_exhaustion(status: int) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        count += 1
        return httpx.Response(
            status, text="private raw error fake-test-key", headers={"Retry-After": "0"}
        )

    client = client_for(handler)
    with pytest.raises(LLMError) as caught:
        async with OpenRouterProvider(client, settings()) as provider:
            await provider.complete([Message(role="user", content="hi")], [])
    assert count == 3 and client.is_closed
    assert caught.value.code == ("rate_limit" if status == 429 else "unavailable")
    assert "private" not in str(caught.value) and "fake-test-key" not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 302])
async def test_non_retryable_http_errors(status: int) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        count += 1
        return httpx.Response(
            status, text="secret body", headers={"Location": "https://paid.example.com"}
        )

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        with pytest.raises(LLMError, match="отклонил"):
            await provider.complete([Message(role="user", content="hi")], [])
    assert count == 1


async def test_retry_recovers_and_preserves_messages() -> None:
    count = 0
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        count += 1
        bodies.append(request.content)
        return httpx.Response(
            429 if count == 1 else 200, json=completion(), headers={"Retry-After": "0"}
        )

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        assert (
            await provider.complete([Message(role="user", content="hi")], [])
        ).content == "Привет"
    assert count == 2 and bodies[0] == bodies[1]


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
async def test_transport_unavailability(failure: type[httpx.TransportError]) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        raise failure("raw secret error")

    async with OpenRouterProvider(client_for(handler), settings(llm_max_retries=1)) as provider:
        with pytest.raises(LLMError) as caught:
            await provider.verify_model()
    assert count == 2 and caught.value.code == "unavailable"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        {
            "choices": [
                {"finish_reason": "length", "message": {"role": "assistant", "content": "partial"}}
            ]
        },
        {"error": {"code": 500, "message": "secret"}},
        {
            "choices": [
                {"finish_reason": "stop", "message": {"role": "user", "content": "wrong role"}}
            ]
        },
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": None}}]},
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "bad",
                                "type": "function",
                                "function": {"name": "calculator", "arguments": {}},
                            }
                        ],
                    },
                }
            ]
        },
        {
            "choices": [
                {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "hi"}}
            ]
        },
    ],
)
async def test_invalid_completion(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=catalog() if request.url.path.endswith("/models") else payload
        )

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        with pytest.raises(LLMError) as caught:
            await provider.complete([Message(role="user", content="hi")], [])
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[" * 2000, b"x" * 1_000_001])
async def test_malformed_or_oversized_json(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json=catalog())
            if request.url.path.endswith("/models")
            else httpx.Response(200, content=body)
        )

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        with pytest.raises(LLMError) as caught:
            await provider.complete([Message(role="user", content="hi")], [])
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "variant", ["missing", "paid", "request_price", "no_tools", "unknown_pricing", "bad_catalog"]
)
async def test_free_model_preflight_fails_closed(variant: str) -> None:
    payload = catalog()
    model = payload["data"][0]
    if variant == "missing":
        payload["data"] = []
    elif variant == "paid":
        model["pricing"]["completion"] = "0.1"
    elif variant == "request_price":
        model["pricing"]["request"] = "0.1"
    elif variant == "no_tools":
        model["supported_parameters"] = ["temperature"]
    elif variant == "unknown_pricing":
        model["pricing"] = {}
    else:
        payload = {}
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        with pytest.raises(LLMError):
            await provider.complete([Message(role="user", content="hi")], [])
    assert len(requests) == 1 and requests[0].method == "GET"


async def test_retry_after_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    delays = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "999999"})

    async with OpenRouterProvider(client_for(handler), settings()) as provider:
        with pytest.raises(LLMError):
            await provider.verify_model()
        assert (
            provider._retry_delay(
                httpx.Response(
                    429,
                    headers={
                        "Retry-After": format_datetime(datetime.now(UTC) + timedelta(minutes=10))
                    },
                ),
                0,
            )
            == 5
        )
        assert provider._retry_delay(httpx.Response(429, headers={"Retry-After": "bad"}), 0) == 0
    assert delays == [5, 5]


async def test_missing_key_and_cancelled_request() -> None:
    async with client_for(lambda r: httpx.Response(200)) as client:
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            OpenRouterProvider(client, Settings(_env_file=None))
    started = asyncio.Event()

    async def blocked(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    async with OpenRouterProvider(client, settings()) as provider:
        task = asyncio.create_task(provider.verify_model())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client.is_closed
