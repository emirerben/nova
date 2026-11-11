"""KRI-1335: `collapse_empty_panels` survives every boundary and is gated to protocol 47.

Ways this can fail, and the test that catches each:
* a section without the field changes its bytes (wire, stored timeline, recipe);
* Save, reopen, the recipe or an AI op drops or resurrects the field;
* an app build below 47 saves a collapsing timeline and renders it with fixed panels;
* a recipe that collapses is accepted without the `splitCollapse` capability, or one that
  does not collapse demands it;
* the section duration under collapse is not the longest member's end;
* the worker (draft turn) and the request (approve) offer collapse to different builds.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.kria.split_screen import SplitScreenSection, layout_split_timeline
from app.services.client_protocol import (
    SPLIT_COLLAPSE_PROTOCOL,
    bind_sending_build,
    clear_request_context,
    set_client_protocol,
    unbind_sending_build,
)


def section(**changes):
    return SplitScreenSection.model_validate(
        {"id": "s", "duration_s": 4, "panel_count": 2, "panel_weights": [1, 1], **changes}
    )


def member(panel, offset=0, duration=4):
    return {
        "slot_id": f"p{panel}-{offset}",
        "duration_s": duration,
        "split_section_id": "s",
        "split_panel_index": panel,
        "split_offset_s": offset,
    }


@pytest.fixture(autouse=True)
def _clean_protocol():
    yield
    clear_request_context()


def verified(monkeypatch, *features):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, *features],
    )


def test_default_false_section_keeps_its_exact_bytes():
    legacy = {
        "id": "s",
        "duration_s": 4.0,
        "panel_count": 2,
        "panel_weights": [1.0, 1.0],
        "gutter": 0.0,
        "background_color": "#000000",
    }
    assert section().model_dump(mode="json") == legacy
    assert section(collapse_empty_panels=False).model_dump(mode="json") == legacy
    assert section(collapse_empty_panels=True).model_dump(mode="json") == {
        **legacy,
        "collapse_empty_panels": True,
    }
    assert SplitScreenSection.model_validate(legacy).collapse_empty_panels is False


def test_collapse_section_duration_is_the_longest_member_end():
    rows = [member(0, 0, 2), member(1, 1, 2.5)]
    starts, sections, total = layout_split_timeline(rows, [section(collapse_empty_panels=True)])
    assert (starts, total) == ([0, 1], 3.5)
    assert (sections[0].start, sections[0].end, sections[0].collapse_empty_panels) == (0, 3.5, True)
    _, fixed, fixed_total = layout_split_timeline(rows, [section()])
    assert (fixed[0].end, fixed_total) == (4, 4)


def test_collapse_ignores_a_longer_stored_duration_and_follows_later_serial_clips():
    rows = [member(0, 0, 1), {"duration_s": 2}]
    starts, sections, total = layout_split_timeline(
        rows, [section(duration_s=9, collapse_empty_panels=True)]
    )
    assert (starts, sections[0].end, total) == ([0, 1], 1, 3)


def test_collapse_still_needs_a_member_and_a_valid_panel():
    with pytest.raises(ValueError):
        layout_split_timeline([{"duration_s": 1}], [section(collapse_empty_panels=True)])
    with pytest.raises(ValueError):
        layout_split_timeline([member(2)], [section(collapse_empty_panels=True)])


def recipe_with(collapse):
    from app.kria.recipes_v2 import EditRecipeV2

    raw = json.loads((Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json").read_text())
    entry = {"id": "s", "start": 0, "end": 4, "panel_weights": [1, 1]}
    if collapse is not None:
        entry["collapse_empty_panels"] = collapse
    raw["split_screen_sections"] = [entry]
    raw["tracks"][0]["clips"][0]["split_screen"] = {"section_id": "s", "panel_index": 1}
    return EditRecipeV2.model_validate(raw)


@pytest.mark.parametrize(("flag", "needs"), [(None, False), (False, False), (True, True)])
def test_recipe_requires_split_collapse_only_when_a_section_collapses(flag, needs):
    recipe = recipe_with(flag)
    assert "splitScreen" in recipe.required_capabilities
    assert ("splitCollapse" in recipe.required_capabilities) is needs
    wire = recipe.model_dump(mode="json")
    entry = wire["split_screen_sections"][0]
    assert ("collapse_empty_panels" in entry) is needs
    from app.kria.recipes_v2 import EditRecipeV2

    assert EditRecipeV2.model_validate(wire).required_capabilities == recipe.required_capabilities


def test_phone_pilot_refuses_a_collapsing_recipe_until_the_token_is_verified(monkeypatch):
    from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe

    verified(monkeypatch, "splitScreen")
    recipe = recipe_with(True)
    with pytest.raises(PhoneCapabilityUnavailable) as error:
        validate_phone_pilot_recipe(recipe)
    assert error.value.capability == "splitCollapse"


def test_gates_read_the_build_and_the_verified_tokens(monkeypatch):
    from app.services.phone_rollout import split_collapse_planned_for, split_collapse_supported

    assert SPLIT_COLLAPSE_PROTOCOL == 47
    assert not split_collapse_planned_for(47)
    verified(monkeypatch, "splitScreen")
    assert not split_collapse_planned_for(47)
    verified(monkeypatch, "splitCollapse")
    assert [split_collapse_planned_for(p) for p in (None, 45, 46, 47, 48)] == [
        False,
        False,
        False,
        True,
        True,
    ]
    set_client_protocol(46)
    assert not split_collapse_supported()
    set_client_protocol(47)
    assert split_collapse_supported()
    clear_request_context()
    assert split_collapse_supported()


def authored(monkeypatch, **tokens):
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    verified(monkeypatch, "splitScreen", "sourceCrop", *tokens.get("extra", ()))
    return job, variant


def collapse_payload(duration=4, collapse=True):
    from app.routes import generative_jobs as gj

    return gj.EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        timeline_slots=[
            gj.TimelineSlotEdit(
                slot_id="11111111-1111-4111-8111-111111111111",
                clip_index=1,
                in_s=2,
                duration_s=2,
                source_end_s=8,
                split_section_id="s",
                split_panel_index=0,
                split_offset_s=0,
            ),
            gj.TimelineSlotEdit(
                clip_index=1,
                in_s=7,
                duration_s=2,
                split_section_id="s",
                split_panel_index=1,
                split_offset_s=0.5,
                muted=True,
            ),
        ],
        split_screen_sections=[section(duration_s=duration, collapse_empty_panels=collapse)],
    )


def test_save_reopen_and_recipe_keep_the_field_and_derive_the_duration(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status

    job, _ = authored(monkeypatch, extra=("splitCollapse",))
    gj.prepare_editor_commit(job, "original_text", collapse_payload(duration=4))
    saved = job.assembly_plan["variants"][0]["user_timeline"]["split_screen_sections"][0]
    assert saved["collapse_empty_panels"] is True
    assert saved["duration_s"] == 2.5
    recipe = device_status(job, "original_text").request.recipe
    assert recipe.duration == 2.5
    assert recipe.split_screen_sections[0].collapse_empty_panels is True
    assert {"splitScreen", "splitCollapse"} <= recipe.required_capabilities
    reopened = gj.dispatch_get_timeline(
        job, "original_text", sign_url=lambda *_: "https://example.test/media"
    )
    assert reopened["split_screen_sections"][0]["collapse_empty_panels"] is True
    assert reopened["total_duration_s"] == 2.5


def test_save_without_the_field_stays_byte_identical(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch)
    gj.prepare_editor_commit(job, "original_text", collapse_payload(collapse=False))
    saved = job.assembly_plan["variants"][0]["user_timeline"]["split_screen_sections"][0]
    assert "collapse_empty_panels" not in saved
    assert saved["duration_s"] == 4


@pytest.mark.parametrize("protocol", [45, 46])
def test_a_build_below_47_cannot_save_a_collapsing_timeline(monkeypatch, protocol):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, extra=("splitCollapse",))
    set_client_protocol(protocol)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(job, "original_text", collapse_payload())
    assert error.value.status_code == 426
    if protocol == 46:
        assert error.value.detail == {
            "code": "client_update_required",
            "feature": "split_collapse",
            "minimum_client_protocol": 47,
        }
    else:
        assert error.value.detail["feature"] == "split_screen"


def test_a_build_below_47_cannot_save_over_a_saved_collapsing_timeline(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, extra=("splitCollapse",))
    set_client_protocol(47)
    gj.prepare_editor_commit(job, "original_text", collapse_payload())
    saved = job.assembly_plan["variants"][0]
    set_client_protocol(46)
    from app.services.split_screen_editor import require_split_screen_commit

    with pytest.raises(HTTPException) as error:
        require_split_screen_commit(saved, gj.EditorCommitRequest(text_elements=[]))
    assert error.value.detail["feature"] == "split_collapse"
    set_client_protocol(47)
    require_split_screen_commit(saved, gj.EditorCommitRequest(text_elements=[]))


def test_build_46_still_saves_an_ordinary_split(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch)
    set_client_protocol(46)
    gj.prepare_editor_commit(job, "original_text", collapse_payload(collapse=False))


def test_collapse_is_refused_while_the_token_is_unverified(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch)
    set_client_protocol(47)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(job, "original_text", collapse_payload())
    assert (error.value.status_code, error.value.detail) == (
        422,
        {"code": "split_collapse_unavailable"},
    )


def test_device_render_426_names_collapse_for_46_and_split_for_45(monkeypatch):
    from app.routes.device_render import _client_update_detail
    from app.services.device_render import device_status

    job, _ = authored(monkeypatch, extra=("splitCollapse",))
    from app.routes import generative_jobs as gj

    gj.prepare_editor_commit(job, "original_text", collapse_payload())
    status = device_status(job, "original_text")
    expectations = {45: ("split_screen", 46), 46: ("split_collapse", 47), 47: None}
    for protocol, expected in expectations.items():
        set_client_protocol(protocol)
        detail = _client_update_detail(job, {}, status)
        assert (
            None if detail is None else (detail["feature"], detail["minimum_client_protocol"])
        ) == expected


@pytest.mark.parametrize("protocol", [45, 46, 47])
def test_worker_and_request_offer_collapse_to_the_same_builds(monkeypatch, protocol):
    """KRI-1321: the draft turn binds the sender's build; the request reads its header."""
    from app.routes import generative_jobs as gj

    job, variant = authored(monkeypatch, extra=("splitCollapse",))
    variant["render_destination"] = "device"
    set_client_protocol(protocol)
    request_side = gj._editor_capabilities(job, variant).get("split_collapse") is True
    clear_request_context()
    token = bind_sending_build(protocol)
    try:
        worker_side = gj._editor_capabilities(job, variant).get("split_collapse") is True
    finally:
        unbind_sending_build(token)
    assert request_side is worker_side is (protocol >= 47)
    assert gj._editor_capabilities(job, variant).get("split_collapse") is not True


