"""Author the KRI-1391 speech and beat-mark replay fixtures.

    cd src/apps/api && python -m tests.evals.request_following.author_kri1391

The `variant_snapshot` in each fixture is NOT hand-written. It is what the real
`build_editor_snapshot` returns for the variant below, so the cassette proves the model is
shown the spoken words and the song beats the server now fills in. `test_kri1391_marks.py`
re-runs the builder and fails when a checked-in fixture drifts from it.

The recorded model output is authored (it is a cassette, not a live run). The replay compiles
it through the real parser and editor compiler, so the evidence is compile-only.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from .runner import THREAD_DIR

TRACK_ID = "00000000-0000-0000-0000-000000000007"
MUSIC_START_S = 12.0
# Song-clock beats (seconds into the track), as MusicTrack.beat_timestamps_s stores them.
SONG_BEATS = [11.5, 12.48, 12.97, 13.46, 13.95, 14.44, 14.93, 15.42]

WORDS = [
    ("um", 0.20, 0.45),
    ("let", 0.55, 0.75),
    ("me", 0.78, 0.90),
    ("start", 0.95, 1.30),
    ("over", 1.30, 1.70),
    ("so", 2.10, 2.30),
    ("this", 2.35, 2.60),
    ("is", 2.62, 2.75),
    ("my", 2.78, 2.90),
    ("sunday", 2.95, 3.40),
    ("reset", 3.40, 3.95),
]

_CAPABILITIES = {
    "text_elements": True,
    "timeline": True,
    "clips": {"transitions": {"editable": True}},
    "music_operations": {"remove": True},
    "mix": True,
}


def author_variant() -> dict[str, Any]:
    return {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "resolved_archetype": "montage",
        "mix": 0.5,
        "music_track_id": TRACK_ID,
        "music_start_s": MUSIC_START_S,
        "text_elements": [
            {
                "id": "title",
                "role": "title",
                "text": "Sunday reset",
                "start_s": 0.0,
                "end_s": 3.0,
                "font_family": "Inter",
                "size_px": 72,
                "color": "#FFFFFF",
                "effect": "static",
                "alignment": "center",
                "position": "middle",
            }
        ],
        "caption_cues": [
            {
                "id": "cue-1",
                "text": "um let me start over",
                "start_s": 0.2,
                "end_s": 1.7,
                "words": [
                    {"text": text, "start_s": start, "end_s": end} for text, start, end in WORDS[:5]
                ],
            },
            {
                "id": "cue-2",
                "text": "so this is my sunday reset",
                "start_s": 2.1,
                "end_s": 3.95,
                "words": [
                    {"text": text, "start_s": start, "end_s": end} for text, start, end in WORDS[5:]
                ],
            },
        ],
        "ai_timeline": {
            "beat_grid": [],
            "slots": [
                {
                    "slot_id": "F1",
                    "clip_index": 0,
                    "source_gcs_path": "users/u/a.mp4",
                    "source_duration_s": 8.0,
                    "in_s": 0.0,
                    "duration_beats": None,
                    "duration_s": 6.0,
                    "output_start_s": 0.0,
                    "output_end_s": 6.0,
                    "removed": False,
                    "transition_after": "cut",
                },
                {
                    "slot_id": "F2",
                    "clip_index": 1,
                    "source_gcs_path": "users/u/b.mp4",
                    "source_duration_s": 5.0,
                    "in_s": 0.0,
                    "duration_beats": None,
                    "duration_s": 4.0,
                    "output_start_s": 6.0,
                    "output_end_s": 10.0,
                    "removed": False,
                    "transition_after": "cut",
                },
            ],
        },
    }


def author_snapshot(*, timeline_dirty: bool = False) -> dict[str, Any]:
    """The snapshot the planner would hand the copilot for `author_variant`."""
    from types import SimpleNamespace

    from app.services.kria_editor_ops import build_editor_snapshot

    variant = author_variant()
    job = SimpleNamespace(
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["users/u/a.mp4", "users/u/b.mp4"]},
    )
    context: dict[str, Any] = {"music_beats": {"track_id": TRACK_ID, "beats": SONG_BEATS}}
    if timeline_dirty:
        context["timeline_dirty"] = True
    with patch(
        "app.services.kria_editor_ops._editor_capabilities", lambda _job, _variant: _CAPABILITIES
    ):
        return json.loads(json.dumps(build_editor_snapshot(job, variant, clip_context=context)))


def _turn(turn_id: str, message: str, ops: list[dict[str, Any]], reply: str) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "user_message": message,
        "engine": "v2_kria",
        "kria": {
            "cassette": {
                "agent_input": {"utterance": message, "variant_snapshot": author_snapshot()},
                "model_output": {
                    "intent": "edit",
                    "ops": ops,
                    "confidence": 0.95,
                    "reply": reply,
                    "suggestions": [],
                    "needs_clarification": False,
                },
            }
        },
    }


def _plan_clips(first_in_s: float = 0.0) -> list[dict[str, Any]]:
    first_start = 0.0
    return [
        {
            "clip_id": "F1",
            "start_s": first_start,
            "end_s": 6.0 - first_in_s,
            "source_start_s": first_in_s,
        },
        {
            "clip_id": "F2",
            "start_s": 6.0 - first_in_s,
            "end_s": 10.0 - first_in_s,
            "source_start_s": 0.0,
        },
    ]


def speech_cut_fixture() -> dict[str, Any]:
    message = "Cut the part where I say 'um let me start over'."
    return {
        "fixture_id": "v2_speech_cut_at_word",
        "provenance": "authored",
        "footage": "food_day",
        "description": (
            "Deterministic v2 cassette over a snapshot the real builder produced: the creator "
            "names a spoken phrase, the model copies the phrase's last word end from SPEECH "
            "WORDS, and the compiler trims the front there. Compile-only evidence."
        ),
        "turns": [
            _turn(
                "cut-the-phrase",
                message,
                [{"op": "trim_output_start", "start_s": 1.7}],
                "Cut the opening through 'over', so the video now starts at 'so'.",
            )
        ],
        "requirements": [
            {
                "id": "cut-at-word-end",
                "kind": "timing",
                "scope": "clip:F1",
                "request_type": "restructure",
                "checker": "clip_source_start",
                "params": {"clips": {"F1": 1.7}},
                "source": message,
                "introduced_in_turn": 0,
            }
        ],
        "reference": {"plan_after": {"clips": _plan_clips(1.7), "texts": []}},
    }


def beat_title_fixture() -> dict[str, Any]:
    message = "Move the title to the first beat."
    first_beat = round(SONG_BEATS[1] - MUSIC_START_S, 3)
    return {
        "fixture_id": "v2_title_on_first_beat",
        "provenance": "authored",
        "footage": "food_day",
        "description": (
            "Deterministic v2 cassette over a snapshot the real builder produced: the song "
            "beats arrive shifted into timeline seconds, the model copies the first one, and "
            "the compiler retimes the title bar there. Compile-only evidence."
        ),
        "turns": [
            _turn(
                "title-on-first-beat",
                message,
                [
                    {
                        "op": "set_text_timing",
                        "bar_index": 0,
                        "start_s": first_beat,
                        "end_s": round(first_beat + 3.0, 3),
                    }
                ],
                f"Moved the title to the first beat at {first_beat:.2f}s, keeping its length.",
            )
        ],
        "requirements": [
            {
                "id": "title-on-first-beat",
                "kind": "timing",
                "scope": "title",
                "request_type": "style",
                "checker": "text_geometry",
                "params": {
                    "texts": {"title": {"start_s": first_beat, "end_s": round(first_beat + 3.0, 3)}}
                },
                "source": message,
                "introduced_in_turn": 0,
            }
        ],
        "reference": {
            "plan_after": {
                "clips": _plan_clips(),
                "texts": [
                    {
                        "id": "title",
                        "role": "title",
                        "text": "Sunday reset",
                        "start_s": first_beat,
                        "end_s": round(first_beat + 3.0, 3),
                    }
                ],
            }
        },
    }


def main() -> None:
    for fixture in (speech_cut_fixture(), beat_title_fixture()):
        path = THREAD_DIR / f"{fixture['fixture_id']}.json"
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
