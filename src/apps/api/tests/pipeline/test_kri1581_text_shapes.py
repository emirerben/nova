"""KRI-1581: the Kyoto edit drawn in designs A1, A2, A3 and B1, end to end on the fixture.

Rows go through the real resolver under the job's own text policy, then the real Skia burn and
the real phone compile. The reference is the teardown mock-up itself: the glyph boxes of the
generator's own row layers, checked in as numbers (`expected_rows.json` target
`mockup_ink_boxes`).

Ways this can fail, written before the code:
1. The design is ignored: the writer's font, colour and spot survive, or her saved
   preferences (title: top, all_text: bottom_left) move the hook off its centred spot.
2. A pair splits: heading and detail are placed one by one, so they overlap, drift apart or
   land on different corners; or a lone guide line is never laid out at all.
3. The block sits off the design's bottom edge, or a design size can never shrink and the
   stack is left unplaced.
4. The scrim is missing, drawn twice (two layers of a pair at 0.6 stack to 0.84), drawn on a
   design without one, or sent to a build that cannot decode it.
5. The shadow keeps the renderer default (sigma 12, drop 6) instead of the design's 14 and 3.
6. A creator's edit is overwritten by the design.
7. A job approved from an older build is stamped for designs its editor cannot redraw.
8. The cloud receipt reads the scrim, which reaches the bottom edge, as clipped text.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest
import skia

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.informative_text import POLICY_KEY, SHAPES_KEY, stamp_policy
from app.pipeline.phone_text_style import resolve_phone_text_style
from app.pipeline.portable_text_layout import compile_text_overlay
from app.pipeline.text_designs import TEXT_DESIGNS
from app.services.client_protocol import INFORMATIVE_TEXT_DESIGNS_PROTOCOL, set_client_protocol

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1585_kyoto"
_CANVAS = (1080, 1920)


def _load(name: str) -> dict:
    return json.loads((_FIXTURE / name).read_text())


def _design_rows(design: str) -> list[dict]:
    return [
        TextElement.model_validate(row).model_dump(mode="json")
        for row in _load("designed_rows.json")["designs"][design]
    ]


def _resolve(rows: list[dict], *, shapes: bool = True) -> list[dict]:
    policy = _load("plan.json")["variant"]["phone_text_style"]
    return resolve_phone_text_style(
        rows,
        preferences=policy["preferences"],
        requested=policy["requested"],
        selected_fonts=policy["selected_fonts"],
        informative=True,
        shapes=shapes,
    )


def _groups(rows: list[dict]) -> list[list[dict]]:
    """The hook, then each fact pair or guide line, in the mock-up's row order."""
    groups: list[list[dict]] = []
    for row in rows:
        stack = (row["source_params"].get("text_stack_v1") or {}).get("stack_id")
        if (
            groups
            and stack
            and groups[-1][0]["source_params"].get("text_stack_v1", {}).get("stack_id") == stack
        ):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _burn(rows: list[dict]) -> np.ndarray:
    """One full-opacity frame of ``rows`` through the cloud Skia burn, RGBA."""
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=60,
        independent_box_alignment=True,
    )
    surface = skia.Surfaces.MakeRasterN32Premul(*_CANVAS)
    for overlay in overlays:
        duration = overlay["end_s"] - overlay["start_s"]
        cloud._draw_overlay_on_canvas(surface.getCanvas(), overlay, duration / 2, duration)
    return surface.makeImageSnapshot().toarray(
        colorType=skia.ColorType.kRGBA_8888_ColorType,
        alphaType=skia.AlphaType.kUnpremul_AlphaType,
    )


def _ink_box(frame: np.ndarray) -> list[int]:
    ys, xs = np.nonzero(frame[..., 3] > 250)
    return [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]


