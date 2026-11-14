"""KRI-1421: a blank block fills a split panel with a flat colour (protocol 51).

Ways this can fail, and the test that catches each:
* a blank placed outside a split panel, with a `clip_index`, with a bad colour, or overlapping a
  neighbour in its panel is accepted by Save (the shapes the client writes and the server's render
  compile must agree on, KRI-1423): the schema, layout and Save tests below;
* a slot without a `clip_index` that is not a blank stops being refused;
* Save accepts a blank but the render compile refuses it, or compiles it as a source clip: the
  authored Save test runs the real recipe compile;
* the colour or kind is dropped on Save, reopen or the recipe (pydantic ignores unknown keys);
* a section made only of blanks reaches the device (nothing to play);
* a build below 51 saves over, polls or renders a timeline that holds a blank and loses it;
* the recipe asks for `splitBlankPanel` when no blank is drawn, or omits it when one is;
* the token is unverified and the blank still ships.
"""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import settings
from app.kria.split_screen import SplitScreenSection, layout_split_timeline
from app.services.client_protocol import (
    SPLIT_BLANK_PANEL_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)

FIXTURE = (
    Path(__file__).parents[3]
    / "ios/Packages/KriaMediaEngine/Tests/KriaMediaEngineTests"
    / "Fixtures/split-screen-blank-recipe.json"
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


def blank(panel, offset=0, duration=2, color="#FF0000", **changes):
    return {
        "slot_id": f"b{panel}-{offset}",
        "media_kind": "blank",
        "color": color,
        "in_s": 0,
        "duration_s": duration,
        "split_section_id": "s",
        "split_panel_index": panel,
        "split_offset_s": offset,
        **changes,
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


def test_the_protocol_is_the_next_free_number():
    assert SPLIT_BLANK_PANEL_PROTOCOL == 51


def test_layout_gives_a_blank_its_own_window_and_exports_it_with_the_section():
    rows = [member(0), blank(1, offset=1, duration=2)]
    starts, sections, total = layout_split_timeline(rows, [section()])
    assert (starts, total) == ([0, 1], 4)
    wire = sections[0].model_dump(mode="json")
    assert wire["blanks"] == [
        {"id": "b1-1", "panel_index": 1, "start": 1.0, "end": 3.0, "color": "#FF0000"}
    ]
    plain = layout_split_timeline([member(0), member(1)], [section()])[1][0]
    assert "blanks" not in plain.model_dump(mode="json")


def test_a_blank_keeps_a_collapsing_section_alive_while_it_plays():
    rows = [member(0, duration=1), blank(1, offset=0, duration=3)]
    _, sections, total = layout_split_timeline(rows, [section(collapse_empty_panels=True)])
    assert (sections[0].end, total) == (3, 3)


@pytest.mark.parametrize(
    "rows",
    [
        [blank(1), blank(1, offset=1)],
        [member(0), blank(0, offset=1)],
        [blank(0, offset=0, duration=4)],
        [member(0), {**blank(1), "split_section_id": None, "split_panel_index": None}],
        [member(0), {k: v for k, v in blank(1).items() if k != "split_offset_s"}],
        [{**member(0), "transition_after": "crossfade"}, blank(0, offset=3.8, duration=0.2)],
        [member(0), blank(1, color="red")],
        [member(0), blank(1, color=None)],
    ],
    ids=[
        "only-blanks",
        "overlaps-a-clip-in-its-panel",
        "section-has-no-media",
        "outside-a-split",
        "no-offset",
        "crossfade-into-a-blank",
        "named-colour",
        "no-colour",
    ],
)
def test_layout_refuses_a_blank_that_cannot_render(rows):
    with pytest.raises(ValueError):
        layout_split_timeline(rows, [section()])


def test_two_blanks_may_sit_end_to_end_in_one_panel():
    rows = [
        member(0),
        blank(1, offset=0, duration=2),
        blank(1, offset=2, duration=2, color="#00FF00"),
    ]
    _, sections, _ = layout_split_timeline(rows, [section()])
    assert [(b.start, b.end, b.color) for b in sections[0].blanks] == [
        (0, 2, "#FF0000"),
        (2, 4, "#00FF00"),
    ]


def edit(**fields):
    from app.routes import generative_jobs as gj

    return gj.TimelineSlotEdit(**fields)


def test_a_blank_slot_is_valid_without_a_clip_index_and_uppercases_its_colour():
    slot = edit(**{**blank(1, color="#ff00aa"), "slot_id": None})
    assert slot.clip_index is None
    assert slot.color == "#FF00AA"
    assert slot.model_dump(mode="json", exclude_none=True)["media_kind"] == "blank"


@pytest.mark.parametrize(
    "fields",
    [
        {**blank(1), "clip_index": 0},
        {**blank(1), "color": "#FFF"},
        {**blank(1), "color": "red"},
        {**blank(1), "color": None},
        {**blank(1), "color": "#FF000G"},
        {**blank(1), "playback_rate": 2},
        {**blank(1), "source_end_s": 3},
        {**blank(1), "muted": False, "source_audio_volume": 1},
        {**blank(1), "transition_after": "crossfade"},
        {"in_s": 0, "duration_s": 2},
        {"media_kind": "video", "in_s": 0, "duration_s": 2},
    ],
    ids=[
        "clip-index",
        "short-hex",
        "name",
        "null-colour",
        "bad-digit",
        "speed",
        "source-edge",
        "source-audio",
        "transition",
        "no-source-no-kind",
        "video-with-no-source",
    ],
)
def test_slot_schema_refuses_the_wrong_shape(fields):
    with pytest.raises(ValidationError):
        edit(**{k: v for k, v in fields.items() if k != "slot_id"})


def test_a_source_slot_keeps_its_old_shape_whatever_kind_or_colour_it_carries():
    slot = edit(clip_index=0, in_s=1, duration_s=2, media_kind="image", color="red")
    dumped = slot.model_dump(mode="json")
    assert "media_kind" not in dumped and "color" not in dumped
    assert dumped["clip_index"] == 0


def authored(monkeypatch, *tokens):
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    verified(monkeypatch, "splitScreen", "sourceCrop", *tokens)
    return job, variant


def payload(*, rows=None, collapse=False):
    from app.routes import generative_jobs as gj

    clip = gj.TimelineSlotEdit(
        slot_id="11111111-1111-4111-8111-111111111111",
        clip_index=1,
        in_s=2,
        duration_s=4,
        split_section_id="s",
        split_panel_index=0,
        split_offset_s=0,
    )
    fill = (
        rows if rows is not None else [edit(**{**blank(1, offset=1, duration=2), "slot_id": None})]
    )
    return gj.EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        timeline_slots=[clip, *fill],
        split_screen_sections=[section(collapse_empty_panels=collapse)],
    )


def test_save_compiles_reopens_and_renders_the_blank_through_the_real_recipe_path(monkeypatch):
    from app.kria.recipes_v2 import EditRecipeV2
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status

    job, _ = authored(monkeypatch, "splitBlankPanel")
    gj.prepare_editor_commit(job, "original_text", payload())
    saved = job.assembly_plan["variants"][0]["user_timeline"]["slots"]
    stored = next(row for row in saved if row.get("media_kind") == "blank")
    assert (stored["color"], stored["split_panel_index"], stored["split_offset_s"]) == (
        "#FF0000",
        1,
        1,
    )
    assert stored.get("clip_index") is None
    assert "source_gcs_path" not in stored and "media_id" not in stored

    recipe = device_status(job, "original_text").request.recipe
    assert {"splitScreen", "splitBlankPanel"} <= recipe.required_capabilities
    clips = [clip for track in recipe.tracks for clip in track.clips]
    assert [clip.split_screen.panel_index for clip in clips] == [0]
    blanks = recipe.split_screen_sections[0].blanks
    assert [(b.panel_index, b.start, b.end, b.color) for b in blanks] == [(1, 1, 3, "#FF0000")]
    assert recipe.duration == 4
    wire = recipe.model_dump(mode="json")
    assert EditRecipeV2.model_validate(wire).split_screen_sections[0].blanks == blanks

    reopened = gj.dispatch_get_timeline(
        job, "original_text", sign_url=lambda *_: "https://example.test/media"
    )
    row = next(r for r in reopened["slots"] if r.get("media_kind") == "blank")
    assert (row["color"], row["split_panel_index"], row.get("clip_index")) == ("#FF0000", 1, None)
    assert reopened["total_duration_s"] == 4


def test_a_blank_in_a_collapsing_section_extends_it_and_keeps_the_panel_visible(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status

    job, _ = authored(monkeypatch, "splitBlankPanel", "splitCollapse")
    rows = [edit(**{**blank(1, offset=3, duration=3), "slot_id": None})]
    gj.prepare_editor_commit(job, "original_text", payload(rows=rows, collapse=True))
    recipe = device_status(job, "original_text").request.recipe
    assert recipe.duration == 6
    assert recipe.split_screen_sections[0].end == 6
    assert "splitCollapse" in recipe.required_capabilities


def test_save_keeps_a_blank_colour_change_and_a_second_blank(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, "splitBlankPanel")
    second = edit(**{**blank(1, offset=3, duration=1, color="#00ff00"), "slot_id": None})
    first = edit(**{**blank(1, offset=1, duration=2, color="#0000FF"), "slot_id": None})
    gj.prepare_editor_commit(job, "original_text", payload(rows=[first, second]))
    colors = [
        row["color"]
        for row in job.assembly_plan["variants"][0]["user_timeline"]["slots"]
        if row.get("media_kind") == "blank"
    ]
    assert colors == ["#0000FF", "#00FF00"]


@pytest.mark.parametrize(
    "build",
    [
        lambda: edit(
            **{
                **blank(1),
                "slot_id": None,
                "split_section_id": None,
                "split_panel_index": None,
                "split_offset_s": None,
            }
        ),
    ],
    ids=["outside-a-split"],
)
def test_save_refuses_a_blank_outside_a_split_panel(monkeypatch, build):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, "splitBlankPanel")
    clip = gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(
            job,
            "original_text",
            gj.EditorCommitRequest(
                base_generation="first", editor_state_version=2, timeline_slots=[clip, build()]
            ),
        )
    assert error.value.status_code == 422


def test_save_refuses_a_section_whose_only_members_are_blanks(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, "splitBlankPanel")
    only = gj.EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        timeline_slots=[edit(**{**blank(0, duration=4), "slot_id": None})],
        split_screen_sections=[section()],
    )
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(job, "original_text", only)
    assert error.value.status_code == 422


