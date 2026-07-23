"""Tests for the curated style-set library + resolution layer."""

from __future__ import annotations

from app.pipeline.style_sets import (
    _INTRO_ANIMATION_EFFECTS,
    STYLE_SETS_VERSION,
    VALID_STYLE_ROLES,
    get_style_set,
    list_style_sets,
    resolve_overlay_style,
    style_set_ids,
    validate_style_sets,
)


def test_special_editor_effects_are_selectable_but_not_style_defaults() -> None:
    assert "staggered-slice" in _INTRO_ANIMATION_EFFECTS
    assert "giant-title-wipe" in _INTRO_ANIMATION_EFFECTS
    assert all(
        role.get("effect") not in {"staggered-slice", "giant-title-wipe"}
        for style_set_id in style_set_ids()
        for role in get_style_set(style_set_id).get("roles", {}).values()
    )


def test_library_is_valid() -> None:
    """Every shipped set passes validation (fonts in registry, valid effects/positions)."""
    assert validate_style_sets() == []


def test_version_is_set() -> None:
    assert STYLE_SETS_VERSION and STYLE_SETS_VERSION != "unknown"


def test_default_set_always_present() -> None:
    assert "default" in style_set_ids()
    s = get_style_set("default")
    assert s["id"] == "default"


def test_unknown_set_falls_back_to_default() -> None:
    assert get_style_set("does-not-exist")["id"] == "default"
    assert get_style_set(None)["id"] == "default"


def test_applies_to_filter() -> None:
    music = {s["id"] for s in list_style_sets(applies_to="music")}
    assert "lyric_karaoke_bold" in music
    # An agentic-only set must not be offered to the music selector.
    assert "travel_editorial" not in music


def test_resolve_owns_styling() -> None:
    """The set's non-null fields win; advisory only fills gaps."""
    resolved = resolve_overlay_style(
        "travel_editorial",
        "hook",
        advisory={"font_family": "Comic Sans", "text_color": "#000000"},
    )
    # Set defines font_family + color for hook → advisory is ignored.
    assert resolved["font_family"] == "Playfair Display"
    assert resolved["text_color"] == "#FFFFFF"
    assert resolved["effect"] == "fade-in"


def test_resolve_advisory_fills_only_gaps() -> None:
    """A key the set leaves unset is filled from advisory."""
    # `default` hook defines no highlight_color; advisory supplies one.
    resolved = resolve_overlay_style("default", "hook", advisory={"highlight_color": "#ABCDEF"})
    assert resolved["highlight_color"] == "#ABCDEF"


def test_resolve_role_fallback_chain() -> None:
    """A set without the requested role falls back to a sibling, then default set."""
    # lyric_karaoke_bold only defines lyric_karaoke; asking for `hook` should
    # fall through to the default set's hook rather than blowing up.
    resolved = resolve_overlay_style("lyric_karaoke_bold", "hook")
    assert resolved.get("font_family")  # something resolved
    assert resolved.get("effect")


def test_resolve_lyric_timing_present() -> None:
    resolved = resolve_overlay_style("lyric_line_calm", "lyric_line")
    assert resolved["effect"] == "lyric-line"
    timing = resolved.get("timing", {})
    assert timing.get("fade_in_ms") == 300
    assert "pre_roll_s" in timing


def test_all_roles_recognized() -> None:
    for s in list_style_sets():
        full = get_style_set(s["id"])
        for role in full.get("roles", {}):
            assert role in VALID_STYLE_ROLES


def test_no_set_pins_intro_size() -> None:
    """The generative AI hero-intro size is agent-decided from clip composition
    (overlay_sizing) — NOT a curated constant. A style set may own the intro's
    font/color/effect/position, but pinning `text_size_px` on the `intro` role
    short-circuits the sizer (precedence: set px > computed), which is the
    "default size" we removed in v0.4.69. Guard so it can't silently come back.
    Other roles (hook/body/lyrics) keep their pinned sizes — this is intro-only.
    """
    offenders = []
    for set_id in style_set_ids():
        intro = (get_style_set(set_id).get("roles") or {}).get("intro") or {}
        if intro.get("text_size_px") is not None:
            offenders.append(f"{set_id}={intro['text_size_px']}")
    assert not offenders, (
        "style sets must not pin intro text_size_px (intro size is agent-decided): "
        + ", ".join(offenders)
    )


def test_validation_catches_bad_font() -> None:
    bad = {
        "version": "x",
        "sets": [
            {
                "id": "default",
                "roles": {"hook": {"font_family": "NotARealFont"}},
            }
        ],
    }
    problems = validate_style_sets(bad)
    assert any("font_family" in p for p in problems)


# ── Gradient preset tests ────────────────────────────────────────────────────

_GRADIENT_PRESET_IDS = [
    "iridescent",
    "sunset_gradient",
    "ocean_drift",
    "candy_pop",
    "aurora",
]


def test_gradient_presets_present() -> None:
    """All 5 gradient style-set presets must be in the library."""
    ids = set(style_set_ids())
    missing = [sid for sid in _GRADIENT_PRESET_IDS if sid not in ids]
    assert not missing, f"Gradient presets missing from library: {missing}"


def test_gradient_presets_pass_validation() -> None:
    """validate_style_sets() must return no problems (covers gradient color checks)."""
    problems = validate_style_sets()
    gradient_problems = [p for p in problems if any(pid in p for pid in _GRADIENT_PRESET_IDS)]
    assert not gradient_problems, f"Gradient presets have validation problems: {gradient_problems}"


def test_gradient_preset_resolve_returns_text_gradient() -> None:
    """resolve_overlay_style on a gradient preset yields a text_gradient with ≥2 colors."""
    for preset_id in _GRADIENT_PRESET_IDS:
        resolved = resolve_overlay_style(preset_id, "hook")
        gradient = resolved.get("text_gradient")
        assert gradient is not None, (
            f"resolve_overlay_style('{preset_id}', 'hook') returned no text_gradient"
        )
        colors = gradient.get("colors", [])
        assert len(colors) >= 2, (
            f"Gradient preset '{preset_id}' has fewer than 2 color stops: {colors}"
        )
        # Verify all colors are valid hex strings
        import re

        hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")
        bad = [c for c in colors if not hex_re.match(c)]
        assert not bad, f"Preset '{preset_id}' has invalid hex colors: {bad}"


def test_gradient_presets_target_agentic_or_generative() -> None:
    """Gradient presets must target agentic or generative (Skia renderer paths)."""
    for preset_id in _GRADIENT_PRESET_IDS:
        s = get_style_set(preset_id)
        applies_to = s.get("applies_to", [])
        assert any(t in applies_to for t in ("agentic", "generative")), (
            f"Gradient preset '{preset_id}' does not target agentic/generative: {applies_to}"
        )


def test_validation_catches_bad_gradient_color() -> None:
    """validate_style_sets catches malformed hex in text_gradient."""
    bad = {
        "version": "x",
        "sets": [
            {
                "id": "default",
                "roles": {"hook": {"text_gradient": {"colors": ["not-a-hex", "#FFFFFF"]}}},
            }
        ],
    }
    problems = validate_style_sets(bad)
    assert any("text_gradient" in p or "gradient" in p for p in problems), (
        f"Expected gradient color problem, got: {problems}"
    )
