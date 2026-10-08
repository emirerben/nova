"""Text lane of the Kria copilot (KRI-219 Lane A): selectors, bulk ops, text diff.

Two halves that must agree:

* the PARSER half (``edit_copilot`` -> ``editor_ops_v2.text`` coerce) resolves a
  selector against the snapshot's ``text_bars`` and records ``target_ids`` +
  ``expected_count`` on the op;
* the COMPILER half (handlers below) re-resolves the same selector against the
  authoritative (pre-bundle) variant and raises ``KriaEditorOpError`` when the
  match set drifted, so a stale parse can never edit the wrong text.

Both halves build the same bar descriptors (``id``, ``text``, ``role``,
``clip_id``, ``removed``, ``start_s``) from their own source, then run ONE
resolver, so they cannot disagree about what "the labels" or "the title" means.

Circular-import rule: this module imports ``kria_editor_ops`` at top level, so
nothing that ``kria_editor_ops`` imports at top level may import this module at
top level (the lane module ``editor_ops_v2.text`` imports it lazily).
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
import uuid
from typing import Any

from app.schemas.guided_edit_revision import MAX_GUIDED_EDITOR_TEXT_ELEMENTS
from app.services.kria_editor_ops import (
    _ALLOWED_FONTS,
    _CLIP_LABEL_BAR_PREFIX,
    _CLIP_LABEL_MEDIA_PREFIX,
    _LABEL_STYLE_KEYS,
    KriaEditorOpError,
    _clip_label_links,
    _DraftState,
    _slot_duration,
    is_caption_text_bar,
)

MAX_TEXT_CHARS = 500
MAX_SELECTOR_LIST = 100
SELECTOR_KEYS = frozenset(
    {"ids", "bar_indexes", "group", "clip_ids", "clip_indexes", "contains", "equals"}
)
GROUPS = frozenset({"title", "labels", "free", "all"})
# Appearance fields the manual editor can set that patch_text accepts beyond the
# portable style fields (`edit_copilot._STYLE_PATCH_FIELDS`).
EXTRA_PATCH_FIELDS = frozenset({"animation_phases", "background_color", "behind_subject"})
PHASE_KEYS = frozenset({"entrance", "exit", "loop", "speed"})
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_DRIFT = "Text changed before this edit could be drafted"
# Style a new bar inherits from the bar it copies (a superset of the label lane's).
_COPY_STYLE_KEYS = (
    *_LABEL_STYLE_KEYS,
    "text_case",
    "letter_spacing",
    "line_spacing",
    "rotation_deg",
    "background_color",
    "animation_phases",
    "editor_preset",
    "stroke_color",
    "shadow_color",
    "shadow_opacity",
)


def _entrance_settle_s(row: dict[str, Any]) -> float:
    """Canonical phase timing for a text bar's entrance animation."""
    duration = max(0.0, float(row.get("end_s") or 0.0) - float(row.get("start_s") or 0.0))
    explicit = row.get("animation_phases")
    if isinstance(explicit, dict):
        from app.agents._schemas.text_animation_phases import TextAnimationPhases  # noqa: PLC0415
        from app.pipeline.text_animation_phases import phase_duration  # noqa: PLC0415

        try:
            phases = TextAnimationPhases(**explicit)
        except (TypeError, ValueError):
            return 0.0
        return phase_duration(phases, duration) if phases.entrance != "none" else 0.0

    # Legacy phone text uses the same renderer timing math as authored v2
    # motion, with default parameters when no motion object is present. Keep
    # this separate from explicit phase timing, which has its own envelope.
    effect = str(row.get("effect") or "none")
    if effect in {"none", "static"}:
        return 0.0
    from app.pipeline.text_motion_v2 import renderer_settle_duration_s  # noqa: PLC0415

    motion = row.get("motion")
    raw_motion = motion if isinstance(motion, dict) and motion.get("version") == 2 else {}
    return min(
        duration,
        renderer_settle_duration_s(effect, str(row.get("text") or ""), raw_motion),
    )


def _relation_source(state: _DraftState, relation: str) -> dict[str, Any] | None:
    if relation == "title":
        return _style_source(state, "title")
    return next((row for row in state.text if row.get("id") == relation), None)


