"""Generate the account-free iOS effect catalog through the production compiler.

Run from src/apps/api with PYTHONPATH=. and the API virtualenv.
"""

import argparse
import json
from pathlib import Path

from app.pipeline.canvas import Canvas
from app.pipeline.portable_text_layout import compile_text_overlay

root = Path(__file__).resolve().parents[1]
path = root / "src/apps/ios/Kria/Resources/device-effects.json"
parser = argparse.ArgumentParser()
parser.add_argument(
    "--default-fonts-only",
    action="store_true",
    help="merge only the no-text and default variable-font qualification cases into the existing catalog",
)
args = parser.parse_args()
cases = []


def append_case(
    case_id: str,
    overlay: dict,
    *,
    expected_variations: dict[str, float] | None = None,
    source_overlay: dict | None = None,
) -> None:
    """Compile one deterministic catalog entry from the production text compiler."""
    layer, font = compile_text_overlay(
        overlay,
        layer_id=case_id,
        canvas=Canvas(1080, 1920),
        dissolve_seed=138 if overlay["effect"] == "dissolve-out" else None,
    )
    expected_effect = "static" if case_id == "pop-suffix" else overlay["effect"]
    assert layer.effect == expected_effect, (
        f"{case_id} silently fell back to {layer.effect}"
    )
    if expected_variations is not None:
        actual_variations = [run.font_variations for run in layer.runs]
        assert actual_variations and all(
            variations == expected_variations for variations in actual_variations
        ), f"{case_id} did not retain the production font axes: {actual_variations}"
    case = dict(
        id=case_id,
        layer=layer.model_dump(mode="json"),
        font=font.model_dump(mode="json") if font else None,
    )
    if source_overlay is not None:
        case["overlay"] = source_overlay
    cases.append(case)


# This has the same source recipe as the text fixtures, but no authored text.
# Its samples are the no-text montage baseline for cloud/device comparisons.
cases.append(dict(id="baseline-no-text", layer=None, font=None))

effects = (
    []
    if args.default_fonts_only
    else [
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
        "giant-title-typewriter",
        "giant-title-smooth",
        "giant-title-staggered",
        "giant-title-karaoke",
        "giant-title-handwriting",
        "giant-title-dissolve",
    ]
)
for effect in effects:
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
        overlay.update(
            effect="fade-in",
            text="GO ON",
            text_size_px=180,
            theme_transition={"type": "giant-title-wipe", "target_glyph": "O"},
        )
        giant_effect = {
            "giant-title-typewriter": "typewriter",
            "giant-title-smooth": "smooth-type",
            "giant-title-staggered": "staggered-slice",
            "giant-title-karaoke": "karaoke-line",
            "giant-title-handwriting": "handwriting",
            "giant-title-dissolve": "dissolve-out",
        }.get(effect, "fade-in")
        overlay["effect"] = giant_effect
        if effect in {
            "giant-title-typewriter",
            "giant-title-smooth",
            "giant-title-staggered",
        }:
            overlay.update(
                text="GO ON GO ON",
                text_size_px=110,
                motion={"version": 2, "speed": 0.25},
            )
        if effect == "giant-title-karaoke":
            overlay["word_timings"] = [
                dict(text="GO", start_s=0.5, end_s=3.9),
                dict(text="ON", start_s=3.9, end_s=5.5),
            ]
        if effect in {"giant-title-glow", "giant-title-handwriting"}:
            overlay.update(glow_color="#9C40FF", glow_strength=0.7)
    if effect == "pop-suffix":
        overlay["effect"] = "pop-in"
        overlay["pop_animated_suffix"] = "remembering"
    if effect == "karaoke-line":
        overlay["word_timings"] = [
            dict(text=word, start_s=index * 0.45, end_s=(index + 1) * 0.45)
            for index, word in enumerate("Make this moment worth remembering".split())
        ]
    append_case(effect, overlay)

# Qualify these against Linux production Skia/FreeType. Do not regenerate this
# catalog on macOS: CoreText chooses opsz=12 for these variable fonts, while
# the production faces use the fvar defaults pinned below.
for case_id, font_family, expected_variations in [
    (
        "fade-in-fraunces-default",
        "Fraunces",
        {"opsz": 9.0, "wght": 900.0, "SOFT": 0.0, "WONK": 1.0},
    ),
    ("fade-in-dm-sans-default", "DM Sans", {"opsz": 9.0, "wght": 400.0}),
]:
    overlay = dict(
        text="Make this moment\nworth remembering",
        start_s=0.5,
        end_s=5.5,
        effect="fade-in",
        text_size_px=86,
        font_family=font_family,
        text_color="#FFFFFF",
        shadow_enabled=True,
        outline_px=2,
        max_width_frac=0.85,
        preserve_font_size=True,
    )
    append_case(
        case_id,
        overlay,
        expected_variations=expected_variations,
        source_overlay=overlay,
    )

if args.default_fonts_only:
    try:
        existing_cases = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(
            f"Cannot merge default-font cases into {path}: {error}"
        ) from error
    if not isinstance(existing_cases, list):
        raise SystemExit(
            f"Cannot merge default-font cases: {path} is not a catalog list"
        )
    replacement_ids = {case["id"] for case in cases}
    cases = [
        case
        for case in existing_cases
        if not isinstance(case, dict) or case.get("id") not in replacement_ids
    ] + cases
path.write_text(json.dumps(cases, indent=2) + "\n")
verb = "Merged default-font cases into" if args.default_fonts_only else "Generated"
print(f"{verb} {len(cases)} cases: {path}")
