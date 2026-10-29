"""KRI-988: the Don Julio lunch M2 chat, replayed offline through the real server gate.

Prod Fly v1877 (af613077f), 2026-10-27, thread 4C536AFE, plan item b736702c: five uploaded
takes, two Visuals-pool photos (provoleta, steak). "10-second montage, only about the steak.
Skip the clip where he cuts the cheese ... Use my steak photo too. End on the dessert. Quick
cuts." Five turns later the creator had a refusal and no way forward:

1. Turn 4 (events 16-17): the creator tapped the last clip picker. The server gate refused:
   "some of the selected clips are no longer attached to this edit", with one option, "Update
   the selected clips" (KRI-989). The selection held the steak photo as ``asset-cfb745e0-...``;
   the length plan looked every selected id up in the item's own clip rows (videos only), so
   the pool photo was reported as a clip that vanished. Nothing was removed.
2. Turn 5 (events 18-19): "All my clips are still attached, nothing was removed. Use the steak
   photo and make the montage." The one ask was spent, so the gate answered with a bare
   "Your length choice is still waiting." text: no card, the earlier card went read-only on the
   phone, and "Update the selected clips" only ever waits for new footage (KRI-990).
3. Turn 6 (events 20-21): "Forget the steak photo" changed the strategy so the conflict was
   gone. That was the only exit.

Failure modes, first:

- A selected pool photo reported as a missing clip (``no_usable_sources``) -> test 1 (prod's
  refusal, byte-for-byte digest) and test 2 (it is gone on the current code).
- The photo counted as a source row, or lost from the selection by the gate -> test 2.
- A clip that really is gone gets only "wait for new footage" -> test 3.
- A typed non-answer ends the question for good -> test 4.

RECONSTRUCTION: prod redacts the turn-4 strategy (sha256 only). The strategy here is the
approved turn-6 strategy (after the creator dropped the photo) cut back to the two video clips
picked by turn 4 plus the steak photo id; the two clips were recovered because only that pair
reproduces prod's recorded ``input_digest`` on the old behaviour (test 1); the brief is the
pinned turn-6 brief minus r8 ("clips only"), which the creator had not yet said at turn 4. The
clip rows (with analysis), media snapshot, pool
assets, the recorded card text and its ``input_digest`` are prod's, verbatim. See the
``reconstructed`` field of ``tests/fixtures/kri988_donjulio_lunch_m2_turns.json``.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreatorMediaRef, ResolvedCreatorManifest
from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.services.choice_questions import tag_event

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri988_donjulio_lunch_m2_turns.json"
    ).read_text(encoding="utf-8")
)
STEAK = FIXTURE["steak_photo_id"]
PROVOLETA = f"asset-{FIXTURE['pool_assets'][0]['id']}"
CARD = FIXTURE["prod_events"]["source_coverage_card"]
PROD_DIGEST = CARD["choice_question"]["input_digest"]
CLIP_ROWS = FIXTURE["media_snapshot"]["clip_assignments"]
STRATEGY = FIXTURE["strategy"]
SELECTED_CLIPS = [m for m in STRATEGY["selected_media_ids"] if not m.startswith("asset-")]
GONE = "analysis-proxy-ios-00000000-0000-0000-0000-000000000000.mp4"
CREATOR_ID = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
THREAD_ID = uuid.UUID(FIXTURE["incident"]["thread_id"])
TYPED = FIXTURE["prod_events"]["typed_non_answer"]["content"]
NO_LONGER_ATTACHED = "no longer attached to this edit"


def prod_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under (phone pilot, clip intents + facts on)."""
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "stillImages"],
    )


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


def _brief() -> CreativeBrief:
    """The turn-4 brief: prod's pinned brief without r8 (see the module docstring)."""
    return CreativeBrief.model_validate(copy.deepcopy(FIXTURE["brief"]))


def _strategy(*, extra_selected: tuple[str, ...] = ()) -> dict:
    strategy = copy.deepcopy(STRATEGY)
    strategy["selected_media_ids"] = [*strategy["selected_media_ids"], *extra_selected]
    return strategy


def _manifest(*, pool: tuple[str, ...]) -> ResolvedCreatorManifest:
    """The item's media as prod's manifest lists it: five takes plus the ready pool photos."""
    return ResolvedCreatorManifest(
        item_id="item-m2",
        edit_format="montage",
        render_program="guided",
        media=[
            *(CreatorMediaRef(media_id=row["media_id"], kind="video") for row in CLIP_ROWS),
            *(CreatorMediaRef(media_id=media_id, kind="image") for media_id in pool),
        ],
        context_hash="b" * 64,
        manifest_hash="a" * 64,
    )


POOL = (PROVOLETA, STEAK)


def _planned(strategy: dict, manifest: ResolvedCreatorManifest) -> PlannedKriaTurn:
    """The Main Creator's planned turn (draft + render request), before the server's gate."""
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply-strategy",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"summary": "A fast 10-second steak montage.", "strategy": strategy},
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
        media_snapshot=copy.deepcopy(FIXTURE["media_snapshot"]),
        brief_manifest=manifest,
    )


async def run_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    strategy: dict | None = None,
    manifest: ResolvedCreatorManifest | None = None,
    events: tuple = (),
) -> PlannedKriaTurn:
    """The real server gate (``plan_live_turn`` runs it once the snapshot is attached)."""
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=_brief()))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    return await planner._gate_unresolved_choices(
        SimpleNamespace(),
        _planned(
            strategy if strategy is not None else _strategy(),
            manifest if manifest is not None else _manifest(pool=POOL),
        ),
        thread_id=THREAD_ID,
        creator_id=CREATOR_ID,
    )


