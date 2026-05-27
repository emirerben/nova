"""Shared helpers for karaoke + popup invariant test suites.

Keep the math here so the karaoke and popup suites share one implementation
of "extract overlays from a one-slot recipe", "compute song-time of a word
arrival", and similar reusable bits. The suites themselves stay focused on
their style's distinct invariants.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable

from app.pipeline.lyric_injector import inject_lyric_overlays


def build_single_slot_recipe(target_duration_s: float = 20.0) -> dict:
    """Return a fresh one-slot recipe shaped like the lyrics-preview path
    builds (single slot, no clips, empty text_overlays array).
    """
    return {
        "slots": [
            {
                "position": 1,
                "target_duration_s": target_duration_s,
                "text_overlays": [],
            }
        ]
    }


def make_lyrics_cached(
    *,
    lines: Iterable[dict],
) -> dict:
    """Wrap raw lines in the lyrics_cached envelope the injector expects.

    Each line must already have `text`, `start_s`, `end_s`, and `words` —
    we don't synthesize them so the tests stay literal about timings.
    """
    return {"lines": list(lines)}


def inject_overlays_for_style(
    *,
    style: str,
    target_duration_s: float = 20.0,
    best_start_s: float = 0.0,
    best_end_s: float | None = None,
    lines: Iterable[dict],
    extra_cfg: dict | None = None,
) -> list[dict]:
    """Run the production injector for `style` against a one-slot recipe
    and return the resulting overlays. Deep-copies the lines so the caller
    can reuse them across multiple style runs without cross-contamination.
    """
    recipe = build_single_slot_recipe(target_duration_s)
    cached = make_lyrics_cached(lines=copy.deepcopy(list(lines)))
    cfg = {"enabled": True, "style": style}
    if extra_cfg:
        cfg.update(extra_cfg)
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=best_start_s,
        best_end_s=best_end_s if best_end_s is not None else target_duration_s,
        lyrics_config=cfg,
    )
    return out["slots"][0]["text_overlays"]


def word_song_onset_s(*, line_song_start_s: float, word_local_start_s: float) -> float:
    """Compute the absolute song time at which a word's vocal onset happens.

    The injector's per-word timings are RELATIVE to their containing line's
    start. Song-time = line_song_start + word_offset_inside_line.
    """
    return line_song_start_s + word_local_start_s
