"""HTTP contracts for the gated Kria runtime-v2 control plane."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents._schemas.visual_block import VisualBlock
from app.kria.contracts import KriaProblem
from app.kria.draft_schemas import DraftSnapshotOut as DraftSnapshotOut
from app.kria.plan_contract import ManualEdit
from app.routes.generative_jobs import EditorCommitRequest, TimelineSlotEdit
from app.schemas.user_song import SongOrderAnswerIn
from app.services.choice_questions import ChoiceSelectionIn
from app.services.clip_selection import ClipSelectionIn

TurnStatus = Literal[
    "pending",
    "queued",
    "planning",
    "executing",
    "awaiting_approval",
    "observing",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]
ThreadStatus = Literal["active", "archived", "failed"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired", "cancelled", "consumed"]


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


EDITOR_STATE_MAX_BYTES = 256 * 1024
_EDITOR_STATE_LANE_MAX = 200
# Fields that only make sense on an editor Save (receipts, suggestion resolution,
# guided revision CAS); a turn's editor state must never carry them.
_SAVE_ONLY_LANE_FIELDS = (
    "copilot_receipt_ids",
    "accepted_suggestion_ids",
    "retry_guided_revision",
    "guided_revision",
    "guided_revision_number",
)


class EditorStateTimelineSlot(TimelineSlotEdit):
    """A Save timeline row plus the media identity of an UNSAVED added clip.

    A saved row's media is recoverable from ``clip_index`` against the persisted
    sources; a clip the creator just added is not in them yet, so the client names it
    here (all optional, ignored by the Save route which uses ``TimelineSlotEdit``).
    """

    media_id: str | None = Field(default=None, max_length=128)
    media_kind: Literal["image", "video"] | None = None
    source_duration_s: float | None = Field(default=None, ge=0.0, le=36000.0)


class EditorStateLanes(EditorCommitRequest):
    """``EditorCommitRequest`` as a turn's editor state (adds list caps)."""

    text_elements: list[dict] | None = Field(default=None, max_length=_EDITOR_STATE_LANE_MAX)
    caption_cues: list[dict] | None = Field(default=None, max_length=_EDITOR_STATE_LANE_MAX)
    visual_blocks: list[VisualBlock] | None = Field(default=None, max_length=_EDITOR_STATE_LANE_MAX)
    motion_scenes: list[dict] | None = Field(default=None, max_length=_EDITOR_STATE_LANE_MAX)
    camera_effects: list[dict] | None = Field(default=None, max_length=_EDITOR_STATE_LANE_MAX)
    timeline_slots: list[EditorStateTimelineSlot] | None = None  # type: ignore[assignment]


class EditorStateIn(_StrictBody):
    """The editor's CURRENT UNSAVED state, sent with a chat turn.

    A lane present is the client's full current value; absent equals the saved variant.
    A present-but-empty ``lanes`` is still authoritative: "no unsaved edits".
    The server never recomputes a hash; ``client_state_id`` is echoed verbatim.
    """

    version: Literal[1] = 1
    base_generation: str = Field(max_length=128)
    client_state_id: str = Field(min_length=1, max_length=64)
    lanes: EditorStateLanes

    @model_validator(mode="after")
    def _bounded_and_save_free(self) -> EditorStateIn:
        # An explicit null / empty / false is a no-op default, not a Save-only payload.
        offending = [k for k in _SAVE_ONLY_LANE_FIELDS if getattr(self.lanes, k, None)]
        if offending:
            raise ValueError(f"editor_state.lanes must not carry Save-only fields: {offending}")
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > EDITOR_STATE_MAX_BYTES:
            raise ValueError(f"editor_state must be at most {EDITOR_STATE_MAX_BYTES} bytes")
        return self


