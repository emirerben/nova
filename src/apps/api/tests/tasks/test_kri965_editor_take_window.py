"""KRI-965: the iPhone editor plays the same part of the take the phone renders.

Don Julio walk-in T1 (2026-10-26, job ac91bb4f): one 26.93 s phone Talking take, cut to the
creator's quoted lines with speech cleanup off -- start at "what do you think?" (16.05 s), end
right after "Nice!" (20.27 s). The phone rendered the right 4.22 s. The editor rebuilds its
source from ``silence_cut.removed`` alone (iOS ``NativePhoneTalkingSource.removedSpans``, whose
server mirror is ``editor_deletion_timeline``), and a start/end cut with cleanup off was never
written there (``opening_cut_only``). So the editor played -- and Save/Share exported -- the
whole take, with the captions at 0-4.2 s over footage 16 s before anyone speaks.

Each case runs the real worker (``_run_phone_subtitled_job``) and compares the source spans
the editor plays with the ones the device recipe plays. The word timings follow the kit take's
shape (``kria-julio2-test`` T1); the window each case expects is placed off those words, never
off the worker's own receipts.

How a fix could go wrong, each pinned below (``[guard]`` = already true on main, must stay):

1. Start + end lines, cleanup off: the editor plays the whole take (the incident).
2. Start line only: the editor plays the silent head the phone cut.
3. A line named to cut out of the middle (KRI-821), cleanup off: the editor keeps it, and every
   caption after it lands early by its length.
4. The fix reports the trim as speech cleanup: a ``silence_cut_outcome`` or outcome context is
   written, so the creator is told pauses were removed (or there was nothing to clean) for a
   cleanup they declined; or the review counts the trim as pauses, fillers or retakes; or the
   creator's own trim receipt stops being "met".
5. The fix changes what the phone renders: the recipe must keep the window placed off the words.
6. Cleanup on with a quoted line already wrote the whole cut [guard]: editor and phone agree and
   it still reports its cleanup.
7. No trim asked, cleanup off [guard]: nothing is written; both play the whole take.
8. A quoted line the take doesn't hold is not applied [guard]: nothing is cut on either side.
9. The editor plays the original, which can be a hair longer than the analysis proxy the worker
   probed (T1: 26.934 s vs 26.933 s). An end cut recorded only to the proxy's end leaves the
   editor a sub-frame sliver of the take's last moment, played after the end line.

Not here: several speakers run no start/end or named cut at all (``multi_clip`` skips the opening
cut and refuses named cuts: ``test_multi_clip_talking_refuses_required_speech_cleanup`` and
friends), so the editor's whole-take sources are already the phone's.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    speech_cleanup_review_note,
)
from app.pipeline.phone_opening_cut import END_CUT_REASON, NAMED_CUT_REASON, OPENING_CUT_REASON
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.transcribe import Word
from app.routes.generative_jobs import editor_deletion_timeline
from app.services.clip_speech import SilenceDetectionResult
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests._silent_pcm import silent_pcm
from tests.tasks.test_generative_build_silence_cut import DURATION as CLEANUP_S
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _raw_preflight_snapshot,
    _setup_subtitled,
)

TAKE_S = 26.933
START = "what do you think?"
END = "Nice!"
NAMED = "Are you excited for the meat?"


def _w(text: str, start_s: float, end_s: float) -> Word:
    return Word(text=text, start_s=start_s, end_s=end_s, confidence=1.0)


# Dining room and the walk out are wordless; the talk is at the plaque, then "Zoom." at the end.
WORDS = [
    _w("So,", 15.2, 15.5),
    _w("what", 16.15, 16.3), _w("do", 16.3, 16.4), _w("you", 16.4, 16.5), _w("think?", 16.5, 16.8),
    _w("Are", 17.0, 17.1), _w("you", 17.1, 17.2), _w("excited", 17.2, 17.6), _w("for", 17.6, 17.7),
    _w("the", 17.7, 17.8), _w("meat?", 17.8, 18.1),
    _w("Super", 18.6, 18.9), _w("excited.", 18.9, 19.3),
    _w("Nice!", 19.7, 20.1),
    _w("Zoom.", 24.0, 24.4),
]  # fmt: skip
SO_END_S, WHAT_S = 15.5, 16.15
NICE_END_S, ZOOM_S = 20.1, 24.0
NAMED_S, NAMED_END_S = 17.0, 18.1


def _trim(*, start: str = START, end: str | None = END) -> BriefRequirement:
    facts = {"start_line": start}
    if end is not None:
        facts["end_line"] = end
    tail = f' and end right after "{end}"' if end else ""
    return BriefRequirement(
        id="r1",
        kind="timing",
        scope="global",
        description=f'Start at "{start}"{tail}',
        facts=facts,
    )


def _named_cut() -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="audio",
        scope="global",
        description=f'Cut the part where I ask "{NAMED}"',
        facts={"cut_lines": [NAMED]},
    )


def _title() -> BriefRequirement:
    return BriefRequirement(
        id="r3", kind="text", scope="title", literal="Before the meat", description="Title"
    )


def _checked_words_snapshot(storage_path: str) -> dict:
    """The checked preflight words a cleanup-off job carries (the KRI-704 shape)."""
    fingerprint = "a" * 64
    analysis = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=TAKE_S,
            source_window_start_s=0.0,
            source_window_end_s=TAKE_S,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=WORDS, language="en", low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
        pcm_loader=silent_pcm,
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": "c0",
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": TAKE_S,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _run(
    monkeypatch,
    reqs: list[BriefRequirement],
    *,
    cleanup: str = "off",
    cut: bool = True,
    original_s: float | None = None,
):
    """``cut``: a cut runs, so its preflight words are the captions' too (no new Whisper run);
    without one, a cleanup-off render transcribes the take for its captions as it always has.
    ``original_s``: the original's duration when it differs from the probed proxy's."""
    duration = TAKE_S if cleanup == "off" else CLEANUP_S
    job, snapshot, _session, binding = _setup_subtitled(
        monkeypatch, duration_s=duration, transcript_words=WORDS
    )
    # As in prod (T1's `clip_paths`): the editor finds the take by its analysis proxy.
    job.all_candidates = {**job.all_candidates, "clip_paths": [binding.proxy_path]}
    if original_s is not None:
        for plan in (snapshot, job.assembly_plan):
            plan[PHONE_SOURCES_FIELD][0]["original"]["duration_s"] = original_s
    if cleanup == "off":
        snapshot["speech_cleanup_contract"] = "off_v1"
        preflight = _checked_words_snapshot(binding.proxy_path)
    else:
        snapshot["speech_cleanup_contract"] = "required_v1"
        preflight = _raw_preflight_snapshot(storage_path=binding.proxy_path)
    snapshot["_speech_cleanup_internal"] = {"preflight_snapshot": preflight}
    if reqs:
        brief = CreativeBrief(version=1, requirements=reqs)
        snapshot["creator_brief_binding"] = BriefBinding.create(uuid.uuid4(), brief).model_dump(
            mode="json"
        )
    for key in ("creator_brief_binding", "speech_cleanup_contract", "_speech_cleanup_internal"):
        if key in snapshot:
            job.assembly_plan[key] = copy.deepcopy(snapshot[key])

    if cut:
        import app.pipeline.transcribe as transcribe_mod

        def _explode(*_a, **_k):
            raise AssertionError("the checked preflight words ground the cut, not a Whisper run")

        monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", _explode, raising=False)

    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    return job, job.assembly_plan["variants"][0]


def _phone_spans(job) -> list[tuple[float, float]]:
    """The source spans the device recipe plays, in order."""
    recipe = device_status(job, "subtitled").request.recipe
    video = next(t for t in recipe.tracks if t.kind == "video")
    return [(c.source_start, c.source_start + c.source_duration) for c in video.clips]


def _editor_spans(job, variant) -> list[tuple[float, float]]:
    """The source spans the editor plays (the server mirror of the iOS source rebuild)."""
    return [
        (row["in_s"], row["in_s"] + row["duration_s"])
        for row in editor_deletion_timeline(job, variant)
    ]


def _assert_editor_plays_the_phone_render(job, variant) -> list[tuple[float, float]]:
    phone, editor = _phone_spans(job), _editor_spans(job, variant)
    assert len(editor) == len(phone), (editor, phone)
    # The record persists source times to the millisecond.
    for (e_start, e_end), (p_start, p_end) in zip(editor, phone, strict=True):
        assert e_start == pytest.approx(p_start, abs=0.001), (editor, phone)
        assert e_end == pytest.approx(p_end, abs=0.001), (editor, phone)
    return phone


def _assert_not_reported_as_cleanup(variant) -> None:
    assert "silence_cut_outcome" not in variant
    assert "_speech_cleanup_outcome_context" not in variant
    assert speech_cleanup_review_note(variant) is None
    reasons = {row["reason"] for row in (variant.get("silence_cut") or {}).get("removed") or []}
    assert reasons <= {OPENING_CUT_REASON, END_CUT_REASON, NAMED_CUT_REASON}, reasons


def _receipt(reqs: list[BriefRequirement], variant, req_id: str):  # noqa: ANN202
    receipts = build_receipts(reqs, plan_facts_from_phone_variant(variant), include_unchecked=True)
    return {r.requirement_id: r for r in receipts}[req_id]


# -- 1. the incident: start + end lines, cleanup off ----------------------------------------------


def test_the_editor_plays_the_quoted_window_not_the_whole_take(monkeypatch):
    reqs = [_trim(), _title()]
    job, variant = _run(monkeypatch, reqs)

    [(start, end)] = _assert_editor_plays_the_phone_render(job, variant)
    # 5. The phone keeps the window placed off the words: after "So," and up to "what", and
    # from right after "Nice!" to well before "Zoom.".
    assert SO_END_S < start <= WHAT_S
    assert NICE_END_S <= end < ZOOM_S
    # Captions and title sit on that window: the first caption is the quoted line, at its start.
    cue = variant["caption_cues"][0]
    assert cue["text"].lower().startswith("what do you think")
    assert cue["start_s"] == pytest.approx(WHAT_S - start, abs=0.05)
    assert all(c["end_s"] <= end - start + 1e-6 for c in variant["caption_cues"])
    _assert_not_reported_as_cleanup(variant)
    assert _receipt(reqs, variant, "r1").status == "met"


def test_a_window_of_the_right_length_in_the_wrong_place_is_caught(monkeypatch):
    """Negative control: the same 4.35 s kept from the take's start (the dining room) has the
    count and length of the phone's window but the wrong footage, and the check rejects it."""
    job, variant = _run(monkeypatch, [_trim()])
    [(start, end)] = _phone_spans(job)
    wrong = copy.deepcopy(variant)
    wrong["silence_cut"]["removed"] = [
        {"start_s": round(end - start, 3), "end_s": TAKE_S, "reason": END_CUT_REASON}
    ]

    [(w_start, w_end)] = _editor_spans(job, wrong)
    assert w_end - w_start == pytest.approx(end - start, abs=0.001)
    with pytest.raises(AssertionError):
        _assert_editor_plays_the_phone_render(job, wrong)


