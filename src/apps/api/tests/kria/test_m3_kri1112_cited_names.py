"""Leverkusen M3 run 1 (KRI-1112, part 2/3): the clip-request resolver cites file names it was
never given, and a match whose evidence names ANOTHER clip is kept.

Prod thread 83f2dcaa (2026-10-31, Fly 7b5504165, TestFlight 1819). Five iPhone clips staged as
``<upload UUID>-IMG_10xx.mov`` (the app shows ``IMG_10xx``) plus the ticket photo. Resolver
aliases follow upload order: m001 = IMG_1039, m002 = IMG_1028, m003 = IMG_1037, m004 =
IMG_1036, m005 = IMG_1043. The resolver is never sent a file name (``IntentClip.file_name``),
only the creator's words, which at 13:38 listed "IMG_1028, IMG_1036, IMG_1037, IMG_1039,
IMG_1043". At 13:49:15 it matched "the match from kick-off to the end" to m001..m005 in alias
order at confidence 1.0, each with evidence "explicitly selected by creator in the order
'IMG_10xx'": m001 (IMG_1039) cited as 'IMG_1028', m002 (IMG_1028) as 'IMG_1036', m004
(IMG_1036) as 'IMG_1039'. ``resolve_clip_intents_for_turn`` kept all five: nothing checks
evidence that quotes a file name (KRI-763 checks only the internal ``m00N`` handles).

Ways this can fail, written before the fix (each is pinned below or named):

 F1 the incident: a match whose evidence quotes another clip's file name is kept.
 F2 the fix drops those matches silently: the ask resolves on the two clips whose citation
    happened to be right, and three clips the creator listed leave "the match" unasked.
 F3 the creator is asked, but the picker comes with the miscited clips ticked (KRI-991 ticks
    what the resolver put forward), so Done keeps the wrong mapping.
 F4 over-reach: a match whose evidence quotes its OWN clip's name, or no name at all, is
    dropped (control: every citation corrected).
 F5 a typed variant of a name ("img_1039.MOV", the iOS upload prefix) is not read as that name
    (``ClipNameIndex.references`` folds them, as the file-name step does).
 F6 a name two clips share is read as one of them (``ClipNameIndex`` names neither).
 F7 a word that only looks like a name ("2026", "IMG") is read as a clip name
    (``ClipNameIndex`` needs letters and digits).

Runs the clip-intent planning orchestration with the planner's row replayed through its real
parser and the REAL resolver with the recorded model output replayed. No model is called.
Fixture ``m3_kri1112_cited_names.json`` says what is recorded and what is reconstructed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.agents.clip_question import ClipQuestionOutput
from app.config import settings
from app.schemas.clip_understanding import UNDERSTANDING_KEY
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_file_names import clip_display_name
from app.services.clip_intent_resolution import IntentClip
from tests.agents.conftest import MockModelClient

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("m3_kri1112_cited_names.json").read_text()
)
NAME_OF = {c["media_id"]: clip_display_name(c["source_filename"]) for c in CASE["clips"]}
BY_NAME = {name: media_id for media_id, name in NAME_OF.items()}
BY_ALIAS = {c["alias"]: c for c in CASE["clips"]}
VIDEOS = [c["media_id"] for c in CASE["clips"] if c["kind"] == "video"]
TURN_C = CASE["turn_1349"]
FILE_NAME = re.compile(r"\bIMG_\d{4}\b", re.IGNORECASE)
CLIPS = [
    IntentClip(
        media_id=c["media_id"],
        kind=c["kind"],
        analysis={UNDERSTANDING_KEY: c["record"]},
        file_name=NAME_OF[c["media_id"]],
    )
    for c in CASE["clips"]
]


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # On in prod for this account: the thread's turns carried clip pickers.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


class _RecordedResolver:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text

    def invoke(self, **_kwargs: Any) -> ModelInvocation:
        return ModelInvocation(raw_text=self.raw_text)


async def _turn(monkeypatch: pytest.MonkeyPatch, raw_text: str = TURN_C["raw_text"]) -> Any:
    """The 13:49 turn's clip planning: the planner's row through its real parser, the real
    resolver with the recorded (or an edited) model output."""
    [recorded] = TURN_C["resolver_intents"]
    client = MockModelClient()
    client.queue(
        "gemini-2.5-flash",
        {
            "intents": [
                {
                    **{k: recorded[k] for k in ("intent_id", "attribute", "position")},
                    "op": TURN_C["planner_op"][recorded["intent_id"]],
                    "label_source": "clip",
                    "transcript_kind": None,
                    "creator_text": None,
                    "caption_attribute": None,
                    "placeholder": False,
                    "source_quote": "Montage of the match from kick-off to the end",
                }
            ],
            "question": None,
        },
    )
    monkeypatch.setattr(planning, "default_client", lambda: client)
    monkeypatch.setattr(resolution, "default_client", lambda: _RecordedResolver(raw_text))
    monkeypatch.setattr(resolution, "query_clip_vision", lambda *a, **k: ClipQuestionOutput())
    return await planning.plan_and_resolve_clip_intents(
        creator_request=TURN_C["creator_request"],
        latest_user_message="Animate the title",
        candidate_intents=None,
        clips=CLIPS,
        run_context=RunContext(),
    )


def test_the_recorded_evidence_cites_three_wrong_names() -> None:
    [block] = json.loads(TURN_C["raw_text"])["intents"]
    cited = [
        (NAME_OF[BY_ALIAS[a["media"]]["media_id"]], FILE_NAME.search(a["evidence"])[0])
        for a in block["assignments"]
    ]
    assert cited == [
        ("IMG_1039", "IMG_1028"),
        ("IMG_1028", "IMG_1036"),
        ("IMG_1037", "IMG_1037"),
        ("IMG_1036", "IMG_1039"),
        ("IMG_1043", "IMG_1043"),
    ]


@pytest.mark.asyncio
async def test_a_match_citing_another_clips_file_name_is_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 (incident C). No kept assignment may cite a file name the clip does not carry."""
    planned = await _turn(monkeypatch)
    miscited = [
        (NAME_OF[a.media_id], a.evidence)
        for intent in planned.resolution.intents
        if intent.status == "resolved"
        for a in intent.assignments
        if any(m.upper() != NAME_OF[a.media_id].upper() for m in FILE_NAME.findall(a.evidence))
    ]
    assert not miscited, miscited


