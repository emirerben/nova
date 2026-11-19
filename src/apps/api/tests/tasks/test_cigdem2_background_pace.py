"""Çiğdem S2 "Cigdem 2" (2026-11-15, job ef12ddfd): "changing every 3-5 seconds" with four pictures.

The planner turns "3-5 seconds" into 4 s, which is also the default, and the strategy's dump
dropped a 4.0, so the asked pace never reached the worker: the drone photo stayed 15.1 s
(10.4-25.5 s) and neither the draft nor the review said so. Built from the run's own records
(``tests/fixtures/cigdem2_ef12ddfd_background_pace.json``: whisper words, the consented speech cut,
the planner's stored ``background_media``, the ready Visuals). Ground truth for where each picture
belongs is the kit's answer key, never the phrase the planner fed in. Failure modes:
``docs/reviews/cigdem-s2-failure-modes.md`` E1-E14.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import BackgroundMedia
from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.services import phone_background_sequence as seq
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)

RUN = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "cigdem2_ef12ddfd_background_pace.json").read_text()
)
WORDS = [{"text": t, "start_s": s, "end_s": e} for t, s, e in RUN["words"]]
TAKE_FULL_S = RUN["take_duration_s"]
REMOVED = [(start, end) for start, end, _reason in RUN["silence_cut_removed"]]


def _keep(removed, full_s):
    keep, t = [], 0.0
    for start, end in sorted(removed):
        if start > t + 1e-9:
            keep.append((t, start))
        t = max(t, end)
    if full_s > t + 1e-9:
        keep.append((t, full_s))
    return keep


KEEP = _keep(REMOVED, TAKE_FULL_S)
TAKE_S = seq.kept_duration(KEEP, TAKE_FULL_S)  # 25.474 s, the finished edit's speech


def _kept_words():
    """The words as the viewer hears them: the consented cut applied (output seconds)."""
    kept = []
    for item in WORDS:
        start = seq.to_output_time(item["start_s"], KEEP)
        end = seq.to_output_time(max(item["start_s"], item["end_s"] - 1e-3), KEEP)
        if start is not None and end is not None:
            kept.append({**item, "start_s": round(start, 3), "end_s": round(end + 1e-3, 3)})
    return kept


CADENCE_S = 4.0  # "changing every 3-5 seconds" -> the midpoint the planner is taught
PACE_LIMIT_S = CADENCE_S + 1.0  # the receipt's own slack: "3-5 s" is over 5 s

STORED = RUN["stored_background_media"]
ORDER = [entry["media_id"].removeprefix("asset-") for entry in STORED["sequence"]]
PHRASES = {
    entry["media_id"].removeprefix("asset-"): entry["spoken_phrase"] for entry in STORED["sequence"]
}
FILL = STORED["media_id"].removeprefix("asset-")
KIT = {visual["file"]: visual for visual in RUN["kit_answer_key"]["visuals"]}
NAMES = {visual["pool_id"]: name for name, visual in KIT.items()}
ON_WORD = {v["pool_id"]: v["on_word"] for v in KIT.values() if v.get("on_word")}
NEVER = {v["pool_id"] for v in KIT.values() if v.get("never")}
TOLERANCE_S = RUN["kit_answer_key"]["tolerance_s"]
DRONE = KIT["IMG_7123"]["pool_id"]


def _word_output_s(word: str, *, keep=KEEP, last: bool = False) -> float:
    """Where the kit word is first said (``last``: where the word ends), in output seconds."""
    for item in WORDS:
        if item["text"].strip(".,?!").casefold().startswith(word):
            at = seq.to_output_time(item["end_s" if last else "start_s"], keep)
            assert at is not None, word
            return at
    raise AssertionError(f"{word!r} is not in the Cigdem 2 words")


def _lay_out(*, keep_pace: bool, cadence_s: float = CADENCE_S, keep=KEEP, take_s=TAKE_S):
    """`lay_out` exactly as the worker calls it for the stored strategy; windows by media."""
    layout = seq.lay_out(
        [
            seq.SequenceEntry(index=i, spoken_phrase=PHRASES[media], media_key=media)
            for i, media in enumerate(ORDER)
        ],
        cadence_s=cadence_s,
        take_s=take_s,
        words=WORDS,
        keep_segments=keep,
        fill_key=FILL,
        keep_pace=keep_pace,
    )
    return layout, [(ORDER[w.index], w.start_s, w.end_s) for w in layout.windows]


def _describe(windows) -> str:
    return " | ".join(f"{NAMES.get(m, m)} {s:.2f}-{e:.2f}" for m, s, e in windows)


def _assert_lane_valid(windows, *, take_s=TAKE_S):
    """S62: what the phone lane accepts, and every second covered (the fill is a sequence photo,
    so it never shows in a gap)."""
    assert 1 <= len(windows) <= seq.MAX_WINDOWS
    assert windows[0][1] == pytest.approx(0.0, abs=1e-6)
    assert windows[-1][2] == pytest.approx(take_s, abs=1e-6)
    for _media, start, end in windows:
        assert end - start >= seq.MIN_WINDOW_S - 1e-6, _describe(windows)
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later[1] == pytest.approx(earlier[2], abs=1e-6), (
            f"gap or overlap: {_describe(windows)}"
        )


def _assert_each_picture_on_its_word(windows, *, keep=KEEP):
    """The kit's acceptance (``tools/measure.py``): each picture is on screen within 1.5 s of its
    word, and it does not first appear later than that; the decoy never shows."""
    for media, word in ON_WORD.items():
        at = _word_output_s(word, keep=keep)
        own = [(s, e) for m, s, e in windows if m == media]
        assert own, f"{NAMES[media]} never shows: {_describe(windows)}"
        assert any(s <= at + TOLERANCE_S and e >= at - TOLERANCE_S for s, e in own), (
            f"{NAMES[media]} is not on screen near its word {word!r} ({at:.2f}): "
            f"{_describe(windows)}"
        )
        assert min(s for s, _e in own) <= at + TOLERANCE_S, _describe(windows)
    assert not any(m in NEVER for m, _s, _e in windows)


# ------------------------------------------------------------------ the incident, reproduced


def test_the_incident_layout_holds_the_drone_photo_fifteen_seconds():
    """Control: without the asked pace the layout is S57's (each picture once), as prod drew it."""
    layout, windows = _lay_out(keep_pace=False)

    longest = max(windows, key=lambda w: w[2] - w[1])
    assert longest[0] == DRONE, _describe(windows)
    assert longest[2] - longest[1] == pytest.approx(15.0, abs=0.3), _describe(windows)
    assert len({m for m, _s, _e in windows}) == len(windows), "S57: no picture twice"


