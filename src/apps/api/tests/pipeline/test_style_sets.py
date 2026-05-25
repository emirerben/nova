"""Tests for the curated style-set library + resolution layer."""

from __future__ import annotations

from app.pipeline.style_sets import (
    STYLE_SETS_VERSION,
    VALID_STYLE_ROLES,
    get_style_set,
    list_style_sets,
    resolve_overlay_style,
    style_set_ids,
    validate_style_sets,
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
