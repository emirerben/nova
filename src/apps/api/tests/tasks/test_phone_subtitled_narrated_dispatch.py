"""Worker-level tests for `_run_phone_subtitled_job` / `_run_phone_narrated_job`
(KRI-132), modeled on `test_phone_montage_dispatch.py`'s `setup()` -- same
fences (immutable generation, bound sources, single pinned device revision,
redelivery idempotency), but exercising the LEAN subtitled/narrated paths
instead of the montage-family archetype/spec prework.

KRI-174 Phase 1 extends this file with `_run_phone_subtitled_job`'s optional
media-lanes handling (overlay sticker/photo cards, catalog sound effects, a
muted ending clip) and the new `_resolve_phone_sound_effect` helper.

KRI-176 extends it further with the transcript-grounded overlay lane
(`ground_phone_subtitled_overlays`, `app.services.phone_overlay_grounding`):
the worker's own wiring is exercised here with that function mocked --
`tests/services/test_phone_overlay_grounding.py` covers its internals.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_lanes import (
    PHONE_SUBTITLED_LANES_FIELD,
    ResolvedSoundEffect,
    SubtitledLaneError,
    SubtitledOverlayCard,
    SubtitledSoundEffect,
)
from app.pipeline.transcribe import Transcript, Word
from app.services.device_render import device_status
from app.services.phone_overlay_grounding import GroundedOverlayCards
from app.services.phone_reaction_grounding import GroundedReactionBeats
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD, PhoneVisualBinding
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_montage_plan import _binding
from tests.tasks.test_generative_build import _Meta


def _job_and_session(monkeypatch, *, assembly_plan: dict, all_candidates: dict):
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(assembly_plan),
        status="queued",
        all_candidates=all_candidates,
        error_detail=None,
        failure_reason=None,
    )
    session = Mock()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "positionedText",
            "animatedText",
            "narrationAudio",
            "audioMix",
        ],
    )
    monkeypatch.setattr(gb.settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True)
    # No network calls: skip the LLM-based caption spelling pass entirely.
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
    # KRI-174: off by default, same as production. Individual tests flip it.
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", False)
    # KRI-176: off by default, same as production. Individual tests flip it
    # (together with the `PHONE_SUBTITLED_OVERLAY_FEATURES` verified-feature
    # set) to exercise `overlay_grounding_enabled`.
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", False)
    return job, session


def _words(*spans: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in spans]


# --- subtitled ("Talking to camera") ----------------------------------------


def _setup_subtitled(monkeypatch, *, edit_format="subtitled", transcript_words=None):
    binding = _binding("c0", duration_s=10.0)
    snapshot = {
        PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
        "creator_generation_id": "generation",
    }
    all_candidates = {
        "clip_paths": ["phone-proxies/c0.mp4"],
        "edit_format": edit_format,
        "language": "en",
    }
    job, session = _job_and_session(
        monkeypatch, assembly_plan=snapshot, all_candidates=all_candidates
    )

    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c0", 5.0)],
            "clip_id_to_gcs": {"c0": binding.proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
            "probe_map": {},
            "hero": _Meta("c0", 5.0),
        },
        raising=False,
    )

    import app.pipeline.probe as probe_mod
    import app.pipeline.transcribe as transcribe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: SimpleNamespace(duration_s=10.0), raising=False
    )
    words = transcript_words or _words(("Hello", 0.0, 0.5), ("there.", 0.5, 1.0))
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper_cached",
        lambda *a, **k: Transcript(words=words, language="en"),
        raising=False,
    )
    return job, snapshot, session, binding


def test_subtitled_compiles_and_pins_device_request(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    status = device_status(job, "subtitled")
    assert status.request.identity.variant_id == "subtitled"
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == "subtitled"
    assert variant["resolved_archetype"] == "subtitled"
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"
    assert variant["caption_cues"]
    assert job.assembly_plan["phone_deferred_variants"] == []


def test_subtitled_redelivery_is_a_no_op(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    gb._run_generative_job(str(job.id))
    first_request = device_status(job, "subtitled").request

    reingest = Mock(side_effect=AssertionError("redelivery must not re-run ingest"))
    monkeypatch.setattr(gb, "_ingest_clips", reingest, raising=False)
    gb._run_phone_subtitled_job(
        str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
    )
    assert device_status(job, "subtitled").request == first_request
    reingest.assert_not_called()


def test_subtitled_rejects_multi_clip_bindings(monkeypatch):
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch)
    extra = _binding("c1", duration_s=8.0)
    snapshot[PHONE_SOURCES_FIELD] = [
        binding.model_dump(mode="json"),
        extra.model_dump(mode="json"),
    ]
    with pytest.raises(ValueError, match="exactly one clip"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_rejects_when_flag_off(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_rendering_enabled", False)
    with pytest.raises(ValueError, match="talking-to-camera"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_fails_closed_on_required_speech_cleanup_contract(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    snapshot["speech_cleanup_contract"] = "required_v1"
    with pytest.raises(UnsupportedPhonePlan, match="speech-cleanup"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_rejects_clip_over_five_minutes(monkeypatch):
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    import app.pipeline.probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_video", lambda path: SimpleNamespace(duration_s=301.0), raising=False
    )
    with pytest.raises(UnsupportedPhonePlan, match="5 minutes"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_self_narrated_narrated_format_resolves_through_subtitled_compiler(monkeypatch):
    """A `narrated_ready` item with NO voiceover -- the dispatch gate only
    lets the single-clip self-narration shape through, which the worker fork
    routes to `_run_phone_subtitled_job`. This re-verifies the REAL archetype
    resolution and only proceeds when it lands on `subtitled`."""
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *a, **k: ("subtitled", None, None), raising=False
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["resolved_archetype"] == "subtitled"


def test_self_narrated_resolving_to_talking_head_fails_closed(monkeypatch):
    """The real `_resolve_archetype` can decide `talking_head`/`montage` for a
    self-narrated item once footage is actually probed -- neither has a
    phone compiler reachable from this runner, so it must fail closed rather
    than silently render the wrong shape."""
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    monkeypatch.setattr(
        gb, "_resolve_archetype", lambda *a, **k: ("talking_head", "c0", None), raising=False
    )
    with pytest.raises(UnsupportedPhonePlan, match="unsupported"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_subtitled_worker_rejects_direct_format_it_does_not_own(monkeypatch):
    """`_run_phone_subtitled_job`'s own defense-in-depth: only `subtitled` or
    a no-voiceover narrated* item may reach it -- a montage-family item
    (routed to `_run_phone_montage_job` by the dispatch fork in normal
    operation) is rejected if ever called directly."""
    job, snapshot, _session, _binding_ = _setup_subtitled(monkeypatch, edit_format="montage")
    with pytest.raises(ValueError, match="No phone renderer is registered"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


# --- KRI-174: subtitled media lanes (overlays / sound effects / ending clip) -


def _lane_request(*, overlays=None, sound_effects=None, ending_clip=None) -> dict:
    return {
        "overlays": overlays or [],
        "sound_effects": sound_effects or [],
        "ending_clip": ending_clip,
    }


def _overlay_card(card_id: str, media_id: str, *, start_s: float = 0.0, end_s: float = 2.0) -> dict:
    return {
        "id": card_id,
        "media_id": media_id,
        "gcs_path": f"users/u1/plan/item1/pool/{media_id}.jpg",
        "generation": "1",
        "start_s": start_s,
        "end_s": end_s,
    }


def _ending_clip_dict(media_id: str) -> dict:
    return {
        "media_id": media_id,
        "gcs_path": f"users/u1/plan/item1/pool/{media_id}.mp4",
        "generation": "2",
    }


def _sfx_request_dict(sfx_id: str, catalog_id: str, *, at_s: float = 1.0) -> dict:
    return {"id": sfx_id, "catalog_id": catalog_id, "at_s": at_s}


def _fake_resolve_sfx(sfx: SubtitledSoundEffect) -> ResolvedSoundEffect:
    return ResolvedSoundEffect(
        request=sfx,
        asset=LibraryRenderAsset(
            id=f"sfx-{sfx.catalog_id}",
            catalog="sound_effect",
            catalog_id=sfx.catalog_id,
            generation="3",
            fingerprint=RenderFingerprint(sha256="e" * 64, byte_count=50),
        ),
        duration_s=1.5,
    )


def _make_fake_bind(calls: list):
    def _fake_bind(_open_session, *, job_id, pins):  # noqa: ARG001
        calls.append(dict(pins))
        bound = []
        for media_id, (kind, path, generation) in pins.items():
            kwargs: dict = {
                "media_id": media_id,
                "gcs_path": path,
                "generation": generation,
                "sha256": "a" * 64,
                "byte_count": 100,
                "kind": kind,
            }
            if kind == "video":
                kwargs.update(duration_s=5.0, width=1080, height=1920, orientation_degrees=0)
            bound.append(PhoneVisualBinding(**kwargs))
        return tuple(bound)

    return _fake_bind


def _lanes_features(*extra: str) -> list[str]:
    return [
        "basicComposition",
        "local1080Export",
        "positionedText",
        "animatedText",
        "narrationAudio",
        "audioMix",
        *extra,
    ]


def _overlay_grounding_features(*extra: str) -> list[str]:
    """`PHONE_SUBTITLED_OVERLAY_FEATURES` verified, plus whatever else a test
    needs -- `overlay_grounding_enabled` additionally requires
    `phone_subtitled_media_lanes_enabled` and `media_overlays_enabled`."""
    return _lanes_features("stillImages", "visualBlocks", "alphaOverlay", *extra)


def _grounding_mock(cards: list, *, matcher: str = "agent") -> Mock:
    return Mock(
        return_value=GroundedOverlayCards(
            cards=cards,
            receipt={
                "version": 1,
                "matcher": matcher,
                "face_sampling": "skipped",
                "placed": [
                    {
                        "media_id": card.media_id,
                        "label": "photo.jpg",
                        "start_s": card.start_s,
                        "end_s": card.end_s,
                        "reason": "You mention it here.",
                    }
                    for card in cards
                ],
                "unplaced": [],
                "wishlist": [],
            },
        )
    )


# --- KRI-178: creator-authored reaction beats -------------------------------


def _beat_dict(beat_id: str, trigger: str, **kwargs) -> dict:
    return {"beat_id": beat_id, "trigger": trigger, **kwargs}


def _closing_dict(visual_id: str, **kwargs) -> dict:
    return {"visual_id": visual_id, **kwargs}


def _beats_features(*extra: str) -> list[str]:
    """The full verified-feature set `phone_subtitled_reaction_beats_
    supported()` requires: `PHONE_SUBTITLED_OVERLAY_FEATURES` (via
    `_overlay_grounding_features`) plus `soundEffects` (`audioMix` is
    already in both)."""
    return _overlay_grounding_features("soundEffects", *extra)


def _enable_beats(monkeypatch, *, extra_features: tuple = ()) -> None:
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_reaction_beats_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _beats_features(*extra_features)
    )


def _beat_grounding_mock(cards: list, sfx: list, receipt: dict) -> Mock:
    return Mock(return_value=GroundedReactionBeats(cards=cards, sound_effects=sfx, receipt=receipt))


def _basic_beat_receipt(
    *, placed: list | None = None, unplaced: list | None = None, closing: dict | None = None
) -> dict:
    return {
        "version": 1,
        "matcher": "phrase",
        "face_sampling": "ok",
        "placed": placed or [],
        "unplaced": unplaced or [],
        "closing": closing or {"status": "none", "badge": "none"},
    }


def test_subtitled_media_lanes_flag_off_is_byte_identical(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    # Flag stays off (the `_job_and_session` default). A lanes field on the
    # snapshot must be completely ignored.
    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[_overlay_card("card1", "photo1")]
    )
    bind_mock = Mock(side_effect=AssertionError("bind must not run with the flag off"))
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind_mock)
    grounding_mock = Mock(side_effect=AssertionError("grounding must not run with the flag off"))
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", grounding_mock
    )
    # KRI-178: a strategy carrying reaction beats must also be completely
    # ignored with the flag off, same as the KRI-174 lane request above.
    beat_grounding_mock = Mock(
        side_effect=AssertionError("beat grounding must not run with the flag off")
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding_mock
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "goal", visual_id="photo1")]
    }

    import app.pipeline.phone_subtitled_plan as subtitled_plan_mod

    compile_spy = Mock(wraps=subtitled_plan_mod.compile_phone_subtitled_plan)
    monkeypatch.setattr(subtitled_plan_mod, "compile_phone_subtitled_plan", compile_spy)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert "overlay_transcript" not in variant
    assert "phone_lane_receipt" not in variant
    assert "phone_overlay_receipt" not in variant
    assert "phone_beat_receipt" not in variant
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    bind_mock.assert_not_called()
    grounding_mock.assert_not_called()
    beat_grounding_mock.assert_not_called()
    call_kwargs = compile_spy.call_args.kwargs
    assert "visuals" not in call_kwargs
    assert "lanes" not in call_kwargs


def test_subtitled_media_lanes_on_but_overlays_not_supported_skips_grounding(monkeypatch):
    """`phone_subtitled_media_lanes_enabled` alone isn't the KRI-176 gate --
    `phone_subtitled_overlays_supported()` also needs `media_overlays_enabled`
    and every `PHONE_SUBTITLED_OVERLAY_FEATURES` feature verified. Here the
    lanes flag is on (so a hand-authored lane request still resolves) but
    `media_overlays_enabled` stays off, so grounding must never run."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _overlay_grounding_features()
    )
    # `media_overlays_enabled` left at the `_job_and_session` default (False).
    grounding_mock = Mock(side_effect=AssertionError("grounding must not run when unsupported"))
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", grounding_mock
    )
    # KRI-178: `phone_subtitled_reaction_beats_supported()` requires
    # `phone_subtitled_overlays_supported()` -- with overlays unsupported,
    # beats must be unsupported too, even with a strategy that carries them.
    beat_grounding_mock = Mock(
        side_effect=AssertionError("beat grounding must not run when unsupported")
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding_mock
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "goal", visual_id="photo1")]
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert "phone_overlay_receipt" not in variant
    assert "phone_beat_receipt" not in variant
    grounding_mock.assert_not_called()
    beat_grounding_mock.assert_not_called()


