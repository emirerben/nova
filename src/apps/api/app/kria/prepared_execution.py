"""Prepared execution snapshot: one immutable identity per approved edit (KRI-722).

``decide_approval`` mints one snapshot row in the same transaction that approves a
draft. Every later step (dispatch claim, Job stamp, device publication, receipts,
retry, draft pruning) references it by id and verifies it against live rows. A
retry keeps the snapshot id and gets a new ``attempt_id``.

The snapshot is execution identity, not a new creative strategy. It records which
draft, brief, sources, capabilities and decisions the creator approved, as digests
and server-owned identifiers. It never stores signed URLs or secrets.

Unstamped Jobs (historical, or minted with ``KRIA_PREPARED_EXECUTION_ENABLED`` off)
take the legacy path byte for byte. Workers branch on the stamp, never on the flag.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kria.approval_readiness import digest
from app.kria.media_sources import is_analysis_proxy_path

SCHEMA_VERSION = 1
# Bump when what a prepared snapshot promises changes meaning for the compiler
# (KRI-723/724/725 move concrete decisions in). A worker refuses a snapshot whose
# version is above its own, instead of silently executing different decisions.
COMPILER_VERSION = 1

STAMP_FIELD = "creator_prepared_execution"
# Per-variant authority: an editor approval or exact retry re-pins ONE variant, so it
# fences only that variant and never overwrites its siblings' (or the Job's) stamp.
VARIANT_STAMPS_FIELD = "creator_prepared_execution_variants"
RESULT_FIELD = "prepared_execution"

STALE = "prepared_execution_stale"
UNSUPPORTED = "prepared_execution_unsupported"
SOURCE_LINEAGE = "prepared_execution_source_lineage"
SUPERSEDED = "prepared_execution_superseded"

# KRI-725: a slide post's approved draft. Its digest is a compiled ref of the snapshot
# and the dispatched Job carries the exact approved draft, so the cloud slides worker
# renders what was approved instead of whatever ``PlanItem.slide_post`` says later.
SLIDE_POST_REF = "slide_post"
APPROVED_SLIDE_POST_FIELD = "approved_slide_post"
# Bookkeeping the slides route rewrites after a render; never an approved decision.
_SLIDE_POST_BOOKKEEPING = frozenset({"rendered_version", "user_edited"})

Origin = Literal["creator", "creator_answer", "model_default", "system"]
Role = Literal["original", "analysis_proxy"]

# Strategy fields grouped into independently invalidated decisions. Clearing one
# instruction in a new draft changes only that group's digest.
_DECISION_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset(
        {
            "opening_title",
            "opening_title_duration_s",
            "closing_title",
            "intro_hook",
            "shot_labels",
            "pinned_texts",
            "song_captions",
            "caption_style",
            "font_family",
            "text_color",
            "title_animation",
            "label_position",
            "omitted_copy_targets",
        }
    ),
    "length": frozenset({"target_duration_s", "duration_approval"}),
    "order": frozenset(
        {
            "selected_media_ids",
            "ordering_choice",
            "clip_intents",
            "resolved_clip_intents",
            "story_structure",
            "hero_media_id",
            "closing_media",
            "archetype",
            "editorial_plan",
            "confirmed_order_replacement",
            "media_scope",
            "reaction_beats",
        }
    ),
    "audio": frozenset(
        {
            "audio_strategy",
            "song_sync",
            "resolved_song_takes",
            "voice_mode",
            "selected_music_section",
        }
    ),
}
# Advisory prose and creator answers are not execution decisions of their own:
# answers are part of the binding digest already.
_IGNORED_STRATEGY_FIELDS = frozenset({"rationale", "choice_answers"})

# Substrings that mark a URL or a credential. Server identifiers never contain them.
_SECRET_MARKERS = (
    "http://",
    "https://",
    "x-goog-signature",
    "x-goog-credential",
    "token=",
    "signature=",
    "sig=",
)


class PreparedExecutionError(ValueError):
    """A stamped snapshot no longer matches the live inputs it authorized."""

    def __init__(self, code: str, detail: str, changed: Iterable[str] = ()) -> None:
        super().__init__(detail)
        self.code = code
        self.changed = sorted(set(changed))


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: str
    media_id: str | None = None
    path: str
    role: Role
    generation: str | None = None
    # Canonical time basis: the source window the edit is allowed to read.
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    duration_s: float | None = None

    def fence(self) -> dict[str, Any]:
        """What must not change. Duration is an attach-time probe kept for audit."""
        return self.model_dump(mode="json", exclude={"duration_s"})


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    digest: str
    origin: Origin


class PreparedExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    compiler_version: int = COMPILER_VERSION
    creator_id: str
    thread_id: str
    approval_id: str
    draft_id: str
    draft_revision: int
    draft_snapshot_hash: str
    draft_kind: str
    route: str
    readiness_digest: str | None = None
    brief_version: int = 0
    brief_digest: str
    binding_digest: str | None = None
    sources: list[SourceIdentity] = Field(default_factory=list)
    readiness_capabilities_digest: str | None = None
    worker_capabilities_digest: str
    decisions: dict[str, Decision] = Field(default_factory=dict)
    # References to compiled outputs this approval already pinned (editor payload,
    # device recipe revision, guided attempt). KRI-723+ adds compiler outputs here.
    compiled_refs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_secrets(self) -> PreparedExecutionSnapshot:
        _reject_secrets(self.model_dump(mode="json"))
        return self

    @property
    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def decision_digest(self) -> str:
        return digest({key: value.digest for key, value in self.decisions.items()})


def _reject_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)
    elif isinstance(value, str) and any(
        marker in value.lower()  # kri667: scans server paths/ids for URLs, never creator text
        for marker in _SECRET_MARKERS
    ):
        raise ValueError("A prepared execution snapshot cannot hold URLs or credentials")


def worker_capabilities_digest() -> str:
    """Capabilities this process can execute. Recomputed synchronously at claim."""
    from app.config import settings  # noqa: PLC0415
    from app.services.creator_render_contract import (  # noqa: PLC0415
        ADAPTER_DECLARATIONS,
        FIELD_MATRIX,
    )

    return digest(
        {
            "compiler_version": COMPILER_VERSION,
            "verified_phone_features": sorted(settings.phone_render_verified_features),
            "field_matrix": FIELD_MATRIX,
            "adapters": ADAPTER_DECLARATIONS,
        }
    )


def _role(row: Mapping[str, Any], path: str, library_role: str | None) -> Role:
    if library_role in {"original", "analysis_proxy"}:
        return library_role  # type: ignore[return-value]
    contract = row.get("upload_contract")
    purpose = contract.get("purpose") if isinstance(contract, Mapping) else None
    if purpose in {"original", "analysis_proxy"}:
        return purpose  # type: ignore[return-value]
    return "analysis_proxy" if is_analysis_proxy_path(path) else "original"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def targets_slides(item: Any, document: Any = None) -> bool:
    """Whether an approval renders ``item`` as a slide post.

    A strategy draft sets the item's format at claim, so its own ``edit_format`` wins;
    any other draft renders the item's current format.
    """
    strategy = getattr(document, "strategy", None) if document is not None else None
    if getattr(document, "kind", None) == "strategy" and isinstance(strategy, Mapping):
        # exclude_none dumps may omit edit_format: fall back to the item's format.
        fmt = strategy.get("edit_format") or getattr(item, "edit_format", "")
        return str(fmt or "") == "slides"
    return str(getattr(item, "edit_format", "") or "") == "slides"


def slide_post_draft(item: Any) -> Any:
    """The item's parsed slide draft, or None (no draft, or unreadable)."""
    from app.schemas.slide_post import parse_slide_post  # noqa: PLC0415

    return parse_slide_post(getattr(item, "slide_post", None))


