"""Admin + public sound-effect metadata for the creator library (KRI-173)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routes.admin_sound_effects import FileUploadInitRequest, UpdateSoundEffectRequest
from app.routes.sound_effects import SoundEffectSummary
from app.services.sfx_catalog import SFX_CATEGORIES


def test_patch_accepts_a_known_category_and_normalizes_search_terms() -> None:
    req = UpdateSoundEffectRequest(
        category="rejection",
        search_terms=["  Wrong   Answer ", "buzzer", "BUZZER", "", "x" * 60],
    )
    assert req.category == "rejection"
    assert req.search_terms == ["wrong answer", "buzzer", "x" * 40]


def test_patch_rejects_an_unknown_category() -> None:
    with pytest.raises(ValidationError):
        UpdateSoundEffectRequest(category="memes")
    assert "rejection" in SFX_CATEGORIES


@pytest.mark.parametrize("ext", [".ogg", ".opus", ".webm"])
def test_upload_rejects_containers_the_iphone_cannot_play(ext: str) -> None:
    with pytest.raises(ValidationError):
        FileUploadInitRequest(filename=f"fx{ext}", ext=ext, byte_count=4096)


@pytest.mark.parametrize("ext", [".m4a", ".wav", ".mp3", ".aac"])
def test_upload_accepts_iphone_playable_containers(ext: str) -> None:
    assert FileUploadInitRequest(filename=f"fx{ext}", ext=ext, byte_count=4096).ext == ext


def test_public_summary_defaults_keep_legacy_rows_valid() -> None:
    summary = SoundEffectSummary(id="fah", name="Fah", duration_s=2.0)
    assert summary.category is None
    assert summary.search_terms == []
