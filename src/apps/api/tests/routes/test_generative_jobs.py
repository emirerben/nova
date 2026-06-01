"""Request-validation tests for the generative-job routes.

Pydantic field validators are tested directly (no DB / TestClient needed). The clip
prefix allowlist is the security-relevant one — an arbitrary bucket key must be rejected
before any download.
"""

from __future__ import annotations

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
