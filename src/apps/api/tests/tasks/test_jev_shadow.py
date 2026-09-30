from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.services.jev_client import JEV_MODEL, JevError, JevEvaluation
from app.tasks import jev_shadow, kria_runtime


def _payload() -> dict[str, Any]:
    return {
        "brief": {"requirements": [{"id": "r1", "text": "Show the market visit", "kind": "text"}]},
        "proposed_script": {"opening_title": "Market morning"},
        "plan": {"direction": "guided_story", "edit_format": "montage"},
        "media": ["Market stall"],
        "candidate_claims": [{"id": "claim_0", "text": "Market morning"}],
    }


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(jev_shadow.settings, "jev_brief_shadow_enabled", True)
    monkeypatch.setattr(jev_shadow.settings, "typesafe_api_key", "typesafe-secret")
    monkeypatch.setattr(jev_shadow.settings, "jev_model", JEV_MODEL)
    monkeypatch.setattr(jev_shadow.settings, "jev_api_url", "https://api.typesafe.ai/v1/systemone")
    monkeypatch.setattr(jev_shadow.settings, "jev_timeout_seconds", 3.0)
    monkeypatch.setattr(jev_shadow.settings, "jev_max_attempts", 2)


def test_shadow_defaults_off_and_skips_without_key(monkeypatch) -> None:
    assert Settings.model_fields["jev_brief_shadow_enabled"].default is False
    calls: list[object] = []

    monkeypatch.setattr(jev_shadow.settings, "jev_brief_shadow_enabled", False)
    assert not jev_shadow.run_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        client_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
        persist_run=lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(jev_shadow.settings, "jev_brief_shadow_enabled", True)
    monkeypatch.setattr(jev_shadow.settings, "typesafe_api_key", "")
    assert not jev_shadow.run_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        client_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
        persist_run=lambda **kwargs: calls.append(kwargs),
    )
    assert calls == []


def test_success_persists_only_bounded_metadata(monkeypatch) -> None:
    _enable(monkeypatch)
    persisted: list[dict[str, Any]] = []
    seen: dict[str, Any] = {}

    class FakeClient:
        def evaluate(self, state, questions):
            seen["state"] = state
            seen["questions"] = questions
            return JevEvaluation(
                model=JEV_MODEL,
                answers={"required_0": 0.9, "claim_0": 0.1},
                input_tokens=120,
                output_tokens=4,
                attempts=1,
                latency_ms=18.4,
                provider_request_id="req-123",
            )

    def factory(api_key: str, **kwargs):
        assert api_key == "typesafe-secret"
        assert kwargs["model"] == JEV_MODEL
        return FakeClient()

    assert jev_shadow.run_jev_brief_shadow(
        plan_item_id="00000000-0000-0000-0000-000000000001",
        creator_agent_session_id="00000000-0000-0000-0000-000000000002",
        payload=_payload(),
        baseline_receipts=[{"requirement_id": "r1", "status": "met", "reason": "creator text"}],
        client_factory=factory,
        persist_run=lambda **kwargs: persisted.append(kwargs),
    )
    assert set(seen["questions"]) == {"required_0", "claim_0"}
    assert persisted[0]["outcome"] == "ok"
    assert persisted[0]["tokens_in"] == 120
    assert persisted[0]["provider_request_id"] == "req-123"
    assert persisted[0]["raw_text"] is None
    assert persisted[0]["input_dict"]["baseline_receipts"] == [
        {"requirement_id": "r1", "status": "met"}
    ]
    persisted_json = json.dumps(persisted[0], default=str)
    assert "Show the market visit" not in persisted_json
    assert "Market morning" not in persisted_json
    assert "typesafe-secret" not in persisted_json


def test_provider_failure_is_recorded_and_swallowed(monkeypatch) -> None:
    _enable(monkeypatch)
    persisted: list[dict[str, Any]] = []

    class FailedClient:
        def evaluate(self, state, questions):
            raise JevError("rate_limited", attempts=2, status_code=529, retryable=True)

    attempted = jev_shadow.run_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        client_factory=lambda *args, **kwargs: FailedClient(),
        persist_run=lambda **kwargs: persisted.append(kwargs),
    )
    assert attempted is True
    assert persisted[0]["outcome"] == "failed"
    assert persisted[0]["attempts"] == 2
    assert persisted[0]["error"] == "rate_limited"
    assert persisted[0]["raw_text"] is None


def test_unexpected_provider_failure_is_recorded_and_swallowed(monkeypatch) -> None:
    _enable(monkeypatch)
    persisted: list[dict[str, Any]] = []

    class FailedClient:
        def evaluate(self, state, questions):
            raise RuntimeError("provider body must not escape")

    attempted = jev_shadow.run_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        client_factory=lambda *args, **kwargs: FailedClient(),
        persist_run=lambda **kwargs: persisted.append(kwargs),
    )
    assert attempted is True
    assert persisted[0]["outcome"] == "failed"
    assert persisted[0]["error"] == "RuntimeError"
    assert "provider body must not escape" not in json.dumps(persisted[0])


def test_invalid_payload_skips_without_provider_or_persistence(monkeypatch) -> None:
    _enable(monkeypatch)
    calls: list[object] = []
    assert not jev_shadow.run_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload={"brief": {"requirements": "not-a-list"}},
        client_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
        persist_run=lambda **kwargs: calls.append(kwargs),
    )
    assert calls == []


def test_kria_dispatch_is_gated_and_uses_background_queue(monkeypatch) -> None:
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(kria_runtime.settings, "jev_brief_shadow_enabled", False)
    monkeypatch.setattr(kria_runtime.settings, "typesafe_api_key", "key")
    monkeypatch.setattr(
        jev_shadow.evaluate_jev_brief_shadow,
        "apply_async",
        lambda **kwargs: dispatched.append(kwargs),
    )
    kria_runtime._dispatch_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        baseline_receipts=[],
    )
    assert dispatched == []

    monkeypatch.setattr(kria_runtime.settings, "jev_brief_shadow_enabled", True)
    kria_runtime._dispatch_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        baseline_receipts=[],
    )
    assert dispatched[0]["queue"] == "agent-control"
    assert dispatched[0]["kwargs"]["payload"] == _payload()


def test_kria_dispatch_swallows_queue_failure(monkeypatch) -> None:
    monkeypatch.setattr(kria_runtime.settings, "jev_brief_shadow_enabled", True)
    monkeypatch.setattr(kria_runtime.settings, "typesafe_api_key", "key")

    def fail(**_kwargs: Any) -> None:
        raise RuntimeError("queue down")

    monkeypatch.setattr(
        jev_shadow.evaluate_jev_brief_shadow,
        "apply_async",
        fail,
    )
    kria_runtime._dispatch_jev_brief_shadow(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        baseline_receipts=[],
    )


def test_celery_wrapper_delegates_to_shadow_runner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        jev_shadow,
        "run_jev_brief_shadow",
        lambda **kwargs: calls.append(kwargs),
    )
    jev_shadow.evaluate_jev_brief_shadow.run(
        plan_item_id="item",
        creator_agent_session_id="session",
        payload=_payload(),
        baseline_receipts=[],
    )
    assert calls == [
        {
            "plan_item_id": "item",
            "creator_agent_session_id": "session",
            "payload": _payload(),
            "baseline_receipts": [],
        }
    ]
