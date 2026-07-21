"""resolve_sfx_suggestions — the server-side validation floor for SFX auto-suggestions.

Pure-function drop rules: unknown ids, voice-bearing effects, out-of-range or
non-finite times, spacing, caps, gain clamping, and the transcript_hash stamp
the read path stale-filters on.
"""

from __future__ import annotations

from app.services.sfx_autoplace import (
    END_KEEPOUT_S,
    MAX_SUGGESTIONS,
    MIN_SPACING_S,
    resolve_sfx_suggestions,
)

GLOSSARY = [
    {"id": "fx_click", "name": "Badge click", "audio_gcs_path": "sound-effects/a/a.wav"},
    {"id": "fx_tick", "name": "Keyboard tick", "audio_gcs_path": "sound-effects/b/b.wav"},
    {
        "id": "fx_voice",
        "name": "Meme voice clip",
        "audio_gcs_path": "sound-effects/c/c.wav",
        "contains_voice": True,
    },
    {"id": "fx_no_audio", "name": "Broken", "audio_gcs_path": None},
]


def _raw(effect_id: str, at_s: float, **kw) -> dict:
    return {
        "effect_id": effect_id,
        "at_s": at_s,
        "gain": 0.8,
        "anchor": "pause",
        "reason": "r",
        **kw,
    }


def test_accepts_valid_and_stamps_hash() -> None:
    out = resolve_sfx_suggestions([_raw("fx_click", 1.5)], GLOSSARY, 30.0, "hash1")
    assert len(out) == 1
    s = out[0]
    assert s["effect_id"] == "fx_click"
    assert s["name"] == "Badge click"
    assert s["transcript_hash"] == "hash1"
    assert s["id"]


def test_drops_unknown_voice_and_audioless_effects() -> None:
    out = resolve_sfx_suggestions(
        [_raw("fx_nope", 1.0), _raw("fx_voice", 2.0), _raw("fx_no_audio", 3.0)],
        GLOSSARY,
        30.0,
        "h",
    )
    assert out == []


def test_drops_out_of_range_and_non_finite_times() -> None:
    out = resolve_sfx_suggestions(
        [
            _raw("fx_click", -0.5),
            _raw("fx_click", 30.0 - END_KEEPOUT_S + 0.1),
            _raw("fx_click", float("nan")),
            _raw("fx_click", float("inf")),
        ],
        GLOSSARY,
        30.0,
        "h",
    )
    assert out == []


def test_min_spacing_drops_the_later_of_two_close_placements() -> None:
    out = resolve_sfx_suggestions(
        [_raw("fx_click", 1.0), _raw("fx_tick", 1.0 + MIN_SPACING_S - 0.2)],
        GLOSSARY,
        30.0,
        "h",
    )
    assert [s["effect_id"] for s in out] == ["fx_click"]


def test_caps_at_max_suggestions() -> None:
    raw = [_raw("fx_click", 2.0 * i + 1.0) for i in range(MAX_SUGGESTIONS + 4)]
    out = resolve_sfx_suggestions(raw, GLOSSARY, 60.0, "h")
    assert len(out) == MAX_SUGGESTIONS


def test_gain_clamped_and_defaulted() -> None:
    out = resolve_sfx_suggestions(
        [_raw("fx_click", 1.0, gain=9.0), _raw("fx_tick", 5.0, gain="junk")],
        GLOSSARY,
        30.0,
        "h",
    )
    assert out[0]["gain"] == 1.5
    assert out[1]["gain"] == 0.8


def test_sorted_by_time_regardless_of_input_order() -> None:
    out = resolve_sfx_suggestions(
        [_raw("fx_tick", 9.0), _raw("fx_click", 1.0)], GLOSSARY, 30.0, "h"
    )
    assert [s["at_s"] for s in out] == [1.0, 9.0]


# ── boundary values (operator-flip guards) ──────────────────────────────────────


def test_at_s_exactly_at_keepout_boundary_accepted() -> None:
    out = resolve_sfx_suggestions([_raw("fx_click", 30.0 - END_KEEPOUT_S)], GLOSSARY, 30.0, "h")
    assert len(out) == 1


def test_spacing_exactly_at_min_accepted() -> None:
    out = resolve_sfx_suggestions(
        [_raw("fx_click", 1.0), _raw("fx_tick", 1.0 + MIN_SPACING_S)], GLOSSARY, 30.0, "h"
    )
    assert [s["effect_id"] for s in out] == ["fx_click", "fx_tick"]


# ── SfxPlacementAgent.parse — LLM-output trust boundary ─────────────────────────


def _agent_input():
    from app.agents.sfx_placement import SfxCatalogEffect, SfxPlacementInput

    return SfxPlacementInput(
        words=[{"word": "hi", "start_s": 0.5, "end_s": 0.9}],
        effects=[SfxCatalogEffect(effect_id="a", name="Click")],
        duration_s=10.0,
    )


def test_parse_invalid_json_raises_schema_error() -> None:
    import pytest

    from app.agents._runtime import SchemaError
    from app.agents.sfx_placement import SfxPlacementAgent

    agent = SfxPlacementAgent.__new__(SfxPlacementAgent)
    with pytest.raises(SchemaError):
        agent.parse("not json", _agent_input())
    with pytest.raises(SchemaError):
        agent.parse('["a list, not an object"]', _agent_input())
    with pytest.raises(SchemaError):
        agent.parse('{"placements": "nope"}', _agent_input())


def test_parse_drops_malformed_items_keeps_valid() -> None:
    from app.agents.sfx_placement import SfxPlacementAgent

    agent = SfxPlacementAgent.__new__(SfxPlacementAgent)
    out = agent.parse(
        '{"placements": [{"effect_id": "a", "at_s": 1.0}, {"at_s": "x"}, 5]}',
        _agent_input(),
    )
    assert [p.effect_id for p in out.placements] == ["a"]


def test_parse_coerces_unknown_anchor_to_pause() -> None:
    from app.agents.sfx_placement import SfxPlacementAgent

    agent = SfxPlacementAgent.__new__(SfxPlacementAgent)
    out = agent.parse(
        '{"placements": [{"effect_id": "a", "at_s": 1.0, "anchor": "banana"}]}',
        _agent_input(),
    )
    assert out.placements[0].anchor == "pause"


def test_suggestion_cap_mirrors_copilot_renderer() -> None:
    # Mirrored literals coupled only by comments — pin them together so a
    # one-sided edit fails loudly (snapshot.ts COPILOT_SFX_SUGGESTIONS_MAX is
    # pinned by the web suite).
    from app.agents.edit_copilot import _SFX_SUGGESTIONS_SHOWN_MAX

    assert _SFX_SUGGESTIONS_SHOWN_MAX == MAX_SUGGESTIONS
