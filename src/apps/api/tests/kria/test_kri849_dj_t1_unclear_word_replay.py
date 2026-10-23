"""KRI-849: "if you can't tell what he says, ask me instead of guessing" (Don Julio T1).

The creator's prompt (kit ``T1_lucas_full_take_talking``, one 25.7 s Talking take, waiter Lucas
speaks, English chat): "... Spell the sauces right: salsa criolla, chimichurri. I didn't catch the
third sauce he names: if you can't tell what he says, ask me instead of guessing. ..." Done when:
**on DJ T1 Kria asks about the third sauce before approval, or the captions leave it out. It never
prints a guess.**

On main nothing reads that sentence. The Main Creator can only file it as a prose ``style`` row, the
draft shows it as "I can't verify this one automatically yet.", no question is asked before approval
and the real writer (``_run_phone_subtitled_job``) prints whisper-1's guess ("... chimichurri and
salma y limón."). whisper-1 returns no per-word confidence (every word is 1.0), so a "low-confidence
word" detector has nothing to read. What the draft DOES hold before approval are two independent
hearings of the same speech in the approved-media snapshot row: Gemini's untimed ``transcript`` and
whisper-1's timed sentence ``segments``. On the real prod Don Julio utterance
(``tests/fixtures/kri803_don_julio_m1_clips.json`` clip ``m1_03``) they disagree exactly on the
third
sauce ("sal marina" / "salma y limón"). That disagreement is the signal this fix builds on.

This module drives the real boundaries without replacing the planner, the gate, the binding, the job
factory, the writer or the receipts: gate (``planner._gate_unresolved_choices``) -> approval
backstop
(``_unresolved_choice_plan``) -> tap -> pinned ``BriefBinding`` -> real ``_dispatch_item_render`` ->
real ``_run_phone_subtitled_job`` -> ``build_receipts(plan_facts_from_phone_variant(variant))``.
Caption
correction (a model) is off, as in ``tests/tasks/test_kri793_talking_caption_spellings.py``; no
model
call anywhere. ``tests/fixtures/kri849_dj_t1/dj_t1.json`` says which parts are recorded and which
are
hand-authored (DJ T1 itself has never been sent, so the take's row, brief and render words are
hand-authored from the kit's timings and the recorded m1_03 hearing pair).

Failure modes (written before the code; every one has a case below)
-------------------------------------------------------------------
Extraction / fact
 1. The model records the ask only as prose: the detector must key on the recorded fact
    ``facts["ask_if_unclear"]`` on ANY live row (the captions row, or a row of its own), never on
    words (KRI-667). No fact -> no question, captions byte-identical to today.
 2. The creator later answers in their own words: ``facts["unclear_answer"]`` ("omit", or the exact
    words they typed) stops the question and drives the render; ``"omit"`` leaves the word out.
Detection
 3. whisper-1 has no confidence: use disagreement between the two hearings; both agreeing -> no
    question (known gap: a shared wrong word still ships; the draft line says so honestly).
 4. The hearings are not comparable (different language, a hearing missing, too little in common,
    more than two spans): ask nothing, change nothing, and say so honestly in the draft line.
 5. Noise must not nag: fillers, punctuation, accents, digits, a word the creator listed to spell
    (whisper-1 writes "Creole sauce" for the listed "salsa criolla") are not unclear words.
Gate / question
 6. The question is asked before approval, the approval backstop blocks the card, and "Leave it out"
    plus the heard candidates are tappable (the card needs two or more options).
 7. "You decide" must never pick a word; the answer is scoped to the clip and the heard words, so it
    is neither asked again on every re-plan nor kept after the footage changes.
 8. The answer must survive approval -> binding digest -> dispatch -> writer (KRI-788 class).
 9. A question asked twice and never answered, or never asked at all (old planner, gate off), must
    not let the guess ship: the writer leaves the span out on its own (fail closed).
Render
10. Leaving a word out never retimes any other word or cue, never leaves an empty cue and keeps text
    and words in step; a typed/tapped replacement lands in the same window; the creator's spellings
    and the other hearing's guess never come back (the guess is checked in EVERY caption field).
Receipts
11. The ask row never reads "I can't verify this one automatically yet."; after the render the
    finished-video line says what was left out or written; no receipt quotes a heard guess.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    requirements_to_check_at_draft,
)
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.transcribe import Word
from app.services.choice_questions import answered_brief, tag_event
from app.services.clip_speech import SilenceDetectionResult
from app.tasks import generative_build as gb
from app.tasks.content_plan_build import _dispatch_item_render
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri849_dj_t1" / "dj_t1.json").read_text(
        encoding="utf-8"
    )
)
THREAD_ID = uuid.UUID(FIXTURE["thread_id"])
CREATOR_ID = uuid.UUID(FIXTURE["creator_id"])
ITEM_ID = uuid.UUID(FIXTURE["plan_item_id"])
TAKE = FIXTURE["take"]["media_id"]
TAKE_S = FIXTURE["take"]["duration_s"]
KIND = "unclear_word"
LEAVE_OUT = "leave_out"
# whisper-1's rendering of the third sauce, and the words each hearing offers the creator.
WHISPER_WORDS = ("salma", "y", "limón.")
GUESSES = tuple(FIXTURE["guess_tokens"])
# Where whisper-1 puts the third sauce on the take (the render words).
SPAN_S = (8.9, 9.7)
OLD_RECEIPT = "I can't verify this one automatically yet."


def prod_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "kria_runtime_v2_phone_user_ids": [],
        "guided_edit_capability_enabled": True,
        "subtitled_archetype_enabled": True,
        "phone_subtitled_rendering_enabled": True,
        "clip_intents_enabled": True,
        "clip_facts_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        "kria_plan_authority_enabled": True,
        "phone_render_verified_features": ["positionedText", "animatedText"],
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


# -- the recorded project -------------------------------------------------------------------------


def take_row(
    *, gemini: str | None = None, segments: list[dict] | None = None, drop_gemini: bool = False
) -> dict:
    """The take as the approved-media snapshot holds it (both hearings of the speech)."""
    row = copy.deepcopy(FIXTURE["take_row"])
    speech = row["analysis"]["understanding"]["speech"]
    if gemini is not None:
        speech["transcript"] = gemini
        row["analysis"]["transcript"] = gemini
    if drop_gemini:
        speech.pop("transcript", None)
        row["analysis"].pop("transcript", None)
    if segments is not None:
        speech["segments"] = segments
    return row


def item_for(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=ITEM_ID,
        clip_gcs_paths=[row["gcs_path"]],
        clip_assignments=[row],
        filming_guide=[],
        theme="",
        idea="Untitled video",
        edit_format="subtitled",
        audio_mode="kria",
        voiceover_gcs_path=None,
        landscape_fit="fit",
        montage_preset="classic",
        voiceover_bed_level=None,
        voiceover_caption_style=None,
        smart_sound_design_enabled=True,
        speech_cleanup_enabled=False,
        speech_cleanup_notice=None,
        current_job_id=None,
        edit_proposal=None,
    )


def brief_for(
    *,
    ask: bool = True,
    ask_row: str = "captions",
    unclear_answer: str | None = None,
    start_line: str | None = None,
    spellings: bool = True,
) -> CreativeBrief:
    """The brief the Main Creator records for the DJ T1 prompt (cassette
    ``kri849_dj_t1_ask_if_unclear.json``). ``ask_row="own"`` files the ask as a row of its own."""
    data = copy.deepcopy(FIXTURE["brief"])
    for requirement in data["requirements"]:
        facts = requirement["facts"]
        if requirement["id"] == "r2" and start_line is not None:
            facts["start_line"] = start_line
        if requirement["id"] != "r3":
            continue
        if not spellings:
            facts.pop("spellings", None)
        fact = facts.pop("ask_if_unclear")
        if ask and ask_row == "captions":
            facts["ask_if_unclear"] = fact
            if unclear_answer is not None:
                facts["unclear_answer"] = unclear_answer
        if ask and ask_row == "own":
            data["requirements"].append(
                {
                    "id": "r7",
                    "kind": "style",
                    "scope": "global",
                    "facts": {
                        "ask_if_unclear": fact,
                        **({"unclear_answer": unclear_answer} if unclear_answer else {}),
                    },
                    "literal": None,
                    "description": (
                        "I didn't catch the third sauce he names: if you can't tell what he says, "
                        "ask me instead of guessing"
                    ),
                    "status": "open",
                    "source_turn_id": requirement["source_turn_id"],
                }
            )
    return CreativeBrief.model_validate(data)


def strategy_for() -> dict:
    return copy.deepcopy(FIXTURE["strategy"])


def planned(strategy: dict, snapshot: dict) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply-strategy",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"summary": "", "strategy": copy.deepcopy(strategy)},
                ),
                KriaToolIntent(
                    intent_id="request-render",
                    tool_name="render.request",
                    tool_version=1,
                    depends_on=["apply-strategy"],
                ),
            ],
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        media_snapshot=snapshot,
        brief_route="replan",
        brief_clip_ids=(TAKE,),
    )


async def gate(
    monkeypatch: pytest.MonkeyPatch,
    brief: CreativeBrief,
    row: dict,
    events: list[tuple] | None = None,
) -> PlannedKriaTurn:
    """The server gate on the drafted strategy, with the chat so far (``events``)."""
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    return await planner._gate_unresolved_choices(
        SimpleNamespace(),
        planned(strategy_for(), snapshot_media(item_for(row))),
        thread_id=THREAD_ID,
        creator_id=CREATOR_ID,
    )


def asked(result: PlannedKriaTurn) -> dict | None:
    """The unclear-word question this gate result asks, if it asks one."""
    question = result.plan.choice_question
    return question if isinstance(question, dict) and question.get("kind") == KIND else None


def option(question: dict, key: str) -> dict:
    return next(o for o in question["options"] if o["key"] == key)


def heard_options(question: dict) -> list[dict]:
    return [o for o in question["options"] if o["key"] != LEAVE_OUT]


def question_event(question: dict, text: str = "") -> tuple[str, dict]:
    return tag_event("assistant", {"choice_question": question}, "assistant_response", text)


def tap_event(question: dict, key: str, turn: str = "t-tap") -> tuple[str, dict]:
    selection = {"question_id": question["question_id"], "option_key": key}
    return tag_event("user", {"turn_id": turn, "choice_selection": selection}, "user_message", key)


def message_event(text: str, turn: str) -> tuple[str, dict]:
    return tag_event("user", {"turn_id": turn}, "user_message", text)


def approval_blocker(strategy: dict, brief: CreativeBrief, row: dict, events: list) -> tuple | None:
    """The runtime's approval backstop: None = the approval card is offered."""
    from app.tasks.kria_runtime import _unresolved_choice_plan  # noqa: PLC0415

    return _unresolved_choice_plan(
        strategy,
        brief,
        snapshot_media(item_for(row)),
        contract_brief=brief,
        events=events,
        has_draft=True,
        creator_id=CREATOR_ID,
    )


