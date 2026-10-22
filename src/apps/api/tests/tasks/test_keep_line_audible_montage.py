"""KRI-803: "keep his name exchange audible ("What's your name?" "Lucas.")" keeps the line.

Incident (prod thread F4083F1D, Don Julio kit M1, 2026-10-21): the brief filed both asks as
``kind=audio, scope=global, facts={"audio_intent": "source_unchanged", "triggers": [line,
...]}`` with no ``keep_whole_take``. KRI-760 only read ``keep_whole_take``, so the pop-in
checker took them and the held draft said "Couldn't: Keep waiter's name exchange audible /
Keep that "Cheers!" audible (Photo and sound pop-ins timed to your words aren't available
for this edit yet)". Retrying could never pass.

Even on the whole-line path the lines were not findable: the line finder matched two shared
5-letter word stems, and "Cheers!", "Lucas." and "What's your name?" have none. And the name
clip's timed sentences came back in Turkish ("Ne isminiz?") while its untimed transcript has
the English line.

The clip records below are prod's own (``tests/fixtures/kri803_don_julio_m1_clips.json``).
Ground truth, measured on the kit's source audio (free, local: whisper.cpp ggml-medium word
times + the 100 ms loudness envelope): m1_05 says "What's your name?" at 0.14-1.64 s and
"Lucas." at 2.0-2.5 s (more talk "Lucas, yeah." to 3.6 s); m1_01's "Cheers!" bursts sit at
0.9-1.2 and 1.7-1.8 s (kit README: ~0.9-1.9 s).

Ways this can fail, each pinned below (written before the code):
1. The asks as prod filed them are refused as pop-ins (the incident), at draft or render.
2. A one-word line ("Cheers!", "Lucas.") or a line of short words ("What's your name?") is
   not found, so the receipt says "couldn't find" for a line the clip plainly says.
3. A line the timed sentences mistranscribed (another language) but the untimed transcript
   has is never looked for by word timings; or a word pass that misses it is called found.
4. The word pass runs when the timed sentences already have the line (needless cost), or a
   failed pass breaks planning.
5. Two lines of one ask in the same clip: only the first seats the window and the second is
   cut off ("Lucas." after 2.5 s).
6. The cuts for m1_05 / m1_01 miss the measured spans, or the edit drifts off 15 s.
7. The receipt says met while the finished cut misses one of the lines.
8. A one-word line two clips say is guessed onto one of them.
9. A plain "keep the original audio" ask (no named line) turns into a whole-line ask.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    check_requirement,
    is_judged,
    plan_facts_from_rendered_montage,
    wants_whole_line,
)
from app.kria.planner import _enrich_best_parts_snapshot, _ground_spoken_lines
from tests.tasks.test_named_line_windows_montage import (
    _finished,
    _holds,
    _render,
    _snapshot,
    _transcriber,
    _window,
)
from tests.tasks.test_selected_clip_dispatch import _verify

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri803_don_julio_m1_clips.json"
CLIPS = json.loads(_FIXTURE.read_text(encoding="utf-8"))["clips"]
NAME_CLIP, CHEERS_CLIP, RED_CAP, BLURRY = "m1_05", "m1_01", "m1_02", "m1_04"
USED = [c for c in CLIPS if c["media_id"] != BLURRY]
NAME_Q, LUCAS, CHEERS = "What's your name?", "Lucas.", "Cheers!"
# Measured on the source audio (see the module docstring).
NAME_SPAN = (0.14, 2.5)
CHEERS_SPAN = (0.9, 1.9)
# m1_05's next words ("Lucas, yeah.") start at 2.9 s and end by 3.6 s.
NEXT_WORDS = (2.9, 3.6)
# The word pass on m1_05 (whisper.cpp medium on the kit file; "Lucas." end from loudness).
NAME_WORDS = [
    {"text": "What's", "start_s": 0.14, "end_s": 0.77},
    {"text": "your", "start_s": 0.77, "end_s": 1.21},
    {"text": "name?", "start_s": 1.21, "end_s": 1.64},
    {"text": "Lucas.", "start_s": 2.0, "end_s": 2.5},
]

R1 = BriefRequirement(id="r1", kind="text", scope="title", literal="Lunch at Don Julio")
R3 = BriefRequirement(
    id="r3",
    kind="order",
    scope="global",
    description="Sequence: waiter, sauce bowls, menus, cheers, ending on friend in red cap",
    facts={
        "closing": "friend in the red cap smiling at the camera",
        "first_clip": "waiter smiling at the camera",
    },
)
R4 = BriefRequirement(
    id="r4", kind="select", scope="global", description="Skip the blurry shot of the empty table"
)
R5 = BriefRequirement(
    id="r5",
    kind="timing",
    scope="global",
    description="Make it 15 seconds",
    facts={"duration_s": 15},
)
# r6 / r7 exactly as prod filed them.
R6 = BriefRequirement(
    id="r6",
    kind="audio",
    scope="global",
    description="Keep waiter's name exchange audible",
    facts={"audio_intent": "source_unchanged", "triggers": [NAME_Q, LUCAS]},
)
R7 = BriefRequirement(
    id="r7",
    kind="audio",
    scope="global",
    description='Keep that "Cheers!" audible too',
    facts={"audio_intent": "source_unchanged", "triggers": [CHEERS]},
)
R8 = BriefRequirement(
    id="r8", kind="audio", scope="global", description="Suggest an upbeat tango song for it"
)
MONTAGE_DRAFT = PlanFacts(reaction_beats_available=False, reaction_beats=(), edit_format="montage")


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=2, requirements=list(reqs or (R1, R3, R4, R5, R6, R7, R8)))


def _intent(position: str, media_id: str, attribute: str) -> dict:
    return {
        "op": "order",
        "status": "resolved",
        "intent_id": f"order_{position}",
        "attribute": attribute,
        "position": position,
        "assignments": [{"media_id": media_id, "confidence": 1.0, "evidence": "visual"}],
    }


def _strategy() -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "selected",
        "selected_media_ids": [c["media_id"] for c in USED],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "opening_title": "Lunch at Don Julio",
        "target_duration_s": 15.0,
        "creator_duration_s": 15.0,
        "target_duration_requested": True,
        "video_reuse_policy": "once",
        "resolved_clip_intents": [
            _intent("first", NAME_CLIP, "waiter smiling at the camera"),
            _intent("last", RED_CAP, "friend in the red cap smiling at the camera"),
        ],
    }


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


async def _plan(brief: CreativeBrief, *, clips=CLIPS, words=NAME_WORDS, calls=None):
    strategy, snapshot = _strategy(), _snapshot(clips)
    transcribe = _transcriber(words, calls)
    closing_quote, named = await _ground_spoken_lines(
        strategy, snapshot, brief, transcribe=transcribe
    )
    enriched = _enrich_best_parts_snapshot(
        strategy, snapshot, brief, closing_quote=closing_quote, named_lines=named
    )
    return strategy, enriched


async def _rendered(brief: CreativeBrief | None = None, **plan_kw):
    brief = brief or _brief()
    strategy, snapshot = await _plan(brief, **plan_kw)
    plan, contract, unified = _render(strategy, snapshot, brief, USED)
    return snapshot, plan, contract, unified


def _cut(plan, media_id: str):
    return next(c for c in plan.snapshot.fast_cuts if c.media_id == media_id)


# -- 1: the asks as prod filed them ----------------------------------------------------


@pytest.mark.parametrize("req", [R6, R7], ids=["name-exchange", "cheers"])
def test_the_filed_asks_are_whole_line_asks_and_the_draft_never_refuses_them(req) -> None:
    assert wants_whole_line(req)
    receipt = check_requirement(req, MONTAGE_DRAFT)
    assert receipt.status != "not_possible"
    assert "pop-in" not in (receipt.reason or "").lower()
    # Nothing rendered yet: neutral, judged at render-ready.
    assert not is_judged(req, receipt)


# -- 2-7: the incident, end to end -----------------------------------------------------


async def test_incident_keeps_the_name_exchange_and_the_cheers_whole_and_says_met() -> None:
    calls: list = []
    snapshot, plan, contract, unified = await _rendered(calls=calls)
    length_plan = snapshot["length_plan"]
    assert not length_plan["conflicts"]

    # 2 + 3: every named line is found, each in its own clip.
    rows = {row["quote"]: row for row in length_plan["named_lines"]}
    assert set(rows) == {NAME_Q, LUCAS, CHEERS}
    assert {q: (r["status"], r["media_id"]) for q, r in rows.items()} == {
        NAME_Q: ("found", NAME_CLIP),
        LUCAS: ("found", NAME_CLIP),
        CHEERS: ("found", CHEERS_CLIP),
    }
    # 4: the word pass ran once, only for the line the timed sentences lack.
    assert calls == [(f"users/u/analysis-proxy-{NAME_CLIP}.mp4", None, NAME_Q)]

    # 5 + 6: one window holds the whole exchange; the cheers clip holds its line.
    assert _holds(*_window(snapshot, NAME_CLIP), NAME_SPAN)
    assert _holds(*_window(snapshot, CHEERS_CLIP), CHEERS_SPAN)
    name, cheers = _cut(plan, NAME_CLIP), _cut(plan, CHEERS_CLIP)
    assert name.source_start_s == pytest.approx(0.0)
    assert _holds(name.source_start_s, name.source_end_s, NAME_SPAN)
    # Never cut mid-word: the cut ends in the pause before the next words, or after them.
    assert name.source_end_s <= NEXT_WORDS[0] or name.source_end_s >= NEXT_WORDS[1]
    assert _holds(cheers.source_start_s, cheers.source_end_s, CHEERS_SPAN)
    assert plan.snapshot.fast_cuts[0].media_id == NAME_CLIP
    assert BLURRY not in [c.media_id for c in plan.snapshot.fast_cuts]
    assert plan.duration_s == pytest.approx(15.0, abs=0.1)
    assert _verify(contract, plan, unified)

    # 7: the finished video's review judges both asks, and both lines are there.
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    for req, quotes in ((R6, (NAME_Q, LUCAS)), (R7, (CHEERS,))):
        receipt = check_requirement(req, facts)
        assert receipt.status == "met", receipt.reason
        assert is_judged(req, receipt)
        assert all(quote in (receipt.reason or "") for quote in quotes)


async def test_the_answer_named_first_still_keeps_the_question() -> None:
    # 5: the same exchange recorded answer-first: one window still runs from the first
    # line's start to the last line's end, whichever the model listed first.
    r6 = BriefRequirement(
        id="r6",
        kind="audio",
        scope="global",
        description=R6.description,
        facts={"audio_intent": "source_unchanged", "triggers": [LUCAS, NAME_Q]},
    )
    snapshot, plan, _contract, _unified = await _rendered(_brief(R1, R3, R4, R5, r6, R7, R8))
    assert _holds(*_window(snapshot, NAME_CLIP), NAME_SPAN)
    name = _cut(plan, NAME_CLIP)
    assert _holds(name.source_start_s, name.source_end_s, NAME_SPAN)


async def test_without_word_timings_the_unplaced_line_is_named_and_never_met() -> None:
    # 3 + 4: the word pass fails (timeout, no key): planning goes on, the cheers is still
    # held, and the name ask says which line it could not place.
    def failing(gcs_path: str, *, language: str | None, prompt: str | None):
        raise TimeoutError("whisper unavailable")

    strategy, snapshot = _strategy(), _snapshot(CLIPS)
    closing_quote, named = await _ground_spoken_lines(
        strategy, snapshot, _brief(), transcribe=failing
    )
    rows = {row["quote"]: row["status"] for row in named or ()}
    assert rows == {NAME_Q: "not_found", LUCAS: "found", CHEERS: "found"}
    enriched = _enrich_best_parts_snapshot(
        strategy, snapshot, _brief(), closing_quote=closing_quote, named_lines=named
    )
    plan, _contract, _unified = _render(strategy, enriched, _brief(), USED)
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    name = check_requirement(R6, facts)
    assert name.status == "partial"
    assert NAME_Q in (name.reason or "") and "pop-in" not in (name.reason or "").lower()
    assert check_requirement(R7, facts).status == "met"


async def test_a_word_pass_without_the_line_is_not_found() -> None:
    # 3: whisper heard other words; nothing is guessed.
    other = [{"text": "Gracias.", "start_s": 0.2, "end_s": 0.8}]
    _strategy_, snapshot = await _plan(_brief(), words=other)
    rows = {row["quote"]: row["status"] for row in snapshot["length_plan"]["named_lines"]}
    assert rows[NAME_Q] == "not_found"


async def test_a_cut_that_drops_the_answer_is_never_met() -> None:
    # 7: the finished cut stops before "Lucas.".
    _snap, plan, _contract, _unified = await _rendered()
    finished = _finished(plan)
    for row in finished["story_timeline"]:
        if row.get("media_id") == NAME_CLIP:
            row["source_end_s"] = 1.8
    facts = plan_facts_from_rendered_montage(finished, plan.record())
    receipt = check_requirement(R6, facts)
    assert receipt.status == "partial"
    assert LUCAS in (receipt.reason or "")


async def test_a_one_word_line_two_clips_say_is_not_guessed() -> None:
    # 8: a second clip also says "Cheers." in its timed sentences.
    clips = json.loads(json.dumps(CLIPS))
    twin = next(c for c in clips if c["media_id"] == "m1_07")
    twin["analysis"]["understanding"]["speech"]["segments"].append(
        {"text": "Cheers.", "start_s": 1.3, "end_s": 1.6}
    )
    _strategy_, snapshot = await _plan(_brief(), clips=clips)
    rows = {row["quote"]: row["status"] for row in snapshot["length_plan"]["named_lines"]}
    assert rows[CHEERS] == "ambiguous"


async def test_plain_keep_the_original_audio_moves_no_window() -> None:
    # 9: no named line recorded: the source-sound ask keeps its own path and windows.
    plain = BriefRequirement(
        id="r6",
        kind="audio",
        scope="global",
        description="Keep the original audio",
        facts={"audio_intent": "source_unchanged"},
    )
    assert not wants_whole_line(plain)
    _strategy_, asked = await _plan(_brief(R1, R5, plain))
    assert "named_lines" not in asked["length_plan"]
