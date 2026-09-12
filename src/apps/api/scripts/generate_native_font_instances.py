"""Pin variable-font defaults used by the Linux cloud renderer for native text."""

import argparse
import json
from pathlib import Path

from fontTools.ttLib import TTFont

API_ROOT = Path(__file__).resolve().parents[1]


def generate() -> str:
    # FreeType/Skia on production Linux uses the fvar defaults. CoreText on
    # macOS overrides optical size with 12pt when opening the same font, so
    # querying the host typeface would produce a different native contract.
    fonts_dir = API_ROOT / "assets/fonts"
    registry = json.loads((fonts_dir / "font-registry.json").read_text())
    instances = {}
    for name, metadata in sorted(registry["fonts"].items()):
        with TTFont(fonts_dir / metadata["file"]) as font:
            if "fvar" in font:
                instances[name] = {axis.axisTag: axis.defaultValue for axis in font["fvar"].axes}
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
