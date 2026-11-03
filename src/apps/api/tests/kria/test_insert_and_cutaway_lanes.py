"""The two Talking planner lanes agree: full-screen cutaways (#2057) and the pausing insert (#2058).

Both landed under parent KRI-1151 and both teach the Main Creator through the same
`$reaction_beats_section` slot. Merged naively they contradict each other: the cutaway section
said "a clip that stops the talk and lets it continue afterwards does not exist on iPhone" while
the insert section taught exactly that; the cutaway opening forbade `ask_user` while the routing
between the lanes needs one question; the ask-refusal note told the model a pause is not
available. Each lane is open on its OWN gate (`talking_footage_cutaways`, `insert_clip_candidates`).

Failure modes, written before the code (each is pinned below):

 L1 a lane's text depends on the other lane's gate when it should not: a cutaway-only or a closed
    prompt is not main's bytes (pinned by sha of what origin/main renders for the same input).
 L2 both open: the cutaway section still says an insert does not exist.
 L3 both open: the opening paragraph forbids the one question the routing needs.
 L4 cutaway section before the insert section (slot order), insert-only text points at a lane
    that is not in the prompt.
 L5 routing: "show it when I mention X (use their audio)" is a cutaway, only an explicit
    pause / stop / "while I wait" is an insert.
 L6 the corrective re-run note tells the model a pause is unavailable while the insert lane is
    open; the guard fires on an insert / pause refusal, or misses an audio refusal that merely
    names a pause as the alternative while the insert lane is open.

Re-review of #2058 (each pinned below):

 M1 the brief section sends a clip shown while the talk keeps running to `triggers`, but the
    cutaway lane reads only `display_mode` (no `triggers`) and `clip_sound`: the ask is recorded
    where the worker cannot see it.
 M2 the only "ask once" example nearly paraphrases the prod request, so it may get a question
    instead of the cutaway; "in between my speech" with a mention anchor is never named as the
    cutaway lane.
 M3 the refusal guard: (a) a generic code + an audio refusal that offers a pause is read as a
    pause refusal while the pause exists; (b) the one legitimate clarifying question with a
    refusal word in its code is re-run and replaced by the unmet reply; (c) the insert section
    never says earlier "unavailable" replies are out of date.
 M4 insert lane open with the cutaways closed: the model is told to leave `insert_clips` empty
    and nothing else, so it improvises what is possible.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.agents._schemas.creator_agent import AskUser
from app.config import settings
from app.kria import ask_refusals
from app.kria.ask_refusals import stale_capability_refusal
from app.services import creator_capabilities as capabilities
from tests.kria.test_planner_knows_talking_cutaways import (
    INCIDENT_CODE,
    SECTION_MARK,
    THREAD,
    _brief,
    _flat,
    _gates,
    _manifest,
    _prompt,
    _refusal_output,
    _strategy_output,
    _turn,
)

INSERT_MARK = "A CLIP PLAYED IN THE MIDDLE OF THE TALK"
TAIL_CLOSED_INSERT = "is not available on iPhone"
TAIL_OPEN_INSERT = "exists only as `insert_clips`"

# sha256 of what origin/main (d58637f63 + #2055/#2059, creator v109) renders for the same input
# with the manifest line removed (its item id and capability order are random per run): the
# closed-gate prompt of this branch must not drift from main's bytes.
# Re-pinned for KRI-1162 (PR #2045): the brief section always teaches the `speaker_background`
# requirement fact (KRI-1161 honesty); removing that one sentence reproduces the values above.
# Re-pinned for KRI-1177 / KRI-1164 (creator v113): the brief section also teaches the caption
# look facts and `audio_intent: no_music`; removing exactly those two passages reproduces the
# values above.
# Re-pinned for KRI-1189 (creator v117): the brief section's cleanup sentence teaches
# `cut_restarts`; restoring v116's sentence reproduces 87126862 / c9d68e98 / e83242e4 / 00392b4c.
MAIN_PROMPT_SHA = {
    "neither": "e442b680d967898d3bd39de0b9e49df9fb0fb9419b7dd648c9253ba10b84ae58",
    (True, True): "1004a3917e7d0e8bf8ce535b3b391a72040808954e7fad2459fb152e737a063e",
    (True, False): "e11a0e9b09d3c653420b45658bfda246e0f15ac5fe5cc30b11f8ac97e0212062",
    (False, True): "b41741e380534631a6f82005f93084634af3b26bd9635a5bd0c9f99738a98aef",
}
MAIN_NOTE_SHA = {
    ask_refusals.CAPABILITY_MENTION: (
        "303f73f4ad537b820bf2f87e36cfea5180a89832a5c91f630ea826fa9d77cac1"
    ),
    ask_refusals.CAPABILITY_SOUND: (
        "729eca09193829c1789209e5114bfa3e78acb718ccc63e391bdfa56f1dca77e7"
    ),
}


def _lanes(monkeypatch, *, cutaways: bool, inserts: bool, mention: bool = True, sound: bool = True):
    _gates(monkeypatch, mention=mention and cutaways, sound=sound and cutaways)
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", inserts)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        sorted({*settings.phone_render_verified_features, "audioMix"}),
    )
    manifest = _manifest()
    assert bool(capabilities.talking_footage_cutaways(manifest)) is cutaways
    assert bool(capabilities.insert_clip_candidates(manifest)) is inserts
    return manifest


def _stable(prompt: str) -> str:
    return "\n".join(line for line in prompt.split("\n") if '"manifest_hash"' not in line)


def _sha(prompt: str) -> str:
    return hashlib.sha256(_stable(prompt).encode()).hexdigest()


# ---------------------------------------------------------------- L1: closed lanes are main's bytes


def test_l1_neither_lane_open_is_the_prompt_main_renders(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=False, inserts=False)
    prompt = _prompt(manifest)

    assert SECTION_MARK not in prompt and INSERT_MARK not in prompt
    assert _sha(prompt) == MAIN_PROMPT_SHA["neither"]


@pytest.mark.parametrize(("mention", "sound"), [(True, True), (True, False), (False, True)])
def test_l1_cutaways_only_is_the_prompt_main_renders(monkeypatch, mention, sound):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=False, mention=mention, sound=sound)
    prompt = _prompt(manifest)

    assert SECTION_MARK in prompt and INSERT_MARK not in prompt
    assert "insert_clip" not in prompt
    assert TAIL_CLOSED_INSERT in _flat(prompt)
    assert _sha(prompt) == MAIN_PROMPT_SHA[(mention, sound)]


# ---------------------------------------------------------------- L2-L4: both open


def test_l2_the_cutaway_section_stops_denying_the_insert_when_it_exists(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))

    assert TAIL_CLOSED_INSERT not in flat
    assert TAIL_OPEN_INSERT in flat
    assert (
        "only when the creator asks the talk to pause or stop, or to put a clip with speech or "
        "sound of its own in between my speech"
    ) in flat


def test_l3_one_clarifying_question_is_allowed_only_when_both_lanes_exist(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))
    assert "the single exception is ONE short question" in flat
    assert "use `ask_user` ONCE" in flat
    # ... and it never reopens the refusal: unsupported / Montage stay forbidden.
    assert "never use `ask_user` to say it is unsupported (" in flat
    assert "never offer a Montage as the answer" in flat

    manifest = _lanes(monkeypatch, cutaways=True, inserts=False)
    closed = _flat(_prompt(manifest))
    assert "single exception" not in closed and "ask_user` ONCE" not in closed


def test_l4_the_cutaway_section_comes_before_the_insert_section(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    prompt = _prompt(manifest)

    assert prompt.count(SECTION_MARK) == 1 and prompt.count(INSERT_MARK) == 1
    assert prompt.index(SECTION_MARK) < prompt.index(INSERT_MARK)
    # ... and the insert section points at a lane that is really there.
    assert "the full-screen clip lane above" in _flat(prompt)


def test_l4_an_insert_only_prompt_points_at_no_lane_that_is_not_in_it(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=False, inserts=True)
    prompt = _prompt(manifest)
    flat = _flat(prompt)

    assert INSERT_MARK in prompt and SECTION_MARK not in prompt
    assert "full-screen clip lane" not in flat and "ask_user` ONCE" not in flat
    assert "leave `insert_clips` empty" in flat


def test_l4_each_lane_is_open_on_its_own_gate(monkeypatch):
    # An insert-only render does not need either cutaway flag, and a cutaway-only render does
    # not need the insert flag. Neither reads the other.
    only_insert = _lanes(monkeypatch, cutaways=False, inserts=True)
    assert capabilities.talking_footage_cutaways(only_insert) is None
    assert capabilities.insert_clip_candidates(only_insert)
    only_cutaways = _lanes(monkeypatch, cutaways=True, inserts=False)
    assert capabilities.insert_clip_candidates(only_cutaways) == []
    assert capabilities.talking_footage_cutaways(only_cutaways) is not None


# ---------------------------------------------------------------- L5: routing


def test_l5_show_it_when_i_mention_it_is_a_cutaway_and_a_pause_or_in_between_is_an_insert(
    monkeypatch,
):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))
    insert = flat[flat.index(INSERT_MARK) :]

    for ask in ("show the congress video when I mention congress", "use their audio"):
        assert ask in insert
    assert "is NOT an insert, with or without the clip's sound" in insert
    assert "the clip shown over the talk" in insert
    assert (
        'Without a pause, a stop, an in-the-middle-while-I-wait wording or "in between my speech"'
    ) in insert
    # The cutaway section keeps the user's prod phrasing on its own side.
    cutaway = flat[flat.index(SECTION_MARK) : flat.index(INSERT_MARK)]
    for ask in ("show the talks when I talk about them", "use their audio"):
        assert ask in cutaway
    assert "does not pause the take" in cutaway


# ---------------------------------------------------------------- L6: the guard and its note


@pytest.mark.parametrize("capability", sorted(MAIN_NOTE_SHA))
def test_l6_the_note_is_main_s_bytes_while_the_insert_lane_is_closed(capability):
    note = ask_refusals.correction_note(capability)
    assert hashlib.sha256(note.encode()).hexdigest() == MAIN_NOTE_SHA[capability]
    assert ask_refusals.correction_note(capability, inserts_open=False) == note


@pytest.mark.parametrize("capability", sorted(MAIN_NOTE_SHA))
def test_l6_the_note_points_a_pause_ask_at_insert_clips_when_that_lane_is_open(capability):
    note = ask_refusals.correction_note(capability, inserts_open=True)

    assert "insert_clips" in note
    assert "it is not available" not in note
    assert "simply shown while the talk keeps running" in note
    for jargon in ("reason_code", "gates", "gate "):
        assert jargon not in note


@pytest.mark.parametrize("inserts", [False, True])
@pytest.mark.parametrize(
    "code",
    [
        "insert_unavailable",
        "insert_not_available",
        "unsupported_pause_and_resume",
        "pause_not_supported",
        "cannot_pause_talk",
        "resume_unavailable",
    ],
)
def test_l6_an_insert_or_pause_refusal_code_is_never_contradicted(monkeypatch, code, inserts):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=inserts)
    ask = AskUser(
        kind="ask_user",
        question="I can't play their audio while your talk is stopped.",
        reason_code=code,
        options=["Overlay it", "Skip the audio"],
    )

    assert stale_capability_refusal(ask, manifest, _brief()) is None


@pytest.mark.parametrize("inserts", [False, True])
@pytest.mark.parametrize(
    "code", ["clarify_pause_or_overlay", "pause_or_cover", "which_lane", "ambiguous_insert"]
)
def test_l6_the_one_clarifying_question_is_not_a_refusal(monkeypatch, code, inserts):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=inserts)
    ask = AskUser(
        kind="ask_user",
        question="Should the talk pause while the clip plays, or should the clip cover it?",
        reason_code=code,
        options=["Pause my talk", "Cover my talk"],
    )

    assert stale_capability_refusal(ask, manifest, _brief()) is None


@pytest.mark.parametrize("inserts", [False, True])
def test_l6_a_refusal_of_the_audio_ask_is_stale_whatever_the_insert_gate(monkeypatch, inserts):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=inserts)
    ask = AskUser(
        kind="ask_user",
        question="I can't intercut those videos with their own audio.",
        reason_code=INCIDENT_CODE,
        options=["Silent full-screen", "Switch to Montage"],
    )

    assert stale_capability_refusal(ask, manifest, _brief()) is not None


def test_l6_an_audio_refusal_that_offers_a_pause_is_re_run_only_when_the_pause_exists(
    monkeypatch,
):
    ask = AskUser(
        kind="ask_user",
        question="I can't use their audio over your talk. Pause your talk instead?",
        reason_code=INCIDENT_CODE,
        options=["Pause my talk", "Keep it silent"],
    )
    closed = _lanes(monkeypatch, cutaways=True, inserts=False)
    # Known limit: with no pause lane the guard cannot tell this stale audio refusal from an honest
    # "I can't pause your talk", so it is left alone rather than re-run.
    assert stale_capability_refusal(ask, closed, _brief()) is None
    opened = _lanes(monkeypatch, cutaways=True, inserts=True)
    assert stale_capability_refusal(ask, opened, _brief()) is not None


def test_l6_a_refusal_whose_own_code_names_the_pause_stays_alone_even_with_an_audio_question(
    monkeypatch,
):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    ask = AskUser(
        kind="ask_user",
        question="I can't intercut those videos with their own audio.",
        reason_code="unsupported_pause_audio",
        options=["a", "b"],
    )

    assert stale_capability_refusal(ask, manifest, _brief()) is None


async def test_l6_the_planner_hands_the_insert_gate_to_the_rerun_note(monkeypatch):
    from unittest.mock import AsyncMock

    from app.kria import planner

    for inserts in (False, True):
        manifest = _lanes(monkeypatch, cutaways=True, inserts=inserts)
        rerun = AsyncMock(return_value=_strategy_output(manifest))
        monkeypatch.setattr(planner, "_call_main_creator", rerun)

        await _turn(monkeypatch, _refusal_output(), manifest, _brief())

        note = rerun.await_args.args[0].agent_input.server_note
        assert ("insert_clips" in note) is inserts, note


# ---- M1: the brief records the right asks ----

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals" / "main_creator"
BRIEF_FIXTURE = FIXTURES / "kri1151_talking_cutaways_insert_open_brief.json"
RECORD_RULE = (
    "record it exactly as the full-screen (`display_mode`) and clip-sound (`clip_sound`) asks are "
    "recorded, never as `triggers` and never as `insert_clip`"
)


def _show_while_talking_rule(text: str) -> str:
    flat = _flat(text)
    start = flat.index("A clip shown while the talk keeps running")
    return flat[start : flat.index("On a montage,", start)]


def _extractor_prompt(*, insert_clips_open: bool) -> str:
    from app.agents._schemas.brief_extractor import BriefExtractionInput
    from app.agents.brief_extractor import BriefExtractorAgent

    return BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(
            creator_request="",
            user_message="x",
            conversation=[],
            insert_clips_open=insert_clips_open,
        )
    )


def test_m1_the_creator_brief_records_a_show_while_talking_ask_as_display_mode_and_clip_sound(
    monkeypatch,
):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    rule = _show_while_talking_rule(_prompt(manifest))

    assert RECORD_RULE in rule
    assert "triggers` ask" not in rule
    for ask in ("show the congress video when I mention congress", "use their audio"):
        assert ask in rule


def test_m1_the_extractor_s_narrowed_brief_says_the_same_when_inserts_are_open():
    rule = _show_while_talking_rule(_extractor_prompt(insert_clips_open=True))

    assert RECORD_RULE in rule
    assert "triggers` ask" not in rule
    assert "A clip shown while the talk keeps running" not in _flat(
        _extractor_prompt(insert_clips_open=False)
    )


def test_m1_the_triggers_rule_for_photos_and_sounds_timed_to_a_word_is_untouched(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))

    assert '"show the medal photo when I say medal"' in flat
    assert '=> {"triggers": ["medal"]}' in flat
    assert "a photo, sticker or sound timed to a spoken word keeps `triggers`" in _flat(
        _show_while_talking_rule(_prompt(manifest))
    )


def test_m1_the_prod_request_brief_shape_is_what_the_both_lanes_golden_carries(monkeypatch):
    fixture = json.loads(BRIEF_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["input"]["brief_enabled"] is True
    # The thread's own message: it has no "in between my speech" (the wording that adds it is
    # the insert fixtures `kri1151_insert_between_speech_*.json`).
    assert fixture["input"]["user_message"] == THREAD["creator_request"]
    updates = fixture["output"]["brief_updates"]
    prod = {r["id"]: r for r in THREAD["brief"]["requirements"]}

    def shape(row: dict) -> tuple[str, str, str]:
        return (row["kind"], row["scope"].split(":")[0], json.dumps(row["facts"], sort_keys=True))

    assert [shape(row) for row in updates] == [shape(prod[rid]) for rid in ("r1", "r2", "r3", "r4")]
    assert all("triggers" not in row["facts"] for row in updates)
    assert fixture["output"]["action"]["strategy"].get("insert_clips") is None

    # ... and that brief is exactly what the cutaway lane reads, with the prompt of that fixture
    # teaching it.
    from app.kria.cutaway_show_ask import brief_full_screen_show_ask

    ask = brief_full_screen_show_ask(_brief())
    assert ask is not None and ask.whole_video

    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

    for name, value in fixture["meta"]["settings"].items():
        monkeypatch.setattr(settings, name, value)
    from tests._prod_profile import apply_prod_profile

    apply_prod_profile(monkeypatch)
    for name, value in fixture["meta"]["settings"].items():
        monkeypatch.setattr(settings, name, value)
    prompt = _flat(
        MainCreatorAgent(None).render_prompt(MainCreatorInput.model_validate(fixture["input"]))
    )
    assert RECORD_RULE in prompt
    assert SECTION_MARK in prompt and INSERT_MARK in prompt


# ---- M2: the request goes to the cutaway ----


def test_m2_the_ask_once_example_does_not_paraphrase_the_prod_request(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    insert = _flat(_prompt(manifest))
    insert = insert[insert.index(INSERT_MARK) :]

    assert "in the middle of my talk with its sound" not in insert
    assert '("put the congress clip in the middle of my talk")' in insert
    assert "no mention anchor" in insert and "and no pause word" in insert


def test_m2_in_between_my_speech_with_a_mention_is_named_as_the_insert_lane(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))
    insert = flat[flat.index(INSERT_MARK) :]

    assert '"place them in between my speech"' in insert
    assert "when I talk about them" in insert
    assert "end of the sentence the creator says about that clip" in insert
    # ... while "over my speech" and a clip with no sound of its own stay with the overlay lane.
    assert "is the clip shown over the talk (the full-screen clip lane above" in insert
    # The cutaway opening only allows its one question for wording with no anchor and no pause.
    cutaway = flat[flat.index(SECTION_MARK) : flat.index(INSERT_MARK)]
    assert "the single exception is ONE short question when the wording has no spoken anchor" in (
        cutaway
    )
    assert "no pause word" in cutaway


def test_m2_the_pause_form_still_routes_to_insert_clips(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))
    insert = flat[flat.index(INSERT_MARK) :]

    assert "pause my talk and play the congress clip with its sound, then go on" in insert
    assert "set `insert_clips`" in insert
    assert "Without a pause, a stop, an in-the-middle-while-I-wait wording" in insert


# ---- M3: the guard and the insert section ----


def test_m3a_a_generic_audio_refusal_that_offers_a_pause_is_re_run_while_the_pause_exists(
    monkeypatch,
):
    ask = AskUser(
        kind="ask_user",
        question=(
            "I can't play their audio over your talk. Should I pause your talk for each clip "
            "instead?"
        ),
        reason_code="capability_gap",
        options=["Pause my talk", "Skip the audio"],
    )
    opened = _lanes(monkeypatch, cutaways=True, inserts=True)
    assert stale_capability_refusal(ask, opened, _brief()) == ask_refusals.CAPABILITY_SOUND
    closed = _lanes(monkeypatch, cutaways=True, inserts=False)
    assert stale_capability_refusal(ask, closed, _brief()) is None


def test_m3b_the_one_clarifying_question_with_a_refusal_word_in_its_code_is_not_a_refusal(
    monkeypatch,
):
    ask = AskUser(
        kind="ask_user",
        question=(
            "Should your talk pause while the clip plays with its own sound, or should the "
            "clip cover your talk while it keeps running?"
        ),
        reason_code="cannot_tell_audio_intent",
        options=["Pause my talk", "Cover my talk"],
    )
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)

    assert stale_capability_refusal(ask, manifest, _brief()) is None


def test_m3b_a_refusal_in_the_question_text_is_still_a_refusal_even_with_a_pause_option(
    monkeypatch,
):
    ask = AskUser(
        kind="ask_user",
        question="I cannot tell whether you want their audio. Should your talk pause instead?",
        reason_code="cannot_tell_audio_intent",
        options=["Pause my talk", "Cover my talk"],
    )
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)

    assert stale_capability_refusal(ask, manifest, _brief()) is not None


@pytest.mark.parametrize("cutaways", [True, False])
def test_m3c_the_insert_section_says_an_earlier_unavailable_reply_is_out_of_date(
    monkeypatch, cutaways
):
    manifest = _lanes(monkeypatch, cutaways=cutaways, inserts=True)
    flat = _flat(_prompt(manifest))
    insert = flat[flat.index(INSERT_MARK) :]

    assert (
        "An earlier reply in this chat that said the talk cannot stop for a clip and carry on is "
        "out of date"
    ) in insert
    closed = _flat(_prompt(_lanes(monkeypatch, cutaways=cutaways, inserts=False)))
    assert "cannot stop for a clip" not in closed


# ---- M4: inserts open, cutaways closed ----


def test_m4_with_the_cutaways_closed_the_insert_section_says_plainly_what_exists(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=False, inserts=True)
    flat = _flat(_prompt(manifest))
    insert = flat[flat.index(INSERT_MARK) :]

    assert "leave `insert_clips` empty" in insert
    assert "A clip shown over a talk that keeps running is not available in this edit" in insert
    assert "offer the nearest thing that exists" in insert
    assert "while the talk pauses and then carries on" in insert
    assert "Never promise a clip shown over the talk" in insert
    # ... and the cutaway-open text does not carry it.
    both = _flat(_prompt(_lanes(monkeypatch, cutaways=True, inserts=True)))
    assert "is not available in this edit" not in both[both.index(INSERT_MARK) :]
