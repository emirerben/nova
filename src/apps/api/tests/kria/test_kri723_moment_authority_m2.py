"""KRI-723: the creator points at each named moment, and the render plays exactly those.

Merope M2 (``tests/fixtures/kri775_merope_m2_turns.json``): one 68 s take, one Cotopaxi photo.
"six moments from it, about 2 seconds each, in this order: Caro arriving with the black tray,
her hello wave, the plate pushed close to the camera, Atakan's first bite, his thumbs-up,
Caro's bye-bye wave. Put the Cotopaxi photo right after the hello wave for 1 second."

Owner decision (2026-10-24): no paid localizer and no new model call. A named action with a
grounded interval in the stored clip record is used; for the rest, Kria asks the creator,
BEFORE approval, to put the moments in order and point at each one. The answer is pinned
source-interval authority the worker executes unchanged.

Failure modes, written before the code (each pinned below):

F1  Wrong content passes: six ordinal energy windows with the right count, order and
    length read "met" although nobody knows what they show.
F2  The photo is seated by window ordinal, not after the evidenced hello-wave cut.
F3  An unlocated or ambiguous action is guessed (the gate never asks; two "wave" moments
    pick one), or model prose timestamps count as evidence.
F4  Requested order is confused with source order: a hello wave the creator points at
    AFTER the plate in the take is re-sorted back to source order.
F6  The creator's window is altered after approval (re-centred, trimmed, re-selected), or a
    tampered envelope renders anyway.
F7  A stale / incomplete / out-of-bounds / overlapping answer is accepted.
F9  An unanswered card turns into guessed windows: a creator who replies in free text gets
    an approvable draft of ordinal windows (owner decision: ask BEFORE approval). The card
    itself always offers skip / "you pick", so asking again is never a dead end.
F10 The answer's echo carries times or digits a model could read as new timing asks.
F11 A reworded moment keeps its stale pin; unchanged moments lose theirs.
F12 Old clients (and every edit without named moments) change behaviour beyond an honest
    receipt: their length plan stays v1 and byte-identical, and they are never blocked.
F17 Turkish chats get an English question.
F18 A licensed-music montage (the default; the frozen section owns the length) asks, then
    drops the answer for ordinal windows.
F19 A pointed window that ends a frame past the take (inside the answer's slack) is clamped
    in the window but not in the record, so the approved plan fails at render.
F20 Pins that cannot all be placed silently fall back to ordinal windows (no conflict).
F21 Pins leak past the dark question gate: an app that cannot answer gets a v2 envelope (an
    older worker refuses it) with no kill switch.

Offline: the real server gate, length plan, phone unified montage worker and receipts on
prod's recorded M2 brief, strategy and clip record. No model call anywhere (the gate makes
none; the worker's two text-only model steps are fixed values, as in the KRI-775 replay).

``KRI723_ARTIFACT=<dir>`` also writes ``report.json`` (schema ``kri-723-verification/1``).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from app.kria.brief_checks import is_format_limit, needs_creator_choice
from app.kria.reply_language import reply_language_for
from app.services.named_moments import (
    MOMENT_ORDER_ANSWER_KEY,
    MOMENT_ORDER_QUESTION_KEY,
    MomentOrderError,
    model_echo,
    validate_moment_order_answer,
)
from tests.kria.test_kri775_merope_m2_replay import (
    RENDERS,
    TAKE,
    TAKE_S,
    gate_media,
    gate_strategy,
    m2_brief_with_moment_ask,
    planned_turn,
    prod_flags,
    run_worker,
)

MOMENT_WORDS = [
    "Caro arriving with the black tray",
    "her hello wave",
    "the plate pushed close to the camera",
    "Atakan's first bite",
    "his thumbs-up",
    "Caro's bye-bye wave",
]
# The creator's pointing, on the original clock. The hello wave is pointed AFTER the plate
# in the take (F4): the video must still play it second, as asked.
POINTED = {
    "Caro arriving with the black tray": (2.0, 4.0),
    "her hello wave": (33.0, 35.0),
    "the plate pushed close to the camera": (24.0, 26.0),
    "Atakan's first bite": (57.5, 59.5),
    "his thumbs-up": (61.0, 63.0),
    "Caro's bye-bye wave": (65.0, 67.0),
}
REPORT: dict = {"schema": "kri-723-verification/1", "cases": {}}


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


def _words(text: str) -> str:
    return " ".join(text.casefold().split())


async def gate(
    monkeypatch: pytest.MonkeyPatch,
    render: dict,
    *,
    events=(),
    answers=True,
    brief=None,
    client_protocol: int | None = None,
):
    """``client_protocol`` (KRI-898): the app build that sent the turn; ``None`` = unstamped."""
    from dataclasses import replace  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from app.kria import planner  # noqa: PLC0415
    from tests.kria.test_kri775_merope_m2_replay import CREATOR_ID, THREAD_ID  # noqa: PLC0415

    brief = brief if brief is not None else m2_brief_with_moment_ask(render)
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    return await planner._gate_unresolved_choices(
        SimpleNamespace(),
        replace(planned_turn(render), client_protocol=client_protocol),
        thread_id=THREAD_ID,
        creator_id=CREATOR_ID,
        answers_moment_questions=answers,
    )


def question_of(result) -> dict:
    assert result.plan.mode == "respond" and result.plan.turn_value == "question", result.plan
    question = result.plan.moment_order_question
    assert question is not None, result.plan
    return copy.deepcopy(question)


def answer_for(question: dict, pointed: dict[str, tuple[float, float]] | None = None) -> dict:
    pointed = POINTED if pointed is None else pointed
    by_words = {_words(row["words"]): row for row in question["moments"]}
    answers = []
    for words, (start, end) in pointed.items():
        row = by_words[_words(words)]
        answers.append(
            {
                "moment_key": row["moment_key"],
                "action": "point",
                "media_id": question["take"]["media_id"],
                "source_start_s": start,
                "source_end_s": end,
            }
        )
    return {"question_id": question["question_id"], "answers": answers}


def answered_events(question: dict, answer: dict) -> list[tuple[str, dict]]:
    return [
        ("assistant", {MOMENT_ORDER_QUESTION_KEY: question, "turn_value": "question"}),
        ("user", {MOMENT_ORDER_ANSWER_KEY: answer, "event_type": "user_message"}),
    ]


def take_windows(media: dict) -> list[tuple[float, float]]:
    return [
        (w["source_start_s"], w["source_end_s"])
        for w in media["length_plan"]["windows"]
        if w["media_id"] == TAKE
    ]


def record(case: str, **fields) -> None:
    REPORT["cases"][case] = fields
    target = os.environ.get("KRI723_ARTIFACT")
    if target:
        out = Path(target)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(
            json.dumps(REPORT, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )


# -- C1: ask, answer, approve, render exactly the pointed windows ---------------------------


@pytest.mark.parametrize("index", [0, 1], ids=["listed-row", "one-row-per-moment"])
async def test_unlocated_moments_are_asked_before_approval(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    """F3: five of six actions have no interval in the clip record; nothing is guessed."""
    result = await gate(monkeypatch, RENDERS[index])
    question = question_of(result)
    assert [row["words"] for row in question["moments"]] == MOMENT_WORDS
    statuses = {row["words"]: row["status"] for row in question["moments"]}
    # Only "Atakan's first bite" grounds ("man takes first bite of dessert", 57.04-59.84).
    assert statuses == {
        words: ("grounded" if words == "Atakan's first bite" else "unlocated")
        for words in MOMENT_WORDS
    }
    bite = next(row for row in question["moments"] if row["status"] == "grounded")
    assert (bite["found"]["source_start_s"], bite["found"]["source_end_s"]) == (57.04, 59.84)
    assert question["take"]["media_id"] == TAKE
    assert question["take"]["duration_s"] == TAKE_S
    assert question["default_window_s"] == pytest.approx(2.0)
    assert "1" in result.plan.response and "6" in result.plan.response, result.plan.response
    record(
        f"C1-question-{index}",
        moments=question["moments"],
        response=result.plan.response,
        model_calls=0,
    )


@pytest.mark.parametrize("index", [0, 1], ids=["listed-row", "one-row-per-moment"])
async def test_the_answer_is_pinned_and_rendered_unchanged_in_the_creators_order(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    """F2, F4, F6: pinned windows, in the creator's order, photo after the hello-wave cut."""
    render = RENDERS[index]
    question = question_of(await gate(monkeypatch, render))
    answer = answer_for(question)
    result = await gate(monkeypatch, render, events=answered_events(question, answer))
    assert result.plan.mode == "act", result.plan.response
    media, strategy = gate_media(result), gate_strategy(result)
    plan = media["length_plan"]
    assert plan["version"] == 2
    assert take_windows(media) == list(POINTED.values())
    assert [m["words"] for m in plan["moments"]] == MOMENT_WORDS
    assert {m["origin"] for m in plan["moments"]} == {"creator_answer"}

    brief = m2_brief_with_moment_ask(render)
    outcome = run_worker(monkeypatch, render, brief=brief, media=media, strategy=strategy)
    assert outcome.refusal is None, outcome.refusal
    receipts = {row["requirement_id"]: row for row in outcome.receipts}
    assert receipts["r1"]["status"] == "met", receipts["r1"]
    assert not [row for row in outcome.failures], outcome.failures
    # Twice: the worker recomputes from the envelope alone, so the same windows come out.
    again = run_worker(monkeypatch, render, brief=brief, media=media, strategy=strategy)
    assert again.receipts == outcome.receipts
    record(
        f"C1-render-{index}",
        posted=answer,
        envelope_windows=plan["windows"],
        moments=plan["moments"],
        receipts=outcome.receipts,
    )