async def answered(
    monkeypatch: pytest.MonkeyPatch,
    row: dict,
    key: str,
    *,
    brief: CreativeBrief | None = None,
) -> tuple[dict, CreativeBrief]:
    """Ask, tap ``key``, gate again: (the strategy approval would pin, the brief it pins)."""
    brief = brief or brief_for()
    first = await gate(monkeypatch, brief, row)
    question = asked(first)
    assert question is not None, first.plan.response
    events = [question_event(question), tap_event(question, key)]
    second = await gate(monkeypatch, brief, row, events)
    assert second.plan.mode == "act", second.plan.response
    strategy = copy.deepcopy(second.plan.intents[0].arguments["strategy"])
    return strategy, answered_brief(brief, strategy)


def approved_binding(
    row: dict, brief: CreativeBrief, strategy: dict | None = None, *, latest: str = "Leave it out"
) -> dict:
    """The BriefBinding approval pins: the answered brief, the media snapshot and the answers."""
    return BriefBinding.create(
        THREAD_ID,
        brief,
        latest_message=latest,
        full_creator_request=FIXTURE["creator_request"],
        media_snapshot=snapshot_media(item_for(row)),
        choice_answers=copy.deepcopy((strategy or {}).get("choice_answers") or []),
    ).model_dump(mode="json")


def dispatch(monkeypatch: pytest.MonkeyPatch, row: dict, strategy: dict, binding: dict):
    """``execute_kria_approval`` -> ``dispatch_item_render_for`` on the approved draft (the real
    ``_dispatch_item_render`` and job factory; only the broker and the session are stand-ins)."""
    item = item_for(row)
    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=CREATOR_ID, preference_summary="", ownership_epoch=0
    )
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.all.return_value = []
    session.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=uuid.UUID(binding["thread_id"]), state={"reply_language": "en"}
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.services.smart_captions.resolve_smart_captions_context_sync", return_value=None
            )
        )
        stack.enter_context(
            patch(
                "app.services.edit_proposals.validate_approved_proposal_media_sync",
                return_value=(None, None),
            )
        )
        stack.enter_context(patch("app.services.job_dispatch.enqueue_orchestrator_sync"))
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "natural and direct", "content_pillars": ["real-life moments"]},
            ownership_epoch=0,
            bypass_guided_edit_gate=True,
            allow_phone_unapproved_montage=True,
            phone_speech_cleanup_unattended=True,
            creator_strategy=copy.deepcopy(strategy),
            creator_request=binding["creator_request"],
            creator_brief_binding=binding,
        )
    assert result.outcome == "dispatched", result
    job = session.add.call_args.args[0]
    job.id = job.id or uuid.uuid4()
    return job


