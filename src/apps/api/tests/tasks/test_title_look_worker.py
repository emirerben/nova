"""Qendresa item 20: her hook title look on a phone Talking edit (TITLE_LOOK_ENABLED).

Her Q2 prompt asks for the title "5 PROJECTS / the AI builder community / can't stop
talking about!!!!" with the GitHub logo above it. Measured on her own openings (V5, V1,
V7: kit `_src/measure/ocr`): every " / " is a line break, each line has its own face and
colour (heavy bold sans, a serif, handwriting), the block sits centred under her face or
top-left, and the logo sits right above it. Before this change the worker drew ONE row
with the slashes drawn literally, in one font.

These run the REAL `_run_phone_subtitled_job` and the real phone recipe compile; only the
transcript, the face sampler and (for the logo) the Visuals pool rows are stubbed.

Ways it could fail (written before the code; full list in the item 20 hand-over):
F1 " / " inside ordinary text split wrongly; F2 lines reordered/dropped/recased;
F3 lines overlap or leave the frame; F4 the block covers her face; F7 the styling pass
re-fonts every line; F8 a whole-title colour paints every line when she gave per-line
looks; F9 the confirmed-text check misses the split title; F13 flag off changes bytes;
F14 logo on her face / in the wrong time base / not reported; F16 hold too short to read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.pipeline.canvas import PORTRAIT
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _basic_beat_receipt,
    _beat_grounding_mock,
    _enable_beats,
    _grounding_mock,
    _make_fake_bind,
    _setup_subtitled,
    _words,
)
from tests.tasks.test_phone_subtitled_title_worker import _faces

HER_TITLE = "5 PROJECTS / the AI builder community / can't stop talking about!!!!"
HER_LINES = ["5 PROJECTS", "the AI builder community", "can't stop talking about!!!!"]
# Her V5 face over the title window, on the 1080x1920 canvas (kit OCR, frames 0-6 s).
HER_V5_FACE = NormalizedBox(0.44, 0.37, 0.93, 0.61)
_MAX_FACE_COVERAGE = 0.05


def _look(monkeypatch, *, on=True):
    # Before the switch existed, every run was "off": recording the FAIL needs no flag.
    if "title_look_enabled" in type(gb.settings).model_fields:
        monkeypatch.setattr(gb.settings, "title_look_enabled", on)


def _titled(monkeypatch, *, title=HER_TITLE, hold=None, request="", **setup):
    job, snapshot, session, binding = _setup_subtitled(monkeypatch, **setup)
    job.all_candidates["creator_strategy"] = {
        "opening_title": title,
        **({"opening_title_duration_s": hold} if hold is not None else {}),
    }
    if request:
        job.all_candidates["creator_request"] = request
    return job, snapshot, session, binding


def _title_rows(job) -> list[dict]:
    rows = job.assembly_plan["variants"][0]["text_elements"]
    return [
        row
        for row in rows
        if (row.get("source_params") or {}).get("source") == title_mod.TALKING_TITLE_SOURCE
    ]


def _box(row) -> NormalizedBox:
    return title_mod._measure_title_box(row, canvas=PORTRAIT)


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


# --- line breaks and per-line looks -------------------------------------------------------


def test_her_three_lines_come_out_as_three_rows_in_her_order(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    rows = _title_rows(job)
    assert [row["text"] for row in rows] == HER_LINES
    assert rows[0]["id"] == title_mod.TALKING_TITLE_ELEMENT_ID
    assert not any(" / " in row["text"] for row in rows)
    # All lines enter and leave together, from the first frame.
    assert {(row["start_s"], row["end_s"]) for row in rows} == {(0.0, rows[0]["end_s"])}
    # Each line is drawn: the recipe carries one text layer per line from t=0.
    layer_texts = [
        " ".join(" ".join(run.text for run in layer.runs).split())
        for layer in _recipe(job).text_layers
        if layer.start == 0.0 and not layer.id.startswith("caption-")
    ]
    for line in HER_LINES:
        assert line in layer_texts


def test_each_line_gets_its_own_face_and_colour_in_her_proportions(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    bold, serif, hand = _title_rows(job)
    assert len({bold["font_family"], serif["font_family"], hand["font_family"]}) == 3
    # Her V5: heavy grotesque caps line biggest, then serif and handwriting.
    assert bold["size_px"] > serif["size_px"]
    assert bold["size_px"] > hand["size_px"]
    assert len({bold["color"], serif["color"], hand["color"]}) >= 2
    # Her casing is never changed.
    assert [r.get("text_case") in (None, "none", "as_typed") for r in (bold, serif, hand)] == [
        True,
        True,
        True,
    ]


def test_her_own_per_line_styles_win_over_a_whole_title_colour(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    request = (
        f'Start with the title "{HER_TITLE}" in three styles: handwritten, bold caps, serif yellow.'
    )
    job, *_ = _titled(monkeypatch, request=request)

    gb._run_generative_job(str(job.id))

    first, second, third = _title_rows(job)
    categories = [_category(row["font_family"]) for row in (first, second, third)]
    assert categories == ["handwriting", "sans", "serif"]
    # "yellow" belongs to the third line only, not the whole title.
    assert third["color"] != first["color"]
    assert third["color"] != second["color"]


def _category(family: str) -> str:
    registry = json.loads(
        (Path(__file__).resolve().parents[2] / "assets/fonts/font-registry.json").read_text()
    )["fonts"]
    return registry[family]["category"]


# --- placement ----------------------------------------------------------------------------


def test_lines_stack_in_order_without_overlapping_and_inside_the_frame(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    boxes = [_box(row) for row in _title_rows(job)]
    for upper, lower in zip(boxes, boxes[1:], strict=False):
        assert upper.bottom <= lower.top + 1e-3, (upper, lower)
    for box in boxes:
        assert box.left >= 0.03 and box.right <= 0.97
        assert box.top >= 0.06 and box.bottom <= 0.97


@pytest.mark.parametrize(
    "face",
    [
        HER_V5_FACE,
        # A close selfie: the face band fills most of the upper frame.
        NormalizedBox(0.18, 0.06, 0.92, 0.66),
        # Face low in frame (she sits low): the title must go above it.
        NormalizedBox(0.25, 0.45, 0.80, 0.80),
    ],
)
def test_the_title_block_never_covers_her_face(monkeypatch, face):
    _look(monkeypatch)
    _faces(monkeypatch, [ProtectedRegion(0.0, 3.0, face, "face")])
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    rows = _title_rows(job)
    assert [row["text"] for row in rows] == HER_LINES
    for row in rows:
        assert _box(row).coverage_by(face) <= _MAX_FACE_COVERAGE, row["text"]


def test_her_top_left_ask_puts_the_block_top_left(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch, request=f'Title "{HER_TITLE}" in the top left corner.')

    gb._run_generative_job(str(job.id))

    rows = _title_rows(job)
    assert {row["alignment"] for row in rows} == {"left"}
    boxes = [_box(row) for row in rows]
    assert max(box.left for box in boxes) <= 0.12
    assert min(box.top for box in boxes) <= 0.25


def test_without_an_asked_hold_the_lines_stay_long_enough_to_read(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(
        monkeypatch, transcript_words=_words(("Hello", 0.2, 0.5), ("there.", 0.5, 1.0))
    )

    gb._run_generative_job(str(job.id))

    # Today's single-line rule ends a second after the first word (1.5 s); three lines
    # of her title hold the full 3 s cap instead.
    assert {row["end_s"] for row in _title_rows(job)} == {3.0}


# --- the styling pass (prod: PHONE_TEXT_STYLING on) ---------------------------------------


def test_the_styling_pass_keeps_each_lines_own_face(monkeypatch):
    import app.services.phone_text_styling as styling

    _look(monkeypatch)
    _faces(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(styling, "select_fonts", lambda rows, policy, **_k: {"title": "Unbounded"})
    job, snapshot, *_ = _titled(monkeypatch)
    job.assembly_plan[styling.PHONE_TEXT_STYLE_FIELD] = {
        "version": 1,
        "preferences": {"all_text": {}},
        "requested": {},
    }

    gb._run_generative_job(str(job.id))

    rows = _title_rows(job)
    assert [row["text"] for row in rows] == HER_LINES
    assert len({row["font_family"] for row in rows}) == 3
    assert "Unbounded" not in {row["font_family"] for row in rows}


# --- the confirmed-text checks still find her title ---------------------------------------


def test_the_recipe_check_and_the_brief_receipt_still_find_her_title(monkeypatch):
    from app.kria.brief import BriefRequirement
    from app.kria.brief_checks import check_requirement, plan_facts_from_phone_variant

    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="opening", text=HER_TITLE),)
    )
    verify_phone_recipe(contract, _recipe(job))  # raises a decline when missing
    requirement = BriefRequirement(
        id="r1", kind="text", scope="title", literal=HER_TITLE, description="Opening title"
    )
    receipt = check_requirement(
        requirement, plan_facts_from_phone_variant(job.assembly_plan["variants"][0])
    )
    assert receipt.status == "met", receipt.reason


def test_the_recipe_check_still_refuses_a_title_missing_a_line(monkeypatch):
    """Negative control: dropping her third line must not pass as her title."""
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(
            TextRequirement(
                role="opening", text=HER_TITLE.replace("can't stop talking", "won't stop talking")
            ),
        )
    )
    with pytest.raises(Exception):  # noqa: B017 - the typed decline
        verify_phone_recipe(contract, _recipe(job))


# --- off switch ---------------------------------------------------------------------------


def test_flag_off_is_todays_single_row(monkeypatch):
    _look(monkeypatch, on=False)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    [row] = _title_rows(job)
    assert (row["id"], row["text"]) == ("opening-title", HER_TITLE)
    assert "title_look" not in json.dumps(variant)
    assert "title-line" not in _recipe(job).model_dump_json()


def test_a_single_line_title_is_unchanged_with_the_flag_on(monkeypatch):
    """Her Q1 title has no line breaks: the look leaves it exactly as today."""
    single = "lets build an agent that reads an APP before I trust it!!"
    _faces(monkeypatch)
    _look(monkeypatch, on=False)
    job_off, *_ = _titled(monkeypatch, title=single, hold=3.0)
    gb._run_generative_job(str(job_off.id))
    off_rows = _title_rows(job_off)

    _look(monkeypatch, on=True)
    job_on, *_ = _titled(monkeypatch, title=single, hold=3.0)
    gb._run_generative_job(str(job_on.id))

    assert _title_rows(job_on) == off_rows


# --- the logo right above the title -------------------------------------------------------

_LOGO = "asset-github-logo"


def _logo_pool(monkeypatch, *, aspect="1:1"):
    asset = {
        "id": "github-logo",
        "gcs_path": "users/u1/plan/item1/pool/github-logo.png",
        "gcs_generation": "7",
        "kind": "image",
        "source_filename": "github logo.png",
        "duration_s": None,
        "aspect": aspect,
        "user_context": "",
        "analysis": {},
        "capture": None,
    }
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "_load_ready_pool_assets", lambda *_a, **_k: [asset]
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "_load_ready_pool_assets", lambda *_a, **_k: [asset]
    )


def _logo_job(monkeypatch, *, face=None):
    _look(monkeypatch)
    _faces(monkeypatch, [ProtectedRegion(0.0, 3.0, face, "face")] if face else ())
    job, *_ = _titled(monkeypatch)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _grounding_mock([])
    )
    # Word beats would go through grounding; this job has none, so grounding must not
    # be what places the logo.
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "ground_phone_reaction_beats",
        _beat_grounding_mock([], [], _basic_beat_receipt()),
    )
    _logo_pool(monkeypatch)
    job.all_candidates["creator_strategy"]["reaction_beats"] = [
        {"beat_id": "logo", "trigger": "GitHub logo", "visual_id": _LOGO, "anchor": "title"}
    ]
    return job


def _logo_cards(job):
    recipe = _recipe(job)
    return [
        clip
        for track in recipe.tracks
        if track.id != "subtitled"
        for clip in track.clips
        if "github-logo" in (clip.source_asset_id or "")
    ]


def test_the_logo_sits_right_above_her_title_while_it_shows(monkeypatch):
    job = _logo_job(monkeypatch)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    rows = _title_rows(job)
    block_top = min(_box(row).top for row in rows)
    [placed] = [e for e in variant["phone_beat_receipt"]["placed"] if e["beat_id"] == "logo"]
    lanes = variant["phone_subtitled_lanes"]["overlays"]
    [card] = [c for c in lanes if c["media_id"] in (_LOGO, "github-logo")]
    # Shown with the title, from the first frame until the title leaves.
    assert card["start_s"] == pytest.approx(0.0, abs=0.05)
    assert card["end_s"] == pytest.approx(rows[0]["end_s"], abs=0.05)
    # Right above the block: its bottom edge sits on or above the block's top edge.
    half_height = card["scale"] * PORTRAIT.width / PORTRAIT.height / 2  # square logo
    assert card["y_frac"] + half_height <= block_top + 1e-3
    assert card["y_frac"] - half_height >= 0.06
    assert placed["at_s"] == pytest.approx(0.0, abs=0.05)
    assert _logo_cards(job)


def test_the_logo_never_lands_on_her_face(monkeypatch):
    job = _logo_job(monkeypatch, face=HER_V5_FACE)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    lanes = variant["phone_subtitled_lanes"]["overlays"]
    cards = [c for c in lanes if c["media_id"] in (_LOGO, "github-logo")]
    receipt = variant["phone_beat_receipt"]
    if not cards:
        # Dropped only with an honest reason, never silently.
        [missed] = [e for e in receipt["unplaced"] if e["beat_id"] == "logo"]
        assert missed["reason"]
        return
    [card] = cards
    half_w = card["scale"] / 2
    half_h = card["scale"] * PORTRAIT.width / PORTRAIT.height / 2
    logo_box = NormalizedBox(
        card["x_frac"] - half_w,
        card["y_frac"] - half_h,
        card["x_frac"] + half_w,
        card["y_frac"] + half_h,
    )
    assert logo_box.coverage_by(HER_V5_FACE) <= _MAX_FACE_COVERAGE


def test_a_title_logo_beat_with_the_flag_off_goes_to_grounding_as_before(monkeypatch):
    job = _logo_job(monkeypatch)
    _look(monkeypatch, on=False)

    gb._run_generative_job(str(job.id))

    grounding = phone_reaction_grounding_mod.ground_phone_reaction_beats
    grounding.assert_called_once()
    [beat] = grounding.call_args.kwargs["beats"]
    assert beat["beat_id"] == "logo"
