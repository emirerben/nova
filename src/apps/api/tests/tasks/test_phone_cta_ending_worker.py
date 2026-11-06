"""Qendresa's ending on a phone Talking edit (item 14, CREATOR_CTA_ENDING_ENABLED).

Her posts end with a small white handwritten "comment", her keyword big and bold in cream
under it, and a drawn curly arrow pointing at TikTok's buttons, for the last ~3 s. Asked
"End with "comment READ" and an arrow for the last 3 seconds", the phone Talking writer
used to draw one lime pill ("comment READ", `phone_subtitled_title.talking_closing_element`)
and no arrow.

Ways this could fail (written before the code; each is pinned below or in the receipt test):
- text or arrow on her face; the block over the captions in the last 3 s;
- "comment" and "READ" split into two rows so the approved contract's exact closing text no
  longer matches and the render is declined;
- an app build that can't draw freehand drawings (below protocol 6, or no stamp) is sent a
  recipe it must refuse, instead of today's pill;
- the switch off changes the variant or the recipe;
- an ending clip after the speaker extends only one row and leaves the arrow short;
- a closing text that isn't "comment <keyword>" is split anyway;
- (review #2103) the receipt, which now needs the arrow over the ending rows, stops counting
  the arrow the worker drew.

Runs the real worker (`_run_generative_job`) on the shared Talking harness: ingest, probe,
transcription and the face sampler are faked, the planner rows, placement, compiler, contract
check and device pin are the production ones.
"""

from __future__ import annotations

import copy
import uuid

import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_visuals as phone_visuals_mod
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.phone_subtitled_lanes import PHONE_SUBTITLED_LANES_FIELD
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _ending_clip_dict,
    _lane_request,
    _lanes_features,
    _make_fake_bind,
    _setup_subtitled,
    _words,
)

_CTA = "comment READ"
# Her face in V7's last seconds (Apple Vision, normalised x/y/w/h 0.40/0.35/0.42/0.24),
# widened a little: upper middle of the frame, like every talk-to-camera take.
_FACE = NormalizedBox(0.30, 0.30, 0.78, 0.60)
_ENDING = BriefRequirement(
    id="ending",
    kind="text",
    scope="global",
    description='End with "comment READ" and an arrow for the last 3 seconds',
    literal=_CTA,
    facts={"closing": _CTA},
)
_ENDING_NO_ARROW = BriefRequirement(
    id="ending",
    kind="text",
    scope="global",
    description='End with "comment READ" for the last 3 seconds',
    literal=_CTA,
    facts={"closing": _CTA},
)
# She keeps talking over the ending ("comment READ and I'll send it to you"), so captions
# are on screen while the block shows.
_TAKE_WORDS = _words(
    ("So", 0.0, 0.4),
    ("that's", 0.4, 0.8),
    ("it.", 0.8, 1.2),
    ("Comment", 7.1, 7.6),
    ("read", 7.6, 8.0),
    ("and", 8.0, 8.3),
    ("I'll", 8.3, 8.6),
    ("send", 8.6, 8.9),
    ("it.", 8.9, 9.3),
)


def _set_switch(monkeypatch, settings_obj, enabled: bool) -> None:
    """CREATOR_CTA_ENDING_ENABLED; a build without the setting (today's main) has it off."""
    if "creator_cta_ending_enabled" in type(settings_obj).model_fields:
        monkeypatch.setattr(settings_obj, "creator_cta_ending_enabled", enabled)


def _faces(monkeypatch, regions=(ProtectedRegion(0.0, 10.0, _FACE, "face"),)):
    calls: list = []

    def sample(path, anchors, **kwargs):
        calls.append((path, list(anchors)))
        return list(regions), {"attempted": len(anchors), "decoded": len(anchors)}

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)
    return calls