def test_save_without_a_blank_still_refuses_a_clip_row_with_no_source(monkeypatch):
    from app.routes import generative_jobs as gj

    with pytest.raises(ValidationError):
        gj.TimelineSlotEdit(in_s=0, duration_s=2)


@pytest.mark.parametrize("protocol", [48, 50])
def test_a_build_below_51_cannot_save_a_blank(monkeypatch, protocol):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, "splitBlankPanel")
    set_client_protocol(protocol)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(job, "original_text", payload())
    assert error.value.status_code == 426
    assert error.value.detail == {
        "code": "client_update_required",
        "feature": "split_blank_panel",
        "minimum_client_protocol": 51,
    }


def test_a_build_below_51_cannot_save_over_a_saved_blank_and_51_can(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.split_screen_editor import require_split_screen_commit

    job, _ = authored(monkeypatch, "splitBlankPanel")
    set_client_protocol(51)
    gj.prepare_editor_commit(job, "original_text", payload())
    saved = job.assembly_plan["variants"][0]
    set_client_protocol(50)
    with pytest.raises(HTTPException) as error:
        require_split_screen_commit(saved, gj.EditorCommitRequest(text_elements=[]))
    assert error.value.detail["feature"] == "split_blank_panel"
    set_client_protocol(51)
    require_split_screen_commit(saved, gj.EditorCommitRequest(text_elements=[]))


def test_a_build_below_51_is_refused_a_timeline_that_holds_a_blank(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch, "splitBlankPanel")
    gj.prepare_editor_commit(job, "original_text", payload())
    set_client_protocol(50)
    with pytest.raises(HTTPException) as error:
        gj.dispatch_get_timeline(
            job, "original_text", sign_url=lambda *_: "https://example.test/media"
        )
    assert error.value.status_code == 426
    assert error.value.detail["feature"] == "split_blank_panel"
    set_client_protocol(51)
    gj.dispatch_get_timeline(job, "original_text", sign_url=lambda *_: "https://example.test/media")


def test_a_blank_is_refused_while_the_token_is_unverified(monkeypatch):
    from app.routes import generative_jobs as gj

    job, _ = authored(monkeypatch)
    set_client_protocol(51)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(job, "original_text", payload())
    assert (error.value.status_code, error.value.detail) == (
        422,
        {"code": "split_blank_panel_unavailable"},
    )


def test_device_render_426_names_the_blank_for_a_build_below_51(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.routes.device_render import _client_update_detail
    from app.services.device_render import device_status

    job, _ = authored(monkeypatch, "splitBlankPanel")
    gj.prepare_editor_commit(job, "original_text", payload())
    status = device_status(job, "original_text")
    expectations = {50: ("split_blank_panel", 51), 51: None}
    for protocol, expected in expectations.items():
        set_client_protocol(protocol)
        detail = _client_update_detail(job, {}, status)
        assert (
            None if detail is None else (detail["feature"], detail["minimum_client_protocol"])
        ) == expected


def test_phone_pilot_refuses_a_blank_recipe_until_the_token_is_verified(monkeypatch):
    from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe

    verified(monkeypatch, "splitScreen")
    recipe = recipe_from_fixture()
    with pytest.raises(PhoneCapabilityUnavailable) as error:
        validate_phone_pilot_recipe(recipe)
    assert error.value.capability == "splitBlankPanel"


@pytest.mark.parametrize("protocol", [50, 51])
def test_the_editor_offers_the_blank_only_to_builds_that_draw_it(monkeypatch, protocol):
    from app.routes import generative_jobs as gj

    job, variant = authored(monkeypatch, "splitBlankPanel")
    variant["render_destination"] = "device"
    set_client_protocol(protocol)
    offered = gj._editor_capabilities(job, variant).get("split_blank_panel") is True
    assert offered is (protocol >= 51)
    clear_request_context()
    assert gj._editor_capabilities(job, variant).get("split_blank_panel") is not True


def recipe_from_fixture():
    from app.kria.recipes_v2 import EditRecipeV2

    return EditRecipeV2.model_validate(json.loads(FIXTURE.read_text()))


def test_the_swift_engine_fixture_is_a_valid_server_recipe():
    recipe = recipe_from_fixture()
    blanks = recipe.split_screen_sections[0].blanks
    assert [(b.panel_index, b.start, b.end, b.color) for b in blanks] == [(1, 1.0, 3.0, "#FF0000")]
    assert "splitBlankPanel" in recipe.required_capabilities


def test_the_capability_follows_the_blanks_only():
    recipe = recipe_from_fixture()
    wire = recipe.model_dump(mode="json")
    wire["split_screen_sections"][0].pop("blanks")
    wire["required_capabilities"] = [
        c for c in wire["required_capabilities"] if c != "splitBlankPanel"
    ]
    from app.kria.recipes_v2 import EditRecipeV2

    assert "splitBlankPanel" not in EditRecipeV2.model_validate(wire).required_capabilities


def test_a_recipe_blank_must_sit_inside_its_section_and_panel():
    recipe = recipe_from_fixture()
    from app.kria.recipes_v2 import EditRecipeV2

    for change in (
        {"panel_index": 2},
        {"start": 0, "end": 9},
        {"color": "#12"},
    ):
        wire = recipe.model_dump(mode="json")
        wire["split_screen_sections"][0]["blanks"][0].update(change)
        with pytest.raises(ValidationError):
            EditRecipeV2.model_validate(wire)


def test_an_unrelated_chat_edit_keeps_a_blank_saved_by_hand(monkeypatch):
    """An edit over a timeline that holds a blank the creator placed must not drop it."""
    from copy import deepcopy

    from app.agents.edit_copilot import _ParseState, parse_editor_operation
    from app.services import kria_editor_ops as editor
    from app.services.client_protocol import bind_sending_build, unbind_sending_build
    from tests.services.test_kria_editor_ops import _job, _variant

    variant = _variant()
    variant["render_destination"] = "device"
    variant["editor_timeline_mode"] = "authored"
    variant["user_timeline"] = deepcopy(variant.pop("ai_timeline"))
    monkeypatch.setattr(
        editor, "_editor_capabilities", lambda *args: {"timeline": True, "clips": {}}
    )
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    verified(monkeypatch, "splitScreen", "splitBlankPanel")
    token = bind_sending_build(51)
    try:
        job = _job(variant)
        snapshot = editor.build_editor_snapshot(job, variant)
        snapshot["editor_ops_version"] = 2
        parsed = parse_editor_operation(
            {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 3},
            snapshot,
            _ParseState(0.99),
        )
        draft = editor.compile_editor_ops(job, variant, [parsed])
        payload = draft.payload.model_dump(mode="json", exclude_none=True)
        section_id = payload["split_screen_sections"][0]["id"]
        variant = editor.project_editor_draft(variant, payload, job)
        slots = variant["user_timeline"]["slots"]
        stored = variant["user_timeline"]["split_screen_sections"][0]
        stored["duration_s"] += 1
        slots.append(
            {
                "slot_id": "blank-1",
                "media_kind": "blank",
                "color": "#112233",
                "in_s": 0,
                "duration_s": 1,
                "removed": False,
                "transition_after": "cut",
                "split_section_id": section_id,
                "split_panel_index": 0,
                "split_offset_s": stored["duration_s"] - 1,
            }
        )
        job = _job(variant)
        snapshot = editor.build_editor_snapshot(job, variant)
        snapshot["editor_ops_version"] = 2
        parsed = parse_editor_operation(
            {"op": "update_split_screen", "section_id": section_id, "gutter": 0.02},
            snapshot,
            _ParseState(0.99),
        )
        assert parsed is not None
        draft = editor.compile_editor_ops(job, variant, [parsed])
        kept = draft.payload.model_dump(mode="json", exclude_none=True)["timeline_slots"]
        blanks = [row for row in kept if row.get("media_kind") == "blank"]
        assert [(row["color"], row.get("clip_index")) for row in blanks] == [("#112233", None)]
    finally:
        unbind_sending_build(token)


def test_deleting_a_saved_blank_removes_it_and_keeps_the_section(monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status

    job, _ = authored(monkeypatch, "splitBlankPanel")
    gj.prepare_editor_commit(job, "original_text", payload())
    saved = job.assembly_plan["variants"][0]["user_timeline"]["slots"]
    stored = next(row for row in saved if row.get("media_kind") == "blank")

    clip = next(row for row in saved if row.get("media_kind") != "blank")

    def wire(row, **changes):
        known = gj.TimelineSlotEdit.model_fields
        return gj.TimelineSlotEdit.model_validate(
            {k: v for k, v in row.items() if k in known and v is not None} | changes
        )

    gone = wire(stored, removed=True)
    kept = wire(clip)
    gj.prepare_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            base_generation=job.assembly_plan["variants"][0]["render_generation_id"],
            editor_state_version=2,
            timeline_slots=[kept, gone],
            split_screen_sections=[section()],
            deletions=[gj.EditorDeletionRequest(kind="clip", id=stored["slot_id"])],
        ),
    )
    after = job.assembly_plan["variants"][0]["user_timeline"]["slots"]
    assert not [row for row in after if row.get("media_kind") == "blank" and not row.get("removed")]
    recipe = device_status(job, "original_text").request.recipe
    assert recipe.split_screen_sections[0].blanks == []
    assert "splitBlankPanel" not in recipe.required_capabilities