def _asked(result: PlannedKriaTurn) -> tuple[str, dict]:
    assert result.plan.turn_value == "question" and result.plan.choice_question, result.plan
    return tag_event(
        "assistant", {"choice_question": result.plan.choice_question}, "assistant_response", None
    )


def _picked(question: dict, option: str) -> tuple[str, dict]:
    return tag_event(
        "user",
        {"choice_selection": {"question_id": question["question_id"], "option_key": option}},
        "user_message",
        None,
    )


def _typed(text: str) -> tuple[str, dict]:
    return tag_event("user", {}, "user_message", text)


def gate_strategy(result: PlannedKriaTurn) -> dict:
    assert result.plan.mode == "act", result.plan.response
    return copy.deepcopy(result.plan.intents[0].arguments["strategy"])


def _option_keys(result: PlannedKriaTurn) -> list[str]:
    return [o["key"] for o in result.plan.choice_question["options"]]


# -- the replay is faithful: what prod refused ------------------------------------------------


async def test_prod_refused_the_photo_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``asset-*`` media in the manifest (what the old code effectively saw: the length
    plan never consulted the pool), the gate yields prod's refusal: a ``source_coverage``
    question about clips "no longer attached", with prod's recorded ``input_digest``."""
    result = await run_gate(monkeypatch, manifest=_manifest(pool=()))
    question = result.plan.choice_question
    assert question["kind"] == "source_coverage"
    # The digest covers the conflict, the three source rows (id, duration, paths) and the
    # codes, so a match proves the sources and conflict are prod's.
    assert question["input_digest"] == PROD_DIGEST
    assert NO_LONGER_ATTACHED in CARD["content"]
    assert NO_LONGER_ATTACHED in result.plan.response
    assert question["conflict"] == CARD["choice_question"]["conflict"]
    # Prod's one option is still offered first, under its recorded key and label.
    prod_first = CARD["choice_question"]["options"][0]
    assert question["options"][0]["key"] == prod_first["key"]
    assert question["options"][0]["label"] == prod_first["label"]


async def test_the_old_attachment_blindness_reproduces_prods_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof that KRI-989's ``_attached_visual_ids`` is what lets the photo through: the real
    manifest (photo attached) with that lookup returning nothing is prod's refusal again."""
    monkeypatch.setattr(planner, "_attached_visual_ids", lambda _manifest: ())
    result = await run_gate(monkeypatch)
    assert result.plan.choice_question["kind"] == "source_coverage"
    assert result.plan.choice_question["input_digest"] == PROD_DIGEST
    assert NO_LONGER_ATTACHED in result.plan.response


# -- what the creator should have had -----------------------------------------------------------


async def test_the_gate_accepts_the_steak_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_gate(monkeypatch)
    assert result.plan.mode == "act", result.plan.response
    plan = result.media_snapshot["length_plan"]
    assert plan["conflicts"] == []
    assert "missing_source_ids" not in plan
    # The photo is neither a length-plan source nor missing: the sources are the selected
    # video clips (the plan lists them in clip-row order).
    assert {row["media_id"] for row in plan["sources"]} == set(SELECTED_CLIPS)
    assert len(plan["sources"]) == len(SELECTED_CLIPS)
    assert STEAK not in {row["media_id"] for row in plan["sources"]}
    # The gate leaves the selection alone: the photo stays in the draft the creator approves.
    assert STEAK in gate_strategy(result)["selected_media_ids"]


async def test_a_genuinely_gone_clip_gets_a_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = _strategy(extra_selected=(GONE,))
    first = await run_gate(monkeypatch, strategy=strategy)
    question = first.plan.choice_question
    assert question["kind"] == "source_coverage"
    assert NO_LONGER_ATTACHED in first.plan.response
    assert _option_keys(first) == ["update_footage", "leave_out_sources"]
    # The photo is attached, so only the gone clip is named for dropping.
    answered = await run_gate(
        monkeypatch,
        strategy=strategy,
        events=(_asked(first), _picked(question, "leave_out_sources")),
    )
    done = gate_strategy(answered)
    assert GONE not in done["selected_media_ids"]
    assert done["selected_media_ids"] == STRATEGY["selected_media_ids"]
    assert STEAK in done["selected_media_ids"]
    assert answered.media_snapshot["length_plan"]["conflicts"] == []
    (answer,) = done["choice_answers"]
    assert answer["kind"] == "source_coverage" and answer["option"] == "leave_out_sources"


async def test_a_typed_non_answer_gets_the_card_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod's turn 5 (event 18) as typed: with the one ask spent, the creator got a bare
    "Your length choice is still waiting." and nothing to tap. Now the same question comes
    back as a live card, same inputs (``input_digest``), new ``question_id``."""
    strategy = _strategy(extra_selected=(GONE,))
    first = await run_gate(monkeypatch, strategy=strategy)
    again = await run_gate(monkeypatch, strategy=strategy, events=(_asked(first), _typed(TYPED)))
    assert again.plan.turn_value == "question", again.plan.response
    question = again.plan.choice_question
    assert question["kind"] == "source_coverage"
    assert question["input_digest"] == first.plan.choice_question["input_digest"]
    assert question["question_id"] != first.plan.choice_question["question_id"]
    assert "leave_out_sources" in _option_keys(again)
    assert "still waiting" in again.plan.response
    # Prod's bare reply was this text alone; it is now only the card's lead.
    bare = FIXTURE["prod_events"]["still_waiting_reply"]["content"]
    assert again.plan.response != bare and again.plan.response.startswith(bare.split(".")[0])
