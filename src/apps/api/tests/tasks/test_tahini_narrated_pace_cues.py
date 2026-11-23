"""Tahini run: "change the picture about once a second" and clips named twice, replayed.

Phone Voiceover job 1d062f21 (2026-11-17): a 33.1 s recipe voiceover over twelve ~5 s clips. The
brief typed the pace ask (r16: ``approximate_cut_duration_s: 1``, "reuse a clip if needed") and
three clips the opening list named were named again on words at the end (r13 "carrots on",
r14 "topping over", r15 "grab a fork"). The render had 12 shots (one per clip, 1.5-5.2 s), the
three clips played only in the opening, the last 5.2 s showed the yogurt clip alone, the 3.7 s
dates clip was slowed to 0.73 over a 5.04 s step, the "sticky" tray moment never played, and the
review listed the pace and all three cues under "Have a look at these in the video".

``tests/fixtures/narrated_alignment/tahini_pace_cues.json`` is the job's own record: word times
and the alignment agent's answer (its run's I/O), each clip's length and analysed moments, the
resolved intents, the approved brief and the recorded steps (media renamed c0..c11).

Ways this can fail, written before the code:

 T1 the replay is not the incident: with both switches off the steps are not the recorded 12
    (clips, windows, in-points), the dates clip is not slowed, or the review is not "Have a
    look" for the pace and the three cues, so a pass proves nothing (red control).
 T2 the pace is not followed: fewer than 20 shots for 33.1 s, a shot far longer than the pace,
    or a clip still slowed below 1.0 while unseen footage exists; or the steps stop tiling the
    voiceover and the compile refuses; or the editor's cut and the recipe disagree.
 T3 a twice-named clip is still placed once: when the voice says "carrots on", "topping over",
    "grab a fork" another clip is on screen, or the video does not end on the fork clip; or a
    re-shown clip takes the opening step's first seat (its record row carries `placed`).
 T4 the re-shown clip replays footage although the plan forbids repeats, or the pace splits a
    clip off its own named word.
 T5 the 1 ms rounding still refuses the "sticky" fill, so the roasted-tray moment never plays.
 T6 the review still says "Have a look" (unchecked) for the pace or a cue; or it says "Done"
    for a cue whose clip is not on screen on its word (wrong-content control: the same render
    judged against a cue scoped to another clip must be "Couldn't", with both clips' times).
 T7 a draft is newly held: the pace ask gets a pre-render verdict.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    check_requirement,
    is_judged,
    plan_facts_from_narrated_alignment,
    plan_facts_from_phone_variant,
)
from app.schemas.edit_proposal import resolve_video_reuse_policy
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from app.tasks import kria_runtime
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks import test_phone_subtitled_narrated_dispatch as nd

CASE = json.loads(
    (Path(__file__).parent.parent / "fixtures/narrated_alignment/tahini_pace_cues.json").read_text(
        encoding="utf-8"
    )
)
CLIPS = CASE["clips"]
VOICE_END = CASE["voiceover_s"]
WORDS = CASE["words"]
REQUIREMENTS = [BriefRequirement.model_validate(row) for row in CASE["requirements"]]
PACE_ASK = next(r for r in REQUIREMENTS if r.id == "r16")
CARROTS, TOPPING, FORK, DATES, TRAY, YOGURT = "c0", "c1", "c2", "c8", "c6", "c11"


def _word_at(text: str, after_s: float = 0.0) -> tuple[float, float]:
    """Start and end of the first word ``text`` said at or after ``after_s``."""
    return next(
        (start, end)
        for word, start, end in WORDS
        if word.strip(".,").casefold() == text.casefold() and start >= after_s
    )


def _metas() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            clip_id=clip["clip_id"],
            hook_score=5.0,
            hook_text="",
            transcript="",
            clip_summary=clip["summary"],
            activity=clip["activity"],
            detected_subject=clip["subject"],
            setting=clip["setting"],
            best_moments=clip["best_moments"],
        )
        for clip in CLIPS
    ]


def _setup(monkeypatch, *, fixed: bool, requirements=REQUIREMENTS):
    """The recorded job. ``fixed``: both Tahini switches on, and the plan's reuse permission is
    what the fixed reader makes of r16's words (the recorded plan said "once")."""
    reuse = resolve_video_reuse_policy(PACE_ASK.description) if fixed else "once"
    job, snapshot, _session, _bindings = nd._setup_narrated(
        monkeypatch,
        n_clips=len(CLIPS),
        spoken_words=nd._words(*[(t, s, e) for t, s, e in WORDS]),
        voiceover_s=VOICE_END,
        metas=_metas(),
        extra_candidates={
            "creator_request": "",
            "creator_strategy": {
                "resolved_clip_intents": CASE["resolved_clip_intents"],
                "video_reuse_policy": reuse,
                "edit_format": "narrated_planned",
                "audio_strategy": "voiceover",
            },
            "language": "en",
        },
        real_phrases=True,
        alignment=True,
    )
    sources = [
        _binding(clip["clip_id"], duration_s=clip["duration_s"]).model_dump(mode="json")
        for clip in CLIPS
    ]
    snapshot[PHONE_SOURCES_FIELD] = sources
    job.assembly_plan[PHONE_SOURCES_FIELD] = [dict(source) for source in sources]
    binding = BriefBinding.create(
        uuid.uuid4(),
        CreativeBrief(version=2, requirements=list(requirements)),
        media_snapshot={
            "clip_assignments": [
                {"media_id": clip["clip_id"], "analysis": clip["stored_analysis"]}
                for clip in CLIPS
                if clip["stored_analysis"]
            ]
        },
    )
    snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    monkeypatch.setattr(gb.settings, "narrated_footage_fit_enabled", True)
    monkeypatch.setattr(gb.settings, "narrated_cut_pace_enabled", fixed)
    monkeypatch.setattr(gb.settings, "narrated_cue_receipts_enabled", fixed)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "variableSpeed"],
    )
    nd._mock_alignment_agent(
        monkeypatch, CASE["alignment"]["order"], CASE["alignment"]["start_words"]
    )
    return job, snapshot, binding


