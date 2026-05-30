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
