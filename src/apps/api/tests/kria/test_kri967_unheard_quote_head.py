"""KRI-967: Don Julio walk-in T1 -- the quoted start line's first words the timed transcript lost.

Yasin's kit, 2026-10-25 (``T1_atakan_tail_take_talking/PROMPT.txt``): "Start at my question
"Parrilla Don Julio, what do you think?" and end right after "Nice!" ... Spell "Parrilla Don
Julio" right ... About 7 seconds." One 26.9 s phone Talking take. Prod thread F0F3D5FB
(2026-10-26, ``a2426885d``): whisper auto-detected Turkish, the KRI-808 check re-transcribed it
in English, and the stored timed lines became "So," / "what do you think?" / "Are you excited
for the meet?" / "Super excited." / "Nice." -- "Parrilla Don Julio" came out as "So,". The
question asked which line to open on and RECOMMENDED "what do you think?"; the creator took it
and the video opened one line late (16.05 s), without the words they asked to open on.

Gemini's transcript of the same clip -- stored on the same record (``speech.transcript``) and
read by the KRI-808 check -- says "Parilla Don Julio. What do you think? ...": the head WAS said,
right before the tail, where whisper's only words are "So,".

``tests/fixtures/kri967_dj_walkin_t1_unheard_head.json`` holds the thread's real record (its
round-1 question digest is reproduced from it below), the real question and tap, the prod
receipts, the reference listener's word timings (an independent ASR) and the worker's word
timings (reconstructed from the prod caption cues; see its note). This module drives the REAL
chain on them -- nothing replaced but the Main Creator's draft (a literal strategy) and the
thread's database reads:

    brief r1 (start_line / end_line)
      -> the planner's clarification gate (``planner._gate_unresolved_choices``)
      -> the creator's tap
      -> ``answered_brief`` (the copy approval pins)
      -> the draft card (``estimate_take_window``)
      -> the phone Talking worker's own cut (``generative_build._phone_opening_cut``).

Acceptance (measured on independent sources, not on the picker's own lines): the video opens
within 0.3 s before the onset of "Parrilla" measured on the uploaded original's audio
(14.850-14.855 s; ``audio_measured_s``), and ends within [-0.1, +0.5] s of the reference
listener's "Nice." end. The pre-fix code recommended the tail line, which opens at 16.05 s
(prod), in the silence after the head -- outside the window.

Failure modes (F1-F12) are written in ``docs/reviews/kri-967-unheard-quote-head.md``.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.opening_cut_ask import estimate_take_window
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.pipeline.phone_opening_cut import apply_take_window
from app.pipeline.silence_cut import no_op_plan
from app.services.choice_questions import (
    CONFLICT_TAKE_LINE_END,
    CONFLICT_TAKE_LINE_START,
    MAX_CHOICE_OPTIONS,
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    choice_question_text,
    delegated_choice,
    resolve_choices,
    tag_event,
)
from app.tasks import generative_build as gb
from tests.services.test_choice_questions_turkish_guard import _ENGLISH, _QUOTED

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri967_dj_walkin_t1_unheard_head.json"
    ).read_text(encoding="utf-8")
)
TAKE = FIXTURE["take"]
SPEECH = TAKE["speech"]
TAKE_S = float(TAKE["duration_s"])
MEDIA_ID = TAKE["media_id"]
R1 = FIXTURE["brief_round1"]["requirements"][0]
QUOTE = R1["facts"]["start_line"]  # "Parrilla Don Julio, what do you think?"
END_QUOTE = R1["facts"]["end_line"]  # "Nice!"
PROD_QUESTION = FIXTURE["question_round1"]["choice_question"]
PROD_RECEIPTS = FIXTURE["prod_receipts"]
REFERENCE_WORDS = FIXTURE["reference_listener"]["words"]
WORKER_WORDS = FIXTURE["worker_words"]["words"]
STRATEGY = FIXTURE["strategy_round1"]["strategy"]
CREATOR_ID = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
THREAD_ID = uuid.UUID(FIXTURE["provenance"]["thread_id"])
GCS_PATH = f"users/{CREATOR_ID}/creation-threads/{THREAD_ID}/{MEDIA_ID}".lower()
CAP = ChoiceCapability(creator_id=CREATOR_ID)

# The acceptance window, from sources independent of the picker's lines: the audio of the
# uploaded original (where "Parrilla" starts) and the reference listener (where "Nice." ends).
HEAD_ONSET = FIXTURE["audio_measured_s"]["head_onset"]  # [14.85, 14.855]
NICE_END_S = next(w["end_s"] for w in REFERENCE_WORDS if w["text"].startswith("Nice"))
OPENS_BETWEEN = (HEAD_ONSET[0] - 0.3, HEAD_ONSET[1])
ENDS_BETWEEN = (NICE_END_S - 0.1, NICE_END_S + 0.5)
HEAD = "Parrilla Don Julio"
TAIL = "what do you think?"


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings this chat ran under: phone Talking pilot, brief binding, choice gate."""
    for name, value in {
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "subtitled_archetype_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


# -- the recorded project ------------------------------------------------------------------


def clip_row(speech: dict | None = None) -> dict:
    """The take as its clip record stores it (``understanding.speech`` verbatim from prod)."""
    speech = copy.deepcopy(SPEECH if speech is None else speech)
    return {
        "kind": "video",
        "media_id": MEDIA_ID,
        "gcs_path": GCS_PATH,
        "storage_generation": "1",
        "duration_s": TAKE_S,
        "has_audio": True,
        "analysis": {
            "duration_s": TAKE_S,
            "transcript": speech.get("transcript", ""),
            "understanding": {"kind": "video", "speech": speech},
        },
    }


def media(speech: dict | None = None) -> dict:
    """The media snapshot approval pins (``snapshot_media`` over the one-take item)."""
    return snapshot_media(
        SimpleNamespace(clip_gcs_paths=[GCS_PATH], clip_assignments=[clip_row(speech)])
    )


def snap(speech: dict | None = None) -> dict:
    return {"clip_paths": [GCS_PATH], "clip_assignments": [clip_row(speech)]}


def brief(start: str | None = QUOTE, end: str | None = END_QUOTE) -> CreativeBrief:
    facts = {k: v for k, v in (("start_line", start), ("end_line", end)) if v is not None}
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id=R1["id"],
                kind=R1["kind"],
                scope=R1["scope"],
                description=R1["description"],
                facts=facts,
            )
        ],
    )


