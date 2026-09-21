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
        "Not in GUIDED_EDIT_FORMATS, not `subtitled`, not a NARRATED_EDIT_FORMATS "
        "member -> the dispatch gate's non-guided branch falls to its final "
        "`fmt not in phone_render_supported_formats()` check "
        "(app/tasks/content_plan_build.py) and rejects unconditionally -- "
        "talking_head and slides have no phone compiler at all, so every "
        "scenario rejects identically regardless of approval or voiceover."
    ),
)

# --- subtitled ("Talking to camera") ---------------------------------------
#
# subtitled is never guided-applicable (audio-led), so `approved` never
# changes its outcome -- "plain" and "guided_approved" behave identically.
# `_run_phone_dispatch`'s test item always carries exactly ONE clip
# (`_phone_dispatch_item`), which is the only shape subtitled ever accepts.

_SUBTITLED_NO_VOICEOVER = PhoneFormatExpectation(
    outcome="dispatched",
    phone_gate=None,
    works_on_phone=True,
    worker_note=(
        "subtitled is audio-led (guided_edit_applicable() is always False), so "
        "the dispatch gate's non-guided branch checks "
        "`phone_render_supported_formats()` (phone_subtitled_rendering_enabled + "
        "subtitled_archetype_enabled) and the one-clip shape -- both hold here, "
        "so it dispatches. The worker fork routes it to _run_phone_subtitled_job "
        "-> compile_phone_subtitled_plan."
    ),
)

_SUBTITLED_VOICEOVER = PhoneFormatExpectation(
    outcome="invalid_clips",
    phone_gate="unsupported_format",
    works_on_phone=False,
    worker_note=(
        "subtitled is defined as spined by the CLIP's own audio (no voiceover, "
        "by product definition) -- the dispatch gate rejects a subtitled item "
        "that also carries a recorded voiceover unconditionally, independent of "
        "any rollout flag, since no phone compiler accepts that combination "
        "(compile_phone_subtitled_plan has no voiceover input at all)."
    ),
)

_BY_SCENARIO_FOR_SUBTITLED = {
    "plain": _SUBTITLED_NO_VOICEOVER,
    "guided_approved": _SUBTITLED_NO_VOICEOVER,
    "voiceover": _SUBTITLED_VOICEOVER,
    "voiceover_enabled": _SUBTITLED_VOICEOVER,
}

# --- narrated / narrated_planned / narrated_ready ---------------------------
#
# Also always audio-led (never guided-applicable). Two independent phone
# lanes reach these formats: a recorded voiceover (mirrors the montage-family
# voiceover rollout exactly, gated by phone_narrated_rendering_enabled +
# phone_narration_rendering_enabled + narrationAudio + narrated_archetype_enabled),
# or self-narration with NO voiceover -- `_run_phone_dispatch`'s test item is
# always exactly one clip, which is the only self-narration shape that can
# possibly resolve to the phone-supported `subtitled` archetype (2+ clips
# would need `talking_head`, which has no phone compiler and is refused at
# the dispatch gate before any Job is minted).

_NARRATED_SELF_NARRATION = PhoneFormatExpectation(
    outcome="dispatched",
    phone_gate=None,
    works_on_phone=True,
    worker_note=(
        "No recorded voiceover -> self-narration branch: "
        "narrated_self_narration_enabled + `subtitled` phone-supported + "
        "exactly one clip all hold, so the dispatch gate lets it through. The "
        "worker fork routes it to _run_phone_subtitled_job, which re-resolves "
        "the REAL archetype post-ingest (_resolve_archetype) and only proceeds "
        "when it actually lands on `subtitled` -- a no-speech clip (-> montage "
        "fallback) or non-single-clip talking_head resolution fails closed "
        "there via UnsupportedPhonePlan instead of silently rendering the "
        "wrong shape."
    ),
)

_NARRATED_VOICEOVER_DISABLED = PhoneFormatExpectation(
    outcome="invalid_clips",
    phone_gate="narrated_voiceover_unavailable",
    works_on_phone=False,
    worker_note=(
        "Recorded voiceover present, but phone_narration_rendering_enabled is "
        "off (the scenario default) -> `phone_render_supported_formats()` "
        "excludes the narrated family -> the dispatch gate refuses before a Job "
        "is minted, mirroring the montage-family voiceover_unavailable gate "
        "exactly (distinct reason key so the creator hears about their "
        "voiceover, not a generic format rejection)."
    ),
)

_NARRATED_VOICEOVER_ENABLED = PhoneFormatExpectation(
    outcome="dispatched",
    phone_gate=None,
    works_on_phone=True,
    worker_note=(
        "Recorded voiceover + phone_narration_rendering_enabled on + "
        "narrationAudio verified -> `phone_render_supported_formats()` includes "
        "the narrated family -> dispatches. The worker fork routes it to "
        "_run_phone_narrated_job -> compile_phone_narrated_plan, mirroring "
        "_render_narrated_variant's script/auto-segment step timing."
    ),
)

_BY_SCENARIO_FOR_NARRATED = {
    "plain": _NARRATED_SELF_NARRATION,
    "guided_approved": _NARRATED_SELF_NARRATION,
    "voiceover": _NARRATED_VOICEOVER_DISABLED,
    "voiceover_enabled": _NARRATED_VOICEOVER_ENABLED,
}

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
_NARRATED_FORMATS = frozenset({"narrated", "narrated_planned", "narrated_ready"})


def _expectation_for(fmt: str, scenario: str) -> PhoneFormatExpectation:
    if fmt in _GUIDED_FORMATS:
        return {
            "plain": _GUIDED_PLAIN,
            "guided_approved": _GUIDED_APPROVED,
            "voiceover": _GUIDED_VOICEOVER,
            "voiceover_enabled": _GUIDED_VOICEOVER_ENABLED,
        }[scenario]
    if fmt == "subtitled":
        return _BY_SCENARIO_FOR_SUBTITLED[scenario]
    if fmt in _NARRATED_FORMATS:
        return _BY_SCENARIO_FOR_NARRATED[scenario]
    return _UNSUPPORTED


EXPECTATIONS: dict[tuple[str, str], PhoneFormatExpectation] = {
    (fmt, scenario): _expectation_for(fmt, scenario)
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