def slide_post_digest(draft: Any) -> str:
    """Digest of what a creator approves in a slide post (None digests too: fail closed)."""
    if draft is None:
        return digest(None)
    return digest(draft.model_dump(mode="json", exclude=set(_SLIDE_POST_BOOKKEEPING)))


def slide_asset_slots(draft: Any) -> list[tuple[str, str]]:
    """(slot, asset id) for every slide and pasted sticker, in render order."""
    slots: list[tuple[str, str]] = []
    for index, ref in enumerate(getattr(draft, "slides", None) or []):
        slots.append((f"slide:{index}", str(ref.asset_id)))
        for sticker in (ref.edits.stickers or []) if ref.edits is not None else []:
            slots.append((f"sticker:{index}:{sticker.id}", str(sticker.asset_id)))
    return slots


def source_identities(
    item: Any,
    *,
    asset_generations: Mapping[str, str | None],
    library_sources: Mapping[str, tuple[str, str]],
    slide_assets: Mapping[str, tuple[str, str | None]] | None = None,
) -> list[SourceIdentity]:
    """Pure: the item's render sources with generation and original/proxy lineage.

    ``asset_generations`` maps a PlanItemAsset path to its GCS generation;
    ``library_sources`` maps a media-library object path to (role, generation).
    ``slide_assets`` (slide posts only, KRI-725) maps a PlanItemAsset id to
    (path, generation); None leaves a non-slide snapshot byte-identical.
    """

    rows = [r for r in (getattr(item, "clip_assignments", None) or []) if isinstance(r, Mapping)]
    by_path = {str(r.get("gcs_path") or r.get("storage_path") or ""): r for r in rows}
    sources: list[SourceIdentity] = []
    for index, path in enumerate(getattr(item, "clip_gcs_paths", None) or []):
        path = str(path)
        row = by_path.get(path, {})
        library = library_sources.get(path)
        generation = (
            (library[1] if library else None)
            or asset_generations.get(path)
            or next(
                (
                    str(row[key])
                    for key in ("gcs_generation", "storage_generation", "generation")
                    if row.get(key) not in (None, "")
                ),
                None,
            )
        )
        sources.append(
            SourceIdentity(
                slot=f"clip:{index}",
                media_id=str(row["media_id"]) if row.get("media_id") else None,
                path=path,
                role=_role(row, path, library[0] if library else None),
                generation=generation,
                trim_start_s=_number(row.get("trim_start_s")),
                trim_end_s=_number(row.get("trim_end_s")),
                duration_s=_number(row.get("duration_s")),
            )
        )
    for slot in ("voiceover", "song"):
        path = getattr(item, f"{slot}_gcs_path", None)
        if not path:
            continue
        generation = getattr(item, f"{slot}_generation", None)
        sources.append(
            SourceIdentity(
                slot=slot,
                path=str(path),
                role="analysis_proxy" if is_analysis_proxy_path(str(path)) else "original",
                generation=str(generation) if generation is not None else None,
                duration_s=_number(getattr(item, f"{slot}_duration_s", None)),
            )
        )
    if slide_assets is not None:
        for slot, asset_id in slide_asset_slots(slide_post_draft(item)):
            path, generation = slide_assets.get(asset_id, ("", None))
            sources.append(
                SourceIdentity(
                    slot=slot,
                    media_id=asset_id,
                    path=path,
                    role="analysis_proxy" if is_analysis_proxy_path(path) else "original",
                    generation=generation,
                )
            )
    return sources