def planned() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply-strategy",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"summary": "", "strategy": copy.deepcopy(STRATEGY)},
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
        media_snapshot=media(),
        brief_route="replan",
        brief_clip_ids=(MEDIA_ID,),
    )


class Chat:
    """The thread as the server reads it: the prompt, asked questions and the taps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.brief = brief()
        self.events: list[tuple] = [
            tag_event("user", {"turn_id": "t1"}, "user_message", FIXTURE["prompt"])
        ]
        self.question: dict | None = None
        monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=self.brief))
        monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(side_effect=self._events))

    async def _events(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return list(self.events)

    async def turn(self):
        result = await planner._gate_unresolved_choices(
            SimpleNamespace(), planned(), thread_id=THREAD_ID, creator_id=CREATOR_ID
        )
        self.question = result.plan.choice_question
        if self.question is not None:
            self.events.append(
                tag_event(
                    "assistant",
                    {"choice_question": self.question},
                    "assistant_question",
                    result.plan.response,
                )
            )
        return result

    def tap_key(self, key: str) -> None:
        assert self.question is not None, "no question is open"
        option = next(o for o in self.question["options"] if o["key"] == key)
        self.events.append(
            tag_event(
                "user",
                {
                    "turn_id": f"t{len(self.events)}",
                    "choice_selection": {
                        "question_id": self.question["question_id"],
                        "option_key": key,
                    },
                },
                "user_message",
                option["label"],
            )
        )


def recommended_key(question: dict) -> str | None:
    keys = [o["key"] for o in question["options"] if o.get("recommended")]
    assert len(keys) <= 1
    return keys[0] if keys else None


async def answer_start(chat: Chat, key: str | None = None) -> dict:
    """Tap ``key`` (default: the recommended line) on the start question, settle any later
    question that is not about the take's lines with its first option, and return the strategy
    the gate finally lets through."""
    first = await chat.turn()
    assert first.plan.mode == "respond"
    assert chat.question is not None and chat.question["kind"] == CONFLICT_TAKE_LINE_START
    chat.tap_key(key or recommended_key(chat.question) or "")
    for _ in range(4):
        result = await chat.turn()
        if result.plan.mode == "act":
            return copy.deepcopy(result.plan.intents[0].arguments["strategy"])
        assert chat.question is not None
        assert chat.question["kind"] not in {CONFLICT_TAKE_LINE_START, CONFLICT_TAKE_LINE_END}
        # A later question (the length one, KRI-966's) is not under test here.
        chat.tap_key(chat.question["options"][0]["key"])
    raise AssertionError("the gate never let the draft through")


def worker_cut(strategy: dict, words: list[dict]) -> tuple[tuple[float, float], dict]:
    """What the phone Talking worker keeps of the take for the approved (answered) brief."""
    binding = BriefBinding.create(
        THREAD_ID,
        answered_brief(brief(), strategy),
        media_snapshot=media(),
        choice_answers=list(strategy.get("choice_answers") or []),
    ).model_dump(mode="json")
    plan, receipts = gb._phone_opening_cut(
        {"creator_brief_binding": binding}, None, (), duration_s=TAKE_S, off_words=words
    )
    if plan is None or not plan.keep_segments:
        return (0.0, TAKE_S), receipts or {}
    return (plan.keep_segments[0][0], plan.keep_segments[-1][1]), receipts or {}


def within_the_ticket_window(kept: tuple[float, float]) -> bool:
    return OPENS_BETWEEN[0] <= kept[0] <= OPENS_BETWEEN[1] and (
        ENDS_BETWEEN[0] <= kept[1] <= ENDS_BETWEEN[1]
    )


def start_answer(strategy: dict) -> dict:
    return next(
        a for a in strategy.get("choice_answers") or [] if a.get("kind") == CONFLICT_TAKE_LINE_START
    )


# -- the replay is the prod question ---------------------------------------------------------


async def test_the_replayed_question_is_the_prod_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same inputs (digest), same option keys, every prod alias still answers its option (F7)."""
    chat = Chat(monkeypatch)
    await chat.turn()
    question = chat.question
    assert question is not None and question["kind"] == CONFLICT_TAKE_LINE_START
    assert question["input_digest"] == PROD_QUESTION["input_digest"]
    assert [o["key"] for o in question["options"]] == [o["key"] for o in PROD_QUESTION["options"]]
    by_key = {o["key"]: o for o in question["options"]}
    for prod in PROD_QUESTION["options"]:
        assert set(prod["aliases"]) <= set(by_key[prod["key"]]["aliases"]), prod["key"]