def render_words(*, drop: tuple[str, ...] = ()) -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
        for w in FIXTURE["render_words"]
        if w["text"] not in drop
    ]


def write_captions(
    monkeypatch: pytest.MonkeyPatch,
    binding: dict | None,
    *,
    words: list[Word] | None = None,
    dispatched=None,  # noqa: ANN001
    cut_words: list[Word] | None = None,
) -> dict:
    """The real phone Talking writer on the take's words with ``binding`` pinned on the job (the
    one the real dispatch built when ``dispatched`` is given). Returns the finished variant.

    ``cut_words``: the take as the speech-check preflight heard it (cleanup off, checked): the
    writer then runs the creator's quoted start line as an opening cut over THESE words (KRI-704),
    so the captions move with the cut."""
    job, snapshot, _session, source = _setup_subtitled(
        monkeypatch, duration_s=TAKE_S, transcript_words=words or render_words()
    )
    if cut_words is not None:
        snapshot["speech_cleanup_contract"] = "off_v1"
        snapshot["_speech_cleanup_internal"] = {
            "preflight_snapshot": checked_snapshot(source.proxy_path, cut_words)
        }
        job.assembly_plan["speech_cleanup_contract"] = "off_v1"
        job.assembly_plan["_speech_cleanup_internal"] = copy.deepcopy(
            snapshot["_speech_cleanup_internal"]
        )
    monkeypatch.setattr(settings, "subtitled_caption_correction_enabled", False)
    strategy = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "caption_style": "clean",
    }
    request = FIXTURE["creator_request"]
    if dispatched is not None:
        candidates = dispatched.all_candidates
        strategy = copy.deepcopy(candidates["creator_strategy"])
        request = candidates.get("creator_request") or request
        binding = dispatched.assembly_plan["creator_brief_binding"]
    job.all_candidates.update({"creator_request": request, "creator_strategy": strategy})
    if binding is not None:
        snapshot["creator_brief_binding"] = binding
        job.assembly_plan["creator_brief_binding"] = binding
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def checked_snapshot(storage_path: str, words: list[Word]) -> dict:
    """The speech check's finished ``checked_no_change`` snapshot over ``words`` (as in
    tests/tasks/test_phone_opening_cut_cleanup_off.py): the words the opening cut is grounded in."""
    from types import SimpleNamespace as NS  # noqa: PLC0415

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
        transcribe_fn=lambda *_a, **_k: NS(words=words, language="en", low_confidence=False),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
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


def caption_text(variant: dict) -> str:
    return " ".join(cue["text"] for cue in variant["caption_cues"])


def cue_words(variant: dict) -> list[dict]:
    return [word for cue in variant["caption_cues"] for word in cue.get("words") or []]


def guesses_in(value: object) -> list[str]:
    """Every guess token found anywhere in ``value`` (cue text, cue words, every variant field)."""
    blob = json.dumps(value, ensure_ascii=False).casefold()
    return [guess for guess in GUESSES if guess.casefold() in blob]


def overlaps(word: dict, window: tuple[float, float]) -> bool:
    return word["start_s"] < window[1] and word["end_s"] > window[0]


def assert_clean_and_untouched(variant: dict, baseline: dict, window: tuple[float, float]) -> None:
    """The finished captions hold none of the unclear words anywhere, no empty cue, and every
    word and cue outside the window keeps its text and its timing (10)."""
    assert guesses_in(variant) == [], guesses_in(variant)
    assert all(cue["text"].strip() and cue.get("words") for cue in variant["caption_cues"])
    for cue in variant["caption_cues"]:
        assert cue["text"].split() == [w["text"] for w in cue["words"]], cue
    kept = [(w["text"], w["start_s"], w["end_s"]) for w in cue_words(variant)]
    expected = [
        (w["text"], w["start_s"], w["end_s"])
        for w in cue_words(baseline)
        if not overlaps(w, window)
    ]
    assert kept == expected
    # A cue the omission did not touch is byte-identical, timing included.
    by_start = {cue["start_s"]: cue for cue in variant["caption_cues"]}
    for cue in baseline["caption_cues"]:
        if any(overlaps(w, window) for w in cue["words"]):
            continue
        assert by_start[cue["start_s"]] == cue


@pytest.fixture
def baseline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Today's writer on the same words with no unclear-word ask at all (the control)."""
    row = take_row()
    brief = brief_for(ask=False)
    return write_captions(monkeypatch, approved_binding(row, brief, latest="control"))


# == 0. the control: what ships today ========================


def test_today_the_guess_is_in_the_captions(baseline: dict) -> None:
    """The control (no ask recorded) prints whisper-1's rendering of the third sauce."""
    assert "limón" in caption_text(baseline)
    assert "chimichurri" in caption_text(baseline).casefold()  # the spelling list works (KRI-793)


# == 1. asked before approval ================================


async def test_kria_asks_about_the_third_sauce_before_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row, brief = take_row(), brief_for()
    result = await gate(monkeypatch, brief, row)

    question = asked(result)
    assert question is not None, (
        f"no question before approval; the plan says: {result.plan.response!r}"
    )
    assert result.plan.mode == "respond" and result.plan.turn_value == "question"
    keys = [o["key"] for o in question["options"]]
    assert (
        LEAVE_OUT in keys and len(keys) >= 3
    )  # "Leave it out" + both hearings, as the creator's choices
    assert option(question, LEAVE_OUT)["label"] == "Leave it out"
    labels = {o["label"].casefold() for o in heard_options(question)}
    assert labels == {"salma y limón", "sal marina"}
    assert question["allow_free_text"] is True
    assert "0:09" in result.plan.response and "leave it out" in result.plan.response.casefold()
    # Delegation never picks a word (7).
    from app.services.choice_questions import delegated_choice  # noqa: PLC0415

    assert delegated_choice(question, "you decide") is None

    # The approval backstop agrees: a draft with this open question is not approvable (6).
    blocked = approval_blocker(strategy_for(), brief, row, [])
    assert blocked is not None and blocked[0].choice_question["kind"] == KIND


