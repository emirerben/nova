"""KRI-844: Don Julio T2, an English quote of a Spanish take, replayed offline end to end.

Yasin's kit, 2026-10-21, ``T2_lucas_hook_photo_cards/PROMPT.txt``: "Cut my question and his
name so it opens straight on "Here we have traditional Argentinian sauces" ... End on the
"Thank you". ... Under 10 seconds." One 12.9 s phone Talking take (``T_lucas_sauces.mp4``) in
which Lucas answers in SPANISH: "Acá tenemos salsas tradicionales argentinas, salsas criollas,
chimichurri y sardinas ... Gracias". Both quotes were English, the take's words Spanish, so
neither line was found: the worker (`apply_take_window`) kept the whole 12.9 s, the draft card
showed no times, the finished card said Partly "I couldn't find ...", and nobody was asked.

``tests/fixtures/kri844_dj_t2_take.json`` holds the take's words (whisper.cpp medium, forced
Spanish; its provenance note says how they were timed and what the independent audio
measurement of the real clip is). This module drives the REAL chain on them, nothing in it
replaced but the Main Creator's draft (a literal strategy) and the thread's database reads:

    brief (timing r1: start_line / end_line, r2: duration_s 10)
      -> the planner's clarification gate (``planner._gate_unresolved_choices``)
      -> the creator's two taps (start line, end line)
      -> ``answered_brief`` (the copy approval pins)
      -> the draft card (``estimate_take_window`` + ``build_receipts``)
      -> the phone Talking worker's own cut (``generative_build._phone_opening_cut``).

Acceptance (the ticket, measured on the audio, not on the planner's own labels): the video
opens within 0.3 s before "Acá" (4.20 s) and ends within 0.5 s after "Gracias" (11.46 s) -- OR
the creator is asked to pick the lines before approval. The pre-fix code did neither: the gate
let the draft through (``mode == "act"``) and the worker kept 0.0-12.9 s.

``test_the_acceptance_window_rejects_the_wrong_lines`` is the negative control: the same
window check, fed the cut a wrong pick ("Lucas", then "salsas criollas,") makes, fails it.
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
from app.kria.brief_checks import PlanFacts, build_receipts
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.opening_cut_ask import estimate_take_window
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.services.choice_questions import answered_brief, tag_event
from app.services.speech_segments import attach_segments_to_analysis
from app.tasks import generative_build as gb

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri844_dj_t2_take.json").read_text(
        encoding="utf-8"
    )
)
WORDS = FIXTURE["words"]
TAKE_S = float(FIXTURE["clip"]["duration_s"])
MEASURED = FIXTURE["audio_measured_s"]
START_LINE = "Here we have traditional Argentinian sauces"
END_LINE = "Thank you"
CREATOR_ID = uuid.UUID("0f5b3c1e-0d4a-4a52-9a8e-5d3a3c1f7e11")
THREAD_ID = uuid.UUID("7c0a9e10-3b6d-4f0e-8e0a-2b1d6a9c4f22")
MEDIA_ID = "analysis-proxy-ios-DJ-T2-LUCAS.mp4"
GCS_PATH = f"users/{CREATOR_ID}/creation-threads/{THREAD_ID}/{MEDIA_ID}"

# The ticket's bar, in source seconds, from the independent audio measurement.
OPENS_BETWEEN = (MEASURED["aca_onset"] - 0.3, MEASURED["aca_onset"])
ENDS_BETWEEN = (MEASURED["gracias_end"], MEASURED["gracias_end"] + 0.5)


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


def t2_media() -> dict:
    """The media snapshot approval pins: one phone take whose record holds its Spanish speech
    segments, built the way clip understanding stores them (``attach_segments_to_analysis``)."""
    analysis = attach_segments_to_analysis(
        {
            "duration_s": TAKE_S,
            "understanding": {
                "kind": "video",
                "speech": {"has_speech": True, "to_camera": True},
            },
        },
        WORDS,
        FIXTURE["language"],
    )
    item = SimpleNamespace(
        clip_gcs_paths=[GCS_PATH],
        clip_assignments=[
            {
                "kind": "video",
                "media_id": MEDIA_ID,
                "gcs_path": GCS_PATH,
                "storage_generation": "1",
                "duration_s": TAKE_S,
                "has_audio": True,
                "analysis": analysis,
            }
        ],
    )
    return snapshot_media(item)


def t2_brief(*, with_length: bool = False) -> CreativeBrief:
    """What the brief writer files for the kit's prompt (KRI-789 shape, Main Creator v67).

    ``with_length`` adds the prompt's "Under 10 seconds" (r2). The clarification gate runs
    WITHOUT it: ``length_vs_natural`` asks its own question about that ask (it compares 10 s
    with the whole 12.9 s take, not with the part the lines keep -- KRI-847's, not this
    ticket's), and the question would only bury the ones under test. The draft card and the
    worker's cut are checked with it.
    """
    requirements = [
        BriefRequirement(
            id="r1",
            kind="timing",
            scope="global",
            description=f'Open straight on "{START_LINE}" and end on "{END_LINE}".',
            facts={"start_line": START_LINE, "end_line": END_LINE},
        )
    ]
    if with_length:
        requirements.append(
            BriefRequirement(
                id="r2",
                kind="timing",
                scope="global",
                description="Under 10 seconds.",
                facts={"duration_s": 10},
            )
        )
    return CreativeBrief(version=1, requirements=requirements)


def t2_strategy() -> dict:
    """The Main Creator's draft for the prompt: a phone Talking edit of the one take."""
    return {
        "pacing": "balanced",
        "direction": "native",
        "rationale": "A talking edit of Lucas explaining the sauces.",
        "edit_format": "subtitled",
        "media_scope": "selected",
        "caption_style": "auto",
        "audio_strategy": "original_audio",
        "render_program": "native",
        "target_duration_s": 10,
        "selected_media_ids": [MEDIA_ID],
        "video_reuse_policy": "once",
        "optional_treatments": [],
    }


def planned(strategy: dict) -> PlannedKriaTurn:
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
        media_snapshot=t2_media(),
        brief_route="replan",
        brief_clip_ids=(MEDIA_ID,),
    )