def test_an_asked_four_second_pace_survives_the_strategy_dump():
    """E1: "3-5 seconds" is recorded as 4 s, which is also the default, and the dump dropped it."""
    background = {**STORED, "cadence_s": CADENCE_S}
    dumped = CreativeStrategy.model_validate(
        {
            "direction": "native",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "target_duration_s": 26,
            "selected_media_ids": ["analysis-proxy-ios-take.mp4"],
            "rationale": "Green screen.",
            "background_media": background,
        }
    ).model_dump(mode="json", exclude_none=True)["background_media"]
    assert dumped["cadence_s"] == CADENCE_S
    # E1 / negative control: no pace named stays absent, so nothing new is recorded or said.
    assert "cadence_s" not in BackgroundMedia(media_id=STORED["media_id"]).model_dump(mode="json")


# ------------------------------------------------------------------ the asked pace is kept


def test_an_asked_pace_keeps_every_picture_within_it():
    """E2-E9: every window within 5 s, every picture first on its own word, returns only of
    pictures already shown, never the same picture twice in a row, the lane still valid."""
    _layout, windows = _lay_out(keep_pace=True)

    assert all(end - start <= PACE_LIMIT_S + 1e-6 for _m, start, end in windows), _describe(windows)
    _assert_each_picture_on_its_word(windows)
    _assert_lane_valid(windows)
    first_start = {}
    for media, start, _end in windows:
        first_start.setdefault(media, start)
    for media, start, _end in windows:
        assert start >= first_start[media] - 1e-6, "E3: a picture before its own word"
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier[0] != later[0], f"E5: the same picture twice in a row: {_describe(windows)}"