def _slide_query(item: Any, slides: bool):  # noqa: ANN202
    """The item's own slide/sticker asset rows (never another item's), or None."""
    if not slides:
        return None
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import PlanItemAsset  # noqa: PLC0415

    ids = []
    for _slot, asset_id in slide_asset_slots(slide_post_draft(item)):
        try:
            ids.append(uuid.UUID(asset_id))
        except ValueError:
            continue
    return select(PlanItemAsset.id, PlanItemAsset.gcs_path, PlanItemAsset.gcs_generation).where(
        PlanItemAsset.plan_item_id == item.id, PlanItemAsset.id.in_(ids)
    )


def _slide_map(rows) -> dict[str, tuple[str, str | None]]:  # noqa: ANN001
    return {
        str(asset_id): (str(path), str(gen) if gen is not None else None)
        for asset_id, path, gen in rows
    }


def _source_queries(item: Any, owner: Any):  # noqa: ANN202
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import MediaAssetSource, PlanItemAsset  # noqa: PLC0415

    paths = [str(p) for p in (getattr(item, "clip_gcs_paths", None) or [])]
    assets = select(PlanItemAsset.gcs_path, PlanItemAsset.gcs_generation).where(
        PlanItemAsset.plan_item_id == item.id, PlanItemAsset.gcs_path.in_(paths)
    )
    library = select(
        MediaAssetSource.object_path, MediaAssetSource.role, MediaAssetSource.object_generation
    ).where(
        MediaAssetSource.user_id == owner,
        MediaAssetSource.object_path.in_(paths),
        MediaAssetSource.status == "available",
    )
    return assets, library


