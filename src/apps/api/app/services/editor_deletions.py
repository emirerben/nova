"""Pure normalization for explicit native-editor removals.

The editor normally sends complete replacement lanes.  A deliberately removed
row must survive clients that omit a lane (or feature gates that make a lane
unrenderable), so removals are normalized against the server baseline before
the ordinary validators see the request.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DELETE_KINDS = frozenset(
    {
        "clip",
        "text",
        "caption_cue",
        "music",
        "sound_effect",
        "media_overlay",
        "visual_block",
        "motion_scene",
        "camera_effect",
        "carousel",
        "lyric_line",
    }
)


class EditorDeletionError(ValueError):
    """An explicit removal does not name one current baseline object."""


@dataclass(frozen=True)
class EditorDeletion:
    kind: str
    id: str


@dataclass(frozen=True)
class EditorDeletionResult:
    sections: dict[str, Any]
    deleted_kinds: frozenset[str]


_LANES = {
    "text": "text_elements",
    "caption_cue": "caption_cues",
    "sound_effect": "sound_effects",
    "media_overlay": "media_overlays",
    "visual_block": "visual_blocks",
    "motion_scene": "motion_scenes",
    "camera_effect": "camera_effects",
}


def _rows(value: object) -> list[dict[str, Any]]:
    return [dict(row) for row in value or [] if isinstance(row, Mapping)]


def canonical_caption_rows(value: object) -> list[dict[str, Any]]:
    """Give legacy caption rows the same stable identities as the iOS decoder.

    Caption JSON pre-dates native editing and often has no ``id``.  The client
    presents ``native-caption-{index}`` (adding ``-legacy`` if that name is
    already occupied), so deletion validation must use precisely that view.
    The returned rows are safe to persist: the first Save upgrades the legacy
    records with their stable identity.
    """
    rows = _rows(value)
    used = {str(row["id"]) for row in rows if isinstance(row.get("id"), str) and row["id"]}
    canonical: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row.get("id"), str) or not row["id"]:
            identifier = f"native-caption-{index}"
            while identifier in used:
                identifier += "-legacy"
            row["id"] = identifier
            used.add(identifier)
        canonical.append(row)
    return canonical


def _id(row: Mapping[str, Any], *, kind: str) -> str | None:
    value = row.get("slot_id") if kind == "clip" else row.get("id")
    return str(value) if isinstance(value, str) and value else None


def _suppressed_lyric_keys(variant: Mapping[str, Any]) -> set[str]:
    overrides = variant.get("lyric_line_overrides")
    return {
        str(key).strip()
        for key in (
            variant.get("lyric_line_suppressions")
            or (overrides.get("_suppressed_line_keys") if isinstance(overrides, Mapping) else [])
            or []
        )
        if str(key).strip()
    }


def _lyric_key(row: Mapping[str, Any]) -> str | None:
    """Read the line key from both current and legacy editor projections."""
    source = row.get("source_params")
    if isinstance(source, Mapping) and str(source.get("key") or "").strip():
        return str(source["key"]).strip()
    for value in (row.get("line_key"), row.get("lyric_line_key")):
        if str(value or "").strip():
            return str(value).strip()
    identifier = str(row.get("id") or "")
    for prefix in ("lyric_", "lyr-"):
        if identifier.startswith(prefix) and identifier[len(prefix) :]:
            return identifier[len(prefix) :]
    return None


def apply_editor_deletions(
    variant: Mapping[str, Any],
    *,
    deletions: Iterable[EditorDeletion],
    sections: Mapping[str, object],
    baseline_sections: Mapping[str, object] | None = None,
) -> EditorDeletionResult:
    """Return complete filtered lanes without mutating the inputs.

    ``baseline_sections`` lets callers use a canonical projected timeline (for
    guided/device variants) instead of the legacy JSON projection.  Every id is
    checked before any result is returned, making the caller's later JSON write
    atomic by construction.
    """
    requested = list(deletions)
    seen: set[tuple[str, str]] = set()
    for deletion in requested:
        if deletion.kind not in DELETE_KINDS or not deletion.id:
            raise EditorDeletionError("invalid deletion target")
        key = (deletion.kind, deletion.id)
        if key in seen:
            raise EditorDeletionError("duplicate deletion target")
        seen.add(key)

    baseline = dict(baseline_sections or {})
    output = {key: copy.deepcopy(value) for key, value in sections.items()}

    # Validate every target first, then construct the replacements. This avoids
    # accepting a partially stale multi-delete request.
    for deletion in requested:
        kind, target = deletion.kind, deletion.id
        if kind == "music":
            if str(variant.get("music_track_id") or "") != target:
                raise EditorDeletionError("stale deletion target")
        elif kind == "carousel":
            carousel = variant.get("carousel_moment")
            if not isinstance(carousel, Mapping) or str(carousel.get("id") or "carousel") != target:
                raise EditorDeletionError("stale deletion target")
        elif kind == "lyric_line":
            snapshot = variant.get("lyric_overlay_snapshot")
            line_ids = {
                identifier: str(row.get("line_key")).strip()
                for row in _rows(snapshot)
                if str(row.get("line_key") or "").strip()
                for identifier in (
                    str(row["line_key"]).strip(),
                    f"lyric_{str(row['line_key']).strip()}",
                )
            }
            if target not in line_ids:
                raise EditorDeletionError("stale deletion target")
            if line_ids[target] in _suppressed_lyric_keys(variant):
                raise EditorDeletionError("stale deletion target")
        else:
            lane = "timeline_slots" if kind == "clip" else _LANES[kind]
            source = baseline.get(lane)
            if source is None:
                source = (
                    variant.get("user_timeline", {}).get("slots")
                    if kind == "clip"
                    else variant.get(lane)
                )
            source_rows = canonical_caption_rows(source) if kind == "caption_cue" else _rows(source)
            if target not in {_id(row, kind=kind) for row in source_rows}:
                raise EditorDeletionError("stale deletion target")

    for deletion in requested:
        kind, target = deletion.kind, deletion.id
        if kind == "music":
            output["remove_music"] = True
        elif kind == "carousel":
            output["carousel_moment"] = None
            output["carousel_moment_touched"] = True
        elif kind == "lyric_line":
            # Multiple explicit lyric deletions arrive in one transaction.
            # Accumulate against the in-progress replacement rather than
            # re-reading the persisted variant for every intent.
            suppressed = set(
                output.get("lyric_line_suppressions") or _suppressed_lyric_keys(variant)
            )
            # Native sends the raw line key (L<n>); projected element IDs may
            # carry the lyric_ prefix. Persist one renderer identity for both.
            suppressed.add(target.removeprefix("lyric_"))
            output["lyric_line_suppressions"] = sorted(suppressed)
        else:
            lane = "timeline_slots" if kind == "clip" else _LANES[kind]
            # A complete enabled-editor lane may legitimately remove one row
            # and add/change another in the same Save.  Use its submitted
            # replacement when present; omitted lanes materialize from the
            # canonical server baseline.
            current = output.get(lane)
            if current is None:
                current = baseline.get(lane)
            if current is None:
                current = (
                    variant.get("user_timeline", {}).get("slots")
                    if kind == "clip"
                    else variant.get(lane)
                )
            rows = canonical_caption_rows(current) if kind == "caption_cue" else _rows(current)
            if kind == "clip":
                for row in rows:
                    if _id(row, kind=kind) == target:
                        row["removed"] = True
            else:
                rows = [row for row in rows if _id(row, kind=kind) != target]
            output[lane] = rows

    deleted_lyric_keys = {
        item.id.removeprefix("lyric_") for item in requested if item.kind == "lyric_line"
    }
    if deleted_lyric_keys:
        # The native editor removes the visible lyric TextElement as well as
        # submitting its durable lyric intent. Materialize the same filtered
        # lane when it was omitted, otherwise an empty-draft snapshot would
        # lazily project the deleted line again on reopen.
        current_text = output.get("text_elements")
        if current_text is None:
            current_text = baseline.get("text_elements", variant.get("text_elements"))
        output["text_elements"] = [
            row for row in _rows(current_text) if _lyric_key(row) not in deleted_lyric_keys
        ]

    # Visual/text links are a bidirectional editor relationship. Removing a
    # text element leaves its card/block in place but clears any stored
    # membership list; removing a block removes only the text explicitly
    # linked to it.  Materialize both lanes so the ordinary validator sees a
    # coherent complete pair even when the client omitted either lane.
    deleted_text_ids = {item.id for item in requested if item.kind == "text"}
    deleted_block_ids = {item.id for item in requested if item.kind == "visual_block"}
    if deleted_text_ids or deleted_block_ids:
        current_text = output.get("text_elements")
        if current_text is None:
            current_text = baseline.get("text_elements", variant.get("text_elements"))
        current_blocks = output.get("visual_blocks")
        if current_blocks is None:
            current_blocks = baseline.get("visual_blocks", variant.get("visual_blocks"))
        text_rows = _rows(current_text)
        block_rows = _rows(current_blocks)
        if deleted_block_ids:
            text_rows = [
                row
                for row in text_rows
                if str(row.get("visual_block_id") or "") not in deleted_block_ids
            ]
            block_rows = [
                row for row in block_rows if _id(row, kind="visual_block") not in deleted_block_ids
            ]
        if deleted_text_ids:
            for block in block_rows:
                members = block.get("text_element_ids")
                if isinstance(members, list):
                    block["text_element_ids"] = [
                        member for member in members if str(member) not in deleted_text_ids
                    ]
        output["text_elements"] = text_rows
        output["visual_blocks"] = block_rows
    return EditorDeletionResult(output, frozenset(item.kind for item in requested))
