"""KRI-547: `_run_phone_subtitled_job` face-fills a sideways speaker on request.

The Kadıköy chat: a 1920x1080 selfie take, speaker in the left third, and the
brief's r1 "yatay videoyu dikey (9:16) formata getir ve yüzü hep kadrajda tut".
The worker reads the ask off the pinned brief, samples the face across the take
and compiles a cover-filled crop shifted onto it (instead of the letterbox the
item's default ``landscape_fit="fit"`` gives), persists ``speaker_framing`` on
the variant, and maps the face through the crop for the title and the cards.
No ask, or ``PHONE_SPEAKER_FACE_FILL_ENABLED=false``: byte-identical.
"""

from __future__ import annotations

import copy
import uuid

import pytest

import app.pipeline.phone_speaker_framing as framing_mod
import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_subtitled_plan import _binding as _wide_binding
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _basic_beat_receipt,
    _beat_grounding_mock,
    _enable_beats,
    _grounding_mock,
    _make_fake_bind,
    _setup_subtitled,
)

R1 = "yatay videoyu dikey (9:16) formata getir ve yüzü hep kadrajda tut"
_FIT_SCALE = 0.31640625


def _brief(*descriptions: str) -> dict:
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id=f"r{index}", kind="style", scope="global", description=text)
            for index, text in enumerate(descriptions, start=1)
        ],
    )
    return BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")


def _kadikoy(monkeypatch, *, brief: dict | None, landscape_fit: str = "fit"):
    job, snapshot, session, _binding = _setup_subtitled(monkeypatch)
    landscape = _wide_binding("c0", duration_s=10.0, width=1920, height=1080)
    for plan in (job.assembly_plan, snapshot):
        plan[PHONE_SOURCES_FIELD] = [landscape.model_dump(mode="json")]
        if brief is not None:
            plan["creator_brief_binding"] = copy.deepcopy(brief)
    job.all_candidates["landscape_fit"] = landscape_fit
    return job, snapshot, session, landscape


def _faces(monkeypatch, box_for=lambda index: NormalizedBox(0.2, 0.15, 0.45, 0.6)):
    calls: list = []

    def sample(path, anchors, **kwargs):
        calls.append((path, list(anchors), kwargs))
        regions = [
            ProtectedRegion(at - 0.5, at + 0.5, box, "face")
            for index, at in enumerate(anchors)
            if (box := box_for(index)) is not None
        ]
        return regions, {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    monkeypatch.setattr(framing_mod, "sample_face_regions", sample)
    return calls


def _speaker(job):
    recipe = device_status(job, "subtitled").request.recipe
    return next(t for t in recipe.tracks if t.id == "subtitled").clips


def test_the_kadikoy_ask_face_fills_the_sideways_speaker(monkeypatch):
    calls = _faces(monkeypatch)
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=_brief(R1))

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    [clip] = _speaker(job)
    assert clip.transform.scale == 1
    # The speaker sits in the left third: the picture slides right onto them.
    assert clip.transform.position_x > 0
    variant = job.assembly_plan["variants"][0]
    framing = variant["speaker_framing"]
    assert (framing["mode"], framing["reason"]) == ("face_fill", "face_in_window")
    assert framing["asked_by"] == ["r1"]
    assert framing["window"]["position_x"] == pytest.approx(clip.transform.position_x, abs=0.01)
    assert variant["landscape_fit"] == "fill"
    [(path, anchors, kwargs)] = calls
    assert path == "/tmp/c0.mp4" and len(anchors) == 8  # 10 s at one per 1.25 s
    assert kwargs["raw_boxes"] is True
    # Render-ready: the brief receipt reads the variant and says Done.
    [receipt] = build_receipts(
        [BriefRequirement(id="r1", kind="style", scope="global", description=R1)],
        plan_facts_from_phone_variant(variant),
        include_unchecked=True,
    )
    assert (receipt.status, receipt.verification) == ("met", "checked")


def test_a_face_that_wanders_keeps_the_letterbox_and_records_why(monkeypatch):
    _faces(
        monkeypatch,
        lambda index: (
            NormalizedBox(0.05, 0.15, 0.3, 0.6)
            if index < 4
            else NormalizedBox(0.6, 0.15, 0.85, 0.6)
        ),
    )
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=_brief(R1))

    gb._run_generative_job(str(job.id))

    [clip] = _speaker(job)
    assert clip.transform.scale == pytest.approx(_FIT_SCALE)
    assert clip.transform.position_x == 0
    variant = job.assembly_plan["variants"][0]
    assert (variant["speaker_framing"]["mode"], variant["speaker_framing"]["reason"]) == (
        "letterbox",
        "face_moves_too_much",
    )
    assert "landscape_fit" not in variant
    [receipt] = build_receipts(
        [BriefRequirement(id="r1", kind="style", scope="global", description=R1)],
        plan_facts_from_phone_variant(variant),
        include_unchecked=True,
    )
    assert (receipt.status, receipt.verification) == ("partial", "checked")


