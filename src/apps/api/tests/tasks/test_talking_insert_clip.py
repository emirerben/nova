"""Talking edit: a clip the creator asked to INSERT after something he says pauses his take.

Replays prod thread 6fa36e51 / job 8cb3fc21 (2026-11-01): the creator's 58 s take, three
podium talks and a drone shot (`talking_show_when_mentioned_6fa36e51.json`). The 15 s congress
talk is inserted right after he says "Fener'i Kadıköy'den çıkarmak". Only transport (download,
probe, Whisper) is replaced; the clip classifier, the insert placement, the shift of every lane,
the compiler and recipe validation are the real ones, driven through `_run_generative_job`.
"""

from __future__ import annotations

import pytest

from app.tasks import generative_build as gb
from tests.tasks.test_talking_show_when_mentioned import (
    CLIPS,
    CONGRESS_TALK,
    CREATOR,
    FINAL_TALK,
    STADIUM,
    _run,
    _tracks,
)

PHRASE = "Fener'i Kadıköy'den çıkarmak"
STRATEGY = {"insert_clips": [{"media_id": CONGRESS_TALK, "at_phrase": PHRASE, "keep_audio": True}]}
FLAG = {"phone_talking_insert_clips_enabled": True}
CONGRESS_S = next(c["duration_s"] for c in CLIPS if c["media_id"] == CONGRESS_TALK)
TAKE_S = 57.97
# "çıkarmak." ends at 21.8 s in the creator's words (the next word starts at 21.8).
PHRASE_END_S = 21.8


def _speaker_pieces(recipe):
    return [
        (c.source_start, c.source_start + c.source_duration, c.timeline_start)
        for c in _tracks(recipe)["subtitled"].clips
        if c.source_asset_id == CREATOR
    ]


def test_the_congress_talk_plays_right_after_the_phrase_and_the_take_resumes(monkeypatch):
    _job, recipe, variant = _run(
        monkeypatch, mention=True, strategy=STRATEGY, settings_overrides=FLAG
    )
    main = _tracks(recipe)["subtitled"].clips
    assert [c.source_asset_id for c in main] == [CREATOR, CONGRESS_TALK, CREATOR]
    before, insert, after = main
    # Paused at a natural pause just after the phrase, never mid-word.
    assert PHRASE_END_S - 1e-6 <= before.source_duration <= PHRASE_END_S + 2.5
    assert insert.timeline_start == pytest.approx(before.source_duration)
    assert insert.source_duration == pytest.approx(CONGRESS_S)
    assert insert.volume == 1
    # The take resumes exactly where it stopped, and runs to its end.
    assert after.source_start == pytest.approx(before.source_duration)
    assert after.source_start + after.source_duration == pytest.approx(TAKE_S)
    assert after.timeline_start == pytest.approx(insert.timeline_start + CONGRESS_S)
    # Nothing the creator said is lost: every kept source second still plays, in order.
    assert sum(end - start for start, end, _ in _speaker_pieces(recipe)) == pytest.approx(TAKE_S)
    assert recipe.duration == pytest.approx(TAKE_S + CONGRESS_S)


def test_the_inserted_clip_is_not_also_a_cutaway_and_the_receipt_says_where_it_went(
    monkeypatch,
):
    _job, recipe, variant = _run(
        monkeypatch, mention=True, strategy=STRATEGY, settings_overrides=FLAG
    )
    cutaways = _tracks(recipe)["talking-head-cutaways"].clips
    assert CONGRESS_TALK not in {c.source_asset_id for c in cutaways}
    assert {c.source_asset_id for c in cutaways} == {FINAL_TALK, STADIUM, _other_talk()}
    # No cutaway covers the insert point.
    insert = _tracks(recipe)["subtitled"].clips[1]
    assert all(
        c.timeline_start + c.source_duration <= insert.timeline_start + 1e-6
        or c.timeline_start >= insert.timeline_start + insert.source_duration - 1e-6
        for c in cutaways
    )
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    (row,) = receipt["placed"]
    assert row["media_id"] == CONGRESS_TALK
    assert row["basis"] == "phrase"
    assert row["start_s"] == pytest.approx(insert.timeline_start)
    assert row["end_s"] == pytest.approx(insert.timeline_start + CONGRESS_S)
    assert receipt["dropped"] == []


def test_captions_after_the_insert_shift_by_exactly_its_length(monkeypatch):
    _job, plain_recipe, plain = _run(monkeypatch, mention=True)
    _job, recipe, variant = _run(
        monkeypatch, mention=True, strategy=STRATEGY, settings_overrides=FLAG
    )
    point = _tracks(recipe)["subtitled"].clips[1].timeline_start
    before = {tuple(c["text"].split()): c for c in plain["caption_cues"]}
    moved = 0
    for cue in variant["caption_cues"]:
        original = before.get(tuple(cue["text"].split()))
        if original is None:
            continue
        shift = cue["start_s"] - original["start_s"]
        assert shift == pytest.approx(0.0, abs=1e-3) or shift == pytest.approx(CONGRESS_S, abs=1e-3)
        moved += shift > 1
        if original["start_s"] >= point - 1e-6:
            assert shift == pytest.approx(CONGRESS_S, abs=1e-3)
    assert moved > 0