@pytest.mark.parametrize("design", ["A1", "A2", "A3", "B1"])
def test_kyoto_designs_draw_where_the_mockups_drew_them(design):
    rows = _resolve(_design_rows(design))
    mockup = _load("expected_rows.json")["target"]["mockup_ink_boxes"][design]
    groups = _groups(rows)
    assert len(groups) == len(mockup)
    for index, (group, expected) in enumerate(zip(groups, mockup, strict=True)):
        frame = _burn(group)
        drawn = _ink_box(frame)
        for edge in range(4):
            assert abs(drawn[edge] - expected[edge]) <= 4, (design, index, drawn, expected)
        # The scrim reaches the canvas bottom at the design's peak, exactly once per pair.
        bottom_alpha = int(frame[_CANVAS[1] - 1, _CANVAS[0] // 2, 3])
        scrim = TEXT_DESIGNS[design].scrim
        if index == 0 or scrim is None:
            assert bottom_alpha == 0, (design, index)
        else:
            assert abs(bottom_alpha - round(255 * scrim["opacity"])) <= 2, (design, index)


def test_hook_keeps_its_spot_over_her_saved_corner():
    rows = _resolve(_design_rows("A2"))
    hook = rows[0]
    assert (hook["x_frac"], hook["y_frac"], hook["alignment"]) == (0.5, 0.42, "center")
    # Her saved all_text corner still places the facts' side: the design's bottom-left.
    pair = [row for row in rows if row["id"].startswith("kri1585-fact-0")]
    assert {row["alignment"] for row in pair} == {"left"}
    heading, detail = pair
    assert heading["y_frac"] < detail["y_frac"]


def _with_gate(monkeypatch, *, verified: bool = True):
    monkeypatch.setattr(settings, "informative_text_designs_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, *(["textDesigns"] if verified else [])],
    )


def _phone_layers(rows: list[dict]):
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=60,
        independent_box_alignment=True,
    )
    from app.pipeline.canvas import PORTRAIT

    return [
        compile_text_overlay(overlay, layer_id=f"text-{i}", canvas=PORTRAIT)[0]
        for i, overlay in enumerate(overlays)
    ]


def test_phone_layers_carry_the_design_shadow_and_one_scrim_per_pair(monkeypatch):
    _with_gate(monkeypatch)
    set_client_protocol(INFORMATIVE_TEXT_DESIGNS_PROTOCOL)
    try:
        layers = _phone_layers(_resolve(_design_rows("A2")))
    finally:
        set_client_protocol(None)
    for layer in layers:
        (blur,) = {
            (b.sigma, b.dy, round(b.color.alpha, 2)) for r in layer.runs for b in r.blur_layers
        }
        assert blur == (14.0, 3.0, 0.55), layer.id
    # Every designed layer blends in sRGB values on the device, as the cloud burn does.
    assert all(layer.srgb_blend is True for layer in layers)
    scrims = [layer.scrim for layer in layers if layer.scrim is not None]
    assert len(scrims) == 5  # one per fact pair, none on the hook
    scrim = scrims[0]
    assert scrim.top == pytest.approx(0.48 * 1920)
    assert scrim.top + scrim.height == pytest.approx(1920)
    assert [(s.position, round(s.color.alpha, 3)) for s in scrim.stops] == [
        (0.0, 0.0),
        (0.6, 0.6),
        (1.0, 0.6),
    ]
    wire = layers[1].model_dump(mode="json")
    assert "scrim" in wire and "scrim" not in layers[0].model_dump(mode="json")


@pytest.mark.parametrize(
    "protocol, verified", [(INFORMATIVE_TEXT_DESIGNS_PROTOCOL - 1, True), (None, False)]
)
def test_a_device_that_cannot_draw_the_scrim_gets_the_text_without_it(
    monkeypatch, protocol, verified
):
    _with_gate(monkeypatch, verified=verified)
    set_client_protocol(protocol)
    try:
        layers = _phone_layers(_resolve(_design_rows("A2")))
    finally:
        set_client_protocol(None)
    assert all(layer.scrim is None and not layer.srgb_blend for layer in layers)
    assert all(not {"scrim", "srgb_blend"} & set(layer.model_dump(mode="json")) for layer in layers)
    assert {b.sigma for layer in layers for r in layer.runs for b in r.blur_layers} == {14.0}


@pytest.mark.parametrize(
    "flag, verified, protocol, stamped",
    [
        (True, True, INFORMATIVE_TEXT_DESIGNS_PROTOCOL, {POLICY_KEY, SHAPES_KEY}),
        (True, True, INFORMATIVE_TEXT_DESIGNS_PROTOCOL - 1, {POLICY_KEY}),
        (True, True, None, {POLICY_KEY}),
        (True, False, INFORMATIVE_TEXT_DESIGNS_PROTOCOL, {POLICY_KEY}),
        (False, True, INFORMATIVE_TEXT_DESIGNS_PROTOCOL, set()),
    ],
)
def test_only_a_build_that_draws_designs_is_stamped_for_them(
    monkeypatch, flag, verified, protocol, stamped
):
    monkeypatch.setattr(settings, "informative_text_designs_enabled", flag)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["textDesigns"] if verified else []
    )
    policy = stamp_policy({"version": 1}, client_protocol=protocol)
    assert set(policy) - {"version"} == stamped


