"""Cluster-style parity: render → persist → read adapter must agree.

THE BUG THIS PINS: the static cluster's `cluster_style` is chosen at RENDER
time from `editorial_sequence_enabled and allow_sequence` plus the per-role
`intro_cluster_*` pins. `allow_sequence` is a task kwarg — nothing on the
variant recorded it — so the read adapter in `agents/_schemas/text_element.py`
called `build_persistent_intro_overlays` with NO `cluster_style` and every
editorially-styled cluster projected into the plan-item editor through the
LEGACY profile: wrong block count (4 vs 3), wrong sizes (58/166/166/90 vs
64/109/80), wrong faces, wrong y positions.

The fix is a snapshot: the render stamps `intro_cluster_style` on the variant
and the adapter rebuilds the style from it (`resolve_cluster_style_from_variant`).

These tests assert PARITY, not a hardcoded profile: they capture the `style`
kwarg that actually reaches `compute_cluster_blocks` on the render side, then
feed the render's own persisted variant dict through the adapter and capture
the style reaching the same engine. Equality is the contract — so a future
change to WHICH style the render picks can never silently desync the editor
again.

Both sides go through the shared cluster-engine stub from
`test_generative_build_sequence`, which records the style verbatim.
"""

from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb
from app.agents._schemas.text_element import text_elements_for_variant
from app.pipeline.generative_overlays import build_persistent_intro_overlays
from app.pipeline.intro_cluster import (
    EDITORIAL_STYLE,
    cluster_style_marker,
    resolve_cluster_style,
    resolve_cluster_style_from_variant,
)
from tests.tasks.test_generative_build_sequence import (
    _bomb_emphasis_agent,
    _fake_transcript,
    _patch_reburn_helpers,
    _patch_render_helpers,
    _patch_trace,
    _patch_transcribe,
    _render,
    _sequence_existing,
    _transcript_words,
)

# Per-role pins, as `_run_regenerate_variant` resolves them onto the render.
_PINS = {
    "cluster_hero_font_override": "Instrument Serif",
    "cluster_body_font_override": "Playfair Display Italic",
    "cluster_accent_font_override": "Great Vibes",
    "cluster_hero_size_px_override": 150,
    "cluster_body_size_px_override": 70,
    "cluster_accent_size_px_override": 95,
}


def _static_editorial_render(monkeypatch, tmp_path, **kwargs):
    """Render that lands on the STATIC cluster with the editorial switch ON.

    Same recipe as `test_quote_agent_failure_falls_back_to_static_cluster`: the
    transcript is too short to sync and the quote agent declines, so both
    sequence modes fall through to the static styled cluster.
    """
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _, engine_calls = _patch_render_helpers(monkeypatch)
    _patch_transcribe(monkeypatch, transcript=_fake_transcript(words=_transcript_words()[:3]))
    _bomb_emphasis_agent(monkeypatch)
    _patch_trace(monkeypatch)
    res = _render(monkeypatch, tmp_path, author_quote_fn=lambda _d: None, **kwargs)
    assert res["ok"] is True
    assert res["intro_mode"] == "cluster", "test setup must land on the STATIC cluster"
    return res, engine_calls


def _adapter_style(engine_calls: list[dict], variant: dict):
    """The `style` the read adapter feeds the cluster engine for `variant`."""
    engine_calls.clear()
    elements = text_elements_for_variant(variant)
    assert elements, "adapter must project the cluster intro"
    assert engine_calls, "adapter must reach the cluster engine"
    styles = [c["style"] for c in engine_calls]
    assert all(s == styles[0] for s in styles)
    return styles[0]


# ── Render → persist → adapter parity (the regression contract) ──────────────────


def test_editorial_static_cluster_projects_with_the_rendered_style(monkeypatch, tmp_path):
    """THE regression: an editorial static cluster must NOT project as legacy."""
    res, engine_calls = _static_editorial_render(monkeypatch, tmp_path)

    render_styles = [c["style"] for c in engine_calls]
    assert render_styles and all(s is EDITORIAL_STYLE for s in render_styles)
    assert res["intro_cluster_style"] == "editorial", "render must snapshot its decision"

    assert _adapter_style(engine_calls, res) == EDITORIAL_STYLE, (
        "adapter projected the LEGACY profile for an editorially-rendered cluster"
    )


