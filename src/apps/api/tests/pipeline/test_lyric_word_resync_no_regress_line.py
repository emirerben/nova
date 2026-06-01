"""Line-style overlay regression armor for the M2 resync pass.

The resync pass MUST be mechanically unreachable for Line:
  - `_inject_line` does NOT stamp `section_anchor_s` on its overlays.
  - `resync_overlay_against_snapped_slot` requires `effect == "lyric-line"`
    NOT be eligible (it's not in `_RESYNC_EFFECTS`).
  - With the kill-switch enabled OR disabled, a Line-only slot must produce
    byte-identical overlays.

This module is the contract proof. Failure here means the M2 pass leaked
into Line behavior and the strict isolation contract in the design doc is
violated.
"""

from __future__ import annotations

import copy

from app.pipeline.lyric_injector import inject_lyric_overlays


def _line_only_recipe_and_lyrics() -> tuple[dict, dict, dict]:
    """Build the minimal scaffold for a Line-style injection — recipe with
    one slot, a lyrics_cached payload, and a line-style cfg.
    """
    recipe = {
        "slots": [
            {
                "position": 1,
                "target_duration_s": 20.0,
                "text_overlays": [],
            }
        ]
    }
    lyrics_cached = {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "hello world",
                "start_s": 2.0,
                "end_s": 4.0,
                "words": [
                    {"text": "hello", "start_s": 2.0, "end_s": 3.0},
                    {"text": "world", "start_s": 3.0, "end_s": 4.0},
                ],
            }
        ],
    }
    cfg = {
        "enabled": True,
        "style": "line",
        "pre_roll_s": 0.4,
        "post_dwell_s": 1.0,
        "fade_in_ms": 50,
        "fade_out_ms": 250,
    }
    return recipe, lyrics_cached, cfg


def test_line_style_injection_does_not_stamp_section_anchor() -> None:
    """The whole point of the strict-isolation contract: Line overlays do
    NOT carry the `section_anchor_s` field. If they did, the resync pass
    would rewrite their start/end and we'd be back in regression territory.
    Pin the absence at the injection site.
    """
    recipe, lyrics_cached, cfg = _line_only_recipe_and_lyrics()
    out = inject_lyric_overlays(
        recipe,
        lyrics_cached,
        best_start_s=0.0,
        best_end_s=20.0,
        lyrics_config=cfg,
    )
    overlays = out["slots"][0]["text_overlays"]
    assert overlays, "expected at least one Line overlay"
    for o in overlays:
        assert o.get("effect") == "lyric-line", f"unexpected effect: {o.get('effect')}"
        assert "section_anchor_s" not in o, (
            "Line overlay grew a section_anchor_s stamp — resync would now "
            "rewrite Line behavior, violating the strict-isolation contract"
        )
        assert "section_end_anchor_s" not in o


def test_resync_pass_leaves_line_overlays_byte_identical() -> None:
    """Run the resync helper directly against a Line-only slot. Every
    overlay must be returned unchanged. This is the byte-identical Line
    regression guarantee for the M2 codepath.
    """
    from app.pipeline.lyric_word_resync import resync_slot_overlays  # noqa: PLC0415

    recipe, lyrics_cached, cfg = _line_only_recipe_and_lyrics()
    out = inject_lyric_overlays(
        recipe,
        lyrics_cached,
        best_start_s=0.0,
        best_end_s=20.0,
        lyrics_config=cfg,
    )
    overlays = out["slots"][0]["text_overlays"]
    snapshot = copy.deepcopy(overlays)

    rewritten = resync_slot_overlays(
        overlays,
        slot_post_snap_section_start_s=0.4,  # simulate beat-snap drift
        slot_post_snap_duration_s=19.6,
    )
    assert rewritten == 0, f"resync touched {rewritten} Line overlays — must be 0 for Line"
    assert overlays == snapshot, "Line overlay dicts mutated during resync pass"


def test_karaoke_and_popup_overlays_carry_section_stamps() -> None:
    """Counterpart of the negative test above: karaoke and pop-in overlays
    DO get stamped. Without these stamps the resync pass becomes a no-op
    for the very styles it's meant to fix.
    """
    recipe = {
        "slots": [
            {
                "position": 1,
                "target_duration_s": 20.0,
                "text_overlays": [],
            }
        ]
    }
    lyrics_cached = {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "hello world",
                "start_s": 2.0,
                "end_s": 4.0,
                "words": [
                    {"text": "hello", "start_s": 2.0, "end_s": 3.0},
                    {"text": "world", "start_s": 3.0, "end_s": 4.0},
                ],
            }
        ],
    }
    for style, expected_effects in (
        ("karaoke", {"karaoke-line"}),
        ("per-word-pop", {"pop-in"}),
    ):
        fresh_recipe = copy.deepcopy(recipe)
        out = inject_lyric_overlays(
            fresh_recipe,
            lyrics_cached,
            best_start_s=0.0,
            best_end_s=20.0,
            lyrics_config={"enabled": True, "style": style},
        )
        overlays = out["slots"][0]["text_overlays"]
        assert overlays, f"{style}: expected at least one overlay"
        for o in overlays:
            assert o.get("effect") in expected_effects, (
                f"{style}: unexpected effect {o.get('effect')}"
            )
            assert isinstance(o.get("section_anchor_s"), int | float), (
                f"{style}: overlay missing section_anchor_s stamp — resync "
                "will be a silent no-op for this style"
            )
            assert isinstance(o.get("section_end_anchor_s"), int | float), (
                f"{style}: overlay missing section_end_anchor_s stamp"
            )
