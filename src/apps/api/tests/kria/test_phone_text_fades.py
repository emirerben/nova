"""Alpha oracle from the production drawing dispatcher, not duplicated formulas."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_text_fades.json"


def reference():
    assert (
        Path(cloud.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / "app")
    )
    cases = []
    for effect in ["lyric-line", "static", "none", "fade-in", "handwriting", "ink-reveal"]:
        for duration in [0.15, 0.6, 4.0]:
            for curve in ["square", "sqrt"]:
                for authored in [False, True]:
                    overlay = {
                        "text": "Hello world",
                        "start_s": 1,
                        "end_s": 1 + duration,
                        "effect": effect,
                        "font_family": "Inter",
                        "text_size_px": 32,
                        "fade_out_curve": curve,
                        "fade_out_ms": 500,
                    }
                    if effect == "lyric-line" and not authored:
                        overlay.pop("fade_out_ms")
                    if effect != "lyric-line":
                        overlay["role"] = "generative_sequence"
                    if authored:
                        overlay["motion"] = {
                            "version": 2,
                            "speed": 0.53,
                            "intensity": 0.7,
                            "exit_s": 0.2,
                        }
                    layer, _ = compile_text_overlay(
                        overlay, layer_id="fade", canvas=Canvas(300, 200)
                    )
                    samples = []
                    for time in [
                        -0.01,
                        0,
                        0.01,
                        duration * 0.1,
                        duration * 0.5,
                        duration * 0.8,
                        duration * 0.99,
                        duration,
                        duration + 0.1,
                    ]:
                        with (
                            patch.object(cloud, "_draw_centered_text") as draw,
                            patch.object(cloud, "_draw_handwriting_strokes") as pen,
                        ):
                            cloud._draw_overlay_on_canvas(
                                None, overlay, time, duration, render_canvas=Canvas(300, 200)
                            )
                        call = pen.call_args if pen.called else draw.call_args
                        samples.append({"time": time, "alpha": call.kwargs.get("alpha", 1)})
                    cases.append(
                        {
                            "effect": effect,
                            "duration": duration,
                            "motion": layer.motion.model_dump() if layer.motion else None,
                            "fade": layer.fade.model_dump(),
                            "samples": samples,
                        }
                    )
    return cases


def test_full_dispatch_alpha_matches_fixture():
    assert reference() == json.loads(FIXTURE.read_text())


def test_unrelated_fade_fields_are_ignored_by_production_and_compiler():
    for effect in ["static", "fade-in", "pop-in", "karaoke-line"]:
        overlay = {
            "text": "Hello",
            "effect": effect,
            "start_s": 0,
            "end_s": 4,
            "fade_in_ms": 300,
            "fade_out_ms": 500,
            "font_family": "Inter",
        }
        layer, _ = compile_text_overlay(overlay, layer_id="ignored", canvas=Canvas(300, 200))
        assert layer.fade is None


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2) + "\n")


def test_fade_contract_rejects_mismatched_effect_and_invalid_window():
    import pytest
    from pydantic import ValidationError

    from app.kria.portable_text import PortableTextLayer

    layer, _ = compile_text_overlay(
        {"text": "Hello", "effect": "lyric-line", "start_s": 0, "end_s": 2},
        layer_id="invalid",
        canvas=Canvas(300, 200),
    )
    for change in [
        {"fade": None},
        {"effect": "static"},
        {"fade": {"kind": "sequence", "in_ms": 1, "out_ms": 200, "curve": "square"}},
        {"fade": {"kind": "lyric", "in_ms": -1, "out_ms": 200, "curve": "square"}},
    ]:
        with pytest.raises(ValidationError):
            PortableTextLayer.model_validate({**layer.model_dump(), **change})
