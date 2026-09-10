import json
import sys
from pathlib import Path

import skia

from app.pipeline.portable_text_layout import resolve_legacy_glyphs

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_legacy_glyphs.json"


def reference_cases():
    font = skia.Font(skia.Typeface.MakeFromFile("assets/fonts/Inter-Regular.ttf"), 36)
    font.setSubpixel(True)
    cases = []
    for text, spacing in [("AV fi", 0), ("AV fi", 2.3), ("İstanbul çok güzel", -0.6), ("Hello", 0)]:
        glyphs = resolve_legacy_glyphs(font, text, spacing)
        cases.append(
            {
                "text": text,
                "font_size": 36,
                "spacing": spacing,
                "glyphs": [glyph.model_dump() for glyph in glyphs],
            }
        )
    return cases


def test_reference_matches_cloud_glyph_advances():
    assert json.loads(FIXTURE.read_text()) == reference_cases()


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference_cases(), indent=2) + "\n")
