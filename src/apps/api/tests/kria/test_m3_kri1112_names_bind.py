"""Leverkusen M3 run 1 (KRI-1112 part 3/3): clips the creator tied to a subject by file name in
an earlier sentence, and an order that replaces the earlier opening and ending.

Prod thread 83f2dcaa (2026-10-31, Fly 7b5504165, TestFlight 1819, protocol 25), Montage. Five
iPhone clips staged as ``<upload UUID>-IMG_10xx.mov`` (the app shows ``IMG_10xx``) plus the
ticket photo as a Visual. Resolver aliases follow upload order: m001 = IMG_1039, m002 =
IMG_1028, m003 = IMG_1037, m004 = IMG_1036, m005 = IMG_1043, m006 = the ticket.

 B. 13:22:58 "Leave the names and minutes out, just write "TOR!" on both goal clips. The goals
    are IMG_1037 and IMG_1039." Later turns' planner wrote the TOR caption's clips as "the two
    goal clips". The file-name step (KRI-614, ``apply_file_name_references``) reads only an
    intent's own words and the resolver never sees a file name, so at 13:30:42 Kria asked "I
    couldn't tell which of your clips show "the two goal clips"" with nothing ticked (recorded
    resolver output: no assignments), though the same two names settled the planner's other
    row for them ("IMG_1037 and IMG_1039").
 D. 13:38:18 "Use exactly this order: IMG_1028, IMG_1036, IMG_1037, IMG_1039, IMG_1043. This
    replaces my earlier order and ending requests." The Main Creator changed r1 to
    ``key: exact_sequence`` with that list, but brief v5 still held ``first_clip: "my ticket"``
    and the old ``last_clip``: a same-kind change that leaves a sticky fact out carries it back
    (``brief._carry_sticky_facts``, KRI-522), and those stale words held the complete list
    back (approval readiness and the render contract read them).

Ways this can fail, written before any fix (each is pinned below):

 F1 incident B: a description the creator tied to file names in an earlier sentence goes to
    the name-blind resolver.
 F2 case / extension variants ("img_1037.MOV") or the iOS upload UUID prefix
    ("94675A31-...-IMG_1037.mov") typed by the creator do not count as the name.
 F3 a stem two clips share (IMG_1037 uploaded twice) silently picks one.
 F4 a stem that matches no clip ("IMG_9999"), or a bare number ("1037"), pins something.
 F5 over-binding: a sentence that names a file for another subject ("End on the Nürnberg
    players thanking their fans (IMG_1043)") settles the goal caption.
 F6 over-binding: a sentence about more than the intent's subject ("The goal celebrations
    are ...", "IMG_1037 for the goal and IMG_1039 for the fans") settles it.
 F7 two sentences tie the subject to names (a correction): the earlier one wins silently.
 F8 incident D: an order change to another basis keeps the replaced opening and ending.
 F9 over-reach on D: a reworded order on the same basis (KRI-522, KRI-1068 F9), or a first
    basis given to a pin-only "start with X", loses the clip.

Runs the real clip-intent planning orchestration with the planner's output replayed through its
real parser (B), and the real brief merge over the recorded brief v4 (D). No model is called.
Fixture ``m3_kri1112_names_bind.json`` says what is recorded and what is reconstructed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import RunContext
from app.config import settings
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.schemas.clip_intents import FILE_NAME_EVIDENCE
from app.schemas.clip_understanding import UNDERSTANDING_KEY
from app.services import clip_intent_planning as planning
from app.services.clip_file_names import clip_display_name
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.clip_order_sequence import stated_anchors
from tests.agents.conftest import MockModelClient

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("m3_kri1112_names_bind.json").read_text(encoding="utf-8")
)
NAME_OF = {c["media_id"]: clip_display_name(c["source_filename"]) for c in CASE["clips"]}
BY_NAME = {name: media_id for media_id, name in NAME_OF.items()}
GOALS_SENTENCE = "The goals are IMG_1037 and IMG_1039."
GOALS = {BY_NAME["IMG_1037"], BY_NAME["IMG_1039"]}  # m003 + m001
TURN_B = CASE["turn_1330"]
CLIPS = [
    IntentClip(
        media_id=c["media_id"],
        kind=c["kind"],
        analysis={UNDERSTANDING_KEY: c["record"]},
        file_name=NAME_OF[c["media_id"]],
    )
    for c in CASE["clips"]
]
ENDING = "the Nürnberg players thanking their fans (IMG_1043)"


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # On in prod for this account: the turns carried a clip picker and the exclude op.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def test_fixture_names_are_what_the_app_shows() -> None:
    """The iOS staging UUID and extension are stripped; each goal name is one clip."""
    assert [c["alias"] for c in CASE["clips"] if NAME_OF[c["media_id"]] == "IMG_1039"] == ["m001"]
    assert [c["alias"] for c in CASE["clips"] if NAME_OF[c["media_id"]] == "IMG_1037"] == ["m003"]
    assert GOALS_SENTENCE in TURN_B["creator_request"]  # recorded


# == B: "The goals are IMG_1037 and IMG_1039." (13:22) -> "the two goal clips" (13:30) ==========


async def _plan_turn_b(
    monkeypatch: pytest.MonkeyPatch,
    *,
    goals_sentence: str = GOALS_SENTENCE,
    clips: list[IntentClip] = CLIPS,
) -> tuple[AsyncMock, Any]:
    """The 13:30 turn's planning, the planner's rows replayed through its real parser and
    every intent left for the resolver recorded (the resolver itself is name-blind).

    ``goals_sentence`` replaces the creator's 13:22 sentence; the planner's own file-name row
    for the same clips (the control) quotes it, as the parser requires."""
    caption, control = copy.deepcopy(TURN_B["planner_inventory"])
    control.update(
        attribute=goals_sentence.removeprefix("The goals are ").rstrip("."),
        source_quote=goals_sentence,
    )
    client = MockModelClient()
    client.queue("gemini-2.5-flash", {"intents": [caption, control], "question": None})
    monkeypatch.setattr(planning, "default_client", lambda: client)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(planning, "resolve_clip_intents_for_turn", resolver)
    planned = await planning.plan_and_resolve_clip_intents(
        creator_request=TURN_B["creator_request"].replace(GOALS_SENTENCE, goals_sentence),
        latest_user_message=TURN_B["latest_user_message"],
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
    )
    return resolver, planned


def _sent_to_resolver(resolver: AsyncMock) -> set[str]:
    return {i.intent_id for call in resolver.await_args_list for i in call.kwargs["intents"]}


def _settled(planned: Any, intent_id: str) -> tuple[set[str], set[str]]:
    rows = [i for i in planned.resolution.intents if i.intent_id == intent_id]
    if not rows or rows[0].status != "resolved":
        return set(), set()
    return {a.media_id for a in rows[0].assignments}, {a.evidence for a in rows[0].assignments}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goals_sentence",
    [
        GOALS_SENTENCE,
        "The goals are img_1037.MOV and IMG_1039.mov.",
        "The goals are 94675A31-3866-4B20-91ED-1D8AA65817A4-IMG_1037.mov and "
        "4D8ED9A9-ABE9-4060-A4D7-BFE3EB8A12C3-IMG_1039.mov.",
    ],
    ids=["recorded", "case-and-extension", "ios-upload-prefix"],
)
async def test_goal_clips_the_creator_named_by_file_are_settled_by_name(
    monkeypatch: pytest.MonkeyPatch, goals_sentence: str
) -> None:
    """F1 (incident B) + F2. The same two names settle the planner's "IMG_1037 and IMG_1039"
    row today; the caption the creator tied to them ("the two goal clips") must land on the
    same clips without the resolver, which never sees a file name."""
    resolver, planned = await _plan_turn_b(monkeypatch, goals_sentence=goals_sentence)
    assert _settled(planned, "mute_all_except_goals") == (GOALS, {FILE_NAME_EVIDENCE})
    assert "caption_goal_tor" not in _sent_to_resolver(resolver), (
        "13:30 again: the goal caption went to the name-blind resolver, which answered with no "
        f"clips (recorded output: {TURN_B['raw_text']!r:.120}) -> 'I couldn't tell which of "
        'your clips show "the two goal clips"\''
    )
    assert _settled(planned, "caption_goal_tor") == (GOALS, {FILE_NAME_EVIDENCE})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goals_sentence", "duplicate"),
    [
        ("The goals are IMG_1037 and IMG_1039.", True),
        ("The goals are IMG_1037 and IMG_9999.", False),
        ("The goals are 1037 and 1039.", False),
        ("End on the Nürnberg players thanking their fans (IMG_1043).", False),
        ("The goal celebrations are IMG_1037 and IMG_1039.", False),
        ("Use IMG_1037 for the goal and IMG_1039 for the fans.", False),
        ("The goals are IMG_1037 and IMG_1039. Sorry, the goals are IMG_1036 and IMG_1039.", False),
    ],
    ids=[
        "stem-shared-by-two-clips",
        "unknown-stem",
        "bare-numbers",
        "names-another-subject",
        "a-wider-subject",
        "two-subjects-in-one-sentence",
        "a-later-correction",
    ],
)
async def test_a_name_that_does_not_pin_the_goals_never_settles_the_caption(
    monkeypatch: pytest.MonkeyPatch, goals_sentence: str, duplicate: bool
) -> None:
    """F3 - F7. No silent pick when a name is shared, unknown, a bare number, tied to another
    or a wider subject, or when two sentences tie the goals to names: the resolver (or the
    picker) decides, as today."""
    clips = CLIPS
    if duplicate:
        clips = [
            *CLIPS,
            IntentClip(
                media_id="analysis-proxy-ios-00000000-0000-4000-8000-000000001037.mp4",
                kind="video",
                analysis={UNDERSTANDING_KEY: CASE["clips"][2]["record"]},
                file_name="IMG_1037",
            ),
        ]
    _resolver, planned = await _plan_turn_b(monkeypatch, goals_sentence=goals_sentence, clips=clips)
    _ids, evidence = _settled(planned, "caption_goal_tor")
    assert FILE_NAME_EVIDENCE not in evidence


# == D: "This replaces my earlier order and ending requests." (13:38) ==========================


def _brief(version: int) -> CreativeBrief:
    return CreativeBrief.model_validate(copy.deepcopy(CASE[f"brief_v{version}"]))


def _order_change(description: str, facts: dict[str, Any]) -> BriefUpdate:
    return BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=4,
        kind="order",
        scope="global",
        description=description,
        facts=facts,
    )


REPLACING = _order_change(
    "exactly this order: IMG_1028, IMG_1036, IMG_1037, IMG_1039, IMG_1043",
    {"key": "exact_sequence"},
)


def test_the_13_38_change_is_what_v5_records() -> None:
    """The Main Creator's r1 change as v4 -> v5 records it (its output is redacted; on main
    the merge carries both old ends back onto it)."""
    turn = CASE["brief_v5"]["requirements"][0]["source_turn_id"]
    assert stated_anchors(_brief(5)) == {"first": "my ticket", "last": ENDING}  # recorded
    assert CASE["message_1338"].endswith("This replaces my earlier order and ending requests.")
    recorded = _brief(5).requirements[0]
    assert (recorded.description, recorded.facts["key"]) == (
        REPLACING.description,
        "exact_sequence",
    )
    v5 = apply_updates(_brief(4), [REPLACING], source_turn_id=turn)
    assert [r.model_dump() for r in v5.requirements[1:]] == [
        r.model_dump() for r in _brief(5).requirements[1:]
    ]


def test_an_order_on_another_basis_does_not_keep_the_replaced_ends() -> None:
    """F8 (incident D). The new basis is a new order: the old "start with" / "end on" words
    are not carried onto it, so nothing holds the creator's complete list back."""
    after = apply_updates(_brief(4), [REPLACING], source_turn_id="t")

    assert after.requirements[0].facts == {"key": "exact_sequence"}
    assert stated_anchors(after) == {}