def test_subtitled_overlay_grounding_happy_path(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _overlay_grounding_features()
    )
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))

    cards = [
        SubtitledOverlayCard(
            id="pip-0",
            media_id="photo1",
            gcs_path="users/u1/plan/item1/pool/photo1.jpg",
            generation="1",
            start_s=0.0,
            end_s=2.0,
        ),
        SubtitledOverlayCard(
            id="pip-1",
            media_id="photo2",
            gcs_path="users/u1/plan/item1/pool/photo2.jpg",
            generation="1",
            start_s=3.0,
            end_s=5.0,
        ),
    ]
    grounding_mock = _grounding_mock(cards)
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", grounding_mock
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    grounding_mock.assert_called_once()
    assert grounding_mock.call_args.kwargs["job_id"] == str(job.id)
    assert grounding_mock.call_args.kwargs["clip_path"] == "/tmp/c0.mp4"
    receipt = variant["phone_lane_receipt"]
    assert receipt["applied"] == ["overlays"]
    assert receipt["dropped"] == []
    overlay_receipt = variant["phone_overlay_receipt"]
    assert len(overlay_receipt["placed"]) == 2
    assert {c["media_id"] for c in overlay_receipt["placed"]} == {"photo1", "photo2"}
    assert len(bind_calls) == 1
    assert set(bind_calls[0]) == {"photo1", "photo2"}
    assert job.assembly_plan[PHONE_VISUALS_FIELD]