# -- F1: the card recommends where the head was said, and says what it could not hear -------


async def test_the_card_recommends_opening_where_parrilla_don_julio_was_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    first = await chat.turn()
    question = chat.question
    assert question is not None
    # Prod recommended "what do you think?" (line_1): the edit opened one line late.
    assert recommended_key(question) == "line_0"
    head_option = next(o for o in question["options"] if o["key"] == "line_0")
    assert head_option["label"] == "“So, what do you think?”"
    assert HEAD in (head_option.get("description") or "")
    text = first.plan.response or ""
    assert f"“{HEAD}”" in text and f"“{TAIL}”" in text
    assert "“So”" in text, "the card says what the transcript has where the head was said"
    # "You choose" takes the head line too (F12).
    assert delegated_choice(question, "you choose") == "line_0"


async def test_the_recommended_pick_opens_the_draft_and_the_cut_at_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    strategy = await answer_start(chat)
    assert start_answer(strategy)["value"] == "So, what do you think?"

    estimate = estimate_take_window(answered_brief(brief(), strategy).live(), media())
    assert estimate is not None and estimate.start_s is not None and estimate.end_s is not None
    assert within_the_ticket_window((estimate.start_s, estimate.end_s)), estimate

    kept, receipts = worker_cut(strategy, WORKER_WORDS)
    assert within_the_ticket_window(kept), kept
    assert {r["status"] for r in receipts.values()} == {"applied"}
    # The creator's own words stay the receipt's quote; the pick is only what was grounded.
    assert receipts["opening_cut"]["quote"] == QUOTE
    assert receipts["opening_cut"]["spoken"] == "So, what do you think?"


