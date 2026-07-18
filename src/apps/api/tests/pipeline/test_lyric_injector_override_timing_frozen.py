from __future__ import annotations

import copy

import pytest

from app.pipeline.lyric_injector import apply_lyric_line_overrides, inject_lyric_overlays


def _recipe() -> dict:
    return {
        "slots": [
            {"position": 1, "target_duration_s": 4.0, "text_overlays": []},
            {"position": 2, "target_duration_s": 4.0, "text_overlays": []},
        ]
    }


def _cache() -> dict:
    return {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "Hello bright world",
                "start_s": 0.5,
                "end_s": 2.0,
                "words": [
                    {"text": "Hello", "start_s": 0.5, "end_s": 0.9},
                    {"text": "bright", "start_s": 0.9, "end_s": 1.4},
                    {"text": "world", "start_s": 1.4, "end_s": 2.0},
                ],
            },
            {
                "text": "Next lyric lands",
                "start_s": 2.1,
                "end_s": 3.8,
                "words": [
                    {"text": "Next", "start_s": 2.1, "end_s": 2.6},
                    {"text": "lyric", "start_s": 2.6, "end_s": 3.1},
                    {"text": "lands", "start_s": 3.1, "end_s": 3.8},
                ],
            },
        ],
    }


def _inject(style: str, cache: dict) -> list[dict]:
    recipe = inject_lyric_overlays(
        _recipe(),
        cache,
        0.0,
        8.0,
        {"enabled": True, "style": style},
    )
    return [
        overlay
        for slot in recipe["slots"]
        for overlay in slot.get("text_overlays", [])
        if overlay.get("role") == "lyrics"
    ]


def _content_neutral(value):
    if isinstance(value, dict):
        return {
            key: (
                "<text>"
                if key in {"text", "original_text", "pop_animated_suffix"}
                else _content_neutral(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_content_neutral(item) for item in value]
    return value


@pytest.mark.parametrize("style", ["line", "karaoke", "per-word-pop"])
def test_text_override_keeps_every_non_content_overlay_field_identical(style: str) -> None:
    cache = _cache()
    overridden = apply_lyric_line_overrides(
        cache,
        {
            "L0": {
                "text": "Good calm night",
                "orig_text": "Hello bright world",
                "orig_start_s": 0.5,
            }
        },
    )

    baseline = _inject(style, copy.deepcopy(cache))
    edited = _inject(style, overridden)

    assert len(edited) == len(baseline)
    assert [overlay["lyric_line_key"] for overlay in edited] == [
        overlay["lyric_line_key"] for overlay in baseline
    ]
    assert _content_neutral(edited) == _content_neutral(baseline)