def test_subtitled_overlay_grounding_failure_drops_lane_not_job(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _overlay_grounding_features()
    )

    def _broken_grounding(*args, **kwargs):
        raise RuntimeError("pool query exploded")

    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _broken_grounding
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    lane_receipt = variant["phone_lane_receipt"]
    assert lane_receipt["applied"] == []
    assert len(lane_receipt["dropped"]) == 1
    assert lane_receipt["dropped"][0]["lane"] == "overlays"
    assert "grounding failed" in lane_receipt["dropped"][0]["reason"]
    overlay_receipt = variant["phone_overlay_receipt"]
    assert overlay_receipt["matcher"] == "failed"
    assert overlay_receipt["placed"] == []
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


def test_subtitled_overlay_grounding_skipped_when_admin_authored_overlays_present(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _overlay_grounding_features()
    )
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))
    grounding_mock = Mock(side_effect=AssertionError("manual overlays must skip grounding"))
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", grounding_mock
    )

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[_overlay_card("card1", "photo1", start_s=0.0, end_s=2.0)],
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    grounding_mock.assert_not_called()
    overlay_receipt = variant["phone_overlay_receipt"]
    assert overlay_receipt == {
        "version": 1,
        "matcher": "manual",
        "face_sampling": "skipped",
        "placed": [],
        "unplaced": [],
        "wishlist": [],
    }
    assert variant["phone_lane_receipt"]["applied"] == ["overlays"]


