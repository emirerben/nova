"""Round-trip invariant: text_elements_for_variant(v) → build_overlays_from_text_elements()
must produce byte-identical burn dicts to the direct legacy generators.

Critical (A1/A14): the byte-equality tests cover the ADAPTER PATH, not just the
compiler forward path.  Every legacy variant shape is exercised:

  Shape 1 — Linear intro:  build_persistent_intro_overlays(layout="linear")
  Shape 2 — Cluster intro:  build_persistent_intro_overlays(layout="cluster")
  Shape 3 — Sequence:       build_sequence_overlays()

Known limitation (documented as xfail): burn dicts whose font_family is NOT in
TextElement._ALLOWED_FONTS lose the font after the round-trip because the
adapter's security guard silently drops unknown fonts. Production engines use
allowlisted registry fonts; the xfail test proves the unknown-font failure mode
explicitly rather than letting it hide.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import (
    _ADAPTER_REVEAL_WINDOW_S,
    TextElement,
    _burn_dict_to_text_element,
    coerce_text_elements,
    text_elements_for_variant,
)
from app.pipeline.generative_overlays import (
    _HOLD_TO_END_S,
    build_overlays_from_text_elements,
    build_persistent_intro_overlays,
    build_sequence_overlays,
)

# ── Comparison helper ─────────────────────────────────────────────────────────────


def _normalize(burn_dicts: list[dict]) -> list[dict]:
    """Normalize burn dicts for comparison: sort by (start_s, role, effect), round floats.

    Sorting is stable across test runs and handles any ordering differences from
    independent calls to the same generator function.
    """
    out = []
    for d in sorted(
        burn_dicts,
        key=lambda x: (
            round(float(x.get("start_s", 0)), 6),
            x.get("role", ""),
            x.get("effect", ""),
            x.get("text", ""),
        ),
    ):
        nd: dict = {}
        for k, v in sorted(d.items()):
            if isinstance(v, float):
                nd[k] = round(v, 6)
            else:
                nd[k] = v
        out.append(nd)
    return out


# ── Monkeypatch helper: cluster engine with allowlisted font ──────────────────────


def _patch_cluster_engine(
    monkeypatch, *, blocks_per_scene: int = 1, decline_indices: set = frozenset()
):
    """Stub compute_cluster_blocks with deterministic blocks using PlayfairDisplay-Bold.

    PlayfairDisplay-Bold is in TextElement._ALLOWED_FONTS so the adapter preserves it
    in the TextElement — this is the prerequisite for a byte-identical round-trip.
    Using an ALLOWLISTED font is intentional: the separate xfail test covers the
    non-allowlisted path.

    The stub accepts all kwargs (including `language`) that the real caller
    (_build_cluster_intro_overlays) passes through.
    """
    import app.pipeline.intro_cluster as ic

    counter = {"i": 0}

    def _fake_blocks(
        text,
        *,
        word_roles,
        base_size_px,
        font_family=None,
        reveal_window_s,
        style,
        accent_parity=0,
        language="en",  # forwarded by _build_cluster_intro_overlays
    ):
        idx = counter["i"]
        counter["i"] += 1
        if idx in decline_indices:
            return None
        return [
            {
                "text": text,
                "role": "hero",
                "text_size_px": base_size_px,
                "font_family": "PlayfairDisplay-Bold",  # in _ALLOWED_FONTS
                "position_x_frac": 0.4,
                "position_y_frac": 0.44,
                "start_offset_s": 0.0,
                "reveal_s": min(reveal_window_s, 0.7),
            }
            for _ in range(blocks_per_scene)
        ]

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks)


# ── Sequence scene fixture ────────────────────────────────────────────────────────


def _make_seq_scenes() -> list[dict]:
    return [
        {
            "words": ["when", "the", "days"],
            "word_roles": ["connector", "connector", "hero"],
            "start_s": 0.0,
            "end_s": 2.25,
            "fade_out": False,
        },
        {
            "words": ["found", "us."],
            "word_roles": ["hero", "closer"],
            "start_s": 2.0,
            "end_s": 6.0,
            "fade_out": True,
        },
    ]


# ── Gate tests: adapter returns early for recognized text_mode values ─────────────


class TestGates:
    def test_text_mode_none_returns_empty(self):
        v = {"text_mode": "none", "intro_text": "some text", "duration_s": 10.0}
        assert text_elements_for_variant(v) == []

    def test_text_mode_lyrics_returns_empty(self):
        v = {"text_mode": "lyrics", "intro_text": "some lyrics", "duration_s": 10.0}
        assert text_elements_for_variant(v) == []

    def test_no_intro_text_no_scenes_returns_empty(self):
        v = {"text_mode": "agent_text", "intro_text": None, "duration_s": 10.0}
        assert text_elements_for_variant(v) == []

    def test_empty_intro_text_returns_empty(self):
        v = {"intro_text": "", "duration_s": 10.0}
        assert text_elements_for_variant(v) == []

    def test_default_text_mode_agent_text_is_not_gated(self):
        """Absent text_mode defaults to agent_text — adapter produces elements."""
        v = {"intro_text": "Hello world", "duration_s": 10.0}
        elements = text_elements_for_variant(v)
        assert len(elements) > 0

    def test_sequence_mode_with_empty_scenes_returns_empty(self):
        v = {"intro_mode": "sequence", "scenes": [], "text_mode": "agent_text"}
        assert text_elements_for_variant(v) == []


# ── Shape 1: Linear round-trip ────────────────────────────────────────────────────


class TestLinearRoundTrip:
    """Linear intro: adapter → compiler == build_persistent_intro_overlays(layout="linear").

    The adapter calls build_persistent_intro_overlays(reveal_window_s=_ADAPTER_REVEAL_WINDOW_S=3.0)
    and groups each generated intro into one editable TextElement.  Animated
    intros carry reveal_s so the compiler can emit the old reveal+hold burn pair;
    static intros compile to one persistent static burn dict with identical visual
    output.
    """

    def _direct(self, text: str, effect: str, text_color: str = "#FFFFFF", **kwargs) -> list[dict]:
        """Build legacy burn dicts directly using the same args the adapter would use."""
        return build_persistent_intro_overlays(
            text=text,
            effect=effect,
            reveal_window_s=_ADAPTER_REVEAL_WINDOW_S,
            text_color=text_color,
            layout="linear",
            **kwargs,
        )

    def _roundtrip(self, variant: dict) -> list[dict]:
        """Adapter → compiler → burn dicts."""
        elements = text_elements_for_variant(variant)
        return build_overlays_from_text_elements(elements, video_duration_s=12.0)

    def test_karaoke_roundtrip_byte_identical(self):
        """Karaoke-line linear variant: round-trip is byte-identical."""
        v = {
            "intro_text": "This changed my life",
            "intro_layout": "linear",
            "intro_effect": "karaoke-line",
            "intro_text_color": "#FFFFFF",
            "text_mode": "agent_text",
        }
        legacy = self._direct("This changed my life", effect="karaoke-line")
        roundtrip = self._roundtrip(v)
        assert len(roundtrip) == len(legacy), (
            f"Expected {len(legacy)} overlays (reveal+hold), got {len(roundtrip)}"
        )
        assert _normalize(roundtrip) == _normalize(legacy), (
            "Karaoke linear round-trip must be byte-identical"
        )

    def test_fade_in_roundtrip_byte_identical(self):
        """Non-karaoke effect (fade-in): settled color is text_color, not highlight."""
        v = {
            "intro_text": "My journey started here",
            "intro_layout": "linear",
            "intro_effect": "fade-in",
            "intro_text_color": "#FFFFFF",
            "text_mode": "agent_text",
        }
        legacy = self._direct("My journey started here", effect="fade-in")
        roundtrip = self._roundtrip(v)
        assert _normalize(roundtrip) == _normalize(legacy), (
            "fade-in linear round-trip must be byte-identical"
        )

    def test_text_placement_candidate_projects_into_text_element(self):
        v = {
            "intro_text": "Pocket story",
            "intro_layout": "linear",
            "intro_effect": "static",
            "intro_text_color": "#111111",
            "intro_text_size_px": 78,
            "text_mode": "agent_text",
            "text_placement_candidates": [
                {
                    "source": "masonry_whitespace",
                    "x_frac": 0.1468,
                    "y_frac": 0.4979,
                    "max_width_frac": 0.64,
                    "rotation_deg": 90.0,
                }
            ],
        }
        elements = text_elements_for_variant(v)

        assert len(elements) == 1
        assert elements[0].position == "custom"
        assert elements[0].x_frac == 0.1468
        assert elements[0].y_frac == 0.4979
        assert elements[0].max_width_frac == 0.64
        assert elements[0].rotation_deg == 90.0

    @pytest.mark.xfail(
        reason=(
            "Known limitation: 'pop-in' is in generative_overlays._SKIA_EFFECTS (the renderer "
            "can draw it) but NOT in TextElement._ALLOWED_EFFECTS. The adapter's _burn_dict_to_"
            "text_element() coerces unknown effects to 'static' via _BURN_EFFECT_TO_TEXT_ELEMENT, "
            "so the reveal overlay loses its pop-in effect. The Skia-effects set and the "
            "TextElement allowlist are intentionally decoupled (the editor surface only exposes "
            "4 effects); this test documents the mismatch rather than hiding it."
        ),
        strict=True,
    )
    def test_pop_in_roundtrip_byte_identical(self):
        """Known mismatch: pop-in is in _SKIA_EFFECTS but not _ALLOWED_EFFECTS → coerced to static."""  # noqa: E501
        v = {
            "intro_text": "You need to see this",
            "intro_effect": "pop-in",
            "intro_text_color": "#FFDDCC",
            "text_mode": "agent_text",
        }
        legacy = self._direct("You need to see this", effect="pop-in", text_color="#FFDDCC")
        roundtrip = self._roundtrip(v)
        # This MUST fail (strict xfail): reveal has effect='pop-in' in legacy,
        # but effect='static' in roundtrip (adapter coerces pop-in → static).
        assert _normalize(roundtrip) == _normalize(legacy)

    def test_static_roundtrip_byte_identical(self):
        """Static effect groups to one persistent editor/render bar."""
        v = {
            "intro_text": "Open with this",
            "intro_effect": "static",
            "text_mode": "agent_text",
        }
        roundtrip = self._roundtrip(v)
        assert _normalize(roundtrip) == [
            {
                "effect": "static",
                "end_s": _HOLD_TO_END_S,
                "highlight_color": "#FFD24A",
                "position": "center",
                "role": "generative_intro",
                "start_s": 0.0,
                "subject_substitute": False,
                "text": "Open with this",
                "text_anchor": "center",
                "text_color": "#FFFFFF",
                "text_size": "jumbo",
            }
        ]

    def test_adapter_produces_one_grouped_intro_bar(self):
        """Linear intro produces one editable TextElement with reveal metadata."""
        v = {"intro_text": "One grouped bar expected", "intro_effect": "karaoke-line"}
        elements = text_elements_for_variant(v)
        assert len(elements) == 1
        intro = elements[0]
        assert intro.text == "One grouped bar expected"
        assert intro.effect == "karaoke-line"
        assert intro.start_s == 0.0
        assert intro.end_s == _HOLD_TO_END_S
        assert intro.reveal_s == _ADAPTER_REVEAL_WINDOW_S
        assert intro.color == "#FFFFFF"
        assert intro.highlight_color == "#FFD24A"
        assert intro.source_params["identity"] == "intro:intro"

    def test_karaoke_word_timings_preserved_verbatim(self):
        """A17: stored word_timings survive the round-trip without re-synthesis."""
        v = {
            "intro_text": "i did not expect this",
            "intro_effect": "karaoke-line",
            "intro_text_color": "#FFFFFF",
        }
        legacy = self._direct("i did not expect this", effect="karaoke-line")
        roundtrip = self._roundtrip(v)
        reveal_legacy = next(o for o in legacy if o["effect"] == "karaoke-line")
        reveal_roundtrip = next(o for o in roundtrip if o["effect"] == "karaoke-line")
        assert reveal_roundtrip["word_timings"] == reveal_legacy["word_timings"], (
            "Word timings must be byte-identical (stored value, not re-synthesized)"
        )

    def test_karaoke_hold_uses_settled_highlight_color(self):
        """Karaoke settles to highlight_color; the hold overlay inherits it."""
        v = {
            "intro_text": "hello world",
            "intro_effect": "karaoke-line",
            "intro_text_color": "#FFFFFF",
        }
        roundtrip = self._roundtrip(v)
        hold = next(o for o in roundtrip if o["effect"] == "static")
        # Karaoke settled color = highlight_color (#FFD24A default)
        assert hold["text_color"] == "#FFD24A"

    def test_no_word_timings_on_hold_overlay(self):
        """The static hold overlay must never carry word_timings."""
        v = {"intro_text": "steady state text", "intro_effect": "karaoke-line"}
        roundtrip = self._roundtrip(v)
        hold = next(o for o in roundtrip if o["effect"] == "static")
        assert "word_timings" not in hold

    def test_subject_substitute_is_false_on_compiled_overlays(self):
        """subject_substitute=False is a renderer-parity invariant."""
        v = {"intro_text": "stay safe always", "intro_effect": "static"}
        roundtrip = self._roundtrip(v)
        for o in roundtrip:
            assert o["subject_substitute"] is False

    def test_word_roles_forwarded_by_adapter(self):
        """Adapter forwards intro_word_roles to build_persistent_intro_overlays."""
        roles = ["connector", "connector", "connector", "hero", "connector"]
        v = {
            "intro_text": "i did not expect this",
            "intro_effect": "karaoke-line",
            "intro_text_color": "#FFFFFF",
            "intro_word_roles": roles,
        }
        # For layout="linear", word_roles is threaded but has no effect on output
        # (the linear path ignores it — cluster only). Both paths should match.
        legacy = self._direct(
            "i did not expect this",
            effect="karaoke-line",
            word_roles=roles,
        )
        roundtrip = self._roundtrip(v)
        assert _normalize(roundtrip) == _normalize(legacy)

    def test_intro_text_size_px_roundtrip_byte_identical(self):
        """When intro_text_size_px is set, grouped bar carries text_size_px."""
        v = {
            "intro_text": "Large text overlay",
            "intro_effect": "static",
            "intro_text_size_px": 80,
            "text_mode": "agent_text",
        }
        roundtrip = self._roundtrip(v)
        assert len(roundtrip) == 1
        assert roundtrip[0]["text_size_px"] == 80
        assert "text_size" not in roundtrip[0]
        assert roundtrip[0]["start_s"] == 0.0
        assert roundtrip[0]["end_s"] == _HOLD_TO_END_S
        # Both should carry text_size_px, not text_size
        for o in roundtrip:
            assert "text_size_px" in o
            assert "text_size" not in o

    def test_all_compiled_overlays_carry_generative_intro_role(self):
        """Linear intro overlays always have role='generative_intro'."""
        v = {"intro_text": "role check", "intro_effect": "static"}
        roundtrip = self._roundtrip(v)
        for o in roundtrip:
            assert o["role"] == "generative_intro"


# ── A14: size_class round-trip ────────────────────────────────────────────────────


class TestSizeClassRoundTrip:
    """A14: non-jumbo size_class must survive the adapter → compiler round-trip.

    The default compiler path falls back to size_class='jumbo' when elem.size_class
    is None.  A14 requires that when the adapter reads size_class from the burn
    dict it is preserved, so a variant that was rendered with 'xlarge' doesn't
    silently become 'jumbo' on the first compile.
    """

    def test_xlarge_size_class_compiles_to_text_size_xlarge(self):
        """TextElement(size_class='xlarge') → burn dict text_size='xlarge' (not 'jumbo')."""
        elem = TextElement(text="Test text", start_s=0.0, end_s=3.0, size_class="xlarge")
        burn_dicts = build_overlays_from_text_elements([elem], video_duration_s=10.0)
        assert len(burn_dicts) == 1
        assert burn_dicts[0]["text_size"] == "xlarge", (
            f"size_class='xlarge' must compile to text_size='xlarge', got: {burn_dicts[0]}"
        )
        assert "text_size_px" not in burn_dicts[0], (
            "No size_px override — text_size bucket must be present"
        )

    def test_all_size_classes_compile_to_matching_text_size_bucket(self):
        """Every size_class value compiles to its corresponding text_size bucket."""
        for cls in ("small", "medium", "large", "xlarge", "xxlarge", "jumbo"):
            elem = TextElement(text="hello", start_s=0.0, end_s=2.0, size_class=cls)
            burn_dicts = build_overlays_from_text_elements([elem], video_duration_s=10.0)
            assert burn_dicts[0]["text_size"] == cls, (
                f"size_class={cls!r} must compile to text_size={cls!r}"
            )

    def test_size_px_overrides_size_class_and_drops_text_size(self):
        """When size_px is set, compiled burn dict uses text_size_px and drops text_size."""
        elem = TextElement(text="hello", start_s=0.0, end_s=2.0, size_px=72.0, size_class="xlarge")
        burn_dicts = build_overlays_from_text_elements([elem], video_duration_s=10.0)
        assert burn_dicts[0].get("text_size_px") == 72
        assert "text_size" not in burn_dicts[0], (
            "text_size_px is authoritative — text_size bucket must be absent"
        )

    def test_none_size_class_defaults_to_jumbo(self):
        """When size_class is None, compiler defaults to jumbo (the renderer's own default)."""
        elem = TextElement(text="default size", start_s=0.0, end_s=2.0, size_class=None)
        burn_dicts = build_overlays_from_text_elements([elem], video_duration_s=10.0)
        assert burn_dicts[0]["text_size"] == "jumbo"

    def test_adapter_reads_size_class_from_burn_dict_text_size(self):
        """Adapter sets size_class='xlarge' when burn dict has text_size='xlarge'."""
        burn_dict = {
            "text": "Test",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "text_size": "xlarge",
            "text_anchor": "center",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFD24A",
            "effect": "static",
        }
        elem = _burn_dict_to_text_element(burn_dict)
        assert elem is not None
        assert elem.size_class == "xlarge", (
            "Adapter must read size_class from burn_dict['text_size']"
        )

    def test_adapter_size_class_none_when_text_size_px_used(self):
        """When burn dict uses text_size_px (no text_size bucket), adapter sets size_class=None."""
        burn_dict = {
            "text": "Test",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "text_size_px": 72,  # No "text_size" key — px wins
            "text_anchor": "center",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFD24A",
            "effect": "static",
        }
        elem = _burn_dict_to_text_element(burn_dict)
        assert elem is not None
        assert elem.size_class is None, "No text_size bucket → size_class must be None"
        assert elem.size_px == 72.0

    def test_adapter_preserves_spacing_fields_from_burn_dict(self):
        """Parity-gated layout fields survive legacy burn dict → TextElement → compiler."""
        burn_dict = {
            "text": "Spaced text",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "text_size_px": 72,
            "text_anchor": "center",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFD24A",
            "effect": "static",
            "letter_spacing": 0.12,
            "line_spacing": 1.4,
            "max_width_frac": 0.4,
        }
        elem = _burn_dict_to_text_element(burn_dict)
        assert elem is not None
        assert elem.letter_spacing == 0.12
        assert elem.line_spacing == 1.4
        assert elem.max_width_frac == 0.4

        compiled = build_overlays_from_text_elements([elem], video_duration_s=10.0)
        assert compiled[0]["letter_spacing"] == 0.12
        assert compiled[0]["line_spacing"] == 1.4
        assert compiled[0]["max_width_frac"] == 0.4

    def test_adapter_preserves_rotation_from_burn_dict(self):
        burn_dict = {
            "text": "Rotated text",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "text_size_px": 72,
            "text_anchor": "center",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFD24A",
            "effect": "static",
            "rotation_deg": 90,
        }
        elem = _burn_dict_to_text_element(burn_dict)
        assert elem is not None
        assert elem.rotation_deg == 90.0

        compiled = build_overlays_from_text_elements([elem], video_duration_s=10.0)
        assert compiled[0]["rotation_deg"] == 90.0

    def test_size_class_roundtrip_via_adapter_xlarge(self):
        """A14 end-to-end: a burn dict with text_size='xlarge' round-trips to 'xlarge'."""
        burn_dict = {
            "text": "Check the size",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "text_size": "xlarge",
            "text_anchor": "center",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFD24A",
            "effect": "static",
        }
        elem = _burn_dict_to_text_element(burn_dict)
        assert elem is not None
        compiled = build_overlays_from_text_elements([elem], video_duration_s=10.0)
        assert compiled[0]["text_size"] == "xlarge", (
            "A14: xlarge size_class must survive adapter → compiler round-trip"
        )


# ── Shape 2: Cluster intro round-trip ─────────────────────────────────────────────


class TestClusterRoundTrip:
    """Cluster intro: adapter → compiler == build_persistent_intro_overlays(layout='cluster').

    The cluster engine is stubbed (its geometry is tested separately in
    test_intro_cluster.py) to return blocks with allowlisted fonts.  The round-trip
    focuses on whether the adapter correctly captures the per-block burn dicts
    (which carry position_x_frac / position_y_frac and text_size_px) and whether
    the compiler reproduces them byte-identically.
    """

    def test_cluster_roundtrip_byte_identical(self, monkeypatch):
        """Cluster variant: adapter → compiler == direct build_persistent_intro_overlays."""
        _patch_cluster_engine(monkeypatch)

        v = {
            "intro_text": "what's your favorite place?",
            "intro_layout": "cluster",
            "intro_effect": "fade-in",
            "intro_text_color": "#FFFFFF",
            "intro_text_size_px": 60,
            "text_mode": "agent_text",
        }
        legacy = build_persistent_intro_overlays(
            text="what's your favorite place?",
            effect="fade-in",
            reveal_window_s=_ADAPTER_REVEAL_WINDOW_S,
            text_color="#FFFFFF",
            layout="cluster",
            text_size_px=60,
            hook_window_s=_HOLD_TO_END_S,
        )
        assert len(legacy) >= 2, "Cluster should produce at least [reveal, hold] per block"

        elements = text_elements_for_variant(v)
        assert len(elements) > 0
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=12.0)

        assert len(roundtrip) == len(legacy), (
            f"Cluster round-trip overlay count mismatch: {len(roundtrip)} vs {len(legacy)}"
        )
        assert _normalize(roundtrip) == _normalize(legacy), (
            "Cluster round-trip must be byte-identical"
        )

    def test_cluster_adapter_produces_elements_with_custom_position(self, monkeypatch):
        """Cluster burn dicts carry position_x_frac → adapter sets position='custom'."""
        _patch_cluster_engine(monkeypatch)

        v = {
            "intro_text": "clustered text here",
            "intro_layout": "cluster",
            "intro_effect": "fade-in",
            "intro_text_size_px": 60,
        }
        elements = text_elements_for_variant(v)
        assert elements, "Adapter should produce elements for cluster variant"
        # Cluster blocks carry explicit fracs → adapter picks "custom" position
        for elem in elements:
            assert elem.position == "custom", (
                "Cluster burn dicts carry position_x_frac → position must be 'custom'"
            )
            assert elem.x_frac is not None
            assert elem.y_frac is not None

    def test_cluster_adapter_preserves_allowlisted_font(self, monkeypatch):
        """Allowlisted font from cluster engine survives the adapter."""
        _patch_cluster_engine(monkeypatch)

        v = {
            "intro_text": "clustered text here",
            "intro_layout": "cluster",
            "intro_effect": "fade-in",
            "intro_text_size_px": 60,
        }
        elements = text_elements_for_variant(v)
        assert elements
        for elem in elements:
            assert elem.font_family == "PlayfairDisplay-Bold", (
                "Allowlisted font from cluster engine must be preserved in TextElement"
            )

    def test_cluster_fallback_to_linear_roundtrip_byte_identical(self, monkeypatch):
        """When the cluster engine declines (returns None), adapter falls back to linear."""
        _patch_cluster_engine(monkeypatch, decline_indices={0, 1, 2, 3, 4})

        v = {
            "intro_text": "short",
            "intro_layout": "cluster",
            "intro_effect": "fade-in",
            "intro_text_color": "#FFFFFF",
        }
        # Both paths fall back to linear
        legacy_linear = build_persistent_intro_overlays(
            text="short",
            effect="fade-in",
            reveal_window_s=_ADAPTER_REVEAL_WINDOW_S,
            text_color="#FFFFFF",
            layout="linear",
        )
        roundtrip = build_overlays_from_text_elements(
            text_elements_for_variant(v), video_duration_s=12.0
        )
        assert _normalize(roundtrip) == _normalize(legacy_linear)