def test_editorial_static_cluster_with_pins_projects_with_the_rendered_style(monkeypatch, tmp_path):
    """Per-role font/size pins survive the snapshot round-trip too.

    The pins were already persisted; only the editorial marker was missing, so
    the adapter dropped the whole patched profile — pins included.
    """
    res, engine_calls = _static_editorial_render(monkeypatch, tmp_path, **_PINS)

    render_styles = [c["style"] for c in engine_calls]
    assert render_styles
    render_style = render_styles[0]
    assert all(s == render_style for s in render_styles)
    # A patched COPY, never the shared constant.
    assert render_style is not EDITORIAL_STYLE
    assert render_style["hero_font"] == "Instrument Serif"
    assert render_style["hero_size_px_override"] == 150
    assert render_style["connector_size_px_override"] == 70
    assert render_style["closer_size_px_override"] == 95
    assert EDITORIAL_STYLE["hero_font"] == "Great Vibes", "EDITORIAL_STYLE must not be mutated"

    assert res["intro_cluster_style"] == "editorial"
    assert _adapter_style(engine_calls, res) == render_style


def test_opted_out_render_projects_legacy(monkeypatch, tmp_path):
    """`allow_sequence=False` (a layout/text edit) renders — and projects — legacy."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _, engine_calls = _patch_render_helpers(monkeypatch)
    _patch_trace(monkeypatch)
    res = _render(monkeypatch, tmp_path, allow_sequence=False)

    assert res["ok"] is True
    assert [c["style"] for c in engine_calls] == [None] * len(engine_calls)
    assert res["intro_cluster_style"] == "legacy"
    assert _adapter_style(engine_calls, res) is None


def test_kill_switch_off_projects_legacy(monkeypatch, tmp_path):
    """EDITORIAL_SEQUENCE_ENABLED=false: legacy on BOTH sides of the round-trip."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", False, raising=False)
    _, engine_calls = _patch_render_helpers(monkeypatch)
    _patch_trace(monkeypatch)
    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert [c["style"] for c in engine_calls] == [None] * len(engine_calls)
    assert res["intro_cluster_style"] == "legacy"
    assert _adapter_style(engine_calls, res) is None


@pytest.mark.parametrize(
    ("sequence_allowed", "expected_style", "expected_marker"),
    [
        # Sequence-eligible fallback keeps the editorial restyle...
        (True, EDITORIAL_STYLE, "editorial"),
        # ...an explicit opt-out (layout/text edit) burns AND stamps legacy.
        # Without this case, deleting `and sequence_allowed` from the reburn
        # gate leaves the whole suite green (mutation-verified).
        (False, None, "legacy"),
    ],
)
def test_fast_reburn_snapshots_its_static_cluster_style(
    monkeypatch, sequence_allowed, expected_style, expected_marker
):
    """The fast-reburn path stamps the marker too, and the adapter honors it."""
    monkeypatch.setattr(gb.settings, "editorial_sequence_enabled", True, raising=False)
    _patch_reburn_helpers(monkeypatch)
    _patch_trace(monkeypatch)

    import app.pipeline.generative_overlays as go
    import app.pipeline.intro_cluster as ic

    engine_calls: list[dict] = []
    real_blocks = ic.compute_cluster_blocks

    def _spy(text, **kw):
        engine_calls.append({"style": kw.get("style")})
        return real_blocks(text, **kw)

    monkeypatch.setattr(ic, "compute_cluster_blocks", _spy, raising=False)
    # Force the static rebuild: no persisted scenes ⇒ no deterministic sequence.
    monkeypatch.setattr(
        go,
        "build_sequence_overlays",
        lambda *a, **k: pytest.fail("no scenes ⇒ sequence rebuild must not run"),
        raising=False,
    )

    existing = _sequence_existing(
        scenes=None,
        intro_mode="cluster",
        sequence_mode=None,
        # A hook the REAL editorial engine actually clusters (3 blocks).
        intro_text="this habit changed everything",
    )
    result = gb._reburn_text_on_base(
        job_id="j",
        variant_id="original_text",
        existing=existing,
        agent_text=types.SimpleNamespace(
            text=existing["intro_text"], highlight_word=None, word_roles=None
        ),
        agent_form={"effect": "karaoke-line", "layout": "cluster"},
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
        sequence_allowed=sequence_allowed,
    )

    render_styles = [c["style"] for c in engine_calls]
    assert render_styles and all(s is expected_style for s in render_styles)
    assert result["intro_layout"] == "cluster", "setup must land on a real cluster"
    assert result["intro_cluster_style"] == expected_marker

    merged = {**existing, **result}
    assert _adapter_style(engine_calls, merged) == expected_style


