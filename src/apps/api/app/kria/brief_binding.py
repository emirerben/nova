"""Immutable approval authority, read independently of rollout flags or live memory."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from app.kria.brief import CreativeBrief, render_brief_request

# Opens the block appended to an approved request for the requirements the creator removed.
SUPERSEDED_REQUIREMENTS_HEADING = "Superseded requirements (historical only; do not reintroduce)."


class BriefBindingRequestTooLongError(ValueError):
    """An approved request cannot retain its full source safely."""


class BriefBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str
    state: Literal["pinned", "none"]
    brief: CreativeBrief | None = None
    creator_request: str = ""
    media_snapshot: dict = {}
    # KRI-476 (PR-C): the creator's answers to material conflicts that approval froze.
    # Part of the digest ONLY when non-empty, so every stored binding keeps its digest.
    # Kept out of the derived JSON schema (and omitted when empty): the mobile OpenAPI
    # subset models this class with additionalProperties=false, so the wire shape every
    # existing client decodes must not move.
    choice_answers: SkipJsonSchema[list[dict]] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    # KRI-827: the creator's own marks of what to keep text off ("the plate": a box on one
    # frame of one of their clips), folded from their chat turns at approval
    # (`protected_targets.fold_protected_regions`). Server-owned like the answers above, in
    # the digest only when non-empty, so every stored binding keeps its digest.
    protected_regions: SkipJsonSchema[list[dict]] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    # KRI-881: the sourced wording and citations the creator saw before approving, from the
    # server's own research step (`app.kria.research_preview`). The render replays it and
    # never searches again. Server-owned like the fields above, in the digest only when
    # present, so every stored binding keeps its digest and wire shape.
    prepared_research: SkipJsonSchema[dict | None] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    digest: str

    @staticmethod
    def _digest(
        thread_id: str,
        state: str,
        brief: CreativeBrief | None,
        request: str,
        media_snapshot: dict | None = None,
        choice_answers: list[dict] | None = None,
        protected_regions: list[dict] | None = None,
        prepared_research: dict | None = None,
    ) -> str:
        payload = {
            "thread_id": thread_id,
            "state": state,
            "brief": brief.model_dump(mode="json") if brief else None,
            "creator_request": request,
        }
        if media_snapshot:
            payload["media_snapshot"] = media_snapshot
        if choice_answers:
            payload["choice_answers"] = choice_answers
        if protected_regions:
            payload["protected_regions"] = protected_regions
        if prepared_research is not None:
            payload["prepared_research"] = prepared_research
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def create(
        cls,
        thread_id: object,
        brief: CreativeBrief | None,
        *,
        latest_message: str = "",
        full_creator_request: str | None = None,
        media_snapshot: dict | None = None,
        choice_answers: list[dict] | None = None,
        protected_regions: list[dict] | None = None,
        prepared_research: dict | None = None,
    ) -> BriefBinding:
        snapshot = brief.model_copy(deep=True) if brief is not None else None
        state = "pinned" if snapshot is not None else "none"
        brief_request = render_brief_request(snapshot, latest_message=latest_message)
        if full_creator_request is not None:
            raw = full_creator_request.strip()
            request = (
                f"Full creator request (chronological):\n{raw}\n\n{brief_request}"
                if raw and brief_request
                else raw or brief_request
            )
        else:
            request = brief_request
        if snapshot is not None:
            omitted = [req for req in snapshot.requirements if not req.live]
            if omitted:
                # Chronological chat is evidence, not permission to resurrect an
                # earlier request the creator subsequently removed (KRI-698).
                request += (
                    f"\n\n{SUPERSEDED_REQUIREMENTS_HEADING} "
                    "The current creative brief above is authoritative:\n"
                    + "\n".join(f"- {req.id}: {req.text()}" for req in omitted)
                )
        if len(request) > 12_000:
            raise BriefBindingRequestTooLongError("Approved creator request exceeds the safe limit")
        media = copy.deepcopy(media_snapshot or {})
        answers = copy.deepcopy(list(choice_answers or []))
        regions = copy.deepcopy(list(protected_regions or []))
        research = copy.deepcopy(prepared_research) if prepared_research is not None else None
        return cls(
            thread_id=str(thread_id),
            state=state,
            brief=snapshot,
            creator_request=request,
            media_snapshot=media,
            choice_answers=answers,
            protected_regions=regions,
            prepared_research=research,
            digest=cls._digest(
                str(thread_id), state, snapshot, request, media, answers, regions, research
            ),
        )

    @model_validator(mode="after")
    def _verify(self) -> BriefBinding:
        if (self.state == "pinned") != (self.brief is not None):
            raise ValueError("Brief binding state disagrees with snapshot")
        if self.digest != self._digest(
            self.thread_id,
            self.state,
            self.brief,
            self.creator_request,
            self.media_snapshot,
            self.choice_answers,
            self.protected_regions,
            self.prepared_research,
        ):
            raise ValueError("Brief binding digest mismatch")
        return self

    def resolve(self, thread_id: object | None = None) -> CreativeBrief | None:
        self._verify()
        if thread_id is not None and self.thread_id != str(thread_id):
            raise ValueError("Brief binding belongs to another thread")
        return self.brief.model_copy(deep=True) if self.brief is not None else None


# KRI-1069: the snapshot key listing the item's Visuals-pool media beside its clips.
SNAPSHOT_VISUALS_KEY = "visuals"


def snapshot_visual(asset: object) -> dict:
    """One Visuals-pool row (``PlanItemAsset``) as the snapshot keeps it: its manifest id,
    kind, capture context and the file name the app shows."""
    capture = getattr(asset, "capture", None)
    return {
        "media_id": f"asset-{getattr(asset, 'id', '')}",
        "kind": getattr(asset, "kind", None),
        "capture": dict(capture) if isinstance(capture, dict) and capture else None,
        "source_filename": getattr(asset, "source_filename", None),
    }


def snapshot_media(item: object, *, visuals: list | None = None) -> dict:
    """Keep the approved source identities AND analysis, in the existing draft/job.

    Analysis answers may be enriched later, but cannot replace evidence used by an
    accepted plan. Source paths and timing are server-owned and never accepted
    from a client.  The scalar durations are the attach-time probes; song
    analysis supplies the same song duration plus authoritative lyric/section
    timing when the analysis is ready.  No output target is used as a fallback.

    ``visuals`` (KRI-1069) are the item's Visuals-pool rows: each is kept with its own
    capture time (``snapshot_visual``), so a filming-order promise can date a selected
    photo like a clip. ``None`` (the default) leaves the key out, byte-identical.
    """
    voiceover_duration_s = getattr(item, "voiceover_duration_s", None)
    song_duration_s = getattr(item, "song_duration_s", None)
    extra = (
        {SNAPSHOT_VISUALS_KEY: [snapshot_visual(row) for row in visuals]}
        if visuals is not None
        else {}
    )
    return copy.deepcopy(
        {
            "clip_paths": list(getattr(item, "clip_gcs_paths", None) or []),
            "clip_assignments": list(getattr(item, "clip_assignments", None) or []),
            "voiceover_path": getattr(item, "voiceover_gcs_path", None),
            "voiceover_generation": getattr(item, "voiceover_generation", None),
            # Keep both the scalar and named shape: KRI-497 capacity readers
            # can consume this without deriving a duration from a strategy.
            "voiceover_duration_s": voiceover_duration_s,
            "voiceover": (
                {"duration_s": voiceover_duration_s}
                if isinstance(voiceover_duration_s, (int, float)) and voiceover_duration_s > 0
                else None
            ),
            "song_path": getattr(item, "song_gcs_path", None),
            "song_generation": getattr(item, "song_generation", None),
            "song_duration_s": song_duration_s,
            # SongAnalysis contains the source duration and its timed lines;
            # SongAlignment records server-resolved per-take song positions.
            "song_analysis": getattr(item, "song_analysis", None),
            "song_alignment": getattr(item, "song_alignment", None),
            **extra,
        }
    )


def media_identity(snapshot: dict) -> dict:
    """Only source identity/version changes invalidate approval, not new analysis."""
    keys = (
        "media_id",
        "asset_id",
        "gcs_path",
        "storage_path",
        "gcs_generation",
        "storage_generation",
        "generation",
        "duration_probe_generation",
        "upload_id",
        "kind",
        "trim_start_s",
        "trim_end_s",
        "shot_id",
        "manifest_identity",
    )
    return {
        "clip_paths": snapshot.get("clip_paths") or [],
        "voiceover_path": snapshot.get("voiceover_path"),
        "voiceover_generation": snapshot.get("voiceover_generation"),
        "song_path": snapshot.get("song_path"),
        "song_generation": snapshot.get("song_generation"),
        "clips": [
            {k: row[k] for k in keys if k in row}
            for row in snapshot.get("clip_assignments") or []
            if isinstance(row, dict)
        ],
    }