def _run(job, snapshot):
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    recipe = device_status(job, "narrated").request.recipe
    video = next(track for track in recipe.tracks if track.kind == "video")
    return recipe, list(video.clips)


def _media(clip) -> str:
    return clip.source_asset_id  # the harness's render asset id is the clip id


def _on_screen(clips, at_s: float):
    return next(c for c in clips if c.timeline_start <= at_s < c.timeline_start + _length(c))


def _length(clip) -> float:
    return clip.source_duration / clip.rate


def _shown_at(clips, media: str, source_s: float) -> float | None:
    for clip in clips:
        if _media(clip) != media:
            continue
        if clip.source_start <= source_s < clip.source_start + clip.source_duration:
            return clip.timeline_start + (source_s - clip.source_start) / clip.rate
    return None


def _rows(job) -> list[dict]:
    return job.assembly_plan["narrated_alignment"]["steps"]


def _review(job, binding) -> dict[str, dict]:
    [variant] = job.assembly_plan["variants"]
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    _text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return {row["requirement_id"]: row for row in payload}


def _assert_tiles(clips) -> None:
    cursor = 0.0
    for clip in clips:
        assert clip.timeline_start == pytest.approx(cursor, abs=1e-3)
        cursor += _length(clip)
    assert cursor == pytest.approx(VOICE_END, abs=1e-3)


# -- T1: the replay is the incident ---------------------------------------------------------


def test_t1_with_both_switches_off_the_render_and_review_are_the_recorded_ones(
    monkeypatch,
) -> None:
    job, snapshot, binding = _setup(monkeypatch, fixed=False)

    _recipe, clips = _run(job, snapshot)

    recorded = CASE["recorded"]["steps"]
    assert [
        (_media(c), round(c.timeline_start, 2), round(c.timeline_start + _length(c), 2))
        for c in clips
    ] == [(s["clip_id"], round(s["start_s"], 2), round(s["end_s"], 2)) for s in recorded]
    assert [round(c.source_start, 3) for c in clips] == [
        round(s["source_start_s"], 3) for s in recorded
    ]
    dates = next(c for c in clips if _media(c) == DATES)
    assert dates.rate == pytest.approx(0.726, abs=0.002)  # the slowed 3.7 s clip
    assert _media(clips[-1]) == YOGURT  # the last 5.2 s: one clip
    assert _shown_at(clips, TRAY, 3.7) is None  # "sticky": the tray moment never plays
    rows = _review(job, binding)
    for rid in ("r13", "r14", "r15", "r16"):
        assert rows[rid]["verification"] == "unchecked", rows[rid]


