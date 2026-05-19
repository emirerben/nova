"""Tests for app.pipeline.font_identification.

Three groups:
  1. Crop-math correctness (no torch dependency)
  2. Aggregation algorithm (no torch / no FFmpeg)
  3. Stub-matcher orchestrator drive (no torch / no FFmpeg)
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.pipeline.font_identification import (
    FontIdExtractError,
    FontMatch,
    RegistryEmbeddings,
    _aggregate_font_default,
    cosine_rank,
    crop_bbox,
    identify_fonts,
    load_registry_embeddings,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_frame(width: int, height: int, *, fill: tuple[int, int, int] = (200, 200, 200)) -> bytes:
    img = Image.new("RGB", (width, height), color=fill)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ── Group 1: crop math ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "frame_size,bbox,expected_size",
    [
        # Center bbox, half-frame square
        ((1080, 1920), {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 0.5, "h_norm": 0.25}, (540, 480)),
        # Tight strip near top
        ((1080, 1920), {"x_norm": 0.5, "y_norm": 0.1, "w_norm": 0.8, "h_norm": 0.05}, (864, 96)),
        # Asymmetric resolution (720p landscape)
        ((1280, 720), {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 1.0, "h_norm": 1.0}, (1280, 720)),
        # Sub-pixel rounding — fractional norm coords map to integer pixels
        (
            (1000, 1000),
            {"x_norm": 0.501, "y_norm": 0.499, "w_norm": 0.1, "h_norm": 0.1},
            (100, 100),
        ),
    ],
)
def test_crop_bbox_pixel_dimensions(
    frame_size: tuple[int, int],
    bbox: dict[str, float],
    expected_size: tuple[int, int],
) -> None:
    """Pixel dimensions match the math for typical bbox shapes + resolutions."""
    frame = _make_frame(*frame_size)
    cropped_bytes = crop_bbox(frame, bbox)
    cropped = Image.open(io.BytesIO(cropped_bytes))
    assert cropped.size == expected_size


def test_crop_bbox_rejects_empty_region() -> None:
    """A bbox that rounds to zero pixels raises FontIdExtractError rather
    than silently producing a 0x0 PIL image."""
    frame = _make_frame(100, 100)
    # Bbox is technically valid by PR1's range rules but its width is below
    # the rounding floor at this resolution.
    bbox = {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 0.001, "h_norm": 0.001}
    with pytest.raises(FontIdExtractError):
        crop_bbox(frame, bbox)


def test_crop_bbox_clamps_to_frame() -> None:
    """PR1's bbox validation forbids overflow, but the cropper still defends
    against float drift at the edges by clamping. Regression guard."""
    frame = _make_frame(1000, 1000)
    # Engineered to overflow by ~1 pixel after rounding without the clamp.
    bbox = {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 1.0, "h_norm": 1.0}
    cropped_bytes = crop_bbox(frame, bbox)
    cropped = Image.open(io.BytesIO(cropped_bytes))
    assert cropped.size == (1000, 1000)


# ── Group 2: aggregation algorithm ───────────────────────────────────────────


def test_aggregate_picks_highest_weighted_family() -> None:
    """Duration acts as a tie-breaker: a 6s overlay beats a 1.5s overlay
    even when the 1.5s one has slightly higher top-similarity."""
    per_overlay = [
        # 1.5s overlay, top-1 = Montserrat at sim=0.95
        ([FontMatch("Montserrat", 0.95), FontMatch("DM Sans", 0.85)], 1.5),
        # 6s overlay, top-1 = DM Sans at sim=0.88
        ([FontMatch("DM Sans", 0.88), FontMatch("Outfit", 0.82)], 6.0),
    ]
    # Montserrat score: 0.95 * 1.5 = 1.425
    # DM Sans score:    (0.95 * 1.5 — Montserrat overlay has DM Sans in top-3)
    #                 + (0.88 * 6.0 — DM Sans overlay top) = 1.425 + 5.28 = 6.705
    assert _aggregate_font_default(per_overlay) == "DM Sans"


def test_aggregate_returns_empty_when_no_overlays() -> None:
    assert _aggregate_font_default([]) == ""


def test_aggregate_skips_below_floor_matches() -> None:
    """An overlay whose top-1 similarity is below the floor (0.5) doesn't
    poison the vote — we trust the matcher's 'I don't know' signal."""
    per_overlay = [
        ([FontMatch("Junk", 0.3)], 10.0),  # below floor — ignored
        ([FontMatch("Inter Regular", 0.7)], 2.0),  # above floor
    ]
    assert _aggregate_font_default(per_overlay) == "Inter Regular"


def test_aggregate_skips_empty_overlay_lists() -> None:
    """Overlays that were skipped (null bbox, extract error) come through
    as ([], duration). Must not crash the reducer."""
    per_overlay = [
        ([], 5.0),
        ([FontMatch("Outfit", 0.8)], 3.0),
    ]
    assert _aggregate_font_default(per_overlay) == "Outfit"


# ── Group 3: stub-matcher orchestrator drive ─────────────────────────────────


class _StubMatcher:
    """Deterministic FontMatcher conforming to the Protocol structurally.

    Returns a fixed ranking on every call. Useful for proving the
    orchestrator's plumbing without loading CLIP.
    """

    def __init__(self, ranking: list[FontMatch]) -> None:
        self.ranking = ranking
        self.embed_calls: list[bytes] = []

    def embed_image(self, image_bytes: bytes) -> np.ndarray:
        self.embed_calls.append(image_bytes)
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def rank_registry(self, query_embedding: np.ndarray, top_n: int = 5) -> list[FontMatch]:
        return self.ranking[:top_n]


@dataclass
class _StubRecipe:
    """Minimal stand-in for the TemplateRecipe dataclass."""

    slots: list[dict[str, Any]] = field(default_factory=list)
    font_default: str = ""


def test_orchestrator_skips_overlays_without_bbox(monkeypatch, tmp_path) -> None:
    """Overlays where text_bbox is null get no font_alternatives field
    written; the recipe.font_default stays empty if all overlays skip."""
    recipe = _StubRecipe(
        slots=[
            {"text_overlays": [{"text_bbox": None, "sample_text": "hi", "start_s": 0, "end_s": 2}]}
        ]
    )
    matcher = _StubMatcher([FontMatch("DM Sans", 0.9)])

    # Stub the FFmpeg extractor so the test doesn't shell out.
    from app.pipeline import font_identification as fid

    monkeypatch.setattr(fid, "extract_frame_png", lambda *a, **k: _make_frame(100, 100))

    identify_fonts(recipe, "/dev/null", matcher)

    ov = recipe.slots[0]["text_overlays"][0]
    assert "font_alternatives" not in ov
    assert recipe.font_default == ""
    assert matcher.embed_calls == []


def test_orchestrator_writes_alternatives_and_default(monkeypatch) -> None:
    """A single overlay with a valid bbox produces font_alternatives on the
    overlay and aggregates to font_default on the recipe."""
    recipe = _StubRecipe(
        slots=[
            {
                "text_overlays": [
                    {
                        "text_bbox": {
                            "x_norm": 0.5,
                            "y_norm": 0.5,
                            "w_norm": 0.5,
                            "h_norm": 0.5,
                            "sample_frame_t": 1.0,
                        },
                        "start_s": 0.0,
                        "end_s": 4.0,
                        "sample_text": "WELCOME",
                    }
                ]
            }
        ]
    )
    matcher = _StubMatcher(
        [
            FontMatch("Outfit", 0.91),
            FontMatch("DM Sans", 0.84),
            FontMatch("Montserrat", 0.78),
        ]
    )

    from app.pipeline import font_identification as fid

    monkeypatch.setattr(fid, "extract_frame_png", lambda *a, **k: _make_frame(1000, 1000))

    identify_fonts(recipe, "/dev/null", matcher)

    ov = recipe.slots[0]["text_overlays"][0]
    assert ov["font_alternatives"] == [
        {"family": "Outfit", "similarity": 0.91},
        {"family": "DM Sans", "similarity": 0.84},
        {"family": "Montserrat", "similarity": 0.78},
    ]
    assert recipe.font_default == "Outfit"
    assert len(matcher.embed_calls) == 1


def test_orchestrator_recovers_from_overlay_failure(monkeypatch) -> None:
    """If FFmpeg extract fails for one overlay, others still get processed
    and the failed overlay simply doesn't gain font_alternatives. Font
    identification is best-effort."""
    recipe = _StubRecipe(
        slots=[
            {
                "text_overlays": [
                    {
                        "text_bbox": {
                            "x_norm": 0.5,
                            "y_norm": 0.5,
                            "w_norm": 0.5,
                            "h_norm": 0.5,
                            "sample_frame_t": 1.0,
                        },
                        "start_s": 0.0,
                        "end_s": 4.0,
                        "sample_text": "DOOMED",
                    },
                    {
                        "text_bbox": {
                            "x_norm": 0.5,
                            "y_norm": 0.5,
                            "w_norm": 0.5,
                            "h_norm": 0.5,
                            "sample_frame_t": 2.0,
                        },
                        "start_s": 4.0,
                        "end_s": 8.0,
                        "sample_text": "OK",
                    },
                ]
            }
        ]
    )
    matcher = _StubMatcher([FontMatch("Fraunces", 0.82)])

    calls = {"n": 0}

    def _flaky_extract(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FontIdExtractError("simulated FFmpeg failure")
        return _make_frame(1000, 1000)

    from app.pipeline import font_identification as fid

    monkeypatch.setattr(fid, "extract_frame_png", _flaky_extract)

    identify_fonts(recipe, "/dev/null", matcher)

    overlays = recipe.slots[0]["text_overlays"]
    assert "font_alternatives" not in overlays[0]
    assert overlays[1]["font_alternatives"] == [{"family": "Fraunces", "similarity": 0.82}]
    assert recipe.font_default == "Fraunces"


# ── Group 4: registry artifact loader (metadata regression) ──────────────────


def _write_test_artifact(
    tmp_path,
    *,
    model_id: str,
    model_version: str,
    registry_sha256: str = "deadbeef",
) -> str:
    families = np.array(["Inter Regular", "DM Sans"], dtype=object)
    embeddings = np.random.rand(2, 512).astype(np.float32)
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    meta = json.dumps(
        {
            "model_id": model_id,
            "model_version": model_version,
            "registry_sha256": registry_sha256,
            "generated_at": "2026-05-15T00:00:00Z",
        }
    )
    path = tmp_path / "registry-embeddings.npz"
    np.savez(path, families=families, embeddings=embeddings, meta=np.array(meta, dtype=object))
    return str(path)


def test_load_registry_embeddings_round_trip(tmp_path) -> None:
    path = _write_test_artifact(
        tmp_path,
        model_id="open-clip/ViT-B-32/openai",
        model_version="1",
    )
    reg = load_registry_embeddings(
        expected_model_id="open-clip/ViT-B-32/openai",
        expected_model_version="1",
        path=path,
    )
    assert reg.model_id == "open-clip/ViT-B-32/openai"
    assert reg.model_version == "1"
    assert list(reg.families) == ["Inter Regular", "DM Sans"]
    assert reg.embeddings.shape == (2, 512)
    assert reg.embeddings.dtype == np.float32


def test_load_registry_rejects_model_id_mismatch(tmp_path) -> None:
    path = _write_test_artifact(
        tmp_path,
        model_id="open-clip/ViT-B-32/openai",
        model_version="1",
    )
    from app.pipeline.font_identification import FontIdLoadError

    with pytest.raises(FontIdLoadError, match="model_id mismatch"):
        load_registry_embeddings(
            expected_model_id="font-clip/v1",
            expected_model_version="1",
            path=path,
        )


def test_load_registry_rejects_model_version_mismatch(tmp_path) -> None:
    path = _write_test_artifact(
        tmp_path,
        model_id="open-clip/ViT-B-32/openai",
        model_version="1",
    )
    from app.pipeline.font_identification import FontIdLoadError

    with pytest.raises(FontIdLoadError, match="model_version mismatch"):
        load_registry_embeddings(
            expected_model_id="open-clip/ViT-B-32/openai",
            expected_model_version="2",
            path=path,
        )


def test_load_registry_missing_file(tmp_path) -> None:
    from app.pipeline.font_identification import FontIdLoadError

    with pytest.raises(FontIdLoadError, match="not found"):
        load_registry_embeddings(
            expected_model_id="x",
            expected_model_version="1",
            path=str(tmp_path / "does-not-exist.npz"),
        )


# ── Group 5: cosine_rank ordering + determinism ──────────────────────────────


def test_cosine_rank_returns_sorted_descending() -> None:
    families = np.array(["A", "B", "C"], dtype=object)
    # Construct embeddings so that the cosine with [1, 0, 0] is well-ordered.
    embeddings = np.array(
        [
            [0.1, 0.9, 0.0],  # low sim
            [1.0, 0.0, 0.0],  # high sim
            [0.5, 0.5, 0.0],  # mid sim (normalized)
        ],
        dtype=np.float32,
    )
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    reg = RegistryEmbeddings(
        families=families,
        embeddings=embeddings,
        model_id="x",
        model_version="1",
        registry_sha256="",
        generated_at="",
    )
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    matches = cosine_rank(query, reg, top_n=3)
    assert [m.family for m in matches] == ["B", "C", "A"]
    # Strictly descending
    sims = [m.similarity for m in matches]
    assert sims == sorted(sims, reverse=True)


def test_cosine_rank_is_deterministic() -> None:
    families = np.array(["A", "B"], dtype=object)
    embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    reg = RegistryEmbeddings(
        families=families,
        embeddings=embeddings,
        model_id="x",
        model_version="1",
        registry_sha256="",
        generated_at="",
    )
    query = np.array([0.7, 0.3], dtype=np.float32)
    a = cosine_rank(query, reg, top_n=2)
    b = cosine_rank(query, reg, top_n=2)
    assert [m.family for m in a] == [m.family for m in b]
    assert [m.similarity for m in a] == [m.similarity for m in b]
