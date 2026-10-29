"""Turn failure code and copy for provider deadline / truncation (KRI-1037).

Ways this fails: a wrapped provider error falls back to the generic `runtime_turn_failed`; a
spend cap hidden under a deadline loses to it; a transport outcome-unknown is marked as a
deadline (and so degrades the creator's next turn); the new codes have no copy.
"""

from __future__ import annotations

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