async def test_the_ask_on_a_row_of_its_own_is_asked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """1: the model may file the ask as its own ``style`` row; the fact decides, not the row."""
    result = await gate(monkeypatch, brief_for(ask_row="own"), take_row())
    assert asked(result) is not None


async def test_the_question_is_not_asked_again_once_answered_or_for_new_footage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7: the answer is scoped to the clip and the heard words."""
    row, brief = take_row(), brief_for()
    question = asked(await gate(monkeypatch, brief, row))
    assert question is not None
    events = [question_event(question), tap_event(question, LEAVE_OUT)]

    replayed = await gate(monkeypatch, brief, row, events)  # a later re-plan of the same footage
    assert replayed.plan.mode == "act" and asked(replayed) is None

    other = take_row(gemini=FIXTURE["gemini_transcript"].replace("sal marina", "sal gruesa"))
    again = await gate(monkeypatch, brief, other, events)  # the footage was analysed differently
    assert asked(again) is not None


# == 2. the answer reaches the captions ======================


async def test_leave_it_out_reaches_the_real_writer_and_no_guess_ships(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    row = take_row()
    strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT)
    assert [a["kind"] for a in strategy["choice_answers"]] == [KIND]
    binding = approved_binding(row, final_brief, strategy)
    assert binding["choice_answers"] and BriefBinding.model_validate(binding)  # digest holds

    job = dispatch(monkeypatch, row, strategy, binding)
    variant = write_captions(monkeypatch, None, dispatched=job)

    assert_clean_and_untouched(variant, baseline, SPAN_S)
    text = caption_text(variant)
    assert "and" in text.split()  # what was said around the word stays
    assert "chimichurri" in text.casefold() and "Lucas" in text  # the spelling list still applies


async def test_tapping_a_heard_candidate_writes_the_creators_choice_not_the_other_guess(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    """The "use" path: the creator picks "sal marina" (a hearing they chose, in their own tap)."""
    row = take_row()
    question = asked(await gate(monkeypatch, brief_for(), row))
    assert question is not None
    pick = next(o for o in heard_options(question) if o["label"].casefold() == "sal marina")
    strategy, final_brief = await answered(monkeypatch, row, pick["key"])
    binding = approved_binding(row, final_brief, strategy, latest=pick["label"])
    variant = write_captions(
        monkeypatch, None, dispatched=dispatch(monkeypatch, row, strategy, binding)
    )

    text = caption_text(variant)
    assert "sal marina" in text
    assert guesses_in({"cues": variant["caption_cues"]}) == ["sal marina", "marina"]
    shown = [w for w in cue_words(variant) if overlaps(w, SPAN_S)]
    assert [w["text"].strip(".") for w in shown] == ["sal", "marina"]
    assert min(w["start_s"] for w in shown) >= SPAN_S[0] - 1e-6
    assert max(w["end_s"] for w in shown) <= SPAN_S[1] + 1e-6
    others = [
        (w["text"], w["start_s"], w["end_s"]) for w in cue_words(variant) if not overlaps(w, SPAN_S)
    ]
    assert others == [
        (w["text"], w["start_s"], w["end_s"])
        for w in cue_words(baseline)
        if not overlaps(w, SPAN_S)
    ]


async def test_the_creators_typed_leave_it_out_is_recorded_by_the_model_and_obeyed(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    """2 (typed path): the Main Creator records ``unclear_answer="omit"`` (cassette
    ``kri849_dj_t1_unclear_answer_omit.json``): no question, and the writer leaves the span out."""
    row = take_row()
    brief = brief_for(unclear_answer="omit")
    result = await gate(monkeypatch, brief, row)
    assert asked(result) is None and result.plan.mode == "act"
    strategy = copy.deepcopy(result.plan.intents[0].arguments["strategy"])
    assert approval_blocker(strategy, brief, row, []) is None

    binding = approved_binding(
        row, answered_brief(brief, strategy), strategy, latest="leave it out"
    )
    variant = write_captions(
        monkeypatch, None, dispatched=dispatch(monkeypatch, row, strategy, binding)
    )
    assert_clean_and_untouched(variant, baseline, SPAN_S)


async def test_the_creator_typing_the_word_writes_exactly_those_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2: ``unclear_answer`` = the exact words typed ("Sal de mar") lands in the window."""
    row = take_row()
    brief = brief_for(unclear_answer="sal de mar")
    result = await gate(monkeypatch, brief, row)
    assert asked(result) is None
    strategy = copy.deepcopy(result.plan.intents[0].arguments["strategy"])
    binding = approved_binding(row, brief, strategy, latest="sal de mar")
    variant = write_captions(monkeypatch, binding)
    text = caption_text(variant)
    assert "sal de mar" in text
    assert guesses_in(variant) == []


# == 3. fail closed ==========================================


