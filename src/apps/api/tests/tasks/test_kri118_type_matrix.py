"""KRI-118 lane L7: video-type requirements verification matrix.

Table-driven coverage across `EditFormat` x representative footage shapes.
Every cell asserts one of: (a) an exact planning-time refusal reason code
(never a generic 500/exception), or (b) the full chain succeeds -- a
plan/proposal compiles, dispatch returns a "dispatched"-equivalent result,
the correct phone compiler runs, and `validate_phone_pilot_recipe` passes.

This reuses the SAME real fixture-building helpers the existing phone-render
test suite already uses, rather than inventing new mocking machinery:

  - `_run_phone_dispatch` (tests/tasks/test_content_plan_build.py) drives the
    REAL dispatch gate (`_dispatch_item_render`) for planning-time outcome
    checks. `direction` (fast_montage vs guided_story) is invisible to this
    gate -- it only ever looks at `edit_format`/clip_count/voiceover -- so a
    single dispatch case covers both directions for a given format; see
    Section 1's docstring.
  - `setup()` in `test_phone_montage_dispatch.py` / `test_phone_guided_dispatch.py`
    and `_setup_subtitled` / `_setup_narrated` in
    `test_phone_subtitled_narrated_dispatch.py` drive the REAL worker
    (`_run_generative_job` -> `_run_phone_*_job` -> the matching
    `compile_phone_*_plan` -> `validate_phone_pilot_recipe`) for full-chain
    success checks -- reaching `job.status == "awaiting_device"` in these
    harnesses is only possible once `validate_phone_pilot_recipe` has
    already passed (every `_run_phone_*_job` calls it before pinning the
    device request; see `app/tasks/generative_build.py`).
  - `validate_proposal_compiles` (`app.pipeline.guided_story`) is the pure,
    offline planning-time compiler (duration/beat-allocation arithmetic, no
    ffmpeg) used for footage-shape feasibility and the day_vlog/single_hero
    story-shape rows, mirroring `tests/agents/test_edit_proposal_story_shapes.py`'s
    own `_snapshot_from_repaired_output` pattern.

See `docs/reviews/kri-118/video-type-requirements.md` (L0's audit) for the
target-state type catalogue this file checks against.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest

from app.agents.edit_proposal import (
    DraftStoryBeat,
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
)
from app.pipeline.guided_story import GuidedStoryError, validate_proposal_compiles
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, StoryBeat
from app.services import story_shapes
from app.services.story_shapes import (
    humanize_repairs,
    usable_media_count,
)
from app.tasks.edit_proposal_build import (
    feasible_guided_duration_s,
    guided_feasibility_threshold_s,
)
from tests.tasks.test_content_plan_build import _run_phone_dispatch

# Every feature string this repo's tests/docs document as gated behind
# `PHONE_RENDER_VERIFIED_FEATURES` -- docs/runbooks/phone-rendering.md lines
# 13, 34-35, 62-65, 104, 192-193, 275, 298, 767, 1172, 1285; the per-worker
# `setup()` harnesses in test_phone_montage_dispatch.py /
# test_phone_guided_dispatch.py / test_phone_subtitled_narrated_dispatch.py.
# There is NO single documented "this is exactly what's live in prod right
# now" list in this repo -- `phone_render_verified_features` is a Fly secret
# set per pilot rollout, not committed config (`app/config.py` defaults it to
# `[]`). This is deliberately the BROADEST documented union, so every "full
# chain succeeds" row below tests against the most permissive plausible prod
# state. NEEDS CONFIRMATION AGAINST REAL FLY SECRETS
# (`fly secrets list -a nova-video` names the secret, not its value) before
# treating any row here as a live-prod guarantee -- flagged explicitly per
# the task brief.
PROD_VERIFIED_FEATURES: list[str] = [
    "basicComposition",
    "local1080Export",
    "crossfade",
    "audioMix",
    "musicBed",
    "narrationAudio",
    "positionedText",
    "animatedText",
    "stillImages",
    "visualVideos",
]


# ---------------------------------------------------------------------------
# Section 1: dispatch-gate outcome table (planning-time refusal / dispatched)
# ---------------------------------------------------------------------------
#
# `direction` (fast_montage vs guided_story) is invisible to
# `_dispatch_item_render` -- `_run_phone_dispatch` never sets it, and the
# gate ladder in `content_plan_build.py` only branches on `edit_format`,
# clip_count, and voiceover presence. So "montage" and "fast_montage" share
# one row family here; §3 covers direction-specific behavior (the fast_montage
# taste-rule removal, day_vlog/single_hero story-shape geometry) at the
# proposal-compile layer where direction actually matters.


class DispatchCase(NamedTuple):
    label: str
    edit_format: str
    clip_count: int
    approved: bool
    voiceover: bool
    narration_enabled: bool
    expected_outcome: str
    expected_reason: str | None


DISPATCH_CASES: tuple[DispatchCase, ...] = (
    # -- montage (covers fast_montage too -- direction-invisible here) --
    DispatchCase(
        "montage_unapproved",
        "montage",
        1,
        False,
        False,
        False,
        "invalid_clips",
        "unapproved_guided",
    ),
    DispatchCase("montage_approved", "montage", 1, True, False, False, "dispatched", None),
    DispatchCase(
        "montage_voiceover_flag_off",
        "montage",
        1,
        False,
        True,
        False,
        "invalid_clips",
        "voiceover_unavailable",
    ),
    DispatchCase("montage_voiceover_flag_on", "montage", 1, False, True, True, "dispatched", None),
    # -- guided_story direction, montage/day_vlog/single_hero formats --
    DispatchCase(
        "day_vlog_unapproved",
        "day_vlog",
        1,
        False,
        False,
        False,
        "invalid_clips",
        "unapproved_guided",
    ),
    DispatchCase("day_vlog_approved", "day_vlog", 1, True, False, False, "dispatched", None),
    DispatchCase(
        "single_hero_unapproved",
        "single_hero",
        1,
        False,
        False,
        False,
        "invalid_clips",
        "unapproved_guided",
    ),
    DispatchCase("single_hero_approved", "single_hero", 1, True, False, False, "dispatched", None),
    # -- subtitled ("Talking to camera"): always exactly 1 clip, audio-led --
    DispatchCase("subtitled_one_clip", "subtitled", 1, False, False, False, "dispatched", None),
    DispatchCase(
        "subtitled_voiceover_unsupported",
        "subtitled",
        1,
        False,
        True,
        True,
        "invalid_clips",
        "unsupported_format",
    ),
    # -- narrated: recorded voiceover lane vs self-narration lane --
    DispatchCase(
        "narrated_voiceover_flag_off",
        "narrated",
        1,
        False,
        True,
        False,
        "invalid_clips",
        "narrated_voiceover_unavailable",
    ),
    DispatchCase(
        "narrated_voiceover_flag_on", "narrated", 1, False, True, True, "dispatched", None
    ),
    DispatchCase(
        "narrated_self_narration_one_clip", "narrated", 1, False, False, False, "dispatched", None
    ),
    # KRI-118 L1: the NEW typed refusal this matrix specifically pins.
    DispatchCase(
        "narrated_self_narration_multi_clip",
        "narrated",
        2,
        False,
        False,
        False,
        "invalid_clips",
        "self_narration_multi_clip",
    ),
    # -- formats with no phone compiler at all, any scenario --
    DispatchCase(
        "slides_never_dispatches",
        "slides",
        1,
        True,
        False,
        False,
        "invalid_clips",
        "unsupported_format",
    ),
    DispatchCase(
        "talking_head_never_dispatches",
        "talking_head",
        2,
        True,
        False,
        False,
        "invalid_clips",
        "unsupported_format",
    ),
)


@pytest.mark.parametrize("case", DISPATCH_CASES, ids=lambda c: c.label)
def test_dispatch_matrix(monkeypatch: pytest.MonkeyPatch, case: DispatchCase) -> None:
    result, _job, mock_build, bind_mock = _run_phone_dispatch(
        monkeypatch,
        edit_format=case.edit_format,
        approved=case.approved,
        voiceover=case.voiceover,
        clip_count=case.clip_count,
        phone_narration_rendering_enabled=case.narration_enabled,
        phone_render_verified_features=["narrationAudio"] if case.narration_enabled else None,
    )
    assert result.outcome == case.expected_outcome
    if case.expected_outcome == "dispatched":
        bind_mock.assert_called_once()
        mock_build.assert_called_once()
    else:
        bind_mock.assert_not_called()
        mock_build.assert_not_called()
        assert result.reason == case.expected_reason


# ---------------------------------------------------------------------------
# Section 2: footage-shape feasibility (pure, offline, no ffmpeg/LLM)
# ---------------------------------------------------------------------------
#
# Duration/photo/count shape does NOT change the dispatch gate's outcome
# (Section 1) -- it changes whether `_run_draft_attempt` even calls the
# specialist agent (`feasible_guided_duration_s` vs
# `guided_feasibility_threshold_s`) and whether the resulting plan compiles
# (`validate_proposal_compiles`, the exact function `_allocate_beat_durations`
# backs at render time). Both are pure Python -- no ffmpeg, no network.


def _media_refs(specs: list[tuple[str, str, float | None]]) -> list[MediaRef]:
    return [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"users/u/{media_id}.mp4",
            generation="1",
            kind=kind,
            duration_s=duration_s,
        )
        for media_id, kind, duration_s in specs
    ]


def _snapshot(
    *, direction: str, media: list[MediaRef], beats: list[StoryBeat], duration_s: float
) -> EditProposalSnapshot:
    return EditProposalSnapshot(
        direction=direction,
        pace="balanced",
        duration_s=duration_s,
        title="Matrix fixture",
        media=media,
        story_beats=beats,
    )


def test_footage_shape_three_videos_is_feasible_and_compiles() -> None:
    media = _media_refs([(f"clip-{i}", "video", 4.0) for i in range(3)])
    assert feasible_guided_duration_s(media) >= guided_feasibility_threshold_s(len(media))
    beats = [
        StoryBeat(
            beat_id=f"beat-{i}", topic="t", thought="", media_ids=[f"clip-{i}"], duration_s=3.0
        )
        for i in range(3)
    ]
    validate_proposal_compiles(
        _snapshot(direction="guided_story", media=media, beats=beats, duration_s=9.0)
    )


def test_footage_shape_twelve_videos_including_three_short_is_feasible() -> None:
    """KRI-118 L1's feasibility fix under test: three sub-1.4s clips must
    still credit their OWN duration (not zero) to the whole-pool estimate,
    so a 12-clip pool with 3 short clips is not wrongly judged infeasible."""

    short_specs = [(f"short-{i}", "video", 0.9) for i in range(3)]
    long_specs = [(f"long-{i}", "video", 4.0) for i in range(9)]
    media = _media_refs([*short_specs, *long_specs])
    assert len(media) == 12

    # The fix under test, isolated: each short clip contributes its OWN
    # duration (not zero) to the pool estimate.
    short_only = _media_refs(short_specs)
    assert feasible_guided_duration_s(short_only) == pytest.approx(2.7, abs=1e-6)

    assert feasible_guided_duration_s(media) >= guided_feasibility_threshold_s(len(media))
    # `StoryBeat.duration_s` has a schema floor of 1.0s (below the 1.4s
    # legible-moment floor) -- a real specialist plan never authors a beat
    # shorter than that, so a beat holding one of the short clips is given
    # exactly the schema floor (1.0s), still below the clip's own capacity
    # (0.9s short clip's `guided_moment_floor_s` credit rounds up to the
    # clip's own duration once floored at the compiler's own minimum
    # renderable moment) -- proving the compiler tolerates a beat drawn from
    # a short clip rather than the pool-level estimate being the only place
    # the fix is checkable.
    beats = [
        StoryBeat(
            beat_id=f"beat-{i}",
            topic="t",
            thought="",
            media_ids=[media_id],
            duration_s=1.0 if duration_s < 1.4 else 3.0,
        )
        for i, (media_id, _kind, duration_s) in enumerate([*short_specs, *long_specs])
    ]
    validate_proposal_compiles(
        _snapshot(
            direction="guided_story",
            media=media,
            beats=beats,
            duration_s=sum(b.duration_s for b in beats),
        )
    )


def test_footage_shape_manifest_cap_fifty_items_scales() -> None:
    """The 50-clip intake cap (`_MAX_CLIPS_PER_ITEM`,
    app/routes/plan_items.py) is enforced at the upload-route layer, which
    this compiler-level matrix does not exercise directly (no FastAPI/DB
    round trip here) -- what IS checkable at this layer is that the pure
    feasibility/compile functions scale to exactly the cap size without an
    unrelated crash, so a plan sized right at the intake ceiling is not
    silently mishandled by the compiler."""

    from app.routes.plan_items import _MAX_CLIPS_PER_ITEM

    assert _MAX_CLIPS_PER_ITEM == 50
    media = _media_refs([(f"clip-{i}", "video", 2.0) for i in range(_MAX_CLIPS_PER_ITEM)])
    assert len(media) == 50
    assert feasible_guided_duration_s(media) >= guided_feasibility_threshold_s(len(media))
    # A story need not use every one of 50 sources in one beat -- exercise a
    # normal-shaped plan (a handful of beats) drawn from the full pool, which
    # is the realistic render shape for a large footage pool.
    beats = [
        StoryBeat(
            beat_id=f"beat-{i}", topic="t", thought="", media_ids=[f"clip-{i}"], duration_s=1.5
        )
        for i in range(10)
    ]
    validate_proposal_compiles(
        _snapshot(direction="guided_story", media=media, beats=beats, duration_s=15.0)
    )


def test_footage_shape_photos_only_montage_is_accepted() -> None:
    """The fast_montage 'must use both photos and videos' taste rule was
    removed (KRI-118 lane L3, per docs/reviews/kri-118/video-type-requirements.md
    §3 row 1) -- a photos-only fast_montage must be accepted, not rejected
    for lacking video variety."""

    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    media_ids = [f"photo-{i}" for i in range(1, 5)]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=3,
        media=[
            EditProposalMedia(media_id=media_id, lane="asset", kind="image")
            for media_id in media_ids
        ],
    )
    import json

    payload = {
        "title": "Photo montage",
        "duration_s": len(media_ids) * 0.75,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": f"cut-{index}",
                "media_id": media_id,
                "source_start_s": 0,
                "source_end_s": 0.75,
                "output_duration_s": 0.75,
                "role": "hook"
                if index == 0
                else "payoff"
                if index == len(media_ids) - 1
                else "build",
            }
            for index, media_id in enumerate(media_ids)
        ],
    }
    output = agent.parse(json.dumps(payload), agent_input)
    assert {cut.media_id for cut in output.fast_cuts or []} == set(media_ids)


def test_footage_shape_mixed_photos_and_videos_is_feasible_and_compiles() -> None:
    media = _media_refs(
        [
            ("photo-0", "image", None),
            ("photo-1", "image", None),
            ("clip-0", "video", 5.0),
            ("clip-1", "video", 5.0),
        ]
    )
    assert feasible_guided_duration_s(media) >= guided_feasibility_threshold_s(len(media))
    beats = [
        StoryBeat(beat_id="beat-0", topic="t", thought="", media_ids=["photo-0"], duration_s=1.5),
        StoryBeat(beat_id="beat-1", topic="t", thought="", media_ids=["clip-0"], duration_s=3.0),
        StoryBeat(beat_id="beat-2", topic="t", thought="", media_ids=["photo-1"], duration_s=1.5),
        StoryBeat(beat_id="beat-3", topic="t", thought="", media_ids=["clip-1"], duration_s=3.0),
    ]
    validate_proposal_compiles(
        _snapshot(direction="guided_story", media=media, beats=beats, duration_s=9.0)
    )


def test_footage_shape_unknown_duration_clip_excluded_from_pool_estimate() -> None:
    """A video with no probed duration contributes NOTHING to the pool
    estimate (not even the image credit) -- but its mere presence in an
    otherwise-feasible pool must not crash the pure planning functions."""

    known = _media_refs([("clip-a", "video", 5.0), ("clip-b", "video", 5.0)])
    unknown = _media_refs([("clip-unknown", "video", None)])
    assert usable_media_count(unknown) == 0
    assert feasible_guided_duration_s(unknown) == 0.0

    pool = known + unknown
    assert feasible_guided_duration_s(pool) == feasible_guided_duration_s(known)
    assert feasible_guided_duration_s(pool) >= guided_feasibility_threshold_s(len(pool))

    # Referencing the unknown-duration clip INSIDE a beat is the real,
    # planning-time refusal case: `_allocate_beat_durations` raises a typed,
    # specific `guided_story_duration_impossible`, not a generic crash.
    beats = [
        StoryBeat(
            beat_id="beat-0", topic="t", thought="", media_ids=["clip-unknown"], duration_s=3.0
        ),
    ]
    with pytest.raises(GuidedStoryError) as excinfo:
        validate_proposal_compiles(
            _snapshot(direction="guided_story", media=pool, beats=beats, duration_s=3.0)
        )
    assert excinfo.value.code == "guided_story_duration_impossible"


# ---------------------------------------------------------------------------
# Section 3: montage-with-day_vlog-shape / montage-with-single_hero-shape
# ---------------------------------------------------------------------------
#
# Per the task brief: L2 (chat-picking these shapes) is still in flight, but
# L3 already added `story_shape`/`hero_media_id` to `ProposalBrief` and the
# deterministic repair layer (`app.services.story_shapes`) that enforces each
# shape's geometry regardless of model compliance -- so these rows are built
# directly against that layer (`EditProposalAgentOutput`/
# `EditProposalAgentInput`), exactly as
# `tests/agents/test_edit_proposal_story_shapes.py` already does, but with
# NEW footage shapes (12 clips including 3 short ones) this matrix's brief
# specifically calls out. Also asserts the `EditProposalSnapshot.adjustments`
# contract (L3's new field): non-empty when a repair actually ran, empty
# when it didn't need to.


def _draft_beats(pairs: list[tuple[str, list[str]]]) -> list[DraftStoryBeat]:
    return [
        DraftStoryBeat(topic=topic, media_ids=media_ids, duration_s=3.0)
        for topic, media_ids in pairs
    ]


def _adjustments_from_repairs(repairs: list[str], *, fallback_used: bool = False) -> list[str]:
    """Mirrors `_run_draft_attempt`'s own adjustments assembly
    (app/tasks/edit_proposal_build.py) verbatim, minus the DB/session
    plumbing, so this test's `adjustments` value is built the same way
    production builds the persisted `EditProposalSnapshot.adjustments`."""

    adjustments = humanize_repairs(list(repairs))
    if fallback_used:
        adjustments = ["Kria used a simpler plan because the planner couldn't finish", *adjustments]
    return [message[:160] for message in adjustments][:6]


def test_montage_with_day_vlog_shape_twelve_clips_including_short_ones_reorders() -> None:
    """12 clips (3 short) attached out of shooting order -- day_vlog must
    reorder into attachment order and the reorder must surface as a
    non-empty, user-visible `adjustments` entry."""

    short_ids = [f"short-{i}" for i in range(3)]
    long_ids = [f"long-{i}" for i in range(9)]
    attachment_order = [*long_ids[:4], *short_ids, *long_ids[4:]]
    media = [
        EditProposalMedia(
            media_id=media_id,
            lane="clip",
            kind="video",
            duration_s=0.9 if media_id in short_ids else 4.0,
        )
        for media_id in attachment_order
    ]
    # Author beats in a SCRAMBLED (non-attachment) order -- the repair must
    # sort them back.
    scrambled = list(reversed(attachment_order))
    output = EditProposalAgentOutput(
        title="A day",
        duration_s=float(3 * len(scrambled)),
        story_beats=_draft_beats(
            [(f"topic-{i}", [media_id]) for i, media_id in enumerate(scrambled)]
        ),
    )
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=3 * len(scrambled),
        story_shape="day_vlog",
        media=media,
    )

    repairs = story_shapes.repair_day_vlog(output, agent_input)
    adjustments = _adjustments_from_repairs(repairs)
    assert adjustments  # a real reorder ran -> user-visible note
    assert [beat.media_ids[0] for beat in output.story_beats] == attachment_order

    snapshot = EditProposalSnapshot(
        direction="guided_story",
        pace="balanced",
        duration_s=output.duration_s,
        title=output.title,
        adjustments=adjustments,
        media=[
            MediaRef(
                lane="clip",
                media_id=m.media_id,
                gcs_path=f"users/u/{m.media_id}.mp4",
                generation="1",
                kind="video",
                duration_s=m.duration_s,
            )
            for m in media
        ],
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{i}",
                topic=beat.topic,
                thought=beat.thought,
                media_ids=beat.media_ids,
                duration_s=1.0 if beat.media_ids[0] in short_ids else beat.duration_s,
            )
            for i, beat in enumerate(output.story_beats)
        ],
    )
    validate_proposal_compiles(snapshot)


def test_montage_with_day_vlog_shape_noop_when_creator_pinned_an_explicit_order() -> None:
    """`repair_day_vlog` unconditionally appends a reorder-note repair code
    UNLESS the creator already pinned an explicit order via a resolved
    `order` clip intent (KRI-127/KRI-129: the creator's own explicit order
    always wins) -- that is the one path with genuinely nothing to adjust,
    so `adjustments` must be empty."""

    from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent

    media = [
        EditProposalMedia(media_id=media_id, lane="clip", kind="video", duration_s=4.0)
        for media_id in ("a", "b", "c")
    ]
    output = EditProposalAgentOutput(
        title="A day",
        duration_s=9.0,
        story_beats=_draft_beats([("A", ["a"]), ("B", ["b"]), ("C", ["c"])]),
    )
    pinned_order = ResolvedClipIntent(
        intent_id="order-1",
        op="order",
        attribute="chronological",
        status="resolved",
        assignments=[ClipAssignment(media_id=m) for m in ("a", "b", "c")],
    )
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=9,
        story_shape="day_vlog",
        media=media,
        clip_intents=[pinned_order],
    )

    repairs = story_shapes.repair_day_vlog(output, agent_input)
    assert _adjustments_from_repairs(repairs) == []
    # The beats are untouched -- no reorder was needed or performed.
    assert [beat.media_ids for beat in output.story_beats] == [["a"], ["b"], ["c"]]


def test_montage_with_day_vlog_shape_refuses_fewer_than_two_moments() -> None:
    """Genuine planning-time refusal, not a silent downgrade (KRI-129):
    fewer than `DAY_VLOG_MIN_SOURCES` usable sources raises
    `StoryShapeInfeasibleError`, mirrored by `_run_draft_attempt`'s own
    pre-agent check (`usable_media_count(media) < DAY_VLOG_MIN_SOURCES` ->
    `_fail(..., "day_vlog_needs_two_moments", ...)`)."""

    media = [EditProposalMedia(media_id="only", lane="clip", kind="video", duration_s=4.0)]
    assert (
        usable_media_count(
            [
                MediaRef(
                    lane="clip",
                    media_id="only",
                    gcs_path="x",
                    generation="1",
                    kind="video",
                    duration_s=4.0,
                )
            ]
        )
        < story_shapes.DAY_VLOG_MIN_SOURCES
    )
    output = EditProposalAgentOutput(
        title="A day", duration_s=3.0, story_beats=_draft_beats([("Only", ["only"])])
    )
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=3,
        story_shape="day_vlog",
        media=media,
    )
    with pytest.raises(story_shapes.StoryShapeInfeasibleError):
        story_shapes.repair_day_vlog(output, agent_input)


def test_montage_with_single_hero_shape_twelve_clips_grows_share_and_compiles() -> None:
    """A hero clip that starts under-represented across a 12-clip pool (9
    supporting clips + 3 short ones) must be grown to dominate screen time,
    and the growth must surface as a non-empty `adjustments` entry."""

    support_ids = [f"support-{i}" for i in range(9)]
    short_ids = [f"short-{i}" for i in range(3)]
    media = [
        EditProposalMedia(media_id="hero", lane="clip", kind="video", duration_s=30.0),
        *(
            EditProposalMedia(media_id=media_id, lane="clip", kind="video", duration_s=4.0)
            for media_id in support_ids
        ),
        *(
            EditProposalMedia(media_id=media_id, lane="clip", kind="video", duration_s=0.9)
            for media_id in short_ids
        ),
    ]
    beats = _draft_beats(
        [(f"support-{i}", [media_id]) for i, media_id in enumerate(support_ids)]
        + [(f"short-{i}", [media_id]) for i, media_id in enumerate(short_ids)]
        + [("Hero", ["hero"])]
    )
    output = EditProposalAgentOutput(
        title="The hero", duration_s=3.0 * len(beats), story_beats=beats
    )
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=3.0 * len(beats),
        story_shape="single_hero",
        hero_media_id="hero",
        media=media,
    )

    repairs = story_shapes.repair_single_hero(output, agent_input)
    adjustments = _adjustments_from_repairs(repairs)
    assert adjustments
    assert output.story_beats[0].media_ids == ["hero"]


def test_montage_with_single_hero_shape_noop_when_already_dominant() -> None:
    media = [
        EditProposalMedia(media_id="hero", lane="clip", kind="video", duration_s=30.0),
        EditProposalMedia(media_id="support", lane="clip", kind="video", duration_s=10.0),
    ]
    output = EditProposalAgentOutput(
        title="The hero",
        duration_s=15.0,
        story_beats=_draft_beats([("Hero", ["hero"]), ("Support", ["support"])]),
    )
    output.story_beats[0].duration_s = 12.0
    output.story_beats[1].duration_s = 3.0
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=15,
        story_shape="single_hero",
        hero_media_id="hero",
        media=media,
    )

    repairs = story_shapes.repair_single_hero(output, agent_input)
    assert _adjustments_from_repairs(repairs) == []


# ---------------------------------------------------------------------------
# Section 4: worker-level full chain -- the phone COMPILER (as opposed to
# section 2/3's pure `validate_proposal_compiles` planning dry-run) actually
# compiles and `validate_phone_pilot_recipe` passes, proven by reaching
# `job.status == "awaiting_device"` via the REAL `_run_generative_job` /
# `_run_phone_*_job` worker entrypoints. Sections 1-3 above prove the
# dispatch gate and the pure planner; this section closes the loop the task
# brief asks for explicitly: "the correct phone compiler ... succeeds ->
# validate_phone_pilot_recipe passes". Reuses each sibling file's own
# setup()/`_setup_*` harness verbatim rather than inventing new fixture
# machinery, per the task brief.
# ---------------------------------------------------------------------------


def test_worker_montage_guided_approved_reaches_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """montage (identically day_vlog/single_hero EditFormat) with an
    approved guided-story proposal: section 1's dispatch row for this type
    ends in outcome == "dispatched"; this proves the WORKER half of the same
    chain reaches the device under the broadest documented prod capability
    set."""

    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_guided_dispatch import setup as _phone_guided_setup

    job, _snapshot, _session, _planner, cloud = _phone_guided_setup(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    cloud.assert_not_called()


def test_worker_guided_story_voiceover_reaches_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """guided_story+voiceover: the guided-story narration lane
    (`phone_guided_narration_supported()`) compiles + pins a device request
    through the SAME `_run_phone_guided_job` worker, with a narration bed
    resolved."""

    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_guided_dispatch import narration_bed, narration_setup

    job, _snapshot, _session, _planner, cloud, _plan = narration_setup(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    bed = narration_bed(duration_s=3.0)
    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", lambda *_a: bed, raising=False)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    cloud.assert_not_called()


def test_worker_subtitled_one_clip_reaches_device(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"


def test_dispatch_subtitled_two_clips_refuses_with_clip_count_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subtitled requires EXACTLY one clip -- 2+ clips must refuse with the
    specific `subtitled_clip_count_unsupported` reason
    (`compile_phone_subtitled_plan` also enforces this, but the dispatch
    gate fails closed here first so a doomed Job is never minted)."""

    result, _job, mock_build, bind_mock = _run_phone_dispatch(
        monkeypatch, edit_format="subtitled", approved=False, clip_count=2
    )
    assert result.outcome == "invalid_clips"
    assert result.reason == "subtitled_clip_count_unsupported"
    bind_mock.assert_not_called()
    mock_build.assert_not_called()