# -- T2-T5: the fixed render ------------------------------------------------------------------


def test_t2_the_picture_changes_about_once_a_second_without_slow_motion(monkeypatch) -> None:
    job, snapshot, _binding_ = _setup(monkeypatch, fixed=True)

    recipe, clips = _run(job, snapshot)

    assert len(clips) >= 20, len(clips)
    _assert_tiles(clips)
    assert recipe.duration == pytest.approx(VOICE_END, abs=1e-3)
    # Unseen footage was there for every step: nothing is slowed.
    assert min(c.rate for c in clips) == pytest.approx(1.0)
    assert max(_length(c) for c in clips) <= 1.5 + 1e-3
    for before, after in zip(clips, clips[1:]):
        assert _media(before) != _media(after)
    # The editor's cut is this recipe's.
    variant = job.assembly_plan["variants"][0]
    assert [(t["start_s"], t["end_s"]) for t in variant["narrated_timings"]] == [
        (round(c.timeline_start, 3), round(c.timeline_start + _length(c), 3)) for c in clips
    ]
    # Every aligned clip still comes on screen on its own word (the pace never moves it).
    for step in CASE["recorded"]["steps"]:
        assert _media(_on_screen(clips, step["start_s"] + 1e-3)) == step["clip_id"]


def test_t3_t4_each_twice_named_clip_is_on_screen_on_its_word_and_the_video_ends_on_the_fork(
    monkeypatch,
) -> None:
    job, snapshot, _binding_ = _setup(monkeypatch, fixed=True)

    _recipe, clips = _run(job, snapshot)

    carrots_at, _ = _word_at("carrots", 28.0)
    topping_at, _ = _word_at("Topping", 29.0)
    _over, over_end = _word_at("over", 29.0)
    grab_at, _ = _word_at("grab")
    assert _media(_on_screen(clips, carrots_at + 0.05)) == CARROTS
    topping = _on_screen(clips, over_end - 0.05)
    assert _media(topping) == TOPPING
    assert topping.timeline_start - topping_at <= 0.5  # on "Topping over", at most 0.5 s late
    fork = _on_screen(clips, grab_at + 0.01)
    assert _media(fork) == FORK and fork.timeline_start <= grab_at + 1e-3
    assert _media(clips[-1]) == FORK  # the video ends on the fork bite
    # Each comes back from footage the opening did not show, or the moment the creator named
    # (repeats are allowed here: "reuse a clip if needed").
    opening = {_media(c): c for c in clips if c.timeline_start < 6.0}
    for media in (CARROTS, TOPPING, FORK):
        assert media in opening
    # The record: the re-shown shots carry the creator's words but not the opening seat.
    again = [row for row in _rows(job) if row.get("shown_on_word")]
    assert {row["media_id"] for row in again} == {CARROTS, TOPPING, FORK}
    assert all(row["placed"] == [] and row["labels"] for row in again)
    carrots_row = next(row for row in again if row["media_id"] == CARROTS)
    assert carrots_row["moment"]["words"] == 'carrots laid on it on "carrots on"'
    # The split carrot plays within the word ("carrots" 28.86 s, "on." ends 29.34 s).
    assert 0 <= carrots_row["moment"]["at_s"] - carrots_at <= 0.5


def test_t4_without_permission_to_repeat_a_reshown_clip_plays_only_unseen_footage(
    monkeypatch,
) -> None:
    job, snapshot, _binding_ = _setup(monkeypatch, fixed=True)
    job.all_candidates["creator_strategy"]["video_reuse_policy"] = "once"

    _recipe, clips = _run(job, snapshot)

    seen: dict[str, list[tuple[float, float]]] = {}
    for clip in clips:
        span = (clip.source_start, clip.source_start + clip.source_duration)
        for start, end in seen.get(_media(clip), []):
            assert span[1] <= start + 1e-3 or span[0] >= end - 1e-3, (_media(clip), span)
        seen.setdefault(_media(clip), []).append(span)
    grab_at, _ = _word_at("grab")
    assert _media(_on_screen(clips, grab_at + 0.01)) == FORK


