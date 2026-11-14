"""Chat undo on the phone path (KRI-1384): "undo that" puts back what the last chat turn changed.

A committed chat edit turn stores the pre-turn value of every lane it changed: the KRI-442
restore record (``plan_review.build_restore_record``) over every section, on the draft's own
execution (``result["plan_review_before"]``). It is marked undoable from chat (``chat_undo``)
only when replaying it over the turn's own draft gives back exactly the editor the creator had
before the turn (``seal``), so a lane the record cannot restore never becomes a partial undo.

Undo is offered (``undo_point``) only while the head draft is still that turn's draft, built on
the variant's current render, and the editor the turn arrives with still holds what the chat
wrote in every lane and row the undo would restore. A hand edit to one of those, a Save, a
re-plan, another chat turn or an earlier undo (an undo turn stores no record) takes the offer
away. The point rides on the projected variant (``POINT_KEY``, never persisted), so the
snapshot (``history_state``), the copilot's family and the compile all read one value.

The phone shows the result only from protocol ``chat_undo_min_client_protocol``: an older build
keeps a staged chat lane that a newer head returns to the saved value.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

import structlog

from app.kria import plan_review
from app.kria.plan_contract import SCOPABLE_SECTIONS
from app.kria.reply_language import say
from app.routes.generative_jobs import EditorCommitRequest, variant_render_baseline
from app.services.kria_editor_ops import (
    CompiledEditorDraft,
    EditorStateReplyError,
    _DraftState,
    _variant_slots,
    _variant_split_sections,
    build_editor_snapshot,
    editor_payload_dump,
    merge_editor_draft,
    project_editor_draft,
)
from app.services.kria_editor_ops_diff import (
    EditorDiff,
    describe_diff,
    diff_compiled_state,
    snapshot_before,
)

UNDO_OP = "undo_last_edit"
POINT_KEY = "_kria_chat_undo"
RECORD_KEY = plan_review.restore_result_key()
MARK = "chat_undo"
_ROW_LANES = ("text_elements", "caption_cues")
# Lanes `merge_editor_draft` merges key by key, so restoring one is per key, deletes included.
_KEYED_LANES = ("caption_meta", "mix")
# Keys of a commit payload that are not edits (`kria_editor_ops._NOT_EDIT_KEYS` plus Save
# bookkeeping a compiled payload always carries).
_NOT_LANES = frozenset(
    {
        "base_generation",
        "copilot_receipt_ids",
        "accepted_suggestion_ids",
        "retry_guided_revision",
        "guided_revision",
        "guided_revision_number",
        "editor_state_version",
        "learning_temporary",
        "deletions",
        "sections",
        "remove_music",
    }
)
_MISSING = object()
log = structlog.get_logger()


@dataclasses.dataclass(frozen=True)
class CompiledUndo(CompiledEditorDraft):
    """An undo's compile. ``draft`` is the editor payload it writes: the merge of ``payload``
    over the current draft, minus the keys the record deletes. The commit stores it as is."""

    draft: dict[str, Any] = dataclasses.field(default_factory=dict)


class ChatUndoUnavailable(EditorStateReplyError):
    """The last chat turn can no longer be undone from chat; nothing was changed."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


def unavailable_reply() -> str:
    return say(
        en=(
            "I can't undo that from chat: the edit changed after my last change, or that "
            "change can't be put back exactly. Nothing was changed. Use Undo in the editor, "
            "or tell me what to change back."
        ),
        tr=(
            "Bunu sohbetten geri alamıyorum: son değişikliğimden sonra düzenleme değişti ya da "
            "o değişiklik tam olarak geri getirilemiyor. Hiçbir şey değişmedi. Editördeki Geri "
            "Al'ı kullan ya da neyi eski haline getireyim, söyle."
        ),
    )


def is_undo_bundle(ops: list[dict[str, Any]]) -> bool:
    return any(str(op.get("op") or "") == UNDO_OP for op in ops)