def _maps(asset_rows, library_rows):  # noqa: ANN001, ANN202
    return (
        {str(path): (str(gen) if gen is not None else None) for path, gen in asset_rows},
        {str(path): (str(role), str(gen)) for path, role, gen in library_rows},
    )


async def load_source_identities(
    db: Any, item: Any, owner: Any, *, slides: bool = False
) -> list[SourceIdentity]:
    assets, library = _source_queries(item, owner)
    asset_map, library_map = _maps(
        (await db.execute(assets)).all(), (await db.execute(library)).all()
    )
    slide_query = _slide_query(item, slides)
    return source_identities(
        item,
        asset_generations=asset_map,
        library_sources=library_map,
        slide_assets=(
            _slide_map((await db.execute(slide_query)).all()) if slide_query is not None else None
        ),
    )


def load_source_identities_sync(
    db: Any, item: Any, owner: Any, *, slides: bool = False
) -> list[SourceIdentity]:
    assets, library = _source_queries(item, owner)
    asset_map, library_map = _maps(db.execute(assets).all(), db.execute(library).all())
    slide_query = _slide_query(item, slides)
    return source_identities(
        item,
        asset_generations=asset_map,
        library_sources=library_map,
        slide_assets=(
            _slide_map(db.execute(slide_query).all()) if slide_query is not None else None
        ),
    )


def _creator_authored_editor(document: Any) -> bool:
    """Only the creator's own editor state, carried unchanged, is creator-authored.

    Kria's chat edits (compiled from model tool arguments), undo baselines and
    comparison drafts are model or system output the creator approved, never intent.
    """
    return (
        document.client_state_id is not None
        and not document.changes
        and document.editor_text_diff is None
    )


def build_decisions(
    document: Any,
    *,
    execution_result: Mapping[str, Any] | None = None,
) -> dict[str, Decision]:
    """Per-key digests of what the creator approved. Origin comes from provenance only.

    A strategy group is written by the planner, so it is ``model_default`` even when a
    brief requirement asked for something in it: field-level creator attribution
    belongs to the requirement ledger (KRI-824), not to a whole digested group.
    """

    decisions: dict[str, Decision] = {}
    if document.kind == "strategy" and isinstance(document.strategy, Mapping):
        grouped: dict[str, dict[str, Any]] = {}
        for field, value in document.strategy.items():
            if field in _IGNORED_STRATEGY_FIELDS:
                continue
            key = next((k for k, fields in _DECISION_FIELDS.items() if field in fields), None)
            grouped.setdefault(key or "direction", {})[field] = value
        for key, values in sorted(grouped.items()):
            decisions[key] = Decision(digest=digest(values), origin="model_default")
    elif document.kind == "editor":
        origin: Origin = "creator" if _creator_authored_editor(document) else "model_default"
        decisions["editor"] = Decision(digest=digest(document.editor_payload), origin=origin)
        if document.editor_text_diff is not None:
            decisions["text"] = Decision(digest=digest(document.editor_text_diff), origin=origin)
    result = execution_result or {}
    # Explicit approval-body choices are the creator's answers at the approval card.
    for key, field in (("shape", "render_shape"), ("speech_cleanup", "speech_cleanup")):
        if isinstance(result.get(field), Mapping):
            decisions[key] = Decision(digest=digest(result[field]), origin="creator_answer")
    from app.services.render_shape import shape_is_creator_pick  # noqa: PLC0415

    shape = result.get("render_shape")
    if isinstance(shape, Mapping) and not shape_is_creator_pick(shape):
        # The app echoes the preselected shape back untouched: that is the server's
        # default, not an answer the creator gave.
        decisions["shape"] = Decision(digest=digest(shape), origin="model_default")
    return decisions


