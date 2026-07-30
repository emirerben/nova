"""Parity: the editor's projected intro element must sit where the burn put it.

`_resolve_intro_overlay_params` folds knobs > curated style set > agent advisory
into one intro placement — and both the curated sets and `overlay_format_matcher`
can resolve "bottom"/"top". That resolution was never written back to the variant,
so `_base_text_elements_for_variant` re-guessed it and always fell through to
`build_intro_overlay`'s `_DEFAULT_POSITION` ("center") — the editor drew a bottom
hook at mid-frame no matter where the render burned it.

The invariant pinned here: for a given resolved placement, the burn dicts the
ADAPTER projects carry the same geometry as the burn dicts the RENDER produced.

Byte-identity guards (the placement snapshot must be inert for everything that
isn't off-center) live in the second half of the file.
"""

from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb
from app.agents._schemas.text_element import (
    _INTRO_PLACEMENT_ADAPTER_KEYS,
    text_elements_for_variant,
)
from app.pipeline.generative_overlays import build_persistent_intro_overlays

# Fields that decide WHERE a burn dict lands. `text_size_px` rides along because a
# placement regression that silently rescaled the block would be just as wrong.
_GEOMETRY_KEYS = (
    "position",
    "position_x_frac",
    "position_y_frac",
    "max_width_frac",
    "rotation_deg",
    "text_anchor",
    "text_size_px",
)

_INTRO_TEXT = "this changed everything"
_SIZE_PX = 64
# The adapter always builds with _ADAPTER_REVEAL_WINDOW_S; match it on the render
# side so the two overlay lists differ ONLY where the bug would show.
_REVEAL_S = 3.0


def _geometry(burn_dicts: list[dict]) -> list[dict]:
    return [{k: bd.get(k) for k in _GEOMETRY_KEYS} for bd in burn_dicts]


def _render(agent_form: dict, **resolver_kwargs) -> tuple[list[dict], dict | None, int]:
    """Resolve params the way a render does, burn, and snapshot the placement.

    Returns ``(burn_dicts, persisted_placement, intro_px)`` — i.e. exactly what
    `_render_generative_variant` / `_render_talking_head_variant` produce and store.
    """
    agent_text = types.SimpleNamespace(text=_INTRO_TEXT, highlight_word=None, word_roles=None)
    params, intro_px, _source = gb._resolve_intro_overlay_params(
        agent_text,
        agent_form,
        resolver_kwargs.pop("style_set_id", None),
        size_override_px=_SIZE_PX,
        **resolver_kwargs,
    )
    params.pop("_bs_pregate", None)
    # Mirrors the render write sites: a variant that CARRIES candidates must record
    # its placement even when centered, or the adapter falls back to those candidates.
    placement = gb._intro_placement_from_params(
        params, has_candidates=bool(resolver_kwargs.get("placement_candidates"))
    )
    burn_dicts = build_persistent_intro_overlays(reveal_window_s=_REVEAL_S, beats=[], **params)
    return burn_dicts, placement, intro_px


def _variant(placement: dict | None, intro_px: int, **extra) -> dict:
    v = {
        "text_mode": "agent_text",
        "intro_text": _INTRO_TEXT,
        "intro_layout": "linear",
        "intro_mode": "linear",
        "intro_effect": "karaoke-line",
        "intro_text_color": "#FFFFFF",
        "intro_text_size_px": intro_px,
        "intro_placement": placement,
        **extra,
    }
    if placement is None:
        # A render that resolved the centered default stores None; prove the
        # adapter behaves the same whether the key is None or absent entirely.
        v.pop("intro_placement")
    return v


def _projected(variant: dict) -> list[dict]:
    elements = text_elements_for_variant(variant)
    assert elements, "adapter projected no element"
    return elements[0].source_params["burn_dicts"]


# ── Parity: resolved placement survives to the editor ─────────────────────────────


