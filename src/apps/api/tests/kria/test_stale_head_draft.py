"""A chat draft built on an older render must never be overlaid on a saved render.

After an editor Save the session pointer and ``CreatorEditDraft.base_generation_id``
stay on the original generation while the variant's ``render_generation_id`` moves
on. Freshness is therefore the draft payload's own ``base_generation`` compared with
``variant_render_baseline(variant)``; one shared helper serves the planner and the
runtime so they cannot drift.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.kria import planner
from app.models import CreationThread, CreatorAgentSession, Job
from app.services.kria_editor_ops import (
    compile_editor_ops,
    fresh_editor_head_payload,
    merge_editor_draft,
    project_editor_draft,
)


def _variant(generation: str = "G2") -> dict:
    return {
        "variant_id": "v1",
        "render_status": "ready",
        "render_generation_id": generation,
        "resolved_archetype": "montage",
        "text_elements": [
            {
                "id": "title",
                "text": "Saved title",
                "start_s": 0.0,
                "end_s": 2.0,
                "font_family": "Playfair Display",
                "size_px": 90,
                "x_frac": 0.2,
                "y_frac": 0.3,
            },
            {
                "id": "label",
                "text": "Saved label",
                "start_s": 0.0,
                "end_s": 2.0,
                "font_family": "Playfair Display",
                "size_px": 40,
            },
        ],
        "ai_timeline": {
            "beat_grid": [],
            "slots": [
                {
                    "slot_id": f"s{i}",
                    "clip_index": i,
                    "source_gcs_path": f"users/u/{i}.mp4",
                    "source_duration_s": 8.0,
                    "in_s": 0.0,
                    "duration_beats": None,
                    "duration_s": 4.0,
                    "removed": False,
                    "transition_after": "cut",
                }
                for i in range(2)
            ],
        },
    }


def _head(base_generation: str, column: str = "G1") -> SimpleNamespace:
    stale_bars = copy.deepcopy(_variant()["text_elements"])
    stale_bars[0].update(x_frac=0.9, y_frac=0.9, text="Stale title")
    stale_bars[1]["text"] = "Stale label"
    return SimpleNamespace(
        base_generation_id=column,
        snapshot_json={
            "kind": "editor",
            "editor_payload": {"base_generation": base_generation, "text_elements": stale_bars},
        },
    )


def _job(variant: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["users/u/0.mp4", "users/u/1.mp4"]},
    )


def _bars(variant: dict) -> dict:
    return {row["id"]: row for row in variant["text_elements"]}


def test_stale_head_is_ignored_by_helper_and_projection() -> None:
    variant = _variant("G2")
    assert fresh_editor_head_payload(_head("G1"), variant) == {}
    assert fresh_editor_head_payload(None, variant) == {}
    projected = project_editor_draft(variant, fresh_editor_head_payload(_head("G1"), variant))
    assert _bars(projected)["title"]["x_frac"] == 0.2
    assert _bars(projected)["label"]["text"] == "Saved label"


def test_fresh_head_is_honored() -> None:
    variant = _variant("G2")
    payload = fresh_editor_head_payload(_head("G2"), variant)
    assert payload
    projected = project_editor_draft(variant, payload)
    assert _bars(projected)["title"]["x_frac"] == 0.9
    assert _bars(projected)["label"]["text"] == "Stale label"


def test_variant_without_generation_matches_empty_payload_baseline() -> None:
    variant = _variant("")
    variant.pop("render_generation_id")
    assert fresh_editor_head_payload(_head(""), variant)


def test_runtime_compile_path_builds_on_saved_variant_when_head_stale(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"text_elements": True, "timeline": True},
    )
    variant = _variant("G2")
    job = _job(variant)
    ops = [{"op": "set_total_duration", "target_s": 4, "strategy": "proportional"}]

    def run(head):  # noqa: ANN001, ANN202
        prior = fresh_editor_head_payload(head, variant)
        compiled = compile_editor_ops(job, project_editor_draft(variant, prior), ops)
        payload = compiled.payload.model_dump(mode="json", exclude_none=True)
        return payload, merge_editor_draft(prior, payload), prior

    payload, merged, prior = run(_head("G1"))
    assert prior == {}
    bars = {row["id"]: row for row in merged.get("text_elements") or []}
    if bars:  # text lane present only when the compile emitted it
        assert bars["title"].get("x_frac", 0.2) == 0.2
        assert bars["label"]["text"] == "Saved label"
    assert "Stale" not in str(merged)
    assert merged["base_generation"] == "G2"

    _, merged_fresh, prior_fresh = run(_head("G2"))
    assert prior_fresh
    assert "Stale label" in str(merged_fresh)


def _planner_db(head, session, job):  # noqa: ANN001, ANN202
    thread = SimpleNamespace(active_creator_agent_session_id=session.id)

    async def get(model, _identifier):  # noqa: ANN001, ANN202
        return {CreationThread: thread, CreatorAgentSession: session, Job: job}[model]

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: head,
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )
        ),
        rollback=AsyncMock(),
    )


async def _planner_snapshot(monkeypatch, head, generation="G2"):  # noqa: ANN001, ANN202
    variant = _variant(generation)
    job = _job(variant)
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job.id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        target_job_id=job.id,
        target_variant_id="v1",
        # The stale pointer: equals the head row's column, not the variant's render.
        target_generation_id="G1",
    )
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner,
        "build_editor_snapshot",
        lambda _job, v, **_k: {"allowed_op_families": ["text"], "text_bars": v["text_elements"]},
    )
    target = await planner._load_editor_target(
        _planner_db(head, session, job), thread_id=uuid.uuid4(), item=item
    )
    return target


async def _bars_of(target) -> dict:  # noqa: ANN001
    snap = target.snapshot if hasattr(target, "snapshot") else target[0]
    return {row["id"]: row for row in snap["text_bars"]}


@pytest.mark.asyncio
async def test_planner_ignores_stale_head_even_when_session_and_column_match(monkeypatch) -> None:
    target = await _planner_snapshot(monkeypatch, _head("G1", column="G1"))
    assert target is not None
    bars = await _bars_of(target)
    assert bars["title"]["x_frac"] == 0.2
    assert bars["title"]["y_frac"] == 0.3
    assert bars["label"]["text"] == "Saved label"


@pytest.mark.asyncio
async def test_planner_honors_fresh_head(monkeypatch) -> None:
    target = await _planner_snapshot(monkeypatch, _head("G2", column="G1"))
    bars = await _bars_of(target)
    assert bars["title"]["x_frac"] == 0.9
    assert bars["label"]["text"] == "Stale label"


def test_multi_turn_chat_save_chat_does_not_revert_the_save(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"text_elements": True, "timeline": True},
    )
    # Turn 1 on G1: chat draft stamped with G1.
    g1 = _variant("G1")
    turn1 = merge_editor_draft(
        {},
        compile_editor_ops(
            _job(g1), g1, [{"op": "set_total_duration", "target_s": 6, "strategy": "proportional"}]
        ).payload.model_dump(mode="json", exclude_none=True),
    )
    assert turn1["base_generation"] == "G1"
    head = SimpleNamespace(snapshot_json={"kind": "editor", "editor_payload": turn1})
    # Editor Save: generation bumps, creator moved the title and renamed the label.
    g2 = _variant("G2")
    g2["text_elements"][0].update(x_frac=0.55, y_frac=0.12, size_px=120)
    g2["text_elements"][1]["text"] = "Creator label"
    # Turn 2 builds on the saved variant, not the G1 head.
    prior = fresh_editor_head_payload(head, g2)
    assert prior == {}
    compiled = compile_editor_ops(
        _job(g2),
        project_editor_draft(g2, prior),
        [{"op": "set_total_duration", "target_s": 5, "strategy": "proportional"}],
    )
    merged = merge_editor_draft(prior, compiled.payload.model_dump(mode="json", exclude_none=True))
    assert merged["base_generation"] == "G2"
    text = str(merged)
    assert "Stale" not in text
    for row in merged.get("text_elements") or []:
        if row["id"] == "title":
            assert row["x_frac"] == 0.55 and row["size_px"] == 120
        if row["id"] == "label":
            assert row["text"] == "Creator label"
