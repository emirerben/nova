from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from scripts.generate_kria_format_matrix import TOKENS, build_rows

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/kria_format_matrix.jsonl"

UNIVERSAL_CATEGORIES = {
    "cancellation",
    "commit",
    "creation",
    "cross_device",
    "partial_failure",
    "queued",
    "reconciliation",
    "render",
    "retry",
    "review",
    "rollback",
    "select",
    "stale",
    "status",
    "undo",
}

REQUIRED_OPERATIONS = {
    "montage": {
        "reorder_clip",
        "remove_clip",
        "trim_clip_start",
        "split_clip",
        "set_media_duration",
        "edit_text",
        "set_title",
        "remove_music",
        "set_mix",
        "set_transition",
        "set_look_preset",
        "add_unused_sources",
    },
    "day_vlog": {
        "reorder_clip",
        "remove_clip",
        "trim_clip_start",
        "split_clip",
        "set_clip_duration",
        "edit_text",
        "set_title",
        "set_transition",
        "set_look_preset",
        "add_unused_sources",
        "set_media_duration",
        "stack_images",
    },
    "single_hero": set(),
    "talking_head": {
        "edit_caption",
        "replace_caption_text",
        "set_caption_timing",
        "set_caption_meta",
        "set_caption_emphasis",
        "apply_speech_cut_candidate",
        "trim_output_start",
        "set_mix",
        "add_unused_sources",
        "set_media_duration",
        "stack_images",
    },
    "subtitled": set(),
    "narrated": {
        "voiceover_readiness",
        "ask_voiceover",
        "reorder_clip",
        "remove_clip",
        "trim_clip_start",
        "set_clip_duration",
        "edit_text",
        "set_title",
        "set_mix",
        "remove_music",
        "add_unused_sources",
        "set_media_duration",
        "stack_images",
    },
    "narrated_planned": set(),
    "narrated_ready": set(),
}
REQUIRED_OPERATIONS["single_hero"] = REQUIRED_OPERATIONS["day_vlog"]
REQUIRED_OPERATIONS["subtitled"] = REQUIRED_OPERATIONS["talking_head"]
REQUIRED_OPERATIONS["narrated_planned"] = REQUIRED_OPERATIONS["narrated"]
REQUIRED_OPERATIONS["narrated_ready"] = REQUIRED_OPERATIONS["narrated"]


def _rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_checked_format_matrix_matches_generator() -> None:
    assert _rows() == build_rows()


def test_every_canonical_format_has_thirty_independent_structural_cases() -> None:
    rows = _rows()
    counts = Counter(row["format_token"] for row in rows)
    assert counts == {token: 30 for token in TOKENS}
    assert len({row["case_id"] for row in rows}) == 240
    assert all(row["structural_only"] is True for row in rows)
    assert all(row["contains_media"] is False for row in rows)


def test_every_format_freezes_universal_recovery_and_required_operation_coverage() -> None:
    by_format: dict[str, list[dict]] = defaultdict(list)
    for row in _rows():
        by_format[row["format_token"]].append(row)

    for token in TOKENS:
        categories = {row["category"] for row in by_format[token]}
        operations = {row["expected_operation"] for row in by_format[token]}
        assert UNIVERSAL_CATEGORIES <= categories
        assert REQUIRED_OPERATIONS[token] <= operations