async def test_a_question_asked_twice_and_never_answered_leaves_the_span_out(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    """9: the creator ignored the card twice; the plan passes through unchanged (nothing is chosen
    for them) and the writer still never prints the guess."""
    row, brief = take_row(), brief_for()
    question = asked(await gate(monkeypatch, brief, row))
    assert question is not None
    reasked = {**question, "question_id": str(uuid.uuid4())}
    events = [
        question_event(question),
        message_event("hmm", "t2"),
        question_event(reasked),
        message_event("just make it", "t3"),
    ]
    result = await gate(monkeypatch, brief, row, events)
    assert asked(result) is None and result.plan.mode == "act"
    strategy = copy.deepcopy(result.plan.intents[0].arguments["strategy"])
    assert not strategy.get("choice_answers")

    binding = approved_binding(
        row, answered_brief(brief, strategy), strategy, latest="just make it"
    )
    variant = write_captions(
        monkeypatch, None, dispatched=dispatch(monkeypatch, row, strategy, binding)
    )
    assert_clean_and_untouched(variant, baseline, SPAN_S)


def test_a_draft_that_was_never_asked_still_never_prints_the_guess(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    """9: the ask is on the pinned brief but no gate ran (flag off, older planner): fail closed."""
    row = take_row()
    variant = write_captions(monkeypatch, approved_binding(row, brief_for(), latest="go"))
    assert_clean_and_untouched(variant, baseline, SPAN_S)


# == 4. negative controls ====================================


async def test_no_ask_no_question_and_the_captions_are_what_main_prints_today(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    row, brief = take_row(), brief_for(ask=False)
    result = await gate(monkeypatch, brief, row)
    assert asked(result) is None and result.plan.mode == "act"
    again = write_captions(monkeypatch, approved_binding(row, brief, latest="go"))
    assert again["caption_cues"] == baseline["caption_cues"]
    assert "caption_unclear" not in again


async def test_hearings_that_agree_ask_nothing_and_change_nothing(
    monkeypatch: pytest.MonkeyPatch, baseline: dict
) -> None:
    """3 (known gap): both listens heard the same words, so there is nothing to ask."""
    agreeing = FIXTURE["gemini_transcript"].replace("sal marina", "salma y limón")
    row, brief = take_row(gemini=agreeing), brief_for()
    result = await gate(monkeypatch, brief, row)
    assert asked(result) is None and result.plan.mode == "act"
    variant = write_captions(monkeypatch, approved_binding(row, brief, latest="go"))
    assert variant["caption_cues"] == baseline["caption_cues"]


@pytest.mark.parametrize(
    "gemini",
    [
        pytest.param(
            "Adın ne? Lucas. Burada geleneksel Arjantin soslarımız var: salsa criolla, "
            "chimichurri ve sal marina. Teşekkürler. Şerefe!",
            id="other-language",
        ),
        pytest.param(None, id="no-second-hearing"),
        pytest.param(
            "Welcome to the show everyone, today we taste the sauces.", id="little-in-common"
        ),
    ],
)
async def test_hearings_that_cannot_be_compared_ask_nothing_and_touch_nothing(
    monkeypatch: pytest.MonkeyPatch, baseline: dict, gemini: str | None
) -> None:
    """4: never flood the creator with questions, never wipe captions on a bad comparison."""
    row = take_row(gemini=gemini, drop_gemini=gemini is None)
    brief = brief_for()
    result = await gate(monkeypatch, brief, row)
    assert asked(result) is None and result.plan.mode == "act"
    variant = write_captions(monkeypatch, approved_binding(row, brief, latest="go"))
    assert variant["caption_cues"] == baseline["caption_cues"]


async def test_a_word_the_creator_listed_is_not_an_unclear_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5: whisper-1 writes "Creole sauce" for the listed "salsa criolla" and "Chivichurri" for the
    listed "chimichurri": the creator told us how those read, so neither is a question."""
    result = await gate(monkeypatch, brief_for(), take_row())
    question = asked(result)
    assert question is not None
    shown = " ".join(o["label"] for o in question["options"]).casefold()
    assert "creole" not in shown and "chivichurri" not in shown and "criolla" not in shown
    assert "creole" not in result.plan.response.casefold()


# == 4b. typed replies and a Turkish chat =====================================================


async def typed_choice(message: str, question: dict) -> dict | None:
    """``submit_turn``'s free-text matcher over a chat whose last turn asked ``question``."""
    from app.kria.runtime import _free_text_choice_selection  # noqa: PLC0415

    rows = [("assistant", {"choice_question": question}, "assistant_response", "")]
    result = MagicMock()
    result.all.return_value = rows
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    derived = await _free_text_choice_selection(db, SimpleNamespace(id=THREAD_ID), message)
    return None if derived is None else derived[0].model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize(
    ("message", "option"),
    [
        pytest.param("Leave it out", LEAVE_OUT, id="label"),
        pytest.param("leave that word out.", LEAVE_OUT, id="alias"),
        pytest.param(
            "I don't know it either, leave that word out of the captions.",
            LEAVE_OUT,
            id="the-kits-own-answer",
        ),
        pytest.param("çıkar", LEAVE_OUT, id="turkish-alias"),
        pytest.param("sal marina", "use_2", id="a-heard-candidate-typed"),
        pytest.param("2", "use_1", id="list-number"),
        pytest.param("you decide", None, id="delegation-never-picks-a-word"),
        pytest.param("sen seç", None, id="turkish-delegation"),
        pytest.param("hmm let me think", None, id="anything-else-goes-to-the-model"),
    ],
)
async def test_a_typed_reply_answers_only_when_it_names_an_option(
    monkeypatch: pytest.MonkeyPatch, message: str, option: str | None
) -> None:
    question = asked(await gate(monkeypatch, brief_for(), take_row()))
    assert question is not None
    selection = await typed_choice(message, question)
    assert (selection or {}).get("option_key") == option


async def test_a_turkish_chat_is_asked_and_answered_in_turkish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415

    row, brief = take_row(), brief_for()
    with reply_language_for("tr"):
        result = await gate(monkeypatch, brief, row)
        question = asked(result)
        assert question is not None
        assert option(question, LEAVE_OUT)["label"] == "Çıkar"
        assert "0:09" in result.plan.response and "tahmin yürütmeyeceğim" in result.plan.response
        assert "leave it out" in option(question, LEAVE_OUT)["aliases"]  # the English label stays
        strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT)
        receipt = ask_receipt(draft_receipts(strategy, final_brief, row), final_brief)
        assert "Anlayamadığım kelimeyi çıkarırım" in receipt.reason and "0:09" in receipt.reason
        assert guesses_in(receipt.reason) == []


async def test_the_clock_wording_is_turkish_in_a_turkish_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415

    row = take_row()
    with reply_language_for("tr"):
        strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT)
        draft = ask_receipt(draft_receipts(strategy, final_brief, row), final_brief)
        assert "klibinde yaklaşık 0:09" in draft.reason
        binding = approved_binding(row, final_brief, strategy)
        variant = write_captions(
            monkeypatch, None, dispatched=dispatch(monkeypatch, row, strategy, binding)
        )
        facts = plan_facts_from_phone_variant(variant)
        finished = ask_receipt(
            {r.requirement_id: r for r in build_receipts(final_brief.live(), facts)}, final_brief
        )
        assert "Anlayamadığım kelimeyi çıkardım" in finished.reason
        assert "videoda 0:09" in finished.reason


# == 5. receipts =============================================


def draft_receipts(strategy: dict, brief: CreativeBrief, row: dict) -> dict:
    """The receipts a Talking draft shows before approval, built as ``_complete_draft_turn`` builds
    them (same facts helpers, same checker list)."""
    from app.tasks import kria_runtime  # noqa: PLC0415

    snapshot = snapshot_media(item_for(row))
    facts = dataclasses.replace(
        plan_facts_from_strategy(strategy),
        phone_talking=True,
        caption_words_at_render=True,
        take_duration_s=TAKE_S,
        unclear_words=kria_runtime._draft_unclear_words(brief, strategy, snapshot, item_for(row)),
    )
    checked = requirements_to_check_at_draft(
        brief.live(),
        creator_id=str(CREATOR_ID),
        strategy=strategy,
        item_edit_format="subtitled",
        clip_paths=(row["gcs_path"],),
    )
    return {r.requirement_id: r for r in build_receipts(checked, facts, include_unchecked=True)}


def ask_receipt(receipts: dict, brief: CreativeBrief):
    row = next(r for r in brief.live() if (r.facts or {}).get("ask_if_unclear"))
    return receipts[row.id]


@pytest.mark.parametrize("ask_row", ["captions", "own"])
async def test_the_draft_line_for_the_ask_is_a_real_line_not_the_old_placeholder(
    monkeypatch: pytest.MonkeyPatch, ask_row: str
) -> None:
    row = take_row()
    brief = brief_for(ask_row=ask_row)
    strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT, brief=brief)
    receipt = ask_receipt(draft_receipts(strategy, final_brief, row), final_brief)
    assert receipt.reason and OLD_RECEIPT not in receipt.reason
    assert "leave out" in receipt.reason.casefold()
    assert "around 0:09 in your clip" in receipt.reason  # the source clip's time
    assert guesses_in(receipt.reason) == []


