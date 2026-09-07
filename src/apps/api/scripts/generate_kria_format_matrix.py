#!/usr/bin/env python3
"""Generate the checked, non-media Kria all-format behavioral corpus."""

from __future__ import annotations

import json
from pathlib import Path

TOKENS = (
    "montage",
    "talking_head",
    "day_vlog",
    "single_hero",
    "subtitled",
    "narrated",
    "narrated_planned",
    "narrated_ready",
)

UNIVERSAL = (
    ("creation", "Build the first cut around the strongest opening.", "strategy"),
    ("undo", "Undo the last draft change.", "undo"),
    ("commit", "Save these draft changes together.", "commit"),
    ("render", "Render this exact cut.", "render"),
    ("status", "What is happening with the render?", "status"),
    ("review", "Review the finished cut for hook and continuity.", "review"),
    ("retry", "Retry the saved edit without changing it.", "retry"),
    ("select", "Use the second ready variant.", "select"),
    ("stale", "Apply this after another device changed the draft.", "stale_recovery"),
    ("cross_device", "Continue the draft I changed on my phone.", "refresh_replan"),
    ("queued", "After this render, make the opening shorter.", "queue_successor"),
    ("cancellation", "Cancel the queued revision.", "cancel"),
    ("partial_failure", "Keep the valid text edit if the music change fails.", "partial_recovery"),
    ("reconciliation", "Check whether the interrupted render was actually queued.", "reconcile"),
    ("rollback", "Open this project with the previous runtime safely.", "rollback_read"),
)

FORMAT_CASES = {
    "montage": (
        ("reorder", "Put the product reveal first.", "reorder_clip"),
        ("remove", "Remove the third clip.", "remove_clip"),
        ("trim", "Trim half a second from the first clip.", "trim_clip_start"),
        ("split", "Split the second clip at two seconds.", "split_clip"),
        ("duration", "Make every image last one and a half seconds.", "set_media_duration"),
        ("text", "Change the hook to Fresh matcha, finally.", "edit_text"),
        ("title", "Rename this edit Matcha launch day.", "set_title"),
        ("text_timing", "Keep the hook on screen until three seconds.", "set_text_timing"),
        ("music", "Remove the song and keep the original sound.", "remove_music"),
        ("mix", "Turn the music down to thirty percent.", "set_mix"),
        (
            "music_recovery",
            "Use another licensed song if this one is unavailable.",
            "music_recovery",
        ),
        ("transition", "Use a crossfade after the first clip.", "set_transition"),
        ("hard_cut", "Make the second transition a hard cut.", "set_transition"),
        ("look", "Give the opening clip the stadium diffusion look.", "set_look_preset"),
        (
            "mixed_media",
            "Add every unused image and group the images together.",
            "add_unused_sources",
        ),
    ),
    "day_vlog": (),
    "single_hero": (),
    "talking_head": (),
    "subtitled": (),
    "narrated": (),
    "narrated_planned": (),
    "narrated_ready": (),
}

GUIDED = (
    ("reorder", "Move the café arrival before the walk.", "reorder_clip"),
    ("remove", "Remove the repeated packing beat.", "remove_clip"),
    ("trim", "Shorten the opening establishing shot.", "trim_clip_start"),
    ("split", "Split the product demonstration into two beats.", "split_clip"),
    ("duration", "Hold the finished product for two seconds.", "set_clip_duration"),
    ("text", "Change the opening text to A quiet launch morning.", "edit_text"),
    ("title", "Call this story Launch morning.", "set_title"),
    ("text_timing", "End the opening title at two seconds.", "set_text_timing"),
    ("transition", "Fade into the final reveal.", "set_transition"),
    ("hard_cut", "Use a hard cut into the preparation beat.", "set_transition"),
    ("look", "Use the warm look on the outdoor beat.", "set_look_preset"),
    ("look_clear", "Remove the look from the final clip.", "set_look_preset"),
    ("mixed_add", "Add all unused photos to the story.", "add_unused_sources"),
    ("mixed_duration", "Make every photo one second.", "set_media_duration"),
    ("mixed_stack", "Group the photos into one continuous sequence.", "stack_images"),
)

