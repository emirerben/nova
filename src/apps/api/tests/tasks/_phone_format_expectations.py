"""Source-of-truth table: for every `EditFormat` x scenario, what does the
REAL dispatch/worker path actually do today?

Every value below was produced by driving the real `_dispatch_item_render`
(via `tests.tasks.test_content_plan_build._run_phone_dispatch`) and reading
the worker (`app/tasks/generative_build.py`) it hands off to -- not assumed
from reading the gate code alone. See `test_phone_format_matrix.py`, which
asserts every row against the live function, and
`docs/reviews/kri-132/phone-format-matrix.md` for the narrative writeup.
KRI-132.

Scenarios (mirrors `_run_phone_dispatch`'s kwargs):
  "plain"           - no approved guided proposal, no voiceover.
  "guided_approved" - `validate_approved_proposal_media_sync` returns an
                       approved proposal. Only changes behavior for
                       GUIDED_EDIT_FORMATS (montage/day_vlog/single_hero) --
                       see the comment on _UNSUPPORTED_* below for why.
  "voiceover"       - item has audio_mode="voiceover" + voiceover_gcs_path,
                       rollout flag off (the default).
  "voiceover_enabled" - same item with PHONE_NARRATION_RENDERING_ENABLED on
                       and narrationAudio in PHONE_RENDER_VERIFIED_FEATURES.

`works_on_phone` is the important field: it answers "does this actually
render end to end on the device today", which is NOT the same question as
"does `_dispatch_item_render` return outcome == 'dispatched'". A row is only
`works_on_phone=True` when nothing downstream of dispatch is known to reject it.
"""

from __future__ import annotations

from typing import NamedTuple


class PhoneFormatExpectation(NamedTuple):
    # `DispatchResult.outcome` from the real `_dispatch_item_render` call.
    outcome: str
    # The `phone_gate` kwarg on the rejection `log.warning(...)` call, or
    # None when the item was not rejected at the dispatch gate.
    phone_gate: str | None
    # Whether the edit renders end to end on the device today (dispatch
    # succeeding is necessary but not sufficient -- see module docstring).
    works_on_phone: bool
    # Why, with a path:line citation.
    worker_note: str


# --- montage / day_vlog / single_hero (GUIDED_EDIT_FORMATS) ---------------

_GUIDED_PLAIN = PhoneFormatExpectation(
    outcome="invalid_clips",
    phone_gate="unapproved_guided",
    works_on_phone=False,
    worker_note=(
        "guided_edit_applicable() is True for these three formats without a "
        "voiceover (app/agents/_schemas/edit_format.py GUIDED_EDIT_FORMATS), so "
        "the phone gate requires an approved guided-story proposal. Without one "
        "it rejects before build_generative_job is ever called. "
        "app/tasks/content_plan_build.py:1467-1471"
    ),
)

_GUIDED_APPROVED = PhoneFormatExpectation(
    outcome="dispatched",
    phone_gate=None,
    works_on_phone=True,
    worker_note=(
        "Approved guided proposal -> approved_proposal is not None -> "
        "bind_phone_sources() runs and the Job dispatches. The worker fork "
        "(app/tasks/generative_build.py:2172-2173) routes it to "
        "_run_phone_guided_job because the snapshot carries a `guided_edit` dict."
    ),
)

_GUIDED_VOICEOVER = PhoneFormatExpectation(
    outcome="invalid_clips",
    phone_gate="voiceover_unavailable",
    works_on_phone=False,
    worker_note=(
        "Rollout flag off (the default). has_voiceover=True makes "
        "guided_edit_applicable() False, so the gate takes its non-guided "
        "branch; montage/day_vlog/single_hero are in "
        "PHONE_RENDER_SUPPORTED_FORMATS, but without "
        "PHONE_NARRATION_RENDERING_ENABLED + a verified narrationAudio the gate "
        "now refuses here with a typed reason instead of minting a Job that "
        "_run_phone_montage_job would reject on pickup."
    ),
)

_GUIDED_VOICEOVER_ENABLED = PhoneFormatExpectation(
    outcome="dispatched",
    phone_gate=None,
    works_on_phone=True,
    worker_note=(
        "Flag on + narrationAudio verified: binds phone sources and dispatches "
        "with no guided proposal (voiceover is never guided-applicable). The "
        "worker routes to _run_phone_montage_job, which compiles the voiceover "
        "as a narration audio track (compile_phone_montage_plan). Rendered on "
        "the simulator by DeviceMontageRenderE2ETests."
    ),
)


_UNSUPPORTED = PhoneFormatExpectation(
    outcome="invalid_clips",
    phone_gate="unsupported_format",
    works_on_phone=False,
    worker_note=(
        "Not in GUIDED_EDIT_FORMATS -> guided_edit_applicable() is False -> the "
        "dispatch gate's non-guided branch checks "
        "`fmt not in PHONE_RENDER_SUPPORTED_FORMATS` "
        "(app/tasks/content_plan_build.py:1473-1476), which is "
        "{montage, day_vlog, single_hero} -- this format is never a member, so "
        "every scenario rejects identically regardless of approval or voiceover."
    ),
)

SCENARIOS: tuple[str, ...] = ("plain", "guided_approved", "voiceover", "voiceover_enabled")

_ALL_FORMATS: tuple[str, ...] = (
    "montage",
    "talking_head",
    "day_vlog",
    "single_hero",
    "subtitled",
    "narrated",
    "narrated_planned",
    "narrated_ready",
    "slides",
)

_GUIDED_FORMATS = frozenset({"montage", "day_vlog", "single_hero"})

_BY_SCENARIO_FOR_GUIDED = {
    "plain": _GUIDED_PLAIN,
    "guided_approved": _GUIDED_APPROVED,
    "voiceover": _GUIDED_VOICEOVER,
    "voiceover_enabled": _GUIDED_VOICEOVER_ENABLED,
}

EXPECTATIONS: dict[tuple[str, str], PhoneFormatExpectation] = {
    (fmt, scenario): (_BY_SCENARIO_FOR_GUIDED[scenario] if fmt in _GUIDED_FORMATS else _UNSUPPORTED)
    for fmt in _ALL_FORMATS
    for scenario in SCENARIOS
}

# Derived, not hand-maintained: the formats the table says genuinely render
# end to end on the device today. This is compared against the real
# `PHONE_RENDER_SUPPORTED_FORMATS` constant in test_phone_format_matrix.py --
# if a future format is added to the allowlist without this table being
# updated to say it actually works, that test fails.
PHONE_WORKING_FORMATS: frozenset[str] = frozenset(
    fmt
    for (fmt, scenario), expectation in EXPECTATIONS.items()
    if scenario == "guided_approved" and expectation.works_on_phone
)
