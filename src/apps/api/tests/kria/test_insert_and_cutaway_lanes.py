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
"""

from __future__ import annotations

import hashlib

import pytest

from app.agents._schemas.creator_agent import AskUser
from app.config import settings
from app.kria import ask_refusals
from app.kria.ask_refusals import stale_capability_refusal
from app.services import creator_capabilities as capabilities
from tests.kria.test_planner_knows_talking_cutaways import (
    INCIDENT_CODE,
    SECTION_MARK,
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
MAIN_PROMPT_SHA = {
    "neither": "3e4c34416da710a2ce0e21cc2ed1717685382236cf505f25ebe52eec677538c8",
    (True, True): "2bcfab4ad250e8c5b53261ea3a2c7d1455e2c60a0ae7e68aad48603bce955fb5",
    (True, False): "863f8d53707861feaf994843f1a2935bee37da0570be7cbf67991da84281f82b",
    (False, True): "874e2e7538ab59d1b20296efa2a23e1b5edbe47f386bd6b2bb89396c0730115f",
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
    assert "only when the creator explicitly asks the talk to pause or stop" in flat


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


def test_l5_show_it_when_i_mention_it_is_a_cutaway_and_only_a_pause_is_an_insert(monkeypatch):
    manifest = _lanes(monkeypatch, cutaways=True, inserts=True)
    flat = _flat(_prompt(manifest))
    insert = flat[flat.index(INSERT_MARK) :]

    for ask in ("show the congress video when I mention congress", "use their audio"):
        assert ask in insert
    assert "is NOT an insert, whatever it says about sound" in insert
    assert "the clip shown over the talk" in insert
    assert "Without a pause, a stop or an explicit in-the-middle-while-I-wait wording" in insert
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


def test_l6_an_audio_refusal_that_offers_a_pause_is_corrected_only_when_the_pause_exists(
    monkeypatch,
):
    ask = AskUser(
        kind="ask_user",
        question="I can't use their audio over your talk. Pause your talk instead?",
        reason_code=INCIDENT_CODE,
        options=["Pause my talk", "Keep it silent"],
    )
    closed = _lanes(monkeypatch, cutaways=True, inserts=False)
    assert stale_capability_refusal(ask, closed, _brief()) is None  # an honest "no pause" answer
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