# ── Back-compat: no marker == legacy, byte-identically ───────────────────────────


def test_unmarked_legacy_variant_projects_byte_identically():
    """Every variant rendered before the snapshot existed must project unchanged.

    Runs the REAL cluster engine (no stub): the adapter's burn dicts for an
    UNMARKED variant must equal `build_persistent_intro_overlays(cluster_style=None)`
    with every other input held identical — so `cluster_style` is the only
    variable under test.
    """
    from app.agents._schemas.text_element import _ADAPTER_REVEAL_WINDOW_S, _text_window

    text = "this habit changed everything"
    v = {
        "intro_text": text,
        "intro_layout": "cluster",
        "intro_mode": "cluster",
        "intro_effect": "fade-in",
        "intro_text_color": "#FFFFFF",
        "intro_text_size_px": 64,
        "text_mode": "agent_text",
        # NO intro_cluster_style key — exactly the pre-fix persisted shape.
    }
    start_s, end_s = _text_window(v)
    builder_kwargs = dict(
        text=text,
        effect="fade-in",
        reveal_window_s=min(_ADAPTER_REVEAL_WINDOW_S, max(0.1, end_s - start_s)),
        text_color="#FFFFFF",
        layout="cluster",
        word_roles=None,
        start_s=start_s,
        end_s=end_s,
        text_size_px=64,
    )
    legacy = build_persistent_intro_overlays(cluster_style=None, **builder_kwargs)
    assert len(legacy) > 2, "fixture must build a real multi-block cluster"
    assert legacy != build_persistent_intro_overlays(
        cluster_style=EDITORIAL_STYLE, **builder_kwargs
    ), "fixture must be a text where the two profiles actually differ"

    captured: list[list[dict]] = []
    import app.pipeline.generative_overlays as go

    real_builder = go.build_persistent_intro_overlays
    try:
        go.build_persistent_intro_overlays = lambda **kw: (
            captured.append(  # type: ignore[assignment]
                real_builder(**kw)
            )
            or captured[-1]
        )
        text_elements_for_variant(v)
    finally:
        go.build_persistent_intro_overlays = real_builder  # type: ignore[assignment]

    assert captured, "adapter must call the intro builder"
    assert captured[0] == legacy, "unmarked legacy variant must project byte-identically"


@pytest.mark.parametrize("marker", [None, "legacy", "", "unknown-future-value"])
def test_only_the_editorial_marker_selects_the_editorial_style(marker):
    """Fail-safe: anything that is not exactly "editorial" resolves to legacy."""
    assert resolve_cluster_style_from_variant({"intro_cluster_style": marker}) is None


def test_marker_survives_a_real_projection_difference():
    """Sanity: the two profiles genuinely differ, so parity above is meaningful."""
    text = "this habit changed everything"
    common = {
        "intro_text": text,
        "intro_layout": "cluster",
        "intro_mode": "cluster",
        "intro_effect": "fade-in",
        "intro_text_size_px": 64,
        "text_mode": "agent_text",
    }
    legacy = text_elements_for_variant({**common, "intro_cluster_style": "legacy"})
    editorial = text_elements_for_variant({**common, "intro_cluster_style": "editorial"})

    assert len(legacy) != len(editorial), "profiles must differ in block count"
    assert [e.text for e in editorial] == ["this", "habit changed", "everything"]
    assert [round(e.size_px) for e in editorial] == [64, 109, 80]
    assert [e.font_family for e in editorial] == [
        "Playfair Display Regular",
        "Great Vibes",
        "Playfair Display Italic",
    ]


# ── resolve_cluster_style: the refactored branch semantics ───────────────────────


def _reference_style(editorial, pins):
    """Verbatim re-implementation of the pre-refactor render branch.

    Guards the extraction of the duplicated if/elif/else in `generative_build`
    into `resolve_cluster_style` — the helper must be a pure move.
    """
    if editorial and any(pins.values()):
        style = dict(EDITORIAL_STYLE)
        if pins["hero_font"]:
            style["hero_font"] = pins["hero_font"]
        if pins["body_font"]:
            style["body_font"] = pins["body_font"]
        if pins["accent_font"]:
            style["accent_font"] = pins["accent_font"]
        if pins["hero_size_px"]:
            style["hero_size_px_override"] = pins["hero_size_px"]
        if pins["body_size_px"]:
            style["connector_size_px_override"] = pins["body_size_px"]
        if pins["accent_size_px"]:
            style["closer_size_px_override"] = pins["accent_size_px"]
        return style
    if editorial:
        return EDITORIAL_STYLE
    return None