def normalize_text_relations(state: _DraftState) -> None:
    """Resolve relative add-text placement/timing after every bundle op ran."""
    total = _variant_total(state)
    from app.agents.edit_copilot import _resolve_placement  # noqa: PLC0415

    for row in state.text:
        relation = row.pop("_after_animation_of", None)
        below = row.pop("_below", False)
        source_id = row.pop("_below_source_id", None)
        source = _relation_source(state, relation) if relation else None
        below_source = _relation_source(state, source_id) if source_id else None
        if relation and source is None:
            raise KriaEditorOpError(_DRIFT)
        if below and below_source is None:
            raise KriaEditorOpError(_DRIFT)
        if source is not None:
            row["start_s"] = round(
                float(source.get("start_s") or 0.0) + _entrance_settle_s(source), 3
            )
        if below_source is not None:
            source_position = below_source.get("position")
            source_y = below_source.get("y_frac")
            if isinstance(source_position, str) and source_y is None:
                source_y = _resolve_placement({"position": source_position}).get("y_frac")
            if not isinstance(source_y, (int, float)):
                source_y = {"top": 0.12, "middle": 0.5, "bottom": 0.85}.get(source_position, 0.5)
            target_y = float(source_y) + 0.1
            if target_y > 0.94:
                raise KriaEditorOpError("That text would fall outside the phone-safe area")
            row["position"] = "custom"
            row["y_frac"] = round(target_y, 3)
        if relation or below:
            start = float(row.get("start_s") or 0.0)
            end = float(row.get("end_s") or 0.0)
            if start < 0 or end <= start or (total > 0 and end > total + 1e-6):
                raise KriaEditorOpError("That text timing does not fit on the video")


# ----------------------------------------------------------------------- folding


def fold(text: str) -> str:
    """Turkish-safe fold: İ/ı -> i, diacritics stripped, casefolded, spaces collapsed."""
    from app.kria.brief_route import loose_text  # noqa: PLC0415

    return loose_text(text)


def _fold_map(text: str) -> tuple[str, list[int]]:
    """Per-character fold that remembers which source character each folded char came from."""
    chars: list[str] = []
    origin: list[int] = []
    for index, ch in enumerate(text):
        folded = " " if ch.isspace() else fold(ch)
        for out in folded:
            chars.append(out)
            origin.append(index)
    return "".join(chars), origin


def apply_replace(text: str, find: str, replacement: str) -> tuple[str, int]:
    """Replace every fold-insensitive occurrence of ``find`` ("Istanbul" hits "İstanbul")."""
    needle = _fold_map(find.strip())[0]
    if not needle:
        return text, 0
    haystack, origin = _fold_map(text)
    out: list[str] = []
    cursor = 0
    count = 0
    position = haystack.find(needle)
    while position != -1:
        start = origin[position]
        end = origin[position + len(needle) - 1] + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        if start >= cursor:
            out.append(text[cursor:start])
            out.append(replacement)
            cursor = end
            count += 1
        position = haystack.find(needle, position + len(needle))
    out.append(text[cursor:])
    return clean_text("".join(out)), count


def clean_text(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())


def contains_find(text: str, find: str) -> bool:
    needle = _fold_map(find.strip())[0]
    return bool(needle) and needle in _fold_map(text)[0]


# ------------------------------------------------------------------ bar descriptors


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def bars_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Descriptors for the snapshot's ``text_bars`` (what the model was shown)."""
    out: list[dict[str, Any]] = []
    for index, row in enumerate(snapshot.get("text_bars") or []):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        clip = row.get("clip_id")
        source_params = row.get("source_params")
        sequence_source_id = row.get("sequence_source_id")
        if not isinstance(sequence_source_id, str) and isinstance(source_params, dict):
            sequence_source_id = source_params.get("sequence_source_id")
        out.append(
            {
                "index": index,
                "id": row["id"],
                "text": str(row.get("text") or ""),
                "role": row.get("role"),
                "sequence_source_id": (
                    sequence_source_id if isinstance(sequence_source_id, str) else None
                ),
                "clip_id": clip if isinstance(clip, str) and clip else None,
                "removed": bool(row.get("removed")),
                "start_s": _number(row.get("start_s")),
                "caption": row.get("caption_cue") is True,
            }
        )
    return out