def _cta_job(
    monkeypatch,
    *,
    enabled: bool = True,
    protocol: int | None = 35,
    requirement: BriefRequirement | None = _ENDING,
    closing: str = _CTA,
    features: tuple[str, ...] = ("authoredText", "freehandDrawing"),
):
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, transcript_words=_TAKE_WORDS)
    _set_switch(monkeypatch, gb.settings, enabled)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", _lanes_features(*features))
    job.all_candidates["creator_strategy"] = {"closing_title": closing}
    if protocol is not None:
        job.all_candidates["creator_client_protocol"] = protocol
    if requirement is not None:
        binding_dump = BriefBinding.create(
            uuid.uuid4(), CreativeBrief(version=1, requirements=[requirement])
        ).model_dump(mode="json")
        snapshot["creator_brief_binding"] = binding_dump
        job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding_dump)
    return job


def _closing_rows(variant: dict) -> list[dict]:
    return [
        row
        for row in variant.get("text_elements") or []
        if (row.get("source_params") or {}).get("source") == "closing_title"
    ]


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _closes_with(job, text: str = _CTA) -> list:
    """The device-pin check the approved contract runs: "comment READ" must still count as
    the confirmed closing text once it is drawn as two rows."""
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="closing", text=text),)
    )
    return verify_phone_recipe(contract, _recipe(job))


def _receipt_status(variant: dict) -> str:
    """The finished-video receipt for the arrow ask, on the variant the worker persisted."""
    from app.kria import brief_checks as bc

    return bc.check_requirement(_ENDING, bc.plan_facts_from_phone_variant(variant)).status


def _drawn_box(row: dict) -> NormalizedBox:
    """Where the row's ink sits on the 1080x1920 story canvas (the production measure)."""
    return title_mod._measure_title_box(row, canvas=_STORY_CANVAS)


def _arrow_box(drawing: dict) -> NormalizedBox:
    points = [point for stroke in drawing["strokes"] for point in stroke["points"]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return NormalizedBox(min(xs), min(ys), max(xs), max(ys))


def test_comment_read_with_an_arrow_draws_her_ending(monkeypatch):
    _faces(monkeypatch)
    job = _cta_job(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    variant = job.assembly_plan["variants"][0]
    lead, keyword = _closing_rows(variant)
    # A small white handwritten "comment" over her keyword, big and bold in cream.
    assert (lead["text"], keyword["text"]) == ("comment", "READ")
    assert lead["font_family"] == "Caveat Brush"
    assert lead["color"].upper() == "#FFFFFF"
    assert keyword["font_family"] == "Bricolage Grotesque"
    assert keyword["color"].upper() == "#FCF3C3"
    assert not lead.get("background_color") and not keyword.get("background_color")
    assert keyword["size_px"] >= 1.5 * lead["size_px"]
    for row in (lead, keyword):
        assert (row["start_s"], row["end_s"]) == (7.0, 10.0)
    lead_box, keyword_box = _drawn_box(lead), _drawn_box(keyword)
    assert lead_box.bottom <= keyword_box.top + 0.005  # "comment" sits above the keyword
    # Big enough to read (and for the kit's OCR to see as text, not screen text: >= 0.026).
    assert lead_box.bottom - lead_box.top >= 0.04
    assert keyword_box.bottom - keyword_box.top >= 1.5 * (lead_box.bottom - lead_box.top)

    # A curly arrow drawn in, white, to the right of "comment", held to the end.
    [arrow] = variant["drawings"]
    assert arrow["id"] == "closing-arrow"
    assert (arrow["start_s"], arrow["end_s"]) == (7.0, 10.0)
    assert arrow["animation"]["entrance"] == "draw"
    assert len(arrow["strokes"]) >= 2  # the curl and its head
    assert {stroke["color"] for stroke in arrow["strokes"]} == {"#FFFFFF"}
    box = _arrow_box(arrow)
    assert box.left >= (lead_box.left + lead_box.right) / 2
    assert box.right <= 0.97 and box.bottom <= 0.95
    assert box.bottom - box.top >= 0.05  # it curls down toward the keyword, not a dash

    # Never on her face (the product rule): text and arrow.
    for drawn in (lead_box, keyword_box, box):
        assert drawn.coverage_by(_FACE) <= 0.05, drawn

    # Placed where it was promised, and the receipt says so.
    placement = variant["closing_title_placement"]
    assert placement["status"] == "cta_ending"
    assert placement["face_collision"] is False
    assert placement["text_overlap"] is False  # clear of the captions she speaks over it

    # The phone draws exactly that: two text layers and one drawing layer, and the
    # approved contract still finds "comment READ" closing the video.
    recipe = _recipe(job)
    assert [layer.id for layer in recipe.drawing_layers] == ["closing-arrow"]
    assert "freehandDrawing" in recipe.required_capabilities
    assert _closes_with(job)
    # The finished-video receipt counts the arrow the worker drew (review #2103: it now has to
    # sit over the ending rows, not just carry the `closing-arrow` ID).
    assert _receipt_status(variant) == "met"


def test_comment_read_without_an_arrow_gets_her_two_styles_and_no_arrow(monkeypatch):
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, requirement=_ENDING_NO_ARROW)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    lead, keyword = _closing_rows(variant)
    assert (lead["text"], keyword["text"]) == ("comment", "READ")
    assert not variant.get("drawings")
    assert not _recipe(job).drawing_layers
    assert _closes_with(job)


@pytest.mark.parametrize("protocol", [None, 5])
def test_an_app_build_without_drawings_keeps_todays_closing_tag(monkeypatch, protocol):
    """Older builds (no `X-Kria-Client-Protocol` stamp, or below the drawings protocol 6)
    keep today's lime tag and never receive a drawing layer."""
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, protocol=protocol)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    [row] = _closing_rows(variant)
    assert row["text"] == _CTA
    assert row["background_color"] == "#C5F82A"
    assert not variant.get("drawings")
    assert not _recipe(job).drawing_layers
    assert _closes_with(job)