def test_without_the_design_stamp_the_rows_ignore_their_design():
    rows = _design_rows("A2")
    plain = [
        {
            **row,
            "source_params": {
                key: value for key, value in row["source_params"].items() if key != "text_design"
            },
        }
        for row in rows
    ]
    assert _resolve(rows, shapes=False) == [
        {**styled, "source_params": {**styled["source_params"], "text_design": "A2"}}
        for styled in _resolve(plain, shapes=False)
    ]


def test_her_edits_survive_the_design():
    rows = _design_rows("A2")
    heading = next(row for row in rows if row["id"] == "kri1585-fact-1-heading")
    heading.update(color="#FF5A5A", font_family="Montserrat", shadow_blur=4.0)
    heading["source_params"]["creator_style_fields"] = ["color", "font_family", "shadow_blur"]
    styled = {row["id"]: row for row in _resolve(rows)}
    kept = styled["kri1585-fact-1-heading"]
    assert (kept["color"], kept["font_family"], kept["shadow_blur"]) == (
        "#FF5A5A",
        "Montserrat",
        4.0,
    )
    # The rest of her pair, and every other pair, is still the design.
    assert styled["kri1585-fact-1-detail"]["font_family"] == "Inter Medium"
    assert styled["kri1585-fact-2-heading"]["color"] == "#FFE7A0"


def test_cloud_receipt_reads_the_text_not_the_scrim(tmp_path):
    (row,) = [r for r in _resolve(_design_rows("A2")) if r["id"] == "kri1585-fact-0-heading"]
    (overlay,) = build_overlays_from_text_elements(
        [TextElement.model_validate(row)], video_duration_s=60, independent_box_alignment=True
    )
    duration = overlay["end_s"] - overlay["start_s"]
    frame = cloud._draw_frame(overlay, duration / 2, duration)
    path = tmp_path / "frame-0000.png"
    frame.save(str(path), skia.kPNG)
    sequence = {"element_id": "heading", "pattern": str(tmp_path / "frame-%04d.png"), "n_frames": 1}

    def receipt(seq):
        return cloud._sequence_alpha_evidence(
            [seq], required_element_ids=["heading"], canvas=cloud.PORTRAIT
        )[0]

    with_scrim = receipt({**sequence, "scrim": overlay["scrim"]})
    assert with_scrim["visible"] is True
    assert with_scrim["pixel_bounds"][3] < 0.72 * 1920
    # Negative control: read raw, the scrim runs to the bottom edge and fails the safe margin.
    assert receipt(sequence)["visible"] is False


# Computed on origin/main 5c02b92b (before KRI-1581) from a detached worktree; the branch must
# reproduce it while the flag is off.
_FLAG_OFF_SHA256 = "4f567b3766aec35b6a155e92ecbd2956286b2ea24b551dd17206ff89c2dd1e4f"


def test_flag_off_rows_layers_and_pixels_are_byte_identical_to_main():
    """Production's Kyoto rows, as shipped and after her editor save: their dumps, their
    compiled phone layers and their Skia pixels."""
    import hashlib

    from app.pipeline.canvas import PORTRAIT

    assert settings.informative_text_designs_enabled is False
    digest = hashlib.sha256()
    for name in ("text_rows.json", "text_rows_after_editor_save.json"):
        elements = [TextElement.model_validate(row) for row in _load(name)["text_elements"]]
        overlays = build_overlays_from_text_elements(
            elements, video_duration_s=61.6, independent_box_alignment=True
        )
        for index, overlay in enumerate(overlays):
            layer, _font = compile_text_overlay(overlay, layer_id=f"text-{index}", canvas=PORTRAIT)
            digest.update(json.dumps(layer.model_dump(mode="json"), sort_keys=True).encode())
            duration = overlay["end_s"] - overlay["start_s"]
            digest.update(cloud._draw_frame(overlay, duration / 2, duration).tobytes())
        for element in elements:
            digest.update(json.dumps(element.model_dump(mode="json"), sort_keys=True).encode())
    assert digest.hexdigest() == _FLAG_OFF_SHA256


def _set_fields(rows: list[dict]) -> list[dict]:
    """Rows with unset (None) fields dropped: a cleared field and a never-set one are the same."""
    return [{key: value for key, value in row.items() if value is not None} for row in rows]