def test_subtitled_overlay_grounding_compiler_drop_demotes_receipt(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _overlay_grounding_features()
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))

    card = SubtitledOverlayCard(
        id="pip-0",
        media_id="photo1",
        gcs_path="users/u1/plan/item1/pool/photo1.jpg",
        generation="1",
        start_s=0.0,
        end_s=2.0,
    )
    monkeypatch.setattr(
        phone_overlay_grounding_mod,
        "ground_phone_subtitled_overlays",
        _grounding_mock([card]),
    )

    import app.pipeline.phone_subtitled_plan as subtitled_plan_mod

    real_compile = subtitled_plan_mod.compile_phone_subtitled_plan
    call_count = {"n": 0}

    def _flaky_compile(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise SubtitledLaneError(
                "overlays", "overlay geometry rejected", capability="visualBlocks"
            )
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(subtitled_plan_mod, "compile_phone_subtitled_plan", _flaky_compile)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert call_count["n"] == 2
    lane_receipt = variant["phone_lane_receipt"]
    assert lane_receipt["applied"] == []
    assert lane_receipt["dropped"] == [{"lane": "overlays", "reason": "overlay geometry rejected"}]
    overlay_receipt = variant["phone_overlay_receipt"]
    assert overlay_receipt["placed"] == []
    assert overlay_receipt["unplaced"] == [
        {"media_id": "photo1", "label": "photo.jpg", "reason": "compile_dropped"}
    ]
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


def test_subtitled_media_lanes_happy_path(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features(
            "stillImages", "visualVideos", "visualBlocks", "alphaOverlay", "soundEffects"
        ),
    )
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[_overlay_card("card1", "photo1", start_s=0.0, end_s=2.0)],
        sound_effects=[
            _sfx_request_dict("sfx1", "cat1", at_s=1.0),
            _sfx_request_dict("sfx2", "cat2", at_s=3.0),
        ],
        ending_clip=_ending_clip_dict("video1"),
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["overlay_transcript"] == [
        {"text": "Hello", "start_s": 0.0, "end_s": 0.5, "confidence": 1.0},
        {"text": "there.", "start_s": 0.5, "end_s": 1.0, "confidence": 1.0},
    ]
    receipt = variant["phone_lane_receipt"]
    assert receipt["applied"] == ["overlays", "sound_effects", "ending_clip"]
    assert receipt["dropped"] == []
    # One bind call for the overlay (image) pin, one for the ending (video) pin.
    assert len(bind_calls) == 2
    assert job.assembly_plan[PHONE_VISUALS_FIELD]


def test_subtitled_media_lanes_sfx_partial_failure(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", _lanes_features("soundEffects")
    )

    def _resolver(sfx: SubtitledSoundEffect) -> ResolvedSoundEffect:
        if sfx.catalog_id == "bad":
            raise UnsupportedPhonePlan(
                "sound effect is no longer available for phone rendering",
                capability="soundEffects",
            )
        return _fake_resolve_sfx(sfx)

    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _resolver)

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        sound_effects=[
            _sfx_request_dict("s-good", "good", at_s=1.0),
            _sfx_request_dict("s-bad", "bad", at_s=2.0),
        ],
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_lane_receipt"]
    assert receipt["applied"] == ["sound_effects"]
    assert receipt["dropped"] == [
        {
            "lane": "sound_effects",
            "id": "s-bad",
            "reason": "sound effect is no longer available for phone rendering",
        }
    ]


