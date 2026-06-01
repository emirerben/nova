"""Capture pre-fix scheduler snapshots for the kill-switch byte-identical test.

Run BEFORE any scheduler change. The snapshot files are the ground truth
that `test_kill_switch_disabled_reproduces_pre_fix_output` compares against
when LYRIC_DYNAMIC_CROSSFADE_ENABLED is set to False.

Run again only if the pre-fix scheduler itself changes (which it must NOT
during this PR). After the PR lands, the kill-switch path must continue to
reproduce these snapshots byte-identically.

Usage:
    cd src/apps/api && .venv/bin/python tests/fixtures/lyric_no_stacking/capture_snapshots.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # src/apps/api
sys.path.insert(0, str(ROOT))

from app.pipeline.lyric_injector import inject_lyric_overlays  # noqa: E402


def _cache(lines: list[tuple[str, float, float]]) -> dict:
    return {
        "lines": [
            {
                "text": text,
                "start_s": start,
                "end_s": end,
                "words": [{"text": text, "start_s": start, "end_s": end}],
            }
            for text, start, end in lines
        ]
    }


def _recipe(slot_durations_s: list[float]) -> dict:
    return {
        "slots": [
            {"position": i + 1, "target_duration_s": d, "text_overlays": []}
            for i, d in enumerate(slot_durations_s)
        ]
    }


def _extract_overlays(recipe: dict) -> list[dict]:
    return [
        ov
        for slot in recipe["slots"]
        for ov in slot.get("text_overlays", [])
        if ov.get("effect") == "lyric-line"
    ]


def capture(
    name: str,
    lines: list[tuple[str, float, float]],
    slot_durations_s: list[float],
    *,
    cfg_extra: dict | None = None,
) -> dict:
    cfg = {"enabled": True, "style": "line"}
    if cfg_extra:
        cfg.update(cfg_extra)
    recipe = _recipe(slot_durations_s)
    span_start = min(s for _, s, _ in lines) - 1.0 if lines else 0.0
    span_end = max(e for _, _, e in lines) + 1.0 if lines else sum(slot_durations_s)
    out = inject_lyric_overlays(recipe, _cache(lines), span_start, span_end, cfg)
    return {
        "name": name,
        "lines_in": [{"text": t, "start_s": s, "end_s": e} for t, s, e in lines],
        "slot_durations_s": slot_durations_s,
        "cfg": cfg,
        "overlays": _extract_overlays(out),
    }


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    fixtures = [
        capture(
            "mirea_pair",
            [("this year we've had to lose", 12.4, 13.0), ("our space we've lost", 13.1, 14.3)],
            [20.0],
        ),
        capture(
            "two_line_default_gap",
            [("First", 1.0, 2.0), ("Second", 2.3, 3.0)],
            [10.0],
        ),
        capture(
            "three_line_dense",
            [("A", 10.0, 10.4), ("B", 10.5, 10.8), ("C", 10.9, 11.3)],
            [15.0],
        ),
        capture(
            "short_line_hit",
            [("oh!", 12.92, 13.00), ("nah!", 13.05, 13.25)],
            [15.0],
        ),
    ]
    for fx in fixtures:
        path = out_dir / f"{fx['name']}_pre_fix.json"
        path.write_text(json.dumps(fx, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.name} ({len(fx['overlays'])} overlays)")


if __name__ == "__main__":
    main()
