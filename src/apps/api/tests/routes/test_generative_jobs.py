"""Request-validation tests for the generative-job routes.

Pydantic field validators are tested directly (no DB / TestClient needed). The clip
prefix allowlist is the security-relevant one — an arbitrary bucket key must be rejected
before any download.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from app.routes.generative_jobs import (
    ChangeStyleRequest,
    CreateGenerativeJobRequest,
    RetextRequest,
    SwapSongRequest,
    list_generative_style_sets,
)


def test_valid_request():
    req = CreateGenerativeJobRequest(
        clip_gcs_paths=["music-uploads/a.mp4", "slot-uploads/b.mp4"],
    )
    assert len(req.clip_gcs_paths) == 2


def test_target_duration_field_removed():
    # The slider is gone: output length is derived from footage, never user-set.
    # A stale frontend that still posts the field must be tolerated (ignored),
    # not rejected.
    assert "target_duration_s" not in CreateGenerativeJobRequest.model_fields
    req = CreateGenerativeJobRequest(
        clip_gcs_paths=["music-uploads/a.mp4"],
        target_duration_s=120.0,  # extra field — silently ignored
    )
    assert not hasattr(req, "target_duration_s")


def test_rejects_empty_clips():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=[])


def test_rejects_too_many_clips():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/x.mp4"] * 21)


def test_rejects_arbitrary_bucket_prefix():
    # Security: only upload-endpoint prefixes allowed.
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["processed-outputs/secret.mp4"])


def test_rejects_path_traversal():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/../../etc/passwd"])


def test_language_defaults_to_en():
    # User selects language at job creation; the model remembers it so re-renders
    # (retext/swap_song/change_style) inherit it without the frontend re-passing.
    req = CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/a.mp4"])
    assert req.language == "en"


def test_language_accepts_tr():
    req = CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/a.mp4"], language="tr")
    assert req.language == "tr"


def test_language_rejects_unsupported_code():
    # Closed allowlist (Literal["en","tr"]) — adding a new language requires
    # corresponding render-side glyph coverage and TR-style prompt branches,
    # so Pydantic must reject unknowns at the edge.
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/a.mp4"], language="de")


def test_swap_song_request():
    assert SwapSongRequest(new_track_id="t1").new_track_id == "t1"


def test_retext_request_defaults():
    r = RetextRequest()
    assert r.text is None
    assert r.remove is False


def test_change_style_request():
    assert ChangeStyleRequest(style_set_id="travel_editorial").style_set_id == "travel_editorial"


def test_change_style_validation_source_excludes_music_only_sets():
    # The endpoint rejects (422) any id not in this set. Generative-eligible only:
    # a music-only lyric set must NOT be acceptable; a generative set must be.
    from app.pipeline.style_sets import style_set_ids

    ids = set(style_set_ids(applies_to="generative"))
    assert "travel_editorial" in ids
    assert "default" in ids
    assert "lyric_karaoke_bold" not in ids  # music-only


@pytest.mark.asyncio
async def test_list_style_sets_endpoint_returns_generative_only():
    resp = await list_generative_style_sets()
    ids = {s.id for s in resp.style_sets}
    assert "travel_editorial" in ids
    assert "lyric_line_calm" not in ids  # music-only set is filtered out
    assert all(s.label and isinstance(s.tags, list) for s in resp.style_sets)


@pytest.mark.asyncio
async def test_list_style_sets_endpoint_includes_real_typography():
    """Each set projects its representative-role typography (font + colors) so the
    plan picker can render a real-font preview chip before a re-render. The fields
    are display-only and must resolve to a real registry font (css_family set)."""
    resp = await list_generative_style_sets()
    by_id = {s.id: s for s in resp.style_sets}
    default = by_id["default"]
    # font_family resolves to a registry entry → css_family + a colour are present.
    assert default.font_family  # e.g. "Playfair Display"
    assert default.css_family and "'" in default.css_family  # e.g. "'Playfair Display', serif"
    assert default.text_color and default.text_color.startswith("#")
    # Every generative set should carry a usable css_family for its chip.
    assert all(s.css_family for s in resp.style_sets)


# ── Voiceover request validation + mix dispatch ─────────────────────────────────


def test_voiceover_path_accepted():
    req = CreateGenerativeJobRequest(
        clip_gcs_paths=["slot-uploads/b.mp4"],
        voiceover_gcs_path="voiceover-uploads/abc/voice.webm",
    )
    assert req.voiceover_gcs_path == "voiceover-uploads/abc/voice.webm"


def test_voiceover_path_defaults_none():
    req = CreateGenerativeJobRequest(clip_gcs_paths=["slot-uploads/b.mp4"])
    assert req.voiceover_gcs_path is None


def test_voiceover_path_rejects_clip_prefix():
    # A footage-clip prefix must NOT be accepted as a voiceover (no cross-contamination).
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(
            clip_gcs_paths=["slot-uploads/b.mp4"], voiceover_gcs_path="slot-uploads/voice.mp3"
        )


def test_voiceover_path_rejects_traversal():
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(
            clip_gcs_paths=["slot-uploads/b.mp4"],
            voiceover_gcs_path="voiceover-uploads/../etc/passwd",
        )


def test_clip_allowlist_rejects_voiceover_prefix():
    # The inverse guard: a voiceover path must NOT be usable as a footage clip.
    with pytest.raises(ValidationError):
        CreateGenerativeJobRequest(clip_gcs_paths=["voiceover-uploads/abc/voice.webm"])


def _voiceover_job():
    import types
    import uuid

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {"variant_id": "voiceover_only", "render_status": "ready", "mix": 1.0},
                {
                    "variant_id": "voiceover_music",
                    "render_status": "ready",
                    "mix": 0.7,
                    "music_track_id": "t1",
                },
            ]
        },
    )


def test_dispatch_set_mix_enqueues_override(monkeypatch):
    import types

    import app.tasks.generative_build as gb
    from app.routes.generative_jobs import dispatch_set_mix

    calls: list = []
    fake = types.SimpleNamespace(delay=lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(gb, "regenerate_generative_variant", fake, raising=False)

    dispatch_set_mix(_voiceover_job(), "voiceover_only", mix=0.4)
    assert len(calls) == 1
    assert calls[0][1]["mix_override"] == 0.4


def test_dispatch_set_mix_rejects_non_voiceover_variant(monkeypatch):
    import types
    import uuid

    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_set_mix

    job = types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"variants": [{"variant_id": "song_text", "render_status": "ready"}]},
    )
    with pytest.raises(HTTPException) as exc:
        dispatch_set_mix(job, "song_text", mix=0.5)
    assert exc.value.status_code == 422


def test_build_generative_job_stores_voiceover_path():
    import uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["slot-uploads/b.mp4"],
        voiceover_gcs_path="voiceover-uploads/abc/voice.webm",
    )
    assert job.all_candidates["voiceover_gcs_path"] == "voiceover-uploads/abc/voice.webm"


def test_build_generative_job_omits_voiceover_when_absent():
    import uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/b.mp4"])
    assert "voiceover_gcs_path" not in job.all_candidates


def test_build_generative_job_rejects_bad_voiceover_path():
    import uuid

    from app.services.generative_jobs import build_generative_job

    with pytest.raises(ValueError):
        build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=["slot-uploads/b.mp4"],
            voiceover_gcs_path="processed-outputs/voice.mp3",
        )


# ── Playback URL re-signing (expired-signature bug) ─────────────────────────────
# generative-jobs/ blobs persist forever but `output_url` is signed for 1 day at
# render time, so a >24h-old "ready" item served the stale signature → 400
# ExpiredToken → empty <video>. `_variants_for_response` must re-sign on read from
# the persisted `video_path` key. See PLAYBACK_URL_TTL_MIN.


def _resign_job():
    import types
    import uuid

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_lyrics",
                    "render_status": "ready",
                    "video_path": "generative-jobs/j/variant_1_song_lyrics.mp4",
                    "output_url": "https://stale.example/expired?X-Goog-Expires=86400",
                    "ok": True,
                },
                {
                    "variant_id": "original_text",
                    "render_status": "failed",
                    "output_url": None,
                    "ok": False,
                },
            ]
        },
    )


def test_variants_for_response_resigns_ready_variant(monkeypatch):
    import app.routes.generative_jobs as gj

    calls: list = []

    def fake_sign(path, ttl):
        calls.append((path, ttl))
        return f"https://fresh.example/{path}?sig=new"

    monkeypatch.setattr(gj, "signed_get_url", fake_sign)

    out = gj._variants_for_response(_resign_job())

    # Ready variant gets a fresh URL signed from its video_path, not the stale stored one.
    ready = next(v for v in out if v["variant_id"] == "song_lyrics")
    assert ready["output_url"] == (
        "https://fresh.example/generative-jobs/j/variant_1_song_lyrics.mp4?sig=new"
    )
    assert calls == [("generative-jobs/j/variant_1_song_lyrics.mp4", gj.PLAYBACK_URL_TTL_MIN)]

    # Failed variant (no video_path) keeps its null URL and is not re-signed.
    failed = next(v for v in out if v["variant_id"] == "original_text")
    assert failed["output_url"] is None


def test_variants_for_response_does_not_mutate_stored_dicts(monkeypatch):
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: "https://fresh.example/x")
    job = _resign_job()
    gj._variants_for_response(job)

    # The raw assembly_plan variant (read by the mutate endpoints) must be untouched —
    # we never want a short-lived re-signed URL written back to the DB.
    stored = job.assembly_plan["variants"][0]
    assert stored["output_url"] == "https://stale.example/expired?X-Goog-Expires=86400"


def test_variants_for_response_signing_failure_falls_back(monkeypatch):
    import app.routes.generative_jobs as gj

    def boom(path, ttl):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(gj, "signed_get_url", boom)
    out = gj._variants_for_response(_resign_job())

    # A signing failure must not 500 the poll — fall back to the stored URL.
    ready = next(v for v in out if v["variant_id"] == "song_lyrics")
    assert ready["output_url"] == "https://stale.example/expired?X-Goog-Expires=86400"


# ── Phase tracking fields (PR2) ─────────────────────────────────────────────────


def _phase_job(current_phase="analyze_clips", phase_log=None, started_at=None):
    """Build a minimal fake job with phase tracking fields set."""
    import types
    import uuid
    from datetime import datetime

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        status="processing",
        mode="generative",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "pending",
                    "ok": False,
                }
            ]
        },
        error_detail=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        all_candidates={"edit_format": "montage"},
        current_phase=current_phase,
        phase_log=phase_log or [{"name": "analyze_clips", "ts": "2026-06-06T10:00:00+00:00"}],
        started_at=started_at or datetime.now(UTC),
        finished_at=None,
    )


def test_status_includes_phase_fields(monkeypatch):
    """Status response must include current_phase, phase_log, started_at,
    and expected_phase_durations when a job has phase instrumentation."""
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://fresh/{path}")

    job = _phase_job()
    resp = gj.GenerativeJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        variants=gj._variants_for_response(job),
        error_detail=job.error_detail,
        created_at=job.created_at,
        updated_at=job.updated_at,
        edit_format=(job.all_candidates or {}).get("edit_format"),
        current_phase=job.current_phase,
        phase_log=list(job.phase_log or []),
        started_at=job.started_at,
        finished_at=job.finished_at,
        expected_phase_durations={"analyze_clips": 45000, "match_song": 15000},
    )
    assert resp.current_phase == "analyze_clips"
    assert isinstance(resp.phase_log, list)
    assert len(resp.phase_log) == 1
    assert resp.started_at is not None
    assert resp.expected_phase_durations is not None
    assert resp.expected_phase_durations["analyze_clips"] == 45000


def test_status_phase_fields_null_for_uninstrumented_job(monkeypatch):
    """A job without phase data must return nulls — no crash."""
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://fresh/{path}")

    import types
    import uuid
    from datetime import datetime

    bare_job = types.SimpleNamespace(
        id=uuid.uuid4(),
        status="processing",
        mode="generative",
        assembly_plan={"variants": []},
        error_detail=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        all_candidates={},
        current_phase=None,
        phase_log=None,
        started_at=None,
        finished_at=None,
    )
    resp = gj.GenerativeJobStatusResponse(
        job_id=str(bare_job.id),
        status=bare_job.status,
        variants=[],
        error_detail=None,
        created_at=bare_job.created_at,
        updated_at=bare_job.updated_at,
        current_phase=None,
        phase_log=None,
        started_at=None,
        finished_at=None,
        expected_phase_durations=None,
    )
    assert resp.current_phase is None
    assert resp.phase_log is None
    assert resp.started_at is None
    assert resp.expected_phase_durations is None


def test_variant_includes_error_class():
    """A variant dict with error_class passes through _variants_for_response unchanged."""
    import types
    import uuid

    import app.routes.generative_jobs as gj

    job = types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "failed",
                    "ok": False,
                    "error": "timed out",
                    "error_class": "timeout",
                }
            ]
        },
    )
    out = gj._variants_for_response(job)
    assert out[0]["error_class"] == "timeout"


def test_variant_error_class_fallback():
    """A variant with raw `error` but no `error_class` key returns None for error_class
    (the frontend uses generic copy for unknown error class)."""
    import types
    import uuid

    import app.routes.generative_jobs as gj

    job = types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "failed",
                    "ok": False,
                    "error": "some old error with no classification",
                    # no error_class key
                }
            ]
        },
    )
    out = gj._variants_for_response(job)
    # No error_class key → .get returns None, which is the correct fallback signal.
    assert out[0].get("error_class") is None


# ── Phase baseline scaling ────────────────────────────────────────────────────────


def test_phase_baselines_scale_render_variants():
    """scale_render_variants adjusts the render_variants baseline by variant count."""
    from app.services.phase_baselines import get_baselines, scale_render_variants

    baselines = get_baselines("generative")
    assert baselines is not None
    # 3 pending → unscaled (3/3 = 1.0)
    scaled_3 = scale_render_variants(baselines, 3)
    assert scaled_3["render_variants"] == baselines["render_variants"]
    # 1 pending → 1/3 of the base
    scaled_1 = scale_render_variants(baselines, 1)
    assert scaled_1["render_variants"] == baselines["render_variants"] // 3
    # Other keys untouched
    assert scaled_1["analyze_clips"] == baselines["analyze_clips"]


def test_phase_baselines_unknown_pipeline_returns_none():
    from app.services.phase_baselines import get_baselines

    assert get_baselines("template") is None
    assert get_baselines("music") is None


# ── Instant edit: base_video_url re-signing ──────────────────────────────────────
# The instant editor plays the text-free fast-reburn base under a client-side DOM
# overlay. `base_video_path` is a persisted GCS key; `_variants_for_response` must
# mint a fresh `base_video_url` on every read (mirrors output_url re-signing).


def _base_video_job():
    import types
    import uuid

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/j/variant_1_song_text.mp4",
                    "base_video_path": "generative-jobs/j/base_1_song_text.mp4",
                    "output_url": "https://stale.example/expired",
                    "ok": True,
                },
                {
                    "variant_id": "song_lyrics",
                    "render_status": "ready",
                    "video_path": "generative-jobs/j/variant_2_song_lyrics.mp4",
                    "output_url": "https://stale.example/expired2",
                    "ok": True,
                },
                {
                    "variant_id": "original_text",
                    "render_status": "rendering",
                    "video_path": "generative-jobs/j/variant_3_original_text.mp4",
                    "base_video_path": "generative-jobs/j/base_3_original_text.mp4",
                    "output_url": None,
                    "ok": None,
                },
            ]
        },
    )


def test_variants_for_response_signs_base_video_url(monkeypatch):
    import app.routes.generative_jobs as gj

    calls: list = []

    def fake_sign(path, ttl):
        calls.append((path, ttl))
        return f"https://fresh.example/{path}?sig=new"

    monkeypatch.setattr(gj, "signed_get_url", fake_sign)
    out = gj._variants_for_response(_base_video_job())

    ready = next(v for v in out if v["variant_id"] == "song_text")
    assert ready["base_video_url"] == (
        "https://fresh.example/generative-jobs/j/base_1_song_text.mp4?sig=new"
    )
    assert ("generative-jobs/j/base_1_song_text.mp4", gj.PLAYBACK_URL_TTL_MIN) in calls


def test_variants_for_response_base_url_present_while_rendering(monkeypatch):
    """The editor keeps playing the base during a committed re-render, so the
    base URL must be signed even when render_status != ready (output_url is not)."""
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: f"https://fresh.example/{p}")
    out = gj._variants_for_response(_base_video_job())

    rendering = next(v for v in out if v["variant_id"] == "original_text")
    assert rendering["base_video_url"] == (
        "https://fresh.example/generative-jobs/j/base_3_original_text.mp4"
    )
    # output_url is NOT re-signed for a non-ready variant.
    assert rendering["output_url"] is None


def test_variants_for_response_no_base_no_url(monkeypatch):
    """Lyrics/legacy variants without a base key must not grow a base_video_url."""
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: f"https://fresh.example/{p}")
    out = gj._variants_for_response(_base_video_job())

    lyrics = next(v for v in out if v["variant_id"] == "song_lyrics")
    assert "base_video_url" not in lyrics


def test_variants_for_response_base_sign_failure_omits_url(monkeypatch):
    """A base-signing failure must not 500 the poll NOR break output_url signing."""
    import app.routes.generative_jobs as gj

    def sign(path, ttl):
        if "base_" in path:
            raise RuntimeError("no credentials")
        return f"https://fresh.example/{path}"

    monkeypatch.setattr(gj, "signed_get_url", sign)
    out = gj._variants_for_response(_base_video_job())

    ready = next(v for v in out if v["variant_id"] == "song_text")
    assert "base_video_url" not in ready
    assert ready["output_url"] == "https://fresh.example/generative-jobs/j/variant_1_song_text.mp4"


def test_variants_for_response_base_does_not_mutate_stored_dicts(monkeypatch):
    import app.routes.generative_jobs as gj

    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: "https://fresh.example/x")
    job = _base_video_job()
    gj._variants_for_response(job)

    stored = job.assembly_plan["variants"][0]
    assert "base_video_url" not in stored


# ── Instant edit: combined /edit dispatch ────────────────────────────────────────
# One commit → ONE regenerate_generative_variant enqueue carrying all overrides.


def _editable_job(text_mode="agent_text", render_status="ready"):
    import types
    import uuid

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": render_status,
                    "text_mode": text_mode,
                }
            ]
        },
    )


def _capture_delay(monkeypatch):
    import types

    import app.tasks.generative_build as gb

    calls: list = []
    fake = types.SimpleNamespace(delay=lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(gb, "regenerate_generative_variant", fake, raising=False)
    return calls


def test_dispatch_edit_enqueues_combined_kwargs(monkeypatch):
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _editable_job(),
        "song_text",
        text="  new hook  ",
        remove_text=False,
        style_set_id="travel_editorial",
        text_size_px=64,
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["override_text"] == "new hook"
    assert kwargs["remove_text"] is False
    assert kwargs["style_set_id"] == "travel_editorial"
    assert kwargs["size_override_px"] == 64


def test_dispatch_edit_rejects_empty_payload(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(),
            "song_text",
            text=None,
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
        )
    assert exc.value.status_code == 422
    assert calls == []


def test_dispatch_edit_rejects_text_and_remove(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(),
            "song_text",
            text="hi",
            remove_text=True,
            style_set_id=None,
            text_size_px=None,
        )
    assert exc.value.status_code == 422
    assert calls == []


def test_dispatch_edit_rejects_blank_text(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(),
            "song_text",
            text="   ",
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
        )
    assert exc.value.status_code == 422
    assert calls == []


def test_dispatch_edit_rejects_unknown_style_set(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(),
            "song_text",
            text=None,
            remove_text=False,
            style_set_id="lyric_karaoke_bold",
            text_size_px=None,
        )
    assert exc.value.status_code == 422
    assert calls == []


def test_dispatch_edit_size_only_rejected_on_non_text_variant(monkeypatch):
    """Size without text on a lyrics variant → 422 (same rule as /intro-size)."""
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(text_mode="lyrics"),
            "song_text",
            text=None,
            remove_text=False,
            style_set_id=None,
            text_size_px=60,
        )
    assert exc.value.status_code == 422
    assert calls == []


def test_dispatch_edit_size_with_new_text_allowed_on_none_variant(monkeypatch):
    """A text-removed variant gains a resizable overlay when the edit adds text."""
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _editable_job(text_mode="none"),
        "song_text",
        text="fresh text",
        remove_text=False,
        style_set_id=None,
        text_size_px=60,
    )

    assert len(calls) == 1
    assert calls[0][1]["override_text"] == "fresh text"
    assert calls[0][1]["size_override_px"] == 60


def test_dispatch_edit_clamps_size(monkeypatch):
    from app.pipeline.overlay_sizing import MAX_INTRO_PX
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _editable_job(),
        "song_text",
        text=None,
        remove_text=False,
        style_set_id=None,
        text_size_px=500,
    )

    assert calls[0][1]["size_override_px"] == MAX_INTRO_PX


def test_dispatch_edit_409_while_rendering(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(render_status="rendering"),
            "song_text",
            text="hi",
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
        )
    assert exc.value.status_code == 409
    assert calls == []


def test_dispatch_edit_remove_text_only(monkeypatch):
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _editable_job(),
        "song_text",
        text=None,
        remove_text=True,
        style_set_id=None,
        text_size_px=None,
    )

    assert len(calls) == 1
    assert calls[0][1]["override_text"] is None
    assert calls[0][1]["remove_text"] is True


# ── Instant edit: style-set intro preview block ──────────────────────────────────


@pytest.mark.asyncio
async def test_list_style_sets_includes_intro_preview_block():
    """Every generative set carries the full intro-role look for the client preview
    (anchor + x-frac drive left-anchored sets like word_reveal)."""
    resp = await list_generative_style_sets()
    by_id = {s.id: s for s in resp.style_sets}

    word_reveal = by_id["word_reveal"]
    assert word_reveal.intro is not None
    assert word_reveal.intro.text_anchor == "left"
    assert word_reveal.intro.position_x_frac == 0.06

    # Every set resolves an intro block with effect + font (fallback chain covers
    # sets without an explicit intro role).
    for s in resp.style_sets:
        assert s.intro is not None
        assert s.intro.effect
        assert s.intro.font_family
        assert s.intro.css_family


def test_style_set_intro_preview_unknown_id_falls_back():
    from app.pipeline.style_sets import style_set_intro_preview

    block = style_set_intro_preview("no-such-set")
    assert block["font_family"]  # default set's intro role
    assert block["effect"]


def test_dispatch_edit_size_with_text_still_rejected_on_lyrics_variant(monkeypatch):
    """Lyrics typography is set-driven — a size override would silently drop
    downstream, so reject it even when the edit also carries text."""
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _editable_job(text_mode="lyrics"),
            "song_text",
            text="new line",
            remove_text=False,
            style_set_id=None,
            text_size_px=60,
        )
    assert exc.value.status_code == 422
    assert calls == []


# ── dispatch_edit_variant: intro_layout (post-render layout pick) ───────────────


def _layout_job(text_mode="agent_text", intro_text="what's your favorite place?"):
    import types
    import uuid

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "text_mode": text_mode,
                    "intro_text": intro_text,
                }
            ]
        },
    )


def test_dispatch_edit_layout_cluster_enqueues_override(monkeypatch):
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _layout_job(),
        "song_text",
        text=None,
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
        intro_layout="cluster",
    )
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["layout_override"] == "cluster"


def test_dispatch_edit_layout_alone_is_a_valid_edit(monkeypatch):
    # intro_layout must count as "at least one edit field".
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _layout_job(),
        "song_text",
        text=None,
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
        intro_layout="linear",
    )
    assert len(calls) == 1


def test_dispatch_edit_layout_unknown_value_422(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    _capture_delay(monkeypatch)
    import pytest

    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _layout_job(),
            "song_text",
            text=None,
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
            intro_layout="diagonal",
        )
    assert exc.value.status_code == 422


def test_dispatch_edit_layout_cluster_rejects_wordy_text(monkeypatch):
    # Persisted hook has 8 words → cluster can't lay it out; reject with an
    # actionable message instead of silently rendering linear.
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    _capture_delay(monkeypatch)
    import pytest

    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _layout_job(intro_text="when they don't even listen to your feelings"),
            "song_text",
            text=None,
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
            intro_layout="cluster",
        )
    assert exc.value.status_code == 422
    assert "3-6 word" in exc.value.detail


def test_dispatch_edit_layout_cluster_validates_override_text(monkeypatch):
    # A new short text in the SAME edit makes cluster valid even when the
    # persisted text is wordy.
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _layout_job(intro_text="when they don't even listen to your feelings"),
        "song_text",
        text="the quiet side of bangkok",
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
        intro_layout="cluster",
    )
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["layout_override"] == "cluster"
    assert kwargs["override_text"] == "the quiet side of bangkok"


def test_dispatch_edit_layout_rejected_on_lyrics_variant(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    _capture_delay(monkeypatch)
    import pytest

    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _layout_job(text_mode="lyrics"),
            "song_text",
            text=None,
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
            intro_layout="cluster",
        )
    assert exc.value.status_code == 422


def test_dispatch_edit_layout_rejected_with_remove_text(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    _capture_delay(monkeypatch)
    import pytest

    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _layout_job(),
            "song_text",
            text=None,
            remove_text=True,
            style_set_id=None,
            text_size_px=None,
            intro_layout="cluster",
        )
    assert exc.value.status_code == 422


# ── Transcript-synced sequence variants (intro_mode="sequence", D19/T4) ──────────


def _sequence_job(intro_mode="sequence", transcript=None, intro_text=None):
    import types
    import uuid

    variant = {
        "variant_id": "original_text",
        "render_status": "ready",
        "text_mode": "agent_text",
        "intro_mode": intro_mode,
        # 8 words — would FAIL the 3-6 cluster gate if it applied.
        "intro_text": intro_text or "what a magic day this truly was indeed",
    }
    if transcript is not None:
        variant["transcript"] = transcript
    return types.SimpleNamespace(id=uuid.uuid4(), assembly_plan={"variants": [variant]})


def test_dispatch_edit_layout_cluster_bypasses_word_gate_on_sequence_variant(monkeypatch):
    """A synced variant renders the editorial treatment from the SPOKEN words —
    the intro_text word-count gate must not block the layout pick."""
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _sequence_job(),
        "original_text",
        text=None,
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
        intro_layout="cluster",
    )
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["layout_override"] == "cluster"


def test_dispatch_edit_layout_cluster_bypasses_word_gate_with_persisted_transcript(monkeypatch):
    """A variant that still carries a persisted transcript (e.g. rendered synced
    before) bypasses the gate even when its current intro_mode isn't sequence."""
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    job = _sequence_job(
        intro_mode="cluster",
        transcript=[{"word": "we", "start_s": 0.5, "end_s": 0.7}],
    )
    dispatch_edit_variant(
        job,
        "original_text",
        text=None,
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
        intro_layout="cluster",
    )
    assert len(calls) == 1