# ── Shape 3: Sequence round-trip ──────────────────────────────────────────────────


class TestSequenceRoundTrip:
    """Sequence variant: adapter → compiler == build_sequence_overlays.

    Uses a stubbed cluster engine with an allowlisted font so the adapter can
    preserve font_family in the TextElement.  The xfail test at the bottom of
    this class proves the non-allowlisted failure mode.
    """

    def test_sequence_roundtrip_byte_identical(self, monkeypatch):
        """Sequence variant: adapter → compiler == direct build_sequence_overlays."""
        _patch_cluster_engine(monkeypatch)
        scenes = _make_seq_scenes()

        v = {
            "intro_mode": "sequence",
            "intro_text": None,
            "intro_text_color": "#FFFFFF",
            "text_mode": "agent_text",
            "scenes": scenes,
            "duration_s": 12.0,
        }
        legacy = build_sequence_overlays(scenes, base_size_px=60, text_color="#FFFFFF")
        assert legacy is not None, "Stubbed engine must produce overlays"

        elements = text_elements_for_variant(v)
        assert len(elements) > 0
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=12.0)

        assert len(roundtrip) == len(legacy), (
            f"Expected {len(legacy)} overlays from sequence, got {len(roundtrip)}"
        )
        assert _normalize(roundtrip) == _normalize(legacy), (
            "Sequence round-trip must be byte-identical"
        )

    def test_sequence_adapter_produces_one_element_per_block(self, monkeypatch):
        """Adapter produces one TextElement per scene block (no reveal/hold split)."""
        _patch_cluster_engine(monkeypatch, blocks_per_scene=1)
        scenes = _make_seq_scenes()
        v = {
            "intro_mode": "sequence",
            "intro_text": None,
            "text_mode": "agent_text",
            "scenes": scenes,
        }
        elements = text_elements_for_variant(v)
        # 2 scenes × 1 block = 2 elements
        assert len(elements) == 2

    def test_sequence_all_elements_have_generative_sequence_role(self, monkeypatch):
        """All sequence TextElements carry role='generative_sequence'."""
        _patch_cluster_engine(monkeypatch)
        scenes = _make_seq_scenes()
        v = {"intro_mode": "sequence", "text_mode": "agent_text", "scenes": scenes}
        elements = text_elements_for_variant(v)
        assert all(e.role == "generative_sequence" for e in elements), (
            "Sequence elements must carry role='generative_sequence'"
        )

    def test_sequence_compiled_roles_are_generative_sequence(self, monkeypatch):
        """Compiled overlays all carry role='generative_sequence'."""
        _patch_cluster_engine(monkeypatch)
        scenes = _make_seq_scenes()
        v = {"intro_mode": "sequence", "text_mode": "agent_text", "scenes": scenes}
        elements = text_elements_for_variant(v)
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=12.0)
        assert all(o["role"] == "generative_sequence" for o in roundtrip)

    def test_sequence_fade_out_ms_preserved_through_roundtrip(self, monkeypatch):
        """fade_out_ms=350 from fade_out scenes survives the round-trip."""
        _patch_cluster_engine(monkeypatch)
        scenes = _make_seq_scenes()  # scene 1 has fade_out=True
        v = {
            "intro_mode": "sequence",
            "text_mode": "agent_text",
            "scenes": scenes,
            "intro_text_color": "#FFFFFF",
        }
        elements = text_elements_for_variant(v)
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=12.0)

        fade_out_overlays = [o for o in roundtrip if o.get("fade_out_ms") is not None]
        assert fade_out_overlays, "fade_out_ms=350 must survive the round-trip"
        assert all(o["fade_out_ms"] == 350 for o in fade_out_overlays)

    def test_sequence_no_fade_out_ms_on_normal_scenes(self, monkeypatch):
        """Normal scenes (fade_out=False) must NOT carry fade_out_ms."""
        _patch_cluster_engine(monkeypatch)
        normal_only = [_make_seq_scenes()[0]]  # scene 0 has fade_out=False
        v = {"intro_mode": "sequence", "text_mode": "agent_text", "scenes": normal_only}
        elements = text_elements_for_variant(v)
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=12.0)
        for o in roundtrip:
            assert "fade_out_ms" not in o

    def test_sequence_custom_base_size_px_roundtrip(self, monkeypatch):
        """sequence_base_size_px is forwarded to build_sequence_overlays."""
        _patch_cluster_engine(monkeypatch)
        scenes = _make_seq_scenes()
        v = {
            "intro_mode": "sequence",
            "text_mode": "agent_text",
            "scenes": scenes,
            "sequence_base_size_px": 90,
        }
        legacy = build_sequence_overlays(scenes, base_size_px=90, text_color="#FFFFFF")
        elements = text_elements_for_variant(v)
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=12.0)
        assert _normalize(roundtrip) == _normalize(legacy), (
            "sequence_base_size_px=90 must be forwarded; round-trip must match"
        )

    @pytest.mark.xfail(
        reason=(
            "Known limitation: round-trip is NOT byte-identical when the cluster engine "
            "assigns a font_family NOT in TextElement._ALLOWED_FONTS. "
            "The adapter's A19 security guard silently drops unknown fonts so the compiled "
            "output loses font_family entirely. "
            "Production engines use allowlisted fonts (PlayfairDisplay-Bold, etc.); "
            "this test documents the failure mode rather than hiding it."
        ),
        strict=True,
    )
    def test_sequence_roundtrip_fails_with_non_allowlisted_font(self, monkeypatch):
        """Known mismatch: unknown fonts are dropped by the adapter (A19)."""
        import app.pipeline.intro_cluster as ic

        def _bad_font_blocks(
            text, *, word_roles, base_size_px, reveal_window_s, style, accent_parity=0, **kw
        ):
            return [
                {
                    "text": text,
                    "role": "hero",
                    "text_size_px": base_size_px,
                    "font_family": "Definitely Not A Font",
                    "position_x_frac": 0.4,
                    "position_y_frac": 0.44,
                    "start_offset_s": 0.0,
                    "reveal_s": reveal_window_s,
                }
            ]

        monkeypatch.setattr(ic, "compute_cluster_blocks", _bad_font_blocks)
        scenes = [
            {
                "words": ["hello"],
                "word_roles": None,
                "start_s": 0.0,
                "end_s": 2.0,
                "fade_out": False,
            }
        ]
        v = {"intro_mode": "sequence", "text_mode": "agent_text", "scenes": scenes}

        legacy = build_sequence_overlays(scenes, base_size_px=60, text_color="#FFFFFF")
        elements = text_elements_for_variant(v)
        roundtrip = build_overlays_from_text_elements(elements, video_duration_s=10.0)

        # This assertion MUST fail (strict xfail):
        # legacy has an unknown font_family; roundtrip has no font_family key.
        assert _normalize(roundtrip) == _normalize(legacy)


