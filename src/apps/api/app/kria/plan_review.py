"""Live plan & review: section-scoped turns and per-section undo (KRI-441, KRI-442).

Contract: docs/pipelines/live-plan-blocks.md (frozen v2, ``plan_contract.py``). All of it is
dark behind ``LIVE_PLAN_REVIEW_ENABLED``.

A turn may carry ``scope`` (the sections the creator flagged with "Change"). It is planned
ONLY as editor operations and enforced in three layers so an unflagged section can never move:

1. **Snapshot filter** (:func:`scoped_snapshot`): the copilot sees only the op families of the
   flagged sections and a "change only X" prompt fragment.
2. **Op filter** (:func:`filter_ops_to_scope`): after parsing, ops whose section is not flagged
   are dropped (and recorded).
3. **Post-compile repair** (:func:`repair_compiled`): any commit lane the compiled draft changed
   outside the scope is stripped, and text bars outside the scope are reverted per bar.

Every repair is reported, never fatal. The pure functions here take plain dicts so the unit of
behaviour is testable without a database; the DB-touching parts (preflight, per-section undo)
are at the bottom.

Deviation from the written contract (KRI-442): a scoped update re-renders the SAME Job (the
editor-commit path, ``enqueue_editor_commit_render``), so "the previous job's variant" does not
exist after the render. Instead the pre-update value of every lane the update changed is
captured at compile time (``CompiledEditorDraft.before``), stored on the draft's source
execution (``result["plan_review_before"]``), and Undo restores from that record. See
:func:`build_restore_record`.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.kria.plan_contract import (
    SCOPABLE_SECTIONS,
    SECTION_ORDER,
    ManualEdit,
    PlanSectionUndoBody,
    PlanSectionUndoOut,
)
from app.kria.reply_language import say

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# ---------------------------------------------------------------------------------------
# Section -> allowed lane map (contract 4.3)
# ---------------------------------------------------------------------------------------

SECTION_OPS: dict[str, frozenset[str]] = {
    "title": frozenset({"set_title", "set_intro_layout"}),
    "clips": frozenset(
        {
            "set_clip_duration",
            "set_clip_in",
            "trim_clip_start",
            "trim_output_start",
            "reorder_clip",
            "remove_clip",
            "split_clip",
            "add_unused_sources",
            "set_media_duration",
            "stack_images",
            "reorder_clips_by",
            "patch_slots",
            "set_total_duration",
            "set_transition",
            "label_each_clip",
            "apply_speech_cut_candidate",
        }
    ),
    "captions": frozenset(
        {"edit_caption", "replace_caption_text", "set_caption_timing", "set_caption_emphasis"}
    ),
    "music": frozenset({"swap_music", "remove_music", "set_mix"}),
    "sfx": frozenset({"add_sfx", "patch_sfx", "remove_sfx"}),
    "overlays": frozenset(
        {
            "add_overlay",
            "patch_overlay",
            "remove_overlay",
            "accept_overlay_suggestion",
            "remove_visual_media",
            "set_visual_fade",
            "add_motion_block",
            "patch_motion_block",
            "remove_motion_block",
        }
    ),
    "look": frozenset(
        {
            "set_look_preset",
            "add_camera_effect",
            "patch_camera_effect",
            "remove_camera_effect",
            "apply_custom_effect",
            "set_carousel_moment",
        }
    ),
}
# `set_caption_meta` splits by key: only `enabled` is captions, anything else is look.
CAPTION_META_OP = "set_caption_meta"
# Ops that act on text bars. Their section is the BAR's section (or "look" for pure styling).
TEXT_BAR_OPS = frozenset(
    {
        "edit_text",
        "set_text_timing",
        "remove_text",
        "patch_text_style",
        "patch_text_appearance",
        "rewrite_text",
        "patch_text",
        "remove_texts",
        "replace_text_sequence",
        "set_texts_timing",
        "realign_labels",
        "add_text",
    }
)
TEXT_STYLE_OPS = frozenset({"patch_text_style", "patch_text_appearance", "patch_text"})
# Never allowed inside a scoped turn (they navigate, replay history or restyle the whole edit).
NEVER_IN_SCOPE = frozenset(
    {
        "set_edit_direction",
        "open_tool",
        "undo_last_edit",
        "repeat_last_edit",
        "set_slide_cover",
        "set_post_caption",
    }
)
# Sections whose lanes include text bars.
TEXT_SECTIONS = ("title", "clips", "captions", "overlays")

# Op families (``snapshot["allowed_op_families"]``) per section (contract 4.4 layer 1).
SECTION_FAMILIES: dict[str, frozenset[str]] = {
    # `render`: set_intro_layout. `motion`/`visual`: motion blocks and visual fade. `clip` under
    # look is set_look_preset (a per-clip op by family); `caption` under look is the caption
    # restyle half of set_caption_meta; `carousel`: set_carousel_moment. Layer 2 still drops
    # every op of these families that does not belong to a flagged section.
    "title": frozenset({"title", "text", "render"}),
    "clips": frozenset({"clip", "transition", "text"}),
    "captions": frozenset({"caption", "text"}),
    "music": frozenset({"music"}),
    "sfx": frozenset({"sfx"}),
    "overlays": frozenset({"overlay", "visual_media", "text", "motion", "visual"}),
    "look": frozenset({"style", "text", "effect", "clip", "caption", "carousel"}),
}

# EditorCommitRequest fields -> owning section(s). `text_elements` and `caption_meta` are
# split finer (per bar / per key) in :func:`repair_compiled`.
FIELD_SECTIONS: dict[str, tuple[str, ...]] = {
    "title": ("title",),
    "timeline_slots": ("clips",),
    "caption_cues": ("captions",),
    "lyrics": ("captions",),
    "music_track_id": ("music",),
    "remove_music": ("music",),
    "music_window": ("music",),
    "background_music": ("music",),
    "user_song": ("music",),
    "mix": ("music",),
    "sound_effects": ("sfx",),
    "media_overlays": ("overlays",),
    "visual_blocks": ("overlays",),
    "motion_scenes": ("overlays",),
    "camera_effects": ("look",),
    "carousel_moment": ("look",),
}
# Documentation of the contract table (4.3, last column); the code above is its inverse.
SECTION_COMMIT_FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("title", "text_elements"),
    "clips": ("timeline_slots", "text_elements"),
    "captions": ("caption_cues", "caption_meta.enabled", "text_elements", "lyrics"),
    "music": (
        "music_track_id",
        "remove_music",
        "music_window",
        "background_music",
        "user_song",
        "mix",
    ),
    "sfx": ("sound_effects",),
    "overlays": ("media_overlays", "visual_blocks", "motion_scenes", "text_elements"),
    "look": ("camera_effects", "carousel_moment", "caption_meta", "text_elements"),
}
_UNOWNED_LANES = frozenset({"orientation", "landscape_fit"})
_LANE_KEYS = frozenset(FIELD_SECTIONS) | {"text_elements", "caption_meta"} | _UNOWNED_LANES
# A bar change in any of these is a content change; everything else is styling.
_TEXT_CONTENT_KEYS = frozenset({"text", "start_s", "end_s", "clip_id", "removed", "role"})

_SECTION_LABELS = {
    "title": ("title", "başlık"),
    "clips": ("clips", "klipler"),
    "captions": ("captions", "altyazılar"),
    "music": ("music", "müzik"),
    "sfx": ("sound effects", "ses efektleri"),
    "overlays": ("overlays", "katmanlar"),
    "look": ("look", "görünüm"),
    "post_caption": ("post caption", "paylaşım metni"),
}
_RESTORE_KEY = "plan_review_before"


class ScopeError(ValueError):
    """A scope that cannot be honoured; ``code`` is the contract problem code."""

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def validate_scope(scope: list[str]) -> list[str]:
    """Normalize a requested scope, in SECTION_ORDER. Raises :class:`ScopeError`."""
    if not scope or len(scope) != len(set(scope)) or any(s not in SECTION_ORDER for s in scope):
        raise ScopeError("scope_invalid", "Choose which parts of the video to change.")
    if "post_caption" in scope:
        raise ScopeError(
            "scope_section_unsupported", "The post caption can't be changed from here yet."
        )
    return [s for s in SCOPABLE_SECTIONS if s in scope]


def section_label(section: str, *, lang: str = "en") -> str:
    en, tr = _SECTION_LABELS.get(section, (section, section))
    return tr if lang == "tr" else en


def scope_phrase(sections: list[str]) -> str:
    """Localized "captions and music" (language comes from the active reply language)."""
    names = [say(en=section_label(s), tr=section_label(s, lang="tr")) for s in sections]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    joiner = say(en=" and ", tr=" ve ")
    return ", ".join(names[:-1]) + joiner + names[-1]


# ---------------------------------------------------------------------------------------
# Text bar classification
# ---------------------------------------------------------------------------------------


def is_caption_bar(bar: dict[str, Any]) -> bool:
    if bar.get("caption_cue") is True:
        return True
    try:
        from app.services.kria_editor_ops import is_caption_text_bar  # noqa: PLC0415

        return is_caption_text_bar(bar)
    except Exception:  # noqa: BLE001 - classification must never raise
        return str(bar.get("id") or "").startswith("narration-caption-")


def section_of_text_bar(bar: dict[str, Any], *, clip_bar_ids: frozenset[str] = frozenset()) -> str:
    """Which section owns a text bar (contract 4.3)."""
    bar_id = str(bar.get("id") or "")
    if is_caption_bar(bar):
        return "captions"
    role = str(bar.get("role") or "")
    if bar_id.startswith("guided-title") or role in {"title", "intro", "hook", "generative_intro"}:
        return "title"
    if (
        bar.get("clip_id")
        or bar_id in clip_bar_ids
        or bar_id.startswith(("clip-label-", "montage-text-"))
    ):
        return "clips"
    return "overlays"


def bar_change_section(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    scope: list[str],
    *,
    clip_bar_ids: frozenset[str] = frozenset(),
) -> str:
    """The section a bar change counts against: its own, or ``look`` for pure restyling."""
    bar = after or before or {}
    own = section_of_text_bar(bar, clip_bar_ids=clip_bar_ids)
    if before is None or after is None:
        return own
    for key in _TEXT_CONTENT_KEYS:
        if before.get(key) != after.get(key):
            return own
    return own if own in scope or "look" not in scope else "look"


# ---------------------------------------------------------------------------------------
# Layer 1: scoped snapshot
# ---------------------------------------------------------------------------------------


def scoped_snapshot(snapshot: dict[str, Any], scope: list[str]) -> dict[str, Any] | None:
    """Intersect the snapshot's op families with the flagged sections' families.

    Returns None when nothing is left: an empty ``allowed_op_families`` is read by the
    copilot as "all ops", so the caller must answer instead of asking the model.
    """
    allowed = {str(f) for f in snapshot.get("allowed_op_families") or []}
    wanted: set[str] = set()
    for section in scope:
        wanted |= SECTION_FAMILIES.get(section, frozenset())
    families = sorted(allowed & wanted)
    if not families:
        return None
    return {**snapshot, "allowed_op_families": families, "scope": list(scope)}


# ---------------------------------------------------------------------------------------
# Layer 2: op filter
# ---------------------------------------------------------------------------------------


def _text_bars(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in snapshot.get("text_bars") or [] if isinstance(b, dict)]


def _bar_sections_for_op(op: dict[str, Any], snapshot: dict[str, Any]) -> set[str] | None:
    """Sections of the bars an op targets, or None when they cannot be resolved."""
    bars = _text_bars(snapshot)
    by_id = {str(b.get("id")): b for b in bars}
    if isinstance(op.get("bar_index"), int) and not isinstance(op.get("bar_index"), bool):
        index = op["bar_index"]
        if 0 <= index < len(bars):
            return {section_of_text_bar(bars[index])}
        return None
    ids = op.get("target_ids")
    if not ids and isinstance(op.get("selector"), dict):
        selector = op["selector"]
        ids = selector.get("target_ids") or selector.get("ids")
        if not ids:
            try:
                from app.services.kria_editor_ops_text import (  # noqa: PLC0415
                    bars_from_snapshot,
                    normalize_selector,
                    resolve_selector,
                )

                normalized = normalize_selector(selector, snapshot)
                if normalized is not None:
                    ids = resolve_selector(bars_from_snapshot(snapshot), normalized)
            except Exception:  # noqa: BLE001 - fall back to the lenient answer below
                ids = None
    if isinstance(ids, list) and ids:
        found = {section_of_text_bar(by_id[str(i)]) for i in ids if str(i) in by_id}
        return found or None
    return None


def op_sections(op: dict[str, Any], snapshot: dict[str, Any]) -> tuple[set[str], str]:
    """``(candidate sections, mode)``: the op is allowed when ANY candidate is flagged
    (mode ``any``) or when ALL are (mode ``all``). Empty candidates mean "never allowed"."""
    name = str(op.get("op") or "")
    if name in NEVER_IN_SCOPE:
        return set(), "any"
    if name == CAPTION_META_OP:
        keys = set((op.get("patch") or {}).keys()) if isinstance(op.get("patch"), dict) else set()
        needed = set()
        if "enabled" in keys:
            needed.add("captions")
        if keys - {"enabled"} or not keys:
            needed.add("look")
        return needed, "all"
    if name in TEXT_BAR_OPS:
        style = name in TEXT_STYLE_OPS
        if name == "add_text":
            if op.get("clip_id"):
                return {"clips"}, "any"
            return {"title", "overlays"}, "any"
        resolved = _bar_sections_for_op(op, snapshot)
        base = resolved if resolved is not None else set(TEXT_SECTIONS)
        return (base | {"look"} if style else base), "any"
    for section, names in SECTION_OPS.items():
        if name in names:
            return {section}, "any"
    return set(), "any"


def filter_ops_to_scope(
    ops: list[dict[str, Any]], snapshot: dict[str, Any], scope: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Drop ops outside the flagged sections. Returns ``(kept, dropped[{op, section}])``."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    flagged = set(scope)
    for op in ops:
        candidates, mode = op_sections(op, snapshot)
        if not candidates:
            allowed = False
        elif mode == "all":
            allowed = candidates <= flagged
        else:
            allowed = bool(candidates & flagged)
        if allowed:
            kept.append(op)
        else:
            ordered = [s for s in SECTION_ORDER if s in candidates]
            dropped.append(
                {"op": str(op.get("op") or ""), "section": ordered[0] if ordered else "other"}
            )
    return kept, dropped


def left_alone_note(dropped: list[dict[str, str]], scope: list[str]) -> str:
    """ "I left captions alone." for the sections the model tried to touch."""
    sections = [
        s for s in SECTION_ORDER if s not in scope and any(d["section"] == s for d in dropped)
    ]
    if not sections:
        return ""
    phrase = scope_phrase(sections)
    return say(en=f"I left {phrase} alone.", tr=f"{phrase.capitalize()} bölümüne dokunmadım.")


def nothing_in_scope_reply(dropped: list[dict[str, str]], scope: list[str]) -> str:
    sections = [s for s in SECTION_ORDER if any(d["section"] == s for d in dropped)]
    phrase = scope_phrase(sections) if sections else scope_phrase(scope)
    return say(
        en=f"That would change {phrase}, which you did not flag. Flag it and try again.",
        tr=(f"Bu, işaretlemediğin {phrase} bölümünü değiştirirdi. Onu da işaretleyip tekrar dene."),
    )


# ---------------------------------------------------------------------------------------
# Manual edits (no model call)
# ---------------------------------------------------------------------------------------


class ManualEditTargetMissing(ValueError):
    """The line a manual edit names is no longer in the video."""


def edit_section(
    edit: ManualEdit,
    *,
    bars: list[dict[str, Any]],
    cue_ids: set[str],
    clip_bar_ids: frozenset[str] = frozenset(),
) -> str:
    """The section a manual edit belongs to. Raises :class:`ManualEditTargetMissing`."""
    if edit.kind == "set_mix":
        return "music"
    target = str(edit.target_id or "")
    for bar in bars:
        if str(bar.get("id")) == target and not bar.get("removed"):
            return section_of_text_bar(bar, clip_bar_ids=clip_bar_ids)
    if target in cue_ids:
        return "captions"
    raise ManualEditTargetMissing(target)


def manual_edits_to_ops(edits: list[ManualEdit], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate manual edits into the copilot's own op vocabulary (parsed afterwards by the
    SAME coercion the model's ops go through)."""
    bars = _text_bars(snapshot)
    cues = [c for c in (snapshot.get("captions") or {}).get("cues") or [] if isinstance(c, dict)]
    ops: list[dict[str, Any]] = []
    for edit in edits:
        if edit.kind == "set_mix":
            op: dict[str, Any] = {"op": "set_mix"}
            for key in ("music_level", "original_level", "music_gain_db"):
                value = getattr(edit, key)
                if value is not None:
                    op[key] = value
            ops.append(op)
            continue
        target = str(edit.target_id or "")
        index = next(
            (i for i, b in enumerate(bars) if str(b.get("id")) == target and not b.get("removed")),
            None,
        )
        if index is not None:
            if is_caption_bar(bars[index]):
                # Caption/narration bars are owned by the caption lane, never by selectors.
                ops.append({"op": "edit_text", "bar_index": index, "text": edit.text})
            else:
                ops.append({"op": "rewrite_text", "selector": {"ids": [target]}, "text": edit.text})
            continue
        cue_index = next((i for i, c in enumerate(cues) if str(c.get("id")) == target), None)
        if cue_index is None:
            raise ManualEditTargetMissing(target)
        ops.append({"op": "edit_caption", "cue_index": cue_index, "text": edit.text})
    return ops