def _ai(monkeypatch, protocol=47, tokens=("splitScreen", "splitCollapse")):
    from app.services import kria_editor_ops as editor
    from tests.services.test_kria_editor_ops import _job, _variant

    variant = _variant()
    variant["render_destination"] = "device"
    variant["editor_timeline_mode"] = "authored"
    variant["user_timeline"] = deepcopy(variant.pop("ai_timeline"))
    monkeypatch.setattr(
        editor, "_editor_capabilities", lambda *args: {"timeline": True, "clips": {}}
    )
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, *tokens],
    )
    token = bind_sending_build(protocol)
    return _job(variant), variant, token


def _apply(job, variant, raw):
    from app.agents.edit_copilot import _ParseState, parse_editor_operation
    from app.services import kria_editor_ops as editor

    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    parsed = parse_editor_operation(raw, snapshot, _ParseState(0.99))
    assert parsed is not None, raw
    draft = editor.compile_editor_ops(job, variant, [parsed])
    payload = draft.payload.model_dump(mode="json", exclude_none=True)
    return editor.project_editor_draft(variant, payload, job), payload


def test_ai_create_update_and_unrelated_edit_preserve_and_clear_the_field(monkeypatch):
    job, variant, token = _ai(monkeypatch)
    try:
        variant, payload = _apply(
            job,
            variant,
            {
                "op": "create_split_screen",
                "slot_indexes": [0, 1],
                "panel_count": 2,
                "collapse_empty_panels": True,
            },
        )
        created = payload["split_screen_sections"][0]
        assert created["collapse_empty_panels"] is True
        variant, payload = _apply(
            job,
            variant,
            {"op": "update_split_screen", "section_id": created["id"], "gutter": 0.02},
        )
        kept = payload["split_screen_sections"][0]
        assert (kept["collapse_empty_panels"], kept["gutter"]) == (True, 0.02)
        variant, payload = _apply(
            job,
            variant,
            {
                "op": "update_split_screen",
                "section_id": created["id"],
                "collapse_empty_panels": False,
            },
        )
        assert "collapse_empty_panels" not in payload["split_screen_sections"][0]
    finally:
        unbind_sending_build(token)


