"""Talking edit: "Place them in between my speech ... use their audio" pauses the creator's take for
each talk at the END OF THE SENTENCE that talks about it.

Replays prod thread b61b7673 / job ae77b21e (2026-11-03): the creator's 94.6 s take (161 words
with their punctuation) and the three podium talks plus the drone shot of the stadium
(`kri1151_insert_at_topic_b61b7673.json`, `talking_show_when_mentioned_6fa36e51.json`). That job
overlaid the talks (the take kept running, the creator muted under them) and spread the one
talk no mention matched to an arbitrary second, silenced. The creator asked for the take to
PAUSE. Only transport (download, probe, Whisper) and the model call are replaced: the sentence
check replays a HAND-AUTHORED response (never a recording of the real model: no live eval of
this check exists), and the classifier,
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
# The take's sentence ends the hand-authored choice names: sentence 5 ends "...teklif olduğunu
# açıkladı." and sentence 6 "... Fenerbahçe'ye verilecekmiş.".
# The pause is the end of the sentence (15.2 / 20.8) held 0.2 s into the silence after it.
SENTENCE_5_END = 15.4
SENTENCE_6_END = 21.0
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
    """The sentence check answers with the hand-authored response (a contract fixture)."""
    client = _Recorded(_FIXTURE["hand_authored_choice"])
    monkeypatch.setattr("app.services.phone_insert_topic_picker.default_client", lambda: client)
    return client


@pytest.fixture
def surely_nothing(monkeypatch):
    """CONTROL: the check is, surely, that nothing he says is about the congress talk."""
    client = _Recorded(_FIXTURE["hand_authored_abstain_control"])
    monkeypatch.setattr("app.services.phone_insert_topic_picker.default_client", lambda: client)
    return client


def _go(monkeypatch, *, strategy=None, brief=None, **kw):
    return _run(
        monkeypatch,
        mention=True,
        clips=THIS_JOB_CLIPS,
        brief=brief or _brief(),
        strategy=strategy if strategy is not None else _strategy(*TALKS),
        settings_overrides=FLAG,
        **kw,
    )


def _pieces(recipe):
    return [c for c in _tracks(recipe)["subtitled"].clips]


def test_the_talks_pause_the_take_at_the_end_of_the_sentence_that_is_about_them(
    monkeypatch, surely_nothing
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


def test_the_receipt_says_how_each_clip_was_placed(monkeypatch, meaning):
    _job, recipe, variant = _go(monkeypatch)
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    placed = {row["media_id"]: row for row in receipt["placed"]}
    assert set(placed) == set(TALKS)
    for media_id, sentence in ((ALLOCATE_TALK, 5), (FINAL_TALK, 6), (CONGRESS_TALK, 16)):
        row = placed[media_id]
        assert (row["basis"], row["snap"], row["via"]) == ("topic", "sentence_end", "meaning")
        assert (row["sentence_first"], row["sentence_last"]) == (sentence, sentence)
        assert row["keep_audio"] is True
    # Pause times on the final timeline: the second talk starts after the first has played.
    assert placed[FINAL_TALK]["start_s"] == pytest.approx(
        SENTENCE_6_END + DURATION[ALLOCATE_TALK], abs=1e-3
    )
    assert receipt["dropped"] == []
    assert {c.source_asset_id for c in _pieces(recipe)} >= set(TALKS)


def test_a_clip_the_check_is_surely_nothing_about_is_left_out_named_with_the_nearest_sentence(
    monkeypatch, surely_nothing
):
    """CONTROL on a hand-authored verdict: the leave-out path, not a claim about the content."""
    _job, recipe, variant = _go(monkeypatch)
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    assert {row["media_id"] for row in receipt["placed"]} == {ALLOCATE_TALK, FINAL_TALK}
    (dropped,) = receipt["dropped"]
    assert (dropped["media_id"], dropped["reason"]) == (CONGRESS_TALK, "no_matching_sentence")
    assert CONGRESS_TALK not in {c.source_asset_id for t in recipe.tracks for c in t.clips}


def test_captions_after_each_pause_move_by_exactly_the_clips_before_them(
    monkeypatch, surely_nothing
):
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


def test_the_stadium_stays_an_overlay_over_his_voice_and_never_covers_a_pause(
    monkeypatch, surely_nothing
):
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
    from app.pipeline.phone_talking_insert_topics import split_sentences

    client = _Recorded(TransientError("provider down"))
    monkeypatch.setattr("app.services.phone_insert_topic_picker.default_client", lambda: client)
    _job, recipe, variant = _go(monkeypatch)
    receipt = variant[gb.PHONE_INSERT_CLIPS_FIELD]
    sentences = split_sentences(
        [{"text": t, "start_s": a, "end_s": b} for t, a, b in CREATOR_CLIP["words"]], TAKE_S
    )
    placed = {row["media_id"]: row for row in receipt["placed"]}
    row = placed[FINAL_TALK]
    # Labelled as a keyword guess, and the pause is still a SENTENCE end, never a matched word.
    assert row["via"] == "keyword"
    assert row["snap"] == "sentence_end"
    assert row["at_take_s"] == pytest.approx(sentences[row["sentence_last"] - 1].end_s, abs=1e-3)
    # Nothing is left out because the check could not run: the two talks no keyword finds are
    # placed at a labelled guess, and the receipt says which and why.
    assert receipt["dropped"] == []
    assert set(placed) == set(TALKS)
    assert (row["via"], row["why"]) == ("keyword", "check_unavailable")
    for media_id in (ALLOCATE_TALK, CONGRESS_TALK):
        assert placed[media_id]["via"] in ("keyword", "guess")
        assert placed[media_id]["why"] == "check_unavailable"
        assert placed[media_id]["snap"] == "sentence_end"


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
    # He stays the one take of the video: first, and played once, not paused for himself.
    ids = [c.source_asset_id for c in _pieces(recipe)]
    assert ids[0] == CREATOR and ids.count(CREATOR) == 1
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


# --- an explicit position wins over every default ---


def _overlay_the_rest() -> CreativeBrief:
    """The talks no insert row names are shown over his speech, so the main track is his take."""
    overlay = BriefRequirement(
        id="r9",
        kind="style",
        scope="global",
        description="Show the other talks full screen while I talk",
        facts={"display_mode": "fullscreen"},
    )
    return CreativeBrief(version=2, requirements=[*_brief().requirements, overlay])


def _explicit(*rows: dict) -> dict:
    return {
        "insert_clips": [{"keep_audio": True, **row} for row in rows],
        "selected_media_ids": [CREATOR, STADIUM, *TALKS],
    }


def _word(text: str) -> tuple[float, float]:
    return next((a, b) for t, a, b in CREATOR_CLIP["words"] if t == text)


def test_a_keyword_mid_sentence_the_creator_named_is_cut_right_after_that_word(monkeypatch):
    # "Euro2032" is the 2nd word of a sentence that ends at 20.8; he said "after Euro2032".
    _job, recipe, variant = _go(
        monkeypatch,
        brief=_overlay_the_rest(),
        strategy=_explicit({"media_id": FINAL_TALK, "at_phrase": "Euro2032"}),
    )
    _start, word_end = _word("Euro2032")
    final = next(c for c in _pieces(recipe) if c.source_asset_id == FINAL_TALK)
    assert final.timeline_start == pytest.approx(word_end, abs=1e-3)
    assert word_end < SENTENCE_6_END - 1.0
    (row,) = variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]
    assert (row["basis"], row["snap"], row["as_asked"]) == ("phrase", "phrase_end", True)
    assert "adjusted" not in row


def test_an_insert_asked_for_in_the_first_second_is_placed_there_and_the_receipt_says_so(
    monkeypatch,
):
    _job, recipe, variant = _go(
        monkeypatch,
        brief=_overlay_the_rest(),
        strategy=_explicit({"media_id": FINAL_TALK, "at_s": 0.6}),
    )
    main = _pieces(recipe)
    assert [c.source_asset_id for c in main] == [CREATOR, FINAL_TALK, CREATOR]
    assert main[0].source_duration == pytest.approx(0.6, abs=1e-3)
    (row,) = variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]
    assert row["overrode"] == ["hook_window"] and row["as_asked"] is True
    assert row["at_take_s"] == pytest.approx(0.6, abs=1e-3)


def test_an_insert_asked_for_at_the_very_start_plays_before_the_take(monkeypatch):
    _job, recipe, _variant = _go(
        monkeypatch,
        brief=_overlay_the_rest(),
        strategy=_explicit({"media_id": FINAL_TALK, "at_s": 0}),
    )
    assert [c.source_asset_id for c in _pieces(recipe)] == [FINAL_TALK, CREATOR]
    assert recipe.duration == pytest.approx(TAKE_S + DURATION[FINAL_TALK])


def test_two_inserts_asked_for_next_to_each_other_both_play(monkeypatch):
    _job, recipe, variant = _go(
        monkeypatch,
        brief=_overlay_the_rest(),
        strategy=_explicit(
            {"media_id": FINAL_TALK, "at_s": 30.0}, {"media_id": ALLOCATE_TALK, "at_s": 31.0}
        ),
    )
    assert [c.source_asset_id for c in _pieces(recipe)] == [
        CREATOR,
        FINAL_TALK,
        CREATOR,
        ALLOCATE_TALK,
        CREATOR,
    ]
    rows = {r["media_id"]: r for r in variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]}
    assert rows[ALLOCATE_TALK]["overrode"] == ["near_another_clip"]
    assert variant[gb.PHONE_INSERT_CLIPS_FIELD]["dropped"] == []


def test_a_fourth_insert_is_played_as_asked(monkeypatch):
    clips = (FINAL_TALK, ALLOCATE_TALK, CONGRESS_TALK, STADIUM)
    _job, recipe, variant = _go(
        monkeypatch,
        brief=_overlay_the_rest(),
        strategy=_explicit(*({"media_id": m, "at_s": 20.0 + 15 * k} for k, m in enumerate(clips))),
    )
    played = [c.source_asset_id for c in _pieces(recipe) if c.source_asset_id != CREATOR]
    assert played == list(clips)
    assert variant[gb.PHONE_INSERT_CLIPS_FIELD]["dropped"] == []
    total = TAKE_S + sum(DURATION[m] for m in clips)
    assert recipe.duration == pytest.approx(total)


def test_a_seventy_second_clip_is_played_whole(monkeypatch):
    long_clips = [
        {**c, "duration_s": 70.0} if c["media_id"] == ALLOCATE_TALK else c for c in THIS_JOB_CLIPS
    ]
    _job, recipe, variant = _run(
        monkeypatch,
        mention=True,
        clips=long_clips,
        brief=_overlay_the_rest(),
        strategy=_explicit({"media_id": ALLOCATE_TALK, "at_s": 40.0}),
        settings_overrides=FLAG,
    )
    insert = next(c for c in _pieces(recipe) if c.source_asset_id == ALLOCATE_TALK)
    assert insert.source_duration == pytest.approx(70.0)
    assert recipe.duration == pytest.approx(TAKE_S + 70.0)
    assert variant[gb.PHONE_INSERT_CLIPS_FIELD]["dropped"] == []


def test_a_phrase_he_never_says_is_placed_where_he_talks_about_the_clip_and_the_receipt_says_so(
    monkeypatch, meaning
):
    _job, recipe, variant = _go(
        monkeypatch,
        brief=_overlay_the_rest(),
        strategy=_explicit({"media_id": FINAL_TALK, "at_phrase": "uzay mekiği"}),
    )
    (row,) = variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]
    assert row["fallback"] == "phrase_not_found" and row["basis"] == "topic"
    final = next(c for c in _pieces(recipe) if c.source_asset_id == FINAL_TALK)
    assert final.timeline_start == pytest.approx(SENTENCE_6_END, abs=1e-3)


def test_the_hook_default_applies_only_where_he_named_nothing(monkeypatch, meaning):
    # The hand-authored choice puts the offer talk after sentence 5 and the final talk after
    # sentence 6: far from the hook, so neither is adjusted.
    _job, _recipe, variant = _go(monkeypatch)
    assert all(
        "adjusted" not in r and "overrode" not in r
        for r in variant[gb.PHONE_INSERT_CLIPS_FIELD]["placed"]
    )


# --- a retry of the same render delivery keeps the first attempt's meaning answer ------------


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def get(self, key):
        return self.rows.get(key)

    def setex(self, key, ttl, value):
        self.rows[key] = value


def _facts():
    from app.agents.talking_insert_sentence import SentenceClip

    return {
        m: SentenceClip(key="c0", summary=f"a talk about {m}", transcript="")
        for m in (FINAL_TALK, ALLOCATE_TALK, CONGRESS_TALK)
    }


def _resolve(retries):
    from app.pipeline.phone_talking_inserts import InsertAsk

    token = gb._TASK_RETRIES.set(retries)
    try:
        return gb._resolve_insert_topics(
            [InsertAsk(m, at_topic=True) for m in (FINAL_TALK, ALLOCATE_TALK, CONGRESS_TALK)],
            spoken_words=[
                {"text": t, "start_s": a, "end_s": b} for t, a, b in CREATOR_CLIP["words"]
            ],
            take_s=TAKE_S,
            in_use=(),
            clip_facts=_facts(),
            language="tr",
            job_id="job-retry",
        )
    finally:
        gb._TASK_RETRIES.reset(token)


def test_a_retried_render_reuses_the_first_attempts_answer_instead_of_falling_to_keywords(
    monkeypatch,
):
    shared = _FakeStore()
    monkeypatch.setattr("app.services.insert_pick_checkpoint._redis", lambda: shared)
    first_client = _Recorded(_FIXTURE["hand_authored_choice"])
    monkeypatch.setattr(
        "app.services.phone_insert_topic_picker.default_client", lambda: first_client
    )
    _asks, drops, info = _resolve(retries=0)
    assert drops == []
    assert {row["via"] for row in info.values()} == {"meaning"}
    # The retry of the same delivery: the provider would now refuse the identical call, but the
    # first answer was kept, so the clips are still placed by meaning and nothing is paid.
    dead = _Recorded(TransientError("the ledger refuses a replay"))
    monkeypatch.setattr("app.services.phone_insert_topic_picker.default_client", lambda: dead)
    _asks, drops, info = _resolve(retries=1)
    assert drops == []
    assert {row["via"] for row in info.values()} == {"meaning"}
    assert dead.prompts == []
