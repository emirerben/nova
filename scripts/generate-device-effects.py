"""Generate the account-free iOS effect catalog through the production compiler.

Run from src/apps/api with PYTHONPATH=. and the API virtualenv.
"""

import json
from pathlib import Path

from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

root = Path(__file__).resolve().parents[1]
cases = []
for effect in [
    "static",
    "fade-in",
    "scale-up",
    "slide-up",
    "slide-down",
    "slide-in",
    "pop-in",
    "bounce",
    "ink-reveal",
    "handwriting",
    "typewriter",
    "stream-in",
    "smooth-type",
    "staggered-slice",
    "dissolve-out",
    "karaoke-line",
    "lyric-line",
    "pop-suffix",
    "giant-title",
    "giant-title-glow",
]:
    overlay = dict(
        text="Make this moment\nworth remembering",
        start_s=0.5,
        end_s=5.5,
        effect=effect,
        text_size_px=86,
        font_family="Inter",
        text_color="#FFFFFF",
        shadow_enabled=True,
        outline_px=2,
        max_width_frac=0.85,
        preserve_font_size=True,
    )
    if effect.startswith("giant-title"):
        overlay.update(effect="fade-in", text="GO ON", text_size_px=180,
                       theme_transition={"type": "giant-title-wipe", "target_glyph": "O"})
        if effect == "giant-title-glow":
            overlay.update(glow_color="#9C40FF", glow_strength=0.7)
    if effect == "pop-suffix":
        overlay["effect"] = "pop-in"
        overlay["pop_animated_suffix"] = "remembering"
    if effect == "karaoke-line":
        overlay["word_timings"] = [
            dict(text=word, start_s=index * 0.45, end_s=(index + 1) * 0.45)
            for index, word in enumerate("Make this moment worth remembering".split())
        ]
    layer, font = compile_text_overlay(
        overlay,
        layer_id=effect,
        canvas=Canvas(1080, 1920),
        dissolve_seed=138 if effect == "dissolve-out" else None,
    )
    expected_effect = "fade-in" if effect.startswith("giant-title") else "static" if effect == "pop-suffix" else effect
    assert layer.effect == expected_effect, (
        f"{effect} silently fell back to {layer.effect}"
    )
    cases.append(
        dict(
            id=effect,
            layer=layer.model_dump(mode="json"),
            font=font.model_dump(mode="json") if font else None,
        )
    )
path = root / "src/apps/ios/Kria/Resources/device-effects.json"
path.write_text(json.dumps(cases, indent=2) + "\n")
print(f"Generated {len(cases)} cases: {path}")