@pytest.mark.asyncio
async def test_the_miscited_clips_are_asked_about_with_nothing_wrong_ticked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 / F3: the ask is not settled on the two lucky citations; the picker ticks only them
    and still offers every video."""
    resolved = (await _turn(monkeypatch)).resolution
    [intent] = [i for i in resolved.intents if i.intent_id == "order_kickoff_to_end"]
    assert intent.status == "needs_creator", (intent.status, intent.assignments)
    assert resolved.clip_question is not None, resolved.question
    [row] = resolved.clip_question["categories"]
    assert row["suggested_media_ids"] == [BY_NAME["IMG_1037"], BY_NAME["IMG_1043"]]
    assert set(VIDEOS) <= set(row["candidate_media_ids"])


@pytest.mark.asyncio
async def test_matches_citing_their_own_names_still_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4 / F5: the same answer with each clip citing its own name (as typed variants too) is
    kept whole, with no question."""
    raw = json.loads(TURN_C["raw_text"])
    spellings = [
        lambda clip: NAME_OF[clip["media_id"]],
        lambda clip: f"{NAME_OF[clip['media_id']].lower()}.MOV",
        lambda clip: clip["source_filename"],  # the iOS staged name, upload UUID included
        lambda clip: NAME_OF[clip["media_id"]],
        lambda clip: NAME_OF[clip["media_id"]],
    ]
    for assignment, spell in zip(raw["intents"][0]["assignments"], spellings, strict=True):
        own = spell(BY_ALIAS[assignment["media"]])
        assignment["evidence"] = f"explicitly selected by creator in the order '{own}'"
    resolved = (await _turn(monkeypatch, json.dumps(raw))).resolution
    [intent] = [i for i in resolved.intents if i.intent_id == "order_kickoff_to_end"]
    assert (intent.status, sorted(intent.media_ids())) == ("resolved", sorted(VIDEOS))
    assert resolved.question is None