def test_subtitled_media_lanes_compiler_lane_error_is_retried(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features("stillImages", "visualBlocks", "alphaOverlay"),
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))

    import app.pipeline.phone_subtitled_plan as subtitled_plan_mod

    real_compile = subtitled_plan_mod.compile_phone_subtitled_plan
    call_count = {"n": 0}

    def _flaky_compile(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise SubtitledLaneError(
                "overlays", "overlay geometry rejected", capability="visualBlocks"
            )
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(subtitled_plan_mod, "compile_phone_subtitled_plan", _flaky_compile)

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[_overlay_card("card1", "photo1", start_s=0.0, end_s=2.0)],
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_lane_receipt"]
    assert receipt["applied"] == []
    assert receipt["dropped"] == [{"lane": "overlays", "reason": "overlay geometry rejected"}]
    assert call_count["n"] == 2
    # The overlay visual was bound before the compiler dropped its lane; the
    # receipt must not leave it behind as a "referenced" pool asset.
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


def test_subtitled_media_lanes_storage_failure_drops_lane_not_job(monkeypatch):
    """A non-ValueError failure while resolving a lane (a storage hiccup in
    `inspect_library_asset`, an unexpected binder error) drops that lane; it
    never terminalizes the job."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features("stillImages", "visualBlocks", "alphaOverlay", "soundEffects"),
    )

    def _broken_bind(_open_session, *, job_id, pins):  # noqa: ARG001
        raise OSError("bucket unreachable")

    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _broken_bind)

    def _broken_resolver(sfx: SubtitledSoundEffect) -> ResolvedSoundEffect:  # noqa: ARG001
        raise RuntimeError("library asset size changed")

    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _broken_resolver)

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[_overlay_card("card1", "photo1")],
        sound_effects=[_sfx_request_dict("s1", "ding")],
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert receipt["applied"] == []
    assert {d["lane"] for d in receipt["dropped"]} == {"overlays", "sound_effects"}
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


def test_subtitled_media_lanes_drops_overlays_when_still_images_not_verified(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    # Default verified features (see `_job_and_session`) carry neither
    # "stillImages" nor "visualBlocks" -- overlays must be dropped before any
    # bind attempt.
    bind_mock = Mock(side_effect=AssertionError("must not bind an unverified lane"))
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind_mock)

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[_overlay_card("card1", "photo1", start_s=0.0, end_s=2.0)],
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_lane_receipt"]
    assert receipt["applied"] == []
    assert receipt["dropped"] == [
        {"lane": "overlays", "reason": "stillImages not verified on the phone"}
    ]
    bind_mock.assert_not_called()


def test_subtitled_media_lanes_malformed_request_drops_and_continues(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = {"overlays": "not-a-list"}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_lane_receipt"]
    assert receipt["applied"] == []
    assert len(receipt["dropped"]) == 1
    assert receipt["dropped"][0]["lane"] == "request"


def test_subtitled_reaction_beats_happy_path(monkeypatch):
    """KRI-178: beats + a placed closing card win outright -- KRI-176's
    generic grounding never runs, both cards bind in one call, the beat sfx
    resolves, and the pipeline events fire with the creator's own missed
    trigger phrase."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)

    cards = [
        SubtitledOverlayCard(
            id="beat-b0-1",
            media_id="celeb1",
            gcs_path="users/u1/plan/item1/pool/celeb1.jpg",
            generation="1",
            start_s=1.0,
            end_s=3.0,
        ),
        SubtitledOverlayCard(
            id="closing-photo",
            media_id="team1",
            gcs_path="users/u1/plan/item1/pool/team1.jpg",
            generation="1",
            start_s=8.0,
            end_s=10.0,
        ),
    ]
    sfx_reqs = [SubtitledSoundEffect(id="beat-b0-1-sfx", catalog_id="cheer", at_s=1.0)]
    receipt = _basic_beat_receipt(
        placed=[
            {
                "beat_id": "b0",
                "trigger": "he scores",
                "at_s": 1.0,
                "end_s": 3.0,
                "visual_label": "celeb1.jpg",
                "sound_label": "Cheer",
            }
        ],
        unplaced=[{"beat_id": "b1", "trigger": "final whistle", "reason": "never_heard"}],
        closing={"status": "placed", "from_s": 8.0, "visual_label": "team1.jpg", "badge": "none"},
    )
    beat_grounding_mock = _beat_grounding_mock(cards, sfx_reqs, receipt)
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding_mock
    )
    kri176_grounding_mock = Mock(
        side_effect=AssertionError("KRI-176 grounding must not run when beats are active")
    )
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", kri176_grounding_mock
    )

    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "he scores", visual_id="celeb1", sound="cheer")],
        "closing_media": _closing_dict("team1"),
    }

    record_mock = Mock()
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", record_mock)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    beat_grounding_mock.assert_called_once()
    call_kwargs = beat_grounding_mock.call_args.kwargs
    assert call_kwargs["job_id"] == str(job.id)
    assert call_kwargs["beats"] == job.all_candidates["creator_strategy"]["reaction_beats"]
    assert call_kwargs["closing"] == job.all_candidates["creator_strategy"]["closing_media"]
    assert call_kwargs["clip_path"] == "/tmp/c0.mp4"
    kri176_grounding_mock.assert_not_called()

    beat_receipt = variant["phone_beat_receipt"]
    assert beat_receipt["matcher"] == "phrase"
    assert beat_receipt == receipt
    overlay_receipt = variant["phone_overlay_receipt"]
    assert overlay_receipt == {
        "version": 1,
        "matcher": "beats",
        "face_sampling": "skipped",
        "placed": [],
        "unplaced": [],
        "wishlist": [],
    }
    lane_receipt = variant["phone_lane_receipt"]
    assert lane_receipt["applied"] == ["overlays", "sound_effects"]
    assert lane_receipt["dropped"] == []
    assert len(bind_calls) == 1
    assert set(bind_calls[0]) == {"celeb1", "team1"}
    assert job.assembly_plan[PHONE_VISUALS_FIELD]

    beats_events = [
        c for c in record_mock.call_args_list if c.args[:2] == ("phone", "subtitled_reaction_beats")
    ]
    assert len(beats_events) == 1
    beats_event_data = beats_events[0].args[2]
    assert beats_event_data == {
        "placed": 1,
        "unplaced": 1,
        "missed": ["final whistle"],
        "missed_reasons": ["never_heard"],
        "closing": "placed",
    }
    beats_card_events = [
        c
        for c in record_mock.call_args_list
        if c.args[:2] == ("media_overlay", "cards_applied") and c.args[2].get("card_count") == 1
    ]
    assert len(beats_card_events) == 1