def test_a_brief_rewritten_to_the_tapped_label_cuts_at_the_head_too() -> None:
    """Prod: after the tap the Main Creator rewrote r1's ``start_line`` to the tapped label
    (brief v4: "what do you think?"). The new label is a line the take holds whole, so that path
    asks nothing and still opens at the head, in the draft card and in the worker's cut."""
    label = "So, what do you think?"
    res = _gate(SPEECH, the_brief=brief(start=label))
    assert res.question is None or res.question.kind not in {
        CONFLICT_TAKE_LINE_START,
        CONFLICT_TAKE_LINE_END,
    }
    estimate = estimate_take_window(brief(start=label).live(), media())
    assert estimate is not None and estimate.start_s is not None and estimate.end_s is not None
    assert within_the_ticket_window((estimate.start_s, estimate.end_s)), estimate
    plan, start, _end = apply_take_window(
        no_op_plan(TAKE_S), WORKER_WORDS, start_quote=label, end_quote=END_QUOTE, duration_s=TAKE_S
    )
    assert start is not None and start["status"] == "applied"
    assert within_the_ticket_window((plan.keep_segments[0][0], plan.keep_segments[-1][1]))


def test_the_acceptance_window_rejects_the_prod_cut_and_other_wrong_openings() -> None:
    """Negative control: the window check the head pick passes rejects the prod cut (the tail
    line), the whole take, a later line and an opening in the street before anyone speaks."""
    prod = (PROD_RECEIPTS["opening_cut"]["start_s"], PROD_RECEIPTS["end_cut"]["end_s"])
    assert not within_the_ticket_window(prod)
    assert not within_the_ticket_window((0.0, TAKE_S))

    def cut(start_spoken: str) -> tuple[float, float]:
        plan, _start, _end = apply_take_window(
            no_op_plan(TAKE_S),
            WORKER_WORDS,
            start_quote=QUOTE,
            end_quote=END_QUOTE,
            duration_s=TAKE_S,
            start_spoken=start_spoken,
        )
        return plan.keep_segments[0][0], plan.keep_segments[-1][1]

    assert cut(TAIL) == pytest.approx(prod), "the reconstructed worker words replay prod's cut"
    assert within_the_ticket_window(cut("So, what do you think?"))
    assert not within_the_ticket_window(cut("Are you excited for the meet?"))
    assert not within_the_ticket_window((10.0, prod[1]))


# -- F7: the prod tap keeps its meaning ----------------------------------------------------


async def test_the_prod_tap_on_the_tail_line_keeps_its_meaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    strategy = await answer_start(chat, FIXTURE["answer_round1"]["choice_selection"]["option_key"])
    answer = start_answer(strategy)
    assert answer["value"] == TAIL
    assert answer["input_digest"] == PROD_QUESTION["input_digest"]
    kept, _receipts = worker_cut(strategy, WORKER_WORDS)
    assert kept == pytest.approx(
        (PROD_RECEIPTS["opening_cut"]["start_s"], PROD_RECEIPTS["end_cut"]["end_s"])
    )