# -- 2. start line only ---------------------------------------------------------------------------


def test_a_start_line_alone_drops_the_head_in_the_editor_too(monkeypatch):
    reqs = [_trim(end=None)]
    job, variant = _run(monkeypatch, reqs)

    [(start, end)] = _assert_editor_plays_the_phone_render(job, variant)
    assert SO_END_S < start <= WHAT_S
    assert end == pytest.approx(TAKE_S, abs=0.001)
    _assert_not_reported_as_cleanup(variant)
    assert _receipt(reqs, variant, "r1").status == "met"


# -- 3. a line named to cut out of the middle (KRI-821) -------------------------------------------


def test_a_named_cut_is_cut_in_the_editor_too(monkeypatch):
    reqs = [_named_cut()]
    job, variant = _run(monkeypatch, reqs)

    [(first_start, first_end), (second_start, second_end)] = _assert_editor_plays_the_phone_render(
        job, variant
    )
    assert first_start == pytest.approx(0.0, abs=0.001)
    # The named line is gone, the words around it stay.
    assert 16.8 <= first_end <= NAMED_S
    assert NAMED_END_S <= second_start <= 18.6
    assert second_end == pytest.approx(TAKE_S, abs=0.001)
    # Captions after the cut are on the cut timeline, so "Super" sits where the phone plays it.
    removed = second_start - first_end
    super_cue = next(c for c in variant["caption_cues"] if c["text"].startswith("Super"))
    assert super_cue["start_s"] == pytest.approx(18.6 - removed, abs=0.05)
    _assert_not_reported_as_cleanup(variant)