def test_the_drone_photo_stays_through_its_own_sentence():
    """E4: a return never starts while the creator still talks about the current picture."""
    _layout, windows = _lay_out(keep_pace=True)

    sentence_end = _word_output_s("drones", last=True)
    first_drone = next(w for w in windows if w[0] == DRONE)
    assert first_drone[2] >= sentence_end - 1e-6, _describe(windows)
    # The talk after it ("cold hardware ... what did you think") changes picture again.
    after = [w for w in windows if w[1] >= first_drone[2] - 1e-6]
    assert len(after) >= 2, _describe(windows)


def test_without_a_named_pace_nothing_repeats():
    """E1 / S57 unchanged: the 4 s default is not an ask, so a longer scene beats a repeat."""
    _layout, windows = _lay_out(keep_pace=False, cadence_s=CADENCE_S)

    assert len({m for m, _s, _e in windows}) == len(windows)


def test_a_long_talk_stays_within_the_phone_window_budget():
    """E9: past 40 windows a hold stays long instead of breaking the lane."""
    words = [
        {"text": "Kervan.", "start_s": 0.0, "end_s": 0.4},
        {"text": "Lumo.", "start_s": 2.0, "end_s": 2.4},
        *({"text": "talk.", "start_s": 3.0 + i, "end_s": 3.4 + i} for i in range(0, 290, 2)),
    ]
    layout = seq.lay_out(
        [
            seq.SequenceEntry(index=0, spoken_phrase="Kervan", media_key="a"),
            seq.SequenceEntry(index=1, spoken_phrase="Lumo", media_key="b"),
        ],
        cadence_s=CADENCE_S,
        take_s=300.0,
        words=words,
        keep_segments=None,
        fill_key="a",
        keep_pace=True,
    )
    windows = [("ab"[w.index], w.start_s, w.end_s) for w in layout.windows]
    _assert_lane_valid(windows, take_s=300.0)


# ------------------------------------------------------------------ the real worker + receipt

ASK = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description=(
        "Make the whole video like a green screen: me small at the bottom right in front, and "
        "behind me full screen whichever photo or video I'm talking about, changing every 3-5 "
        "seconds."
    ),
    facts={"speaker_background": True, "background_changes": True, "speaker_small": True},
)


def _pool_rows():
    rows = []
    for name, visual in KIT.items():
        rows.append(
            {
                "id": visual["pool_id"],
                "gcs_path": f"users/u1/plan/item/pool/{visual['pool_id']}.jpg",
                "gcs_generation": "7",
                "kind": visual["kind"],
                "source_filename": f"{name}.jpg",
                "duration_s": None,
                "aspect": 0.5625,
                "user_context": "",
                "analysis": {},
                "capture": None,
            }
        )
    return rows


