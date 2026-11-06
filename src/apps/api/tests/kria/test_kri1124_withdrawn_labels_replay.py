"""KRI-1124 incident replay: a label the creator withdrew is neither planned nor locked.

Leverkusen M4 run 1 (job 98eac78f, 2026-10-31). The creator dropped "Arrival" ("Skip that
part") and "GOAL" + "Full time" ("Make it without these"): brief r4/r8/r9 are `superseded`
and no live requirement asks for any text. The strategy (`clip_intents` /
`resolved_clip_intents`, written by the clip-intent planner from the whole thread, and
re-derived from it every turn) still carried the three label intents, so the plan printed
them as creator-provenance labels and `snapshot_copy_constraints` locked them as confirmed
copy. The creation-text composer, told the brief no longer asks for them, then either removed
them (-> "Text composition changed confirmed copy" -> `creation_text_failed`) or left them (a
video with text the creator dropped).

The real planner, snapshot and `compose_creation_text` run; only `EditCopilotAgent.run` (the
paid model call) is replaced. Fixture: the brief (v8), strategy and render-contract order exactly
as stored on the failed job.

Ways this could go wrong, written down before the code:

a. A withdrawn label is still planned, so it is locked as confirmed copy and the composer that
   follows the brief fails the render (the incident), or one that changes nothing prints it.
b. The fix prunes too much: a label a LIVE text requirement still asks for, or one the creator
   re-opened, is dropped, or stops being locked against the composer.
c. A KRI-767 move (remove the words from one row, add the same words elsewhere) starts failing.
d. A withdrawn text comes back through another route: a described-shot caption, a chapter
   caption, a positional `shot_labels` list, or the clarification gate's title lookup.
e. Words that merely look like a withdrawn text ("Full time" vs "Full-time!") survive, or a
   different text with a shared word ("Goal kick") is dropped with it.
f. No brief is bound (`brief_view(None)`): every label the strategy carries is kept.
g. A positional `shot_labels` list that is only PARTLY withdrawn is edited: the labels after the
   gap move onto other clips. It is left as written.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agents.edit_copilot import EditCopilotAgent
from app.kria.brief import CreativeBrief
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import (
    UnifiedClip,
    brief_view,
    plan_unified_montage,
    title_source_exists,
)
from app.services.creation_text_composition import (
    CreationTextCompositionError,
    compose_creation_text,
    snapshot_copy_constraints,
)

FX = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "m4_leverkusen_withdrawn_labels.json").read_text(
        "utf-8"
    )
)
BRIEF = CreativeBrief.model_validate(FX["brief"])
STRATEGY = FX["strategy"]
REQUEST = FX["creator_request"]

STADIUM = "asset-76654025-b1b1-4353-986b-c2e716ddde3e"
GOAL_CLIP = "analysis-proxy-ios-67E18328-733F-46AB-99D0-4959F9ADBBBC.mp4"
LAST_PHOTO = "asset-da156baf-ff35-49a0-83c7-76df249e830e"
FANS = "asset-a8bbb7a0-95be-4e37-9689-7c05aac1e190"

LABEL_OPS = ("label", "caption")


def _plan(brief: CreativeBrief | None, strategy: dict[str, Any] | None = None):
    t0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    when = {mid: t0 + timedelta(minutes=i) for i, mid in enumerate(FX["order_ids"])}
    strategy = STRATEGY if strategy is None else strategy
    clips, visuals = [], []
    for mid in strategy["selected_media_ids"]:
        if mid.startswith("analysis-proxy-ios-"):
            clips.append(
                UnifiedClip(
                    media_id=mid,
                    proxy_path=f"users/u/{mid}",
                    generation="1",
                    duration_s=FX["video_durations_s"][mid],
                    width=1920,
                    height=1080,
                    orientation_degrees=90,
                    capture_time=when.get(mid),
                )
            )
        else:
            uid = mid.removeprefix("asset-")
            visuals.append(
                UnifiedClip(
                    media_id=uid,
                    proxy_path=f"users/u/{uid}.jpg",
                    generation="1",
                    duration_s=0.0,
                    lane="asset",
                    kind="image",
                    manifest_id=mid,
                    aspect=0.75,
                    capture_time=when.get(mid),
                )
            )
    return plan_unified_montage(
        clips,
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=True,
        font_covers=lambda *_: True,
        visuals=visuals,
        output_orientation="portrait",
        creator_duration_proven=True,
    )


def _brief(*, open_ids: tuple[str, ...] = (), extra: tuple[dict, ...] = ()) -> CreativeBrief:
    """The incident's brief with some superseded requirements re-opened and some added."""
    data = copy.deepcopy(FX["brief"])
    for req in data["requirements"]:
        if req["id"] in open_ids:
            req["status"] = "open"
    for index, req in enumerate(extra):
        data["requirements"].append(
            {
                "id": f"r{20 + index}",
                "facts": {},
                "status": "open",
                "literal": None,
                "description": "",
                "source_turn_id": "9ffcdd7f-a3f0-4c6c-9426-fd1b9c052dee",
                **req,
            }
        )
    return CreativeBrief.model_validate(data)