def test_dispatch_edit_layout_cluster_gate_still_applies_without_sequence(monkeypatch):
    """Non-synced, no-transcript variant keeps the actionable 3-6-word 422."""
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _sequence_job(intro_mode="cluster"),  # 8-word hook, no transcript
            "original_text",
            text=None,
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
            intro_layout="cluster",
        )
    assert exc.value.status_code == 422
    assert calls == []


def test_dispatch_edit_text_rejected_on_sequence_variant(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _sequence_job(),
            "original_text",
            text="new hook",
            remove_text=False,
            style_set_id=None,
            text_size_px=None,
        )
    assert exc.value.status_code == 422
    assert "synced" in str(exc.value.detail)
    assert "Classic" in str(exc.value.detail)
    assert calls == []


def test_dispatch_edit_remove_text_rejected_on_sequence_variant(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        dispatch_edit_variant(
            _sequence_job(),
            "original_text",
            text=None,
            remove_text=True,
            style_set_id=None,
            text_size_px=None,
        )
    assert exc.value.status_code == 422
    assert "synced" in str(exc.value.detail)
    assert calls == []


def test_dispatch_retext_allowed_on_sequence_variant(monkeypatch):
    """T8: sequence lock removed from dispatch_retext — it now proceeds normally."""
    from app.routes.generative_jobs import dispatch_retext

    calls = _capture_delay(monkeypatch)
    # Should NOT raise — sequence variant is no longer blocked on retext.
    dispatch_retext(_sequence_job(), "original_text", text="new hook", remove=False)
    # Must have enqueued a re-render.
    assert len(calls) == 1


def test_dispatch_edit_size_nudge_allowed_on_sequence_variant(monkeypatch):
    """The size nudge scales the whole sequence (D19) — must stay editable."""
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _sequence_job(),
        "original_text",
        text=None,
        remove_text=False,
        style_set_id=None,
        text_size_px=72,
    )
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["size_override_px"] == 72
    assert kwargs["override_text"] is None