# -- F6: a pin the worker's words do not hold degrades to today's cut, never further ---------


def _without_so(words: list[dict]) -> list[dict]:
    return [w for w in words if w["text"] != "So,"]


@pytest.mark.parametrize(
    "words",
    [
        pytest.param(_without_so(WORKER_WORDS), id="worker-words-without-so"),
        pytest.param(REFERENCE_WORDS, id="reference-listener-words"),
    ],
)
async def test_a_pin_the_worker_words_do_not_hold_never_opens_later_than_today_or_at_zero(
    monkeypatch: pytest.MonkeyPatch, words: list[dict]
) -> None:
    chat = Chat(monkeypatch)
    strategy = await answer_start(chat)
    kept, receipts = worker_cut(strategy, words)
    assert receipts["opening_cut"]["status"] == "applied"
    assert OPENS_BETWEEN[0] <= kept[0] <= PROD_RECEIPTS["opening_cut"]["start_s"] + 0.3, kept


# -- F2 / F11: no evidence the head was said there => no invented start ----------------------


def _speech(**changes) -> dict:
    speech = copy.deepcopy(SPEECH)
    speech.update(changes)
    return speech


def _gate(speech: dict, events=(), *, the_brief: CreativeBrief | None = None):
    data = copy.deepcopy(STRATEGY)
    return resolve_choices(
        data, answered_brief(the_brief or brief(), data), snap(speech), list(events), CAP
    )


def _rec_label(res) -> str | None:
    return next((o.label for o in res.question.options if o.recommended), None)


_SEGMENTS = SPEECH["segments"]
_FAR_SO = [
    {"text": "So,", "start_s": 10.0, "end_s": 10.3},
    {"text": "what do you think?", "start_s": 15.4, "end_s": 16.74},
    *_SEGMENTS[1:],
]


@pytest.mark.parametrize(
    "speech",
    [
        pytest.param(_speech(transcript=""), id="no-independent-transcript"),
        pytest.param(
            _speech(transcript=SPEECH["transcript"].replace("Parilla Don Julio. ", "")),
            id="reference-never-heard-the-head",
        ),
        pytest.param(
            _speech(transcript="Parilla Don Julio. We are here. What do you think? Super excited."),
            id="reference-heard-the-head-elsewhere",
        ),
        pytest.param(
            _speech(transcript="So. " + SPEECH["transcript"]),
            id="preceding-line-is-its-own-utterance",
        ),
        pytest.param(_speech(segments=_FAR_SO), id="preceding-line-far-away"),
        pytest.param(
            _speech(
                segments=[
                    {
                        "text": "So we walked all the way from the hotel, what do you think?",
                        "start_s": 12.0,
                        "end_s": 16.74,
                    },
                    *_SEGMENTS[1:],
                ]
            ),
            id="preceding-line-much-longer-than-the-head",
        ),
        pytest.param(
            _speech(
                segments=[
                    {"text": "Think, what do you think?", "start_s": 15.0, "end_s": 16.74},
                    *_SEGMENTS[1:],
                ]
            ),
            id="preceding-line-is-quote-words",
        ),
    ],
)
def test_without_evidence_the_head_was_said_there_the_tail_stays_recommended(
    speech: dict,
) -> None:
    res = _gate(speech)
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_START
    assert _rec_label(res) == f"“{TAIL}”"
    assert not any(o.description for o in res.question.options if o.key.startswith("line_"))
    assert not any(TAIL in o.label and o.label != f"“{TAIL}”" for o in res.question.options)


def test_without_evidence_the_card_still_names_the_words_it_could_not_find() -> None:
    res = _gate(_speech(transcript=""))
    text = choice_question_text(res.question.candidate())
    assert f"“{HEAD}”" in text and f"“{TAIL}”" in text
    assert "“So”" not in text


# -- F3 / F4: a found quote asks nothing; an unmatched one is asked exactly as before ---------


