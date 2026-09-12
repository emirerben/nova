import subprocess
import sys
from pathlib import Path


def test_native_animation_dto_matches_authoritative_schema():
    script = Path(__file__).resolve().parents[2] / "scripts/generate_native_text_contract.py"
    subprocess.run([sys.executable, str(script), "--check"], check=True)


def test_native_variable_fonts_match_cloud_instances():
    script = Path(__file__).resolve().parents[2] / "scripts/generate_native_font_instances.py"
    subprocess.run([sys.executable, str(script), "--check"], check=True)


def test_native_font_instances_match_production_skia_defaults():
    import json

    import pytest

    if sys.platform != "linux":
        pytest.skip("Cloud font defaults are qualified against Linux FreeType/Skia")
    from app.pipeline.text_overlay_skia import _FONT_REGISTRY, _typeface_for_overlay

    target = Path(__file__).resolve().parents[2] / "assets/fonts/native-font-instances.json"
    expected = json.loads(target.read_text())
    actual = {}
    for name in sorted(_FONT_REGISTRY["fonts"]):
        face = _typeface_for_overlay({"font_family": name})
        try:
            coordinates = face.getVariationDesignPosition()
        except RuntimeError:
            continue
        if coordinates:
            actual[name] = {
                int(coordinate.axis).to_bytes(4, "big").decode("ascii"): coordinate.value
                for coordinate in coordinates
            }
    assert actual == expected
