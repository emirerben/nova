"""Regression fixture for active-only font identification output."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from app.pipeline.font_identification import FontMatch, identify_fonts
from app.pipeline.text_overlay import FONTS_DIR

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "parity_templates" / "just-fine.json"
_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "font_identification"
    / "just-fine.snapshot.json"
)


class _DeterministicMatcher:
    def embed_image(self, image_bytes: bytes) -> np.ndarray:
        return np.array([len(image_bytes), 1.0, 0.0], dtype=np.float32)

    def rank_registry(self, query_embedding: np.ndarray, top_n: int = 5) -> list[FontMatch]:
        ranking = [
            FontMatch("Bebas Neue", 0.96),
            FontMatch("Montserrat", 0.91),
            FontMatch("Outfit", 0.9),
            FontMatch("DM Sans", 0.82),
            FontMatch("Inter Regular", 0.8),
        ]
        return ranking[:top_n]


def _frame() -> bytes:
    img = Image.new("RGB", (1000, 1000), "white")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _active_families() -> set[str]:
    with open(f"{FONTS_DIR}/font-registry.json", encoding="utf-8") as f:
        registry = json.load(f)
    return {
        family for family, entry in registry["fonts"].items() if entry.get("deprecated") is not True
    }


def test_just_fine_font_identification_snapshot(monkeypatch) -> None:
    fixture = json.loads(_FIXTURE.read_text())
    fixture["slots"][0]["text_overlays"] = [
        {
            "text": "JUST FINE",
            "text_bbox": {
                "x_norm": 0.5,
                "y_norm": 0.5,
                "w_norm": 0.5,
                "h_norm": 0.2,
                "sample_frame_t": 1.0,
            },
            "start_s": 0.0,
            "end_s": 3.0,
        }
    ]
    recipe = SimpleNamespace(slots=fixture["slots"], font_default="")

    from app.pipeline import font_identification as fid

    monkeypatch.setattr(fid, "extract_frame_png", lambda *a, **k: _frame())
    identify_fonts(recipe, video_path="/dev/null", matcher=_DeterministicMatcher())

    active = _active_families()
    assert recipe.font_default in active
    alternatives = recipe.slots[0]["text_overlays"][0]["font_alternatives"]
    assert all(alt["family"] in active for alt in alternatives)

    snapshot = {
        "font_default": recipe.font_default,
        "overlays": [
            {
                "text": overlay["text"],
                "font_default": overlay["font_alternatives"][0]["family"],
            }
            for slot in recipe.slots
            for overlay in slot.get("text_overlays", [])
            if overlay.get("font_alternatives")
        ],
    }
    expected = json.loads(_SNAPSHOT.read_text())
    assert snapshot == expected