def _norm(value: Any) -> Any:  # noqa: ANN401
    """Value equality across JSON round trips: no nulls, no float noise, key order ignored."""
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_norm(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def _edit_lanes(payload: dict[str, Any]) -> set[str]:
    lanes = {key for key in payload if key not in _NOT_LANES}
    if payload.get("remove_music") is True:
        lanes.add("music_track_id")
    return lanes


def _music(payload: dict[str, Any]) -> Any:  # noqa: ANN401
    if payload.get("remove_music") is True:
        return None
    return payload.get("music_track_id", _MISSING)


def _lane(payload: dict[str, Any], lane: str) -> Any:  # noqa: ANN401
    return _music(payload) if lane == "music_track_id" else payload.get(lane, _MISSING)


def _pre_turn(lane: str, prior: dict[str, Any], before: dict[str, Any]) -> Any:  # noqa: ANN401
    """What the creator had in ``lane`` before the turn: their unsaved value, else the saved one."""
    if lane in ("caption_meta", "mix"):
        return {**(before.get(lane) or {}), **(prior.get(lane) or {})}
    mine = _lane(prior, lane)
    return before.get(lane, _MISSING) if mine is _MISSING else mine


def _ordered(rows: list[dict[str, Any]], order: list[str]) -> list[dict[str, Any]]:
    """Rows the record put back return to their pre-turn place; others keep theirs, after."""
    rank = {rid: index for index, rid in enumerate(order)}
    known = sorted(
        (row for row in rows if isinstance(row, dict) and str(row.get("id")) in rank),
        key=lambda row: rank[str(row.get("id"))],
    )
    rest = [row for row in rows if not (isinstance(row, dict) and str(row.get("id")) in rank)]
    return known + rest


def _replay(
    record: dict[str, Any], current: dict[str, Any], base_generation: str
) -> tuple[EditorCommitRequest, dict[str, Any]]:
    """The commit that puts ``record`` back over ``current``, and the draft it writes.

    A commit's None means "leave it", so the merge alone cannot take a ``caption_meta`` or
    ``mix`` key away. A key the record holds as None had no value before the turn, so the
    draft drops it. The merge also keeps every chat song key and never un-removes the song,
    so the record's ``user_song`` (the creator's whole section) replaces the chat's."""
    lanes: dict[str, Any] = {}
    flat = copy.deepcopy(current)
    for section in plan_review.record_sections(record):
        section_lanes, _ = plan_review.restore_section(record, section, flat)
        lanes.update(section_lanes)
        flat.update(section_lanes)
    for lane, ids in ((record.get(MARK) or {}).get("order") or {}).items():
        if isinstance(lanes.get(lane), list):
            lanes[lane] = _ordered(lanes[lane], ids)
    request = EditorCommitRequest.model_validate({"base_generation": base_generation, **lanes})
    restored = editor_payload_dump(request)
    draft = merge_editor_draft(current, restored)
    replaced: dict[str, Any] = {}
    for lane in _KEYED_LANES:
        gone = {k for k, v in (lanes.get(lane) or {}).items() if v is None}
        if gone and isinstance(draft.get(lane), dict):
            replaced[lane] = {k: v for k, v in draft[lane].items() if k not in gone}
    if "user_song" in restored:
        replaced["user_song"] = restored["user_song"]
    for lane, value in replaced.items():
        draft[lane] = value
        if isinstance(draft.get("sections"), dict) and lane in draft["sections"]:
            draft["sections"] = {**draft["sections"], lane: copy.deepcopy(value)}
    return request, draft


def _canon(lane: str, value: Any) -> Any:  # noqa: ANN401
    """``value`` as the Save would persist it: through the commit schema, defaults filled in.

    Pre-turn rows read off the variant lack defaults the replayed (validated) rows carry, so
    raw equality calls an exact restore different. A value the schema rejects compares raw."""
    if value is _MISSING:
        return _MISSING
    try:
        dumped = editor_payload_dump(
            EditorCommitRequest.model_validate({"base_generation": "", lane: value})
        )
    except Exception:  # noqa: BLE001 - not a commit value; raw equality decides
        return _norm(value)
    return _norm(dumped.get(lane))


def _same(lane: str, want: Any, got: Any) -> bool:  # noqa: ANN401
    """``got`` holds the creator's pre-turn ``want`` (for a keyed lane: every key ``want`` has)."""
    if want is _MISSING or got is _MISSING:
        return False
    if lane in _KEYED_LANES and isinstance(want, dict):
        got = {k: got.get(k) for k in want} if isinstance(got, dict) else got
    return _canon(lane, want) == _canon(lane, got)


def _visible(job: Any, saved: dict[str, Any], payload: dict[str, Any]) -> Any:  # noqa: ANN401
    """The editor as the copilot sees it with ``payload`` over the saved variant."""
    return _norm(build_editor_snapshot(job, project_editor_draft(saved, payload, job)))


def chat_record(
    data: dict[str, Any],
    before: dict[str, Any],
    *,
    turn_id: str,
    base_job_id: str,
    timeline_rebase: dict[str, tuple[Any, Any]] | None = None,
    clip_bar_ids: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """The KRI-442 restore record of an unscoped turn: every section it changed. None when it
    cannot be built: the edit still commits, it just cannot be undone from chat."""
    try:
        return plan_review.build_restore_record(
            data,
            before,
            list(SCOPABLE_SECTIONS),
            turn_id=turn_id,
            base_job_id=base_job_id,
            timeline_rebase=timeline_rebase,
            clip_bar_ids=clip_bar_ids,
        )
    except Exception:  # noqa: BLE001 - undo is optional; the chat edit must never fail on it
        log.warning("kria_chat_undo_record_failed", turn_id=turn_id, exc_info=True)
        return None


def seal(
    record: dict[str, Any] | None,
    *,
    job: Any,  # noqa: ANN401
    saved: dict[str, Any],
    prior: dict[str, Any],
    written: dict[str, Any],
    before: dict[str, Any],
    changes: list[str],
) -> dict[str, Any] | None:
    """``record`` marked undoable from chat, or None when undoing it would not be exact.

    ``prior`` is the flat editor payload the turn was built on, ``written`` the draft it
    committed and ``before`` the compiler's pre-op lanes. The check replays the undo exactly as
    the compile will (``_replay``) over ``written`` and requires every lane the turn changed, as
    the Save would persist it, and the editor as the copilot sees it, to equal what the creator
    had before. A turn that changed no lane has nothing to undo.
    """
    if not isinstance(record, dict) or not plan_review.record_sections(record):
        return None
    lanes = _edit_lanes(
        {
            k: v
            for k, v in written.items()
            if k not in prior or _norm(prior[k]) != _norm(v) or k == "remove_music"
        }
    )
    if all(_same(lane, _pre_turn(lane, prior, before), _lane(written, lane)) for lane in lanes):
        return None
    order = {
        lane: [str(row.get("id")) for row in pre if isinstance(row, dict)]
        for lane in _ROW_LANES
        if lane in lanes and isinstance(pre := _pre_turn(lane, prior, before), list)
    }
    marked = {**record, MARK: {"v": 1, "changes": list(changes)[:3], "order": order}}
    try:
        _, undone = _replay(marked, written, str(written.get("base_generation") or ""))
    except Exception:  # noqa: BLE001 - an unreplayable record is simply not offered
        return None
    if not all(_same(lane, _pre_turn(lane, prior, before), _lane(undone, lane)) for lane in lanes):
        return None
    try:
        if _visible(job, saved, undone) != _visible(job, saved, prior):
            return None
    except Exception:  # noqa: BLE001
        return None
    return marked


def _untouched(record: dict[str, Any], current: dict[str, Any], written: dict[str, Any]) -> bool:
    """Every lane and row the undo would restore still holds what the chat turn wrote."""
    for lanes in (record.get("sections") or {}).values():
        for lane, spec in lanes.items():
            if "rows" in spec:
                mine = plan_review._row_map(current.get(lane))  # noqa: SLF001
                wrote = plan_review._row_map(written.get(lane))  # noqa: SLF001
                if mine is None or wrote is None:
                    return False
                if any(_norm(mine.get(rid)) != _norm(wrote.get(rid)) for rid in spec["rows"]):
                    return False
            elif lane in ("caption_meta", "mix") and set(spec) == {"value"}:
                mine, wrote = current.get(lane) or {}, written.get(lane) or {}
                if any(_norm(mine.get(k)) != _norm(wrote.get(k)) for k in spec["value"]):
                    return False
            elif _norm(_lane(current, lane)) != _norm(_lane(written, lane)):
                return False
    return True


def undo_point(
    head: Any,  # noqa: ANN401
    result: dict[str, Any] | None,
    *,
    job: Any,  # noqa: ANN401
    variant: dict[str, Any],
    source: str,
    prior: dict[str, Any],
    client_state: Any,  # noqa: ANN401
    client_protocol: int | None,
) -> dict[str, Any] | None:
    """What an undo of the last chat turn would restore, or None when it is not on offer.

    ``variant`` is the saved render; ``source``/``prior`` are the turn's ``EditorBase``: the head
    draft, or the editor's unsaved state (``client_state``) over the saved render. The head must
    be the chat turn's own draft on this render; an editor state must have staged that very draft
    and still hold what it wrote.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.editor_head import payload_current  # noqa: PLC0415

    if client_protocol is None or client_protocol < settings.chat_undo_min_client_protocol:
        return None
    record = (result or {}).get(RECORD_KEY) if isinstance(result, dict) else None
    if (
        head is None
        or not isinstance(record, dict)
        or not isinstance(record.get(MARK), dict)
        or head.base_job_id != job.id
        or not payload_current(job, variant, head)
        or source not in ("head", "client_state")
    ):
        return None
    written = ((head.snapshot_json or {}).get("editor_payload")) or {}
    if not isinstance(written, dict):
        return None
    if source == "client_state" and (
        getattr(client_state, "draft_revision", None) != head.draft_revision
        or not _untouched(record, prior, written)
    ):
        return None
    return {
        "turn_id": record.get("turn_id"),
        "record": copy.deepcopy(record),
        "current": copy.deepcopy(prior),
        "changes": list(record[MARK].get("changes") or []),
    }


def attach(projected: dict[str, Any], point: dict[str, Any] | None) -> None:
    if point is not None:
        projected[POINT_KEY] = point


def available(variant: dict[str, Any]) -> bool:
    return isinstance(variant.get(POINT_KEY), dict)


def _state(job: Any, projected: dict[str, Any]) -> _DraftState:  # noqa: ANN401
    from app.agents._schemas.text_element import resolve_narrated_storyboard_rows  # noqa: PLC0415

    def rows(key: str) -> list[dict[str, Any]]:
        return copy.deepcopy([r for r in projected.get(key) or [] if isinstance(r, dict)])

    text = copy.deepcopy(
        [
            r
            for r in resolve_narrated_storyboard_rows(projected.get("text_elements"))
            if isinstance(r, dict)
        ]
    )
    return _DraftState(
        job=job,
        variant=projected,
        text=text,
        text_bars=list(text),
        captions=rows("caption_cues"),
        camera_effects=rows("camera_effects"),
        sound_effects=rows("sound_effects"),
        slots=_variant_slots(projected, job),
        base_generation=variant_render_baseline(projected),
        media_overlays=rows("media_overlays"),
        split_screen_sections=_variant_split_sections(projected),
    )


def _diff(
    job: Any,  # noqa: ANN401
    variant: dict[str, Any],
    data: dict[str, Any],
    request: EditorCommitRequest,
) -> tuple[EditorDiff, list[dict[str, Any]]]:
    from app.services.kria_editor_ops_text import compute_text_diff  # noqa: PLC0415

    after = _state(job, project_editor_draft(variant, data, job))
    after.changed = set(_edit_lanes(data))
    if request.mix is not None:
        after.mix_level = request.mix.music_level
        after.original_level = request.mix.original_level
    after.music_track_id = request.music_track_id
    after.remove_music = request.remove_music
    after.title = request.title
    if request.caption_meta is not None:
        after.caption_patch = {
            k: v
            for k, v in request.caption_meta.model_dump(exclude_none=True).items()
            if k != "font_set"
        }
    text_diff = compute_text_diff(job, variant, after.text) if "text_elements" in data else []
    try:
        diff = diff_compiled_state(
            job, variant, snapshot_before(_state(job, variant)), after, text_diff, [UNDO_OP]
        )
    except Exception:  # noqa: BLE001 - receipts only; never block the undo
        diff = EditorDiff(unavailable=True)
    return diff, text_diff


def compile_undo(
    job: Any, variant: dict[str, Any], ops: list[dict[str, Any]]
) -> CompiledEditorDraft:  # noqa: ANN401
    """The draft that puts back the last chat turn's lanes over the editor's current state."""
    if len(ops) != 1:
        raise ChatUndoUnavailable(
            say(
                en="Undo has to be its own request. Nothing was changed.",
                tr="Geri alma ayrı bir istek olmalı. Hiçbir şey değişmedi.",
            )
        )
    point = variant.get(POINT_KEY)
    if not isinstance(point, dict):
        raise ChatUndoUnavailable(unavailable_reply())
    request, draft = _replay(point["record"], point["current"], variant_render_baseline(variant))
    data = editor_payload_dump(request)
    lanes = sorted(_edit_lanes(data))
    if not lanes:
        raise ChatUndoUnavailable(unavailable_reply())
    diff, text_diff = _diff(job, variant, data, request)
    undone = "; ".join(point.get("changes") or []) or say(
        en="your last change", tr="son değişikliğin"
    )
    before = {}
    for lane in lanes:
        value = _lane(point["current"], lane)
        before[lane] = None if value is _MISSING else copy.deepcopy(value)
    return CompiledUndo(
        payload=request,
        changes=[say(en="Undid: {what}", tr="Geri alındı: {what}", what=undone)],
        text_diff=text_diff,
        diff=diff,
        before=before,
        draft=draft,
    )


def undo_reply(compiled: CompiledEditorDraft) -> str:
    """The receipt of an undo turn: what it put back, in the diff's words when it has them."""
    phrases = (
        describe_diff(compiled.diff.entries, live_text_count=compiled.diff.live_text_count)
        if not compiled.diff.unavailable
        else []
    )
    what = "; ".join(phrases) if phrases else "; ".join(compiled.changes)
    return say(
        en="I undid my last change ({what}). Save when you're happy with it.",
        tr="Son değişikliğimi geri aldım ({what}). Memnun kaldığında kaydet.",
        what=what,
    )
