"""Evaluate external answers or existing zenture chat answers.

Usage:
    python examples/evaluate.py external
    python examples/evaluate.py chat-turn
"""

from __future__ import annotations

import argparse
import sys
from typing import TypeVar

from zenture import ZentureClient
from zenture.idempotency import idempotency_key

T = TypeVar("T")


def _require(value: T | None, name: str) -> T:
    if value is None:
        raise RuntimeError(f"Expected {name} in the completed operation result.")
    return value


def _first_response_id(result: object) -> str:
    model_response_id = getattr(result, "model_response_id", None)
    if model_response_id:
        return str(model_response_id)
    model_response_ids = tuple(getattr(result, "model_response_ids", ()) or ())
    if model_response_ids:
        return str(model_response_ids[0])
    raise RuntimeError("Expected model_response_id in the completed chat operation result.")


def evaluate_external_answer(client: ZentureClient) -> None:
    """Evaluate text that was produced outside zenture chat history."""

    result = client.evaluations.run(
        user_message="What does the SDK do?",
        ai_answer="It helps server-side Python integrations call zenture.",
        external_id="example-external-answer-v1",
        metadata={"source": "example", "kind": "external_answer"},
        idempotency_key=idempotency_key("example", "evaluation-external", "v1"),
        timeout=120.0,
    )
    print("external", result.operation_id, result.status, result.result)


def evaluate_zenture_chat_turn(client: ZentureClient) -> None:
    """Create a chat, continue it, and evaluate the second AI answer."""

    first_message = "Give me a concise onboarding checklist for a new API user."
    first = client.chat.run(
        message=first_message,
        mode="single",
        idempotency_key=idempotency_key("example", "chat-eval-first-turn", "v1"),
        timeout=120.0,
    )
    first_result = _require(first.result, "first chat result")
    chat_id = _require(first_result.chat_id, "chat_id")

    follow_up_message = "Turn that checklist into three short implementation steps."
    follow_up = client.chat.run(
        message=follow_up_message,
        chat_id=chat_id,
        mode="single",
        idempotency_key=idempotency_key("example", "chat-eval-follow-up", "v1"),
        timeout=120.0,
    )
    follow_up_result = _require(follow_up.result, "follow-up chat result")
    turn_id = _require(follow_up_result.turn_id, "turn_id")
    model_response_id = _first_response_id(follow_up_result)

    messages = client.chat.messages(chat_id)
    turn = next((item for item in messages.turns if item.turn_id == turn_id), None)
    if turn is None:
        raise RuntimeError(f"Could not find completed chat turn {turn_id}.")

    evaluation = client.evaluations.run(
        user_message=turn.user_message,
        ai_answer=turn.model_answer,
        chat_id=chat_id,
        turn_id=turn_id,
        model_response_id=model_response_id,
        idempotency_key=idempotency_key("example", "evaluation-chat-turn", "v1"),
        timeout=120.0,
    )
    print("chat-turn", evaluation.operation_id, evaluation.status, evaluation.result)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a zenture evaluation example.")
    parser.add_argument("mode", choices=("external", "chat-turn"), default="external", nargs="?")
    args = parser.parse_args([] if argv is None else argv)

    with ZentureClient.from_env() as client:
        if args.mode == "external":
            evaluate_external_answer(client)
        else:
            evaluate_zenture_chat_turn(client)


if __name__ == "__main__":
    main(sys.argv[1:])