def build_snapshot(
    *,
    creator_id: Any,
    thread_id: Any,
    approval_id: Any,
    draft: Any,
    document: Any,
    sources: list[SourceIdentity],
    execution_result: Mapping[str, Any] | None = None,
    latest_brief: Any = None,
    slide_post: Any = None,
) -> PreparedExecutionSnapshot:
    """Pure function of stored rows: replaying it makes no new decision.

    Without a brief binding (creators outside that cohort) the brief version current
    at approval, ``latest_brief``, is pinned by version and digest instead.
    ``slide_post`` is ``(draft,)`` for a slide-post approval (the draft may be None)
    and None otherwise, so a non-slide snapshot gains no key (KRI-725).
    """

    binding = document.brief_binding
    brief = binding.resolve(thread_id) if binding is not None else latest_brief
    readiness = (document.brief_coverage or {}).get("approval_readiness")
    readiness = readiness if isinstance(readiness, Mapping) else {}
    route = document.kind
    if document.kind == "editor" and isinstance(document.editor_payload, Mapping):
        route = "speech_cut" if document.editor_payload.get("operation") == "speech_cut" else route
    compiled_refs: dict[str, str] = {}
    if document.kind == "editor" and document.editor_payload is not None:
        compiled_refs["editor_payload"] = digest(document.editor_payload)
    story = getattr(document, "prepared_collection_story", None)
    if story is not None:
        compiled_refs["prepared_collection_story"] = digest(story)
    if slide_post is not None:
        compiled_refs[SLIDE_POST_REF] = slide_post_digest(slide_post[0])
    return PreparedExecutionSnapshot(
        creator_id=str(creator_id),
        thread_id=str(thread_id),
        approval_id=str(approval_id),
        draft_id=str(draft.id),
        draft_revision=int(draft.draft_revision),
        draft_snapshot_hash=str(draft.snapshot_hash),
        draft_kind=document.kind,
        route=route,
        readiness_digest=readiness.get("digest"),
        brief_version=brief.version if brief is not None else 0,
        brief_digest=digest(brief.model_dump(mode="json") if brief is not None else None),
        binding_digest=binding.digest if binding is not None else None,
        sources=sources,
        readiness_capabilities_digest=readiness.get("capabilities_digest"),
        worker_capabilities_digest=worker_capabilities_digest(),
        decisions=build_decisions(document, execution_result=execution_result),
        compiled_refs=compiled_refs,
    )


def parse(row: Any) -> PreparedExecutionSnapshot:
    """Load a stored snapshot; refuse versions this worker does not understand."""
    raw = row.snapshot if hasattr(row, "snapshot") else row
    if not isinstance(raw, Mapping):
        raise PreparedExecutionError(STALE, "The prepared edit is unavailable.")
    if (
        int(raw.get("schema_version") or 0) > SCHEMA_VERSION
        or int(raw.get("compiler_version") or 0) > COMPILER_VERSION
    ):
        raise PreparedExecutionError(
            UNSUPPORTED, "This edit was prepared by a newer version. Prepare it again."
        )
    try:
        snapshot = PreparedExecutionSnapshot.model_validate(raw)
    except ValueError as exc:
        raise PreparedExecutionError(STALE, "The prepared edit is unreadable.") from exc
    stored = getattr(row, "snapshot_digest", None)
    if stored is not None and stored != snapshot.digest:
        raise PreparedExecutionError(STALE, "The prepared edit was altered.")
    return snapshot