async def test_the_draft_line_says_what_will_be_written_for_a_tapped_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = take_row()
    question = asked(await gate(monkeypatch, brief_for(), row))
    pick = next(o for o in heard_options(question) if o["label"].casefold() == "sal marina")
    strategy, final_brief = await answered(monkeypatch, row, pick["key"])
    receipt = ask_receipt(draft_receipts(strategy, final_brief, row), final_brief)
    assert OLD_RECEIPT not in receipt.reason
    assert "sal marina" in receipt.reason and "0:09" in receipt.reason  # the creator's own word


@pytest.mark.parametrize(
    ("gemini", "words"),
    [
        pytest.param(
            FIXTURE["gemini_transcript"].replace("sal marina", "salma y limón"),
            ("both listens heard the same words",),
            id="agree",
        ),
        pytest.param(
            "Welcome to the show everyone, today we taste the sauces.",
            ("couldn't double-check",),
            id="not-comparable",
        ),
    ],
)
async def test_the_draft_line_is_honest_when_nothing_could_be_compared(
    monkeypatch: pytest.MonkeyPatch, gemini: str, words: tuple[str, ...]
) -> None:
    row, brief = take_row(gemini=gemini), brief_for()
    receipt = ask_receipt(draft_receipts(strategy_for(), brief, row), brief)
    assert OLD_RECEIPT not in receipt.reason
    assert all(w in receipt.reason.casefold() for w in words), receipt.reason
    assert guesses_in(receipt.reason) == []


async def test_the_finished_video_line_says_what_was_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = take_row()
    strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT)
    binding = approved_binding(row, final_brief, strategy)
    variant = write_captions(
        monkeypatch, None, dispatched=dispatch(monkeypatch, row, strategy, binding)
    )
    facts = plan_facts_from_phone_variant(variant)
    receipts = {
        r.requirement_id: r
        for r in build_receipts(final_brief.live(), facts, include_unchecked=True)
    }
    receipt = ask_receipt(receipts, final_brief)
    assert OLD_RECEIPT not in (receipt.reason or "")
    assert "left out" in receipt.reason.casefold()
    assert "at 0:09 in the video" in receipt.reason  # the edited video's time
    assert receipt.status != "not_possible"
    assert all(guesses_in(r.reason or "") == [] for r in receipts.values())
    # The spelling check of the same row still passes: nothing the creator listed went missing.
    assert "spelled as you wrote" in receipts["r3"].reason