@pytest.mark.parametrize("position", ["bottom", "top"])
def test_agent_resolved_position_reaches_the_editor(position):
    """An `overlay_format_matcher` "bottom"/"top" pick must not read back as center."""
    burned, placement, intro_px = _render({"effect": "karaoke-line", "position": position})

    assert placement is not None, "a non-centered placement must be persisted"
    assert placement["position"] == position
    assert {bd["position"] for bd in burned} == {position}, "render sanity check"

    projected = _projected(_variant(placement, intro_px))
    assert _geometry(projected) == _geometry(burned)


@pytest.mark.parametrize(
    ("position", "expected_element_position"),
    [("bottom", "bottom"), ("top", "top"), ("center", "middle")],
)
def test_element_position_matches_the_burn(position, expected_element_position):
    """The editor reads `TextElement.position`, not the raw burn dict."""
    _burned, placement, intro_px = _render({"effect": "karaoke-line", "position": position})
    element = text_elements_for_variant(_variant(placement, intro_px))[0]
    assert element.position == expected_element_position


def test_user_style_knob_position_reaches_the_editor():
    """The knob tier (Creator Agent M1) resolves the same way as the agent tier."""
    burned, placement, intro_px = _render(
        {"effect": "karaoke-line"}, user_style_knobs={"position": "bottom"}
    )

    assert placement is not None and placement["position"] == "bottom"
    assert _geometry(_projected(_variant(placement, intro_px))) == _geometry(burned)


@pytest.mark.parametrize("rotation_deg", [90.0, 0.0])
def test_placement_candidate_fracs_reach_the_editor(rotation_deg):
    """Masonry whitespace placement: explicit fracs, not a named position.

    `rotation_deg=0.0` is the COMMON case — `_rotation_for_empty_pocket` only
    returns 90.0 for a genuinely portrait-shaped pocket. A truthiness check on
    the way back out drops it (and any `position_x_frac=0.0`), silently
    re-centring the block.
    """
    candidates = [
        {
            "source": "masonry_whitespace",
            "x_frac": 0.1468,
            "y_frac": 0.4979,
            "max_width_frac": 0.64,
            "rotation_deg": rotation_deg,
        }
    ]
    burned, placement, intro_px = _render(
        {"effect": "karaoke-line"}, placement_candidates=candidates
    )

    assert placement is not None
    assert (placement["position_x_frac"], placement["position_y_frac"]) == (0.1468, 0.4979)

    # The snapshot supersedes text_placement_candidates and must agree with the
    # pre-snapshot projection that read those candidates directly.
    variant = _variant(placement, intro_px, text_placement_candidates=candidates)
    assert _geometry(_projected(variant)) == _geometry(burned)

    legacy = _variant(None, intro_px, text_placement_candidates=candidates)
    assert _geometry(_projected(variant)) == _geometry(_projected(legacy))


def test_zero_valued_fracs_survive_the_projection():
    """0.0 is a position, not an absence. `position_x_frac=0.0` pins the left
    edge and is reachable from the knob route (`ge=0.0`); dropping it re-centres
    the block at 0.5 and moves the hook half a frame."""
    placement = {
        "position": "center",
        "position_x_frac": 0.0,
        "position_y_frac": 0.0,
        "max_width_frac": None,
        "text_anchor": "center",
        "rotation_deg": 0.0,
    }
    element = text_elements_for_variant(_variant(placement, _SIZE_PX))[0]
    assert (element.x_frac, element.y_frac) == (0.0, 0.0)
    assert element.rotation_deg == 0.0


