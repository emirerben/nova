"""KRI-132: phone-render format coverage matrix.

Drives the REAL `_dispatch_item_render` (the same helper KRI-114's
`test_phone_gate_*` tests already use) across every `EditFormat` x scenario
combination and asserts the outcome against the source-of-truth table in
`_phone_format_expectations.py`. Also pins two worker-level guards that
`_dispatch_item_render` alone cannot exercise (see
`test_worker_rejects_*` below), and that a chat-driven planner choosing a
format `/capabilities` never offers a phone account still fails CLOSED with a
typed, user-visible reason rather than a silent cloud render (see
`test_planner_chosen_*` below).
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import patch

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import (
    MixedMediaTimingUnavailableError,
    PhoneFormatUnavailableError,
)
from app.agents._schemas.edit_format import (
    GUIDED_EDIT_FORMATS,
    PHONE_RENDER_SUPPORTED_FORMATS,
    EditFormat,
)
from app.services import creator_capabilities as capabilities
from app.services.creator_errors import CreatorCapabilityError
from app.tasks import generative_build as gb
from tests.services.test_creator_capabilities import _enable_guided
from tests.tasks._phone_format_expectations import (
    EXPECTATIONS,
    PHONE_WORKING_FORMATS,
    SCENARIOS,
)
from tests.tasks.test_content_plan_build import _run_phone_dispatch
from tests.tasks.test_phone_montage_dispatch import setup as _phone_montage_setup

ALL_FORMATS: tuple[str, ...] = get_args(EditFormat)


# --- table-driven dispatch coverage ----------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("edit_format", ALL_FORMATS)
def test_dispatch_outcome_matches_table(
    monkeypatch: pytest.MonkeyPatch, edit_format: str, scenario: str
) -> None:
    """For every (edit_format, scenario), the REAL dispatch gate must match
    `_phone_format_expectations.EXPECTATIONS` exactly -- outcome, and (when
    rejected) the logged `phone_gate` reason."""

    expectation = EXPECTATIONS[(edit_format, scenario)]
    approved = scenario == "guided_approved"
    voiceover = scenario in {"voiceover", "voiceover_enabled"}
    enabled = scenario == "voiceover_enabled"
    with patch("app.tasks.content_plan_build.log") as mock_log:
        result, _job, mock_build, bind_mock = _run_phone_dispatch(
            monkeypatch,
            edit_format=edit_format,
            approved=approved,
            voiceover=voiceover,
            phone_narration_rendering_enabled=enabled,
            phone_render_verified_features=["narrationAudio"] if enabled else None,
        )

    assert result.outcome == expectation.outcome
    if expectation.phone_gate is None:
        bind_mock.assert_called_once()
        mock_build.assert_called_once()
    else:
        bind_mock.assert_not_called()
        mock_build.assert_not_called()
        warning_call = mock_log.warning.call_args
        assert warning_call is not None
        assert warning_call.kwargs.get("phone_gate") == expectation.phone_gate


def test_table_covers_every_edit_format_and_scenario() -> None:
    """A new EditFormat value forces a conscious update to the expectations
    table instead of silently being untested."""

    table_formats = {fmt for fmt, _scenario in EXPECTATIONS}
    assert table_formats == set(ALL_FORMATS)
    table_scenarios = {scenario for _fmt, scenario in EXPECTATIONS}
    assert table_scenarios == set(SCENARIOS)


def test_phone_render_supported_formats_matches_table() -> None:
    """`PHONE_RENDER_SUPPORTED_FORMATS` (the allowlist the dispatch gate and
    the worker both consult) must equal exactly the formats the table says
    genuinely work end to end today -- not merely dispatch successfully. See
    the module docstring on `PhoneFormatExpectation.works_on_phone` for why
    "dispatches" and "works" are different questions (the voiceover row)."""

    assert PHONE_RENDER_SUPPORTED_FORMATS == PHONE_WORKING_FORMATS


# --- worker-level guards `_dispatch_item_render` alone cannot reach --------


def test_worker_rejects_voiceover_while_narration_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the narration rollout flag off, `_run_phone_montage_job` still
    refuses a voiceover on its own -- defense in depth behind the dispatch
    gate's `voiceover_unavailable` refusal."""

    job, snapshot, _session, _bindings, _cloud = _phone_montage_setup(monkeypatch)
    candidates = {**job.all_candidates, "voiceover_gcs_path": "users/u/voice.m4a"}
    with pytest.raises(ValueError, match="voiceover"):
        gb._run_phone_montage_job(str(job.id), snapshot, candidates, ownership_epoch=3)


