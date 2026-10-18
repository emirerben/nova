"""KRI-692: "show '20 dk' when I say 'yirmi dakika'" is never a clip-picker question.

Tigre night N1 (thread 2EA478EC, plan item bfe181bf, 2026-10-15): a phone Voiceover edit
(`narrated_planned` + a recorded voiceover) in a Turkish chat. The brief kept the ask as a
text line with a spoken trigger (`[r6] [text/global] ekrana '20 dk' yazısı gelsin ("20 dk")
facts={"triggers":["yirmi dakika"]}`). The clip planner ALSO minted it as a `caption` clip
intent (`caption_yirmi_dakika`, 0 candidates), so the picker asked "which of your clips show
the moment 'yirmi dakika' is said": a question with no right answer (a moment in the
voiceover is not a clip), worded in English inside a Turkish chat. Answering "none" would
drop the intent and the text would vanish without a word.

Ways this can go wrong (written before the fix):

1. The spoken-word text still reaches the picker on a voiceover edit (the bug).
2. The picker stops asking about real clips: the lights-photo question beside it goes too.
3. The text disappears silently: the draft carries no word about it. (KRI-699's receipt
   must still say a voiceover edit can't time text to a spoken word, in Turkish here.)
4. The drop is too wide: a chapter caption whose text only RESEMBLES the cue literal, a
   label or an include that mentions it, an authored caption (no exact text), a title that
   names a spoken cue, or a superseded ask is dropped too.
5. The drop leaks to another format: a montage (no voiceover) loses its caption question.
6. A Turkish chat still shows an English model-written label in the picker.
7. An English chat, or a label already in Turkish, is rewritten (must stay byte-identical).
8. The brief is off / has no spoken text: something is dropped anyway.

Replayed from the real N1 brief lines, message and intent shape. Only the model calls are
stand-ins (clip-intent planner output, resolver output); the parse, the resolver, the picker
builder and the Voiceover planner are the real ones. No network, no paid model call.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.agents.clip_question import ClipQuestionAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverIntentOut,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_strategy, spoken_text_literals
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as planning
from app.services.clip_intent_planning import (
    plan_and_resolve_clip_intents,
    without_spoken_text_captions,
)
from app.services.clip_intent_resolution import IntentClip

# A real Turkish phone Voiceover turn (`narrated_planned` + a recorded voiceover); the N1
# message, brief and clip intents below replace its words.
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri593_tigre_voiceover_turn.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MANIFEST = CASE["main_creator_input"]["capability_manifest"]
SUBJECTS: dict[str, str] = CASE["clip_subjects"]

N1_MESSAGE = (
    'Başlık: "Atakan\'ı almaya". "Sergio" dediğimde kaptanla iskeledeki klibi göster. '
    '"çalışmadı" dediğimde teknenin çalışmadığı klibi göster ve o klipte "tekne çalışmıyor" '
    'dediğim ses seslendirmenin altında duyulsun. "ışıklar" dediğimde ışıklı ev fotoğrafını '
    'göster. "yirmi dakika" dediğimde ekrana "20 dk" yazısı gelsin. "lunapark" dediğimde '
    "lunapark klibi. En son tekneyi bağladığımız klip olsun. Türkçe altyazı, kelime kelime."
)

# The live lines of N1's brief (proposal_trace "Creative brief v1"), trimmed to what matters.
R_TITLE = BriefRequirement(
    id="r1", kind="text", scope="title", literal="Atakan'ı almaya", description="başlık"
)
R_SERGIO = BriefRequirement(
    id="r2",
    kind="timing",
    scope="per_clip",
    description="kaptanla iskeledeki klip 'Sergio' denildiğinde gösterilsin",
    facts={"triggers": ["Sergio"]},
)
R_SPOKEN_TEXT = BriefRequirement(
    id="r6",
    kind="text",
    scope="global",
    literal="20 dk",
    description="ekrana '20 dk' yazısı gelsin",
    facts={"triggers": ["yirmi dakika"]},
)
N1_BRIEF = CreativeBrief(version=1, requirements=[R_TITLE, R_SERGIO, R_SPOKEN_TEXT])

# What the clip-intent planner returned for N1 (intent_stats: `caption_yirmi_dakika`,
# `include_lights_photo`, 0 candidates each). The caption's label was the model's own
# English phrase, though the prompt asks for the creator's words in a Turkish chat.
LIGHTS_PHOTO = {
    "intent_id": "include_lights_photo",
    "op": "include",
    "attribute": "ışıklı ev fotoğrafını",
    "source_quote": '"ışıklar" dediğimde ışıklı ev fotoğrafını göster',
}
YIRMI_DAKIKA = {
    "intent_id": "caption_yirmi_dakika",
    "op": "caption",
    "attribute": "the moment 'yirmi dakika' is said",
    "creator_text": "20 dk",
    "caption_attribute": None,
    "source_quote": '"yirmi dakika" dediğimde ekrana "20 dk" yazısı gelsin',
}
ENGLISH_LABEL = "the moment 'yirmi dakika' is said"
SPOKEN_TEXT_REASON_TR = "Seslendirmeli düzenleme henüz söylediğin bir kelimede yazı gösteremiyor."


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"subject": SUBJECTS[m["media_id"]]},
            gcs_path=f"users/u/clips/{m['media_id']}",
        )
        for m in MANIFEST["media"]
    ]


def _replay_models(monkeypatch: pytest.MonkeyPatch, *raw_intents: dict[str, Any]) -> None:
    """The REAL clip-intent parse and resolver over recorded model output; nothing is asked."""

    def _plan(self, input, *, ctx=None):  # noqa: A002, ANN001
        return self.parse(json.dumps({"intents": list(raw_intents), "question": None}), input)

    def _resolve(self, input, *, ctx=None):  # noqa: A002, ANN001
        # Nothing matched: 0 candidates for every intent, as in the N1 diagnostics.
        return ClipRequestResolverOutput(
            intents=[ResolverIntentOut(intent_id=raw["intent_id"]) for raw in raw_intents]
        )

    def _no_vision(self, input, *, ctx=None):  # noqa: A002, ANN001
        raise AssertionError("an unmatched ask never pays for a vision check")

    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _plan)
    monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolve)
    monkeypatch.setattr(ClipQuestionAgent, "run", _no_vision)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)


@pytest.fixture
def turkish_chat():
    token = bind_reply_language("tr")
    try:
        yield
    finally:
        release_reply_language(token)


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    raw_intents: list[dict[str, Any]],
    *,
    brief: CreativeBrief | None = N1_BRIEF,
    strategy_update: dict[str, Any] | None = None,
):
    """One recorded Voiceover planning turn through the REAL `_plan_from_creator_output`."""
    data = copy.deepcopy(CASE["main_creator_input"])
    data["user_message"] = N1_MESSAGE
    data["conversation"] = [
        data["conversation"][0],
        {"role": "user", "content": N1_MESSAGE},
    ]
    agent_input = MainCreatorInput.model_validate(data)
    strategy = {
        **copy.deepcopy(CASE["model_strategy"]),
        "clip_intents": [],
        "opening_title": None,
        **(strategy_update or {}),
    }
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": CASE["model_summary"],
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    _replay_models(monkeypatch, *raw_intents)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(MANIFEST["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=N1_MESSAGE,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_clips(),
            creator_request=None,
            raw_creator_request=N1_MESSAGE,
        ),
        output=output,
        brief_request=CASE["main_creator_input"]["creator_request"],
        # As `_plan_live_turn` derives it from the live brief.
        spoken_text=spoken_text_literals(brief),
    )


# -- 1, 2: the journey --------------------------------------------------------------------


async def test_n1_spoken_text_is_not_a_picker_category_but_real_clip_asks_stay(
    prod_profile, monkeypatch, turkish_chat
) -> None:
    planned = await _turn(monkeypatch, [LIGHTS_PHOTO, YIRMI_DAKIKA])

    assert planned.plan.mode == "respond"
    labels = [c["label"] for c in planned.plan.clip_question["categories"]]
    # Only the real clip ask is asked about, in the creator's words.
    assert labels == ["ışıklı ev fotoğrafını"]
    assert "yirmi dakika" not in planned.plan.response
    assert ENGLISH_LABEL not in planned.plan.response


async def test_n1_the_draft_continues_and_the_receipt_says_why_the_text_is_missing(
    prod_profile, monkeypatch, turkish_chat
) -> None:
    planned = await _turn(monkeypatch, [YIRMI_DAKIKA])

    assert planned.plan.mode == "act", planned.plan.response
    strategy = planned.plan.intents[0].arguments["strategy"]
    assert [i["intent_id"] for i in strategy.get("clip_intents") or []] == []
    # 3: the ask is not lost; the brief receipt tells the creator, in Turkish, why.
    (receipt,) = build_receipts(
        [R_SPOKEN_TEXT], plan_facts_from_strategy(strategy), include_unchecked=True
    )
    assert receipt.status == "not_possible"
    assert receipt.reason == SPOKEN_TEXT_REASON_TR


async def test_a_montage_keeps_its_caption_question(
    prod_profile, monkeypatch, turkish_chat
) -> None:
    # 5: only a Voiceover edit has no way to draw a text on a spoken word.
    planned = await _turn(
        monkeypatch,
        [LIGHTS_PHOTO, YIRMI_DAKIKA],
        strategy_update={
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "reaction_beats": [],
        },
    )

    assert planned.plan.mode == "respond"
    ops = [c["op"] for c in planned.plan.clip_question["categories"]]
    assert ops == ["include", "caption"]


async def test_a_brief_with_no_spoken_text_drops_nothing(
    prod_profile, monkeypatch, turkish_chat
) -> None:
    # 8: no brief / no spoken text ask -> the caption question is asked as before.
    for brief in (None, CreativeBrief(version=1, requirements=[R_TITLE, R_SERGIO])):
        planned = await _turn(monkeypatch, [LIGHTS_PHOTO, YIRMI_DAKIKA], brief=brief)
        assert [c["op"] for c in planned.plan.clip_question["categories"]] == ["include", "caption"]


# -- 6, 7: a Turkish chat's labels are the creator's words ---------------------------------


async def test_a_turkish_chat_never_shows_an_english_model_label(monkeypatch, turkish_chat) -> None:
    # The same N1 caption on a non-Voiceover edit still reaches the picker; its label is
    # now the creator's own sentence, not the model's English phrase.
    _replay_models(monkeypatch, LIGHTS_PHOTO, YIRMI_DAKIKA)
    planned = await plan_and_resolve_clip_intents(
        creator_request=N1_MESSAGE,
        latest_user_message=N1_MESSAGE,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
    )

    labels = [c["label"] for c in planned.resolution.clip_question["categories"]]
    assert labels == [
        "ışıklı ev fotoğrafını",
        '"yirmi dakika" dediğimde ekrana "20 dk" yazısı gelsin',
    ]
    assert ENGLISH_LABEL not in planned.resolution.question


def _parse(raw_intent: dict[str, Any], *, language: str | None, request: str = N1_MESSAGE):
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]
    out = agent.parse(
        json.dumps({"intents": [raw_intent], "question": None}),
        ClipIntentPlannerInput(
            creator_request=request, latest_user_message=request, reply_language=language
        ),
    )
    return out.intents[0]


def test_the_label_follows_the_chat_language_only_when_it_is_wrong() -> None:
    # English chat: byte-identical, whatever the model wrote.
    assert _parse(YIRMI_DAKIKA, language=None).attribute == ENGLISH_LABEL
    assert _parse(YIRMI_DAKIKA, language="en").attribute == ENGLISH_LABEL
    # Turkish chat: an English phrase becomes the creator's own words ...
    assert (
        _parse(YIRMI_DAKIKA, language="tr").attribute
        == '"yirmi dakika" dediğimde ekrana "20 dk" yazısı gelsin'
    )
    # ... and a label already in Turkish is left alone.
    assert _parse(LIGHTS_PHOTO, language="tr").attribute == "ışıklı ev fotoğrafını"


def test_a_long_creator_sentence_still_fits_the_label_bound() -> None:
    request = "Show the boat clip " + "and then the quiet river at night " * 8
    raw = {
        "intent_id": "include_boat",
        "op": "include",
        "attribute": "the boat clip",
        "source_quote": request,
    }
    # A Turkish chat quoting a long English sentence is out of scope here; the point is that
    # a replaced label never breaks the 160-character `attribute` bound.
    attribute = _parse(raw, language="tr", request=request).attribute
    assert 0 < len(attribute) <= 160
    assert request.startswith(attribute)


# -- 4, 8: precision of the drop and of the cue reader -------------------------------------


def _caption(text: str | None, **extra: Any) -> ClipIntent:
    return ClipIntent(
        intent_id=extra.pop("intent_id", "caption-1"),
        op="caption",
        attribute=extra.pop("attribute", "the park clips"),
        creator_text=text,
        caption_attribute=None if text else "the weather",
        **extra,
    )


def test_only_a_caption_with_the_exact_cue_text_is_dropped() -> None:
    spoken = _caption("20 dk", attribute=ENGLISH_LABEL)
    other_text = _caption("20 dk bekleme")
    authored = _caption(None)
    label = ClipIntent(intent_id="l", op="label", attribute="x", creator_text="20 dk")
    include = ClipIntent(intent_id="i", op="include", attribute="the 20 dk clip")

    kept = without_spoken_text_captions([spoken, other_text, authored, label, include], ("20 dk",))

    assert [i.intent_id for i in kept] == ["caption-1", "caption-1", "l", "i"]
    assert spoken not in kept
    # Case, spacing and the Turkish dotted/dotless i do not hide an exact match.
    assert without_spoken_text_captions([_caption("İlk  DURAK")], ("ilk durak",)) == []
    # No cue -> the very same list.
    intents = [spoken, include]
    assert without_spoken_text_captions(intents, ()) is intents


def test_the_cue_reader_takes_only_live_non_title_text_asks() -> None:
    title_with_cue = BriefRequirement(
        id="r7",
        kind="text",
        scope="title",
        literal="Üç kural",
        description="üç kural dediğimde başlık",
        facts={"triggers": ["üç kural"]},
    )
    superseded = R_SPOKEN_TEXT.model_copy(update={"id": "r8", "status": "superseded"})
    plain = BriefRequirement(id="r9", kind="text", scope="global", literal="Tigre 2026")

    assert spoken_text_literals(N1_BRIEF) == ("20 dk",)
    assert spoken_text_literals(CreativeBrief(version=1, requirements=[title_with_cue])) == ()
    assert spoken_text_literals(CreativeBrief(version=1, requirements=[superseded])) == ()
    assert spoken_text_literals(CreativeBrief(version=1, requirements=[plain])) == ()
    assert spoken_text_literals(None) == ()
