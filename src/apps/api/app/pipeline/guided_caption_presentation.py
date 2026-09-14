"""Render-only caption grouping; authored guided cue identities stay untouched."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def project_guided_caption_overlays(
    overlays: list[dict[str, Any]], meta: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not meta:
        return overlays
    captions = sorted(
        (row for row in overlays if row.get("role") == "generative_narration_caption"),
        key=lambda row: (float(row["start_s"]), str(row.get("element_id", ""))),
    )
    groups: list[list[dict[str, Any]]] = []
    for row in captions:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        group = groups[-1] if groups else []
        combined = " ".join([*(str(item["text"]).strip() for item in group), text])
        if not group or (
            float(row["start_s"]) - float(group[-1]["end_s"]) > 0.35
            or str(group[-1]["text"]).rstrip().endswith((".", "!", "?"))
            or len(combined.split()) > 6
            or len(combined) > 42
        ):
            groups.append([row])
        else:
            group.append(row)
    output = [row for row in overlays if row.get("role") != "generative_narration_caption"]
    word_style = meta.get("style") == "word"
    appearance = meta.get("appearance") or {}
    highlight = appearance.get("highlight_spoken_word") is True
    for group in groups:
        group_words = [word for row in group for word in str(row["text"]).split()]
        offset = 0
        for index, original in enumerate(group):
            words = str(original["text"]).split()
            start = float(original["start_s"])
            end = (
                float(group[index + 1]["start_s"])
                if index + 1 < len(group)
                else float(original["end_s"])
            )
            end = max(start + 0.01, end)
            if not word_style and not highlight:
                row = deepcopy(original)
                row.update(text=" ".join(group_words), start_s=start, end_s=end, effect="static")
                row.pop("word_timings", None)
                row.pop("animation_phases", None)
                output.append(row)
            else:
                # Use equal bounded windows after copy edits; stored source timings
                # are never overwritten by this presentation projection.
                for word_index, word in enumerate(words):
                    row = deepcopy(original)
                    begin = start + (end - start) * word_index / len(words)
                    stop = start + (end - start) * (word_index + 1) / len(words)
                    row.update(
                        text=word if word_style else " ".join(group_words),
                        start_s=begin,
                        end_s=stop,
                        effect="static",
                    )
                    row.pop("word_timings", None)
                    row.pop("animation_phases", None)
                    if highlight and word_style:
                        row["text_color"] = meta.get("highlight_color") or "#C5F82A"
                    elif highlight:
                        row["effect"] = "karaoke-line"
                        row["highlight_color"] = meta.get("highlight_color") or "#C5F82A"
                        row["word_timings"] = [
                            {
                                "text": token,
                                "start_s": 0
                                if position == offset + word_index
                                else stop - begin + 1,
                                "end_s": stop - begin + 2,
                            }
                            for position, token in enumerate(group_words)
                        ]
                    output.append(row)
            offset += len(words)
    return output
