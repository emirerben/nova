"""Talking edit: "Place them in between my speech ... use their audio" pauses the creator's take for
each talk at the END OF THE SENTENCE that talks about it.

Replays prod thread b61b7673 / job ae77b21e (2026-11-03): the creator's 94.6 s take (161 words
with their punctuation) and the three podium talks plus the drone shot of the stadium
(`kri1151_insert_at_topic_b61b7673.json`, `talking_show_when_mentioned_6fa36e51.json`). That job
overlaid the talks (the take kept running, the creator muted under them) and spread the one
talk no mention matched to an arbitrary second, silenced. The creator asked for the take to
PAUSE. Only transport (download, probe, Whisper) and the model call are replaced: the sentence
check replays a recorded response (hand-authored, not the real model's), and the classifier,
placement, warp, compiler and recipe validation are the real ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import TransientError
from app.kria.brief import BriefRequirement, CreativeBrief
from app.tasks import generative_build as gb
from tests.agents.test_talking_insert_sentence import _Recorded
from tests.tasks.test_talking_show_when_mentioned import (
    ALLOCATE_TALK,
    CLIPS,
    CONGRESS_TALK,
    FINAL_TALK,
    STADIUM,
    _run,
    _tracks,
)

_FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri1151_insert_at_topic_b61b7673.json"
    ).read_text(encoding="utf-8")
)
CREATOR_CLIP = {**_FIXTURE["creator"]}
CREATOR = CREATOR_CLIP["media_id"]
THIS_JOB_CLIPS = [CREATOR_CLIP, *[c for c in CLIPS if c["role"] != "creator"]]
TAKE_S = CREATOR_CLIP["duration_s"]
DURATION = {c["media_id"]: c["duration_s"] for c in CLIPS}
FLAG = {"phone_talking_insert_clips_enabled": True}
# The take's sentence ends the recorded choice names: sentence 5 ends "...teklif olduğunu
# açıkladı." and sentence 6 "... Fenerbahçe'ye verilecekmiş.".
SENTENCE_5_END = 15.2
SENTENCE_6_END = 20.8
STADIUM_SCOPE = "clip:134CDBD9-2781-4407-921B-57D663D2B33B.mp4"


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="style",
                scope="global",
                description="Show the related parts of the talks between my speech",
                facts={"insert_clip": "the talks"},
            ),
            BriefRequirement(
                id="r2",
                kind="style",
                scope=STADIUM_SCOPE,
                description="Place the stadium full screen when I talk",
                facts={"display_mode": "fullscreen"},
            ),
            BriefRequirement(
                id="r3",
                kind="audio",
                scope=STADIUM_SCOPE,
                description="Keep my audio while the stadium is shown",
                facts={"clip_sound": "off"},
            ),
        ],
    )


def _strategy(*media_ids: str) -> dict:
    return {
        "insert_clips": [{"media_id": m, "keep_audio": True} for m in media_ids],
        "selected_media_ids": [CREATOR, STADIUM, *media_ids],
    }


TALKS = (FINAL_TALK, ALLOCATE_TALK, CONGRESS_TALK)


@pytest.fixture
def meaning(monkeypatch):
    """The sentence check answers with the recorded response."""
    client = _Recorded(_FIXTURE["recorded_choice"])
    monkeypatch.setattr("app.services.phone_insert_topic_picker.default_client", lambda: client)
    return client


def _go(monkeypatch, *, strategy=None, **kw):
    return _run(
        monkeypatch,
        mention=True,
        clips=THIS_JOB_CLIPS,
        brief=_brief(),
        strategy=strategy if strategy is not None else _strategy(*TALKS),
        settings_overrides=FLAG,
        **kw,
    )


def _pieces(recipe):
    return [c for c in _tracks(recipe)["subtitled"].clips]


def test_the_talks_pause_the_take_at_the_end_of_the_sentence_that_is_about_them(
    monkeypatch, meaning
):
    _job, recipe, variant = _go(monkeypatch)
    main = _pieces(recipe)
    # The take, the offer talk, the take, the 2032 final talk, the take: a real pause each time.
    assert [c.source_asset_id for c in main] == [
        CREATOR,
        ALLOCATE_TALK,
        CREATOR,
        FINAL_TALK,
        CREATOR,
    ]
    first, allocate, second, final, last = main
    assert first.source_duration == pytest.approx(SENTENCE_5_END, abs=1e-3)
    assert allocate.timeline_start == pytest.approx(SENTENCE_5_END, abs=1e-3)
    assert allocate.source_duration == pytest.approx(DURATION[ALLOCATE_TALK])
    assert allocate.volume == 1
    assert second.source_start == pytest.approx(SENTENCE_5_END, abs=1e-3)
    assert second.source_start + second.source_duration == pytest.approx(SENTENCE_6_END, abs=1e-3)
    assert final.source_duration == pytest.approx(DURATION[FINAL_TALK])
    assert final.volume == 1
    assert last.source_start == pytest.approx(SENTENCE_6_END, abs=1e-3)
    # Nothing he said is lost and the video is the take plus both talks.
    assert sum(c.source_duration for c in (first, second, last)) == pytest.approx(TAKE_S)
    assert recipe.duration == pytest.approx(TAKE_S + DURATION[ALLOCATE_TALK] + DURATION[FINAL_TALK])


def test_the_receipt_says_how_each_clip_was_placed_and_why_the_congress_talk_was_left_out(
    monkeypatch, meaning
):
    _job, recipe, variant = _go(monkeypatch)
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    placed = {row["media_id"]: row for row in receipt["placed"]}
    assert set(placed) == {ALLOCATE_TALK, FINAL_TALK}
    for media_id, sentence in ((ALLOCATE_TALK, 5), (FINAL_TALK, 6)):
        row = placed[media_id]
        assert (row["basis"], row["snap"], row["via"]) == ("topic", "sentence_end", "meaning")
        assert (row["sentence_first"], row["sentence_last"]) == (sentence, sentence)
        assert row["keep_audio"] is True
    # Pause times on the final timeline: the second talk starts after the first has played.
    assert placed[FINAL_TALK]["start_s"] == pytest.approx(
        SENTENCE_6_END + DURATION[ALLOCATE_TALK], abs=1e-3
    )
    # The talk no sentence is about is not placed anywhere, and the receipt says so.
    assert receipt["dropped"] == [{"media_id": CONGRESS_TALK, "reason": "no_matching_sentence"}]
    assert CONGRESS_TALK not in {c.source_asset_id for t in recipe.tracks for c in t.clips}


def test_captions_after_each_pause_move_by_exactly_the_clips_before_them(monkeypatch, meaning):
    _job, _plain_recipe, plain = _go(monkeypatch, strategy={})
    _job, _recipe, variant = _go(monkeypatch)
    before = {tuple(c["text"].split()): c for c in plain["caption_cues"]}
    seen = set()
    for cue in variant["caption_cues"]:
        original = before.get(tuple(cue["text"].split()))
        if original is None:
            continue
        shift = round(cue["start_s"] - original["start_s"], 3)
        if original["start_s"] >= SENTENCE_6_END - 1e-3:
            expected = DURATION[ALLOCATE_TALK] + DURATION[FINAL_TALK]
        elif original["start_s"] >= SENTENCE_5_END - 1e-3:
            expected = DURATION[ALLOCATE_TALK]
        else:
            expected = 0.0
        assert shift == pytest.approx(expected, abs=2e-3), cue["text"]
        seen.add(expected)
    assert seen == {0.0, DURATION[ALLOCATE_TALK], DURATION[ALLOCATE_TALK] + DURATION[FINAL_TALK]}


def test_the_stadium_stays_an_overlay_over_his_voice_and_never_covers_a_pause(monkeypatch, meaning):
    _job, recipe, _variant = _go(monkeypatch)
    cutaways = _tracks(recipe)["talking-head-cutaways"].clips
    assert {c.source_asset_id for c in cutaways} == {STADIUM}
    inserts = [
        (c.timeline_start, c.timeline_start + c.source_duration)
        for c in _pieces(recipe)
        if c.source_asset_id in (ALLOCATE_TALK, FINAL_TALK)
    ]
    for cutaway in cutaways:
        start, end = cutaway.timeline_start, cutaway.timeline_start + cutaway.source_duration
        assert all(end <= a + 1e-6 or start >= b - 1e-6 for a, b in inserts)
    # He keeps talking under the stadium: his voice is never muted and the clip is silent.
    assert recipe.audio.mute_windows == []
    assert _tracks(recipe).get("talking-head-cutaway-audio") is None
    # His voice is absent only where a talk plays: the take has no piece inside a pause.
    for piece in (c for c in _pieces(recipe) if c.source_asset_id == CREATOR):
        start, end = piece.timeline_start, piece.timeline_start + piece.source_duration
        assert all(end <= a + 1e-6 or start >= b - 1e-6 for a, b in inserts)


def test_the_talks_are_not_also_cutaways(monkeypatch, meaning):
    _job, recipe, _variant = _go(monkeypatch)
    cutaways = {c.source_asset_id for c in _tracks(recipe)["talking-head-cutaways"].clips}
    assert cutaways.isdisjoint(TALKS)


def test_one_model_call_covers_all_the_talks_and_sees_their_facts_and_his_sentences(
    monkeypatch, meaning
):
    _go(monkeypatch)
    (prompt,) = meaning.prompts
    assert "Euro2032 için Maltepe'ye yapılması planlanan stat" in prompt
    assert "allocate a stadium" in prompt
    assert "2032 final" in prompt


def test_when_the_meaning_check_is_down_keywords_choose_the_sentence_and_the_receipt_says_so(
    monkeypatch,
):
    client = _Recorded(TransientError("provider down"))
    monkeypatch.setattr("app.services.phone_insert_topic_picker.default_client", lambda: client)
    _job, recipe, variant = _go(monkeypatch)
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    placed = {row["media_id"]: row for row in receipt["placed"]}
    # The final talk shares "Euro2032", "stat" with the sentence ending at 20.8: the pause is the
    # sentence end, not the word.
    assert placed[FINAL_TALK]["via"] == "keyword"
    assert placed[FINAL_TALK]["sentence_last"] == 6
    final = next(c for c in _pieces(recipe) if c.source_asset_id == FINAL_TALK)
    assert final.timeline_start == pytest.approx(SENTENCE_6_END, abs=1e-3)
    # No keyword found the offer talk: it is left out and the reason is that the check could not
    # run, not "nothing is about it".
    assert {"media_id": ALLOCATE_TALK, "reason": "topic_check_unavailable"} in receipt["dropped"]


def test_flag_off_the_recipe_is_byte_identical_and_the_receipt_is_absent(monkeypatch, meaning):
    _j, plain_recipe, _plain = _run(
        monkeypatch, mention=True, clips=THIS_JOB_CLIPS, brief=_brief(), strategy={}
    )
    _j, recipe, variant = _run(
        monkeypatch, mention=True, clips=THIS_JOB_CLIPS, brief=_brief(), strategy=_strategy(*TALKS)
    )
    assert recipe.model_dump_json() == plain_recipe.model_dump_json()
    assert gb.PHONE_INSERT_CLIPS_FIELD not in variant
    assert meaning.prompts == []


def test_the_creators_own_take_named_as_a_talk_is_refused_and_no_model_call_is_spent(
    monkeypatch, meaning
):
    _job, recipe, variant = _go(monkeypatch, strategy=_strategy(CREATOR))
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    assert receipt["placed"] == []
    assert receipt["dropped"] == [{"media_id": CREATOR, "reason": "clip_in_use"}]
    assert [c.source_asset_id for c in _pieces(recipe)] == [CREATOR]
    assert meaning.prompts == []


def test_a_talk_also_asked_as_an_overlay_plays_once_as_an_insert(monkeypatch, meaning):
    overlay = BriefRequirement(
        id="r9",
        kind="style",
        scope="global",
        description="Show the talks full screen",
        facts={"display_mode": "fullscreen"},
    )
    brief = CreativeBrief(version=2, requirements=[*_brief().requirements, overlay])
    _job, recipe, _variant = _run(
        monkeypatch,
        mention=True,
        clips=THIS_JOB_CLIPS,
        brief=brief,
        strategy=_strategy(*TALKS),
        settings_overrides=FLAG,
    )
    ids = [c.source_asset_id for t in recipe.tracks for c in t.clips]
    assert ids.count(ALLOCATE_TALK) == 1
    assert ids.count(FINAL_TALK) == 1