def verify(
    snapshot: PreparedExecutionSnapshot,
    *,
    draft: Any,
    document: Any,
    thread_id: Any,
    sources: list[SourceIdentity] | None,
    item: Any = None,
) -> None:
    """Raise when a live approval input no longer matches the snapshot.

    ``sources=None`` skips the source fence (an accepted redelivery already passed it).
    A slide-post snapshot also fences the live draft (``item.slide_post``).
    """

    if (
        str(draft.id) != snapshot.draft_id
        or int(draft.draft_revision) != snapshot.draft_revision
        or str(draft.snapshot_hash) != snapshot.draft_snapshot_hash
        or str(thread_id) != snapshot.thread_id
    ):
        raise PreparedExecutionError(STALE, "The approved draft changed.", ["draft"])
    binding = document.brief_binding
    if (binding.digest if binding is not None else None) != snapshot.binding_digest:
        raise PreparedExecutionError(STALE, "The approved request changed.", ["brief"])
    if worker_capabilities_digest() != snapshot.worker_capabilities_digest:
        raise PreparedExecutionError(
            STALE, "The available rendering features changed.", ["capabilities"]
        )
    if sources is None:
        return
    promoted_proxy = any(s.role == "original" and is_analysis_proxy_path(s.path) for s in sources)
    same_slots = [(s.slot, s.path) for s in sources] == [(s.slot, s.path) for s in snapshot.sources]
    if promoted_proxy or (
        same_slots and [s.role for s in sources] != [s.role for s in snapshot.sources]
    ):
        raise PreparedExecutionError(
            SOURCE_LINEAGE, "An approved source's original/proxy lineage changed.", ["sources"]
        )
    if [s.fence() for s in sources] != [s.fence() for s in snapshot.sources]:
        raise PreparedExecutionError(STALE, "An approved source changed.", ["sources"])
    approved_slides = snapshot.compiled_refs.get(SLIDE_POST_REF)
    if approved_slides is not None and (
        item is None or slide_post_digest(slide_post_draft(item)) != approved_slides
    ):
        raise PreparedExecutionError(STALE, "The approved slide post changed.", [SLIDE_POST_REF])


def changed_decisions(
    before: PreparedExecutionSnapshot, after: PreparedExecutionSnapshot
) -> list[str]:
    """Decision keys whose approved value differs (scoped invalidation, KRI-723+)."""
    keys = set(before.decisions) | set(after.decisions)
    return sorted(k for k in keys if before.decisions.get(k) != after.decisions.get(k))


def new_attempt_id() -> str:
    return uuid.uuid4().hex


def attempt_record(row: Any, snapshot_digest: str, attempt_id: str, **extra: Any) -> dict:
    """The stamp carried on execution.result and the Job. Ids only, never content."""
    return {
        "id": str(row.id),
        "digest": snapshot_digest,
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        **{k: v for k, v in extra.items() if v is not None},
    }


def read_stamp(container: Mapping[str, Any] | None) -> dict[str, Any] | None:
    stamp = (container or {}).get(STAMP_FIELD)
    return stamp if isinstance(stamp, dict) and stamp.get("id") else None


def variant_stamp(assembly: Mapping[str, Any] | None, variant_id: str) -> dict[str, Any] | None:
    """The full stamp record that authorizes ``variant_id``, or None (legacy).

    A per-variant stamp (editor approval / exact retry) wins; otherwise the Job-level
    strategy stamp covers every variant of its Job.
    """
    per_variant = (assembly or {}).get(VARIANT_STAMPS_FIELD)
    if isinstance(per_variant, dict):
        stamp = per_variant.get(variant_id)
        if isinstance(stamp, dict) and stamp.get("id"):
            return stamp
    stamp = read_stamp(assembly)
    if stamp is None or stamp.get("variant_id") not in (None, variant_id):
        return None
    return stamp