def _cigdem2_job(monkeypatch, background):
    """The Green 3 harness (`test_speaker_background_on_the_word._green3_job`) on this run's
    pool and its CLEANED take: the harness cannot run the consented speech cut, so it is given
    the take as the viewer hears it (the run's words in output seconds, 25.47 s). The cut's own
    source-time mapping is covered by the `lay_out` tests above with the real keep segments."""
    import app.services.phone_overlay_grounding as ogm
    import app.services.phone_reaction_grounding as rgm
    import app.services.phone_visuals as phone_visuals_mod
    from app.pipeline.transcribe import Word
    from app.services.phone_sources import PhoneVisualBinding
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

    rows = {row["id"]: row for row in _pool_rows()}

    def bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001
        return tuple(
            PhoneVisualBinding(
                media_id=media,
                gcs_path=rows[media]["gcs_path"],
                generation="7",
                sha256="a" * 64,
                byte_count=100,
                kind=rows[media]["kind"],
            )
            for media in pins
        )

    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch,
        transcript_words=[
            Word(text=item["text"], start_s=item["start_s"], end_s=item["end_s"], confidence=1.0)
            for item in _kept_words()
        ],
        duration_s=TAKE_S,
    )
    _enable_beats(monkeypatch, extra_features=("visualVideos", "authoredText"))
    monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
        ],
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "background_media": background,
    }
    return job


def _stored_with_pace():
    """The planner's background as production stores it once the pace survives the dump."""
    strategy = CreativeStrategy.model_validate(
        {
            "direction": "native",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "target_duration_s": 26,
            "selected_media_ids": ["analysis-proxy-ios-take.mp4"],
            "rationale": "Green screen.",
            "background_media": {**copy.deepcopy(STORED), "cadence_s": CADENCE_S},
        }
    )
    return strategy.model_dump(mode="json", exclude_none=True)["background_media"]


def _run(monkeypatch, *, pace_on: bool):
    monkeypatch.setattr(settings, "background_keeps_asked_pace_enabled", pace_on)
    job = _cigdem2_job(monkeypatch, _stored_with_pace())
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    (receipt,) = build_receipts(
        [ASK], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    layer = _recipe(job).speaker_background
    windows = [(w.asset_id.removeprefix("visual-"), w.start, w.end) for w in layer.windows]
    return variant["phone_lane_receipt"]["background"], receipt, windows


def test_the_worker_keeps_the_asked_pace_and_the_review_says_done(monkeypatch):
    lane, receipt, windows = _run(monkeypatch, pace_on=True)

    assert lane["cadence_s"] == CADENCE_S
    assert lane["longest_s"] <= PACE_LIMIT_S + 1e-6, lane
    assert lane["media"] == 4, "returns never inflate the picture count (KRI-1177)"
    assert all(end - start <= PACE_LIMIT_S + 1e-6 for _m, start, end in windows), _describe(windows)
    _assert_each_picture_on_its_word(windows)
    assert receipt.status == "met", receipt.reason
    assert "One stays on" not in (receipt.reason or "")


def test_the_kill_switch_keeps_one_long_hold_and_says_so(monkeypatch):
    """E13: flag off, the layout is S57's; the pace now reaches the receipt, so the review says
    the drone photo stays longer than asked (#2230's sentence) instead of "met"."""
    lane, receipt, windows = _run(monkeypatch, pace_on=False)

    assert lane["cadence_s"] == CADENCE_S
    assert lane["longest_s"] > PACE_LIMIT_S
    assert len({m for m, _s, _e in windows}) == len(windows)
    assert receipt.status == "partial"
    assert "One stays on for" in receipt.reason
    assert "you asked for a change about every 4 s" in receipt.reason


def test_a_hold_nothing_could_break_is_told_truthfully():
    """E7 / E14: with the pace kept, a long hold that remains is a picture nothing came before,
    so the reply never claims "none repeats"."""
    variant = {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "caption_cues": [{"text": "kervan", "start_s": 0.0, "end_s": 1.0}],
        "text_elements": [],
        "phone_lane_receipt": {
            "applied": ["background"],
            "dropped": [],
            "background": {
                "windows": 6,
                "media": 3,
                "speaker_frame": "small_bottom_right",
                "cadence_s": 4.0,
                "longest_s": 7.2,
                "pace_kept": True,
            },
        },
    }
    (receipt,) = build_receipts(
        [ASK], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "partial"
    assert "One stays on for 7 s" in receipt.reason
    assert "none repeats" not in receipt.reason
    assert "while you're still talking about it" in receipt.reason
