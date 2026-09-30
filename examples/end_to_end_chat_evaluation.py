"""Run a local end-to-end Public API flow with wallet, chat, and evaluation."""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any
from uuid import uuid4

from zenture import ZentureClient
from zenture.idempotency import idempotency_key

DEFAULT_PROMPT = "Explain why deterministic tests matter for an SDK."


def main() -> None:
    prompt = os.environ.get("ZENTURE_EXAMPLE_PROMPT", DEFAULT_PROMPT)
    run_id = os.environ.get("ZENTURE_EXAMPLE_RUN_ID") or f"local-e2e-{uuid4().hex}"

    with ZentureClient.from_env() as client:
        wallet = client.wallet.get()
        single_models = client.models.list(mode="single").models
        selected_model = _select_single_model(single_models)

        wizard = client.input_wizard.run(
            prompt=prompt,
            idempotency_key=idempotency_key(run_id, "input-wizard", "v1"),
            timeout=120.0,
        )
        optimized_prompt = (
            wizard.result.optimized_prompt
            if wizard.result and wizard.result.optimized_prompt
            else prompt
        )

        chat = client.chat.run(
            message=optimized_prompt,
            mode="single",
            model=selected_model["id"],
            idempotency_key=idempotency_key(run_id, "chat", selected_model["id"], "v1"),
            timeout=120.0,
        )
        if chat.result is None or chat.result.chat_id is None or chat.result.turn_id is None:
            raise RuntimeError("Chat operation did not return required safe ids.")

        turn = _find_turn_by_id(
            client.chat.messages(chat.result.chat_id).turns,
            chat.result.turn_id,
        )
        model_response_id = chat.result.model_response_id or turn.get("model_response_id")
        if not model_response_id:
            raise RuntimeError("Chat turn did not expose a model_response_id for evaluation.")

        evaluation = client.evaluations.run(
            user_message=str(turn["user_message"]),
            ai_answer=str(turn["model_answer"]),
            chat_id=chat.result.chat_id,
            turn_id=chat.result.turn_id,
            model_response_id=str(model_response_id),
            idempotency_key=idempotency_key(run_id, "evaluation", str(model_response_id), "v1"),
            timeout=180.0,
        )
        if evaluation.result is None or evaluation.result.evaluation_id is None:
            raise RuntimeError("Evaluation operation did not return an evaluation_id.")
        evaluation_detail = client.evaluations.get(evaluation.result.evaluation_id)

    chat_billed = _amount(chat.result.amount_billed if chat.result else None)
    evaluation_billed = _amount(evaluation_detail.amount_billed)
    summary = {
        "wallet": {
            "plan": wallet.plan,
            "status": wallet.status,
            "credits_available": wallet.credits_available.model_dump(),
        },
        "model": selected_model,
        "chat": {
            "operation_id": chat.operation_id,
            "status": chat.status.value,
            "chat_id": chat.result.chat_id if chat.result else None,
            "turn_id": chat.result.turn_id if chat.result else None,
            "amount_billed": _dump_amount(chat.result.amount_billed if chat.result else None),
        },
        "evaluation": {
            "operation_id": evaluation.operation_id,
            "status": evaluation.status.value,
            "evaluation_id": evaluation_detail.evaluation_id,
            "score": evaluation_detail.score,
            "amount_billed": _dump_amount(evaluation_detail.amount_billed),
        },
        "total_billed": {"amount": f"{chat_billed + evaluation_billed:.2f}", "unit": "credits"},
    }
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _select_single_model(models: tuple[Any, ...]) -> dict[str, str]:
    if not models:
        raise RuntimeError("No single-mode models are available.")
    available = [model for model in models if model.is_available]
    candidates = available or list(models)
    selected = next(
        (model for model in candidates if "haiku" in model.id.lower()),
        candidates[0],
    )
    return {"id": selected.id, "display_name": selected.display_name}


def _find_turn_by_id(turns: tuple[Any, ...], turn_id: str) -> dict[str, Any]:
    for turn in turns:
        if turn.turn_id == turn_id:
            return turn.model_dump()
    raise RuntimeError(f"Could not find chat turn {turn_id}.")


def _amount(value: Any | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value.amount))


def _dump_amount(value: Any | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"amount": value.amount, "unit": value.unit}


if __name__ == "__main__":
    main()