_PIN_NAMES = (
    "hero_font",
    "body_font",
    "accent_font",
    "hero_size_px",
    "body_size_px",
    "accent_size_px",
)
_PIN_VALUES = {
    "hero_font": "Instrument Serif",
    "body_font": "Playfair Display Italic",
    "accent_font": "Great Vibes",
    "hero_size_px": 150,
    "body_size_px": 70,
    "accent_size_px": 95,
}


@pytest.mark.parametrize("editorial", [True, False])
@pytest.mark.parametrize("pinned", [(), *((n,) for n in _PIN_NAMES), _PIN_NAMES])
def test_resolve_cluster_style_matches_the_original_branch(editorial, pinned):
    pins = {n: (_PIN_VALUES[n] if n in pinned else None) for n in _PIN_NAMES}
    assert resolve_cluster_style(editorial=editorial, **pins) == _reference_style(editorial, pins)


def test_resolve_cluster_style_returns_the_shared_constant_when_unpinned():
    """Identity matters: existing tests assert `style is EDITORIAL_STYLE`."""
    assert resolve_cluster_style(editorial=True) is EDITORIAL_STYLE


def test_resolve_cluster_style_never_mutates_the_shared_constant():
    before = dict(EDITORIAL_STYLE)
    resolve_cluster_style(editorial=True, hero_font="Instrument Serif", hero_size_px=200)
    assert EDITORIAL_STYLE == before


def test_falsy_pins_are_ignored():
    """0 px / "" are not legal pins — matches the render's `if override:` guards."""
    assert resolve_cluster_style(editorial=True, hero_font="", hero_size_px=0) is EDITORIAL_STYLE


def test_cluster_style_marker_round_trips():
    for style in (None, EDITORIAL_STYLE, resolve_cluster_style(editorial=True, hero_size_px=99)):
        marker = cluster_style_marker(style)
        rebuilt = resolve_cluster_style_from_variant(
            {
                "intro_cluster_style": marker,
                "intro_cluster_hero_size_px": (style or {}).get("hero_size_px_override"),
            }
        )
        assert rebuilt == style


# ── Finalization (the whitelist-strip class) ─────────────────────────────────────


def test_finalize_job_preserves_cluster_style(monkeypatch):
    """`_finalize_job` rebuilds every variant through an explicit whitelist.

    The render stamps `intro_cluster_style`, then finalization REPLACES the
    variants list — any field missing from the whitelist is silently stripped.
    Losing the marker here means every FIRST render of an editorially-styled
    cluster projects legacy in the editor, i.e. the snapshot never survives to
    the read path it exists for. Same strip class as
    `test_finalize_job_preserves_ai_timeline`.
    """
    captured: dict = {}

    def _capture_set_status(job_id, status, extra_plan=None, **kwargs):
        captured["plan"] = extra_plan

    monkeypatch.setattr(gb, "_set_status", _capture_set_status)

    results = [
        {
            "variant_id": "original_text",
            "rank": 3,
            "text_mode": "agent_text",
            "render_status": "ready",
            "ok": True,
            "intro_text": "this habit changed everything",
            "intro_layout": "cluster",
            "intro_mode": "cluster",
            "intro_cluster_style": "editorial",
            "intro_cluster_hero_font": "Instrument Serif",
            "intro_cluster_hero_size_px": 150,
        }
    ]

    gb._finalize_job("00000000-0000-0000-0000-000000000001", results)

    v = captured["plan"]["variants"][0]
    assert v["intro_cluster_style"] == "editorial", "finalize stripped intro_cluster_style"
    # The per-role pins are the style's other inputs — a surviving marker with
    # stripped pins would rebuild an UNPINNED editorial profile.
    assert v["intro_cluster_hero_font"] == "Instrument Serif"
    assert v["intro_cluster_hero_size_px"] == 150
    # End-to-end: the finalized variant must still project editorial.
    assert resolve_cluster_style_from_variant(v) is not None