def parse_ops_without_model(
    raw_ops: list[dict[str, Any]], snapshot: dict[str, Any], message: str
) -> tuple[list[dict[str, Any]], str]:
    """Run ops through EditCopilot's own parser/coercion with no model call.

    Returns ``(ops, rejection_detail)``; ``ops`` is empty when anything was rejected (a
    manual turn is atomic: a half-applied edit would be a silent surprise).
    """
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput  # noqa: PLC0415

    agent = EditCopilotAgent(None)  # type: ignore[arg-type]  # parse() never touches the client
    output = agent.parse(
        json.dumps(
            {
                "intent": "edit",
                "ops": raw_ops,
                "reply": "",
                "confidence": 1.0,
                "needs_clarification": False,
            }
        ),
        EditCopilotInput(
            utterance=message or "manual edit", prior_turns=[], variant_snapshot=snapshot
        ),
    )
    ops = list(output.ops)
    if len(ops) != len(raw_ops):
        detail = next(
            (str(r.get("detail")) for r in output.rejection_reasons if r.get("detail")), ""
        )
        return [], detail
    return ops, ""


# ---------------------------------------------------------------------------------------
# Layer 3: post-compile repair
# ---------------------------------------------------------------------------------------


def _row_map(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
    """id -> row, or None when any row has no usable id."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("id"):
            return None
        out[str(row["id"])] = row
    return out


def row_diff(
    before: list[dict[str, Any]] | None, after: list[dict[str, Any]] | None
) -> dict[str, dict[str, Any] | None] | None:
    """``{id: before_row | None}`` for every row that differs (None = added by the update)."""
    b, a = _row_map(before), _row_map(after)
    if b is None or a is None:
        return None
    return {rid: b.get(rid) for rid in {*b, *a} if b.get(rid) != a.get(rid)}


def apply_row_restore(
    current: list[dict[str, Any]], restore: dict[str, dict[str, Any] | None]
) -> list[dict[str, Any]]:
    """Put ``restore`` rows back into ``current``: replace, re-add removed, drop added."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in current:
        rid = str(row.get("id")) if isinstance(row, dict) else ""
        if rid in restore:
            seen.add(rid)
            if restore[rid] is not None:
                out.append(copy.deepcopy(restore[rid]))
        else:
            out.append(row)
    for rid, row in restore.items():
        if rid not in seen and row is not None:
            out.append(copy.deepcopy(row))
    return out


def _lane_present(key: str, value: Any) -> bool:
    if key == "remove_music":
        return value is True
    return value is not None


def lanes_in(data: dict[str, Any]) -> list[str]:
    return [k for k, v in data.items() if k in _LANE_KEYS and _lane_present(k, v)]


@dataclasses.dataclass
class RepairReport:
    reverted_fields: list[str] = dataclasses.field(default_factory=list)
    reverted_bars: list[str] = dataclasses.field(default_factory=list)
    # Sections whose change was undone (the user-facing "I left X alone" note reads this).
    reverted_sections: list[str] = dataclasses.field(default_factory=list)

    def add_section(self, section: str) -> None:
        if section not in self.reverted_sections:
            self.reverted_sections.append(section)

    def __bool__(self) -> bool:
        return bool(self.reverted_fields or self.reverted_bars)


def repair_payload(
    data: dict[str, Any],
    before: dict[str, Any],
    scope: list[str],
    *,
    clip_bar_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], RepairReport]:
    """Strip every commit lane outside ``scope`` from a dumped EditorCommitRequest.

    ``data`` is ``payload.model_dump(mode="json", exclude_none=True)``; ``before`` is
    ``CompiledEditorDraft.before``. Text bars are reverted per bar, caption meta per key.
    """
    flagged = set(scope)
    out = copy.deepcopy(data)
    report = RepairReport()
    for key in list(out):
        if key in _UNOWNED_LANES:
            if _lane_present(key, out[key]):
                report.reverted_fields.append(key)
            out.pop(key)
        elif key in FIELD_SECTIONS and _lane_present(key, out[key]):
            if not (set(FIELD_SECTIONS[key]) & flagged):
                report.reverted_fields.append(key)
                report.add_section(FIELD_SECTIONS[key][0])
                out.pop(key)
    meta = out.get("caption_meta")
    if isinstance(meta, dict):
        kept = {
            k: v for k, v in meta.items() if ("captions" if k == "enabled" else "look") in flagged
        }
        if len(kept) != len(meta):
            report.reverted_fields.append("caption_meta")
            for k in meta:
                if k not in kept:
                    report.add_section("captions" if k == "enabled" else "look")
        if kept and kept != {"font_set": False}:
            out["caption_meta"] = kept
        else:
            out.pop("caption_meta")
    rows = out.get("text_elements")
    if isinstance(rows, list) and isinstance(before.get("text_elements"), list):
        repaired, reverted = _repair_text_rows(
            before["text_elements"], rows, flagged, scope, clip_bar_ids
        )
        report.reverted_bars.extend(reverted)
        for section in _reverted_bar_sections(
            before["text_elements"], rows, reverted, scope, clip_bar_ids
        ):
            report.add_section(section)
        if repaired == before["text_elements"]:
            out.pop("text_elements")
            if reverted:
                report.reverted_fields.append("text_elements")
        else:
            out["text_elements"] = repaired
    return out, report