def _run(monkeypatch, *, brief, enabled=True):
    calls = _faces(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_speaker_face_fill_enabled", enabled)
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=brief)
    gb._run_generative_job(str(job.id))
    variant = job.assembly_plan["variants"][0]
    return device_status(job, "subtitled").request.recipe, variant, calls


def test_without_a_framing_ask_the_job_is_byte_identical(monkeypatch):
    baseline_recipe, baseline_variant, _ = _run(monkeypatch, brief=None)
    other_recipe, other_variant, calls = _run(
        monkeypatch, brief=_brief("altyazılar Türkçe olsun", "faster cuts")
    )
    assert calls == []
    assert other_recipe.model_dump_json() == baseline_recipe.model_dump_json()
    assert other_variant == baseline_variant
    assert "speaker_framing" not in other_variant
    assert all(c.transform.scale == pytest.approx(_FIT_SCALE) for c in _clips(other_recipe))


def test_kill_switch_off_ignores_the_ask_byte_identically(monkeypatch):
    baseline_recipe, baseline_variant, _ = _run(monkeypatch, brief=None)
    off_recipe, off_variant, calls = _run(monkeypatch, brief=_brief(R1), enabled=False)
    assert calls == []
    assert off_recipe.model_dump_json() == baseline_recipe.model_dump_json()
    assert off_variant == baseline_variant


def _clips(recipe):
    return next(t for t in recipe.tracks if t.id == "subtitled").clips


def test_an_unreadable_binding_is_no_ask(monkeypatch):
    calls = _faces(monkeypatch)
    job, _snapshot, _session, _binding = _kadikoy(
        monkeypatch, brief={"thread_id": "t", "state": "pinned", "digest": "nope"}
    )
    gb._run_generative_job(str(job.id))
    assert calls == []
    assert "speaker_framing" not in job.assembly_plan["variants"][0]


def test_the_title_checks_the_face_where_the_crop_draws_it(monkeypatch):
    _faces(monkeypatch)
    seen: list[dict] = []
    real = title_mod.place_talking_title

    def spy(row, **kwargs):
        seen.append(kwargs)
        return real(row, **kwargs)

    monkeypatch.setattr(title_mod, "place_talking_title", spy)
    monkeypatch.setattr(
        title_mod,
        "sample_face_regions",
        lambda path, anchors, **k: ([], {"attempted": len(anchors), "decoded": len(anchors)}),
    )
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=_brief(R1))
    job.all_candidates["creator_strategy"] = {"opening_title": "3 kahveci"}

    gb._run_generative_job(str(job.id))

    [clip] = _speaker(job)
    [kwargs] = seen
    assert kwargs["scale"] == 1
    assert kwargs["position_x"] == pytest.approx(clip.transform.position_x, abs=0.01)


def test_beat_and_overlay_cards_avoid_the_face_on_the_crop(monkeypatch):
    _faces(monkeypatch)
    beats = _beat_grounding_mock([], [], _basic_beat_receipt())
    overlays = _grounding_mock([])
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=_brief(R1))
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(phone_reaction_grounding_mod, "ground_phone_reaction_beats", beats)
    monkeypatch.setattr(phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", overlays)
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [{"beat_id": "b1", "trigger": "İlk durak", "visual_id": "v1"}]
    }

    gb._run_generative_job(str(job.id))

    [clip] = _speaker(job)
    mapper = beats.call_args.kwargs["face_box_to_canvas"]
    assert overlays.call_args.kwargs["face_box_to_canvas"] is mapper
    # The left-third face lands mid-canvas, not at the source's left edge.
    mapped = mapper(NormalizedBox(0.2, 0.15, 0.45, 0.6))
    assert mapped is not None and mapped.left < 0.5 < mapped.right
    assert clip.transform.position_x > 0


def test_without_an_ask_cards_get_no_mapper(monkeypatch):
    _faces(monkeypatch)
    beats = _beat_grounding_mock([], [], _basic_beat_receipt())
    overlays = _grounding_mock([])
    job, _snapshot, _session, _binding = _kadikoy(monkeypatch, brief=None)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(phone_reaction_grounding_mod, "ground_phone_reaction_beats", beats)
    monkeypatch.setattr(phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", overlays)
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [{"beat_id": "b1", "trigger": "İlk durak", "visual_id": "v1"}]
    }

    gb._run_generative_job(str(job.id))

    assert "face_box_to_canvas" not in beats.call_args.kwargs
    assert "face_box_to_canvas" not in overlays.call_args.kwargs
