"""KRI-1331: "Add a title at the beginning of each event" draws each title once, at its start.

Prod job f6671587 (2026-11): "Use good parts from my speech, create a fast paced edit from
the rest. Show the video chronologically. Add a title at the beginning of each event.
Football, dodgeball, beach volleyball and pub. Also add an initial intro title saying Emir
Olympics London Edition". The phone render drew 21 texts instead of 5: the intro title, the
text composer's four titles on clips it guessed, 13 "beach volleyball" labels back to back
(one per event clip) and "Football", "dodgeball", "pub" stacked on one clip at one height.

Replayed here through the real boundaries: the approved strategy and brief through
``build_render_contract``, the composer's six recorded operations through
``replay_text_composition`` (the model's answer is the only stand-in), then the worker's
compile (``compile_phone_guided_repaired``) and the pin-time ``verify_phone_recipe``. What
the creator sees is read from the compiled recipe's text layers.

Ways this could fail, written before the code:
  1. a creator-worded event caption over N clips is pinned as N per-clip texts, so the
     repair draws N copies of the title;
  2. a real per-clip label ask ("Football" on every football clip) collapses into one pin;
  3. the title goes on the event's first clip in assignment order, not in the timeline;
  4. the montage's own title across the event is not recognised and a second copy is added;
  5. the composer's same words on the wrong clip stay there and a new copy is added;
  6. moving a row steals one that already shows another pinned text;
  7. two titles on one clip window overlap at the same height;
  8. a title runs past its event into the next one, or past the timeline;
  9. a title on the wrong event, or on the event's second clip, passes the verifier;
 10. the title -> clip binding is not recorded on the text, so the editor cannot keep it
     with its clip;
 11. the test passes on the code that shipped the incident (the fixture proves nothing).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, get_args

import pytest

from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes import MediaCapability
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_plan_repair import compile_phone_guided_repaired
from app.schemas.text_composition import TextCompositionProgram
from app.services.creation_text_composition import _digest, _lanes, replay_text_composition
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1331_event_titles.json").read_text()
)
TIMELINE = FIXTURE["timeline"]
TITLE = "Emir Olympics London Edition"
FRAME = 1 / 30

# Contiguous chapters of the chronological timeline: clips 0-9 are the spoken intro,
# 47-49 come after the pub and belong to no event.
EVENTS: dict[str, range] = {
    "Football": range(10, 17),
    "dodgeball": range(17, 19),
    "beach volleyball": range(19, 34),
    "pub": range(34, 47),
}


def _ids(indexes) -> list[str]:
    return [TIMELINE[index]["media_id"] for index in indexes]


def _strategy(members: dict[str, list[str]] | None = None, *, label: str | None = None) -> dict:
    """The approved strategy, with each event caption resolved to ``members``.

    ``label`` turns that event into a per-clip label ask instead (op="label", the
    creator's words on every member clip).
    """
    strategy = copy.deepcopy(FIXTURE["strategy"])
    for intent in strategy["resolved_clip_intents"]:
        text = intent.get("caption_text")
        if intent["op"] != "caption" or members is None or text not in members:
            continue
        intent["assignments"] = [{"media_id": m, "confidence": 0.9} for m in members[text]]
        if text == label:
            intent.update(op="label", caption_text=None, caption_grounding=None)
            for row in intent["assignments"]:
                row.update(value=text, grounding="creator_text")
    return strategy


def _contract(strategy: dict):
    return build_render_contract(
        strategy,
        generation_id="kri1331",
        brief=CreativeBrief.model_validate(FIXTURE["brief"]),
        media_snapshot={
            "clip_assignments": [
                {
                    "media_id": media["media_id"],
                    "kind": "video",
                    "duration_s": media["duration_s"],
                    "has_audio": True,
                    "capture": {"capture_time": media["capture_time"]},
                }
                for media in FIXTURE["media"]
            ]
        },
    )


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=media["media_id"],
            proxy_path=f"u/{media['media_id']}",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=f"{index:064x}",
                byte_count=1000,
                duration_s=media["duration_s"],
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for index, media in enumerate(FIXTURE["media"])
    )


def _planner_text(element_id: str, text: str, indexes: range) -> dict[str, Any]:
    """A montage chapter title or clip label across the clips ``indexes`` name."""
    return {
        **{k: v for k, v in FIXTURE["title"].items() if k not in {"id", "text"}},
        "id": element_id,
        "text": text,
        "start_s": TIMELINE[indexes[0]]["output_start_s"],
        "end_s": TIMELINE[indexes[-1]]["output_end_s"],
        "y_frac": 0.78,
        "size_px": 64.0,
    }


def _plan_dict(texts: list[dict[str, Any]]) -> dict[str, Any]:
    total = TIMELINE[-1]["output_end_s"]
    return GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 5,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "fast_montage",
            "goal": "Emir Olympics London Edition",
            "pace": "fast",
            "approved_duration_s": total,
            "resolved_duration_s": total,
            "selected_media_ids": [m["media_id"] for m in TIMELINE],
            "editor_revision_number": 1,
            "montage_audio": {"preserve_source_audio": True},
            "story_timeline": [
                {
                    **moment,
                    "beat_id": moment["moment_id"],
                    "topic": "moment",
                    "lane": "clip",
                    "kind": "video",
                    "gcs_path": f"u/{moment['media_id']}",
                    "generation": "1",
                    "layout": "fullscreen",
                }
                for moment in TIMELINE
            ],
            "beat_windows": [
                {
                    "beat_id": moment["moment_id"],
                    "approved_duration_s": moment["duration_s"],
                    "resolved_duration_s": moment["duration_s"],
                    "start_s": moment["output_start_s"],
                    "end_s": moment["output_end_s"],
                }
                for moment in TIMELINE
            ],
            "text_elements": texts,
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": FIXTURE["title"]["font_family"]},
        }
    ).model_dump(mode="json", exclude_none=False)


def _composed(planner_texts: list[dict[str, Any]], operations: list[dict]) -> Any:
    """The draft after the text composer: its recorded operations replayed on the plan."""
    plan = _plan_dict([FIXTURE["title"], *planner_texts])
    if operations:
        program = TextCompositionProgram(base_digest=_digest(*_lanes(plan)), operations=operations)
        plan = replay_text_composition(plan, program)
    return GuidedStoryExecutionPlan.model_validate(plan)


def _render(strategy: dict, plan: Any):
    contract = _contract(strategy)
    assert contract is not None and not contract.unresolved
    result = compile_phone_guided_repaired(plan, _bindings(), contract=contract)
    verify_phone_recipe(
        contract, result.recipe, source_audio={m["media_id"]: True for m in FIXTURE["media"]}
    )
    return contract, result


def _layers(recipe) -> list[dict[str, Any]]:
    """What the phone draws: words, window and the vertical band their ink can occupy."""
    out = []
    for layer in recipe.text_layers:
        if not layer.runs:
            continue
        tops = [run.baseline_y - run.font_size for run in layer.runs]
        bottoms = [run.baseline_y + 0.3 * run.font_size for run in layer.runs]
        out.append(
            {
                "text": " ".join(" ".join(run.text for run in layer.runs).split()),
                "start": layer.start,
                "end": layer.end,
                "top": min(tops),
                "bottom": max(bottoms),
            }
        )
    return out


def _assert_one_title_per_event(recipe, events: dict[str, list[int]]) -> None:
    layers = _layers(recipe)
    assert sorted(layer["text"] for layer in layers) == sorted([TITLE, *events]), layers
    for text, indexes in events.items():
        [layer] = [layer for layer in layers if layer["text"] == text]
        first = TIMELINE[indexes[0]]
        assert first["output_start_s"] - FRAME <= layer["start"] < first["output_end_s"], (
            text,
            layer,
        )
        assert layer["end"] <= TIMELINE[indexes[-1]]["output_end_s"] + FRAME, (text, layer)
    for index, one in enumerate(layers):
        for other in layers[index + 1 :]:
            at_once = one["start"] < other["end"] and other["start"] < one["end"]
            stacked = one["top"] < other["bottom"] and other["top"] < one["bottom"]
            assert not (at_once and stacked), (one, other)


def _event_members() -> dict[str, list[str]]:
    return {text: _ids(indexes) for text, indexes in EVENTS.items()}


def _event_group_titles() -> list[dict[str, Any]]:
    return [
        _planner_text(f"group-title-caption_{text.replace(' ', '_').lower()}-1", text, indexes)
        for text, indexes in EVENTS.items()
    ]


def _composer_adds() -> list[dict]:
    return [op for op in FIXTURE["composer_operations"] if op["op"] == "add_text"]


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))


def test_each_event_title_is_pinned_once_and_a_per_clip_label_on_every_clip():
    contract = _contract(_strategy(_event_members()))
    clip_texts = [row.text for row in contract.exact_texts if row.role == "clip"]
    assert sorted(clip_texts) == sorted(EVENTS)

    labelled = _contract(_strategy(_event_members(), label="Football"))
    football = [row for row in labelled.exact_texts if row.text == "Football"]
    assert sorted(row.media_id for row in football) == sorted(_ids(EVENTS["Football"]))


def test_incident_the_composer_titles_move_to_each_event_start_and_nothing_is_added():
    """The montage drew a title across each event; the composer removed them and wrote its
    own four titles on clips it guessed ("clip 17"), as prod's recorded operations did."""
    titles = _event_group_titles()
    operations = [
        {
            "op": "remove_texts",
            "selector": {"group": "free"},
            "target_ids": [row["id"] for row in titles],
            "expected_count": len(titles),
        },
        *_composer_adds(),
    ]
    plan = _composed(titles, operations)
    composer_ids = sorted(e.id for e in plan.text_elements if e.id.startswith("creation-"))

    _contract_, result = _render(_strategy(_event_members()), plan)

    _assert_one_title_per_event(
        result.recipe, {text: list(indexes) for text, indexes in EVENTS.items()}
    )
    rows = {e.text: e for e in result.plan.text_elements if not e.removed}
    assert sorted(e.id for e in result.plan.text_elements if e.text in EVENTS) == composer_ids
    for text, indexes in EVENTS.items():
        first = TIMELINE[indexes[0]]
        assert (rows[text].source_params or {}).get("source_asset_id") == first["media_id"]
        assert (rows[text].source_params or {}).get("source_timeline_id") == first["moment_id"]


def test_the_montage_title_across_each_event_is_kept_and_nothing_is_added():
    plan = _composed(_event_group_titles(), [])
    _contract_, result = _render(_strategy(_event_members()), plan)
    _assert_one_title_per_event(
        result.recipe, {text: list(indexes) for text, indexes in EVENTS.items()}
    )
    assert result.notes == []


def test_prod_several_events_on_one_clip_are_drawn_once_each_without_overlap():
    """Prod's own resolution: Football, dodgeball and pub all resolved to the clip at slot 34
    (KRI-1330), beach volleyball to slots 17-30; prod's six composer operations replayed."""
    planner = [
        _planner_text("group-title-caption_beach_volleyball-1", "beach volleyball", range(17, 31)),
        _planner_text("clip-label-unified-cut-35", "Football", range(34, 35)),
    ]
    plan = _composed(planner, FIXTURE["composer_operations"])

    _contract_, result = _render(_strategy(), plan)

    one_clip = [34]
    _assert_one_title_per_event(
        result.recipe,
        {
            "Football": one_clip,
            "dodgeball": one_clip,
            "beach volleyball": list(range(17, 31)),
            "pub": one_clip,
        },
    )


@pytest.mark.parametrize(
    ("text", "indexes"),
    [
        ("beach volleyball", EVENTS["beach volleyball"][1:2]),
        ("Football", EVENTS["dodgeball"][0:1]),
        ("pub", range(EVENTS["pub"][0], 49)),
    ],
    ids=["second-clip-of-its-event", "another-events-first-clip", "runs-past-its-event"],
)
def test_a_title_in_the_wrong_place_is_refused(text, indexes):
    """KRI-577 wrong-content control: the right words on the wrong footage never verify."""
    contract = _contract(_strategy(_event_members()))
    good = _composed(_event_group_titles(), [])
    texts = [
        _planner_text(e.id, e.text, indexes) if e.text == text else e.model_dump(mode="json")
        for e in good.text_elements
    ]
    recipe = compile_phone_guided_plan(_composed(texts[1:], []), _bindings())
    with pytest.raises(CreatorRenderContractError, match="wrong place|missing"):
        verify_phone_recipe(
            contract, recipe, source_audio={m["media_id"]: True for m in FIXTURE["media"]}
        )