def variant_fence(assembly: Mapping[str, Any] | None, variant_id: str) -> dict[str, str] | None:
    """The (snapshot, attempt) a variant's device record must carry, or None (legacy)."""
    stamp = variant_stamp(assembly, variant_id)
    if stamp is None:
        return None
    return {"id": str(stamp["id"]), "attempt_id": str(stamp.get("attempt_id") or "")}


def stamp_job(job: Any, record: Mapping[str, Any]) -> None:
    """Plain JSON sibling of the plan-authority stamp; absent = legacy Job.

    A record naming ``variant_id`` is stored under that variant only, so sibling
    variants keep the authority their in-flight device records were pinned under.
    """
    ids = {k: record[k] for k in ("id", "digest", "schema_version")}
    variant_id = record.get("variant_id")
    candidates = dict(job.all_candidates or {})
    assembly = dict(job.assembly_plan or {})
    if variant_id is None:
        candidates[STAMP_FIELD] = ids
        assembly[STAMP_FIELD] = dict(record)
    else:
        key = str(variant_id)
        candidates.setdefault(STAMP_FIELD, ids)
        candidates[VARIANT_STAMPS_FIELD] = {
            **(candidates.get(VARIANT_STAMPS_FIELD) or {}),
            key: ids,
        }
        assembly[VARIANT_STAMPS_FIELD] = {
            **(assembly.get(VARIANT_STAMPS_FIELD) or {}),
            key: dict(record),
        }
    job.all_candidates = candidates
    job.assembly_plan = assembly


def pinned_brief_for_job(db: Any, job: Any, thread_id: Any) -> tuple[bool, Any]:
    """(stamped, brief): the approved brief of a stamped Job, never the latest one.

    Returns ``(False, None)`` for an unstamped Job, or a snapshot approved with no
    brief at all, so callers keep the legacy read. A stamped Job whose pinned brief
    cannot be recovered exactly yields ``(True, None)``.
    """

    from sqlalchemy import select  # noqa: PLC0415

    from app.kria.brief import _brief_from_row  # noqa: PLC0415
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415
    from app.models import CreativeBriefVersion, CreatorPreparedExecution  # noqa: PLC0415

    stamp = read_stamp(job.assembly_plan)
    if stamp is None:
        return False, None
    try:
        row = db.get(CreatorPreparedExecution, uuid.UUID(str(stamp["id"])))
        snapshot = parse(row) if row is not None else None
        if snapshot is not None and snapshot.binding_digest is None:
            # No binding: the brief version current at approval is pinned on the snapshot.
            if snapshot.brief_version == 0:
                return False, None
            version_row = db.execute(
                select(CreativeBriefVersion).where(
                    CreativeBriefVersion.thread_id == uuid.UUID(str(thread_id)),
                    CreativeBriefVersion.version == snapshot.brief_version,
                )
            ).scalar_one_or_none()
            pinned = _brief_from_row(version_row)
            if pinned is None or digest(pinned.model_dump(mode="json")) != snapshot.brief_digest:
                return True, None
            return True, pinned
        raw = (job.assembly_plan or {}).get("creator_brief_binding")
        binding = BriefBinding.model_validate(raw) if isinstance(raw, dict) else None
    except (PreparedExecutionError, ValueError, TypeError):
        return True, None
    if snapshot is None or binding is None or binding.digest != snapshot.binding_digest:
        return True, None
    try:
        return True, binding.resolve(thread_id)
    except ValueError:
        return True, None