def test_subtitled_beats_requested_but_unplaced_falls_through_to_pip_grounding(monkeypatch):
    """KRI-181: a strategy asks for reaction beats, but grounding hears NONE
    of them (no card placed, no closing shot placed) -- `beats_active` stays
    False, so KRI-176's generic transcript-grounded PiP lane must still run
    normally rather than being silently swallowed by the creator's beats
    direction. Proves the `beats_active` computation (~L4897-4900 in
    `generative_build.py`) and the `if beats_active: ... else: ground_phone_
    subtitled_overlays(...)` fork (~L4911-4935) both fall through to the
    real KRI-176 path when beats ground nothing."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))

    beat_receipt = _basic_beat_receipt(
        unplaced=[{"beat_id": "b0", "trigger": "final whistle", "reason": "never_heard"}],
    )
    beat_grounding_mock = _beat_grounding_mock([], [], beat_receipt)
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding_mock
    )

    # A PiP-eligible Visual the generic KRI-176 pass matches from the
    # transcript -- real-shaped grounding result via `_grounding_mock`, same
    # helper the KRI-176-only happy path uses.
    pip_card = SubtitledOverlayCard(
        id="pip-0",
        media_id="photo1",
        gcs_path="users/u1/plan/item1/pool/photo1.jpg",
        generation="1",
        start_s=4.0,
        end_s=6.0,
    )
    kri176_grounding_mock = _grounding_mock([pip_card])
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", kri176_grounding_mock
    )

    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "final whistle", visual_id="team1")],
    }

    record_mock = Mock()
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", record_mock)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    beat_grounding_mock.assert_called_once()
    kri176_grounding_mock.assert_called_once()

    beat_receipt_out = variant["phone_beat_receipt"]
    assert beat_receipt_out["placed"] == []
    assert beat_receipt_out["unplaced"] == [
        {"beat_id": "b0", "trigger": "final whistle", "reason": "never_heard"}
    ]

    # The KRI-176 card reached the compiled overlay lane / persisted receipt
    # -- `_grounding_mock`'s default `matcher="agent"` is what the KRI-176
    # path itself set on the receipt here (not hard-coded/guessed).
    overlay_receipt = variant["phone_overlay_receipt"]
    assert overlay_receipt["matcher"] == "agent"
    assert {c["media_id"] for c in overlay_receipt["placed"]} == {"photo1"}

    lane_receipt = variant["phone_lane_receipt"]
    assert lane_receipt["applied"] == ["overlays"]
    assert lane_receipt["dropped"] == []
    assert len(bind_calls) == 1
    assert set(bind_calls[0]) == {"photo1"}

    beats_events = [
        c for c in record_mock.call_args_list if c.args[:2] == ("phone", "subtitled_reaction_beats")
    ]
    assert len(beats_events) == 1
    assert beats_events[0].args[2]["missed"] == ["final whistle"]
    assert beats_events[0].args[2]["missed_reasons"] == ["never_heard"]


def test_subtitled_beats_and_pip_in_one_prompt_never_double_place(monkeypatch):
    """KRI-181: a prompt combining reaction beats AND (heuristically) PiP-
    worthy footage for the SAME Visual must never place two cards for it.
    The beat grounds one card + one sound for `celeb1` -- since a beat card
    was placed, `beats_active` is True (~L4897-4900) and the KRI-176 generic
    pass -- which could plausibly have matched that very same `celeb1` photo
    from the surrounding transcript sentence -- must never even run
    (`kri176_grounding_mock` raises if called), so there is exactly one card
    for that media id anywhere in the compiled plan and the overlay receipt
    is the fixed `matcher: "beats"` stub (~L4911-4919)."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)

    cards = [
        SubtitledOverlayCard(
            id="beat-b0-1",
            media_id="celeb1",
            gcs_path="users/u1/plan/item1/pool/celeb1.jpg",
            generation="1",
            start_s=1.0,
            end_s=3.0,
        ),
    ]
    sfx_reqs = [SubtitledSoundEffect(id="beat-b0-1-sfx", catalog_id="cheer", at_s=1.0)]
    receipt = _basic_beat_receipt(
        placed=[
            {
                "beat_id": "b0",
                "trigger": "he scores",
                "at_s": 1.0,
                "end_s": 3.0,
                "visual_label": "celeb1.jpg",
                "sound_label": "Cheer",
            }
        ],
    )
    beat_grounding_mock = _beat_grounding_mock(cards, sfx_reqs, receipt)
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding_mock
    )
    # The strategy also carries reaction beats worth of context around the
    # SAME `celeb1` moment a generic PiP-matching pass could plausibly have
    # picked up too (e.g. "he scores" mentions the celebration photo again a
    # few words later) -- if KRI-176 grounding ran here, it could ground a
    # second, duplicate card for `celeb1`. `beats_active` must keep it from
    # ever running at all.
    kri176_grounding_mock = Mock(
        side_effect=AssertionError(
            "KRI-176 grounding must not run when beats are active -- it could "
            "re-match the same Visual as the beat and double-place it"
        )
    )
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", kri176_grounding_mock
    )

    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "he scores", visual_id="celeb1", sound="cheer")],
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    kri176_grounding_mock.assert_not_called()

    overlay_receipt = variant["phone_overlay_receipt"]
    assert overlay_receipt == {
        "version": 1,
        "matcher": "beats",
        "face_sampling": "skipped",
        "placed": [],
        "unplaced": [],
        "wishlist": [],
    }
    assert variant["phone_beat_receipt"] == receipt

    # Exactly one bound visual for `celeb1` -- not two -- and exactly one
    # beat card was ever handed to the compiler for it.
    assert len(bind_calls) == 1
    assert bind_calls[0]["celeb1"] == ("image", "users/u1/plan/item1/pool/celeb1.jpg", "1")
    assert sum(1 for c in cards if c.media_id == "celeb1") == 1


def test_subtitled_reaction_beats_grounding_failure_drops_lane_not_job(monkeypatch):
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    # Keep KRI-176 grounding trivially successful so this test isolates the
    # beats-grounding failure path.
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _grounding_mock([])
    )

    def _broken_grounding(*args, **kwargs):
        raise RuntimeError("beat pool query exploded")

    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", _broken_grounding
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "he scores", visual_id="celeb1")],
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    beat_receipt = variant["phone_beat_receipt"]
    assert beat_receipt["matcher"] == "failed"
    assert beat_receipt["placed"] == []
    assert "error" in beat_receipt
    lane_receipt = variant["phone_lane_receipt"]
    dropped_reasons = [d["reason"] for d in lane_receipt["dropped"] if d["lane"] == "overlays"]
    assert any("beat grounding failed" in reason for reason in dropped_reasons)