def _repair_text_rows(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    flagged: set[str],
    scope: list[str],
    clip_bar_ids: frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    b, a = _row_map(before), _row_map(after)
    if b is None or a is None:
        return after, []
    revert: dict[str, dict[str, Any] | None] = {}
    for rid in {*b, *a}:
        if b.get(rid) == a.get(rid):
            continue
        if (
            bar_change_section(b.get(rid), a.get(rid), scope, clip_bar_ids=clip_bar_ids)
            not in flagged
        ):
            revert[rid] = b.get(rid)
    if not revert:
        return after, []
    return apply_row_restore(after, revert), sorted(revert)


def _reverted_bar_sections(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    reverted: list[str],
    scope: list[str],
    clip_bar_ids: frozenset[str],
) -> list[str]:
    b, a = _row_map(before) or {}, _row_map(after) or {}
    return [
        bar_change_section(b.get(rid), a.get(rid), scope, clip_bar_ids=clip_bar_ids)
        for rid in reverted
    ]


def changed_sections(
    data: dict[str, Any],
    before: dict[str, Any],
    scope: list[str],
    *,
    clip_bar_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """Sections a (repaired) commit payload really changes, in SECTION_ORDER. Drives the draft
    receipt, so it never claims a change layer 3 took back."""
    found: set[str] = set()
    for lane in lanes_in(data):
        if lane == "text_elements":
            b = _row_map(before.get("text_elements")) or {}
            a = _row_map(data.get("text_elements")) or {}
            for rid in row_diff(before.get("text_elements"), data.get("text_elements")) or {}:
                found.add(
                    bar_change_section(b.get(rid), a.get(rid), scope, clip_bar_ids=clip_bar_ids)
                )
        elif lane == "caption_meta":
            found.update("captions" if k == "enabled" else "look" for k in data["caption_meta"])
        elif lane in FIELD_SECTIONS:
            found.add(FIELD_SECTIONS[lane][0])
    return [s for s in SECTION_ORDER if s in found]


def repair_note(report: RepairReport, already_left_alone: list[str]) -> str:
    """ "I left title alone." for sections layer 3 took back (layer 2 already reported its own)."""
    sections = [
        s for s in SECTION_ORDER if s in report.reverted_sections and s not in already_left_alone
    ]
    if not sections:
        return ""
    phrase = scope_phrase(sections)
    return say(en=f"I left {phrase} alone.", tr=f"{phrase.capitalize()} bölümüne dokunmadım.")


def repair_compiled(
    compiled: Any,
    scope: list[str],
    *,
    clip_bar_ids: frozenset[str] = frozenset(),
) -> tuple[Any, RepairReport]:
    """Layer 3 on a ``CompiledEditorDraft``: returns ``(repaired compiled, report)``."""
    from app.routes.generative_jobs import EditorCommitRequest  # noqa: PLC0415

    payload = compiled.payload
    if not isinstance(payload, EditorCommitRequest):
        return compiled, RepairReport()
    data = payload.model_dump(mode="json", exclude_none=True)
    repaired, report = repair_payload(data, compiled.before, scope, clip_bar_ids=clip_bar_ids)
    if not report:
        return compiled, report
    gone = set(report.reverted_bars)
    # The receipt must not claim a change layer 3 took back: restate what is really left.
    left = changed_sections(repaired, compiled.before, scope, clip_bar_ids=clip_bar_ids)
    changes = (
        [
            say(
                en=f"Updated {scope_phrase(left)}.",
                tr=f"{scope_phrase(left).capitalize()} güncellendi.",
            )
        ]
        if left
        else []
    )
    return (
        dataclasses.replace(
            compiled,
            payload=EditorCommitRequest.model_validate(repaired),
            changes=changes,
            text_diff=[d for d in compiled.text_diff if str(d.get("id")) not in gone],
            before={k: v for k, v in compiled.before.items() if k not in report.reverted_fields}
            if report.reverted_fields
            else compiled.before,
        ),
        report,
    )


# ---------------------------------------------------------------------------------------
# Restore record (what Undo puts back)
# ---------------------------------------------------------------------------------------


def build_restore_record(
    data: dict[str, Any],
    before: dict[str, Any],
    scope: list[str],
    *,
    turn_id: str,
    base_job_id: str | None,
    clip_bar_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Per-section pre-update values of every lane the (repaired) update changed.

    ``{"v":1, "turn_id", "base_job_id", "sections": {section: {lane: spec}}}`` where a lane
    spec is ``{"rows": {id: row|None}}`` (text bars, caption cues) or ``{"value": ...}``.
    """
    sections: dict[str, dict[str, Any]] = {}

    def put(section: str, lane: str, spec: Any) -> None:
        sections.setdefault(section, {})[lane] = spec

    for lane in lanes_in(data):
        if lane == "text_elements":
            diff = row_diff(before.get("text_elements"), data.get("text_elements"))
            b = _row_map(before.get("text_elements")) or {}
            a = _row_map(data.get("text_elements")) or {}
            for rid, row in (diff or {}).items():
                section = bar_change_section(
                    b.get(rid), a.get(rid), scope, clip_bar_ids=clip_bar_ids
                )
                spec = sections.setdefault(section, {}).setdefault("text_elements", {"rows": {}})
                spec["rows"][rid] = row
        elif lane == "caption_cues":
            diff = row_diff(before.get("caption_cues"), data.get("caption_cues"))
            if diff is not None:
                if diff:
                    put("captions", "caption_cues", {"rows": diff})
            elif "caption_cues" in before:
                put("captions", "caption_cues", {"value": before["caption_cues"]})
        elif lane == "caption_meta":
            prior = before.get("caption_meta") or {}
            for key in data["caption_meta"]:
                if key in prior:
                    section = "captions" if key == "enabled" else "look"
                    spec = sections.setdefault(section, {}).setdefault(
                        "caption_meta", {"value": {}}
                    )
                    spec["value"][key] = prior[key]
        elif lane in ("music_track_id", "remove_music"):
            if "music_track_id" in before:
                put("music", "music_track_id", {"value": before["music_track_id"]})
        elif lane == "mix":
            if isinstance(before.get("mix"), dict):
                put("music", "mix", {"value": before["mix"]})
        elif lane in before and lane in FIELD_SECTIONS:
            put(FIELD_SECTIONS[lane][0], lane, {"value": before[lane]})
    return {"v": 1, "turn_id": turn_id, "base_job_id": base_job_id, "sections": sections}


def record_sections(record: dict[str, Any] | None) -> list[str]:
    """Sections an update changed (and so can restore), in SECTION_ORDER."""
    if not isinstance(record, dict) or not isinstance(record.get("sections"), dict):
        return []
    return [s for s in SECTION_ORDER if record["sections"].get(s)]


def restore_section(
    record: dict[str, Any], section: str, head_payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Commit lanes that put ``section`` back, plus the record that would undo THAT (so
    Undo toggles). ``head_payload`` is the head draft's flat ``editor_payload``."""
    spec_by_lane = (record.get("sections") or {}).get(section) or {}
    lanes: dict[str, Any] = {}
    toggle: dict[str, Any] = {}
    for lane, spec in spec_by_lane.items():
        if lane in ("text_elements", "caption_cues"):
            current = head_payload.get(lane)
            if "rows" in spec and isinstance(current, list):
                lanes[lane] = apply_row_restore(current, spec["rows"])
                cur = _row_map(current) or {}
                toggle[lane] = {"rows": {rid: cur.get(rid) for rid in spec["rows"]}}
            elif "value" in spec:
                lanes[lane] = spec["value"]
                toggle[lane] = {"value": current}
        elif lane == "music_track_id":
            value = spec["value"]
            if value is None:
                lanes["remove_music"] = True
            else:
                lanes["music_track_id"] = value
            toggle[lane] = {
                "value": None
                if head_payload.get("remove_music")
                else head_payload.get("music_track_id")
            }
        elif lane in ("mix", "caption_meta"):
            lanes[lane] = dict(spec["value"])
            current = head_payload.get(lane) or {}
            toggle[lane] = {"value": {k: current[k] for k in spec["value"] if k in current}}
        else:
            lanes[lane] = spec["value"]
            toggle[lane] = {"value": head_payload.get(lane)}
    toggle_record = {
        "v": 1,
        "turn_id": record.get("turn_id"),
        "base_job_id": record.get("base_job_id"),
        "sections": {section: {k: v for k, v in toggle.items() if v is not None}},
    }
    return lanes, toggle_record


def head_restore_record(execution_result: dict[str, Any] | None) -> dict[str, Any] | None:
    record = (execution_result or {}).get(_RESTORE_KEY)
    return record if isinstance(record, dict) and record.get("sections") else None


def restore_result_key() -> str:
    return _RESTORE_KEY


def merge_toggle_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine several single-section toggle records (Undo all)."""
    merged: dict[str, Any] = {"v": 1, "sections": {}}
    for record in records:
        merged.setdefault("turn_id", record.get("turn_id"))
        merged.setdefault("base_job_id", record.get("base_job_id"))
        merged["sections"].update(record.get("sections") or {})
    return merged


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------------------
# DB: current block revision, restore turn, per-section undo, Undo all
# ---------------------------------------------------------------------------------------

_ACTIVE_TURN_STATUSES = {
    "pending",
    "queued",
    "planning",
    "executing",
    "awaiting_approval",
    "observing",
}
_APPROVAL_TTL = timedelta(minutes=30)


async def current_block(
    db: AsyncSession, thread_id: uuid.UUID, section: str
) -> dict[str, Any] | None:
    """The current block of ``section``: ``GET /plan``'s reducer over the thread's
    ``plan_block`` events (only the LATEST job's events count; a higher revision replaces and
    within a revision the state only moves forward)."""
    from app.kria import plan_blocks  # noqa: PLC0415
    from app.models import CreationThreadEvent  # noqa: PLC0415

    rows = (
        (
            await db.execute(
                select(CreationThreadEvent.payload)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == plan_blocks.EVENT_TYPE,
                )
                .order_by(CreationThreadEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    payloads = [p for p in rows if isinstance(p, dict)]
    if not payloads:
        return None
    return plan_blocks.reduce_job_blocks(payloads, str(payloads[-1].get("job_id"))).get(section)


def scoped_update_blocks(
    reduced: dict[str, dict[str, Any]],
    scope: list[str],
    payloads: dict[str, dict[str, Any] | None],
    *,
    job_id: str,
) -> list[dict[str, Any]]:
    """The ``decided`` blocks a scoped editor update (or a section undo) adds to the feed.

    A scoped update re-renders the SAME Job, so the feed's "previous job" bookkeeping
    (``plan_blocks.annotate_blocks``) never sees a change. Here ``reduced`` is the job's
    current blocks and ``payloads`` the freshly committed variant's payloads: a section in
    ``scope`` whose value differs gets ``revision + 1``, ``changed=True`` and ``previous``
    (what it replaced). Sections outside the scope, skipped or never-decided sections, and
    sections whose value did not move are left alone. Pure.
    """
    from app.kria import plan_blocks  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for section in SCOPABLE_SECTIONS:
        prior = reduced.get(section)
        payload = payloads.get(section)
        if (
            section not in scope
            or payload is None
            or prior is None
            or prior.get("state") != "decided"
            or prior.get("skipped")
        ):
            continue
        entry = plan_blocks.block(
            section,
            "decided",
            plan_blocks.summary_from_payload(section, payload) or prior.get("summary"),
            None,
            intent=bool(prior.get("intent")),
            payload=payload,
        )
        if plan_blocks.same_value(entry, prior):
            continue
        prior_revision = plan_blocks.block_revision(prior)
        entry["revision"] = prior_revision + 1
        entry["changed"] = True
        entry["previous"] = {
            "revision": prior_revision,
            "job_id": job_id,
            "summary": prior.get("summary"),
            "payload": prior.get("payload"),
            "skipped": False,
        }
        out.append(entry)
    return out


def emit_scoped_update_feed(
    db: Any,
    thread: Any,
    *,
    turn_id: str | None,
    job: Any,
    variant: dict[str, Any],
    scope: list[str],
) -> list[str]:
    """Append the feed events of a scoped editor update (or a section undo) that was just
    queued: the changed sections' ``decided`` blocks, then the "Updated X." summary. Sync, run
    inside the dispatch transaction under the Thread lock the caller already holds. Returns
    the changed section ids (empty = nothing moved, nothing written). Never marks an
    unchanged section changed; the caller wraps this in a savepoint so a feed problem can
    never fail a dispatch."""
    from app.kria import plan_blocks, plan_payloads  # noqa: PLC0415
    from app.kria.reply_language import reply_language_for, thread_reply_language  # noqa: PLC0415
    from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415

    job_id = str(job.id)
    events = plan_blocks.load_plan_events(db, thread.id)
    reduced = plan_blocks.reduce_job_blocks(events, job_id)
    payloads = plan_payloads.finalized(plan_payloads.variant_raws(variant))
    blocks = scoped_update_blocks(reduced, scope, payloads, job_id=job_id)
    if not blocks:
        return []
    _append_sync_event(
        db,
        thread,
        role="system",
        event_type=plan_blocks.EVENT_TYPE,
        content=None,
        payload=plan_blocks.plan_block_payload(
            turn_id=turn_id, job_id=job_id, blocks=blocks, scope=scope
        ),
    )
    changed = [str(entry["section_id"]) for entry in blocks]
    with reply_language_for(thread_reply_language(thread)):
        text = plan_blocks.update_summary_text(changed)
    _append_sync_event(
        db,
        thread,
        role="system",
        event_type=plan_blocks.SUMMARY_EVENT_TYPE,
        content=None,
        payload={
            "turn_id": turn_id,
            "job_id": job_id,
            "text": text,
            "changed_sections": [s for s in SECTION_ORDER if s in changed],
        },
    )
    log.info("plan_scoped_update_feed", job_id=job_id, sections=changed)
    return changed


def _undo_reply(sections: list[str]) -> str:
    phrase = scope_phrase(sections)
    return say(
        en=f"I put {phrase} back to how it was.",
        tr=f"{phrase.capitalize()} önceki hâline döndürüldü.",
    )


async def _mint_restore_turn(
    db: AsyncSession,
    *,
    target: Any,
    session: Any,
    thread: Any,
    head: Any,
    job: Any,
    variant: dict[str, Any],
    lanes: dict[str, Any],
    toggle_record: dict[str, Any],
    sections: list[str],
    kind: str,
) -> tuple[Any, uuid.UUID]:
    """Insert the restoring draft revision (parent = head) and a render-only successor turn
    awaiting the client's auto-approval. The caller holds every lock down to the Thread and
    commits. Returns ``(draft row, successor turn id)``."""
    from app.kria.contracts import KriaTurnPlan  # noqa: PLC0415
    from app.kria.drafts import (  # noqa: PLC0415
        KriaDraftDocument,
        _insert_revision,
        canonical_snapshot,
    )
    from app.kria.runtime import _append_event  # noqa: PLC0415
    from app.models import (  # noqa: PLC0415
        CreatorAgentApproval,
        CreatorAgentExecution,
        CreatorAgentTurn,
    )
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415

    generation = variant_render_baseline(variant) or target.generation_id
    # The commit validates against the variant's CURRENT baseline; align the session pin the
    # same way `_complete_draft_turn` does for an editor Save.
    session.target_generation_id = generation
    target = dataclasses.replace(target, generation_id=generation)
    head_snapshot = head.snapshot_json or {}
    reply = _undo_reply(sections)
    document = KriaDraftDocument(
        kind="editor",
        intent=reply,
        edit_format=str(head_snapshot.get("edit_format") or "montage"),
        editor_payload={"base_generation": generation, **lanes},
        changes=[reply],
        brief_binding=head_snapshot.get("brief_binding"),
        brief_coverage=head_snapshot.get("brief_coverage"),
    )
    row = await _insert_revision(db, target=target, document=document, parent=head)
    snapshot, snapshot_hash = canonical_snapshot(document)  # noqa: F841 - hash is the row's
    turn_id = uuid.uuid4()
    section_scope = [s for s in SCOPABLE_SECTIONS if s in sections]
    source = await _append_event(
        db,
        thread,
        role="system",
        event_type="plan_section_undo" if kind == "section" else "plan_undo_all",
        content=None,
        payload={
            "turn_id": str(turn_id),
            "scope": section_scope,
            "sections": section_scope,
            "runtime_version": 2,
        },
    )
    request_digest = digest({"kind": kind, "sections": section_scope, "draft": str(row.id)})
    plan = KriaTurnPlan(
        mode="act",
        turn_value="action",
        intents=[
            {
                "intent_id": "request-render",
                "tool_name": "render.request",
                "tool_version": 1,
                "arguments": {},
            }
        ],
    )
    turn = CreatorAgentTurn(
        id=turn_id,
        thread_id=thread.id,
        session_id=session.id,
        source_event_id=source.id,
        client_event_id=f"plan-undo-{turn_id.hex}",
        request_digest=request_digest,
        status="awaiting_approval",
        plan_json=plan.model_dump(mode="json"),
    )
    db.add(turn)
    await db.flush()
    now = datetime.now(UTC)
    draft_execution = CreatorAgentExecution(
        session_id=session.id,
        turn_id=turn.id,
        idempotency_key=f"kria:{turn.id}:restore-draft",
        request_digest=request_digest,
        expected_revision=int(session.revision),
        expected_manifest_hash=session.manifest_hash,
        tool_name="draft.apply_editor_ops",
        tool_version=1,
        risk="reversible_draft",
        dependency_group=0,
        group_order=0,
        target_thread_id=thread.id,
        target_draft_id=row.id,
        target_draft_revision=row.draft_revision,
        status="completed",
        result={
            "snapshot_hash": row.snapshot_hash,
            "changes": [reply],
            "draft_id": str(row.id),
            "draft_revision": row.draft_revision,
            _RESTORE_KEY: toggle_record,
        },
        started_at=now,
        completed_at=now,
    )
    db.add(draft_execution)
    await db.flush()
    row.source_execution_id = draft_execution.id
    render_execution = CreatorAgentExecution(
        session_id=session.id,
        turn_id=turn.id,
        idempotency_key=f"kria:{turn.id}:request-render",
        request_digest=request_digest,
        expected_revision=int(session.revision),
        expected_manifest_hash=session.manifest_hash,
        tool_name="render.request",
        tool_version=1,
        risk="approval_required",
        dependency_group=1,
        group_order=1,
        target_thread_id=thread.id,
        target_draft_id=row.id,
        target_draft_revision=row.draft_revision,
        target_job_id=job.id,
        target_variant_id=session.target_variant_id,
        target_generation_id=generation,
        target_manifest_hash=session.manifest_hash,
        target_ownership_epoch=int(session.ownership_epoch),
        status="awaiting_approval",
        result={"consequence": "Start one render from this exact draft.", "creator_request": reply},
        started_at=now,
        awaiting_approval_at=now,
    )
    db.add(render_execution)
    await db.flush()
    approval = CreatorAgentApproval(
        creator_id=thread.creator_id,
        thread_id=thread.id,
        session_id=session.id,
        turn_id=turn.id,
        draft_id=row.id,
        draft_revision=row.draft_revision,
        target_job_id=job.id,
        target_variant_id=session.target_variant_id,
        target_generation_id=generation,
        target_manifest_hash=session.manifest_hash,
        target_ownership_epoch=int(session.ownership_epoch),
        execution_ids=[str(render_execution.id)],
        consequence_summary=f"Render this draft: {reply}",
        cost_summary=say(en="One render", tr="Tek bir video"),
        status="pending",
        expires_at=now + _APPROVAL_TTL,
    )
    db.add(approval)
    await db.flush()
    render_execution.result = {**(render_execution.result or {}), "approval_id": str(approval.id)}
    if kind == "section":
        observed = await _append_event(
            db,
            thread,
            role="assistant",
            event_type="draft_applied",
            content=reply,
            payload={
                "turn_id": str(turn.id),
                "draft_id": str(row.id),
                "draft_revision": row.draft_revision,
                "snapshot_hash": row.snapshot_hash,
                "changes": [reply],
                "can_undo": True,
                "receipt_ids": [str(draft_execution.id)],
            },
        )
    else:
        observed = await _append_event(
            db,
            thread,
            role="assistant",
            event_type="draft_undone",
            content=reply,
            payload={
                "draft_id": str(row.id),
                "draft_revision": row.draft_revision,
                "restored_from_draft_id": str(head.parent_draft_id)
                if head.parent_draft_id
                else None,
                "cancelled_approval_ids": [],
            },
        )
    await _append_event(
        db,
        thread,
        role="system",
        event_type="approval_requested",
        content=None,
        payload={
            "turn_id": str(turn.id),
            "approval_id": str(approval.id),
            "draft_id": str(row.id),
            "draft_revision": row.draft_revision,
            "consequence_summary": approval.consequence_summary,
            "cost_summary": approval.cost_summary,
            "expires_at": approval.expires_at.isoformat(),
            "artifact_key": f"approval:{approval.id}",
        },
    )
    turn.observed_event_id = observed.id
    return row, turn_id


async def _restore_context(
    db: AsyncSession, *, thread_id: uuid.UUID, creator_id: uuid.UUID
) -> dict[str, Any]:
    """Lock the creation graph in canonical order (Plan, PlanItem via the draft target, Job,
    Session, Turn, Draft, Execution) and validate that a restore is possible. The Thread is
    locked by the caller afterwards."""
    from app.kria.drafts import _head, _target  # noqa: PLC0415
    from app.kria.runtime import RuntimeFailure  # noqa: PLC0415
    from app.models import (  # noqa: PLC0415
        CreatorAgentExecution,
        CreatorAgentSession,
        CreatorAgentTurn,
        Job,
    )

    target = await _target(db, thread_id=thread_id, creator_id=creator_id, lock_item=True)

    def not_undoable(message: str) -> RuntimeFailure:
        return RuntimeFailure(
            409,
            "plan_section_not_undoable",
            message,
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )

    if target.job is None:
        raise not_undoable("There is nothing to undo yet.")
    job = (
        await db.execute(select(Job).where(Job.id == target.job.id).with_for_update())
    ).scalar_one()
    session = (
        await db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == target.thread.active_creator_agent_session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None or session.target_job_id != job.id:
        raise not_undoable("The video changed. Refresh and try again.")
    active = (
        await db.execute(
            select(CreatorAgentTurn.id)
            .where(
                CreatorAgentTurn.thread_id == thread_id,
                CreatorAgentTurn.status.in_(_ACTIVE_TURN_STATUSES),
            )
            .order_by(CreatorAgentTurn.id)
            .with_for_update()
        )
    ).first()
    if active is not None:
        raise not_undoable("A render is in progress. Wait for it to finish.")
    variant = next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants") or []
            if isinstance(v, dict) and str(v.get("variant_id")) == target.variant_key
        ),
        None,
    )
    if variant is None or variant.get("render_status") != "ready":
        raise not_undoable("The video is not ready to change.")
    head = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=True)
    execution = (
        (
            await db.execute(
                select(CreatorAgentExecution)
                .where(CreatorAgentExecution.id == head.source_execution_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if head is not None and head.source_execution_id is not None
        else None
    )
    return {
        "target": target,
        "job": job,
        "session": session,
        "variant": variant,
        "head": head,
        "record": head_restore_record(execution.result if execution is not None else None),
        "not_undoable": not_undoable,
    }


async def undo_section(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    section_id: str,
    body: PlanSectionUndoBody,
) -> PlanSectionUndoOut:
    """``POST /creation-threads/{id}/plan/sections/{section_id}/undo`` (KRI-442).

    Restores ONE section to its pre-update value as a new draft revision (parent = head) and
    mints a render-only successor turn the client auto-approves. Lock order is the canonical
    one `undo_draft` uses; the Thread row is last.
    """
    from app.kria.runtime import RuntimeFailure, _owned_thread  # noqa: PLC0415
    from app.services.creation_thread_titles import conversation_revision_matches  # noqa: PLC0415

    if not settings.live_plan_review_enabled:
        raise RuntimeFailure(
            404, "live_plan_review_unavailable", "Plan review is unavailable", phase="tool"
        )
    if section_id not in SECTION_ORDER:
        raise RuntimeFailure(404, "plan_section_not_found", "Unknown plan section", phase="tool")
    if section_id == "post_caption":
        raise RuntimeFailure(
            422,
            "scope_section_unsupported",
            "The post caption can't be changed from here yet.",
            phase="tool",
        )
    ctx = await _restore_context(db, thread_id=thread_id, creator_id=creator_id)
    target, head, record = ctx["target"], ctx["head"], ctx["record"]
    not_undoable = ctx["not_undoable"]
    if head is None or head.draft_revision != body.expected_draft_revision:
        raise RuntimeFailure(
            409,
            "draft_stale",
            "A newer edit is already saved. Refresh before undoing.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    if head.base_job_id != ctx["job"].id:
        raise RuntimeFailure(
            409,
            "draft_base_stale",
            "The rendered video changed while this was open.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    block = await current_block(db, thread_id, section_id)
    if block is None or block.get("state") != "decided":
        raise not_undoable("That part hasn't finished updating.")
    current_revision = int(block.get("revision") or 0)
    if current_revision != body.expected_block_revision:
        raise RuntimeFailure(
            409,
            "plan_section_stale",
            "That part changed again. Refresh and try again.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=current_revision,
        )
    if not block.get("changed") or section_id not in record_sections(record):
        raise not_undoable("There is nothing to undo for that part.")
    thread = await _owned_thread(
        db, thread_id=thread_id, creator_id=creator_id, lock=True, require_active=False
    )
    if not await conversation_revision_matches(db, thread, body.expected_thread_revision):
        raise RuntimeFailure(
            409,
            "thread_revision_stale",
            "The project changed before this undo.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    flat = (head.snapshot_json or {}).get("editor_payload") or {}
    lanes, toggle = restore_section(record, section_id, flat)
    if not lanes:
        raise not_undoable("There is nothing to undo for that part.")
    row, turn_id = await _mint_restore_turn(
        db,
        target=target,
        session=ctx["session"],
        thread=thread,
        head=head,
        job=ctx["job"],
        variant=ctx["variant"],
        lanes=lanes,
        toggle_record=toggle,
        sections=[section_id],
        kind="section",
    )
    result = PlanSectionUndoOut(
        section_id=section_id,  # type: ignore[arg-type]
        thread_revision=int(thread.revision),
        draft_revision=row.draft_revision,
        turn_id=str(turn_id),
    )
    await db.commit()
    log.info(
        "plan_section_undone",
        thread_id=str(thread_id),
        section_id=section_id,
        from_revision=current_revision,
    )
    return result


async def undo_all_with_render(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    expected_revision: int,
) -> Any:
    """``POST /draft/undo`` with ``render=true``: restore every section the LATEST update
    changed, then mint the same render-only successor as a section undo. Returns the new
    ``DraftSnapshotOut``."""
    from app.kria.drafts import _out  # noqa: PLC0415
    from app.kria.runtime import RuntimeFailure, _owned_thread  # noqa: PLC0415

    if not settings.live_plan_review_enabled:
        raise RuntimeFailure(
            404, "live_plan_review_unavailable", "Plan review is unavailable", phase="tool"
        )
    ctx = await _restore_context(db, thread_id=thread_id, creator_id=creator_id)
    target, head, record = ctx["target"], ctx["head"], ctx["record"]
    if head is None or head.draft_revision != expected_revision:
        raise RuntimeFailure(
            409,
            "draft_stale",
            "A newer edit is already saved. Refresh before undoing.",
            phase="tool",
            recovery="refresh_replan",
            current_revision=int(target.thread.revision),
        )
    sections = record_sections(record)
    if not sections or head.base_job_id != ctx["job"].id:
        raise RuntimeFailure(
            409,
            "draft_undo_unavailable",
            "There is no update to undo.",
            phase="tool",
            recovery="none",
            current_revision=int(target.thread.revision),
        )
    thread = await _owned_thread(
        db, thread_id=thread_id, creator_id=creator_id, lock=True, require_active=False
    )
    flat = (head.snapshot_json or {}).get("editor_payload") or {}
    lanes: dict[str, Any] = {}
    toggles: list[dict[str, Any]] = []
    for section in sections:
        section_lanes, toggle = restore_section(record, section, flat)
        # Section restores can address the same lane (text bars, caption cues): apply them in
        # sequence over the already-restored list.
        for lane, value in section_lanes.items():
            lanes[lane] = value
        flat = {**flat, **section_lanes}
        toggles.append(toggle)
    row, _turn_id = await _mint_restore_turn(
        db,
        target=target,
        session=ctx["session"],
        thread=thread,
        head=head,
        job=ctx["job"],
        variant=ctx["variant"],
        lanes=lanes,
        toggle_record=merge_toggle_records(toggles),
        sections=sections,
        kind="all",
    )
    result = _out(row, can_undo=True)
    await db.commit()
    log.info("plan_undo_all", thread_id=str(thread_id), sections=sections)
    return result


__all__ = [
    "CAPTION_META_OP",
    "FIELD_SECTIONS",
    "NEVER_IN_SCOPE",
    "SECTION_COMMIT_FIELDS",
    "SECTION_FAMILIES",
    "SECTION_OPS",
    "ManualEditTargetMissing",
    "RepairReport",
    "ScopeError",
    "apply_row_restore",
    "bar_change_section",
    "build_restore_record",
    "changed_sections",
    "current_block",
    "edit_section",
    "filter_ops_to_scope",
    "head_restore_record",
    "left_alone_note",
    "manual_edits_to_ops",
    "nothing_in_scope_reply",
    "parse_ops_without_model",
    "record_sections",
    "repair_compiled",
    "repair_note",
    "repair_payload",
    "restore_section",
    "row_diff",
    "scope_phrase",
    "scoped_snapshot",
    "section_of_text_bar",
    "undo_all_with_render",
    "undo_section",
    "validate_scope",
]