def test_without_freehand_drawing_verified_it_keeps_todays_closing_tag(monkeypatch):
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, features=("authoredText",))

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    [row] = _closing_rows(variant)
    assert (row["text"], row["background_color"]) == (_CTA, "#C5F82A")
    assert not variant.get("drawings")


def test_a_closing_text_that_is_not_a_comment_ask_keeps_todays_tag(monkeypatch):
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, closing="MY PICK")

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    [row] = _closing_rows(variant)
    assert (row["text"], row["background_color"]) == ("MY PICK", "#C5F82A")
    assert not variant.get("drawings")


def test_after_an_ending_clip_both_rows_and_the_arrow_run_to_the_very_end(monkeypatch):
    _faces(monkeypatch)
    job = _cta_job(
        monkeypatch,
        features=("stillImages", "visualVideos", "visualBlocks", "authoredText", "freehandDrawing"),
    )
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        ending_clip=_ending_clip_dict("video1")
    )

    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    assert recipe.duration == pytest.approx(15.0)
    variant = job.assembly_plan["variants"][0]
    for row in _closing_rows(variant):
        assert row["end_s"] == pytest.approx(15.0)
    [arrow] = variant["drawings"]
    assert arrow["end_s"] == pytest.approx(15.0)
    [layer] = recipe.drawing_layers
    assert layer.end == pytest.approx(15.0)
    assert _closes_with(job)
    assert _receipt_status(variant) == "met"


def test_switched_off_the_job_is_exactly_todays(monkeypatch):
    """CREATOR_CTA_ENDING_ENABLED=false: the arrow ask and the app build change nothing;
    the variant and the recipe equal the same job without them (today's lime tag)."""
    _faces(monkeypatch)
    plain = _cta_job(monkeypatch, enabled=False, protocol=None, requirement=_ENDING_NO_ARROW)
    gb._run_generative_job(str(plain.id))
    expected_variant = copy.deepcopy(plain.assembly_plan["variants"][0])
    expected_recipe = _recipe(plain)

    job = _cta_job(monkeypatch, enabled=False)
    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    [row] = _closing_rows(variant)
    assert (row["text"], row["background_color"]) == (_CTA, "#C5F82A")
    assert "drawings" not in variant
    assert variant == {**expected_variant, "render_generation_id": variant["render_generation_id"]}
    assert _recipe(job) == expected_recipe