@pytest.mark.parametrize("style_set_id", ["travel_editorial", "lifestyle_clean"])
def test_declined_candidates_do_not_leak_into_the_editor(style_set_id):
    """A curated set pins `intro.position="center"`, so `has_explicit_position` is
    True and the resolver DECLINES the masonry candidates — the hook burns
    dead-center. With no snapshot the adapter fell back to those same declined
    candidates and drew the hook in the whitespace pocket instead.

    Finalization hides this on the first render (it strips
    `text_placement_candidates`), but a re-render re-adds them and nothing strips
    them again, so the mismatch becomes permanent.
    """
    candidates = [
        {
            "source": "masonry_whitespace",
            "x_frac": 0.1468,
            "y_frac": 0.4979,
            "max_width_frac": 0.64,
            "rotation_deg": 90.0,
        }
    ]
    burned, placement, intro_px = _render(
        {"effect": "karaoke-line"},
        style_set_id=style_set_id,
        placement_candidates=candidates,
    )

    # The render declined the candidates...
    assert {bd.get("position_x_frac") for bd in burned} == {None}
    # ...so the centered resolution MUST still be recorded, or the adapter reads them.
    assert placement is not None
    assert placement["position"] == "center"
    assert (placement["position_x_frac"], placement["position_y_frac"]) == (None, None)

    variant = _variant(placement, intro_px, text_placement_candidates=candidates)
    assert _geometry(_projected(variant)) == _geometry(burned)
    assert text_elements_for_variant(variant)[0].position == "middle"


def test_candidate_free_centered_variant_still_persists_nothing():
    """The has_candidates carve-out must not widen: no candidates → still None."""
    assert _render({"effect": "karaoke-line"}, style_set_id="travel_editorial")[1] is None


# ── Byte-identity: the snapshot is inert for centered + legacy variants ───────────


def test_centered_placement_persists_nothing():
    """The overwhelming majority: resolved "center" stores None, not a dict."""
    _burned, placement, _intro_px = _render({"effect": "karaoke-line"})
    assert placement is None

    # ...including when "center" arrives explicitly from every tier.
    for kwargs in (
        {"agent_form": {"effect": "karaoke-line", "position": "center"}},
        {"agent_form": {"effect": "karaoke-line"}, "user_style_knobs": {"position": "center"}},
    ):
        agent_form = kwargs.pop("agent_form")
        assert _render(agent_form, **kwargs)[1] is None


def test_centered_variant_projects_identically_with_and_without_the_key():
    """`intro_placement: None` must be indistinguishable from a legacy variant."""
    burned, placement, intro_px = _render({"effect": "karaoke-line"})
    assert placement is None

    legacy = _variant(None, intro_px)
    assert "intro_placement" not in legacy
    explicit_none = {**legacy, "intro_placement": None}

    assert _geometry(_projected(legacy)) == _geometry(burned)
    assert _projected(explicit_none) == _projected(legacy)


def test_legacy_variant_without_snapshot_is_unchanged():
    """Pre-fix rows have no snapshot: the adapter keeps its old center default."""
    element = text_elements_for_variant(
        {
            "text_mode": "agent_text",
            "intro_text": _INTRO_TEXT,
            "intro_layout": "linear",
            "intro_mode": "linear",
        }
    )[0]
    assert element.position == "middle"
    assert (element.x_frac, element.y_frac) == (None, None)


# ── Re-render: a persisted position must not silently re-center ──────────────────


def test_persisted_position_folds_into_the_no_llm_rerender():
    """`_resolve_regen_text` reconstructs agent_form without the original advisory.

    Without the fold, the first text edit re-resolved a "bottom" intro to center —
    which would put the editor and the burn right back out of sync.
    """
    _text, agent_form, mode = gb._resolve_regen_text(
        override_text=None,
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text=_INTRO_TEXT,
        persisted_highlight=None,
        run_text_agents_fn=lambda: (None, None),
        persisted_position="bottom",
    )
    assert mode == "agent_text"
    assert agent_form["position"] == "bottom"

    _burned, placement, _px = _render(agent_form)
    assert placement is not None and placement["position"] == "bottom"


def test_override_text_keeps_the_persisted_position():
    """Retyping the hook moves the words, not the block."""
    _text, agent_form, _mode = gb._resolve_regen_text(
        override_text="a brand new hook",
        remove_text=False,
        existing_text_mode="agent_text",
        persisted_text=_INTRO_TEXT,
        persisted_highlight=None,
        run_text_agents_fn=lambda: (None, None),
        persisted_position="bottom",
    )
    assert agent_form["position"] == "bottom"


