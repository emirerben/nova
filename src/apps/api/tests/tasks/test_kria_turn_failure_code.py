"""Turn failure code and copy for provider deadline / truncation (KRI-1037).

Ways this fails: a wrapped provider error falls back to the generic `runtime_turn_failed`; a
spend cap hidden under a deadline loses to it; a transport outcome-unknown is marked as a
deadline (and so degrades the creator's next turn); the new codes have no copy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.agents._runtime import (
    ProviderDeadlineExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    TerminalError,
    TerminalOutputTruncatedError,
)
from app.tasks.kria_runtime import (
    _TURN_FAILURE_COPY,
    _hit_provider_deadline,
    _previous_turn_hit_provider_deadline,
    _turn_failure_code,
)


def _wrapped(inner: BaseException) -> RuntimeError:
    outer = RuntimeError("main creator failed")
    outer.__cause__ = inner
    return outer


def test_a_wrapped_provider_deadline_maps_to_the_provider_code() -> None:
    exc = _wrapped(ProviderDeadlineExceededError("late"))
    assert _turn_failure_code(exc) == "runtime_provider_outcome_unknown"
    assert _hit_provider_deadline(exc)


def test_a_transport_outcome_unknown_is_not_marked_as_a_deadline() -> None:
    exc = _wrapped(ProviderOutcomeUnknownError("socket closed"))
    assert _turn_failure_code(exc) == "runtime_provider_outcome_unknown"
    assert not _hit_provider_deadline(exc)


def test_a_wrapped_truncation_has_its_own_code() -> None:
    assert (
        _turn_failure_code(_wrapped(TerminalOutputTruncatedError("output truncated")))
        == "runtime_output_truncated"
    )


def test_a_spend_cap_under_a_deadline_still_wins() -> None:
    quota = ProviderQuotaExceededError()
    quota.__cause__ = ProviderDeadlineExceededError("late")
    assert _turn_failure_code(_wrapped(quota)) == "ai_quota_exhausted"
    deadline = ProviderDeadlineExceededError("late")
    deadline.__cause__ = ProviderQuotaExceededError()
    assert _turn_failure_code(_wrapped(deadline)) == "ai_quota_exhausted"


def test_an_unrelated_terminal_error_stays_generic() -> None:
    assert _turn_failure_code(_wrapped(TerminalError("boom"))) == "runtime_turn_failed"


def test_both_new_codes_have_english_and_turkish_copy() -> None:
    for code in ("runtime_provider_outcome_unknown", "runtime_output_truncated"):
        en, tr = _TURN_FAILURE_COPY[code]
        assert en and tr


# --- a deadline degrades only a retry of the same request ---------------------------------


class _FakeDb:
    """Serves the newest finished turn and its source event, like the two lookups do."""

    def __init__(self, previous: SimpleNamespace | None, source_text: str | None) -> None:
        self._previous = previous
        self._source = SimpleNamespace(payload=None, content=source_text)

    def execute(self, *_a, **_k):
        return SimpleNamespace(scalar_one_or_none=lambda: self._previous)

    def get(self, *_a, **_k):
        return self._source


def _deadline_turn(*, age: timedelta = timedelta(minutes=1), deadline: bool = True):
    return SimpleNamespace(
        status="failed",
        source_event_id=uuid.uuid4(),
        completed_at=datetime.now(UTC) - age,
        error={
            "code": "runtime_provider_outcome_unknown",
            **({"provider_deadline": True} if deadline else {}),
        },
    )


def _retry_degrades(failed_text: str, new_text: str, **turn_kwargs) -> bool:
    db = _FakeDb(_deadline_turn(**turn_kwargs), failed_text)
    return _previous_turn_hit_provider_deadline(
        db, SimpleNamespace(id=uuid.uuid4(), thread_id=uuid.uuid4()), new_text
    )


def test_a_retry_of_the_same_request_starts_degraded() -> None:
    request = "Split it into pieces, word by word"
    assert _retry_degrades(request, request)
    assert _retry_degrades(request, "split it into pieces,  word by word!")
    for bare in ("Try again", "try again please", "retry", "One more time", "Tekrar dene"):
        assert _retry_degrades(request, bare), bare


def test_an_unrelated_new_request_after_a_deadline_runs_at_full_strength() -> None:
    request = "Split it into pieces, word by word"
    for unrelated in (
        "Make the opening title bigger",
        "Use a calmer song",
        "Show all content in random order",
    ):
        assert not _retry_degrades(request, unrelated), unrelated


def test_the_window_and_the_deadline_marker_still_gate_a_retry() -> None:
    request = "Try again"
    assert not _retry_degrades(request, request, age=timedelta(minutes=30))
    assert not _retry_degrades(request, request, deadline=False)