def test_talking_head_intro_projects_legacy(monkeypatch):
    """`_render_talking_head_variant` burns the LEGACY cluster on purpose and
    persists NO marker — so the adapter must project legacy for it.

    Pins the one render path deliberately excluded from the editorial cascade
    (see the "DELIBERATE (PR #508 review)" comment at its build call). Without
    this, a future "unify the look" change could restyle the talking-head burn
    and silently desync the editor again — the exact bug this module exists for.
    """
    import app.pipeline.intro_cluster as ic

    styles_seen: list = []
    real_blocks = ic.compute_cluster_blocks

    def _spy(text, **kw):
        styles_seen.append(kw.get("style"))
        return real_blocks(text, **kw)

    monkeypatch.setattr(ic, "compute_cluster_blocks", _spy, raising=False)

    # The persisted talking_head shape: no `intro_cluster_style` key at all.
    talking_head_variant = {
        "variant_id": "talking_head",
        "text_mode": "agent_text",
        "intro_text": "this habit changed everything",
        "intro_layout": "cluster",
        "intro_mode": "cluster",
        "intro_effect": "fade-in",
        "intro_text_size_px": 64,
    }
    assert "intro_cluster_style" not in talking_head_variant
    text_elements_for_variant(talking_head_variant)
    assert styles_seen == [None], "talking_head must project the LEGACY profile"


# ── Projection-shape migration: saved intro owns the intro group ─────────────────


def _projected_intro(text: str, marker: str) -> list[dict]:
    return [
        e.model_dump()
        for e in text_elements_for_variant(
            {
                "intro_text": text,
                "intro_layout": "cluster",
                "intro_mode": "cluster",
                "intro_effect": "fade-in",
                "intro_text_size_px": 64,
                "text_mode": "agent_text",
                "intro_cluster_style": marker,
            }
        )
    ]


def test_saved_intro_owns_the_group_across_a_shape_change():
    """A user-edited variant must not grow GHOST intro bars when the profile flips.

    Honoring the marker changes the projected block count (legacy declines a
    2-word hook to one linear bar; editorial clusters it into two). A variant
    edited BEFORE the marker existed therefore re-projects with an index its
    saved set has never seen — which the merge would append as a bar the user
    never created, and then BURN, since a user-edited variant renders from its
    text elements. Same ownership rule the sequence shape change already has.
    """
    from app.agents._schemas.text_element import merge_projected_text_elements_for_variant

    text = "do it"
    saved = _projected_intro(text, "legacy")
    assert len(saved) == 1, "fixture: legacy declines this hook to a single bar"
    assert len(_projected_intro(text, "editorial")) == 2, (
        "fixture: editorial clusters it into two — the shape change under test"
    )

    merged = merge_projected_text_elements_for_variant(
        {
            "intro_text": text,
            "intro_layout": "cluster",
            "intro_mode": "cluster",
            "intro_effect": "fade-in",
            "intro_text_size_px": 64,
            "text_mode": "agent_text",
            "intro_cluster_style": "editorial",
            "text_elements": saved,
            "text_elements_user_edited": True,
        }
    )
    assert len(merged) == 1, f"ghost intro bar appended: {[m['text'] for m in merged]}"
    assert merged[0]["text"] == saved[0]["text"]


def test_saved_intro_ownership_does_not_suppress_other_generated_bars():
    """The guard is scoped to the intro group — caption bars still merge in."""
    from app.agents._schemas.text_element import merge_projected_text_elements_for_variant

    saved = _projected_intro("do it", "legacy")
    merged = merge_projected_text_elements_for_variant(
        {
            "intro_text": "do it",
            "intro_layout": "cluster",
            "intro_mode": "cluster",
            "intro_effect": "fade-in",
            "intro_text_size_px": 64,
            "text_mode": "agent_text",
            "intro_cluster_style": "editorial",
            "caption_cues": [
                {"text": "a caption line", "start_s": 1.0, "end_s": 2.0},
                {"text": "another line", "start_s": 2.0, "end_s": 3.0},
            ],
            "text_elements": saved,
            "text_elements_user_edited": True,
        }
    )
    texts = [m["text"] for m in merged]
    assert "a caption line" in texts and "another line" in texts, (
        f"intro guard over-suppressed non-intro projections: {texts}"
    )


def test_unedited_variant_still_gets_the_full_editorial_projection():
    """The guard only applies to user-edited variants — a fresh read is unchanged."""
    from app.agents._schemas.text_element import merge_projected_text_elements_for_variant

    merged = merge_projected_text_elements_for_variant(
        {
            "intro_text": "do it",
            "intro_layout": "cluster",
            "intro_mode": "cluster",
            "intro_effect": "fade-in",
            "intro_text_size_px": 64,
            "text_mode": "agent_text",
            "intro_cluster_style": "editorial",
        }
    )
    assert len(merged) == 2, "unedited variant must project both editorial blocks"


