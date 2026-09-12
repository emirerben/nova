"""Pin the cloud renderer's variable-font instances for native authored text."""

import argparse
import json
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
from app.pipeline.text_overlay_skia import _FONT_REGISTRY, _typeface_for_overlay  # noqa: E402


def generate() -> str:
    instances = {}
    for name in sorted(_FONT_REGISTRY["fonts"]):
        face = _typeface_for_overlay({"font_family": name})
        try:
            coordinates = face.getVariationDesignPosition()
        except RuntimeError:
            continue
        if coordinates:
            instances[name] = {
                int(coordinate.axis).to_bytes(4, "big").decode("ascii"): coordinate.value
                for coordinate in coordinates
            }
    return json.dumps(instances, sort_keys=True, indent=2) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = API_ROOT / "assets/fonts/native-font-instances.json"
    expected = generate()
    if args.check:
        if target.read_text() != expected:
            raise SystemExit("Native font instances need regeneration")
    else:
        target.write_text(expected)