def test_a_quote_the_take_holds_asks_nothing() -> None:
    res = _gate(SPEECH, the_brief=brief(start=TAIL))
    assert res.question is None or res.question.kind not in {
        CONFLICT_TAKE_LINE_START,
        CONFLICT_TAKE_LINE_END,
    }


def test_a_quote_that_matches_no_line_is_asked_as_before() -> None:
    quote = "Gracias amigo, ¿qué tal la carne?"
    res = _gate(SPEECH, the_brief=brief(start=quote))
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_START
    assert res.question.intro == f"I couldn't find “{quote}” in what's said in your video."
    assert [o.label for o in res.question.options][:5] == [
        "“So,”",
        "“what do you think?”",
        "“Are you excited for the meet?”",
        "“Super excited.”",
        "“Nice.”",
    ]
    assert not any(o.description for o in res.question.options if o.key.startswith("line_"))


# -- F10: the head line stays offered when lines are capped ---------------------------------


def test_the_head_line_is_offered_even_when_more_lines_than_fit_look_like_the_quote() -> None:
    extra = [
        {"text": text, "start_s": 21.0 + i, "end_s": 21.8 + i}
        for i, text in enumerate(
            ["What a view!", "Do you like it?", "I think so.", "Julio's place."]
        )
    ]
    segments = [*_SEGMENTS[:4], *extra, _SEGMENTS[4]]
    res = _gate(_speech(segments=segments))
    labels = [o.label for o in res.question.options]
    assert len(labels) <= MAX_CHOICE_OPTIONS
    assert _rec_label(res) == "“So, what do you think?”"


# -- F5: the end side mirrors it (a heard head, an unheard tail of the end quote) -----------


def test_the_end_side_recommends_ending_after_the_unheard_tail() -> None:
    segments = [
        *_SEGMENTS[:3],
        {"text": "Nice, I tell you.", "start_s": 19.5, "end_s": 20.4},
        _SEGMENTS[4],
    ]
    transcript = SPEECH["transcript"].replace("Nice.", "Nice, Atakan!")
    speech = _speech(segments=segments, transcript=transcript)
    end_quote = "Super excited. Nice, Atakan!"
    res = _gate(speech, the_brief=brief(start=TAIL, end=end_quote))
    assert res.question is not None and res.question.kind == CONFLICT_TAKE_LINE_END
    assert _rec_label(res) == "“Nice, I tell you.”"
    text = choice_question_text(res.question.candidate())
    assert "“Atakan”" in text and "“I tell you”" in text

    question = build_choice_question(res.question.candidate())
    key = next(o.key for o in res.question.options if o.recommended)
    events = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {"choice_selection": {"question_id": question["question_id"], "option_key": key}},
        ),
    ]
    after = _gate(speech, events, the_brief=brief(start=TAIL, end=end_quote))
    answer = next(a for a in after.answers if a["kind"] == CONFLICT_TAKE_LINE_END)
    assert answer["value"].endswith("I tell you.")
    final = answered_brief(brief(start=TAIL, end=end_quote), after.strategy)
    estimate = estimate_take_window(final.live(), media(speech))
    assert estimate is not None and estimate.end_s is not None
    assert estimate.end_s >= 20.4 - 1e-6


# -- F8: Turkish ----------------------------------------------------------------------------


def test_a_turkish_chat_gets_the_whole_card_in_turkish() -> None:
    with reply_language_for("tr"):
        res = _gate(SPEECH)
        text = choice_question_text(res.question.candidate())
    head_option = next(o for o in res.question.options if o.recommended)
    for sentence in (text, head_option.description or ""):
        bare = _QUOTED.sub(" ", sentence).lower()
        leaked = {w.strip(".,;:!?—-") for w in bare.split()} & _ENGLISH
        assert not leaked, (leaked, sentence)
    assert f"“{HEAD}”" in text and "Hangi cümleyle açılsın?" in text