# ── Security guards ───────────────────────────────────────────────────────────────


class TestSecurityGuards:
    """T1 schema-level security guards (A18/A19)."""

    def test_unknown_font_family_raises_validation_error(self):
        """Unknown font_family raises ValidationError (A19)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TextElement(text="test", start_s=0, end_s=1, font_family="../../etc/passwd")

    def test_non_string_injection_in_font_family_rejected(self):
        """Injection attempt in font_family raises ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TextElement(text="test", start_s=0, end_s=1, font_family="shell_inject; rm -rf /")

    def test_unknown_effect_raises_validation_error(self):
        """Unknown effect raises ValidationError (A19)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TextElement(text="test", start_s=0, end_s=1, effect="shell_inject")

    def test_allowed_effects_accepted(self):
        """The four allowed effects are accepted without error."""
        for effect in ("static", "fade-in", "slide-up", "karaoke-line"):
            elem = TextElement(text="test", start_s=0, end_s=1, effect=effect)
            assert elem.effect == effect

    def test_size_px_clamped_to_max_300(self):
        """size_px > 300 is silently clamped to 300 (A18 — Skia OOM guard)."""
        elem = TextElement(text="test", start_s=0, end_s=1, size_px=9999)
        assert elem.size_px == 300.0

    def test_size_px_clamped_to_min_8(self):
        """size_px < 8 is silently clamped to 8 (A18)."""
        elem = TextElement(text="test", start_s=0, end_s=1, size_px=1)
        assert elem.size_px == 8.0

    def test_invalid_hex_color_coerced_to_none(self):
        """Invalid hex color strings are coerced to None (not rejected)."""
        elem = TextElement(text="test", start_s=0, end_s=1, color="not-a-color")
        assert elem.color is None

    def test_script_injection_in_color_coerced_to_none(self):
        """Script injection attempts in color field are coerced to None."""
        elem = TextElement(text="test", start_s=0, end_s=1, color="javascript:alert(1)")
        assert elem.color is None

    def test_script_injection_in_highlight_color_coerced_to_none(self):
        """Script injection attempts in highlight_color field are coerced to None."""
        elem = TextElement(text="test", start_s=0, end_s=1, highlight_color="<script>")
        assert elem.highlight_color is None

    def test_valid_allowlisted_fonts_accepted(self):
        """All allowlisted font families are accepted."""
        for font in (
            "PlayfairDisplay-Bold",
            "PlayfairDisplay-Regular",
            "Inter-Bold",
            "Inter-Regular",
            "Playfair Display",
            "Great Vibes",
        ):
            elem = TextElement(text="test", start_s=0, end_s=1, font_family=font)
            assert elem.font_family == font

    def test_none_font_family_accepted(self):
        """font_family=None is accepted (renderer uses its default)."""
        elem = TextElement(text="test", start_s=0, end_s=1, font_family=None)
        assert elem.font_family is None

    def test_stroke_width_clamped_to_max_20(self):
        """stroke_width > 20 is silently clamped to 20."""
        elem = TextElement(text="test", start_s=0, end_s=1, stroke_width=999)
        assert elem.stroke_width == 20.0

    def test_stroke_width_clamped_to_min_0(self):
        """stroke_width < 0 is silently clamped to 0."""
        elem = TextElement(text="test", start_s=0, end_s=1, stroke_width=-5)
        assert elem.stroke_width == 0.0

    def test_x_frac_clamped_to_0_1(self):
        """x_frac is clamped to [0, 1]."""
        assert TextElement(text="t", start_s=0, end_s=1, x_frac=2.0).x_frac == 1.0
        assert TextElement(text="t", start_s=0, end_s=1, x_frac=-1.0).x_frac == 0.0


# ── coerce_text_elements ──────────────────────────────────────────────────────────


class TestCoerceTextElements:
    """coerce_text_elements: defensive list[dict] → list[TextElement] | None."""

    def test_empty_list_returns_none(self):
        assert coerce_text_elements([]) is None

    def test_none_returns_none(self):
        assert coerce_text_elements(None) is None

    def test_valid_dicts_return_elements(self):
        raw = [
            {"text": "Hello", "start_s": 0.0, "end_s": 2.0},
            {"text": "World", "start_s": 2.0, "end_s": 4.0},
        ]
        result = coerce_text_elements(raw)
        assert result is not None
        assert len(result) == 2
        assert result[0].text == "Hello"
        assert result[1].text == "World"

    def test_non_dict_entries_silently_skipped(self):
        """Non-dict entries (strings, ints) are skipped — no exception."""
        raw = [
            {"text": "Good", "start_s": 0.0, "end_s": 2.0},
            "not a dict",
            42,
        ]
        result = coerce_text_elements(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].text == "Good"

    def test_invalid_entries_silently_skipped(self):
        """Entries that fail TextElement validation are dropped, not raised."""
        raw = [
            {"text": "Valid", "start_s": 0.0, "end_s": 2.0},
            {"text": "Bad font", "start_s": 1.0, "end_s": 2.0, "font_family": "illegal font"},
        ]
        result = coerce_text_elements(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].text == "Valid"

    def test_all_invalid_returns_none(self):
        """If all entries fail, returns None (not an empty list)."""
        raw: list = ["string", 42, None]
        assert coerce_text_elements(raw) is None

    def test_extra_keys_ignored_by_model(self):
        """Unknown keys in the dict are silently ignored (model_config extra='ignore')."""
        raw = [{"text": "OK", "start_s": 0.0, "end_s": 1.0, "unknown_future_field": "value"}]
        result = coerce_text_elements(raw)
        assert result is not None
        assert len(result) == 1


def test_shadow_enabled_false_survives_validation_and_compile():
    element = TextElement.model_validate(
        {
            "text": "No halo",
            "start_s": 0.0,
            "end_s": 2.0,
            "role": "generative_intro",
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.4,
            "shadow_enabled": False,
        }
    )

    assert element.model_dump()["shadow_enabled"] is False
    overlays = build_overlays_from_text_elements([element], video_duration_s=4.0)
    assert overlays
    assert overlays[0]["shadow_enabled"] is False
