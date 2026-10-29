"""Render-only caption grouping; authored guided cue identities stay untouched."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'’”)\]]*$")


def _sentence_groups(captions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep every spoken sentence together without changing cue receipts.

    A terminal mark must close the token, so decimal tokens such as ``172.5``
    never split a sentence. Closing quotes/brackets are accepted.
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in captions:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        current.append(row)
        # A lyric line ends here even without punctuation (KRI-569).
        if _SENTENCE_END_RE.search(text) or row.get("caption_line_end") is True:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _legacy_groups(captions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Retain the pre-presentation grouping for word-mode compatibility."""
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
            or group[-1].get("caption_line_end") is True
            or len(combined.split()) > 6
            or len(combined) > 42
        ):
            groups.append([row])
        else:
            group.append(row)
    return groups


def project_guided_caption_overlays(
    overlays: list[dict[str, Any]], meta: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Project display-only grouping while keeping canonical cue IDs/timing.

    Sentence display repeats the complete sentence across each source cue's
    adjacent interval. The intervals meet at the next spoken word, preventing
    a mid-sentence flicker while the persisted per-word elements stay intact.
    """
    if not meta:
        for row in overlays:
            row.pop("caption_line_end", None)
        return overlays
    captions = sorted(
        (row for row in overlays if row.get("role") == "generative_narration_caption"),
        key=lambda row: (float(row["start_s"]), str(row.get("element_id", ""))),
    )
    sentence_style = meta.get("style") == "sentence"
    groups = _sentence_groups(captions) if sentence_style else _legacy_groups(captions)
    output = [row for row in overlays if row.get("role") != "generative_narration_caption"]
    word_style = meta.get("style") == "word"
    appearance = meta.get("appearance") or {}
    highlight = appearance.get("highlight_spoken_word") is True
    from app.services.phone_rollout import caption_animation_renderable

    animate = caption_animation_renderable()
    for group in groups:
        group_words = [word for row in group for word in str(row["text"]).split()]
        display_text = " ".join(group_words)
        phases = _group_phases(group, meta) if animate else None
        appearance_phases = appearance.get("animation_phases")
        if phases is None and appearance_phases is not None and not word_style and not highlight:
            row = deepcopy(group[0])
            row.update(
                text=display_text,
                end_s=group[-1]["end_s"],
                effect="static",
                animation_phases=appearance_phases,
            )
            row.pop("word_timings", None)
            output.append(row)
            continue
        offset = 0
        first_output = len(output)
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
                row.update(text=display_text, start_s=start, end_s=end, effect="static")
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
                        text=word if word_style else display_text,
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
        if phases is not None:
            # Display Word draws one word per row, and each word is a line of its own.
            rows = output[first_output:]
            for line in ([row] for row in rows) if word_style else [rows]:
                _stamp_line_phases(line, phases)
    for row in output:
        row.pop("caption_line_end", None)
    return output


def _group_phases(group: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any] | None:
    """The displayed line's animation (KRI-1007): a row's own phases, else the meta default.

    Phases that animate nothing are today's static line, so they yield None.
    """
    from pydantic import ValidationError

    from app.agents._schemas.text_animation_phases import TextAnimationPhases

    raw = next(
        (row["animation_phases"] for row in group if row.get("animation_phases") is not None),
        meta.get("animation_phases"),
    )
    if raw is None:
        return None
    try:
        phases = TextAnimationPhases.model_validate(raw)
    except ValidationError:
        return None
    if phases.entrance == phases.exit == phases.loop == "none":
        return None
    return phases.model_dump()


def _stamp_line_phases(rows: list[dict[str, Any]], phases: dict[str, Any]) -> None:
    """Animate the line, not each interval: the first row enters, the last exits, all loop."""
    for index, row in enumerate(rows):
        row_phases = {
            **phases,
            "entrance": phases["entrance"] if index == 0 else "none",
            "exit": phases["exit"] if index == len(rows) - 1 else "none",
        }
        if row_phases["entrance"] == row_phases["exit"] == row_phases["loop"] == "none":
            continue
        row["animation_phases"] = row_phases
        if row.get("effect") != "karaoke-line":
            row["effect"] = (
                "typewriter"
                if "typewriter" in (row_phases["entrance"], row_phases["exit"])
                else "none"
            )
