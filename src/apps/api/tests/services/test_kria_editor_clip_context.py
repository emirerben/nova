"""KRI-191 + KRI-203: the copilot sees clips/facts/brief, and bulk text edits.

The fixture is East Run-shaped but fully synthetic: a guided (phone unified
montage) variant with per-clip label bars, no legacy timeline, and clip facts
stored on the plan item's assignments (invented names, no prod data).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.kria import planner
from app.kria.brief import (
    BriefRequirement,
    CreativeBrief,
    plan_shape_from_editor_snapshot,
    route_requirements,
)
from app.models import CreationThread, CreatorAgentSession, Job
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    clip_facts_by_media_id,
    compile_editor_ops,
)

PLACES = ["Alpha Quay", "Beta Bridge", "Gamma Tower", "Delta Palace"]
T0 = datetime(2026, 9, 20, 7, 0, tzinfo=UTC)
CLIPS = 6


def _paths() -> list[str]:
    return [f"users/u/proxy-{i}.mp4" for i in range(CLIPS)]


def _assignments() -> list[dict]:
    """Stored per-clip facts. Clip 2 has a geocoded place but no landmark guess."""
    rows = []
    for i, path in enumerate(_paths()):
        row: dict = {
            "gcs_path": path,
            "capture": {"capture_time": (T0 + timedelta(minutes=4 * i)).isoformat()},
        }
        if i % 3 != 2:
            row["analysis"] = {
                "clip_facts": [
                    {
                        "kind": "landmark",
                        "value": PLACES[i % 4],
                        "provenance": "inferred",
                        "confidence": 0.8,
                    }
                ]
            }
        rows.append(row)
    rows[2]["capture"]["place"] = {
        "sub_locality": "Epsilon Cove",
        "locality": "Testville",
        "country": "Testland",
    }
    return rows


def _job_and_variant(*, unlabelled_clips: bool = True):
    """A rendered phone montage: per-clip label bars exist only for clips with a landmark."""
    clips = []
    for i, path in enumerate(_paths()):
        facts = []
        if i % 3 != 2:
            facts.append({"kind": "landmark", "value": PLACES[i % 4], "provenance": "inferred"})
        clips.append(
            UnifiedClip(
                media_id=f"clip-{i}",
                proxy_path=path,
                generation="7",
                duration_s=4.0,
                width=1080,
                height=1920,
                facts=tuple(facts),
            )
        )
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="the place")
        ],
    )
    plan = plan_unified_montage(clips, brief_view(brief))
    guided = plan.guided_edit()
    execution_plan = compile_execution_plan(guided, track=None)
    variant = {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "render_destination": "device",
        "text_elements": execution_plan["text_elements"],
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan={
            "guided_edit": guided,
            "guided_story_execution_plan": execution_plan,
            "variants": [variant],
        },
        all_candidates={"clip_paths": _paths()},
    )
    return job, variant


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True, "timeline": True},
    )


def _context(job, variant) -> dict:
    return {"facts": clip_facts_by_media_id(job, variant, _assignments())}


def _parse(snapshot: dict, ops: list[dict], utterance: str = "do it"):
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": ops,
            "confidence": 0.9,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )
    return EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=utterance, prior_turns=[], variant_snapshot=snapshot)
    )


def _saved_text(compiled) -> list[dict]:
    return compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]


# ── KRI-191: the snapshot ─────────────────────────────────────────────────────


def test_guided_variant_snapshot_has_slots_with_media_ids_moments_and_facts() -> None:
    job, variant = _job_and_variant()
    assert "user_timeline" not in variant and "ai_timeline" not in variant

    snapshot = build_editor_snapshot(job, variant, clip_context=_context(job, variant))

    slots = snapshot["slots"]
    assert [s["media_id"] for s in slots] == [f"clip-{i}" for i in range(CLIPS)]
    assert all("source_gcs_path" not in s for s in slots)
    assert "users/u" not in json.dumps(snapshot), "the snapshot must stay path-free"
    first = slots[0]
    assert first["moment"] == "Alpha Quay"
    kinds = {(f["kind"], f["provenance"]) for f in first["facts"]}
    assert ("landmark", "inferred") in kinds
    assert ("capture_time", "exif") in kinds
    # Clip 2 has no landmark guess but a geocoded place.
    assert ("place", "geocode") in {(f["kind"], f["provenance"]) for f in slots[2]["facts"]}
    assert snapshot["label_facts"] is True


def test_label_bars_link_to_their_clip_and_flag_guesses() -> None:
    job, variant = _job_and_variant()
    snapshot = build_editor_snapshot(job, variant)
    bars = {b["id"]: b for b in snapshot["text_bars"]}
    label_bars = [b for b in snapshot["text_bars"] if b["id"].startswith("clip-label-")]
    assert label_bars, "the fixture must carry a per-clip label lane"
    assert {b["clip_id"] for b in label_bars} == {"clip-0", "clip-1", "clip-3", "clip-4"}
    assert all(b["inferred"] is True for b in label_bars)
    assert "clip_id" not in bars["guided-title"]

    # A creator's own edit is no longer an AI guess.
    edited = next(row for row in variant["text_elements"] if row["id"] == label_bars[0]["id"])
    edited["text"] = "Beyond the Bridge"
    snapshot = build_editor_snapshot(job, variant)
    again = next(b for b in snapshot["text_bars"] if b["id"] == label_bars[0]["id"])
    assert again["inferred"] is False


def test_brief_is_carried_when_supplied_and_absent_otherwise() -> None:
    job, variant = _job_and_variant()
    assert "brief" not in build_editor_snapshot(job, variant)
    snapshot = build_editor_snapshot(job, variant, clip_context={"brief": "- [text/per_clip] x"})
    assert snapshot["brief"] == "- [text/per_clip] x"


def test_snapshot_without_facts_never_offers_label_each_clip() -> None:
    job, variant = _job_and_variant()
    snapshot = build_editor_snapshot(job, variant)
    assert "label_facts" not in snapshot
    output = _parse(snapshot, [{"op": "label_each_clip", "source": "facts"}])
    assert output.ops == []


def test_clip_ops_are_not_advertised_where_labels_would_desync() -> None:
    job, variant = _job_and_variant()
    families = build_editor_snapshot(job, variant)["allowed_op_families"]
    assert "text" in families
    assert "clip" not in families and "transition" not in families


# ── KRI-191: label_each_clip ──────────────────────────────────────────────────


def test_label_each_clip_compiles_to_grounded_labels_in_one_op() -> None:
    job, variant = _job_and_variant()
    context = _context(job, variant)
    # Clips 3 and 4 both resolve to the same landmark: the repeat must be skipped.
    context["facts"]["clip-4"] = list(context["facts"]["clip-3"])
    snapshot = build_editor_snapshot(job, variant, clip_context=context)
    before = {row["id"]: row["text"] for row in variant["text_elements"]}

    output = _parse(
        snapshot,
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                # Model-authored labels must be ignored: only server facts are used.
                "labels": [{"media_id": "clip-0", "text": "INVENTED"}],
            }
        ],
        utterance="label each landmark",
    )

    assert output.outcome == "proposed"
    assert [op["op"] for op in output.ops] == ["label_each_clip"]
    labels = {row["media_id"]: row for row in output.ops[0]["labels"]}
    assert "INVENTED" not in json.dumps(output.ops)
    # Clip 2 had no label bar: it gets one on its own output window, from the geocoded place.
    assert labels["clip-2"]["text"] == "Epsilon Cove"
    assert "bar_id" not in labels["clip-2"]
    assert labels["clip-2"]["end_s"] > labels["clip-2"]["start_s"]
    # Clip 4 repeats clip 3's landmark, so it is skipped; clip 5 has no fact at all.
    assert "clip-5" not in labels
    assert "clip-3" not in labels, "clip 3's bar already shows its landmark"
    assert "clip-4" not in labels

    compiled = compile_editor_ops(job, variant, output.ops)
    saved = {row["id"]: row for row in _saved_text(compiled)}
    assert saved["clip-label-media-clip-2"]["text"] == "Epsilon Cove"
    # The new bar copies a sibling label's style and is timed on clip 2's window.
    sibling = saved["clip-label-unified-cut-1"]
    assert saved["clip-label-media-clip-2"]["font_family"] == sibling["font_family"]
    assert saved["clip-label-media-clip-2"]["y_frac"] == sibling["y_frac"]
    # Nothing that already existed is removed or restyled.
    assert set(before) <= set(saved)
    assert compiled.changes == ["Label each clip"]


def test_label_each_clip_is_refused_when_every_clip_is_already_labelled() -> None:
    job, variant = _job_and_variant()
    context = _context(job, variant)
    # Only clips that already carry the matching label remain.
    context["facts"] = {k: v for k, v in context["facts"].items() if k in {"clip-0", "clip-1"}}
    snapshot = build_editor_snapshot(job, variant, clip_context=context)
    output = _parse(snapshot, [{"op": "label_each_clip", "source": "facts"}])
    assert output.ops == []
    assert output.rejection_reasons


def test_one_label_correction_touches_exactly_one_bar() -> None:
    job, variant = _job_and_variant()
    snapshot = build_editor_snapshot(job, variant, clip_context=_context(job, variant))
    bars = snapshot["text_bars"]
    target = next(i for i, b in enumerate(bars) if b.get("clip_id") == "clip-1")
    before = {row["id"]: dict(row) for row in variant["text_elements"]}

    output = _parse(
        snapshot,
        [{"op": "edit_text", "bar_index": target, "text": "Besiktas Pier"}],
        utterance="that's Besiktas Pier, not Beta Bridge",
    )
    assert [op["op"] for op in output.ops] == ["edit_text"]
    compiled = compile_editor_ops(job, variant, output.ops)

    saved = {row["id"]: row for row in _saved_text(compiled)}
    changed = [bar_id for bar_id, row in saved.items() if row["text"] != before[bar_id]["text"]]
    assert changed == [bars[target]["id"]]
    assert saved[bars[target]["id"]]["text"] == "Besiktas Pier"


def test_per_clip_text_correction_routes_to_the_editor_not_a_replan() -> None:
    job, variant = _job_and_variant()
    snapshot = build_editor_snapshot(job, variant)
    shape = plan_shape_from_editor_snapshot(snapshot)
    assert shape.has_render and shape.has_per_clip_text_lane
    requirement = BriefRequirement(
        id="r2", kind="text", scope="clip:clip-1", literal="Besiktas Pier", description="fix"
    )
    assert route_requirements([requirement], shape) == "editor_ops"


# ── KRI-191: guided facts read ────────────────────────────────────────────────


def test_clip_facts_join_by_storage_path_and_stay_path_free() -> None:
    job, variant = _job_and_variant()
    facts = clip_facts_by_media_id(job, variant, _assignments())
    assert set(facts) == {f"clip-{i}" for i in range(CLIPS)}
    assert "users/u" not in json.dumps(facts)
    assert clip_facts_by_media_id(job, variant, [{"gcs_path": "users/other.mp4"}]) == {}


# ── KRI-203: the East Run font request ────────────────────────────────────────


def _thirteen_bars() -> tuple[SimpleNamespace, dict]:
    variant = {
        "variant_id": "guided_story",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "resolved_archetype": "montage",
        "text_elements": [
            {
                "id": "guided-title" if i == 0 else f"clip-label-unified-cut-{i}",
                "text": f"Bar {i}",
                "start_s": float(i),
                "end_s": float(i) + 1.0,
                "font_family": "DM Sans",
                "size_px": 58,
                "color": "#FFF8F0",
                "effect": "static",
                "alignment": "center",
                "position": "custom",
                "role": "generative_intro",
            }
            for i in range(13)
        ],
    }
    return SimpleNamespace(assembly_plan={"variants": [variant]}, all_candidates={}), variant


def test_all_fonts_request_becomes_one_op_covering_every_bar(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.text_appearance_enabled", True)
    job, variant = _thirteen_bars()
    snapshot = build_editor_snapshot(job, variant)
    assert all(
        "font_family" in t["supported_fields"] for t in snapshot["text_appearance"]["targets"]
    )

    output = _parse(
        snapshot,
        [
            {
                "op": "patch_text_appearance",
                "selector": {"scope": "editable_text", "quantifier": "all"},
                "patch": {"font_family": "Montserrat"},
                "text_appearance_version": 1,
            }
        ],
        utterance="Change all fonts to Montserrat",
    )
    assert output.outcome == "proposed"
    assert len(output.ops) == 1

    compiled = compile_editor_ops(job, variant, output.ops)
    saved = _saved_text(compiled)
    assert len(saved) == 13
    assert {row["font_family"] for row in saved} == {"Montserrat"}


def test_font_group_selector_limits_the_change_to_labels(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.text_appearance_enabled", True)
    job, variant = _thirteen_bars()
    snapshot = build_editor_snapshot(job, variant)
    output = _parse(
        snapshot,
        [
            {
                "op": "patch_text_appearance",
                "selector": {"scope": "editable_text", "quantifier": "all", "group": "labels"},
                "patch": {"font_family": "Montserrat"},
                "text_appearance_version": 1,
            }
        ],
    )
    assert len(output.ops) == 1
    saved = {row["id"]: row for row in _saved_text(compile_editor_ops(job, variant, output.ops))}
    assert saved["guided-title"]["font_family"] == "DM Sans"
    assert {r["font_family"] for i, r in saved.items() if i != "guided-title"} == {"Montserrat"}


def test_unknown_font_is_rejected_by_the_appearance_lane(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.text_appearance_enabled", True)
    job, variant = _thirteen_bars()
    snapshot = build_editor_snapshot(job, variant)
    output = _parse(
        snapshot,
        [
            {
                "op": "patch_text_appearance",
                "selector": {"scope": "editable_text", "quantifier": "all"},
                "patch": {"font_family": "Comic Sans Deluxe"},
                "text_appearance_version": 1,
            }
        ],
    )
    assert output.ops == []
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(
            job,
            variant,
            [
                {
                    "op": "patch_text_appearance",
                    "target_ids": ["guided-title"],
                    "patch": {"font_family": "Comic Sans Deluxe"},
                }
            ],
        )


def test_per_bar_font_ops_no_longer_exceed_the_bundle_cap() -> None:
    """The exact 2026-09-25 failure: thirteen patch_text_style ops -> 'at most eight'."""
    job, variant = _thirteen_bars()
    ops = [
        {"op": "patch_text_style", "bar_index": i, "patch": {"font_family": "Montserrat"}}
        for i in range(13)
    ]
    saved = _saved_text(compile_editor_ops(job, variant, ops))
    assert {row["font_family"] for row in saved} == {"Montserrat"}


def test_coalescing_keeps_later_patches_on_the_same_bar_authoritative() -> None:
    job, variant = _thirteen_bars()
    ops = [
        {"op": "patch_text_style", "bar_index": 0, "patch": {"font_family": "Montserrat"}},
        {"op": "patch_text_style", "bar_index": 1, "patch": {"font_family": "Inter"}},
        {"op": "patch_text_style", "bar_index": 1, "patch": {"font_family": "Montserrat"}},
        {"op": "patch_text_style", "bar_index": 2, "patch": {"font_family": "Montserrat"}},
    ]
    saved = {row["id"]: row for row in _saved_text(compile_editor_ops(job, variant, ops))}
    assert saved["guided-title"]["font_family"] == "Montserrat"
    assert saved["clip-label-unified-cut-1"]["font_family"] == "Montserrat"
    assert saved["clip-label-unified-cut-2"]["font_family"] == "Montserrat"


# ── KRI-203: the editor target exists for a rendered phone thread ─────────────


@pytest.mark.asyncio
async def test_style_only_request_on_a_rendered_phone_thread_routes_to_editor_ops(
    monkeypatch,
) -> None:
    job, variant = _job_and_variant()
    session = SimpleNamespace(
        target_job_id=job.id,
        target_variant_id="guided_story",
        plan_item_id=uuid.uuid4(),
        target_generation_id="gen-1",
    )
    thread = SimpleNamespace(active_creator_agent_session_id=uuid.uuid4(), creator_id=job.user_id)
    item = SimpleNamespace(current_job_id=job.id, clip_assignments=_assignments())

    async def get(model, _identifier):  # noqa: ANN001, ANN202
        return {CreationThread: thread, CreatorAgentSession: session, Job: job}[model]

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )
        ),
        rollback=AsyncMock(),
    )
    monkeypatch.setattr("app.config.settings.clip_facts_enabled", True)

    target = await planner._load_editor_target(db, thread_id=uuid.uuid4(), item=item)

    assert target is not None, "a published phone variant is an editor target (no forced re-plan)"
    assert target.snapshot["slots"] and target.snapshot["slots"][0]["facts"]
    shape = plan_shape_from_editor_snapshot(target.snapshot)
    # "Change all fonts" extracts no new requirement: the router keeps it on the editor tool.
    assert route_requirements([], shape, message="Change all fonts to Montserrat") == "editor_ops"


def test_planner_merges_per_bar_font_ops_so_the_tool_bound_is_not_hit() -> None:
    """The failing edge was the eight-op tool bound; the plan must now be a valid draft."""
    from app.kria.planner import adapt_editor_action
    from app.kria.registry import KRIA_TOOLS

    ops = [
        {"op": "patch_text_style", "bar_index": i, "patch": {"font_family": "Montserrat"}}
        for i in range(13)
    ]
    plan = adapt_editor_action(reply="Changed every font.", ops=ops)

    assert plan.mode == "act"
    arguments = plan.intents[0].arguments
    KRIA_TOOLS.get("draft.apply_editor_ops", 1).arguments_model.model_validate(arguments)
    assert len(arguments["operations"]) == 1
    assert arguments["operations"][0]["bar_indexes"] == list(range(13))


def test_planner_answers_an_oversized_bundle_instead_of_failing_the_turn() -> None:
    from app.kria.planner import adapt_editor_action

    ops = [{"op": "remove_text", "bar_index": i} for i in range(9)]
    plan = adapt_editor_action(reply="Removed them.", ops=ops)

    assert plan.mode == "respond"
    assert plan.turn_value == "recovery"
    assert "smaller steps" in (plan.response or "")


# ── Review round: repeated media, edited labels, guided gating, fail-open ─────


def _label_snapshot(slots: list[dict], bars: list[dict]) -> dict:
    return {
        "allowed_op_families": ["text", "title"],
        "label_facts": True,
        "text_bars": bars,
        "slots": slots,
        "total_duration_s": 10.0,
    }


def _slot(media_id: str, start: float, facts: list[dict] | None = None) -> dict:
    return {
        "key": f"s-{media_id}-{start}",
        "slot_id": f"s-{media_id}-{start}",
        "media_id": media_id,
        "output_start_s": start,
        "output_end_s": start + 1.0,
        "duration_s": 1.0,
        "in_s": 0.0,
        "removed": False,
        "transition_after": "cut",
        "look_preset": "none",
        "facts": facts
        or [{"kind": "landmark", "value": f"Place {media_id}", "provenance": "inferred"}],
    }


def _bar(bar_id: str, text: str, start: float, **extra) -> dict:
    return {
        "id": bar_id,
        "role": "generative_intro",
        "text": text,
        "start_s": start,
        "end_s": start + 1.0,
        "font_family": "DM Sans",
        **extra,
    }


def test_repeated_media_gets_one_label_bar_not_two() -> None:
    snapshot = _label_snapshot(
        [_slot("A", 0.0), _slot("B", 1.0), _slot("A", 2.0)],
        [_bar("guided-title", "Title", 0.0)],
    )
    output = _parse(snapshot, [{"op": "label_each_clip", "source": "facts"}])
    labels = output.ops[0]["labels"]
    assert [row["media_id"] for row in labels] == ["A", "B"]

    job, variant = _thirteen_bars()
    variant["text_elements"] = [
        {**variant["text_elements"][0], "id": "guided-title"},
    ]
    variant["ai_timeline"] = {
        "slots": [
            {
                "slot_id": "sa",
                "clip_index": 0,
                "media_id": "A",
                "output_start_s": 0.0,
                "output_end_s": 1.0,
                "duration_s": 1.0,
                "in_s": 0.0,
                "removed": False,
            },
            {
                "slot_id": "sb",
                "clip_index": 1,
                "media_id": "B",
                "output_start_s": 1.0,
                "output_end_s": 2.0,
                "duration_s": 1.0,
                "in_s": 0.0,
                "removed": False,
            },
            {
                "slot_id": "sa2",
                "clip_index": 0,
                "media_id": "A",
                "output_start_s": 2.0,
                "output_end_s": 3.0,
                "duration_s": 1.0,
                "in_s": 0.0,
                "removed": False,
            },
        ]
    }
    saved = _saved_text(compile_editor_ops(job, variant, output.ops))
    ids = [row["id"] for row in saved]
    assert len(ids) == len(set(ids)) == 3
    assert {"clip-label-media-A", "clip-label-media-B"} <= set(ids)


def test_two_bars_for_the_same_media_update_only_the_first() -> None:
    snapshot = _label_snapshot(
        [_slot("A", 0.0), _slot("B", 1.0), _slot("A", 2.0)],
        [
            _bar("clip-label-c1", "Old A", 0.0, clip_id="A", inferred=True),
            _bar("clip-label-c2", "Old B", 1.0, clip_id="B", inferred=True),
            _bar("clip-label-c3", "Old A", 2.0, clip_id="A", inferred=True),
        ],
    )
    # Not edited: the snapshot marks only creator-changed bars.
    output = _parse(snapshot, [{"op": "label_each_clip", "source": "facts"}])
    targets = [row.get("bar_id") for row in output.ops[0]["labels"]]
    assert targets == ["clip-label-c1", "clip-label-c2"]


def test_compile_refuses_duplicate_targets() -> None:
    job, variant = _thirteen_bars()
    op = {
        "op": "label_each_clip",
        "source": "facts",
        "labels": [
            {"media_id": "A", "text": "One", "start_s": 0.0, "end_s": 1.0},
            {"media_id": "A", "text": "Two", "start_s": 2.0, "end_s": 3.0},
        ],
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(job, variant, [op])
    bar = variant["text_elements"][1]["id"]
    op = {
        "op": "label_each_clip",
        "source": "facts",
        "labels": [
            {"media_id": "A", "text": "One", "bar_id": bar},
            {"media_id": "B", "text": "Two", "bar_id": bar},
        ],
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(job, variant, [op])


def test_a_hand_edited_label_is_never_overwritten() -> None:
    snapshot = _label_snapshot(
        [_slot("A", 0.0), _slot("B", 1.0)],
        [
            _bar("clip-label-c1", "My own words", 0.0, clip_id="A", inferred=False, edited=True),
            _bar("clip-label-c2", "Old B", 1.0, clip_id="B", inferred=True),
        ],
    )
    output = _parse(snapshot, [{"op": "label_each_clip", "source": "facts"}])
    assert [row["media_id"] for row in output.ops[0]["labels"]] == ["B"]
    assert output.ops[0]["kept_edited"] == 1

    only_edited = _label_snapshot(
        [_slot("A", 0.0)],
        [_bar("clip-label-c1", "My own words", 0.0, clip_id="A", edited=True)],
    )
    refused = _parse(only_edited, [{"op": "label_each_clip", "source": "facts"}])
    assert refused.ops == []
    assert "edited by hand" in refused.rejection_reasons[0]["detail"]


def test_snapshot_marks_a_creator_changed_label_as_edited() -> None:
    job, variant = _job_and_variant()
    snapshot = build_editor_snapshot(job, variant)
    label = next(b for b in snapshot["text_bars"] if b["id"] == "clip-label-unified-cut-1")
    assert "edited" not in label
    next(r for r in variant["text_elements"] if r["id"] == label["id"])["text"] = "Mine"
    again = build_editor_snapshot(job, variant)
    assert next(b for b in again["text_bars"] if b["id"] == label["id"])["edited"] is True


def test_an_unlinked_label_bar_blocks_a_second_overlapping_bar() -> None:
    # A clip-label bar whose cut id is missing from fast_cuts has no clip_id link.
    snapshot = _label_snapshot(
        [_slot("A", 0.0), _slot("B", 1.0)],
        [_bar("clip-label-orphan", "Somebody", 0.0)],
    )
    output = _parse(snapshot, [{"op": "label_each_clip", "source": "facts"}])
    assert [row["media_id"] for row in output.ops[0]["labels"]] == ["B"]
    assert all("inferred" not in row for row in output.ops[0]["labels"])


def test_more_than_forty_clips_can_be_labelled() -> None:
    job, variant = _thirteen_bars()
    op = {
        "op": "label_each_clip",
        "source": "facts",
        "labels": [
            {"media_id": f"m{i}", "text": f"P{i}", "start_s": float(i), "end_s": i + 1.0}
            for i in range(60)
        ],
    }
    saved = _saved_text(compile_editor_ops(job, variant, [op]))
    assert len([r for r in saved if r["id"].startswith("clip-label-media-")]) == 60


def test_guided_variants_never_advertise_clip_or_transition_families() -> None:
    job, variant = _job_and_variant()
    labelled = build_editor_snapshot(job, variant)["allowed_op_families"]
    assert not {"clip", "transition"} & set(labelled)

    # Same variant with no label lane: still a guided timeline, still withheld.
    variant["text_elements"] = [
        row for row in variant["text_elements"] if not row["id"].startswith("clip-label-")
    ]
    plain = build_editor_snapshot(job, variant)["allowed_op_families"]
    assert not {"clip", "transition"} & set(plain)
    assert "text" in plain


def test_visual_media_removal_is_withheld_when_it_could_strand_a_label(monkeypatch) -> None:
    job, variant = _job_and_variant()
    monkeypatch.setattr(
        "app.services.kria_editor_ops._removable_visual_media",
        lambda _job, _variant: [{"id": "vb-1", "kind": "media", "origin": "user"}],
    )
    assert "visual_media" not in build_editor_snapshot(job, variant)["allowed_op_families"]

    # Without a label lane the family stays available.
    variant["text_elements"] = [
        row for row in variant["text_elements"] if not row["id"].startswith("clip-label-")
    ]
    assert "visual_media" in build_editor_snapshot(job, variant)["allowed_op_families"]


def test_per_clip_label_routing_needs_the_copilot_to_be_able_to_fill_them() -> None:
    job, variant = _job_and_variant()
    every = BriefRequirement(id="r1", kind="text", scope="per_clip", description="label each clip")
    one = BriefRequirement(
        id="r2", kind="text", scope="clip:clip-1", literal="Besiktas Pier", description="fix"
    )

    with_facts = build_editor_snapshot(job, variant, clip_context=_context(job, variant))
    shape = plan_shape_from_editor_snapshot(with_facts)
    assert route_requirements([every], shape) == "editor_ops"

    no_facts = build_editor_snapshot(job, variant)
    assert "label_facts" not in no_facts
    shape = plan_shape_from_editor_snapshot(no_facts)
    assert shape.has_per_clip_text_lane
    assert route_requirements([every], shape) == "replan"
    # Correcting a single label needs only the existing bar.
    assert route_requirements([one], shape) == "editor_ops"


@pytest.mark.asyncio
async def test_clip_context_failures_degrade_to_no_context(monkeypatch) -> None:
    job, variant = _job_and_variant()
    thread = SimpleNamespace(creator_id=job.user_id)
    item = SimpleNamespace(clip_assignments=_assignments())
    monkeypatch.setattr("app.config.settings.clip_facts_enabled", True)
    monkeypatch.setattr("app.config.settings.kria_creative_brief_enabled", True)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad stored fact")

    async def brief_boom(*_args, **_kwargs):
        raise RuntimeError("db hiccup")

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    db = SimpleNamespace(begin_nested=lambda: Savepoint())
    monkeypatch.setattr(planner, "clip_facts_by_media_id", boom)
    monkeypatch.setattr(planner, "load_latest_brief", brief_boom)

    context = await planner._copilot_clip_context(
        db, thread=thread, thread_id=uuid.uuid4(), job=job, variant=variant, item=item
    )

    assert context == {}