class SubmitTurnBody(_StrictBody):
    message: str = Field(min_length=1, max_length=4000)
    client_event_id: str = Field(min_length=1, max_length=160)
    expected_thread_revision: int = Field(ge=0)
    # Additive and dark (KRIA_EDITOR_STATE_TURNS_ENABLED); old clients omit it.
    editor_state: EditorStateIn | None = None
    # KRI-282: the answer to a thumbnail clip question; old clients omit it.
    clip_selection: ClipSelectionIn | None = None
    # KRI-374: the creator's confirmed take order for a `song_order_question`.
    song_order: SongOrderAnswerIn | None = None
    # KRI-282: the answer to a conflict-choice question; old clients omit it.
    choice_selection: ChoiceSelectionIn | None = None
    # KRI-441 (live plan & review, dark behind LIVE_PLAN_REVIEW_ENABLED): the plan
    # sections this turn may change. Typed `str` so an unknown/duplicate/empty list gets
    # the contract's `scope_invalid` problem (runtime.validate_scope), not a generic 422.
    scope: list[str] | None = Field(default=None, max_length=32)
    # Deterministic edits (no model call); require `scope`, each covered by it.
    manual_edits: list[ManualEdit] | None = Field(default=None, max_length=12)

    @model_validator(mode="after")
    def _manual_edits_well_formed(self) -> SubmitTurnBody:
        for edit in self.manual_edits or []:
            if edit.kind == "rewrite_text" and (not edit.target_id or not edit.text):
                raise ValueError("rewrite_text needs target_id and text")
            if edit.kind == "set_mix" and all(
                value is None
                for value in (edit.music_level, edit.original_level, edit.music_gain_db)
            ):
                raise ValueError("set_mix needs at least one level")
        return self

    @field_validator("message")
    @classmethod
    def _normalize_message(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("message must not be blank")
        return value

    @field_validator("client_event_id")
    @classmethod
    def _opaque_client_event_id(cls, value: str) -> str:
        value = value.strip()
        if not value or any(token in value for token in ("/", "\\", "..")):
            raise ValueError("client_event_id must be opaque")
        return value


class TurnAccepted(BaseModel):
    turn_id: str
    thread_revision: int
    status: TurnStatus
    replayed: bool = False


class TurnCancelBody(_StrictBody):
    expected_thread_revision: int = Field(ge=0)


class TurnCancelled(BaseModel):
    turn_id: str
    thread_revision: int
    status: Literal["cancelled"]
    approval_ids: list[str] = Field(default_factory=list)


class ApprovalDecisionBody(_StrictBody):
    expected_thread_revision: int = Field(ge=0)
    expected_draft_revision: int = Field(ge=0)
    expected_approval_fingerprint: str = Field(min_length=64, max_length=64)
    # KRI-205 phone speech-cleanup offer: an aware client (one that renders the
    # question) submits its exact analysis id/choice; `speech_cleanup_aware`
    # distinguishes it from an older build with no cleanup UI at all, which
    # must never be blocked by enforce mode (see `decide_approval`'s legacy
    # default). None of these three fields feed `approval_fingerprint()` --
    # that hash is computed from the server-authored `CreatorAgentApproval`
    # row alone, never from this request body.
    speech_cleanup_aware: bool = False
    speech_cleanup_analysis_id: uuid.UUID | None = None
    speech_cleanup_choice: Literal["clean", "keep_original", "create_without_cleanup"] | None = None
    # KRI-306: the creator's finished-video shape, sent only when the thread's
    # `render_shape` projection offered a choice. Optional and NOT part of
    # `approval_fingerprint()` -- a shape is a setting on an already-reviewed
    # direction, so changing it must never invalidate the approval.
    output_orientation: Literal["portrait", "landscape"] | None = None
    landscape_fit: Literal["fit", "fill"] | None = None

    @field_validator("expected_approval_fingerprint")
    @classmethod
    def _lowercase_sha256(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("expected_approval_fingerprint must be lowercase SHA-256")
        return value


class ApprovalDecisionOut(BaseModel):
    approval_id: str
    turn_id: str
    status: Literal["approved", "denied"]
    thread_revision: int
    # Approval only records explicit consent. A future exact-state executor
    # consumes it and dispatches through the existing Job dispatcher.
    render_dispatched: Literal[False] = False


class ApprovalSnapshotOut(BaseModel):
    approval_id: str
    turn_id: str
    draft_id: str | None = None
    draft_revision: int | None = None
    status: ApprovalStatus
    consequence_summary: str
    cost_summary: str | None = None
    expires_at: datetime
    approval_fingerprint: str = Field(min_length=64, max_length=64)


class DraftWriteBody(_StrictBody):
    expected_draft_revision: int = Field(ge=0)
    snapshot: dict[str, Any]


class DraftUndoBody(_StrictBody):
    expected_draft_revision: int = Field(ge=0)
    # Live plan & review "Undo all": also re-render the restored state (KRI-442).
    render: bool = False


class DeltaEvent(BaseModel):
    id: str
    client_event_id: str | None = None
    sequence: int
    revision: int
    role: Literal["user", "assistant", "system"]
    event_type: str
    content: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class ThreadDeltaOut(BaseModel):
    thread_id: str
    runtime_version: Literal[2]
    status: ThreadStatus
    thread_revision: int
    events: list[DeltaEvent]
    after_sequence: int
    next_after_sequence: int
    before_sequence: int | None = None
    previous_before_sequence: int | None = None
    has_more: bool


class KriaProblemOut(BaseModel):
    problem: KriaProblem