def bars_from_variant(job: Any, variant: dict[str, Any]) -> list[dict[str, Any]]:
    """Descriptors for the authoritative variant's text elements (compiler side)."""
    links = _clip_label_links(job, variant)
    out: list[dict[str, Any]] = []
    index = 0
    for row in variant.get("text_elements") or []:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if isinstance(row_id, str):
            source_params = row.get("source_params")
            sequence_source_id = row.get("sequence_source_id")
            if not isinstance(sequence_source_id, str) and isinstance(source_params, dict):
                sequence_source_id = source_params.get("sequence_source_id")
            out.append(
                {
                    "index": index,
                    "id": row_id,
                    "text": str(row.get("text") or ""),
                    "role": row.get("role"),
                    "sequence_source_id": (
                        sequence_source_id if isinstance(sequence_source_id, str) else None
                    ),
                    "clip_id": (links.get(row_id) or {}).get("clip_id"),
                    "removed": bool(row.get("removed")),
                    "start_s": _number(row.get("start_s")),
                    "caption": is_caption_text_bar(row),
                }
            )
        index += 1
    return out


def classify(bars: list[dict[str, Any]]) -> dict[str, str]:
    """Bar id -> "title" | "label" | "text".

    label = linked to a clip (or a ``clip-label-*`` bar). title = the guided
    title bar, else the earliest-starting non-label intro-role bar that starts
    within the first half second and was not added by chat.
    """
    live = [bar for bar in bars if not bar["removed"]]

    def is_label(bar: dict[str, Any]) -> bool:
        return bool(bar["clip_id"]) or str(bar["id"]).startswith(_CLIP_LABEL_BAR_PREFIX)

    title_id: str | None = None
    if any(bar["id"] == "guided-title" for bar in live):
        title_id = "guided-title"
    else:
        candidates = [
            bar
            for bar in live
            if not is_label(bar)
            and bar["role"] in (None, "title", "generative_intro", "generative_sequence")
            and (bar["role"] == "title" or not str(bar["id"]).startswith("kria-"))
            and bar["start_s"] is not None
            and bar["start_s"] <= 0.5
        ]
        if candidates:
            title_id = min(candidates, key=lambda bar: bar["start_s"])["id"]
    title_bar = next((bar for bar in live if bar["id"] == title_id), None)
    title_lineage = (
        (title_bar.get("sequence_source_id") or title_id) if title_bar is not None else None
    )
    return {
        bar["id"]: (
            "label"
            if is_label(bar)
            else "title"
            if bar["id"] == title_id
            or (title_lineage is not None and bar.get("sequence_source_id") == title_lineage)
            else "text"
        )
        for bar in bars
    }


def resolve_selector(bars: list[dict[str, Any]], selector: dict[str, Any]) -> list[str]:
    """Ids of the live bars matching every criterion in a NORMALIZED selector."""
    kinds = classify(bars)
    ids = set(selector.get("ids") or [])
    clips = set(selector.get("clip_ids") or [])
    group = selector.get("group")
    contains = fold(selector["contains"]) if selector.get("contains") else None
    equals = fold(selector["equals"]) if selector.get("equals") else None
    matched: list[str] = []
    for bar in bars:
        # Lyric lines are timing-locked to the song; the lyrics editor owns them.
        # Captions/narration are owned by the caption ops, never text selectors.
        if bar["removed"] or bar["role"] == "lyric_line" or bar.get("caption"):
            continue
        kind = kinds[bar["id"]]
        if "ids" in selector and bar["id"] not in ids:
            continue
        if group == "title" and kind != "title":
            continue
        if group == "labels" and kind != "label":
            continue
        if group == "free" and kind != "text":
            continue
        if "clip_ids" in selector and bar["clip_id"] not in clips:
            continue
        folded = fold(bar["text"])
        if contains is not None and contains not in folded:
            continue
        if equals is not None and folded != equals:
            continue
        matched.append(bar["id"])
    if not matched and group == "labels" and set(selector) <= {"group"}:
        # "them" after "add a caption to each clip": chat-added caption bars are free texts
        # (a clip that already has a label refuses a second linked bar), so the model's
        # "labels" selector would match nothing. Target those chat-added bars instead.
        matched = [
            bar["id"]
            for bar in bars
            if not bar["removed"]
            and not bar.get("caption")
            and bar["role"] != "lyric_line"
            and kinds[bar["id"]] == "text"
            and str(bar["id"]).startswith("kria-")
        ]
    return matched