# --- Review findings (written before the fix; each failed on the first version) ---------------


def _ink_on(drawing: dict, box: NormalizedBox) -> bool:
    """Does any point along the drawing's strokes fall inside ``box``?"""
    for stroke in drawing["strokes"]:
        pts = stroke["points"]
        for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
            for step in range(9):
                x, y = ax + (bx - ax) * step / 8, ay + (by - ay) * step / 8
                if box.left <= x <= box.right and box.top <= y <= box.bottom:
                    return True
    return False


def _turns(points) -> list[float]:
    """Degrees the pen turns at each inner point of a stroke."""
    import math

    out = []
    for (ax, ay), (bx, by), (cx, cy) in zip(points, points[1:], points[2:], strict=False):
        a1, a2 = math.atan2(by - ay, bx - ax), math.atan2(cy - by, cx - bx)
        out.append(abs((math.degrees(a2 - a1) + 180) % 360 - 180))
    return out


def test_the_arrow_head_keeps_its_corners_on_the_phone(monkeypatch):
    """The phone draws every stroke as midpoint-smoothed curves through its points
    (`NativeDrawingPainter.smoothPath`), so a stroke that turns a sharp corner comes out as a
    loop. The head's corners must be ends of straight strokes, never inner points."""
    _faces(monkeypatch)
    job = _cta_job(monkeypatch)

    gb._run_generative_job(str(job.id))

    [arrow] = job.assembly_plan["variants"][0]["drawings"]
    for stroke in arrow["strokes"]:
        assert max(_turns(stroke["points"]), default=0.0) <= 45.0, stroke["points"]


@pytest.mark.parametrize("closing", ["comment READ!", "Comment: READ", 'comment "READ"'])
def test_a_closing_text_with_punctuation_still_renders_and_matches_the_contract(
    monkeypatch, closing
):
    """The approved contract wants the creator's exact closing text; the rows drawn for it
    (her ending, or the tag) must still read as exactly that text."""
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, closing=closing)

    gb._run_generative_job(str(job.id))

    assert _closes_with(job, closing)


@pytest.mark.parametrize("keyword", ["READ", "MASTERCLASS", "RECIPE", "FREE GUIDE"])
def test_a_wide_keyword_keeps_the_arrow_off_it_and_stays_one_line(monkeypatch, keyword):
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, closing=f"comment {keyword}")

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    lead, key = _closing_rows(variant)
    assert key["text"] == keyword
    key_box = _drawn_box(key)
    # One line: the ink is no taller than one line of its own size.
    assert (key_box.bottom - key_box.top) * 1920 <= 1.3 * key["size_px"]
    [arrow] = variant["drawings"]
    # The arrow's line never crosses the keyword (its bounding box may wrap a corner of it).
    assert not _ink_on(arrow, key_box), arrow["strokes"]
    for box in (_drawn_box(lead), key_box, _arrow_box(arrow)):
        assert box.coverage_by(_FACE) <= 0.05
        assert 0.0 <= box.left and box.right <= 1.0


def test_an_arrow_asked_for_other_text_is_not_drawn_at_the_ending(monkeypatch):
    menu = BriefRequirement(
        id="menu",
        kind="text",
        scope="global",
        description='Put "MENU" with an arrow pointing at the board',
        literal="MENU",
    )
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, requirement=_ENDING_NO_ARROW)
    binding_dump = BriefBinding.create(
        uuid.uuid4(), CreativeBrief(version=1, requirements=[_ENDING_NO_ARROW, menu])
    ).model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = binding_dump

    gb._run_generative_job(str(job.id))

    assert not job.assembly_plan["variants"][0].get("drawings")


def test_no_arrow_draws_no_arrow(monkeypatch):
    no_arrow = _ENDING.model_copy(
        update={"description": 'End with "comment READ" for the last 3 seconds, no arrow'}
    )
    _faces(monkeypatch)
    job = _cta_job(monkeypatch, requirement=no_arrow)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert [row["text"] for row in _closing_rows(variant)] == ["comment", "READ"]
    assert not variant.get("drawings")