def verify_job_stamp(db: Any, job: Any, *, variant_id: str | None = None) -> dict[str, str] | None:
    """KRI-725: the cloud worker's start fence. ``{id, attempt_id}`` or None (legacy).

    Checks only the snapshot row's integrity and its identity with this Job, never the
    live capabilities: an accepted attempt must survive a deploy (KRI-722 F10 rule).
    ``variant_id`` fences one variant's (editor/retry) stamp instead of the Job's.
    Raises ``PreparedExecutionError`` before any render spend.
    """

    from app.models import CreatorPreparedExecution  # noqa: PLC0415

    assembly = job.assembly_plan if isinstance(job.assembly_plan, Mapping) else {}
    stamp = variant_stamp(assembly, variant_id) if variant_id else read_stamp(assembly)
    if stamp is None:
        return None
    try:
        row = db.get(CreatorPreparedExecution, uuid.UUID(str(stamp["id"])))
    except ValueError as exc:
        raise PreparedExecutionError(STALE, "The prepared edit is missing.") from exc
    if row is None or str(row.creator_id) != str(job.user_id):
        raise PreparedExecutionError(STALE, "The prepared edit is missing.")
    snapshot = parse(row)
    if row.invalidated_at is not None:
        raise PreparedExecutionError(
            row.invalidation_code or STALE, "The prepared edit was invalidated."
        )
    if stamp.get("digest") != snapshot.digest:
        raise PreparedExecutionError(STALE, "This render is not the approved edit.", ["stamp"])
    generation = stamp.get("generation_id")
    if generation is not None and generation != assembly.get("creator_generation_id"):
        raise PreparedExecutionError(
            STALE, "This render belongs to another approved attempt.", ["generation"]
        )
    if variant_id:
        return variant_fence(assembly, variant_id)
    return {"id": str(stamp["id"]), "attempt_id": str(stamp.get("attempt_id") or "")}


def pin_approved_slide_post(db: Any, job: Any, item: Any) -> None:
    """Copy the approved slide draft onto a stamped slides Job at dispatch.

    The claim fence has just proven the live draft equals the approved one, so the
    copy is the approved draft. Asset generations are pinned beside it so the worker
    can refuse a photo replaced after dispatch.
    """

    from app.models import CreatorPreparedExecution  # noqa: PLC0415

    stamp = read_stamp(job.assembly_plan)
    if stamp is None:
        return
    row = db.get(CreatorPreparedExecution, uuid.UUID(str(stamp["id"])))
    approved = parse(row).compiled_refs.get(SLIDE_POST_REF) if row is not None else None
    if approved is None:
        return
    draft = slide_post_draft(item)
    if draft is None or slide_post_digest(draft) != approved:
        raise PreparedExecutionError(STALE, "The approved slide post changed.", [SLIDE_POST_REF])
    query = _slide_query(item, True)
    job.assembly_plan = {
        **(job.assembly_plan or {}),
        APPROVED_SLIDE_POST_FIELD: {
            "draft": draft.model_dump(mode="json"),
            "digest": approved,
            "asset_generations": {
                asset_id: generation
                for asset_id, (_path, generation) in _slide_map(db.execute(query).all()).items()
            },
        },
    }


def approved_slide_post(assembly: Mapping[str, Any] | None) -> tuple[Any, dict[str, Any]] | None:
    """(draft, asset generations) a stamped slides Job must render, or None (live read).

    Raises when the pinned copy no longer matches its digest.
    """

    pinned = (assembly or {}).get(APPROVED_SLIDE_POST_FIELD)
    if not isinstance(pinned, Mapping) or read_stamp(assembly) is None:
        return None
    from app.schemas.slide_post import parse_slide_post  # noqa: PLC0415

    draft = parse_slide_post(pinned.get("draft"))
    if draft is None or slide_post_digest(draft) != pinned.get("digest"):
        raise PreparedExecutionError(STALE, "The approved slide post was altered.", ["slide_post"])
    generations = pinned.get("asset_generations")
    return draft, dict(generations) if isinstance(generations, Mapping) else {}