async def test_a_word_the_creator_chose_is_not_flagged_as_a_caption_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = take_row()
    question = asked(await gate(monkeypatch, brief_for(), row))
    pick = next(o for o in heard_options(question) if o["label"].casefold() == "sal marina")
    strategy, final_brief = await answered(monkeypatch, row, pick["key"])
    binding = approved_binding(row, final_brief, strategy, latest=pick["label"])
    variant = write_captions(
        monkeypatch, None, dispatched=dispatch(monkeypatch, row, strategy, binding)
    )
    receipts = build_receipts(
        final_brief.live(), plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    ask_id = next(q.id for q in final_brief.live() if (q.facts or {}).get("ask_if_unclear"))
    receipt = next(r for r in receipts if r.requirement_id == ask_id)
    assert "sal marina" in receipt.reason and receipt.status != "not_possible"
    assert "the captions write" not in " ".join(r.reason or "" for r in receipts).casefold()


# == 5b. the render never depends on the render pass being comparable (KRI-849 review) =========


def drifted_words(*, third_sauce: tuple[str, str] | None = None) -> list[Word]:
    """whisper-1 on a re-run (the 24 h cache expired): four words differ from the other hearing,
    so the render pass has too many unclear stretches to be compared. ``third_sauce`` = the two
    words the pass now hears for the third sauce ("sal", "marina"), else it hears the same."""
    swap = {"traditional": "classic", "Argentinian": "Latin", "yeah.": "yes.", "Thank": "Bless"}
    out: list[Word] = []
    for word in render_words():
        if third_sauce and word.text in WHISPER_WORDS:
            if word.text == WHISPER_WORDS[0]:
                first, second = third_sauce
                out += [
                    Word(text=first, start_s=word.start_s, end_s=9.3, confidence=1.0),
                    Word(text=second, start_s=9.3, end_s=9.7, confidence=1.0),
                ]
            continue
        text = swap.get(word.text, word.text)
        out.append(Word(text=text, start_s=word.start_s, end_s=word.end_s, confidence=1.0))
    return out


def render_state(words: list[Word]) -> str:
    from app.pipeline import caption_unclear  # noqa: PLC0415

    return caption_unclear.compare_words(
        words, FIXTURE["gemini_transcript"], spellings=("salsa criolla", "chimichurri")
    ).state


def test_the_drifted_render_pass_really_is_not_comparable() -> None:
    """The precondition of the cases below: a re-run that cannot be compared with Gemini's
    hearing (a comparable pass that hears the sauce the other way is the last case)."""
    assert render_state(drifted_words()) == "not_comparable"
    assert render_state(drifted_words(third_sauce=("sal", "marina"))) == "not_comparable"


def swapped_words() -> list[Word]:
    """The render pass hears the third sauce the way the other hearing did: comparable, agrees."""
    out: list[Word] = []
    for word in render_words():
        if word.text == WHISPER_WORDS[0]:
            out += [
                Word(text="sal", start_s=word.start_s, end_s=9.3, confidence=1.0),
                Word(text="marina.", start_s=9.3, end_s=9.7, confidence=1.0),
            ]
        elif word.text not in WHISPER_WORDS:
            out.append(word)
    return out


@pytest.mark.parametrize("pick", ["leave it out", "sal marina"])
async def test_a_tapped_answer_is_honoured_when_the_render_pass_cannot_be_compared(
    monkeypatch: pytest.MonkeyPatch, pick: str
) -> None:
    """Tap, approve, then whisper-1 is re-run at render and differs elsewhere: the creator's
    answer still lands on the word they were asked about (approval-time window, pinned)."""
    row = take_row()
    question = asked(await gate(monkeypatch, brief_for(), row))
    key = (
        LEAVE_OUT
        if pick == "leave it out"
        else next(o for o in heard_options(question) if o["label"].casefold() == pick)["key"]
    )
    strategy, final_brief = await answered(monkeypatch, row, key)
    binding = approved_binding(row, final_brief, strategy)
    variant = write_captions(
        monkeypatch,
        None,
        words=drifted_words(),
        dispatched=dispatch(monkeypatch, row, strategy, binding),
    )
    text = caption_text(variant)
    assert "salma" not in text and "limón" not in text
    assert "classic" in text and "Latin" in text  # the other drifted words are not touched
    if pick == "leave it out":
        assert guesses_in(variant) == []
    else:
        assert "sal marina" in text
    assert variant["caption_unclear"]["spans"][0]["action"] == (
        "omitted" if pick == "leave it out" else "written"
    )


async def test_a_typed_omit_is_honoured_when_the_render_pass_cannot_be_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row, brief = take_row(), brief_for(unclear_answer="omit")
    result = await gate(monkeypatch, brief, row)
    strategy = copy.deepcopy(result.plan.intents[0].arguments["strategy"])
    binding = approved_binding(row, brief, strategy, latest="leave it out")
    variant = write_captions(monkeypatch, binding, words=drifted_words())
    assert guesses_in(variant) == []
    assert variant["caption_unclear"]["spans"][0]["source"] == "model"


def test_an_unanswered_ask_leaves_the_approval_window_out_when_the_render_cannot_be_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never asked / ignored twice, and the render pass drifted: still no guess (fail closed)."""
    row = take_row()
    variant = write_captions(
        monkeypatch, approved_binding(row, brief_for(), latest="go"), words=drifted_words()
    )
    assert guesses_in(variant) == []
    assert variant["caption_unclear"]["spans"][0]["source"] == "unanswered"
    assert "classic" in caption_text(variant)


@pytest.mark.parametrize("answer", ["tap", "none"])
async def test_a_render_pass_that_hears_the_word_the_other_way_is_still_left_out(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """Comparable and now in agreement at the old window: the answer (or the fail-closed default)
    still holds: leaving it out is what was asked."""
    row = take_row()
    if answer == "tap":
        strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT)
        binding = approved_binding(row, final_brief, strategy)
    else:
        binding = approved_binding(row, brief_for(), latest="go")
    assert render_state(swapped_words()) == "agree"
    variant = write_captions(monkeypatch, binding, words=swapped_words())
    assert "sal" not in caption_text(variant).split() and "marina." not in caption_text(variant)
    assert variant["caption_unclear"]["state"] == "applied"


# -- an opening cut moves the captions: the answer must land in the cut video's time ------------

CUT_START = "Here we have traditional Argentinian sauces"


def clock(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


async def test_the_answer_lands_in_the_cut_video_not_the_source_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = take_row()
    control = brief_for(ask=False, start_line=CUT_START)
    base = write_captions(
        monkeypatch, approved_binding(row, control, latest="control"), cut_words=render_words()
    )
    assert base["opening_cut"]["status"] == "applied", base["opening_cut"]
    shifted = [w for w in cue_words(base) if w["text"] in WHISPER_WORDS]
    window = (min(w["start_s"] for w in shifted), max(w["end_s"] for w in shifted))
    assert window[0] < SPAN_S[0] - 3, "the start cut shifted the captions"

    brief = brief_for(start_line=CUT_START)
    strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT, brief=brief)
    draft = ask_receipt(draft_receipts(strategy, final_brief, row), final_brief)
    assert "around 0:09 in your clip" in draft.reason  # the source clip's time, before the cut
    binding = approved_binding(row, final_brief, strategy)
    variant = write_captions(
        monkeypatch,
        None,
        dispatched=dispatch(monkeypatch, row, strategy, binding),
        cut_words=render_words(),
    )
    assert_clean_and_untouched(variant, base, window)
    facts = plan_facts_from_phone_variant(variant)
    receipts = {r.requirement_id: r for r in build_receipts(final_brief.live(), facts)}
    finished = ask_receipt(receipts, final_brief)
    assert f"at {clock(window[0])} in the video" in finished.reason
    assert "0:09" not in finished.reason  # not the source clip's time


async def test_the_answer_lands_in_the_cut_video_when_the_render_pass_cannot_be_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = take_row()
    brief = brief_for(start_line="Here we have")
    strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT, brief=brief)
    binding = approved_binding(row, final_brief, strategy)
    variant = write_captions(
        monkeypatch,
        None,
        dispatched=dispatch(monkeypatch, row, strategy, binding),
        cut_words=drifted_words(),
    )
    assert variant["opening_cut"]["status"] == "applied"
    # Cut at 4.05 s: the render pass is comparable again (fewer words) and finds two stretches
    # the creator was not asked about; they are left out too (nothing is guessed), and the
    # stretch they WERE asked about is answered at its cut-video time (not the source's 8.8 s).
    assert guesses_in(variant) == []
    answered_rows = [r for r in variant["caption_unclear"]["spans"] if r["source"] == "answer"]
    assert len(answered_rows) == 1 and answered_rows[0]["start_s"] < 6.0


# -- a step that fails must not ship the guess silently ----------------------------------------


def boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("the unclear-word step broke")


async def test_a_failing_step_still_drops_the_answered_window_and_says_it_could_not_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria import unclear_word_ask  # noqa: PLC0415

    row = take_row()
    strategy, final_brief = await answered(monkeypatch, row, LEAVE_OUT)
    binding = approved_binding(row, final_brief, strategy)
    job = dispatch(monkeypatch, row, strategy, binding)
    monkeypatch.setattr(unclear_word_ask, "apply_to_cues", boom)

    variant = write_captions(monkeypatch, None, dispatched=job)  # the render does not fail

    assert guesses_in(variant) == []
    assert variant["caption_unclear"]["state"] == "error"
    receipts = build_receipts(
        final_brief.live(), plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    ask_id = next(q.id for q in final_brief.live() if (q.facts or {}).get("ask_if_unclear"))
    line = next(r for r in receipts if r.requirement_id == ask_id)
    assert line.status != "met" and "couldn't check" in line.reason.casefold()
    assert "left out" not in line.reason.casefold()  # it does not claim what it did not verify
    assert all(guesses_in(r.reason or "") == [] for r in receipts)


async def test_a_failing_step_with_nothing_readable_still_renders_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria import unclear_word_ask  # noqa: PLC0415

    row = take_row()
    binding = approved_binding(row, brief_for(), latest="go")
    monkeypatch.setattr(unclear_word_ask, "apply_to_cues", boom)
    monkeypatch.setattr(unclear_word_ask, "detect", boom)

    variant = write_captions(monkeypatch, binding)

    assert variant["caption_unclear"] == {"state": "error", "spans": []}
    receipts = build_receipts(
        brief_for().live(), plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    line = ask_receipt({r.requirement_id: r for r in receipts}, brief_for())
    assert line.status != "met" and "couldn't check" in line.reason.casefold()
    assert all(guesses_in(r.reason or "") == [] for r in receipts)


def test_the_quoted_spelling_asks_reach_the_unclear_word_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A word the creator quoted to spell ('Spell "Lucas" right') is as much theirs as one in the
    brief's list: the step gets the same list the spelling step uses."""
    from app.kria import unclear_word_ask  # noqa: PLC0415

    seen: list[tuple[str, ...]] = []
    real = unclear_word_ask.apply_to_cues

    def spy(cues, raw_binding, *, spellings=(), map_window=None):  # noqa: ANN001, ANN202
        seen.append(tuple(spellings))
        return real(cues, raw_binding, spellings=spellings, map_window=map_window)

    monkeypatch.setattr(unclear_word_ask, "apply_to_cues", spy)
    job, snapshot, _session, _source = _setup_subtitled(
        monkeypatch, duration_s=TAKE_S, transcript_words=render_words()
    )
    monkeypatch.setattr(settings, "subtitled_caption_correction_enabled", False)
    binding = approved_binding(take_row(), brief_for(), latest="go")
    job.all_candidates.update(
        {
            "creator_request": 'Spell "Lucasito" right. ' + FIXTURE["creator_request"],
            "creator_strategy": {
                "edit_format": "subtitled",
                "audio_strategy": "original_audio",
                "caption_style": "clean",
            },
        }
    )
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = binding
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert seen and "Lucasito" in seen[0] and "chimichurri" in seen[0]


# == 6. the detector's own edge cases (failure mode 5) ==========================


def spans(
    *,
    heard: str,
    other: str,
    spellings: tuple[str, ...] = (),
    window: tuple[float, float] | None = None,
):
    """``unclear_spans`` over one timed sentence ``heard`` (0-10 s, spread by characters) and the
    other hearing's untimed text ``other``."""
    from app.pipeline import caption_unclear  # noqa: PLC0415

    segments = [{"start_s": 0.0, "end_s": 10.0, "text": heard}]
    return caption_unclear.compare_segments(segments, other, spellings=spellings, window=window)


@pytest.mark.parametrize(
    ("heard", "other", "state"),
    [
        pytest.param("so we have limón", "so we have limon", "agree", id="accent"),
        pytest.param("we have Salsa, criolla!", "we have salsa criolla", "agree", id="punctuation"),
        pytest.param(
            "so um we have the sauce", "so uh uh we have the sauce", "agree", id="fillers"
        ),
        pytest.param("we have 15 sauces here", "we have fifteen sauces here", "agree", id="digits"),
        pytest.param(
            "we have chivichurri here", "we have chimichurri here", "agree", id="one-letter"
        ),
        pytest.param(
            "we have salma y limón here", "we have sal marina here", "unclear", id="the-case"
        ),
        # KRI-849 review: contractions and repetitions the two hearings write differently.
        pytest.param("it's a nice sauce here", "it is a nice sauce here", "agree", id="its"),
        pytest.param(
            "it is a nice sauce here", "it\u2019s a nice sauce here", "agree", id="its-rev"
        ),
        pytest.param("so I'm going to taste it", "so I am going to taste it", "agree", id="im"),
        pytest.param(
            "we're gonna taste every sauce",
            "we are going to taste every sauce",
            "agree",
            id="gonna",
        ),
        pytest.param(
            "I wanna taste every sauce here",
            "I want to taste every sauce here",
            "agree",
            id="wanna",
        ),
        pytest.param(
            "they're making the best sauce", "they are making the best sauce", "agree", id="theyre"
        ),
        pytest.param(
            "we don't hide the sauce here", "we do not hide the sauce here", "agree", id="dont"
        ),
        pytest.param(
            "okay okay okay we have sauces here", "okay we have sauces here", "agree", id="repeat"
        ),
        pytest.param(
            "we have no no no sauces here", "we have no sauces here", "agree", id="repeat-mid"
        ),
        pytest.param(
            "so you know we have sauces here today",
            "so we have sauces here today",
            "agree",
            id="you-know",
        ),
        pytest.param(
            "I mean we have sauces here today", "we have sauces here today", "agree", id="i-mean"
        ),
        pytest.param(
            "it was like really good sauce here",
            "it was really good sauce here",
            "agree",
            id="like",
        ),
        pytest.param(
            "so we have Lukas and the sauces", "so we have Lucas and the sauces", "agree", id="name"
        ),
        pytest.param(
            "one two three four five",
            "alpha beta gamma delta epsilon",
            "not_comparable",
            id="nothing-shared",
        ),
        pytest.param(
            "red blue green yellow purple orange",
            "red cyan green brown purple pink",
            "not_comparable",
            id="three-spans",
        ),
        pytest.param("", "we have sauces", "not_comparable", id="empty-timed"),
        pytest.param("we have sauces", "", "not_comparable", id="empty-other"),
    ],
)
def test_detector_edge_cases(heard: str, other: str, state: str) -> None:
    assert spans(heard=heard, other=other).state == state


def test_a_listed_spelling_is_never_an_unclear_word() -> None:
    result = spans(
        heard="here are Creole sauce and the chivichurri",
        other="here are salsa criolla and the chimichurri",
        spellings=("salsa criolla", "chimichurri"),
    )
    assert result.state == "agree"


def test_the_window_keeps_discarded_speech_out_of_the_question() -> None:
    heard = "we have salma y limón and then we say a lot of other things about the food"
    other = "we have sal marina and then we say a lot of other things about the food"
    both = spans(heard=heard, other=other)
    assert both.state == "unclear" and both.spans
    late = spans(heard=heard, other=other, window=(5.0, 10.0))  # the span sits before the window
    assert late.state == "agree"