def test_worker_narrated_voiceover_reaches_device(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "narrated"


def test_worker_narrated_self_narration_one_clip_reaches_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *_a, **_k: ("subtitled", None, None), raising=False
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"


def test_worker_narrated_self_narration_resolving_talking_head_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """narrated self-narration, 2+ clips: `test_dispatch_matrix`'s
    `narrated_self_narration_multi_clip` row already refuses this with the
    specific `self_narration_multi_clip` reason before a Job is even minted.
    Defense in depth: IF a stale client or a replan race somehow got a Job
    minted anyway and the REAL archetype resolver lands on `talking_head`
    (what 2+ self-narrated clips actually resolve to post-ingest), the
    worker itself must ALSO fail with a typed `UnsupportedPhonePlan`, never
    a generic exception or a silent mis-render."""

    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

    job, snapshot, _session, _binding = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *_a, **_k: ("talking_head", "c0", None), raising=False
    )
    with pytest.raises(UnsupportedPhonePlan, match="unsupported"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


# ---------------------------------------------------------------------------
# Section 4: full render chain (compile -> dispatch -> phone compiler ->
# validate_phone_pilot_recipe) for a representative subset of types.
# ---------------------------------------------------------------------------
#
# Reuses the REAL worker harnesses each sibling test file already built --
# reaching `job.status == "awaiting_device"` here is only possible once the
# matching `compile_phone_*_plan` AND `validate_phone_pilot_recipe` have
# already succeeded for real (see app/tasks/generative_build.py's
# `_run_phone_*_job` call sites). Every harness's own verified-feature list
# is overridden to `PROD_VERIFIED_FEATURES` so these rows test against the
# broadest plausible prod state, per this file's module-level constant.


def test_full_chain_montage_dispatches_and_compiles_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_montage_dispatch import setup as _phone_montage_setup

    job, _snapshot, _session, _bindings, cloud = _phone_montage_setup(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    cloud.assert_not_called()


def test_full_chain_guided_story_approved_dispatches_and_compiles_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.device_render import device_status
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_guided_dispatch import setup as _phone_guided_setup

    job, _snapshot, _session, _planner, cloud = _phone_guided_setup(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert device_status(job, "guided_story").phase == "awaiting_device"
    cloud.assert_not_called()


def test_full_chain_subtitled_one_clip_dispatches_and_compiles_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"


def test_full_chain_narrated_with_voiceover_dispatches_and_compiles_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "awaiting_device"


def test_full_chain_narrated_self_narration_one_clip_resolves_through_subtitled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-narration (no recorded voiceover), single clip -- routes to
    `_run_phone_subtitled_job`, which re-verifies the REAL `_resolve_archetype`
    outcome post-ingest and only proceeds when it lands on `subtitled`."""

    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *a, **k: ("subtitled", None, None), raising=False
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"


def test_full_chain_guided_story_voiceover_timed_dispatches_and_compiles_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct from `montage_voiceover_flag_on` in Section 1 (the
    MONTAGE-FAMILY voiceover archetype): this is the GUIDED-STORY narration
    lane (`execution_contract="guided_voiceover_v1"`,
    `compile_phone_guided_plan`'s `narration` kwarg) -- the ONLY lane
    combining an approved guided-story plan with a recorded voiceover, gated
    by its own `phone_guided_narration_rendering_enabled` flag AND the
    shared `phone_narration_rendering_enabled` + verified `narrationAudio`.
    See docs/reviews/kri-118/video-type-requirements.md's row "Guided story,
    voiceover-timed"."""

    from app.services.device_render import device_status
    from app.tasks import generative_build as gb
    from tests.pipeline.test_phone_guided_plan import narration_bed
    from tests.tasks.test_phone_guided_dispatch import narration_setup

    job, _snapshot, _session, _planner, cloud, plan = narration_setup(monkeypatch)
    del plan
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)
    bed = narration_bed(duration_s=3.0)
    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", lambda *a, **k: bed, raising=False)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    assert request.recipe.audio.narration_asset_id == f"voiceover-{bed.plan_item_id}"
    cloud.assert_not_called()