# ── Canvas parity (landscape variants) ───────────────────────────────────────────


def test_landscape_variant_projects_on_the_landscape_canvas():
    """The adapter must project on the canvas the RENDER used, not always portrait.

    Every render path resolves `canvas_for_orientation(orientation)` and threads
    it into the overlay builders, which measure text against `canvas.width` /
    `canvas.height`. Projecting a landscape variant on the default portrait
    canvas yields geometry the renderer never burned — and once the user edits
    any element, those values are saved and become the burn dicts.
    """
    from app.agents._schemas.text_element import _ADAPTER_REVEAL_WINDOW_S
    from app.pipeline.canvas import LANDSCAPE, PORTRAIT

    common = {
        "intro_text": "this habit changed everything",
        "intro_layout": "cluster",
        "intro_mode": "cluster",
        "intro_effect": "fade-in",
        "intro_text_size_px": 64,
        "text_mode": "agent_text",
        "intro_cluster_style": "editorial",
    }
    portrait = text_elements_for_variant({**common, "orientation": "portrait"})
    landscape = text_elements_for_variant({**common, "orientation": "landscape"})
    assert portrait and landscape

    def _burned_y_fracs(canvas):
        burns = build_persistent_intro_overlays(
            text=common["intro_text"],
            effect="fade-in",
            reveal_window_s=_ADAPTER_REVEAL_WINDOW_S,
            layout="cluster",
            text_size_px=64,
            cluster_style=EDITORIAL_STYLE,
            canvas=canvas,
        )
        assert burns, "fixture must build a cluster on this canvas"
        # One [reveal, hold] pair per block — dedupe to per-block y.
        return sorted({round(float(b["position_y_frac"]), 6) for b in burns})

    # The canvas genuinely changes the cascade geometry (block heights are
    # measured as a fraction of canvas.height), so this test has teeth.
    assert _burned_y_fracs(LANDSCAPE) != _burned_y_fracs(PORTRAIT)

    assert sorted(round(e.y_frac, 6) for e in landscape) == _burned_y_fracs(LANDSCAPE), (
        "landscape variant must project the LANDSCAPE burn geometry"
    )
    assert sorted(round(e.y_frac, 6) for e in portrait) == _burned_y_fracs(PORTRAIT)

    # Unset orientation keeps the portrait default (back-compat unchanged).
    unset = text_elements_for_variant(common)
    assert [e.y_frac for e in unset] == [e.y_frac for e in portrait]


# ── Merge seam: both intro snapshots on one variant ──────────────────────────────


def test_cluster_style_and_intro_placement_snapshots_compose():
    """A variant can carry BOTH `intro_placement` (#753) and `intro_cluster_style`.

    They land in the same adapter branch from opposite directions: placement fills
    `style_kwargs` (position / fracs / anchor), the marker selects the cluster
    style profile. For a CLUSTER the engine owns per-block geometry and overrides
    the incoming position — on the render side too (`_build_cluster_intro_overlays`
    sets `position="center"` plus explicit fracs per block), so parity holds and a
    placement snapshot must NOT shift an editorial cluster's blocks.

    Neither PR's own suite exercises the combined state; this pins the seam.
    """
    base = {
        "intro_text": "this habit changed everything",
        "intro_layout": "cluster",
        "intro_mode": "cluster",
        "intro_effect": "fade-in",
        "intro_text_size_px": 64,
        "text_mode": "agent_text",
        "intro_cluster_style": "editorial",
    }
    placement = {
        "position": "bottom",
        "position_x_frac": None,
        "position_y_frac": None,
        "max_width_frac": None,
        "text_anchor": "center",
        "rotation_deg": None,
    }

    cluster_only = text_elements_for_variant(base)
    both = text_elements_for_variant({**base, "intro_placement": placement})
    assert cluster_only, "fixture must project a cluster"

    def _geometry(elements):
        return [(e.text, e.size_px, e.font_family, e.x_frac, e.y_frac) for e in elements]

    assert _geometry(both) == _geometry(cluster_only), (
        "a placement snapshot must not move an editorial cluster's blocks"
    )
    # And the marker still selects the editorial profile with placement present.
    assert [e.text for e in both] == ["this", "habit changed", "everything"]