def test_ai_trim_of_a_collapsing_member_shortens_the_section(monkeypatch):
    job, variant, token = _ai(monkeypatch)
    try:
        variant, payload = _apply(
            job,
            variant,
            {
                "op": "create_split_screen",
                "slot_indexes": [0, 1],
                "panel_count": 2,
                "collapse_empty_panels": True,
            },
        )
        assert payload["split_screen_sections"][0]["duration_s"] > 1.5
        for index, seconds in ((0, 1.0), (1, 1.5)):
            variant, payload = _apply(
                job,
                variant,
                {"op": "set_clip_duration", "slot_index": index, "duration_s": seconds},
            )
        assert payload["split_screen_sections"][0]["duration_s"] == 1.5
        assert payload["split_screen_sections"][0]["collapse_empty_panels"] is True
    finally:
        unbind_sending_build(token)


@pytest.mark.parametrize(
    ("protocol", "tokens"),
    [
        (46, ("splitScreen", "splitCollapse")),
        (47, ("splitScreen",)),
        (None, ("splitScreen", "splitCollapse")),
    ],
)
def test_ai_cannot_set_collapse_for_an_unqualified_build(monkeypatch, protocol, tokens):
    from app.agents.edit_copilot import _ParseState, parse_editor_operation
    from app.services import kria_editor_ops as editor

    job, variant, token = _ai(monkeypatch, protocol=protocol, tokens=tokens)
    try:
        snapshot = editor.build_editor_snapshot(job, variant)
        snapshot["editor_ops_version"] = 2
        parsed = parse_editor_operation(
            {
                "op": "create_split_screen",
                "slot_indexes": [0, 1],
                "panel_count": 2,
                "collapse_empty_panels": True,
            },
            snapshot,
            _ParseState(0.99),
        )
        assert parsed is not None
        with pytest.raises(editor.KriaEditorOpError):
            editor.compile_editor_ops(job, variant, [parsed])
    finally:
        unbind_sending_build(token)