def test_worker_rejects_non_guided_format_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_phone_montage_job`'s docstring says only montage/day_vlog/
    single_hero without a voiceover ever reach it -- `content_plan_build`'s
    dispatch gate is what's supposed to guarantee that. This pins the
    worker's OWN defense-in-depth check
    (`edit_format not in GUIDED_EDIT_FORMATS`,
    app/tasks/generative_build.py:3908-3910) directly, the same way
    `test_ownership_epoch_mismatch_bails_before_publishing` in
    test_phone_montage_dispatch.py already calls `_run_phone_montage_job`
    directly for a different fence. No existing test covered this branch --
    in normal operation the dispatch gate and the worker's own
    `coerce_edit_format(...) in PHONE_RENDER_SUPPORTED_FORMATS` fork
    (app/tasks/generative_build.py:2174-2182) both filter this out before
    `_run_phone_montage_job` is even called, so this check is presently
    unreachable except by calling the function directly as this test does.
    """

    job, snapshot, _session, _bindings, _cloud = _phone_montage_setup(monkeypatch)
    candidates = {**job.all_candidates, "edit_format": "subtitled"}
    with pytest.raises(ValueError, match="No phone renderer is registered"):
        gb._run_phone_montage_job(str(job.id), snapshot, candidates, ownership_epoch=3)


# --- planner (chat) choosing an unsupported format on a phone account ------

# `GET /capabilities` only trims the NATIVE PICKER's offered format list
# (app/routes/creation_threads.py:2658-2668, `if phone_enabled and
# native_client: formats = {... in PHONE_RENDER_SUPPORTED_FORMATS}`) -- it
# does not constrain what the chat-driven Main Creator agent can propose.
# Nothing stops a creator from typing "make this a talking-head video" on a
# phone account. This section pins that such a strategy still fails CLOSED
# with a typed, handled exception when compiled -- never a silent cloud
# render, never an unhandled crash.
#
# Two different typed exceptions cover the six non-guided formats, both
# caught and turned into a `session.status = "failed"` + user-facing chat
# message by app/routes/creator_agent.py (~2480, ~2554):
#   - talking_head/subtitled/narrated/narrated_planned/narrated_ready: the
#     format IS a real (non-phone) renderer, so `effective_render_program`
#     reaches its phone-specific guided-format check and raises
#     `PhoneFormatUnavailableError("phone sources require a guided edit
#     format")` (app/agents/_schemas/creator_policy.py:113-114), mapped to
#     chat error code "phone_format_unavailable" in
#     `_record_media_unavailable` (app/routes/creator_agent.py:1517-1519).
#   - slides: `_format_availability` (app/services/creator_capabilities.py:
#     101-148) has NO branch for "slides" at all -- it is unavailable to the
#     Main Creator Agent's chat strategy compiler on EVERY account, phone or
#     not (slide posts are built through a separate drafting flow, not this
#     chat strategy). `effective_render_program` raises a plain `ValueError`
#     for the missing format capability
#     (app/agents/_schemas/creator_policy.py:85-88), which
#     `compile_strategy_to_plan` reclassifies into `CreatorCapabilityError`
#     (code="edit_format_unavailable", app/services/creator_capabilities.py:
#     457-463), handled at app/routes/creator_agent.py:2480-2496.

_PHONE_SPECIFIC_REJECTION_FORMATS = tuple(
    sorted(fmt for fmt in ALL_FORMATS if fmt not in GUIDED_EDIT_FORMATS and fmt != "slides")
)


@pytest.mark.parametrize("edit_format", _PHONE_SPECIFIC_REJECTION_FORMATS)
def test_planner_chosen_unsupported_format_fails_closed_on_phone(
    monkeypatch: pytest.MonkeyPatch, edit_format: str
) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "edit_format_talking_head_enabled", True)
    monkeypatch.setattr(capabilities.settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(capabilities.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(capabilities.settings, "narrated_self_narration_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )

    with pytest.raises(PhoneFormatUnavailableError) as exc:
        capabilities.compile_strategy_to_plan(manifest, CreativeStrategy(edit_format=edit_format))

    # Not the voiceover subtype: this is a format rejection, not an audio one
    # -- confirms creator_agent.py maps it to "phone_format_unavailable"
    # (not "phone_voiceover_unavailable").
    assert exc.value.voiceover is False
    assert isinstance(exc.value, MixedMediaTimingUnavailableError)


def test_planner_chosen_slides_fails_closed_but_not_phone_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slides has no branch in `_format_availability` at all (it is not a
    Main Creator Agent chat format on ANY account, not just phone) -- this
    still fails closed, just via the generic capability gate rather than the
    phone-specific one. Recorded here so the matrix's "planner on phone"
    story is honest about the one format that takes a different path."""

    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format="montage",
        media=[{"media_id": "phone-a", "kind": "video"}],
        phone_source_media_ids=["phone-a"],
        phone_rendering_allowed=True,
    )

    with pytest.raises(CreatorCapabilityError) as exc:
        capabilities.compile_strategy_to_plan(manifest, CreativeStrategy(edit_format="slides"))

    assert exc.value.code == "edit_format_unavailable"