def normalize_selector(raw: object, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a model selector; fold ``bar_indexes``/``clip_indexes`` into ids.

    Returns None for anything malformed or empty (a selector must name at least
    one criterion: "everything" is spelled ``{"group": "all"}``).
    """
    if not isinstance(raw, dict) or not raw or set(raw) - SELECTOR_KEYS:
        return None
    out: dict[str, Any] = {}

    def strings(key: str) -> list[str] | None:
        value = raw.get(key)
        if not isinstance(value, list) or not 0 < len(value) <= MAX_SELECTOR_LIST:
            return None
        if any(not isinstance(item, str) or not item or len(item) > 100 for item in value):
            return None
        return list(dict.fromkeys(value))

    def ints(key: str) -> list[int] | None:
        value = raw.get(key)
        if not isinstance(value, list) or not 0 < len(value) <= MAX_SELECTOR_LIST:
            return None
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            return None
        return list(dict.fromkeys(value))

    bars = bars_from_snapshot(snapshot)
    ids: list[str] = []
    if "ids" in raw:
        picked = strings("ids")
        if picked is None:
            return None
        ids.extend(picked)
    if "bar_indexes" in raw:
        picked_i = ints("bar_indexes")
        by_index = {bar["index"]: bar["id"] for bar in bars}
        if picked_i is None or any(i not in by_index for i in picked_i):
            return None
        ids.extend(by_index[i] for i in picked_i)
    if ids:
        out["ids"] = list(dict.fromkeys(ids))
    clip_ids: list[str] = []
    if "clip_ids" in raw:
        picked = strings("clip_ids")
        if picked is None:
            return None
        clip_ids.extend(picked)
    if "clip_indexes" in raw:
        picked_i = ints("clip_indexes")
        slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
        if picked_i is None:
            return None
        for i in picked_i:
            slot = slots[i] if 0 <= i < len(slots) else None
            media = slot.get("media_id") if isinstance(slot, dict) else None
            if not isinstance(media, str) or not media:
                return None
            clip_ids.append(media)
    if clip_ids:
        out["clip_ids"] = list(dict.fromkeys(clip_ids))
    if "group" in raw:
        if raw["group"] not in GROUPS:
            return None
        out["group"] = raw["group"]
    for key in ("contains", "equals"):
        if key in raw:
            value = raw[key]
            if not isinstance(value, str):
                return None
            value = clean_text(value)
            if not value or len(value) > 200:
                return None
            out[key] = value
    return out or None


def describe_selector(selector: dict[str, Any], *, find: str | None = None) -> str:
    """Creator-facing noun phrase for a zero-match clarification."""
    group = selector.get("group")
    base = {
        "labels": "any clip label",
        "title": "a title",
        "free": "any free text",
    }.get(str(group), "any text")
    parts = [base]
    if selector.get("clip_ids") and not selector.get("ids"):
        parts.append("on that clip")
    if selector.get("ids"):
        parts = ["that text"]
    for key, phrase in (("contains", "containing"), ("equals", "that is exactly")):
        if selector.get(key):
            parts.append(f"{phrase} “{selector[key]}”")
    if find:
        parts.append(f"containing “{find}”")
    return " ".join(parts)


def zero_match_message(selector: dict[str, Any], *, find: str | None = None) -> str:
    return (
        f"I couldn't find {describe_selector(selector, find=find)}, so I changed nothing. "
        "Which text did you mean?"
    )


# --------------------------------------------------------------------- text diff


def compute_text_diff(
    job: Any, variant: dict[str, Any], new_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Before/after of every text whose wording changed, was added or was removed.

    ``role`` is the semantic kind ("title" | "label" | "text"), ``clip_id`` the
    clip a label belongs to. Style-only edits produce no entry. Lyric lines are
    excluded (song-locked, not chat-editable).
    """
    before_bars = bars_from_variant(job, variant)
    kinds = classify(before_bars)
    before = {
        bar["id"]: bar for bar in before_bars if not bar["removed"] and bar["role"] != "lyric_line"
    }
    after = {
        row["id"]: row
        for row in new_rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and not row.get("removed")
        and row.get("role") != "lyric_line"
    }
    diff: list[dict[str, Any]] = []
    for bar_id, bar in before.items():
        row = after.get(bar_id)
        if row is None:
            diff.append(
                {
                    "id": bar_id,
                    "clip_id": bar["clip_id"],
                    "role": kinds[bar_id],
                    "before": bar["text"],
                    "after": None,
                }
            )
        elif str(row.get("text") or "") != bar["text"]:
            diff.append(
                {
                    "id": bar_id,
                    "clip_id": bar["clip_id"],
                    "role": kinds[bar_id],
                    "before": bar["text"],
                    "after": str(row.get("text") or ""),
                }
            )
    for bar_id, row in after.items():
        if bar_id in before:
            continue
        clip = None
        if bar_id.startswith(_CLIP_LABEL_MEDIA_PREFIX):
            clip = bar_id[len(_CLIP_LABEL_MEDIA_PREFIX) :]
        label = clip is not None or bar_id.startswith(_CLIP_LABEL_BAR_PREFIX)
        diff.append(
            {
                "id": bar_id,
                "clip_id": clip,
                "role": "label" if label else "text",
                "before": None,
                "after": str(row.get("text") or ""),
            }
        )
    return diff


# ----------------------------------------------------------------- compile handlers


def _targets(state: _DraftState, op: dict[str, Any], *, find: str | None = None) -> list[dict]:
    """Live text rows the op addresses, after re-resolving its selector (drift check)."""
    selector = op.get("selector")
    target_ids = op.get("target_ids")
    expected = op.get("expected_count")
    if (
        not isinstance(selector, dict)
        or not isinstance(target_ids, list)
        or isinstance(expected, bool)
        or not isinstance(expected, int)
    ):
        raise KriaEditorOpError(_DRIFT)
    bars = bars_from_variant(state.job, state.variant)
    matched = resolve_selector(bars, selector)
    if find is not None:
        by_id = {bar["id"]: bar for bar in bars}
        matched = [bar_id for bar_id in matched if contains_find(by_id[bar_id]["text"], find)]
    if sorted(matched) != sorted(target_ids) or len(matched) != expected or not matched:
        raise KriaEditorOpError(_DRIFT)
    live = {row.get("id"): row for row in state.text if isinstance(row.get("id"), str)}
    rows = []
    for bar_id in matched:
        row = live.get(bar_id)
        if row is None or id(row) in state.removed_text_bars:
            raise KriaEditorOpError(_DRIFT)
        rows.append(row)
    return rows


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def op_rewrite_text(state: _DraftState, op: dict[str, Any]) -> None:
    replace = op.get("replace") if isinstance(op.get("replace"), dict) else None
    text = op.get("text")
    if (replace is None) == (text is None):
        raise KriaEditorOpError("Rewrite needs exactly one of text or replace")
    find = str(replace.get("find")) if replace else None
    rows = _targets(state, op, find=find)
    changed = 0
    for row in rows:
        if replace is not None:
            new, _ = apply_replace(str(row.get("text") or ""), find or "", str(replace["with"]))
        else:
            new = clean_text(str(text))
        if not new:
            raise KriaEditorOpError("A text can't be empty; remove it instead")
        if len(new) > MAX_TEXT_CHARS:
            raise KriaEditorOpError("That text would be too long")
        if new != row.get("text"):
            row["text"] = new
            changed += 1
    state.changed.add("text")
    state.summary = f"Rewrite {_plural(changed, 'text')}"


def _sequence_canonical(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def op_replace_text_sequence(state: _DraftState, op: dict[str, Any]) -> None:
    """Replace one text bar with conserved, equal-duration text segments atomically."""
    segments = op.get("segments")
    if not isinstance(segments, list) or not 0 < len(segments) <= MAX_SELECTOR_LIST:
        raise KriaEditorOpError("A text sequence must contain 1 to 100 segments")
    clean_segments: list[str] = []
    for segment in segments:
        if not isinstance(segment, str):
            raise KriaEditorOpError("A text sequence can contain only strings")
        clean = _sequence_canonical(segment)
        if not clean or len(clean) > MAX_TEXT_CHARS:
            raise KriaEditorOpError("Each text sequence segment must contain 1 to 500 characters")
        clean_segments.append(clean)
    rows = _targets(state, op)
    if len(rows) != 1:
        raise KriaEditorOpError("A text sequence needs exactly one live text")
    source = rows[0]
    expected = op.get("expected_source_text")
    if not isinstance(expected, str) or _sequence_canonical(
        str(source.get("text") or "")
    ) != _sequence_canonical(expected):
        raise KriaEditorOpError(_DRIFT)
    if _sequence_canonical(" ".join(clean_segments)) != _sequence_canonical(
        str(source.get("text") or "")
    ):
        raise KriaEditorOpError("The replacement segments must conserve the original wording")
    start, end = _number(source.get("start_s")), _number(source.get("end_s"))
    if (
        start is None
        or end is None
        or not math.isfinite(start)
        or not math.isfinite(end)
        or end <= start
    ):
        raise KriaEditorOpError("The source text has no valid time window")
    if len(state.text) - 1 + len(clean_segments) > MAX_GUIDED_EDITOR_TEXT_ELEMENTS:
        raise KriaEditorOpError("That sequence exceeds the text lane limit")
    source_id = str(source.get("id"))
    source_params = source.get("source_params")
    sequence_source_id = source.get("sequence_source_id")
    if not isinstance(sequence_source_id, str) and isinstance(source_params, dict):
        sequence_source_id = source_params.get("sequence_source_id")
    if not isinstance(sequence_source_id, str) or not sequence_source_id:
        sequence_source_id = source_id
    existing_ids = {str(row.get("id")) for row in state.text if row is not source}
    child_ids = [f"{source_id}::sequence-{index + 1}" for index in range(len(clean_segments))]
    if len(set(child_ids)) != len(child_ids) or existing_ids.intersection(child_ids):
        raise KriaEditorOpError("Those sequence text ids already exist")
    duration = end - start
    replacements: list[dict[str, Any]] = []
    if "patch" in op and (not isinstance(op["patch"], dict) or not op["patch"]):
        raise KriaEditorOpError("No portable text style fields were supplied")
    patch = op.get("patch")
    for index, segment in enumerate(clean_segments):
        row = copy.deepcopy(source)
        row["id"] = child_ids[index]
        child_params = row.get("source_params")
        child_params = copy.deepcopy(child_params) if isinstance(child_params, dict) else {}
        child_params["sequence_source_id"] = sequence_source_id
        row["source_params"] = child_params
        row["text"] = segment
        row["start_s"] = round(start + duration * index / len(clean_segments), 6)
        row["end_s"] = round(start + duration * (index + 1) / len(clean_segments), 6)
        if row["end_s"] <= row["start_s"]:
            raise KriaEditorOpError("That sequence would create a zero-duration text")
        if isinstance(patch, dict):
            try:
                apply_patch(row, patch)
            except KriaEditorOpError:
                raise
            except (TypeError, ValueError, OverflowError) as exc:
                raise KriaEditorOpError("Those text style fields aren't valid") from exc
        replacements.append(row)
    position = next(index for index, row in enumerate(state.text) if row is source)
    state.text[position : position + 1] = replacements
    state.changed.add("text")
    state.summary = f"Split one text into {_plural(len(replacements), 'segment')}"


def _merge_phases(row: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """The row's phases (or the ones its legacy effect implies) with ``patch`` applied."""
    current = row.get("animation_phases")
    if not isinstance(current, dict):
        entrance = {
            "fade-in": "fade",
            "pop-in": "pop",
            "slide-in": "slide",
            "typewriter": "typewriter",
        }.get(str(row.get("effect")), "none")
        current = {"entrance": entrance, "exit": "none", "loop": "none", "speed": 1}
    merged = {**current, **patch}
    from app.agents._schemas.text_animation_phases import TextAnimationPhases  # noqa: PLC0415

    try:
        return TextAnimationPhases(**merged).model_dump()
    except Exception as exc:  # noqa: BLE001
        raise KriaEditorOpError("Those animation settings aren't valid") from exc


def apply_patch(row: dict[str, Any], patch: dict[str, Any]) -> None:
    """Apply a parser-validated patch (re-validated where the compiler can't trust it)."""
    from app.services.kria_editor_ops import _TEXT_STYLE_FIELDS  # noqa: PLC0415

    for key, value in patch.items():
        if key == "size_scale":
            base = row.get("size_px")
            base = float(base) if isinstance(base, (int, float)) else 72.0
            row["size_px"] = max(8.0, min(300.0, round(base * float(value), 1)))
        elif key == "animation_phases":
            if not isinstance(value, dict) or set(value) - PHASE_KEYS:
                raise KriaEditorOpError("Those animation settings aren't valid")
            row["animation_phases"] = _merge_phases(row, value)
        elif key == "background_color":
            if value is None:
                row.pop("background_color", None)
            elif isinstance(value, str) and _HEX.match(value):
                row["background_color"] = value
            else:
                raise KriaEditorOpError("That background colour isn't valid")
        elif key == "behind_subject":
            if not isinstance(value, bool):
                raise KriaEditorOpError("behind_subject must be true or false")
            row["behind_subject"] = value
        elif key in _TEXT_STYLE_FIELDS:
            if key == "font_family" and value not in _ALLOWED_FONTS:
                raise KriaEditorOpError("That font is not available")
            row[key] = value
        else:
            raise KriaEditorOpError("No portable text style fields were supplied")
    if patch.get("position") not in (None, "custom"):
        # A named position ignores x/y fractions on the burn, but the editors
        # draw y_frac first: drop stale ones so the preview matches.
        row.pop("x_frac", None)
        row.pop("y_frac", None)


def op_patch_text(state: _DraftState, op: dict[str, Any]) -> None:
    patch = op.get("patch")
    if not isinstance(patch, dict) or not patch:
        raise KriaEditorOpError("No portable text style fields were supplied")
    rows = _targets(state, op)
    for row in rows:
        apply_patch(row, patch)
    state.changed.add("text")
    state.summary = f"Restyle {_plural(len(rows), 'text')}"


def op_remove_texts(state: _DraftState, op: dict[str, Any]) -> None:
    rows = _targets(state, op)
    state.removed_text_bars.update(id(row) for row in rows)
    state.text = [row for row in state.text if id(row) not in state.removed_text_bars]
    state.changed.add("text")
    state.summary = f"Remove {_plural(len(rows), 'text')}"


def op_set_texts_timing(state: _DraftState, op: dict[str, Any]) -> None:
    rows = _targets(state, op)
    absolute = {key: float(op[key]) for key in ("start_s", "end_s") if key in op}
    shift = float(op["shift_s"]) if "shift_s" in op else None
    if (shift is None) == (not absolute):
        raise KriaEditorOpError("Timing needs either start/end times or a shift")
    total = _variant_total(state)
    for row in rows:
        start = float(row.get("start_s") or 0.0)
        end = float(row.get("end_s") or 0.0)
        if shift is not None:
            # Keep the on-screen duration; never push a text before 0.
            delta = max(shift, -start)
            start, end = start + delta, end + delta
        else:
            start = absolute.get("start_s", start)
            end = absolute.get("end_s", end)
        if total > 0:
            end = min(end, total)
        if end <= start:
            raise KriaEditorOpError("That timing leaves no time on screen")
        row["start_s"] = round(start, 3)
        row["end_s"] = round(end, 3)
    state.changed.add("text")
    state.summary = f"Retime {_plural(len(rows), 'text')}"


# ------------------------------------------------------------- realign_labels

LABEL_ALIGN_TOLERANCE_S = 0.05
_MIN_LABEL_BAR_S = 0.2


def _slot_window(slot: dict[str, Any]) -> tuple[float, float] | None:
    start = _number(slot.get("output_start_s"))
    end = _number(slot.get("output_end_s"))
    if start is None:
        return None
    if end is None:
        duration = _number(slot.get("duration_s"))
        if duration is None:
            return None
        end = start + duration
    return (start, end) if end > start else None


def plan_label_realign(
    labels: list[dict[str, Any]], slots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Which clip-bound label bars sit off their clip's CURRENT output window.

    ``labels``: ``{id, media_id, segment_id?, start_s, end_s}``. ``slots``: live
    (not removed) slot rows with an output window. A label is matched to its slot
    by ``segment_id`` (== the slot's ``slot_id``), else by media (the slot of
    that clip it overlaps most). Returns ``{id, start_s, end_s, segment_id}`` for
    every label more than ``LABEL_ALIGN_TOLERANCE_S`` off, flushed to the window.
    Pure and shared by the parser (snapshot) and the compiler (live variant).
    """
    windows: list[tuple[dict[str, Any], float, float]] = []
    for slot in slots:
        window = _slot_window(slot)
        if window is not None and not slot.get("removed"):
            windows.append((slot, window[0], window[1]))
    plan: list[dict[str, Any]] = []
    for label in labels:
        start, end = _number(label.get("start_s")), _number(label.get("end_s"))
        if start is None or end is None:
            continue
        match = None
        seg = label.get("segment_id")
        if seg:
            match = next((w for w in windows if str(w[0].get("slot_id")) == str(seg)), None)
        if match is None and label.get("media_id"):
            candidates = [w for w in windows if w[0].get("media_id") == label["media_id"]]
            if candidates:
                match = max(candidates, key=lambda w: max(0.0, min(end, w[2]) - max(start, w[1])))
        if match is None:
            continue
        slot, w_start, w_end = match
        if w_end - w_start < _MIN_LABEL_BAR_S:
            continue
        if (
            abs(start - w_start) <= LABEL_ALIGN_TOLERANCE_S
            and abs(end - w_end) <= LABEL_ALIGN_TOLERANCE_S
        ):
            continue
        plan.append(
            {
                "id": label["id"],
                "start_s": round(w_start, 6),
                "end_s": round(w_end, 6),
                "segment_id": slot.get("slot_id"),
            }
        )
    return plan


def op_realign_labels(state: _DraftState, op: dict[str, Any]) -> None:
    """Flush clip-bound label bars to their clip's current output window.

    Server-computed: the model never supplies a time. Text, style and position
    are untouched; captions, titles and non-clip bars are never matched.
    """
    if "timeline" in state.changed:
        # A timeline op in the same bundle already re-windows every label
        # (rebase_guided_text) onto the new layout; nothing left to do.
        state.summary = "Labels follow the timeline change"
        return
    bars = bars_from_variant(state.job, state.variant)
    selector = op.get("selector") if isinstance(op.get("selector"), dict) else {"group": "labels"}
    matched = {
        bar_id
        for bar_id in resolve_selector(bars, selector)
        if classify(bars).get(bar_id) == "label"
    }
    wanted = set(op.get("target_ids") or matched)
    by_bar = {bar["id"]: bar for bar in bars}
    links = _clip_label_links(state.job, state.variant)
    labels: list[dict[str, Any]] = []
    rows: dict[str, dict[str, Any]] = {}
    for row in state.text:
        row_id = row.get("id")
        if (
            not isinstance(row_id, str)
            or row_id not in matched & wanted
            or id(row) in state.removed_text_bars
            or row.get("removed")
            or is_caption_text_bar(row)
        ):
            continue
        media = (links.get(row_id) or {}).get("clip_id") or by_bar[row_id]["clip_id"]
        if not media:
            continue
        rows[row_id] = row
        labels.append(
            {
                "id": row_id,
                "media_id": media,
                "segment_id": row.get("segment_id"),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
            }
        )
    plan = plan_label_realign(labels, state.slots)
    if not plan:
        raise KriaEditorOpError("The labels already line up with their clips")
    for item in plan:
        row = rows[item["id"]]
        row["start_s"], row["end_s"] = item["start_s"], item["end_s"]
        if item.get("segment_id"):
            row["segment_id"] = item["segment_id"]
    state.changed.add("text")
    state.summary = f"Realign {_plural(len(plan), 'label')} to their clips"


def _variant_total(state: _DraftState) -> float:
    try:
        return float(sum(_slot_duration(row) for row in state.slots if not row.get("removed")))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------- add_text (extended)


def _style_source(state: _DraftState, style_from: str | None) -> dict[str, Any] | None:
    """The live row whose look a new text copies: title, labels, a bar id, or none."""
    bars = bars_from_variant(state.job, state.variant)
    kinds = classify(bars)
    live = {row.get("id"): row for row in state.text if isinstance(row.get("id"), str)}

    def first(kind: str) -> dict[str, Any] | None:
        for bar in bars:
            if kinds[bar["id"]] == kind and bar["id"] in live and not bar["removed"]:
                if id(live[bar["id"]]) not in state.removed_text_bars:
                    return live[bar["id"]]
        return None

    if style_from and style_from not in {"title", "labels"}:
        row = live.get(style_from)
        if row is None or id(row) in state.removed_text_bars:
            raise KriaEditorOpError(_DRIFT)
        return row
    if style_from == "labels":
        return first("label") or first("title") or first("text")
    return first("title") or first("text")


def add_text_v2(state: _DraftState, op: dict[str, Any]) -> None:
    """``add_text`` with style_from / patch / clip_id (legacy path stays untouched)."""
    source = _style_source(state, op.get("style_from"))
    row: dict[str, Any] = {
        "text": str(op["text"]),
        "start_s": float(op["start_s"]),
        "end_s": float(op["end_s"]),
        "role": "generative_intro",
        "font_family": "Playfair Display",
        "size_px": 72,
        "color": "#FFFFFF",
        "effect": "static",
        "alignment": "center",
        "position": "middle",
    }
    if source is not None:
        row.update(
            {
                key: source[key]
                for key in _COPY_STYLE_KEYS
                if key in source and source[key] is not None
            }
        )
    if op.get("after_animation_of"):
        row["_after_animation_of"] = op["after_animation_of"]
    if op.get("below"):
        row["_below"] = True
        row["_below_source_id"] = source.get("id") if source is not None else None
    clip_id = op.get("clip_id")
    if clip_id:
        new_id = f"{_CLIP_LABEL_MEDIA_PREFIX}{clip_id}"
        if any(existing.get("id") == new_id for existing in state.text):
            raise KriaEditorOpError("That clip already has a label bar")
        row["id"] = new_id
    else:
        row["id"] = f"kria-{uuid.uuid4().hex}"
    patch = op.get("patch")
    if isinstance(patch, dict) and patch:
        apply_patch(row, patch)
    state.text.append(row)
    state.changed.add("text")