def test_legacy_rerender_leaves_agent_form_position_absent():
    """No snapshot → no `position` key → resolution is byte-identical to pre-fix."""
    for override in (None, "a brand new hook"):
        _text, agent_form, _mode = gb._resolve_regen_text(
            override_text=override,
            remove_text=False,
            existing_text_mode="agent_text",
            persisted_text=_INTRO_TEXT,
            persisted_highlight=None,
            run_text_agents_fn=lambda: (None, None),
            persisted_position=None,
        )
        assert "position" not in agent_form


def test_persisted_intro_position_reader_tolerates_legacy_shapes():
    assert gb._persisted_intro_position({}) is None
    assert gb._persisted_intro_position({"intro_placement": None}) is None
    assert gb._persisted_intro_position({"intro_placement": "bottom"}) is None
    assert gb._persisted_intro_position({"intro_placement": {"position": None}}) is None
    assert gb._persisted_intro_position({"intro_placement": {"position": "bottom"}}) == "bottom"


def test_persisted_intro_position_never_folds_center():
    """Folding "center" would NOT be a no-op — it becomes the style advisory.

    `resolve_overlay_style` fills any key the curated set leaves null from the
    advisory, so a folded "center" makes `style["position"]` truthy, which flips
    `has_explicit_position` in `_resolve_intro_overlay_params` and skips the
    placement-candidate branch — dropping a masonry intro's fracs on re-render.
    """
    centered_with_fracs = {
        "intro_placement": {"position": "center", "position_x_frac": 0.1468},
    }
    assert gb._persisted_intro_position(centered_with_fracs) is None


# ── Write-side snapshot helper ────────────────────────────────────────────────────


def test_intro_placement_from_params_normalizes_empty_fields():
    """The byte-identity insurance for the centered default."""
    assert gb._intro_placement_from_params({}) is None
    assert gb._intro_placement_from_params({"position": None, "text_anchor": None}) is None
    assert gb._intro_placement_from_params({"position": "", "text_anchor": ""}) is None

    snap = gb._intro_placement_from_params({"position": "bottom"})
    assert set(snap) == set(gb._INTRO_PLACEMENT_KEYS)
    assert snap["text_anchor"] == "center"


def test_adapter_reads_every_persisted_placement_key():
    """Writer and reader keep independent key lists across a module boundary — a
    field the render snapshots but the adapter never reads is silently dropped
    from the editor's projection (the original bug, one field at a time)."""
    assert set(gb._INTRO_PLACEMENT_KEYS) == set(_INTRO_PLACEMENT_ADAPTER_KEYS)


def test_default_placement_covers_every_placement_key():
    """`_DEFAULT_INTRO_PLACEMENT` is derived from the key tuple — if the two ever
    drift, the equality collapse can never hold and EVERY centered variant starts
    persisting a dict (silently flipping them onto the new adapter branch)."""
    assert set(gb._DEFAULT_INTRO_PLACEMENT) == set(gb._INTRO_PLACEMENT_KEYS)


@pytest.mark.parametrize("style_set_id", ["word_reveal", "typewriter", "ai_answer"])
def test_half_pinned_style_sets_project_at_the_burned_y(style_set_id):
    """The three shipped sets that pin geometry: x=0.06, y=null, anchor=left.

    `_resolve_anchor` takes y from the named position when the burn dict has no
    `position_y_frac` — _POSITION_Y["center"] is 0.45. The adapter used to invent
    0.5 for any half-pinned overlay, so the editor drew these intros 0.05*H below
    the burn, and saving baked the offset in as an explicit frac.
    """
    from app.pipeline.text_overlay import _POSITION_Y  # noqa: PLC0415

    burned, placement, intro_px = _render({"effect": "karaoke-line"}, style_set_id=style_set_id)
    assert placement is not None
    assert (placement["position_x_frac"], placement["position_y_frac"]) == (0.06, None)

    # Burn dicts stay shape-identical (the render never emits an explicit y here).
    assert _geometry(_projected(_variant(placement, intro_px))) == _geometry(burned)

    # ...and the element the editor draws carries the RENDERER's y, not 0.5.
    element = text_elements_for_variant(_variant(placement, intro_px))[0]
    assert element.position == "custom"
    assert element.x_frac == 0.06
    assert element.y_frac == _POSITION_Y["center"] != 0.5
    assert element.alignment == "left"


