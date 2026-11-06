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
    # Dispatch pins what her words asked (KRI-470: the worker never re-reads her prose).
    from app.pipeline.phone_title_look import stamp_title_look

    stamp_title_look(job)
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


def test_a_font_kria_picked_on_its_own_never_flattens_her_lines(monkeypatch):
    """Item 21's gotcha: the strategy's font knob can be a provisional face, not her ask."""
    import app.services.phone_text_styling as styling

    _look(monkeypatch)
    _faces(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(styling, "select_fonts", lambda rows, policy, **_k: {})
    job, *_ = _titled(monkeypatch)
    job.all_candidates["creator_strategy"]["font_family"] = "Unbounded"
    styling.stamp_phone_text_style(job)
    job.assembly_plan[styling.PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2

    gb._run_generative_job(str(job.id))

    rows = _title_rows(job)
    assert len({row["font_family"] for row in rows}) == 3
    assert "Unbounded" not in {row["font_family"] for row in rows}


def test_a_font_she_names_for_the_title_is_used_on_every_line(monkeypatch):
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(
        monkeypatch, request=f'Start with the title "{HER_TITLE}". Use Pacifico for the title.'
    )

    gb._run_generative_job(str(job.id))

    assert {row["font_family"] for row in _title_rows(job)} == {"Pacifico"}


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


def _logo_box(job) -> tuple[NormalizedBox, float, float]:
    """The logo card the phone draws (recipe overlay track): its canvas box and window."""
    recipe = _recipe(job)
    [clip] = [
        clip
        for track in recipe.tracks
        if track.id != "subtitled"
        for clip in track.clips
        if "github-logo" in (clip.source_asset_id or "")
    ]
    place = clip.visual_placement
    width = place.width_fraction
    height = width * PORTRAIT.width / PORTRAIT.height  # her logo is square ("1:1")
    box = NormalizedBox(
        place.x_fraction - width / 2,
        place.y_fraction - height / 2,
        place.x_fraction + width / 2,
        place.y_fraction + height / 2,
    )
    return box, place.window_start, place.window_end


def test_the_logo_sits_right_above_her_title_while_it_shows(monkeypatch):
    job = _logo_job(monkeypatch)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    rows = _title_rows(job)
    block_top = min(_box(row).top for row in rows)
    [placed] = [e for e in variant["phone_beat_receipt"]["placed"] if e["beat_id"] == "logo"]
    assert placed["anchor"] == "title"
    logo, start, end = _logo_box(job)
    # Shown with the title, from the first frame until the title leaves.
    assert start == pytest.approx(0.0, abs=0.05)
    assert end == pytest.approx(rows[0]["end_s"], abs=0.05)
    # Right above the block, under the platform header.
    assert logo.bottom <= block_top + 1e-3
    assert logo.bottom >= block_top - 0.03
    assert logo.top >= 0.06 - 1e-3


def test_with_her_v5_face_both_fit_the_title_under_it_and_the_logo_beside_her_head(monkeypatch):
    job = _logo_job(monkeypatch, face=HER_V5_FACE)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    rows = _title_rows(job)
    assert [row["text"] for row in rows] == HER_LINES
    for row in rows:
        assert _box(row).coverage_by(HER_V5_FACE) <= _MAX_FACE_COVERAGE, row["text"]
    assert [e["beat_id"] for e in variant["phone_beat_receipt"]["placed"]] == ["logo"]
    logo, _start, _end = _logo_box(job)
    assert logo.coverage_by(HER_V5_FACE) <= _MAX_FACE_COVERAGE
    assert logo.bottom <= min(_box(row).top for row in rows) + 1e-3


def test_with_no_room_off_her_face_the_logo_is_reported_not_hidden(monkeypatch):
    face = NormalizedBox(0.05, 0.08, 0.95, 0.72)  # a face filling the frame
    job = _logo_job(monkeypatch, face=face)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    for row in _title_rows(job):
        assert _box(row).coverage_by(face) <= _MAX_FACE_COVERAGE, row["text"]
    [missed] = [e for e in variant["phone_beat_receipt"]["unplaced"] if e["beat_id"] == "logo"]
    assert (missed["anchor"], missed["reason"]) == ("title", "no_room_above_title")


def test_a_title_logo_beat_with_the_flag_off_goes_to_grounding_as_before(monkeypatch):
    job = _logo_job(monkeypatch)
    _look(monkeypatch, on=False)

    gb._run_generative_job(str(job.id))

    grounding = phone_reaction_grounding_mod.ground_phone_reaction_beats
    grounding.assert_called_once()
    [beat] = grounding.call_args.kwargs["beats"]
    assert beat["beat_id"] == "logo"


# --- fresh-review findings (written before the fixes) ------------------------------------


def test_the_stacked_title_is_placed_clear_of_her_captions(monkeypatch):
    import app.pipeline.phone_title_look as look_mod

    seen: list = []
    real = look_mod.place_title_look

    def spy(rows, **kwargs):
        placed, receipt = real(rows, **kwargs)
        seen.append((list(kwargs.get("avoid") or ()), placed, receipt))
        return placed, receipt

    monkeypatch.setattr(look_mod, "place_title_look", spy)
    _look(monkeypatch)
    _faces(monkeypatch, [ProtectedRegion(0.0, 3.0, NormalizedBox(0.30, 0.10, 0.70, 0.40), "face")])
    job, *_ = _titled(
        monkeypatch,
        transcript_words=_words(
            ("Five", 0.1, 0.4), ("projects", 0.4, 0.9), ("the", 0.9, 1.0), ("AI", 1.0, 1.2)
        ),
    )

    gb._run_generative_job(str(job.id))

    [(avoid, placed, receipt)] = seen
    assert avoid, "the captions on screen during the title were not passed"
    assert receipt["text_overlap"] is False
    for row in placed:
        assert all(_box(row).coverage_by(caption) <= _MAX_FACE_COVERAGE for caption in avoid)


def test_a_logo_whose_card_fails_to_bind_is_reported_not_placed(monkeypatch):
    job = _logo_job(monkeypatch)

    def broken_bind(*_a, **_k):
        raise RuntimeError("visual bytes unavailable")

    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", broken_bind)

    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    assert [e for e in receipt["placed"] if e["beat_id"] == "logo"] == []
    [missed] = [e for e in receipt["unplaced"] if e["beat_id"] == "logo"]
    assert missed["anchor"] == "title"


def test_a_logo_that_cannot_be_placed_never_fails_the_render(monkeypatch):
    import app.pipeline.phone_title_look as look_mod

    job = _logo_job(monkeypatch)

    def boom(*_a, **_k):
        raise ValueError("bad geometry")

    monkeypatch.setattr(look_mod, "place_title_logo", boom)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    [missed] = [e for e in receipt["unplaced"] if e["beat_id"] == "logo"]
    assert (missed["anchor"], missed["reason"]) == ("title", "error")


def test_a_one_line_title_keeps_its_spot_under_its_logo_through_the_styling_pass(monkeypatch):
    import app.services.phone_text_styling as styling

    single = "lets build an agent that reads an APP before I trust it!!"
    job = _logo_job(monkeypatch)
    job.all_candidates["creator_strategy"]["opening_title"] = single
    monkeypatch.setattr(gb.settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(styling, "select_fonts", lambda rows, policy, **_k: {"title": "Unbounded"})
    job.assembly_plan[styling.PHONE_TEXT_STYLE_FIELD] = {
        "version": 1,
        "preferences": {"all_text": {}},
        "requested": {},
    }

    gb._run_generative_job(str(job.id))

    [row] = _title_rows(job)
    assert row["text"] == single
    logo, _start, _end = _logo_box(job)
    assert logo.bottom <= _box(row).top + 1e-3


def test_the_worker_never_re_reads_her_words_only_what_dispatch_pinned(monkeypatch):
    """KRI-470 plan authority: a request edited after dispatch (or a job dispatched before the
    switch) changes nothing in the render; the look comes from the pinned ask alone."""
    _look(monkeypatch)
    _faces(monkeypatch)
    job, *_ = _titled(monkeypatch)  # pinned with no request: the default look
    job.all_candidates["creator_request"] = f'Title "{HER_TITLE}" in the top left corner.'

    gb._run_generative_job(str(job.id))

    rows = _title_rows(job)
    assert {row["alignment"] for row in rows} == {"center"}
    assert [row["font_family"] for row in rows] == [
        "Bricolage Grotesque",
        "Instrument Serif",
        "Patrick Hand",
    ]


# --- review (Emir, head 72674529a): the logo keeps off her captions -----------------------
# Today the worker places the logo against her face only, so with her captions up in the
# title band (KRI-827 moves them off a marked object) the logo is drawn on them.

TOP_CAPTIONS = NormalizedBox(0.05, 0.10, 0.95, 0.22)


def _captions_at(monkeypatch, *boxes):
    """Her caption lines on screen while the title shows."""
    monkeypatch.setattr(
        gb, "_title_look_caption_avoid", lambda _cues, **_kw: lambda _window: list(boxes)
    )


def test_review_the_logo_keeps_off_her_captions(monkeypatch):
    job = _logo_job(monkeypatch)
    _captions_at(monkeypatch, TOP_CAPTIONS)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert [e["beat_id"] for e in variant["phone_beat_receipt"]["placed"]] == ["logo"]
    logo, _start, _end = _logo_box(job)
    assert logo.coverage_by(TOP_CAPTIONS) <= _MAX_FACE_COVERAGE
    rows = _title_rows(job)
    assert logo.bottom <= min(_box(row).top for row in rows) + 1e-3
    for row in rows:
        assert _box(row).coverage_by(TOP_CAPTIONS) <= _MAX_FACE_COVERAGE, row["text"]


def test_review_when_only_her_captions_leave_no_room_the_receipt_says_so(monkeypatch):
    low_face = NormalizedBox(0.25, 0.45, 0.80, 0.80)
    job = _logo_job(monkeypatch, face=low_face)
    _captions_at(monkeypatch, TOP_CAPTIONS)

    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    assert [e for e in receipt["placed"] if e["beat_id"] == "logo"] == []
    [missed] = [e for e in receipt["unplaced"] if e["beat_id"] == "logo"]
    assert (missed["anchor"], missed["reason"]) == ("title", "no_room_off_captions")


# --- review (Emir, head e0cdfff10): measuring her captions for the title never resolves them --
# Before the fix, the title look measured her caption lines (`caption_boxes`) BEFORE the normal
# whole-timeline caption compile. Under the phone text policy (prod) that compile-for-a-peek
# resolved and cached the whole edit's caption look from the opening cues alone, and sampled
# faces before the real clip timeline was registered (`_environment_sampled` then blocked it).
# Ways it could fail (written before the fix):
# R1 the caption look (size, spot, width) for the WHOLE edit is fitted to the opening cues only;
# R2 faces are sampled on the fallback whole-source window, not the cut / scaled timeline;
# R3 the peek overwrites the policy's caption boxes (the styling pass and card layout read them);
# R4 the title is placed against default-look boxes while the real captions sit elsewhere
#    (she asked for captions at the top) -> title drawn on her captions;
# R5 measuring with the resolved look leaks state of its own; R6 flag off changes anything.

_OPENING = (
    ("Five", 0.0, 0.4),
    ("projects", 0.4, 0.9),
    ("people", 0.9, 1.3),
    ("ask", 1.3, 1.6),
    ("about.", 1.6, 2.2),
)
# A long later passage after the cut (4.0-6.0 s is removed): the tallest caption block of the edit.
_LATER = tuple(
    (word, 6.2 + 0.32 * index, 6.2 + 0.32 * index + 0.3)
    for index, word in enumerate(
        "Honestly the extraordinarily underappreciated documentation generators everybody "
        "recommends kept breaking whenever infrastructure configurations changed overnight.".split()
    )
)


def _styled_cut_job(monkeypatch, *, on: bool, scaled: bool = False, caption_spot=None):
    """Her stacked title on a CUT take (4-6 s removed), phone text policy on (prod), with the
    real environment sampler (only the proxy download and the frame readers are stubbed)."""
    import app.pipeline.phone_text_background as background_mod
    import app.pipeline.render_geometry as geometry_mod
    import app.services.phone_text_styling as styling
    import app.storage as storage_mod
    from app.pipeline.silence_cut import CutPlan, Removal
    from app.services.phone_sources import PHONE_SOURCES_FIELD
    from tests.pipeline.test_phone_subtitled_plan import _binding as _wide_binding

    _look(monkeypatch, on=on)
    _faces(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_text_styling_enabled", True)
    job, snapshot, *_ = _titled(monkeypatch, transcript_words=_words(*_OPENING, *_LATER))
    if scaled:  # a landscape take letterboxed onto the portrait canvas (scale 0.316)
        wide = _wide_binding("c0", duration_s=10.0, width=1920, height=1080)
        job.assembly_plan[PHONE_SOURCES_FIELD] = [wide.model_dump(mode="json")]
        snapshot[PHONE_SOURCES_FIELD] = [wide.model_dump(mode="json")]
        job.all_candidates["landscape_fit"] = "fit"
    cut = CutPlan(
        keep_segments=[(0.0, 4.0), (6.0, 10.0)],
        removed=[Removal(4.0, 6.0, "silence")],
        time_saved_s=2.0,
    )
    monkeypatch.setattr(gb, "_phone_opening_cut", lambda _s, _plan, _w, **_k: (cut, []))
    # The cut take's words come from its checked transcript snapshot (as a cleanup-off job's do).
    from types import SimpleNamespace

    transcript = SimpleNamespace(
        source_words=lambda: _words(*_OPENING, *_LATER),
        analysis=SimpleNamespace(
            language="en", safety_signals=SimpleNamespace(transcript_low_confidence=False)
        ),
    )
    monkeypatch.setattr(gb, "_phone_opening_cut_snapshot", lambda *_a, **_k: transcript)
    seen: list[list[dict]] = []

    def faces(_path, points, **_kwargs):
        policy = styling._POLICY.get()
        seen.append([dict(window) for window in policy.get("_sample_windows") or []])
        face = NormalizedBox(0.30, 0.30, 0.70, 0.55)
        return [ProtectedRegion(0.0, 10.0, face, "face")], {"decoded": len(points)}

    monkeypatch.setattr(storage_mod, "download_to_file", lambda *_a, **_k: None)
    monkeypatch.setattr(geometry_mod, "sample_face_regions", faces)
    monkeypatch.setattr(background_mod, "sample_background", lambda *_a, **_k: None)
    style = {"version": 1, "preferences": {"all_text": {}}, "requested": {}, "selection_calls": 2}
    if caption_spot is not None:
        style["requested"] = {"caption": {"position": caption_spot}}
    job.assembly_plan[styling.PHONE_TEXT_STYLE_FIELD] = style
    return job, seen


def _caption_layers(job) -> list[dict]:
    return [
        layer.model_dump(mode="json")
        for layer in _recipe(job).text_layers
        if layer.id.startswith("caption-")
    ]


@pytest.mark.parametrize("scaled", [False, True], ids=["cut", "scaled_and_cut"])
def test_review_the_title_look_never_changes_her_captions(monkeypatch, scaled):
    """On/off: the same job draws the same caption layers, sampled on the real cut timeline."""
    off, off_seen = _styled_cut_job(monkeypatch, on=False, scaled=scaled)
    gb._run_generative_job(str(off.id))
    assert off.status == "awaiting_device", off.status
    captions_off = _caption_layers(off)

    on, on_seen = _styled_cut_job(monkeypatch, on=True, scaled=scaled)
    gb._run_generative_job(str(on.id))
    assert on.status == "awaiting_device", on.status

    assert [row["text"] for row in _title_rows(on)] == HER_LINES
    # R2: faces were sampled once per run, on the registered (cut) timeline, not the fallback.
    assert off_seen and off_seen[0], "flag off samples on the registered timeline"
    assert on_seen == off_seen
    # R1/R3: every caption layer (text, timing, spot, size, wrap) is identical.
    assert _caption_layers(on) == captions_off
    assert any(layer["start"] >= 4.0 for layer in captions_off), "later cues are drawn"


def test_review_her_title_keeps_off_captions_she_asked_for_at_the_top(monkeypatch):
    """R4: the title is measured against the captions as they are really drawn (resolved look)."""
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.phone_captions import CAPTION_FONT_FAMILY, caption_ink_box

    job, _seen = _styled_cut_job(monkeypatch, on=True, caption_spot="top")
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.status

    rows = _title_rows(job)
    assert [row["text"] for row in rows] == HER_LINES
    title_end = max(row["end_s"] for row in rows)
    face = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    caption_inks: list[NormalizedBox] = []
    for layer in _recipe(job).text_layers:
        if not layer.id.startswith("caption-") or layer.start >= title_end:
            continue
        for run in layer.runs:
            ink = caption_ink_box(run, face)
            if ink is not None:
                caption_inks.append(
                    NormalizedBox(ink[0] / 1080, ink[1] / 1920, ink[2] / 1080, ink[3] / 1920)
                )
    assert caption_inks, "her captions show during the title"
    assert min(box.top for box in caption_inks) < 0.3, "the captions really are at the top"
    for row in rows:
        for caption in caption_inks:
            assert _box(row).coverage_by(caption) <= _MAX_FACE_COVERAGE, (row["text"], caption)