def test_dispatch_edit_text_still_allowed_on_non_sequence_variant(monkeypatch):
    from app.routes.generative_jobs import dispatch_edit_variant

    calls = _capture_delay(monkeypatch)
    dispatch_edit_variant(
        _sequence_job(intro_mode="cluster"),
        "original_text",
        text="fresh hook",
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
    )
    assert len(calls) == 1


def test_variants_for_response_exposes_intro_mode_and_sequence_synced(monkeypatch):
    import types
    import uuid

    import app.routes.generative_jobs as gj

    job = types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {"variant_id": "a", "render_status": "ready", "intro_mode": "sequence"},
                {"variant_id": "b", "render_status": "ready", "intro_layout": "cluster"},
                {"variant_id": "c", "render_status": "ready"},
            ]
        },
    )
    out = gj._variants_for_response(job)
    by_id = {v["variant_id"]: v for v in out}
    assert by_id["a"]["intro_mode"] == "sequence"
    assert by_id["a"]["sequence_synced"] is True
    # Legacy fallback (D19): no intro_mode → derive from intro_layout.
    assert by_id["b"]["intro_mode"] == "cluster"
    assert by_id["b"]["sequence_synced"] is False
    assert by_id["c"]["intro_mode"] is None
    assert by_id["c"]["sequence_synced"] is False


def test_variants_for_response_intro_mode_does_not_mutate_stored_dicts(monkeypatch):
    import types
    import uuid

    import app.routes.generative_jobs as gj

    stored = {"variant_id": "b", "render_status": "ready", "intro_layout": "cluster"}
    job = types.SimpleNamespace(id=uuid.uuid4(), assembly_plan={"variants": [stored]})
    gj._variants_for_response(job)
    assert "intro_mode" not in stored
    assert "sequence_synced" not in stored


# ── T1: topic/intent schema fields ────────────────────────────────────────────


def test_topic_and_intent_accepted_by_schema():
    """topic + intent are optional string fields on CreateGenerativeJobRequest."""
    req = CreateGenerativeJobRequest(
        clip_gcs_paths=["music-uploads/a.mp4"],
        topic="hiking weekend",
        intent="motivate friends",
    )
    assert req.topic == "hiking weekend"
    assert req.intent == "motivate friends"


def test_topic_and_intent_default_to_none():
    """Old clients posting without topic/intent get None — no 422."""
    req = CreateGenerativeJobRequest(clip_gcs_paths=["music-uploads/a.mp4"])
    assert req.topic is None
    assert req.intent is None