def _strategy(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(STRATEGY)
    out.update(changes)
    return out


def _intents(strategy: dict[str, Any], key: str = "resolved_clip_intents") -> list[dict]:
    return strategy[key]


def _without_label_intents(strategy: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(strategy)
    for key in ("clip_intents", "resolved_clip_intents"):
        out[key] = [i for i in out[key] if i["op"] not in LABEL_OPS]
    return out


def _printed(plan) -> list[str]:
    return [label.text for label in plan.snapshot.clip_labels or []]


def _compose(monkeypatch, brief: CreativeBrief, respond):
    plan = _plan(brief)
    base = compile_proposal_execution_plan(plan.snapshot)
    calls = []

    def run(self, input, *, ctx=None):
        calls.append(input)
        return self.parse(json.dumps(respond(base)), input)

    monkeypatch.setattr(EditCopilotAgent, "run", run)
    out = compose_creation_text(
        plan.snapshot,
        creator_request=REQUEST,
        copy_constraints=snapshot_copy_constraints(plan.snapshot),
        text_requirements=brief.live(),
    )
    assert len(calls) == 1  # prod made exactly one nova.edit.copilot call
    return [r["text"] for r in compile_proposal_execution_plan(out)["text_elements"]]


def _remove_every_label(base: dict) -> dict:
    """A composer that obeys the brief ("Make it without these") by deleting the label rows."""
    ids = [row["id"] for row in base["text_elements"]]
    if not ids:
        return {"intent": "describe", "confidence": 0.9, "reply": "No text to change.", "ops": []}
    return {
        "intent": "edit",
        "confidence": 0.9,
        "reply": "Removed the labels you dropped.",
        "ops": [{"op": "remove_texts", "selector": {"ids": ids}}],
    }


def _no_ops(_base: dict) -> dict:
    return {"intent": "describe", "confidence": 0.9, "reply": "Nothing to change.", "ops": []}


def _move_goal(base: dict) -> dict:
    """The KRI-767 shape: remove the label row, add the same words on another clip."""
    label = next(row for row in base["text_elements"] if row["text"] == "GOAL")
    labelled = {row["id"] for row in base["text_elements"] if row["id"].startswith("clip-label-")}
    target = next(
        slot
        for slot in base["story_timeline"]
        if f"clip-label-{slot['moment_id']}" not in labelled
        and slot["output_start_s"] >= label["end_s"]
    )
    return {
        "intent": "edit",
        "confidence": 0.95,
        "reply": "Moved the label.",
        "ops": [
            {"op": "remove_texts", "selector": {"ids": [label["id"]]}},
            {
                "op": "add_text",
                "text": "GOAL",
                "start_s": target["output_start_s"],
                "end_s": target["output_end_s"],
                "clip_id": target["media_id"],
                "style_from": "labels",
            },
        ],
    }


# --- the incident -----------------------------------------------------------------------


def test_the_brief_asks_for_no_text_at_all():
    assert [r.id for r in BRIEF.live()] == ["r1", "r2", "r3", "r10", "r11"]
    assert {r.id for r in BRIEF.requirements if not r.live} >= {"r4", "r8", "r9"}
    assert not [r for r in BRIEF.live() if r.kind == "text"]


def test_withdrawn_labels_are_neither_planned_nor_locked_as_confirmed_copy():
    plan = _plan(BRIEF)
    assert _printed(plan) == []
    assert snapshot_copy_constraints(plan.snapshot) == {}


def test_a_composer_that_follows_the_brief_does_not_fail_the_render(monkeypatch):
    # Before the fix: CreationTextCompositionError("Text composition changed confirmed copy").
    assert _compose(monkeypatch, BRIEF, _remove_every_label) == []


def test_a_composer_that_changes_nothing_does_not_print_the_dropped_labels(monkeypatch):
    # Before the fix: ['Arrival', 'GOAL', 'Full time'] on screen although the creator left
    # them out.
    assert _compose(monkeypatch, BRIEF, _no_ops) == []


# --- the lock the fix must keep (KRI-651 / KRI-767) --------------------------------------


def test_labels_a_live_requirement_still_asks_for_stay_locked(monkeypatch):
    brief = _brief(open_ids=("r8", "r9"))  # GOAL + Full time live again; Arrival still dropped
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _compose(monkeypatch, brief, _remove_every_label)


def test_only_the_labels_still_asked_for_are_printed(monkeypatch):
    brief = _brief(open_ids=("r8", "r9"))
    assert _compose(monkeypatch, brief, _no_ops) == ["GOAL", "Full time"]


def test_a_live_label_the_composer_moves_is_not_a_copy_change(monkeypatch):
    brief = _brief(open_ids=("r8", "r9"))
    assert sorted(_compose(monkeypatch, brief, _move_goal)) == ["Full time", "GOAL"]


def test_a_reopened_label_is_kept_with_its_text():
    plan = _plan(_brief(open_ids=("r4",)))
    assert _printed(plan) == ["Arrival"]
    assert list(snapshot_copy_constraints(plan.snapshot).values()) == ["Arrival"]


def test_a_live_requirement_carrying_the_same_words_keeps_the_label():
    # A new ask for "ARRIVAL!" (r20) outlives the dropped r4 "Arrival": the words are wanted.
    live = {"kind": "text", "scope": "per_clip", "literal": "ARRIVAL!", "description": "stadium"}
    assert _printed(_plan(_brief(extra=(live,)))) == ["Arrival"]


def test_no_bound_brief_means_nothing_is_withdrawn():
    assert _printed(_plan(None)) == ["Arrival", "GOAL", "Full time"]


# --- spelling ----------------------------------------------------------------------------


def test_a_withdrawn_text_is_found_through_case_accents_and_punctuation():
    strategy = _strategy()
    for key in ("clip_intents", "resolved_clip_intents"):
        for intent in _intents(strategy, key):
            if intent.get("creator_text") == "Full time":
                intent["creator_text"] = "FULL-TIME!"
                for row in intent.get("assignments") or []:
                    row["value"] = "FULL-TIME!"
    assert _printed(_plan(BRIEF, strategy)) == []


def test_a_different_text_sharing_a_word_with_a_withdrawn_one_stays():
    strategy = _strategy()
    for key in ("clip_intents", "resolved_clip_intents"):
        for intent in _intents(strategy, key):
            if intent.get("creator_text") == "GOAL":
                intent["creator_text"] = "Goal kick"
                for row in intent.get("assignments") or []:
                    row["value"] = "Goal kick"
    assert _printed(_plan(BRIEF, strategy)) == ["Goal kick"]


# --- the same words through the other routes -------------------------------------------


def test_a_withdrawn_described_shot_caption_is_not_printed():
    strategy = _without_label_intents(STRATEGY)
    for key in ("clip_intents", "resolved_clip_intents"):
        for text, media_id in (
            ("Arrival", STADIUM),
            ("GOAL", GOAL_CLIP),
            ("Full time", LAST_PHOTO),
        ):
            row: dict[str, Any] = {
                "op": "caption",
                "attribute": "the shot",
                "intent_id": f"caption_{text}",
                "creator_text": text,
            }
            if key == "resolved_clip_intents":
                row |= {
                    "status": "resolved",
                    "caption_text": text,
                    "caption_grounding": "creator_text",
                    "assignments": [{"media_id": media_id, "evidence": "creator selected"}],
                }
            strategy[key].append(row)
    assert _printed(_plan(BRIEF, strategy)) == []


def test_a_withdrawn_chapter_caption_is_not_printed():
    strategy = _without_label_intents(STRATEGY)
    strategy["resolved_clip_intents"].append(
        {
            "op": "caption",
            "status": "resolved",
            "attribute": "the stadium",
            "intent_id": "chapter_arrival",
            "caption_attribute": "Arrival",
            "caption_text": "Arrival",
            "caption_grounding": "creator_text",
            "assignments": [{"media_id": STADIUM, "evidence": "creator selected"}],
        }
    )
    assert _printed(_plan(BRIEF, strategy)) == []


def test_a_positional_label_list_that_is_entirely_withdrawn_is_not_printed():
    strategy = _without_label_intents(STRATEGY)
    strategy["shot_labels"] = ["Arrival", "Away end", "Full time"]
    assert _printed(_plan(BRIEF, strategy)) == []


def test_a_positional_label_list_that_is_only_partly_withdrawn_is_left_as_written():
    strategy = _without_label_intents(STRATEGY)
    strategy["shot_labels"] = ["Arrival", "Tunnel"]
    assert _printed(_plan(BRIEF, strategy)) == ["Arrival", "Tunnel"]


def test_the_title_question_reads_the_same_labels_the_render_prints():
    # A live title listing the words of three dropped labels is a title, not their chapter
    # list: the gate must not ask "what are the title's words?" while the render burns one.
    listing = {"kind": "text", "scope": "title", "literal": "Arrival, GOAL, Full time"}
    brief = _brief(extra=(listing,))
    plan = _plan(brief)
    assert title_source_exists(STRATEGY, brief) is True
    assert plan.snapshot.opening_title == "Arrival, GOAL, Full time"