@pytest.mark.parametrize(
    ("before", "change", "kept"),
    [
        # KRI-522 / KRI-1068 F9: the same basis reworded keeps both ends.
        (
            {"key": "capture_time", "first_clip": "my ticket", "last_clip": ENDING},
            {"key": "capture_time"},
            {"first_clip": "my ticket", "last_clip": ENDING},
        ),
        # A pin-only "start with my ticket" given its first basis keeps the opening.
        ({"first_clip": "my ticket"}, {"key": "capture_time"}, {"first_clip": "my ticket"}),
        # The new basis restates an end: the model's words stand.
        (
            {"key": "capture_time", "first_clip": "my ticket", "last_clip": ENDING},
            {"key": "exact_sequence", "last_clip": ENDING},
            {"last_clip": ENDING},
        ),
    ],
    ids=["same-basis-reworded", "first-basis-for-a-pin", "new-basis-restates-the-end"],
)
def test_only_a_new_basis_drops_the_ends_it_does_not_restate(
    before: dict[str, Any], change: dict[str, Any], kept: dict[str, Any]
) -> None:
    """F9. Guards around F8: only a change from one stated basis to another drops the ends."""
    data = copy.deepcopy(CASE["brief_v4"])
    data["requirements"][0]["facts"] = before
    brief = CreativeBrief.model_validate(data)

    after = apply_updates(brief, [_order_change("reworded", change)], source_turn_id="t")

    assert after.requirements[0].facts == {**change, **kept}