class Chat:
    """The thread as the server reads it: user turns, asked questions and the taps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief | None = None):
        self.brief = brief or t2_brief()
        self.events: list[tuple] = [
            tag_event("user", {"turn_id": "t1"}, "user_message", FIXTURE["prompt"])
        ]
        self.question: dict | None = None
        monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=self.brief))
        monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(side_effect=self._events))

    async def _events(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return list(self.events)

    async def turn(self) -> PlannedKriaTurn:
        """The clarification gate on the drafted strategy (one question per turn)."""
        result = await planner._gate_unresolved_choices(
            SimpleNamespace(), planned(t2_strategy()), thread_id=THREAD_ID, creator_id=CREATOR_ID
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

    def tap(self, fragment: str) -> str:
        """Tap the option whose label holds ``fragment``; returns its label."""
        assert self.question is not None, "no question is open"
        option = next(o for o in self.question["options"] if fragment in o["label"])
        self.events.append(
            tag_event(
                "user",
                {
                    "turn_id": f"t{len(self.events)}",
                    "choice_selection": {
                        "question_id": self.question["question_id"],
                        "option_key": option["key"],
                    },
                },
                "user_message",
                option["label"],
            )
        )
        return option["label"]


def labels(question: dict) -> list[str]:
    return [option["label"] for option in question["options"]]


async def answer_both(chat: Chat, start: str, end: str) -> dict:
    """Run the gate through the two questions and return the strategy it finally drafts."""
    first = await chat.turn()
    assert first.plan.mode == "respond", "the creator must be asked before the draft"
    chat.tap(start)
    second = await chat.turn()
    assert second.plan.mode == "respond"
    chat.tap(end)
    final = await chat.turn()
    assert final.plan.mode == "act", final.plan.response
    return copy.deepcopy(final.plan.intents[0].arguments["strategy"])


def worker_cut(strategy: dict) -> tuple[tuple[float, float], dict]:
    """What the phone Talking worker keeps of the take for the approved (answered) brief."""
    final_brief = answered_brief(t2_brief(with_length=True), strategy)
    binding = BriefBinding.create(
        THREAD_ID,
        final_brief,
        media_snapshot=t2_media(),
        choice_answers=list(strategy.get("choice_answers") or []),
    ).model_dump(mode="json")
    plan, receipts = gb._phone_opening_cut(
        {"creator_brief_binding": binding}, None, (), duration_s=TAKE_S, off_words=WORDS
    )
    if plan is None or not plan.keep_segments:
        return (0.0, TAKE_S), receipts or {}
    return (plan.keep_segments[0][0], plan.keep_segments[-1][1]), receipts or {}


def within_the_ticket_window(kept: tuple[float, float]) -> bool:
    return OPENS_BETWEEN[0] <= kept[0] <= OPENS_BETWEEN[1] and (
        ENDS_BETWEEN[0] <= kept[1] <= ENDS_BETWEEN[1]
    )


# -- the journey -----------------------------------------------------------------------------


async def test_the_creator_is_asked_which_lines_before_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    first = await chat.turn()
    assert first.plan.mode == "respond", "pre-fix: the draft went out with no question at all"
    question = first.plan.choice_question
    assert question["kind"] == "take_line_start"
    assert any(
        "Acá tenemos salsas tradicionales argentinas," in label for label in labels(question)
    )
    assert not any("Lucas Acá" in label for label in labels(question)), "name glued to the line"
    assert START_LINE in (first.plan.response or "")
    recommended = [o["label"] for o in question["options"] if o.get("recommended")]
    assert len(recommended) == 1 and "Acá tenemos" in recommended[0]

    chat.tap("Acá tenemos salsas tradicionales argentinas,")
    second = await chat.turn()
    assert second.plan.mode == "respond"
    assert second.plan.choice_question["kind"] == "take_line_end"
    end_labels = labels(second.plan.choice_question)
    assert any("Gracias" in label for label in end_labels)
    assert not any("Lucas" in label or "Qué te llaman" in label for label in end_labels)


async def test_the_picks_cut_the_take_where_the_ticket_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    strategy = await answer_both(chat, "Acá tenemos salsas tradicionales argentinas,", "Gracias")

    kept, receipts = worker_cut(strategy)
    assert within_the_ticket_window(kept), kept
    assert kept[1] - kept[0] < 10.0, "the creator asked for under 10 seconds"
    assert {r["status"] for r in receipts.values()} == {"applied"}
    # The creator's own words stay the receipt's quote; the pick is only what was grounded.
    assert receipts["opening_cut"]["quote"] == START_LINE
    assert receipts["end_cut"]["quote"] == END_LINE


async def test_the_draft_card_places_both_lines_before_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    strategy = await answer_both(chat, "Acá tenemos salsas tradicionales argentinas,", "Gracias")
    final_brief = answered_brief(t2_brief(with_length=True), strategy)

    estimate = estimate_take_window(final_brief.live(), t2_media(), strategy=strategy)
    assert estimate is not None
    assert estimate.start_s is not None and estimate.end_s is not None
    assert estimate.kept_s is not None and estimate.kept_s < 10.0
    facts = PlanFacts(
        edit_format="subtitled", phone_talking=True, take_duration_s=TAKE_S, take_window=estimate
    )
    with reply_language_for("en"):
        receipts = {
            r.requirement_id: r
            for r in build_receipts(
                final_brief.live(), facts, include_unchecked=True, strict_order=True
            )
        }
    lines = receipts["r1"].reason or ""
    assert f"“{START_LINE}” (0:" in lines and f"“{END_LINE}” (0:" in lines
    assert "whole take" not in (receipts["r2"].reason or "")


async def test_declining_both_questions_leaves_the_honest_not_found_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = Chat(monkeypatch)
    strategy = await answer_both(chat, "None of these", "None of these")
    kept, receipts = worker_cut(strategy)
    assert kept == (0.0, TAKE_S)
    assert {r["reason"] for r in receipts.values()} == {"not_found"}
    assert {r["quote"] for r in receipts.values()} == {START_LINE, END_LINE}


async def test_a_turkish_chat_is_asked_in_turkish(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat(monkeypatch)
    with reply_language_for("tr"):
        first = await chat.turn()
    assert first.plan.mode == "respond"
    assert "bulamadım" in (first.plan.response or "")
    assert "Hangi cümleyle açılsın?" in (first.plan.response or "")


def test_the_acceptance_window_rejects_the_wrong_lines() -> None:
    """Negative control: a cut that opens on the NAME (the glued-line bug) or ends early fails
    the very check the right picks pass."""
    from app.pipeline.phone_opening_cut import apply_take_window  # noqa: PLC0415
    from app.pipeline.silence_cut import no_op_plan  # noqa: PLC0415

    def cut(start_spoken: str, end_spoken: str) -> tuple[float, float]:
        plan, _start, _end = apply_take_window(
            no_op_plan(TAKE_S),
            WORDS,
            start_quote=START_LINE,
            end_quote=END_LINE,
            duration_s=TAKE_S,
            start_spoken=start_spoken,
            end_spoken=end_spoken,
        )
        return plan.keep_segments[0][0], plan.keep_segments[-1][1]

    assert within_the_ticket_window(cut("Acá tenemos salsas tradicionales argentinas,", "Gracias"))
    assert not within_the_ticket_window(cut("Lucas", "Gracias"))
    assert not within_the_ticket_window(cut("salsas criollas,", "Gracias"))
    assert not within_the_ticket_window(
        cut("Acá tenemos salsas tradicionales argentinas,", "chivichurros y sardines")
    )
