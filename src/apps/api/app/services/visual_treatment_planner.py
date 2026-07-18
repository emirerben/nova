"""Deterministic guardrails and materialization for visual-treatment output."""

from __future__ import annotations

import math
import re
import unicodedata
import uuid

from app.agents.visual_treatment_planner import RawVisualTreatment

_CARD_MAX_COVERAGE = 0.18
_ALL_MAX_COVERAGE = 0.35


def _overlaps(intervals: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(start < old_end and end > old_start for old_start, old_end in intervals)


def _normalized_copy(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold().replace("%", " percent ")
    number_words = {
        "zero": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
        "ten": "10",
        "twenty": "20",
        "thirty": "30",
        "forty": "40",
        "fifty": "50",
        "sixty": "60",
        "seventy": "70",
        "eighty": "80",
        "ninety": "90",
        "hundred": "100",
    }
    return " ".join(
        number_words.get(token, token) for token in re.findall(r"\w+", folded, flags=re.UNICODE)
    )


def _card_copy_is_transcript_grounded(copy: str, transcript_text: str | None) -> bool:
    """Fail closed: semantic cards may only quote a contiguous transcript span."""
    if not transcript_text:
        return False
    copy_tokens = _normalized_copy(copy).split()
    transcript_tokens = _normalized_copy(transcript_text).split()
    if not copy_tokens:
        return False
    cursor = 0
    for token in copy_tokens:
        try:
            cursor = transcript_tokens.index(token, cursor) + 1
        except ValueError:
            return False
    return True


def build_visual_treatments(
    raw: list[RawVisualTreatment],
    *,
    assets_by_id: dict[str, dict],
    duration_s: float,
    transcript_text: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return validated-shape blocks + linked TextElements after AI caps."""
    blocks: list[dict] = []
    text_elements: list[dict] = []
    occupied: list[tuple[float, float]] = []
    card_windows: list[tuple[float, float]] = []
    montage_count = 0
    card_limit = min(4, max(1, math.ceil(duration_s / 15.0)))

    for proposal in sorted(raw, key=lambda row: (row.start_s, row.end_s)):
        if proposal.kind == "footage" or proposal.confidence == "low":
            continue
        start = max(0.0, min(duration_s, proposal.start_s))
        end = max(start, min(duration_s, proposal.end_s))
        if _overlaps(occupied, start, end):
            continue

        if proposal.kind == "montage":
            if montage_count >= 2:
                continue
            end = min(end, start + 6.0)
            if end - start < 1.2:
                continue
            asset_ids = [asset_id for asset_id in proposal.asset_ids if asset_id in assets_by_id]
            asset_ids = list(dict.fromkeys(asset_ids))[:10]
            if len(asset_ids) < 3:
                continue
            block_id = uuid.uuid4().hex
            shot_duration = (end - start) / len(asset_ids)
            shots: list[dict] = []
            offset = 0.0
            motions = ("zoom_in", "pan_right", "zoom_out", "pan_left")
            for index, asset_id in enumerate(asset_ids):
                asset = assets_by_id[asset_id]
                duration = end - start - offset if index == len(asset_ids) - 1 else shot_duration
                shots.append(
                    {
                        "id": uuid.uuid4().hex,
                        "asset_id": asset_id,
                        "src_gcs_path": asset["gcs_path"],
                        "kind": asset["kind"],
                        "start_offset_s": round(offset, 6),
                        "duration_s": round(duration, 6),
                        "crop": {"x_frac": 0.5, "y_frac": 0.5, "scale": 1.0},
                        "motion": motions[index % len(motions)],
                    }
                )
                offset += duration
            blocks.append(
                {
                    "version": 1,
                    "id": block_id,
                    "kind": "montage",
                    "start_s": start,
                    "end_s": end,
                    "timing_mode": "auto",
                    "origin": "ai",
                    "rationale": proposal.rationale,
                    "transition_in": "cut",
                    "transition_out": "cut",
                    "audio_policy": {"base": "continue", "sfx": "continue"},
                    "shots": shots,
                }
            )
            montage_count += 1
        elif proposal.kind == "text_card":
            if (
                len(card_windows) >= card_limit
                or not proposal.text
                or not _card_copy_is_transcript_grounded(proposal.text, transcript_text)
            ):
                continue
            end = min(end, start + 4.0)
            if end - start < 0.75:
                continue
            card_coverage = sum(e - s for s, e in card_windows) + (end - start)
            if card_coverage > duration_s * _CARD_MAX_COVERAGE:
                continue
            if any(abs(start - prior_end) < 3.0 for _prior_start, prior_end in card_windows):
                continue
            block_id = uuid.uuid4().hex
            blocks.append(
                {
                    "version": 1,
                    "id": block_id,
                    "kind": "text_card",
                    "start_s": start,
                    "end_s": end,
                    "timing_mode": "auto",
                    "origin": "ai",
                    "rationale": proposal.rationale,
                    "transition_in": "cut",
                    "transition_out": "cut",
                    "audio_policy": {"base": "continue", "sfx": "continue"},
                    "style_preset_id": f"nova-{proposal.purpose}",
                    "background": {"type": "solid", "color": "#26382F"},
                }
            )
            text_elements.append(
                {
                    "id": uuid.uuid4().hex,
                    "text": proposal.text[:500],
                    "start_s": start,
                    "end_s": end,
                    "role": "generative_intro",
                    "visual_block_id": block_id,
                    "position": "middle",
                    "font_family": "PlayfairDisplay-Bold",
                    "size_px": 72,
                    "color": "#FFFFFF",
                    "alignment": "center",
                    "effect": "fade-in",
                    "max_width_frac": 0.82,
                }
            )
            card_windows.append((start, end))
        else:
            continue

        occupied.append((start, end))
        if sum(e - s for s, e in occupied) > duration_s * _ALL_MAX_COVERAGE:
            occupied.pop()
            removed = blocks.pop()
            if removed["kind"] == "montage":
                montage_count -= 1
            else:
                card_windows.pop()
                text_elements = [
                    element
                    for element in text_elements
                    if element.get("visual_block_id") != removed["id"]
                ]

    return blocks, text_elements