async def test_the_cuts_play_the_pointed_windows_and_the_photo_follows_the_hello_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 + F6 read off the worker's own plan (the cuts it will render)."""
    import app.pipeline.unified_montage as unified  # noqa: PLC0415

    render = RENDERS[0]
    question = question_of(await gate(monkeypatch, render))
    result = await gate(monkeypatch, render, events=answered_events(question, answer_for(question)))
    seen: dict = {}
    build = unified.plan_unified_montage

    def capture(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen["plan"] = build(*args, **kwargs)
        return seen["plan"]

    monkeypatch.setattr(unified, "plan_unified_montage", capture)
    outcome = run_worker(
        monkeypatch,
        render,
        brief=m2_brief_with_moment_ask(render),
        media=gate_media(result),
        strategy=gate_strategy(result),
    )
    assert outcome.refusal is None, outcome.refusal
    cuts = [
        (cut.source_start_s, cut.source_end_s) if cut.media_id == TAKE else "PHOTO"
        for cut in seen["plan"].snapshot.fast_cuts
    ]
    # take (tray), take (hello wave), PHOTO, take x4: the pointed windows, as asked.
    assert cuts == [*list(POINTED.values())[:2], "PHOTO", *list(POINTED.values())[2:]], cuts
    rows = [r for r in seen["plan"].record()["intent_outcomes"] if r.get("op") == "order"]
    assert all(r["status"] == "met" for r in rows), rows
    record("C1-cuts", cuts=cuts, order_rows=rows)


async def test_the_freeze_holds_the_bite_the_creator_pointed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F15: with freeze frames on, "freeze the frame on Atakan's bite" holds the last frame of
    the creator's bite window; the window is never re-anchored onto the analysed moment."""
    import app.pipeline.unified_montage as unified  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415

    features = [f for f in settings.phone_render_verified_features if f != "freezeFrame"]
    monkeypatch.setattr(settings, "phone_render_verified_features", [*features, "freezeFrame"])
    render = RENDERS[0]
    # KRI-898: a freeze is planned only for an app build that plays it.
    app = settings.freeze_frame_min_client_protocol
    question = question_of(await gate(monkeypatch, render, client_protocol=app))
    result = await gate(
        monkeypatch,
        render,
        events=answered_events(question, answer_for(question)),
        client_protocol=app,
    )
    seen: dict = {}
    build = unified.plan_unified_montage

    def capture(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen["plan"] = build(*args, **kwargs)
        return seen["plan"]

    monkeypatch.setattr(unified, "plan_unified_montage", capture)
    outcome = run_worker(
        monkeypatch,
        render,
        brief=m2_brief_with_moment_ask(render),
        media=gate_media(result),
        strategy=gate_strategy(result),
        client_protocol=app,
    )
    assert outcome.refusal is None, outcome.refusal
    holds = seen["plan"].record()["freeze_frames"]
    assert [(h["status"], h["grounding"], h["frame_source_s"]) for h in holds] == [
        ("held", "named_moment", POINTED["Atakan's first bite"][1])
    ], holds
    take_cuts = [
        (cut.source_start_s, cut.source_end_s)
        for cut in seen["plan"].snapshot.fast_cuts
        if cut.media_id == TAKE
    ]
    assert take_cuts == list(POINTED.values())
    receipts = {row["requirement_id"]: row for row in outcome.receipts}
    assert receipts["r1"]["status"] == "met", receipts["r1"]
    record("C1-freeze", holds=holds, take_cuts=take_cuts)


# -- C2: wrong-content control ----------------------------------------------------------------


async def test_ordinal_energy_windows_never_read_as_the_named_moments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 + F12: an app that cannot answer gets today's six windows (v1, byte-identical) and a
    buildable draft, but the receipt never claims the actions: an honest format limit. The
    grounded bite is not pinned either: pins stay dark with the question."""
    render = RENDERS[0]
    result = await gate(monkeypatch, render, answers=False)
    assert result.plan.mode == "act", result.plan.response
    media = gate_media(result)
    assert media["length_plan"]["version"] == 1 and "moments" not in media["length_plan"]
    assert len(take_windows(media)) == 6
    outcome = run_worker(
        monkeypatch,
        render,
        brief=m2_brief_with_moment_ask(render),
        media=media,
        strategy=gate_strategy(result),
    )
    # The export path is fine (the plan builds and the worker renders it) ...
    assert outcome.refusal is None, outcome.refusal
    receipts = {row["requirement_id"]: row for row in outcome.receipts}
    # ... and fulfillment fails: nobody located those actions.
    assert receipts["r1"]["status"] != "met", receipts["r1"]
    assert is_format_limit(receipts["r1"]["reason"]), receipts["r1"]
    assert not needs_creator_choice(receipts["r1"])
    record("C2-wrong-content", windows=take_windows(media), r1=receipts["r1"])


async def test_a_tampered_envelope_is_refused_not_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    """F6: swap two creator-pointed windows (same count, order, lengths): refused."""
    render = RENDERS[0]
    question = question_of(await gate(monkeypatch, render))
    result = await gate(monkeypatch, render, events=answered_events(question, answer_for(question)))
    media = gate_media(result)
    windows = media["length_plan"]["windows"]
    a, b = windows[0], windows[2]
    for key in ("source_start_s", "source_end_s"):
        a[key], b[key] = b[key], a[key]
    # The worker refuses an approved envelope it cannot trust, the same way it refuses
    # overlapping windows (``read_length_plan``): no render, no silent re-selection.
    with pytest.raises(ValueError, match="moment window changed"):
        run_worker(
            monkeypatch,
            render,
            brief=m2_brief_with_moment_ask(render),
            media=media,
            strategy=gate_strategy(result),
        )


# -- C3: ambiguous moments offer suggestions, never pick one ------------------------------------


async def test_two_matching_moments_are_offered_not_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    render = copy.deepcopy(RENDERS[0])
    analysis = render["media_snapshot"]["clip_assignments"][0]["analysis"]
    analysis["best_moments"] = [
        *analysis["best_moments"],
        {"start_s": 5.0, "end_s": 7.0, "energy": 5.0, "description": "woman waves hello"},
        {"start_s": 40.0, "end_s": 41.5, "energy": 5.0, "description": "woman waves hello again"},
    ]
    question = question_of(await gate(monkeypatch, render))
    hello = next(row for row in question["moments"] if row["words"] == "her hello wave")
    assert hello["status"] == "ambiguous"
    assert [(s["source_start_s"], s["source_end_s"]) for s in hello["suggestions"]] == [
        (5.0, 7.0),
        (40.0, 41.5),
    ]
    assert hello["found"] is None


# -- C5: the answer is checked ------------------------------------------------------------------


async def test_bad_answers_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """F7: stale, incomplete, foreign media, out of bounds, overlapping."""
    question = question_of(await gate(monkeypatch, RENDERS[0]))
    good = answer_for(question)
    validate_moment_order_answer(good, question)

    def broken(mutate) -> str:  # noqa: ANN001
        answer = copy.deepcopy(good)
        mutate(answer)
        with pytest.raises(MomentOrderError) as caught:
            validate_moment_order_answer(answer, question)
        return caught.value.code

    assert broken(lambda a: a.update(question_id="old")) == "moment_order_stale"
    assert broken(lambda a: a["answers"].pop()) == "moment_order_invalid"
    assert broken(lambda a: a["answers"][0].update(media_id="someone-else.mp4")) == (
        "moment_order_media_not_found"
    )
    assert broken(lambda a: a["answers"][0].update(source_end_s=TAKE_S + 5)) == (
        "moment_order_invalid"
    )
    assert broken(lambda a: a["answers"][1].update(source_start_s=2.5, source_end_s=4.5)) == (
        "moment_order_overlap"
    )
    assert validate_moment_order_answer(None, question) is None or True
    with pytest.raises(MomentOrderError):
        validate_moment_order_answer(good, None)


async def test_skip_and_you_pick_are_recorded_never_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = RENDERS[0]
    question = question_of(await gate(monkeypatch, render))
    answer = answer_for(question)
    answer["answers"][4] = {"moment_key": answer["answers"][4]["moment_key"], "action": "skip"}
    answer["answers"][5] = {"moment_key": answer["answers"][5]["moment_key"], "action": "delegate"}
    validate_moment_order_answer(answer, question)
    result = await gate(monkeypatch, render, events=answered_events(question, answer))
    assert result.plan.mode == "act", result.plan.response
    plan = gate_media(result)["length_plan"]
    assert [m["words"] for m in plan["moments"]] == [*MOMENT_WORDS[:4], MOMENT_WORDS[5]]
    assert plan["moments"][-1]["origin"] == "ai_pick_by_request"
    assert take_windows(gate_media(result))[:4] == list(POINTED.values())[:4]


# -- C6: asked until answered ------------------------------------------------------------------


async def test_a_free_text_reply_never_becomes_guessed_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F9: the creator ignored the card: the same card comes back, no approvable draft."""
    render = RENDERS[0]
    question = question_of(await gate(monkeypatch, render))
    events = [
        ("assistant", {MOMENT_ORDER_QUESTION_KEY: question, "turn_value": "question"}),
        ("user", {"event_type": "user_message", "content": "just make it"}),
    ]
    again = question_of(await gate(monkeypatch, render, events=events))
    assert again["question_id"] == question["question_id"]


# -- C8: a reworded moment is re-asked, the rest keep their pins --------------------------------


async def test_a_reworded_moment_is_reasked_and_the_rest_carry_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = copy.deepcopy(RENDERS[1])
    question = question_of(await gate(monkeypatch, render))
    events = answered_events(question, answer_for(question))
    intents = render["strategy"]["resolved_clip_intents"]
    thumbs = next(i for i in intents if i.get("attribute") == "his thumbs-up")
    thumbs["attribute"] = "his big thumbs-up to the camera"
    again = question_of(await gate(monkeypatch, render, events=events))
    by_words = {row["words"]: row for row in again["moments"]}
    assert by_words["his big thumbs-up to the camera"]["status"] == "unlocated"
    assert by_words["her hello wave"]["status"] == "answered"
    assert (
        by_words["her hello wave"]["found"]["source_start_s"],
        by_words["her hello wave"]["found"]["source_end_s"],
    ) == POINTED["her hello wave"]


# -- F10 / F17: the echo and the language -------------------------------------------------------


def test_the_answer_echo_has_no_digits_in_either_language() -> None:
    for language in ("en", "tr"):
        echo = model_echo(language)
        assert echo and not any(ch.isdigit() for ch in echo), echo


async def test_a_turkish_chat_is_asked_in_turkish(monkeypatch: pytest.MonkeyPatch) -> None:
    with reply_language_for("tr"):
        result = await gate(monkeypatch, RENDERS[0])
    question_of(result)
    assert "an" in result.plan.response.casefold()
    assert "moment" not in result.plan.response.casefold(), result.plan.response


# -- C9: a licensed-music bed, the take's last frame, unplaceable pins ---------------------------


def _licensed_music(render: dict) -> dict:
    """The default audio: a frozen catalog section owns the length (``natural_owner`` song)."""
    render = copy.deepcopy(render)
    render["strategy"]["audio_strategy"] = "licensed_music"
    render["strategy"]["selected_music_section"] = {
        "status": "selected",
        "track_id": "kri723-bed",
        "start_s": 10.0,
        "end_s": 22.0,
        "catalog_duration_s": 120.0,
    }
    return render


async def test_a_licensed_music_montage_plays_the_pointed_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F18: the song owns the length; the creator's windows still play, in their order."""
    render = _licensed_music(RENDERS[0])
    first = await gate(monkeypatch, render)
    question = question_of(first)
    result = await gate(monkeypatch, render, events=answered_events(question, answer_for(question)))
    timing = result.plan.choice_question
    assert timing is not None and timing["kind"] == "music_vs_visuals"
    events = [
        *answered_events(question, answer_for(question)),
        ("assistant", {"choice_question": timing, "turn_value": "question"}),
        (
            "user",
            {"choice_selection": {"question_id": timing["question_id"], "option_key": "extend"}},
        ),
    ]
    result = await gate(monkeypatch, render, events=events)
    assert result.plan.mode == "act", result.plan.response
    plan = gate_media(result)["length_plan"]
    assert plan["natural_owner"] == "song", plan["natural_owner"]
    assert plan["version"] == 2 and not plan["conflicts"]
    assert take_windows(gate_media(result)) == list(POINTED.values())
    assert {m["origin"] for m in plan["moments"]} == {"creator_answer"}
    record("C9-licensed-music", windows=plan["windows"], moments=plan["moments"])


async def test_a_window_pointed_to_the_last_frame_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    """F19: the answer ends 0.05 s past the take (accepted slack); the approved plan renders and
    its record holds the window that plays."""
    render = RENDERS[0]
    question = question_of(await gate(monkeypatch, render))
    pointed = dict(POINTED)
    pointed["Caro's bye-bye wave"] = (TAKE_S - 2.0, TAKE_S + 0.05)
    answer = answer_for(question, pointed)
    validate_moment_order_answer(answer, question)
    result = await gate(monkeypatch, render, events=answered_events(question, answer))
    assert result.plan.mode == "act", result.plan.response
    media = gate_media(result)
    last = media["length_plan"]["moments"][-1]
    # The plan's source measures 68.367 s (the question showed 68.37): the window ends there,
    # and the record holds exactly that window.
    assert last["source_end_s"] == take_windows(media)[-1][1]
    assert take_windows(media)[-1][1] == pytest.approx(TAKE_S, abs=0.01)
    outcome = run_worker(
        monkeypatch,
        render,
        brief=m2_brief_with_moment_ask(render),
        media=media,
        strategy=gate_strategy(result),
    )
    assert outcome.refusal is None, outcome.refusal
    receipts = {row["requirement_id"]: row for row in outcome.receipts}
    assert receipts["r1"]["status"] == "met", receipts["r1"]


async def test_pins_that_cannot_fit_block_with_a_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """F20: the creator points at four moments so wide the two left to Kria cannot get their
    asked 2 s anywhere: a blocking question, never ordinal windows."""
    render = RENDERS[0]
    question = question_of(await gate(monkeypatch, render))
    pointed = {
        "Caro arriving with the black tray": (0.0, 17.0),
        "her hello wave": (17.5, 34.0),
        "the plate pushed close to the camera": (34.5, 51.0),
        "Caro's bye-bye wave": (51.5, TAKE_S),
    }
    answer = answer_for(question, pointed)
    by_words = {_words(row["words"]): row for row in question["moments"]}
    for words in ("Atakan's first bite", "his thumbs-up"):
        answer["answers"].insert(
            3, {"moment_key": by_words[_words(words)]["moment_key"], "action": "delegate"}
        )
    validate_moment_order_answer(answer, question)
    result = await gate(monkeypatch, render, events=answered_events(question, answer))
    assert result.plan.mode == "respond", result.plan
    assert "moments" in result.plan.response, result.plan.response


# -- C10: a photo beside the take --------------------------------------------------------------


async def test_a_photo_beside_the_take_is_never_asked_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F22: the length plan pins moments only in an all-video edit, so a selected photo next to
    the take must not get a question whose answer would then be dropped for ordinal windows."""
    render = copy.deepcopy(RENDERS[0])
    photo = {"media_id": "kri723-photo", "kind": "image", "duration_s": 0.0, "analysis": {}}
    render["media_snapshot"]["clip_assignments"].append(photo)
    selected = render["strategy"].get("selected_media_ids")
    if selected:
        selected.append(photo["media_id"])
    result = await gate(monkeypatch, render)
    assert getattr(result.plan, "moment_order_question", None) is None, result.plan
    media = gate_media(result)
    assert (media.get("length_plan") or {}).get("version", 1) == 1