def test_t5_the_sticky_tray_moment_plays_on_its_word(monkeypatch) -> None:
    job, snapshot, _binding_ = _setup(monkeypatch, fixed=True)

    _recipe, clips = _run(job, snapshot)

    sticky_at, _ = _word_at("sticky")
    shown = _shown_at(clips, TRAY, 3.7)  # "still shot of roasted carrots", 3.7-5.0 s
    assert shown is not None and 0 <= shown - sticky_at <= 0.5, shown


# -- T6 / T7: the review ----------------------------------------------------------------------


def test_t6_the_review_judges_the_pace_and_every_cue(monkeypatch) -> None:
    job, snapshot, binding = _setup(monkeypatch, fixed=True)
    _run(job, snapshot)

    rows = _review(job, binding)

    for rid in ("r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"):
        assert rows[rid]["verification"] == "checked", rows[rid]
        assert rows[rid]["status"] == "met", rows[rid]
    assert rows["r15"]["reason"].startswith("When you say grab a fork, the clip you named")
    assert rows["r16"]["verification"] == "checked" and rows["r16"]["status"] == "met"
    assert rows["r16"]["reason"].startswith(f"{len(_rows(job))} shots in 33.1 s")


def test_t6_a_cue_scoped_to_a_clip_that_is_not_on_its_word_is_couldnt(monkeypatch) -> None:
    """Wrong-content control: the same fixed render, judged against "the fork bite" scoped to
    the yogurt clip (which is not on screen on "grab a fork"), must not pass."""
    job, snapshot, _binding_ = _setup(monkeypatch, fixed=True)
    _run(job, snapshot)
    [variant] = job.assembly_plan["variants"]
    facts = dataclasses.replace(
        plan_facts_from_phone_variant(variant),
        narrated_cue_steps=plan_facts_from_narrated_alignment(
            job.assembly_plan["narrated_alignment"]
        ).narrated_steps,
    )
    fork_ask = next(r for r in REQUIREMENTS if r.id == "r15")
    wrong = fork_ask.model_copy(update={"scope": "clip:analysis-proxy-c11.mp4"})

    right = check_requirement(fork_ask, facts)
    receipt = check_requirement(wrong, facts)

    assert right.status == "met"
    assert receipt.status == "not_possible" and is_judged(wrong, receipt)
    assert receipt.reason.startswith("When you say grab a fork, another clip is on screen (32.2")
    assert "27.9–" in receipt.reason.split("the clip you named plays at ", 1)[1]


def test_t6_the_recorded_render_is_said_to_miss_the_pace_and_the_cues(monkeypatch) -> None:
    """The recorded 12-step render, judged with the receipts on: the pace is "Partly" with its
    numbers and the fork cue is "Couldn't", never "Have a look"."""
    job, snapshot, binding = _setup(monkeypatch, fixed=False)
    _run(job, snapshot)
    monkeypatch.setattr(gb.settings, "narrated_cue_receipts_enabled", True)

    rows = _review(job, binding)

    assert rows["r16"]["status"] == "partial"
    assert rows["r16"]["reason"].startswith("12 shots in 33.1 s, about one every 2.8 s, not every")
    assert rows["r15"]["status"] == "not_possible"
    assert "the clip you named plays at 4.2–6.0 s" in rows["r15"]["reason"]


def test_t7_a_draft_gets_the_same_receipts_and_holds_with_the_receipts_on(monkeypatch) -> None:
    """Render-time only: on a phone Voiceover draft (no steps yet) every live ask of the brief
    gets the receipt, and the approval-readiness pre-render reading, it had before."""
    from app.kria.approval_readiness import _no_pre_render_checker  # noqa: PLC0415

    draft = PlanFacts(phone_narrated=True)
    seen = {}
    for on in (False, True):
        monkeypatch.setattr(gb.settings, "narrated_cue_receipts_enabled", on)
        seen[on] = [
            (check_requirement(req, draft).model_dump(), _no_pre_render_checker(req, draft))
            for req in REQUIREMENTS
            if req.live
        ]
    assert seen[True] == seen[False]
    assert not is_judged(PACE_ASK, check_requirement(PACE_ASK, draft))
