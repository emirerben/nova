"""A paid call the ledger already settled is our bug, not an AI timeout.

Prod job cc3e380b (2026-11-20): a second text compose and a worker retry sent the same research
search under the same ledger key. The ledger answered "paid call ... already settled", which
`_classify_phone_plan_failure` filed as `plan_ai_timeout`, so chat said "the AI took too long"
and the worker retried the identical call once more.

Failure modes written down before the fix:
 1. a settled-call refusal, bare or wrapped in another error: `phone_plan_failed`, kind `bug`
    (no unchanged worker retry);
 2. a call whose outcome is genuinely unknown (provider still working, deadline hit): still
    `plan_ai_timeout`, so a real timeout keeps its retry and its copy.
"""

from __future__ import annotations

import pytest

from app.agents._runtime import (
    PaidCallAlreadySettledError,
    ProviderDeadlineExceededError,
    ProviderOutcomeUnknownError,
    TerminalSchemaError,
)
from app.tasks.generative_build import _classify_phone_plan_failure


def test_a_settled_call_refused_again_is_a_bug_not_a_timeout() -> None:
    refused = PaidCallAlreadySettledError("paid call fc5e11869890 already settled")

    assert _classify_phone_plan_failure(refused) == ("phone_plan_failed", "bug")


def test_the_refusal_is_a_bug_when_another_error_wraps_it() -> None:
    try:
        try:
            raise PaidCallAlreadySettledError("paid call fc5e11869890 already settled")
        except PaidCallAlreadySettledError as refused:
            raise TerminalSchemaError("nova.research.public_facts: schema") from refused
    except TerminalSchemaError as wrapped:
        assert _classify_phone_plan_failure(wrapped) == ("phone_plan_failed", "bug")


@pytest.mark.parametrize(
    "unknown",
    [
        ProviderOutcomeUnknownError("paid call abc already provider_started"),
        ProviderDeadlineExceededError("deadline"),
    ],
)
def test_an_unknown_provider_outcome_is_still_a_timeout(unknown) -> None:
    assert _classify_phone_plan_failure(unknown) == ("plan_ai_timeout", "ai_timeout")