@pytest.mark.parametrize("start, switch", [("A1", "A2"), ("A2", "A3"), ("A3", "A1")])
def test_switching_design_draws_what_a_fresh_pick_draws_and_keeps_her_edit(start, switch):
    first = _resolve(_design_rows(start))
    for row in first:
        row["source_params"]["text_design"] = switch
    assert _set_fields(_resolve(first)) == _set_fields(_resolve(_design_rows(switch)))

    edited = _resolve(_design_rows(start))
    detail = next(row for row in edited if row["id"] == "kri1585-fact-2-detail")
    detail["color"] = "#9AD0FF"
    detail["source_params"]["creator_style_fields"] = ["color"]
    for row in edited:
        row["source_params"]["text_design"] = switch
    switched = {row["id"]: row for row in _resolve(edited)}
    assert switched["kri1585-fact-2-detail"]["color"] == "#9AD0FF"
    assert (
        switched["kri1585-fact-2-detail"]["font_family"] == TEXT_DESIGNS[switch].detail.font_family
    )


def test_a_save_keeps_the_design_and_records_only_what_she_changed(monkeypatch):
    """Through `prepare_editor_commit` (the iPhone Save and a chat draft's commit) on a guided
    story whose plan carries the A2 hook and pairs: a wording edit to one detail keeps every
    row's shadow, drop and scrim, and makes no style field hers."""
    import app.routes.generative_jobs as gj
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD
    from tests.routes.test_editor_commit import _arm, _narrated_guided_job

    _arm(monkeypatch)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(settings, "informative_text_designs_enabled", True)
    monkeypatch.setattr(gj, "_TEXT_ELEMENTS_ENABLED", True)
    designed = [row for row in _resolve(_design_rows("A2")) if row["end_s"] <= 25]
    job = _narrated_guided_job()
    job.assembly_plan["guided_story_execution_plan"]["text_elements"].extend(
        copy.deepcopy(designed)
    )
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"].extend(copy.deepcopy(designed))
    variant[PHONE_TEXT_STYLE_FIELD] = {
        "version": 1,
        "preferences": {},
        "requested": {},
        POLICY_KEY: True,
        SHAPES_KEY: True,
    }
    revision = gj._guided_v2_revision(job, variant)
    elements = copy.deepcopy(revision["text_elements"])
    edited = next(row for row in elements if row["id"] == "kri1585-fact-1-detail")
    edited["text"] = "Hozukyo boyunca tarihi hat"
    gj.prepare_editor_commit(
        job,
        variant["variant_id"],
        gj.EditorCommitRequest(
            base_generation=revision["base_generation"],
            guided_revision_number=1,
            text_elements=elements,
        ),
    )
    saved_variant = job.assembly_plan["variants"][0]
    before = {row["id"]: row for row in designed}
    for rows in (
        saved_variant["guided_edit_revision"]["text_elements"],
        saved_variant["text_elements"],
    ):
        saved = {row["id"]: row for row in rows if row["id"] in before}
        assert set(saved) == set(before)
        for row_id, row in saved.items():
            for field in ("shadow_blur", "shadow_offset_y", "scrim", "font_family", "color"):
                assert row.get(field) == before[row_id].get(field), (row_id, field)
            assert not (row.get("source_params") or {}).get("creator_style_fields"), row_id
        assert saved["kri1585-fact-1-detail"]["text"] == "Hozukyo boyunca tarihi hat"


@pytest.mark.parametrize("design", ["A2", "B1"])
def test_rows_tagged_by_the_research_writer_draw_the_same_design(design):
    """KRI-1580's writer tags each row ``research.design`` (``id``, ``level``, ``pair``) instead
    of ``text_design`` and ``text_stack_v1``; both shapes must draw the same video."""
    tagged = []
    for row in _design_rows(design):
        source = dict(row["source_params"])
        stack = source.pop("text_stack_v1", None)
        source.pop("text_design")
        if source.get("phone_text_role") == "hook":
            level, pair = "hook", None
        elif stack is None:
            level, pair = "line", None
        else:
            level = "heading" if stack["heading_level"] == 2 else "detail"
            pair = int(stack["stack_id"].split("-")[1])
        source.pop("phone_text_role", None)
        tag = {"id": design, "level": level, **({"pair": pair} if pair is not None else {})}
        source["research"] = {**source.get("research", {}), "design": tag}
        tagged.append({**row, "source_params": source})

    def drawn(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in row.items() if k != "source_params"} for row in rows]

    assert drawn(_resolve(tagged)) == drawn(_resolve(_design_rows(design)))
