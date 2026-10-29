"""`_run_phone_subtitled_job` honours the creator's landscape pick (phone Talking).

Real boundaries: the worker runs end to end against the same fakes the speaker-framing
worker tests use (ingest, transcription, device pin), the real
`compile_phone_subtitled_plan` builds the recipe, and the assertions read the pinned
device recipe and the variant row. Failure modes: `backend-failure-modes.md` W1-W7.
"""

from __future__ import annotations

from app.kria.protected_targets import PROTECTED_TEXT_FIELD
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _setup_subtitled,
    _setup_talking_head,
)
from tests.tasks.test_phone_subtitled_speaker_framing_worker import (
    R1,
    _brief,
    _faces,
    _kadikoy,
)

LANDSCAPE = {"output_orientation": "landscape", "landscape_fit": "fill"}
PORTRAIT = {"output_orientation": "portrait", "landscape_fit": "fit"}


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _clips(recipe):
    return next(t for t in recipe.tracks if t.id == "subtitled").clips


def test_a_landscape_pick_compiles_the_wide_canvas_without_bars_or_a_face_shift(monkeypatch):
    calls = _faces(monkeypatch)  # a framing ask is present AND a face is findable
    job, _snapshot, _session, _binding = _kadikoy(
        monkeypatch, brief=_brief(R1), landscape_fit="fit"
    )
    job.all_candidates["creator_render_shape"] = dict(LANDSCAPE)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    [clip] = _clips(recipe)
    assert (clip.transform.scale, clip.transform.position_x) == (1, 0)
    # No face is sampled on a wide canvas, but the receipt says why nothing was face-filled
    # (the brief check then reads "you chose landscape", not a face-search failure).
    assert calls == []
    variant = job.assembly_plan["variants"][0]
    framing = variant["speaker_framing"]
    assert (framing["mode"], framing["reason"], framing["eligible"]) == (
        "centre_fill",
        "landscape_canvas",
        False,
    )
    assert framing["asked_by"] == ["r1"]
    assert (variant["orientation"], variant["landscape_fit"]) == ("landscape", "fill")


def test_the_content_plan_bars_default_never_reaches_a_landscape_canvas(monkeypatch):
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=None, landscape_fit="fit")
    job.all_candidates["creator_render_shape"] = dict(LANDSCAPE)
    gb._run_generative_job(str(job.id))
    [clip] = _clips(_recipe(job))
    assert clip.transform.scale == 1  # not the 0.316 letterbox


def test_no_pick_and_an_explicit_portrait_pick_stay_on_the_portrait_canvas(monkeypatch):
    unpicked, _s, _sess, _b = _kadikoy(monkeypatch, brief=None, landscape_fit="fit")
    gb._run_generative_job(str(unpicked.id))
    unpicked_recipe = _recipe(unpicked)
    unpicked_variant = unpicked.assembly_plan["variants"][0]

    picked, _s, _sess, _b = _kadikoy(monkeypatch, brief=None, landscape_fit="fit")
    picked.all_candidates["creator_render_shape"] = dict(PORTRAIT)
    gb._run_generative_job(str(picked.id))

    assert (unpicked_recipe.canvas.width, unpicked_recipe.canvas.height) == (1080, 1920)
    assert unpicked_recipe.model_dump_json() == _recipe(picked).model_dump_json()
    for variant in (unpicked_variant, picked.assembly_plan["variants"][0]):
        # Portrait writes neither key: byte-identical to before the shape existed.
        assert "orientation" not in variant
        assert "landscape_fit" not in variant
    assert unpicked_variant == picked.assembly_plan["variants"][0]