SPEECH = (
    ("caption_text", "Change the first caption from Kriya to Kria.", "edit_caption"),
    ("caption_replace", "Replace every Kriya caption with Kria.", "replace_caption_text"),
    ("caption_timing", "Keep the second caption up for another half second.", "set_caption_timing"),
    ("caption_style", "Use word-by-word captions.", "set_caption_meta"),
    ("caption_position", "Move the captions slightly higher.", "set_caption_meta"),
    ("caption_emphasis", "Emphasize the payoff caption.", "set_caption_emphasis"),
    ("speech_cut", "Apply the validated retake cut.", "apply_speech_cut_candidate"),
    ("output_trim", "Remove the first second of the video.", "trim_output_start"),
    ("mix", "Turn the background music down to twenty percent.", "set_mix"),
    ("mix_raise", "Raise the music to forty percent.", "set_mix"),
    ("caption_disable", "Turn captions off for this version.", "set_caption_meta"),
    ("caption_enable", "Turn captions back on.", "set_caption_meta"),
    ("mixed_add", "Add every unused image after the talking section.", "add_unused_sources"),
    ("mixed_duration", "Make those images last one second each.", "set_media_duration"),
    ("mixed_stack", "Keep the supporting images together.", "stack_images"),
)

NARRATED = (
    ("voiceover", "Use the voiceover I recorded for this cut.", "voiceover_readiness"),
    ("voiceover_missing", "Render this narrated edit before I record audio.", "ask_voiceover"),
    ("reorder", "Put the pouring shot under the first sentence.", "reorder_clip"),
    ("remove", "Remove the duplicate product shot.", "remove_clip"),
    ("trim", "Trim the supporting café clip shorter.", "trim_clip_start"),
    ("duration", "Hold the final product shot for two seconds.", "set_clip_duration"),
    ("text", "Change the hook to Why I started Kinocha.", "edit_text"),
    ("title", "Call this edit The Kinocha story.", "set_title"),
    ("text_timing", "End the title after three seconds.", "set_text_timing"),
    ("mix", "Lower the background music beneath my voice.", "set_mix"),
    ("music_remove", "Remove the background music.", "remove_music"),
    (
        "music_recovery",
        "Choose a quieter licensed bed if this one is unavailable.",
        "music_recovery",
    ),
    ("mixed_add", "Add all unused product photos.", "add_unused_sources"),
    ("mixed_duration", "Make every product photo last one second.", "set_media_duration"),
    ("mixed_stack", "Group the product photos into a short sequence.", "stack_images"),
)

for token in ("day_vlog", "single_hero"):
    FORMAT_CASES[token] = GUIDED
for token in ("talking_head", "subtitled"):
    FORMAT_CASES[token] = SPEECH
for token in ("narrated", "narrated_planned", "narrated_ready"):
    FORMAT_CASES[token] = NARRATED


def build_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for token in TOKENS:
        cases = (*UNIVERSAL, *FORMAT_CASES[token])
        assert len(cases) == 30
        for index, (category, utterance, operation) in enumerate(cases, start=1):
            rows.append(
                {
                    "case_id": f"{token}-{index:02d}-{category}",
                    "format_token": token,
                    "category": category,
                    "utterance": utterance,
                    "expected_operation": operation,
                    "manifest_supported": True,
                    "structural_only": True,
                    "contains_media": False,
                }
            )
    return rows


if __name__ == "__main__":
    output = Path(__file__).resolve().parents[1] / "tests/fixtures/kria_format_matrix.jsonl"
    output.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in build_rows()),
        encoding="utf-8",
    )
    print(f"Wrote {len(build_rows())} cases to {output}")