def test_flag_off_the_recipe_is_byte_identical_and_the_receipt_says_why(monkeypatch):
    _job, plain_recipe, _plain = _run(monkeypatch, mention=True)
    _job, recipe, variant = _run(monkeypatch, mention=True, strategy=STRATEGY)
    assert recipe.model_dump_json() == plain_recipe.model_dump_json()
    assert gb.PHONE_INSERT_CLIPS_FIELD not in variant


def test_a_phrase_the_creator_never_says_inserts_nothing_and_says_so(monkeypatch):
    strategy = {"insert_clips": [{"media_id": CONGRESS_TALK, "at_phrase": "uzay mekiği"}]}
    _job, recipe, variant = _run(
        monkeypatch, mention=True, strategy=strategy, settings_overrides=FLAG
    )
    # Asked for at a moment that never came: not used at all, rather than shown elsewhere.
    assert CONGRESS_TALK not in {c.source_asset_id for t in recipe.tracks for c in t.clips}
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    assert receipt["placed"] == []
    assert receipt["dropped"] == [{"media_id": CONGRESS_TALK, "reason": "phrase_not_found"}]
    assert recipe.duration == pytest.approx(TAKE_S)
    assert [c.source_asset_id for c in _tracks(recipe)["subtitled"].clips] == [CREATOR]


def test_range_is_closed_on_an_edit_with_an_insert(monkeypatch):
    from app.services.phone_talking_range import REASON_MULTI_CLIP, talking_range_reason

    job, _recipe, variant = _run(
        monkeypatch, mention=True, strategy=STRATEGY, settings_overrides=FLAG
    )
    assert talking_range_reason(job, variant) == REASON_MULTI_CLIP


def _other_talk():
    from tests.tasks.test_talking_show_when_mentioned import ALLOCATE_TALK

    return ALLOCATE_TALK


def test_the_phrase_is_found_in_what_he_said_not_in_the_translated_captions(monkeypatch):
    # English captions were asked for: the caption words are a translation, the take is Turkish.
    from types import SimpleNamespace

    from app.pipeline.transcribe import Word

    def _translate(words, **_kw):
        return SimpleNamespace(
            words=[
                Word(text=f"english{i}", start_s=w.start_s, end_s=w.end_s, confidence=1.0)
                for i, w in enumerate(words)
            ],
            language="en",
        )

    monkeypatch.setattr(gb, "_ensure_requested_caption_language", _translate)
    _job, recipe, variant = _run(
        monkeypatch,
        mention=True,
        strategy=STRATEGY,
        settings_overrides=FLAG,
        extra_candidates={"caption_language_request": "en"},
    )
    assert variant["caption_cues"][0]["text"].startswith("english")
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    assert receipt["dropped"] == []
    (row,) = receipt["placed"]
    assert row["basis"] == "phrase"
    assert row["at_take_s"] == pytest.approx(PHRASE_END_S, abs=2.6)


def test_the_receipt_carries_what_the_phone_needs_to_rebuild_the_preview(monkeypatch):
    """The app rebuilds the editor preview from this receipt (never from the recipe), so every
    field it hydrates the paused take from must be there, in the clip's own units."""
    _job, recipe, variant = _run(
        monkeypatch, mention=True, strategy=STRATEGY, settings_overrides=FLAG
    )
    (row,) = variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]
    main = _tracks(recipe)["subtitled"].clips
    before, insert, after = main
    assert row["media_id"] == CONGRESS_TALK
    assert row["at_take_s"] == pytest.approx(before.source_duration, abs=1e-6)
    assert row["duration_s"] == pytest.approx(CONGRESS_S, abs=1e-6)
    assert row["keep_audio"] is True and row["volume"] == 1
    # On the take timeline the point is the seconds of speech before it; on the final one the
    # clip starts at the same second (nothing is inserted before it).
    assert row["start_s"] == pytest.approx(row["at_take_s"], abs=1e-3)
    assert row["end_s"] - row["start_s"] == pytest.approx(row["duration_s"], abs=1e-3)
    assert after.source_start == pytest.approx(row["at_take_s"], abs=1e-6)


def test_a_muted_insert_says_volume_zero_in_the_receipt(monkeypatch):
    strategy = {"insert_clips": [{**STRATEGY["insert_clips"][0], "keep_audio": False}]}
    _job, _recipe, variant = _run(
        monkeypatch, mention=True, strategy=strategy, settings_overrides=FLAG
    )
    (row,) = variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]
    assert row["keep_audio"] is False and row["volume"] == 0