def test_a_portrait_source_on_a_landscape_canvas_is_filled_not_barred(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    job.all_candidates["creator_render_shape"] = dict(LANDSCAPE)
    gb._run_generative_job(str(job.id))
    recipe = _recipe(job)
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    assert job.assembly_plan["variants"][0]["orientation"] == "landscape"


def test_the_protected_plan_is_measured_on_the_wide_canvas_so_its_verdict_stands(monkeypatch):
    from tests.tasks import test_kri827_protected_text_worker as protected

    seen: list = []
    real = protected.title_mod._face_boxes

    def spy(*args, **kwargs):
        seen.append(kwargs["canvas"])
        return real(*args, **kwargs)

    monkeypatch.setattr(protected.title_mod, "_face_boxes", spy)
    job, snapshot = protected._job(monkeypatch, protected._binding(marks=[protected._mark()]))
    protected._stand_ins(monkeypatch)
    job.all_candidates["creator_render_shape"] = dict(LANDSCAPE)

    variant = protected._run(job, snapshot)

    assert seen and all((c.width, c.height) == (1920, 1080) for c in seen)
    receipt = variant[PROTECTED_TEXT_FIELD]
    # Measured where it plays, so it is not blanket-staled any more.
    assert receipt.get("stale_reason") is None
    assert receipt["landscape_fit"] == "fill"


def test_title_and_closing_rows_are_placed_and_measured_inside_the_wide_frame(monkeypatch):
    import app.pipeline.phone_subtitled_title as title_mod
    from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
    from tests.tasks.test_phone_subtitled_closing_title_worker import _PICK, _lanes_features

    placed_on: list = []
    real = title_mod.place_talking_title

    def spy(row, **kwargs):
        placed_on.append(kwargs["canvas"])
        return real(row, **kwargs)

    monkeypatch.setattr(title_mod, "place_talking_title", spy)
    monkeypatch.setattr(
        title_mod,
        "sample_face_regions",
        lambda path, anchors, **k: (
            [ProtectedRegion(0.0, 10.0, NormalizedBox(0.4, 0.2, 0.6, 0.5), "face")],
            {"attempted": len(anchors), "decoded": len(anchors)},
        ),
    )
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=None)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _lanes_features("authoredText")
    )
    job.all_candidates["creator_render_shape"] = dict(LANDSCAPE)
    job.all_candidates["creator_strategy"] = {
        "opening_title": "3 kahveci",
        "opening_title_duration_s": 2.0,
        "closing_title": _PICK,
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    rows = {row["id"]: row for row in variant["text_elements"]}
    assert {"opening-title", "closing-title"} <= set(rows)
    # Every placement ran on the landscape canvas (never the portrait story canvas) ...
    assert placed_on and all((c.width, c.height) == (1920, 1080) for c in placed_on)
    # ... and each row's measured box fits the 1920x1080 frame.
    wide = _canvas(1920, 1080)
    for row in rows.values():
        box = title_mod._measure_title_box(row, canvas=wide)
        assert 0 <= box.left < box.right <= 1 and 0 <= box.top < box.bottom <= 1, (row["id"], box)
    assert variant["closing_title_placement"]["status"] in {"moved", "preset", "kept"}
    recipe = _recipe(job)
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)


def _canvas(width: int, height: int):
    from app.kria.recipes import Canvas

    return Canvas(width=width, height=height)


def test_the_multi_clip_talking_head_renders_its_cutaways_on_the_wide_canvas(monkeypatch):
    job, _snapshot, _bindings, _transcribed = _setup_talking_head(monkeypatch, spine="c1")
    job.all_candidates["creator_render_shape"] = dict(LANDSCAPE)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    tracks = {track.id: track for track in recipe.tracks}
    assert [c.source_asset_id for c in tracks["subtitled"].clips] == ["c1"]
    assert [c.source_asset_id for c in tracks["talking-head-cutaways"].clips] == ["c0", "c2"]
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"  # the phone Talking editor owns it
    assert variant[gb.PHONE_TALKING_HEAD_FIELD]["speaker_media_id"] == "c1"
    assert (variant["orientation"], variant["landscape_fit"]) == ("landscape", "fill")