# ── Adapter branches that must stay inert ────────────────────────────────────────


@pytest.mark.parametrize("anchor", ["left", "right"])
def test_non_center_anchor_reaches_the_editor(anchor):
    """`position` stays "center" but the placement is still off-default — this is
    the case that proves the adapter's `text_anchor` line is load-bearing."""
    burned, placement, intro_px = _render({"effect": "karaoke-line", "text_anchor": anchor})
    assert placement is not None
    assert placement["text_anchor"] == anchor and placement["position"] == "center"
    assert _geometry(_projected(_variant(placement, intro_px))) == _geometry(burned)


def test_cluster_projection_unaffected_by_the_snapshot():
    """The cluster engine owns its own geometry. A persisted placement must not
    shift it — pinned today because `_build_cluster_intro_overlays` hardcodes
    position/text_anchor per block, but nothing else stops a future change from
    forwarding these fields and moving every cluster intro in the editor."""
    placement = {
        **gb._DEFAULT_INTRO_PLACEMENT,
        "rotation_deg": 8.0,  # off-default WITHOUT tripping the cluster→linear downgrade
    }
    base = {
        "text_mode": "agent_text",
        "intro_text": _INTRO_TEXT,
        "intro_layout": "cluster",
        "intro_mode": "cluster",
        "intro_effect": "karaoke-line",
        "intro_text_color": "#FFFFFF",
        "intro_text_size_px": _SIZE_PX,
    }

    def _shape(elements):
        # `id` is a fresh uuid per projection — compare everything else.
        return [{k: val for k, val in e.model_dump().items() if k != "id"} for e in elements]

    with_snapshot = text_elements_for_variant({**base, "intro_placement": placement})
    without = text_elements_for_variant(base)
    assert with_snapshot, "cluster engine declined — test would pass vacuously"
    assert _shape(with_snapshot) == _shape(without)


# ── Finalization ─────────────────────────────────────────────────────────────────


def test_finalize_job_preserves_intro_placement(monkeypatch):
    """REGRESSION: `_finalize_job` rebuilds every variant from a key allowlist.

    With `intro_placement` missing from it, the snapshot was written by the render
    and stripped seconds later, so the editor still drew an off-center hook at
    mid-frame on the FIRST render — the exact bug the snapshot exists to fix. The
    whole parity suite stayed green because nothing else exercises finalization.
    """
    import uuid  # noqa: PLC0415

    from tests.tasks.test_generative_build import _FakeJob, _patch_job_session  # noqa: PLC0415

    job = _FakeJob(assembly_plan={})
    _patch_job_session(monkeypatch, job)
    placement = {
        "position": "bottom",
        "position_x_frac": None,
        "position_y_frac": None,
        "max_width_frac": None,
        "text_anchor": "center",
        "rotation_deg": None,
    }
    result = {
        "variant_id": "original_text",
        "rank": 1,
        "text_mode": "agent_text",
        "ok": True,
        "render_status": "ready",
        "output_url": "u",
        "video_path": "generative-jobs/j/v.mp4",
        "intro_text": _INTRO_TEXT,
        "intro_layout": "linear",
        "intro_mode": "linear",
        "intro_placement": placement,
        "orientation": "portrait",
    }
    gb._finalize_job(str(uuid.uuid4()), [result])
    assert job.assembly_plan["variants"][0]["intro_placement"] == placement
