"""Resource client behavior for the zenture Public API surface."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

import zenture._resources.operations as operations_resource
import zenture.errors as zenture_errors
from zenture import AsyncZentureClient, ZentureClient
from zenture._contract import OperationStatus, PublicOperationResponse
from zenture.errors import ZentureResponseError

if TYPE_CHECKING:
    from collections.abc import Callable

LIVE_CLIENT_KEY = "zt_" + "live_" + "client_123"


OPERATION_PAYLOAD = {
    "operation_id": "op_abc123",
    "status": "queued",
    "result": None,
}
CHAT_SUMMARY = {
    "chat_id": "chat_abc123",
    "created_at": "2026-06-15T10:00:00Z",
    "title": "Demo",
    "updated_at": None,
}
CHAT_TURN = {
    "turn_id": "turn_abc123",
    "created_at": "2026-06-15T10:01:00Z",
    "user_message": "Hello",
    "model_answer": "Hi",
    "model_response_id": "response_abc123",
    "model_response_ids": ["response_abc123"],
}
EVALUATION = {
    "evaluation_id": "eval_abc123",
    "status": "succeeded",
    "score": 0.91,
    "created_at": "2026-06-15T10:02:00Z",
}
PUBLIC_MODEL = {
    "id": "model-public-1",
    "display_name": "General model",
    "modes": ["single", "multi"],
    "capabilities": ["chat"],
    "is_default": True,
    "is_available": True,
    "cost_class": "standard",
    "provider_display_name": None,
    "max_input_tokens": 16000,
}


def _noop_sleep(_seconds: float) -> None:
    return None


def _create_chat(client: ZentureClient) -> PublicOperationResponse:
    return client.chat.create_operation(
        message="Hello",
        idempotency_key="chat-create-1",
        chat_id="chat_abc123",
    )


def _create_input_wizard(client: ZentureClient) -> PublicOperationResponse:
    return client.input_wizard.create(
        prompt="Improve this",
        idempotency_key="wizard-create-1",
    )


def _create_evaluation(client: ZentureClient) -> PublicOperationResponse:
    return client.evaluations.create(
        user_message="Question",
        ai_answer="Answer",
        idempotency_key="evaluation-create-1",
        chat_id="chat_abc123",
        model_response_id="model_response_123",
        turn_id="turn_abc123",
    )


def test_sync_operation_get_validates_contract_model() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OPERATION_PAYLOAD)

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = client.operations.get("op_abc123")

    assert operation.operation_id == "op_abc123"
    assert operation.status is OperationStatus.QUEUED
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://api.zenture.app/v1/operations/op_abc123"
    assert seen[0].headers["authorization"] == f"Bearer {LIVE_CLIENT_KEY}"

    client.close()


def test_sync_operation_get_returns_failed_operation_with_domain_error_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "failed",
                "error": {
                    "code": "chat_execution_failed",
                    "message": "Chat execution failed.",
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = client.operations.get("op_abc123")

    assert operation.status is OperationStatus.FAILED
    assert operation.error is not None
    assert operation.error.code == "chat_execution_failed"
    assert operation.error.message == "Chat execution failed."

    client.close()


def test_sync_operations_wait_returns_succeeded_terminal_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter(("queued", "running", "succeeded"))
    seen_paths: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        status = next(statuses)
        result = None
        if status == "succeeded":
            result = {
                "result_type": "chat",
                "chat_id": "chat_abc123",
                "model_response_id": "response_abc123",
                "model_response_ids": ["response_abc123"],
                "status": "succeeded",
            }
        return httpx.Response(
            200,
            json={"operation_id": "op_abc123", "status": status, "result": result},
        )

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(operations_resource, "sleep_for_polling", record_sleep)
    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = client.operations.wait(
        "op_abc123",
        timeout=10.0,
        initial_interval=1.0,
        max_interval=2.0,
    )

    assert operation.status is OperationStatus.SUCCEEDED
    assert operation.result is not None
    assert operation.result.chat_id == "chat_abc123"
    assert operation.result.model_response_ids == ("response_abc123",)
    assert seen_paths == [
        "/v1/operations/op_abc123",
        "/v1/operations/op_abc123",
        "/v1/operations/op_abc123",
    ]
    assert sleeps == [1.0, 2.0]

    client.close()


def test_sync_operations_wait_returns_failed_operation_with_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "failed",
                "error": {
                    "code": "evaluation_execution_failed",
                    "message": "Evaluation execution failed.",
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = client.operations.wait("op_abc123", timeout=1.0, initial_interval=0.01)

    assert operation.status is OperationStatus.FAILED
    assert operation.error is not None
    assert operation.error.code == "evaluation_execution_failed"

    client.close()


@pytest.mark.parametrize("terminal_status", ["cancelled", "expired"])
def test_sync_operations_wait_returns_cancelled_and_expired_terminal_operations(
    terminal_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"operation_id": "op_abc123", "status": terminal_status},
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = client.operations.wait("op_abc123", timeout=1.0, initial_interval=0.01)

    assert operation.status.value == terminal_status

    client.close()


def test_sync_operations_wait_raises_redacted_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "zt_" + "live_" + "pollingSECRET"
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingTimeoutError) as exc_info:
        client.operations.wait(
            f"op_abc123_{token}",
            timeout=0.000001,
            initial_interval=0.01,
        )

    assert token not in str(exc_info.value)
    assert "timed out" in str(exc_info.value)

    client.close()


def test_sync_operations_wait_does_not_poll_after_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    seen: list[httpx.Request] = []

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    monkeypatch.setattr("zenture._resources.operations.time.monotonic", fake_monotonic)
    monkeypatch.setattr(operations_resource, "sleep_for_polling", fake_sleep)
    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingTimeoutError):
        client.operations.wait("op_abc123", timeout=1.0, initial_interval=1.0)

    assert len(seen) == 1

    client.close()


def test_sync_operations_wait_raises_redacted_local_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "zt_" + "live_" + "stopSECRET"
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "queued"})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingStoppedError) as exc_info:
        client.operations.wait(
            f"op_abc123_{token}",
            timeout=1.0,
            initial_interval=0.01,
            stop=lambda: True,
        )

    assert token not in str(exc_info.value)
    assert "stopped" in str(exc_info.value)
    assert seen == []

    client.close()


def test_sync_resource_response_validation_does_not_chain_raw_payload() -> None:
    token = "zt_" + "live_" + "responseSECRET"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": token})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ZentureResponseError) as exc_info:
        client.operations.get("op_abc123")

    assert token not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None

    client.close()


@pytest.mark.parametrize(
    ("call", "path", "payload"),
    [
        (
            _create_chat,
            "/v1/chat",
            {"message": "Hello", "chat_id": "chat_abc123", "mode": "single"},
        ),
        (
            _create_input_wizard,
            "/v1/input-wizard",
            {"mode": "prompt_improvement", "prompt": "Improve this"},
        ),
        (
            _create_evaluation,
            "/v1/evaluate",
            {
                "user_message": "Question",
                "ai_answer": "Answer",
                "chat_id": "chat_abc123",
                "model_response_id": "model_response_123",
                "turn_id": "turn_abc123",
            },
        ),
    ],
)
def test_sync_mutating_resources_send_idempotency_and_validate_operation(
    call: Callable[[ZentureClient], PublicOperationResponse],
    path: str,
    payload: dict[str, str],
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json=OPERATION_PAYLOAD)

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = call(client)

    assert operation.operation_id == "op_abc123"
    assert seen[0].method == "POST"
    assert seen[0].url.path == path
    assert seen[0].headers["authorization"] == f"Bearer {LIVE_CLIENT_KEY}"
    assert seen[0].headers["idempotency-key"]
    assert seen[0].read() == httpx.Request("POST", "https://api.zenture.app", json=payload).read()

    client.close()


def test_sync_evaluation_create_sends_external_correlation_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json=OPERATION_PAYLOAD)

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    operation = client.evaluations.create(
        user_message="Question",
        ai_answer="Answer",
        idempotency_key="evaluation-create-external-1",
        external_id="customer-eval-123",
        metadata={"customer_id": "safe-correlation"},
    )

    assert operation.operation_id == "op_abc123"
    assert seen[0].url.path == "/v1/evaluate"
    assert (
        seen[0].read()
        == httpx.Request(
            "POST",
            "https://api.zenture.app",
            json={
                "user_message": "Question",
                "ai_answer": "Answer",
                "external_id": "customer-eval-123",
                "metadata": {"customer_id": "safe-correlation"},
            },
        ).read()
    )

    client.close()


def test_sync_chat_read_resources_validate_contract_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chats":
            return httpx.Response(200, json={"chats": [CHAT_SUMMARY], "next_cursor": "cursor_2"})
        if request.url.path == "/v1/chats/chat_abc123":
            return httpx.Response(200, json={"chat": CHAT_SUMMARY, "latest_turns": [CHAT_TURN]})
        if request.url.path == "/v1/chats/chat_abc123/messages":
            return httpx.Response(
                200,
                json={"chat_id": "chat_abc123", "turns": [CHAT_TURN], "next_cursor": None},
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    chats = client.chat.list()
    chat = client.chat.get("chat_abc123")
    messages = client.chat.messages("chat_abc123")

    assert chats.chats[0].chat_id == "chat_abc123"
    assert chats.next_cursor == "cursor_2"
    assert chat.latest_turns[0].turn_id == "turn_abc123"
    assert messages.turns[0].model_answer == "Hi"
    assert messages.turns[0].model_response_id == "response_abc123"
    assert messages.turns[0].model_response_ids == ("response_abc123",)

    client.close()


def test_sync_chat_read_resources_send_pagination_params() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path == "/v1/chats":
            return httpx.Response(200, json={"chats": [CHAT_SUMMARY], "next_cursor": None})
        return httpx.Response(
            200,
            json={"chat_id": "chat_abc123", "turns": [CHAT_TURN], "next_cursor": None},
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.chat.list(limit=25, cursor="cursor_1")
    client.chat.messages("chat_abc123", limit=10, cursor="cursor_2")

    assert seen_urls == [
        "https://api.zenture.app/v1/chats?limit=25&cursor=cursor_1",
        "https://api.zenture.app/v1/chats/chat_abc123/messages?limit=10&cursor=cursor_2",
    ]

    client.close()


@pytest.mark.parametrize(
    ("limit", "cursor", "match"),
    [
        (0, None, "limit"),
        (101, None, "limit"),
        (50, "", "cursor"),
        (50, "x" * 201, "cursor"),
    ],
)
def test_sync_chat_read_resources_validate_pagination_inputs(
    limit: int,
    cursor: str | None,
    match: str,
) -> None:
    client = ZentureClient(api_key=LIVE_CLIENT_KEY)

    with pytest.raises(ValueError, match=match):
        client.chat.list(limit=limit, cursor=cursor)

    with pytest.raises(ValueError, match=match):
        client.chat.messages("chat_abc123", limit=limit, cursor=cursor)

    client.close()


def test_sync_chat_iterators_walk_until_next_cursor_is_none() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path == "/v1/chats":
            cursor = request.url.params.get("cursor")
            if cursor is None:
                return httpx.Response(200, json={"chats": [CHAT_SUMMARY], "next_cursor": "c2"})
            return httpx.Response(
                200,
                json={
                    "chats": [{**CHAT_SUMMARY, "chat_id": "chat_def456"}],
                    "next_cursor": None,
                },
            )
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={"chat_id": "chat_abc123", "turns": [CHAT_TURN], "next_cursor": "m2"},
            )
        return httpx.Response(
            200,
            json={
                "chat_id": "chat_abc123",
                "turns": [{**CHAT_TURN, "turn_id": "turn_def456"}],
                "next_cursor": None,
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    chats = list(client.chat.iter(limit=2))
    turns = list(client.chat.iter_messages("chat_abc123", limit=2))

    assert [chat.chat_id for chat in chats] == ["chat_abc123", "chat_def456"]
    assert [turn.turn_id for turn in turns] == ["turn_abc123", "turn_def456"]
    assert seen_urls == [
        "https://api.zenture.app/v1/chats?limit=2",
        "https://api.zenture.app/v1/chats?limit=2&cursor=c2",
        "https://api.zenture.app/v1/chats/chat_abc123/messages?limit=2",
        "https://api.zenture.app/v1/chats/chat_abc123/messages?limit=2&cursor=m2",
    ]

    client.close()


def test_sync_iterators_continue_across_empty_pages_with_next_cursor() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/chats":
            if cursor is None:
                return httpx.Response(200, json={"chats": [], "next_cursor": "c2"})
            return httpx.Response(200, json={"chats": [CHAT_SUMMARY], "next_cursor": None})
        if cursor is None:
            return httpx.Response(
                200,
                json={"evaluations": [], "next_cursor": "e2"},
            )
        return httpx.Response(
            200,
            json={"evaluations": [EVALUATION], "next_cursor": None},
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    chats = list(client.chat.iter(limit=2))
    evaluations = list(client.evaluations.iter(limit=2))

    assert [chat.chat_id for chat in chats] == ["chat_abc123"]
    assert [evaluation.evaluation_id for evaluation in evaluations] == ["eval_abc123"]
    assert seen_urls == [
        "https://api.zenture.app/v1/chats?limit=2",
        "https://api.zenture.app/v1/chats?limit=2&cursor=c2",
        "https://api.zenture.app/v1/evaluations?limit=2",
        "https://api.zenture.app/v1/evaluations?limit=2&cursor=e2",
    ]

    client.close()


def test_sync_iterators_reject_repeated_next_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chats":
            return httpx.Response(200, json={"chats": [], "next_cursor": "same-cursor"})
        return httpx.Response(200, json={"evaluations": [], "next_cursor": "same-cursor"})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        list(client.chat.iter(limit=2, cursor="same-cursor"))

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        list(client.evaluations.iter(limit=2, cursor="same-cursor"))

    client.close()


def test_sync_iterators_reject_cursor_cycle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        next_cursor = "c3" if cursor == "c2" else "c2"
        if request.url.path == "/v1/chats":
            return httpx.Response(200, json={"chats": [], "next_cursor": next_cursor})
        return httpx.Response(200, json={"evaluations": [], "next_cursor": next_cursor})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        list(client.chat.iter(limit=2))

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        list(client.evaluations.iter(limit=2))

    client.close()


def test_sync_models_resource_lists_models_with_optional_mode_filter() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"models": [PUBLIC_MODEL]})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    all_models = client.models.list()
    multi_models = client.models.list(mode="multi")

    assert all_models.models[0].id == "model-public-1"
    assert multi_models.models[0].modes[1].value == "multi"
    assert seen_urls == [
        "https://api.zenture.app/v1/models",
        "https://api.zenture.app/v1/models?mode=multi",
    ]

    client.close()


def test_sync_run_helpers_require_explicit_idempotency_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("run helper should not send a mutation without idempotency_key")

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(TypeError):
        cast("Any", client.chat.run)(message="Hello")

    with pytest.raises(TypeError):
        cast("Any", client.input_wizard.run)(prompt="Improve this prompt")

    with pytest.raises(TypeError):
        cast("Any", client.evaluations.run)(user_message="Hello", ai_answer="Hi")

    client.close()


def test_sync_chat_create_operation_validates_single_and_multi_model_modes() -> None:
    seen_json: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_json.append(request.read())
        return httpx.Response(202, json=OPERATION_PAYLOAD)

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    single = client.chat.create_operation(
        message="Hello",
        idempotency_key="chat-single-1",
        mode="single",
        model="model-public-1",
    )
    multi = client.chat.create_operation(
        message="Hello",
        idempotency_key="chat-multi-1",
        mode="multi",
        models=["model-public-1", "model-public-2"],
    )

    assert single.operation_id == "op_abc123"
    assert multi.operation_id == "op_abc123"
    assert (
        seen_json[0]
        == httpx.Request(
            "POST",
            "https://api.zenture.app",
            json={"message": "Hello", "mode": "single", "model": "model-public-1"},
        ).read()
    )
    assert (
        seen_json[1]
        == httpx.Request(
            "POST",
            "https://api.zenture.app",
            json={
                "message": "Hello",
                "mode": "multi",
                "models": ["model-public-1", "model-public-2"],
            },
        ).read()
    )

    client.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "single", "models": ["model-public-1"]},
        {"mode": "multi"},
        {"mode": "multi", "model": "model-public-1", "models": ["model-public-2"]},
        {"mode": "multi", "models": ["model-public-1", "model-public-1"]},
        {"mode": "multi", "models": ["model-1", "model-2", "model-3", "model-4"]},
        {"mode": "agentic"},
    ],
)
def test_sync_chat_create_operation_rejects_invalid_model_mode_combinations(
    kwargs: dict[str, Any],
) -> None:
    client = ZentureClient(api_key=LIVE_CLIENT_KEY)

    with pytest.raises(ValueError, match=r"single|multi|models|Input should|agentic"):
        client.chat.create_operation(
            message="Hello",
            idempotency_key="invalid-chat-mode",
            **kwargs,
        )

    client.close()


def test_sync_chat_run_polls_operation_and_returns_idempotency_metadata() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "succeeded",
                "result": {
                    "result_type": "chat",
                    "chat_id": "chat_abc123",
                    "status": "completed",
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.chat.run(
        message="Hello",
        mode="multi",
        models=["model-public-1", "model-public-2"],
        idempotency_key="chat-run-1",
        poll_interval=0.01,
        timeout=1.0,
    )

    assert result.operation_id == "op_abc123"
    assert result.status is OperationStatus.SUCCEEDED
    assert result.idempotency_key == "chat-run-1"
    assert hasattr(result, "last_request_id")
    assert result.result is not None
    assert result.result.chat_id == "chat_abc123"
    assert seen_paths == ["/v1/chat", "/v1/operations/op_abc123"]

    client.close()


def test_sync_chat_run_accepts_nested_live_chat_operation_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "succeeded",
                "result": {
                    "chat": {
                        "result_type": "chat",
                        "chat_id": "chat_abc123",
                        "turn_id": "turn_abc123",
                        "model_response_id": "response_abc123",
                        "model_response_ids": ["response_abc123"],
                        "status": "completed",
                    }
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.chat.run(
        message="Hello",
        mode="single",
        idempotency_key="chat-run-nested-1",
        poll_interval=0.01,
        timeout=1.0,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.result is not None
    assert result.result.result_type == "chat"
    assert result.result.chat_id == "chat_abc123"
    assert result.result.turn_id == "turn_abc123"
    assert result.result.model_response_id == "response_abc123"
    assert result.result.model_response_ids == ("response_abc123",)

    client.close()


def test_sync_chat_run_include_content_attaches_matching_turn() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        if request.url.path == "/v1/operations/op_abc123":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_abc123",
                    "status": "succeeded",
                    "result": {
                        "result_type": "chat",
                        "chat_id": "chat_abc123",
                        "turn_id": "turn_abc123",
                        "model_response_id": "response_abc123",
                    },
                },
            )
        if request.url.path == "/v1/chats/chat_abc123/messages":
            return httpx.Response(
                200,
                json={
                    "chat_id": "chat_abc123",
                    "turns": [CHAT_TURN],
                    "next_cursor": None,
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.chat.run(
        message="Hello",
        idempotency_key="chat-run-content-1",
        poll_interval=0.01,
        timeout=1.0,
        include_content=True,
    )

    assert result.result is not None
    assert result.result.chat_id == "chat_abc123"
    assert result.chat_turn is not None
    assert result.chat_turn.turn_id == "turn_abc123"
    assert result.chat_turn.user_message == "Hello"
    assert seen_paths == [
        "/v1/chat",
        "/v1/operations/op_abc123",
        "/v1/chats/chat_abc123/messages",
    ]

    client.close()


def test_sync_input_wizard_run_creates_and_waits_with_explicit_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[str | None] = []
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/input-wizard":
            seen_headers.append(request.headers.get("idempotency-key"))
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "succeeded",
                "result": {
                    "result_type": "input_wizard",
                    "resource_id": "wizard_abc123",
                    "input_wizard_id": "wizard_abc123",
                    "optimized_prompt": "Improved prompt text",
                    "status": "completed",
                    "wizard_session_id": "session_abc123",
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.input_wizard.run(
        prompt="Improve this prompt",
        idempotency_key="input-wizard-run-1",
        timeout=1.0,
        initial_interval=0.01,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.result is not None
    assert result.result.result_type == "input_wizard"
    assert result.result.input_wizard_id == "wizard_abc123"
    assert result.result.optimized_prompt == "Improved prompt text"
    assert result.result.wizard_session_id == "session_abc123"
    assert result.idempotency_key == seen_headers[0]
    assert result.last_request_id is None

    client.close()


def test_sync_input_wizard_run_returns_terminal_create_response_without_polling() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "succeeded",
                "result": {
                    "result_type": "input_wizard",
                    "resource_id": "wizard_abc123",
                    "input_wizard_id": "wizard_abc123",
                    "optimized_prompt": "Direct optimized prompt",
                    "status": "succeeded",
                    "wizard_session_id": "session_abc123",
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.input_wizard.run(
        prompt="Improve this prompt",
        idempotency_key="wizard-terminal-create-1",
        timeout=1.0,
        initial_interval=0.01,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.result is not None
    assert result.result.optimized_prompt == "Direct optimized prompt"
    assert seen_paths == ["/v1/input-wizard"]

    client.close()


def test_sync_evaluations_run_creates_and_waits_with_explicit_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[str | None] = []
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/evaluate":
            seen_headers.append(request.headers.get("idempotency-key"))
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "succeeded",
                "result": {"result_type": "evaluation", "evaluation_id": "eval_abc123"},
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.evaluations.run(
        user_message="Question",
        ai_answer="Answer",
        idempotency_key="evaluation-run-1",
        timeout=1.0,
        initial_interval=0.01,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.result is not None
    assert result.result.evaluation_id == "eval_abc123"
    assert result.idempotency_key == "evaluation-run-1"
    assert seen_headers == ["evaluation-run-1"]

    client.close()


def test_sync_evaluations_run_include_detail_attaches_evaluation() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/evaluate":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        if request.url.path == "/v1/operations/op_abc123":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_abc123",
                    "status": "succeeded",
                    "result": {"result_type": "evaluation", "evaluation_id": "eval_abc123"},
                },
            )
        if request.url.path == "/v1/evaluations/eval_abc123":
            return httpx.Response(200, json=EVALUATION)
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.evaluations.run(
        user_message="Question",
        ai_answer="Answer",
        idempotency_key="evaluation-detail-1",
        timeout=1.0,
        initial_interval=0.01,
        include_detail=True,
    )

    assert result.result is not None
    assert result.result.evaluation_id == "eval_abc123"
    assert result.evaluation is not None
    assert result.evaluation.evaluation_id == "eval_abc123"
    assert result.evaluation.score == 0.91
    assert seen_paths == [
        "/v1/evaluate",
        "/v1/operations/op_abc123",
        "/v1/evaluations/eval_abc123",
    ]

    client.close()


def test_sync_chat_run_returns_terminal_operation_error_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "failed",
                "error": {
                    "code": "chat_execution_failed",
                    "message": "Chat execution failed.",
                },
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.chat.run(
        message="Hello",
        idempotency_key="chat-run-failed-1",
        poll_interval=0.01,
        timeout=1.0,
    )

    assert result.status is OperationStatus.FAILED
    assert result.error is not None
    assert result.error.code == "chat_execution_failed"
    assert result.error.message == "Chat execution failed."

    client.close()


def test_sync_chat_run_timeout_exposes_recovery_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[str | None] = []
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat":
            seen_headers.append(request.headers.get("idempotency-key"))
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingTimeoutError) as exc_info:
        client.chat.run(
            message="Hello",
            idempotency_key="chat-run-timeout-1",
            timeout=0.000001,
            initial_interval=0.01,
        )

    assert exc_info.value.operation_id == "op_abc123"
    assert exc_info.value.idempotency_key == "chat-run-timeout-1"
    assert seen_headers == ["chat-run-timeout-1"]
    assert exc_info.value.idempotency_key not in str(exc_info.value)
    assert exc_info.value.idempotency_key not in repr(exc_info.value)

    client.close()


def test_sync_input_wizard_run_stop_exposes_recovery_metadata() -> None:
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/input-wizard":
            seen_headers.append(request.headers.get("idempotency-key"))
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingStoppedError) as exc_info:
        client.input_wizard.run(
            prompt="Improve this",
            idempotency_key="wizard-run-stop-1",
            timeout=1.0,
            initial_interval=0.01,
            stop=lambda: True,
        )

    assert exc_info.value.operation_id == "op_abc123"
    assert exc_info.value.idempotency_key == "wizard-run-stop-1"
    assert seen_headers == ["wizard-run-stop-1"]
    assert "wizard-run-stop-1" not in str(exc_info.value)
    assert "wizard-run-stop-1" not in repr(exc_info.value)

    client.close()


def test_sync_evaluations_run_timeout_exposes_recovery_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_resource, "sleep_for_polling", _noop_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/evaluate":
            return httpx.Response(
                202,
                json={"operation_id": "op_eval123", "status": "running", "result": None},
            )
        return httpx.Response(200, json={"operation_id": "op_eval123", "status": "running"})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingTimeoutError) as exc_info:
        client.evaluations.run(
            user_message="Question",
            ai_answer="Answer",
            idempotency_key="evaluation-run-timeout-1",
            timeout=0.000001,
            initial_interval=0.01,
        )

    assert exc_info.value.operation_id == "op_eval123"
    assert exc_info.value.idempotency_key == "evaluation-run-timeout-1"
    assert "evaluation-run-timeout-1" not in str(exc_info.value)
    assert "evaluation-run-timeout-1" not in repr(exc_info.value)

    client.close()


def test_sync_evaluation_and_account_read_resources_validate_contract_models() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path == "/v1/evaluations":
            return httpx.Response(200, json={"evaluations": [EVALUATION], "next_cursor": None})
        if request.url.path == "/v1/evaluations/eval_abc123":
            return httpx.Response(200, json=EVALUATION)
        if request.url.path == "/v1/wallet":
            return httpx.Response(
                200,
                json={
                    "plan": "pro",
                    "status": "active",
                    "credits_available": {"amount": "123.45", "unit": "credits"},
                },
            )
        if request.url.path == "/v1/usage":
            return httpx.Response(200, json={"scope": "all", "operation_count": 7})
        if request.url.path == "/v1/limits":
            return httpx.Response(
                200,
                json={
                    "routes": {
                        "POST /v1/chat": {
                            "auth_mode": "api_token",
                            "scopes": ["chat:create"],
                            "cost_class": "expensive",
                            "idempotency_required": True,
                            "cors_policy": "server_only",
                            "max_body_bytes": 65536,
                            "rate_limit_per_minute": 10,
                        }
                    },
                    "operation_statuses": ["queued", "running", "succeeded"],
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    evaluations = client.evaluations.list()
    evaluation = client.evaluations.get("eval_abc123")
    wallet = client.wallet.get()
    usage = client.usage.get(scope="all")
    limits = client.limits.get()

    assert evaluations.evaluations[0].evaluation_id == "eval_abc123"
    assert evaluation.score == 0.91
    assert wallet.plan == "pro"
    assert wallet.credits_available.amount == "123.45"
    assert usage.operation_count == 7
    assert limits.routes["POST /v1/chat"].idempotency_required is True
    assert "https://api.zenture.app/v1/usage?scope=all" in seen_urls

    client.close()


def test_sync_evaluations_list_sends_pagination_params_and_iterates() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={"evaluations": [EVALUATION], "next_cursor": "eval_cursor_2"},
            )
        return httpx.Response(
            200,
            json={
                "evaluations": [{**EVALUATION, "evaluation_id": "eval_def456"}],
                "next_cursor": None,
            },
        )

    client = ZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = client.evaluations.list(limit=20, cursor="eval_cursor_1")
    evaluations = list(client.evaluations.iter(limit=2))

    assert page.evaluations[0].evaluation_id == "eval_def456"
    assert [evaluation.evaluation_id for evaluation in evaluations] == [
        "eval_abc123",
        "eval_def456",
    ]
    assert seen_urls == [
        "https://api.zenture.app/v1/evaluations?limit=20&cursor=eval_cursor_1",
        "https://api.zenture.app/v1/evaluations?limit=2",
        "https://api.zenture.app/v1/evaluations?limit=2&cursor=eval_cursor_2",
    ]

    client.close()


@pytest.mark.parametrize(
    ("limit", "cursor", "match"),
    [
        (0, None, "limit"),
        (101, None, "limit"),
        (50, "", "cursor"),
        (50, "x" * 201, "cursor"),
    ],
)
def test_sync_evaluations_list_validates_pagination_inputs(
    limit: int,
    cursor: str | None,
    match: str,
) -> None:
    client = ZentureClient(api_key=LIVE_CLIENT_KEY)

    with pytest.raises(ValueError, match=match):
        client.evaluations.list(limit=limit, cursor=cursor)

    client.close()


@pytest.mark.asyncio
async def test_async_resources_match_sync_surface() -> None:
    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        if request.url.path == "/v1/operations/op_abc123":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_abc123",
                    "status": "succeeded",
                    "result": {"result_type": "chat", "chat_id": "chat_abc123"},
                },
            )
        if request.url.path == "/v1/evaluations":
            return httpx.Response(200, json={"evaluations": [EVALUATION], "next_cursor": None})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [PUBLIC_MODEL]})
        if request.url.path == "/v1/wallet":
            return httpx.Response(
                200,
                json={
                    "plan": "core",
                    "status": "core",
                    "credits_available": {"amount": "123.45", "unit": "credits"},
                },
            )
        if request.url.path == "/v1/limits":
            return httpx.Response(
                200,
                json={
                    "routes": {},
                    "operation_statuses": ["queued", "running", "succeeded"],
                },
            )
        return httpx.Response(200, json={"scope": "api", "operation_count": 1})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    created = await client.chat.create_operation(
        message="Hello",
        idempotency_key="async-chat-create",
    )
    run = await client.chat.run(
        message="Hello",
        mode="multi",
        models=["model-public-1", "model-public-2"],
        idempotency_key="async-chat-run",
        poll_interval=0.01,
        timeout=1.0,
    )
    operation = await client.operations.get("op_abc123")
    evaluations = await client.evaluations.list()
    models = await client.models.list(mode="single")
    wallet = await client.wallet.get()
    usage = await client.usage.get()
    limits = await client.limits.get()

    assert created.status is OperationStatus.QUEUED
    assert run.status is OperationStatus.SUCCEEDED
    assert operation.operation_id == "op_abc123"
    assert evaluations.evaluations[0].status == "succeeded"
    assert models.models[0].id == "model-public-1"
    assert wallet.credits_available.amount == "123.45"
    assert usage.scope == "api"
    assert limits.operation_statuses == (
        OperationStatus.QUEUED,
        OperationStatus.RUNNING,
        OperationStatus.SUCCEEDED,
    )
    assert seen_paths == [
        "/v1/chat",
        "/v1/chat",
        "/v1/operations/op_abc123",
        "/v1/operations/op_abc123",
        "/v1/evaluations",
        "/v1/models",
        "/v1/wallet",
        "/v1/usage",
        "/v1/limits",
    ]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_paginated_resources_send_params_and_iterate() -> None:
    seen_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/chats":
            if cursor is None:
                return httpx.Response(200, json={"chats": [CHAT_SUMMARY], "next_cursor": "c2"})
            return httpx.Response(
                200,
                json={
                    "chats": [{**CHAT_SUMMARY, "chat_id": "chat_def456"}],
                    "next_cursor": None,
                },
            )
        if request.url.path == "/v1/chats/chat_abc123/messages":
            if cursor is None:
                return httpx.Response(
                    200,
                    json={"chat_id": "chat_abc123", "turns": [CHAT_TURN], "next_cursor": "m2"},
                )
            return httpx.Response(
                200,
                json={
                    "chat_id": "chat_abc123",
                    "turns": [{**CHAT_TURN, "turn_id": "turn_def456"}],
                    "next_cursor": None,
                },
            )
        if cursor is None:
            return httpx.Response(
                200,
                json={"evaluations": [EVALUATION], "next_cursor": "e2"},
            )
        return httpx.Response(
            200,
            json={
                "evaluations": [{**EVALUATION, "evaluation_id": "eval_def456"}],
                "next_cursor": None,
            },
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    chats = [chat async for chat in client.chat.iter(limit=2)]
    turns = [turn async for turn in client.chat.iter_messages("chat_abc123", limit=2)]
    evaluations = [evaluation async for evaluation in client.evaluations.iter(limit=2)]

    assert [chat.chat_id for chat in chats] == ["chat_abc123", "chat_def456"]
    assert [turn.turn_id for turn in turns] == ["turn_abc123", "turn_def456"]
    assert [evaluation.evaluation_id for evaluation in evaluations] == [
        "eval_abc123",
        "eval_def456",
    ]
    assert seen_urls == [
        "https://api.zenture.app/v1/chats?limit=2",
        "https://api.zenture.app/v1/chats?limit=2&cursor=c2",
        "https://api.zenture.app/v1/chats/chat_abc123/messages?limit=2",
        "https://api.zenture.app/v1/chats/chat_abc123/messages?limit=2&cursor=m2",
        "https://api.zenture.app/v1/evaluations?limit=2",
        "https://api.zenture.app/v1/evaluations?limit=2&cursor=e2",
    ]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_iterators_continue_across_empty_pages_with_next_cursor() -> None:
    seen_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/chats":
            if cursor is None:
                return httpx.Response(200, json={"chats": [], "next_cursor": "c2"})
            return httpx.Response(200, json={"chats": [CHAT_SUMMARY], "next_cursor": None})
        if cursor is None:
            return httpx.Response(
                200,
                json={"evaluations": [], "next_cursor": "e2"},
            )
        return httpx.Response(
            200,
            json={"evaluations": [EVALUATION], "next_cursor": None},
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    chats = [chat async for chat in client.chat.iter(limit=2)]
    evaluations = [evaluation async for evaluation in client.evaluations.iter(limit=2)]

    assert [chat.chat_id for chat in chats] == ["chat_abc123"]
    assert [evaluation.evaluation_id for evaluation in evaluations] == ["eval_abc123"]
    assert seen_urls == [
        "https://api.zenture.app/v1/chats?limit=2",
        "https://api.zenture.app/v1/chats?limit=2&cursor=c2",
        "https://api.zenture.app/v1/evaluations?limit=2",
        "https://api.zenture.app/v1/evaluations?limit=2&cursor=e2",
    ]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_iterators_reject_repeated_next_cursor() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chats":
            return httpx.Response(200, json={"chats": [], "next_cursor": "same-cursor"})
        return httpx.Response(200, json={"evaluations": [], "next_cursor": "same-cursor"})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        [chat async for chat in client.chat.iter(limit=2, cursor="same-cursor")]

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        [evaluation async for evaluation in client.evaluations.iter(limit=2, cursor="same-cursor")]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_iterators_reject_cursor_cycle() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        next_cursor = "c3" if cursor == "c2" else "c2"
        if request.url.path == "/v1/chats":
            return httpx.Response(200, json={"chats": [], "next_cursor": next_cursor})
        return httpx.Response(200, json={"evaluations": [], "next_cursor": next_cursor})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        [chat async for chat in client.chat.iter(limit=2)]

    with pytest.raises(ZentureResponseError, match="repeated next_cursor"):
        [evaluation async for evaluation in client.evaluations.iter(limit=2)]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_run_helpers_require_explicit_idempotency_key() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("run helper should not send a mutation without idempotency_key")

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(TypeError):
        await cast("Any", client.chat.run)(message="Hello")

    with pytest.raises(TypeError):
        await cast("Any", client.input_wizard.run)(prompt="Improve this prompt")

    with pytest.raises(TypeError):
        await cast("Any", client.evaluations.run)(user_message="Hello", ai_answer="Hi")

    await client.aclose()


@pytest.mark.asyncio
async def test_async_operations_wait_returns_succeeded_terminal_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter(("queued", "succeeded"))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def handler(_request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        result = None
        if status == "succeeded":
            result = {"result_type": "chat", "chat_id": "chat_abc123"}
        return httpx.Response(
            200,
            json={"operation_id": "op_abc123", "status": status, "result": result},
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(operations_resource, "async_sleep_for_polling", fake_sleep)
    operation = await client.operations.wait(
        "op_abc123",
        timeout=10.0,
        initial_interval=1.0,
        max_interval=8.0,
    )

    assert operation.status is OperationStatus.SUCCEEDED
    assert operation.result is not None
    assert operation.result.chat_id == "chat_abc123"
    assert sleeps == [1.0]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_operations_wait_returns_failed_operation_with_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "failed",
                "error": {
                    "code": "input_wizard_failed",
                    "message": "Input wizard failed.",
                },
            },
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    operation = await client.operations.wait("op_abc123", timeout=1.0, initial_interval=0.01)

    assert operation.status is OperationStatus.FAILED
    assert operation.error is not None
    assert operation.error.code == "input_wizard_failed"

    await client.aclose()


@pytest.mark.asyncio
async def test_async_operations_wait_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    async def fake_sleep(_seconds: float) -> None:
        return None

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(operations_resource, "async_sleep_for_polling", fake_sleep)
    with pytest.raises(zenture_errors.ZenturePollingTimeoutError):
        await client.operations.wait("op_abc123", timeout=0.000001, initial_interval=0.01)

    await client.aclose()


@pytest.mark.asyncio
async def test_async_operations_wait_does_not_poll_after_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    seen: list[httpx.Request] = []

    def fake_monotonic() -> float:
        return clock

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    monkeypatch.setattr("zenture._resources.operations.time.monotonic", fake_monotonic)
    monkeypatch.setattr(operations_resource, "async_sleep_for_polling", fake_sleep)
    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingTimeoutError):
        await client.operations.wait("op_abc123", timeout=1.0, initial_interval=1.0)

    assert len(seen) == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_async_operations_wait_propagates_task_cancellation() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    task = asyncio.create_task(
        client.operations.wait(
            "op_abc123",
            timeout=10.0,
            initial_interval=10.0,
            max_interval=10.0,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    await client.aclose()


@pytest.mark.asyncio
async def test_async_operations_wait_immediate_stop_does_not_send_request() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingStoppedError) as exc_info:
        await client.operations.wait("op_abc123", stop=lambda: True)

    assert exc_info.value.operation_id == "op_abc123"
    assert seen == []

    await client.aclose()


@pytest.mark.asyncio
async def test_async_chat_run_stop_exposes_recovery_metadata() -> None:
    seen_headers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat":
            seen_headers.append(request.headers.get("idempotency-key"))
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(200, json={"operation_id": "op_abc123", "status": "running"})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(zenture_errors.ZenturePollingStoppedError) as exc_info:
        await client.chat.run(
            message="Hello",
            idempotency_key="async-chat-run-stop-1",
            timeout=1.0,
            initial_interval=0.01,
            stop=lambda: True,
        )

    assert exc_info.value.operation_id == "op_abc123"
    assert exc_info.value.idempotency_key == "async-chat-run-stop-1"
    assert seen_headers == ["async-chat-run-stop-1"]
    assert "async-chat-run-stop-1" not in str(exc_info.value)
    assert "async-chat-run-stop-1" not in repr(exc_info.value)

    await client.aclose()


@pytest.mark.asyncio
async def test_async_input_wizard_and_evaluations_run_create_and_wait() -> None:
    seen: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/input-wizard":
            seen.append(("input_wizard", request.headers.get("idempotency-key")))
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        if request.url.path == "/v1/evaluate":
            seen.append(("evaluation", request.headers.get("idempotency-key")))
            return httpx.Response(
                202,
                json={"operation_id": "op_eval123", "status": "running", "result": None},
            )
        if request.url.path == "/v1/operations/op_abc123":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_abc123",
                    "status": "succeeded",
                    "result": {
                        "result_type": "input_wizard",
                        "resource_id": "wizard_abc123",
                        "input_wizard_id": "wizard_abc123",
                        "optimized_prompt": "Improved async prompt text",
                        "status": "completed",
                        "wizard_session_id": "session_async123",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "operation_id": "op_eval123",
                "status": "succeeded",
                "result": {"result_type": "evaluation", "evaluation_id": "eval_abc123"},
            },
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    wizard = await client.input_wizard.run(
        prompt="Improve this",
        idempotency_key="wizard-run-1",
        timeout=1.0,
        initial_interval=0.01,
    )
    evaluation = await client.evaluations.run(
        user_message="Question",
        ai_answer="Answer",
        idempotency_key="evaluation-run-async-1",
        timeout=1.0,
        initial_interval=0.01,
    )

    assert wizard.status is OperationStatus.SUCCEEDED
    assert wizard.result is not None
    assert wizard.result.input_wizard_id == "wizard_abc123"
    assert wizard.result.optimized_prompt == "Improved async prompt text"
    assert wizard.result.wizard_session_id == "session_async123"
    assert wizard.idempotency_key == "wizard-run-1"
    assert evaluation.status is OperationStatus.SUCCEEDED
    assert evaluation.result is not None
    assert evaluation.result.evaluation_id == "eval_abc123"
    assert seen == [
        ("input_wizard", "wizard-run-1"),
        ("evaluation", "evaluation-run-async-1"),
    ]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_chat_run_include_content_attaches_matching_turn() -> None:
    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        if request.url.path == "/v1/operations/op_abc123":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_abc123",
                    "status": "succeeded",
                    "result": {
                        "result_type": "chat",
                        "chat_id": "chat_abc123",
                        "turn_id": "turn_abc123",
                        "model_response_id": "response_abc123",
                    },
                },
            )
        if request.url.path == "/v1/chats/chat_abc123/messages":
            return httpx.Response(
                200,
                json={
                    "chat_id": "chat_abc123",
                    "turns": [CHAT_TURN],
                    "next_cursor": None,
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await client.chat.run(
        message="Hello",
        idempotency_key="async-chat-run-content-1",
        poll_interval=0.01,
        timeout=1.0,
        include_content=True,
    )

    assert result.result is not None
    assert result.result.chat_id == "chat_abc123"
    assert result.chat_turn is not None
    assert result.chat_turn.turn_id == "turn_abc123"
    assert result.chat_turn.model_answer == "Hi"
    assert seen_paths == [
        "/v1/chat",
        "/v1/operations/op_abc123",
        "/v1/chats/chat_abc123/messages",
    ]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_evaluations_run_include_detail_attaches_evaluation() -> None:
    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/evaluate":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        if request.url.path == "/v1/operations/op_abc123":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_abc123",
                    "status": "succeeded",
                    "result": {"result_type": "evaluation", "evaluation_id": "eval_abc123"},
                },
            )
        if request.url.path == "/v1/evaluations/eval_abc123":
            return httpx.Response(200, json=EVALUATION)
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await client.evaluations.run(
        user_message="Question",
        ai_answer="Answer",
        idempotency_key="async-evaluation-detail-1",
        timeout=1.0,
        initial_interval=0.01,
        include_detail=True,
    )

    assert result.result is not None
    assert result.result.evaluation_id == "eval_abc123"
    assert result.evaluation is not None
    assert result.evaluation.evaluation_id == "eval_abc123"
    assert result.evaluation.score == 0.91
    assert seen_paths == [
        "/v1/evaluate",
        "/v1/operations/op_abc123",
        "/v1/evaluations/eval_abc123",
    ]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_input_wizard_run_returns_terminal_create_response_without_polling() -> None:
    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "succeeded",
                "result": {
                    "result_type": "input_wizard",
                    "resource_id": "wizard_abc123",
                    "input_wizard_id": "wizard_abc123",
                    "optimized_prompt": "Direct async optimized prompt",
                    "status": "completed",
                    "wizard_session_id": "session_async123",
                },
            },
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await client.input_wizard.run(
        prompt="Improve this prompt",
        idempotency_key="wizard-terminal-create-async-1",
        timeout=1.0,
        initial_interval=0.01,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.result is not None
    assert result.result.optimized_prompt == "Direct async optimized prompt"
    assert seen_paths == ["/v1/input-wizard"]

    await client.aclose()


@pytest.mark.asyncio
async def test_async_chat_run_returns_terminal_operation_error_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat":
            return httpx.Response(202, json=OPERATION_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "operation_id": "op_abc123",
                "status": "failed",
                "error": {
                    "code": "evaluation_execution_failed",
                    "message": "Evaluation execution failed.",
                },
            },
        )

    client = AsyncZentureClient(
        api_key=LIVE_CLIENT_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await client.chat.run(
        message="Hello",
        idempotency_key="async-chat-run-failed",
        poll_interval=0.01,
        timeout=1.0,
    )

    assert result.status is OperationStatus.FAILED
    assert result.error is not None
    assert result.error.code == "evaluation_execution_failed"
    assert result.error.message == "Evaluation execution failed."

    await client.aclose()


def test_api_token_management_surface_is_not_exposed() -> None:
    client = ZentureClient(api_key=LIVE_CLIENT_KEY)

    assert not hasattr(client, "api_tokens")
    assert not hasattr(client, "tokens")

    client.close()
