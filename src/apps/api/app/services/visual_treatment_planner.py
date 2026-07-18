"""Deterministic guardrails and materialization for visual-treatment output."""

from __future__ import annotations

import math
import re
import unicodedata
import uuid

from app.agents.visual_treatment_planner import RawVisualTreatment

_CARD_MAX_COVERAGE = 0.18
_SECTION_CARD_MAX_COVERAGE = 0.25
_ALL_MAX_COVERAGE = 0.35
_MAX_SECTION_CARDS = 8
_MIN_SECTION_FOOTAGE_GAP_S = 0.75
_MAX_SECTION_CARD_DURATION_S = 4.0


def _overlaps(intervals: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(start < old_end and end > old_start for old_start, old_end in intervals)


def _uncovered_footage_duration(
    start: float, end: float, occupied: list[tuple[float, float]]
) -> float:
    """Return talking-head time not replaced by another accepted treatment."""
    if end <= start:
        return 0.0
    covered = sum(
        max(0.0, min(end, old_end) - max(start, old_start)) for old_start, old_end in occupied
    )
    return max(0.0, end - start - covered)


_NUMBER_WORDS = {
    # English cardinals + ordinals.
    "zero": "0",
    "one": "1",
    "first": "1",
    "two": "2",
    "second": "2",
    "three": "3",
    "third": "3",
    "four": "4",
    "fourth": "4",
    "five": "5",
    "fifth": "5",
    "six": "6",
    "sixth": "6",
    "seven": "7",
    "seventh": "7",
    "eight": "8",
    "eighth": "8",
    "nine": "9",
    "ninth": "9",
    "ten": "10",
    "tenth": "10",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
    # Turkish cardinals + ordinals. `casefold()` preserves dotted/dotless i,
    # so list every real spelling rather than stripping diacritics.
    "sıfır": "0",
    "bir": "1",
    "ilk": "1",
    "birinci": "1",
    "iki": "2",
    "ikinci": "2",
    "üç": "3",
    "üçüncü": "3",
    "dört": "4",
    "dördüncü": "4",
    "beş": "5",
    "beşinci": "5",
    "altı": "6",
    "altıncı": "6",
    "yedi": "7",
    "yedinci": "7",
    "sekiz": "8",
    "sekizinci": "8",
    "dokuz": "9",
    "dokuzuncu": "9",
    "on": "10",
    "onuncu": "10",
}


def _legacy_normalized_tokens(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", value).casefold().replace("%", " percent ")
    legacy_number_words = {
        key: mapped
        for key, mapped in _NUMBER_WORDS.items()
        if key
        in {
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "twenty",
            "thirty",
            "forty",
            "fifty",
            "sixty",
            "seventy",
            "eighty",
            "ninety",
            "hundred",
        }
    }
    return [
        legacy_number_words.get(token, token)
        for token in re.findall(r"\w+", folded, flags=re.UNICODE)
    ]


def _section_normalized_tokens(value: str) -> list[str]:
    folded = (
        unicodedata.normalize("NFKC", value)
        .casefold()
        # Unicode casefold maps Turkish capital İ to `i` + COMBINING DOT,
        # which `\w+` otherwise splits into `i`, `ki`. Remove only that
        # redundant dot; preserve meaningful Turkish diacritics such as ü/ş/ğ.
        .replace("\u0307", "")
        .replace("%", " percent ")
    )
    return [
        _NUMBER_WORDS.get(token, token) for token in re.findall(r"\w+", folded, flags=re.UNICODE)
    ]


def _normalized_copy(value: str, *, section_item: bool = False) -> str:
    tokens = _section_normalized_tokens(value) if section_item else _legacy_normalized_tokens(value)
    return " ".join(tokens)


def _card_copy_is_transcript_grounded(
    copy: str, transcript_text: str | None, *, section_item: bool = False
) -> bool:
    """Fail closed: semantic-card tokens must appear in transcript order."""
    if not transcript_text:
        return False
    copy_tokens = _normalized_copy(copy, section_item=section_item).split()
    transcript_tokens = _normalized_copy(transcript_text, section_item=section_item).split()
    if not copy_tokens:
        return False
    cursor = 0
    for token in copy_tokens:
        try:
            cursor = transcript_tokens.index(token, cursor) + 1
        except ValueError:
            return False
    return True


def _card_copy_span_in_window(
    copy: str,
    words: list[dict] | None,
    *,
    start_s: float,
    end_s: float,
    tolerance_s: float = 0.35,
) -> tuple[float, float] | None:
    """Return the local contiguous timed span supporting ``copy``.

    Matching is deliberately restricted to the proposed treatment window. A
    repeated title in a hook or preview therefore cannot pull a later section
    card back to the wrong occurrence. When the same title appears more than
    once inside that local window, prefer the later occurrence so a preview
    cannot beat the subsequently announced item even for a broad model window.
    """
    copy_tokens = _section_normalized_tokens(copy)
    if not copy_tokens or not words:
        return None
    timed_tokens: list[tuple[str, float, float]] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        try:
            word_start = float(word.get("start_s", 0.0))
            word_end = float(word.get("end_s", word_start))
        except (TypeError, ValueError):
            continue
        if word_start > end_s + tolerance_s or word_end < start_s - tolerance_s:
            continue
        value = str(word.get("word") or word.get("text") or "").strip()
        for token in _section_normalized_tokens(value):
            timed_tokens.append((token, word_start, word_end))
    matches: list[tuple[float, float]] = []
    width = len(copy_tokens)
    for index in range(0, len(timed_tokens) - width + 1):
        candidate = timed_tokens[index : index + width]
        if [token for token, _start, _end in candidate] == copy_tokens:
            matches.append((candidate[0][1], candidate[-1][2]))
    if not matches:
        return None
    return max(matches, key=lambda span: span[0])


def _transcript_text_in_window(
    words: list[dict] | None,
    *,
    start_s: float,
    end_s: float,
    tolerance_s: float = 0.35,
) -> str | None:
    """Return only timed transcript words overlapping a treatment window."""
    if not words:
        return None
    selected: list[str] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        try:
            word_start = float(word.get("start_s", 0.0))
            word_end = float(word.get("end_s", word_start))
        except (TypeError, ValueError):
            continue
        if word_start <= end_s + tolerance_s and word_end >= start_s - tolerance_s:
            value = str(word.get("word") or word.get("text") or "").strip()
            if value:
                selected.append(value)
    return " ".join(selected) or None


def build_visual_treatments(
    raw: list[RawVisualTreatment],
    *,
    assets_by_id: dict[str, dict],
    duration_s: float,
    transcript_text: str | None = None,
    transcript_words: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return validated-shape blocks + linked TextElements after AI caps."""
    blocks: list[dict] = []
    text_elements: list[dict] = []
    occupied: list[tuple[float, float]] = []
    card_windows: list[tuple[float, float]] = []
    generic_card_windows: list[tuple[float, float]] = []
    section_card_windows: list[tuple[float, float]] = []
    montage_count = 0
    card_limit = min(4, max(1, math.ceil(duration_s / 15.0)))

    for proposal in sorted(raw, key=lambda row: (row.start_s, row.end_s)):
        if proposal.kind == "footage" or proposal.confidence == "low":
            continue
        start = max(0.0, min(duration_s, proposal.start_s))
        end = max(start, min(duration_s, proposal.end_s))

        if proposal.kind == "montage":
            if montage_count >= 2:
                continue
            end = min(end, start + 6.0)
            if end - start < 1.2:
                continue
            if _overlaps(occupied, start, end):
                continue
            if sum(e - s for s, e in occupied) + (end - start) > duration_s * _ALL_MAX_COVERAGE:
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
            is_section = proposal.purpose == "section_item"
            grounded_transcript = (
                _transcript_text_in_window(transcript_words, start_s=start, end_s=end)
                if transcript_words is not None
                else transcript_text
            )
            if (
                (not is_section and len(generic_card_windows) >= card_limit)
                or (is_section and len(section_card_windows) >= _MAX_SECTION_CARDS)
                or not proposal.text
                or not _card_copy_is_transcript_grounded(
                    proposal.text,
                    grounded_transcript,
                    section_item=is_section,
                )
            ):
                continue
            if is_section:
                matched_span = _card_copy_span_in_window(
                    proposal.text,
                    transcript_words,
                    start_s=start,
                    end_s=end,
                )
                if matched_span is None:
                    continue
                start = max(0.0, min(duration_s, matched_span[0]))
                end = max(start, min(duration_s, matched_span[1]))
                if (
                    not math.isfinite(start)
                    or not math.isfinite(end)
                    or end - start < (1.0 / 30.0)
                    or end - start > _MAX_SECTION_CARD_DURATION_S
                ):
                    continue
            else:
                end = min(end, start + 4.0)
            if not is_section and end - start < 0.75:
                continue
            if _overlaps(occupied, start, end):
                continue
            if is_section:
                card_coverage = sum(e - s for s, e in section_card_windows) + (end - start)
                if card_coverage > duration_s * _SECTION_CARD_MAX_COVERAGE:
                    continue
                if (
                    section_card_windows
                    and _uncovered_footage_duration(section_card_windows[-1][1], start, occupied)
                    < _MIN_SECTION_FOOTAGE_GAP_S
                ):
                    continue
            else:
                card_coverage = sum(e - s for s, e in generic_card_windows) + (end - start)
                if card_coverage > duration_s * _CARD_MAX_COVERAGE:
                    continue
                if any(
                    abs(start - prior_end) < 3.0 for _prior_start, prior_end in generic_card_windows
                ):
                    continue
            if sum(e - s for s, e in occupied) + (end - start) > duration_s * _ALL_MAX_COVERAGE:
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
            (section_card_windows if is_section else generic_card_windows).append((start, end))
        else:
            continue

        occupied.append((start, end))

    return blocks, text_elements