def test_subtitled_reaction_beats_sfx_resolve_failure_demotes_sound_only(monkeypatch):
    """A beat sfx that fails `_resolve_phone_sound_effect` is dropped from
    the lane; the beat's CARD still placed, so the receipt entry survives
    with just `sound_label` stripped, not moved to `unplaced`."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    bind_calls: list = []
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind(bind_calls))

    def _broken_resolver(sfx: SubtitledSoundEffect):
        raise UnsupportedPhonePlan(
            "sound effect is no longer available for phone rendering",
            capability="soundEffects",
        )

    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _broken_resolver)

    cards = [
        SubtitledOverlayCard(
            id="beat-b0-1",
            media_id="celeb1",
            gcs_path="users/u1/plan/item1/pool/celeb1.jpg",
            generation="1",
            start_s=1.0,
            end_s=3.0,
        )
    ]
    sfx_reqs = [SubtitledSoundEffect(id="beat-b0-1-sfx", catalog_id="cheer", at_s=1.0)]
    receipt = _basic_beat_receipt(
        placed=[
            {
                "beat_id": "b0",
                "trigger": "he scores",
                "at_s": 1.0,
                "end_s": 3.0,
                "visual_label": "celeb1.jpg",
                "sound_label": "Cheer",
            }
        ],
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "ground_phone_reaction_beats",
        _beat_grounding_mock(cards, sfx_reqs, receipt),
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "he scores", visual_id="celeb1", sound="cheer")],
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    beat_receipt = variant["phone_beat_receipt"]
    assert beat_receipt["placed"] == [
        {
            "beat_id": "b0",
            "trigger": "he scores",
            "at_s": 1.0,
            "end_s": 3.0,
            "visual_label": "celeb1.jpg",
        }
    ]
    assert beat_receipt["unplaced"] == []
    lane_receipt = variant["phone_lane_receipt"]
    assert lane_receipt["applied"] == ["overlays"]
    assert lane_receipt["dropped"] == [
        {
            "lane": "sound_effects",
            "id": "beat-b0-1-sfx",
            "reason": "sound effect is no longer available for phone rendering",
        }
    ]
    assert len(bind_calls) == 1


def test_subtitled_reaction_beats_skipped_when_admin_authored_lane_present(monkeypatch):
    """A hand-authored KRI-174 lane request -- even one carrying only sound
    effects, no overlays -- wins outright over beats: grounding never runs,
    the beat receipt records `matcher: "manual"`."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    beat_grounding_mock = Mock(side_effect=AssertionError("manual lane must skip beat grounding"))
    monkeypatch.setattr(
        phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding_mock
    )

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        sound_effects=[_sfx_request_dict("sfx1", "cat1", at_s=1.0)],
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "he scores", visual_id="celeb1")],
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    beat_grounding_mock.assert_not_called()
    assert variant["phone_beat_receipt"] == {
        "version": 1,
        "matcher": "manual",
        "face_sampling": "skipped",
        "placed": [],
        "unplaced": [],
        "closing": {"status": "none", "badge": "none"},
    }
    # A manual lane also carries `overlays == []` here, so KRI-176's own
    # grounding (not mocked in this test) still runs for the empty overlay
    # set -- unaffected by beats, which is the point being pinned.
    assert variant["phone_overlay_receipt"]["matcher"] != "beats"


