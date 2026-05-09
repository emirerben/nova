"""Forces removal of the subject → inputs.location read-side fallback.

The test asserts that the fallback removal deadline has passed. Today
that assertion fails — which, under @pytest.mark.xfail(strict=True), is
the expected outcome and CI passes. After 2026-05-16 the assertion
passes, the xfail flips to XPASS, and CI breaks until both the inline
fallback in `template_orchestrate._resolve_user_subject` and this test
are deleted.
"""

import datetime as dt

import pytest


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Fallback removal: when this xfail flips, delete _resolve_user_subject "
        "in template_orchestrate.py (and its REMOVE AFTER call sites), "
        "delete the legacy-subject branch in routes/template_jobs.py reroll, "
        "and delete this test."
    ),
)
def test_fallback_removal_date_reached():
    deadline = dt.datetime(2026, 5, 16, tzinfo=dt.UTC)
    assert dt.datetime.now(dt.UTC) >= deadline, (
        "Removal date not yet reached — keep the fallback for now."
    )