# -- 6. cleanup on with a quoted line [guard] -----------------------------------------------------


def test_cleanup_on_with_a_quoted_start_still_agrees_and_reports_its_cleanup(monkeypatch):
    reqs = [_trim(start="today", end=None)]
    job, variant = _run(monkeypatch, reqs, cleanup="on")

    spans = _assert_editor_plays_the_phone_render(job, variant)
    assert spans[0][0] > 0.7  # "so" (0.5-0.7) is before the start line
    assert variant["silence_cut_outcome"] == "applied"
    assert variant["_speech_cleanup_outcome_context"]["output_removal_count"] == 2
    reasons = {row["reason"] for row in variant["silence_cut"]["removed"]}
    assert OPENING_CUT_REASON in reasons


# -- 7. no trim asked [guard] ---------------------------------------------------------------------


def test_no_trim_writes_nothing_and_both_play_the_whole_take(monkeypatch):
    job, variant = _run(monkeypatch, [_title()], cut=False)

    [(start, end)] = _assert_editor_plays_the_phone_render(job, variant)
    assert (start, end) == pytest.approx((0.0, TAKE_S), abs=0.001)
    assert "silence_cut" not in variant
    _assert_not_reported_as_cleanup(variant)


# -- 8. a quoted line the take doesn't hold [guard] -----------------------------------------------


def test_a_line_not_in_the_take_cuts_nothing_on_either_side(monkeypatch):
    reqs = [_trim(start="where is the waiter", end=None)]
    job, variant = _run(monkeypatch, reqs, cut=False)

    assert variant["opening_cut"]["status"] == "not_applied"
    [(start, end)] = _assert_editor_plays_the_phone_render(job, variant)
    assert (start, end) == pytest.approx((0.0, TAKE_S), abs=0.001)
    assert "silence_cut" not in variant
    _assert_not_reported_as_cleanup(variant)


# -- 9. the original is a hair longer than the probed proxy ---------------------------------------


@pytest.mark.parametrize("reqs", [[_trim()], [_trim(start=START, end="Super excited.")]])
def test_an_end_cut_leaves_the_editor_no_sliver_of_the_take_end(monkeypatch, reqs):
    job, variant = _run(monkeypatch, reqs, original_s=26.934)  # T1's original vs 26.933 proxy

    [(start, end)] = _assert_editor_plays_the_phone_render(job, variant)
    assert SO_END_S < start <= WHAT_S
    assert end < ZOOM_S
    _assert_not_reported_as_cleanup(variant)