def test_subtitled_reaction_beats_compiler_drop_demotes_receipt(monkeypatch):
    """A beat card that survives grounding/binding but gets dropped by the
    compiler's own retry loop (the whole "overlays" lane rejected) must not
    linger in the beat receipt as `placed`."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))

    card = SubtitledOverlayCard(
        id="beat-b0-1",
        media_id="celeb1",
        gcs_path="users/u1/plan/item1/pool/celeb1.jpg",
        generation="1",
        start_s=1.0,
        end_s=3.0,
    )
    receipt = _basic_beat_receipt(
        placed=[
            {
                "beat_id": "b0",
                "trigger": "he scores",
                "at_s": 1.0,
                "end_s": 3.0,
                "visual_label": "celeb1.jpg",
            }
        ],
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "ground_phone_reaction_beats",
        _beat_grounding_mock([card], [], receipt),
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [_beat_dict("b0", "he scores", visual_id="celeb1")],
    }

    import app.pipeline.phone_subtitled_plan as subtitled_plan_mod

    real_compile = subtitled_plan_mod.compile_phone_subtitled_plan
    call_count = {"n": 0}

    def _flaky_compile(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise SubtitledLaneError(
                "overlays", "overlay geometry rejected", capability="visualBlocks"
            )
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(subtitled_plan_mod, "compile_phone_subtitled_plan", _flaky_compile)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert call_count["n"] == 2
    beat_receipt = variant["phone_beat_receipt"]
    assert beat_receipt["placed"] == []
    assert beat_receipt["unplaced"] == [
        {"beat_id": "b0", "trigger": "he scores", "reason": "compile_dropped"}
    ]
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


# --- KRI-174: _resolve_phone_sound_effect -----------------------------------


def _sfx_row(
    *,
    status: str = "ready",
    published: bool = True,
    archived: bool = False,
    path: str = "sound-effects/cat1/click.m4a",
    duration_s: float | None = 2.0,
):
    return SimpleNamespace(
        status=status,
        published_at=datetime.now(UTC) if published else None,
        archived_at=datetime.now(UTC) if archived else None,
        audio_gcs_path=path,
        duration_s=duration_s,
    )


def _sfx_session(monkeypatch, row):
    session = Mock()
    session.get = Mock(return_value=row)

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    return session


def test_resolve_phone_sound_effect_ready_returns_resolved(monkeypatch):
    _sfx_session(monkeypatch, _sfx_row(path="sound-effects/cat1/click.m4a"))
    import app.services.render_library as render_library_mod

    monkeypatch.setattr(
        render_library_mod,
        "inspect_library_asset",
        lambda path, *, asset_id, catalog, catalog_id: LibraryRenderAsset(  # noqa: ARG005
            id=asset_id,
            catalog=catalog,
            catalog_id=catalog_id,
            generation="7",
            fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=123),
        ),
    )
    sfx = SubtitledSoundEffect(id="s1", catalog_id="cat1", at_s=1.0)
    resolved = gb._resolve_phone_sound_effect(sfx)
    assert resolved.asset.generation == "7"
    assert resolved.duration_s == 2.0
    assert resolved.request is sfx


def test_resolve_phone_sound_effect_rejects_unplayable_format(monkeypatch):
    _sfx_session(monkeypatch, _sfx_row(path="sound-effects/cat1/click.ogg"))
    sfx = SubtitledSoundEffect(id="s1", catalog_id="cat1", at_s=1.0)
    with pytest.raises(UnsupportedPhonePlan, match="format"):
        gb._resolve_phone_sound_effect(sfx)


def test_resolve_phone_sound_effect_rejects_archived(monkeypatch):
    _sfx_session(monkeypatch, _sfx_row(path="sound-effects/cat1/click.m4a", archived=True))
    sfx = SubtitledSoundEffect(id="s1", catalog_id="cat1", at_s=1.0)
    with pytest.raises(UnsupportedPhonePlan, match="no longer available"):
        gb._resolve_phone_sound_effect(sfx)


def test_resolve_phone_sound_effect_rejects_wrong_prefix(monkeypatch):
    _sfx_session(monkeypatch, _sfx_row(path="sound-effects/other-catalog/click.m4a"))
    sfx = SubtitledSoundEffect(id="s1", catalog_id="cat1", at_s=1.0)
    with pytest.raises(ValueError, match="invalid render catalog path"):
        gb._resolve_phone_sound_effect(sfx)


# --- narrated (WITH a recorded voiceover) -----------------------------------


def _fake_narration_bed(_job_id, voiceover_gcs_path):
    if not voiceover_gcs_path:
        return None
    from app.pipeline.phone_recipe_shared import PhoneNarrationBed

    return PhoneNarrationBed(
        plan_item_id="item-1",
        generation="9",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=999),
        duration_s=12.0,
    )


def _setup_narrated(monkeypatch, *, edit_format="narrated_ready", filming_guide=None):
    bindings = tuple(_binding(f"c{i}", duration_s=10.0) for i in range(3))
    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
    }
    all_candidates = {
        "clip_paths": [f"phone-proxies/c{i}.mp4" for i in range(3)],
        "edit_format": edit_format,
        "voiceover_gcs_path": "voiceover-uploads/direct/u/i/voice.m4a",
        "filming_guide": filming_guide or [],
        "language": "en",
    }
    job, session = _job_and_session(
        monkeypatch, assembly_plan=snapshot, all_candidates=all_candidates
    )

    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta(f"c{i}", 5.0) for i in range(3)],
            "clip_id_to_gcs": {f"c{i}": bindings[i].proxy_path for i in range(3)},
            "clip_id_to_local": {f"c{i}": f"/tmp/c{i}.mp4" for i in range(3)},
            "probe_map": {},
            "hero": _Meta("c0", 5.0),
        },
        raising=False,
    )
    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", _fake_narration_bed, raising=False)

    import app.pipeline.phrase_sequence as phrase_mod
    import app.pipeline.transcribe as transcribe_mod
    import app.storage as storage_mod
    import app.tasks.template_orchestrate as to

    words = _words(
        ("First", 0.0, 0.5),
        ("clip.", 0.5, 1.0),
        ("Second", 4.0, 4.5),
        ("clip.", 4.5, 5.0),
        ("Third", 8.0, 8.5),
        ("clip.", 8.5, 9.0),
    )
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper",
        lambda *a, **k: Transcript(words=words, language="en"),
        raising=False,
    )
    monkeypatch.setattr(storage_mod, "download_to_file", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(to, "_probe_duration", lambda *a, **k: 12.0, raising=False)
    monkeypatch.setattr(
        phrase_mod,
        "split_phrases",
        lambda *a, **k: [
            {"speech_start_s": 0.0, "speech_end_s": 4.0},
            {"speech_start_s": 4.0, "speech_end_s": 8.0},
            {"speech_start_s": 8.0, "speech_end_s": 12.0},
        ],
        raising=False,
    )
    return job, snapshot, session, bindings


def test_narrated_ready_compiles_and_pins_device_request(monkeypatch):
    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    status = device_status(job, "narrated")
    assert status.request.identity.variant_id == "narrated"
    assert status.request.recipe.duration == pytest.approx(12.0)
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == "narrated"
    assert variant["resolved_archetype"] == "narrated"
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"
    assert variant["caption_cues"]
    video_track = next(t for t in status.request.recipe.tracks if t.kind == "video")
    assert [clip.timeline_start for clip in video_track.clips] == [0.0, 4.0, 8.0]


def test_narrated_scripted_two_plus_steps_uses_force_alignment(monkeypatch):
    filming_guide = [
        {"shot_id": "s0", "what": "First clip narration"},
        {"shot_id": "s1", "what": "Second clip narration"},
        {"shot_id": "s2", "what": "Third clip narration"},
    ]
    job, _snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, edit_format="narrated_planned", filming_guide=filming_guide
    )

    import app.pipeline.narrated_alignment as alignment_mod

    monkeypatch.setattr(
        alignment_mod,
        "align_script_to_voiceover",
        lambda steps, words: [
            alignment_mod.StepTiming(
                step_id=s.step_id, start_s=i * 4.0, end_s=(i + 1) * 4.0, confidence=1.0
            )
            for i, s in enumerate(steps)
        ],
        raising=False,
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"


def test_narrated_rejects_when_voiceover_flag_off(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_narration_rendering_enabled", False)
    with pytest.raises(ValueError, match="voiceover"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_narrated_rejects_without_a_voiceover(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    candidates = {**job.all_candidates, "voiceover_gcs_path": None}
    with pytest.raises(ValueError, match="recorded voiceover"):
        gb._run_phone_narrated_job(str(job.id), snapshot, candidates, ownership_epoch=3)


def test_narrated_fails_closed_on_required_speech_cleanup_contract(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    snapshot["speech_cleanup_contract"] = "required_v1"
    with pytest.raises(UnsupportedPhonePlan, match="speech-cleanup"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)


def test_narrated_bed_level_mix_math_matches_montage_convention(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    candidates = {**job.all_candidates, "voiceover_bed_level": 0.4}
    gb._run_phone_narrated_job(str(job.id), snapshot, candidates, ownership_epoch=3)
    status = device_status(job, "narrated")
    assert status.request.recipe.audio.original_volume == pytest.approx(0.4)


def test_narrated_worker_rejects_format_it_does_not_own(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch, edit_format="montage")
    with pytest.raises(ValueError, match="No phone renderer is registered"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