COLLAPSE_CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures/split_screen_collapse_timeline.json").read_text()
)["cases"]


def active_rects(section_row, rows, time):
    """The contract's layout, written from its prose: hard cut on half-open member windows."""
    active = sorted(
        {
            r["split_panel_index"]
            for r in rows
            if r.get("split_section_id") == section_row["id"]
            and r["split_offset_s"] <= time < r["split_offset_s"] + r["duration_s"]
        }
    )
    weights = section_row["panel_weights"]
    available = 1 - section_row["gutter"] * (len(active) - 1)
    total = sum(weights[i] for i in active)
    y, result = 0.0, []
    for index in active:
        height = available * weights[index] / total
        result.append({"panel_index": index, "y": round(y, 6), "height": round(height, 6)})
        y += height + section_row["gutter"]
    return active, result


@pytest.mark.parametrize("case", COLLAPSE_CASES, ids=lambda case: case["name"])
def test_shared_collapse_fixture_matches_the_server_projection(case):
    starts, sections, total = layout_split_timeline(case["slots"], case["split_screen_sections"])
    assert total == case["expected_duration_s"]
    assert [
        {"slot_id": row["slot_id"], "start_s": start, "end_s": start + row["duration_s"]}
        for row, start in zip(case["slots"], starts, strict=True)
    ] == case["expected_windows"]
    assert [{"id": s.id, "start": s.start, "end": s.end} for s in sections] == case[
        "expected_recipe_sections"
    ]
    assert all(s.collapse_empty_panels for s in sections)
    definition = case["split_screen_sections"][0]
    for sample in case["canvas_samples"]:
        active, rects = active_rects(definition, case["slots"], sample["section_time_s"])
        assert (active, rects) == (sample["active_panels"], sample["rects"]), sample
