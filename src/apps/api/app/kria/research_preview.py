"""Research before approval: show the sourced wording, then pin it to the approval (KRI-881).

T06 ("The Historical Journey ... Include sources for the historical facts") reached the
approval card with nothing about research on it. The search ran only after approval, in
the render's text lane (`creation_text_composition` -> `run_researched_revision`), so the
creator approved wording they had never seen and a research failure arrived as a failed
render.

Now, for a draft that renders through the unified phone montage and whose brief carries a
typed research requirement (a ``text`` requirement with ``facts.research = {topic,
request_quote}``):

1. The planning step builds the worker's own montage plan for the draft
   (`phone_montage_preflight.preflight_montage_plan`), the model view the render's text
   lane reads (`creation_text_composition.creation_model_snapshot`), and runs the existing
   research step on it unchanged (`researched_text.run_researched_revision`: grounded
   search, footage-bound copy, claim review).
2. ``ready``: the reply lists every sourced explanation, its clip window and its sources
   before the approval card, and the result rides the approved brief binding (its digest
   covers it). The render replays it (`researched_text.pinned_research_operations`) with no
   new search and no rewrite.
3. ``failed`` (no institutional source, review rejected, an explicit ask left unmet): the
   requirement gets a checked ``not_possible`` receipt with the precise reason, so the
   existing KRI-775 hold-back asks "a different approach, or this simpler version?" and no
   approval card goes out.

Sourced text is on-screen text only: ``add_text`` rows with ``research`` provenance, never
caption cues, narration or recorded speech.

Budget: research runs outside every lock. With enough of the turn left it runs inline;
otherwise it runs in its own task (`tasks.prepare_kria_research`) and the turn answers
"checking sources" and re-runs (KRI-709 continuation). Results are cached by the basis they
were researched for, so the re-run picks them up.

Kill switch: ``KRIA_RESEARCH_PREVIEW_ENABLED=false`` + restart (api + worker). Off, nothing
here runs and the render researches after approval exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import structlog

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import say
from app.schemas.research import PreparedResearch
from app.services.researched_text import ResearchRequest

log = structlog.get_logger()

RESEARCH_FACT = "research"
# Inline research needs room for search (55 s) + copy (60 s) + review (45 s) at their
# provider timeouts, less what they usually take; below this the task runs it instead.
INLINE_MIN_S = 100.0
# Kept for the draft commit after research returns.
_COMPLETION_RESERVE_S = 20.0
_CACHE_TTL_S = 3600
_CACHE_PREFIX = "kria:research-preview:v1:"


class ResearchNotFinished(Exception):
    """Research could not finish in this turn's budget (timeout, provider outcome unknown)."""


def research_requirement(
    brief: CreativeBrief | None,
) -> tuple[BriefRequirement, ResearchRequest] | None:
    """The brief's live typed research requirement, validated, or ``None``.

    Only the typed fact counts, never words in a description. A topic that carries a URL
    or a private path fails ``ResearchRequest`` and is dropped (logged).
    """
    if brief is None:
        return None
    for req in brief.live():
        raw = req.facts.get(RESEARCH_FACT) if req.kind == "text" else None
        if not isinstance(raw, Mapping):
            continue
        try:
            return req, ResearchRequest(
                topic=str(raw.get("topic") or ""), request_quote=str(raw.get("request_quote") or "")
            )
        except ValueError:
            log.info("kria_research_requirement_dropped", requirement_id=req.id)
    return None


def basis_digest(
    request: ResearchRequest, strategy: Mapping[str, Any] | None, media_snapshot: Mapping[str, Any]
) -> str:
    """What a prepared result was researched for: the ask, the draft and its media."""
    from app.kria.brief_binding import media_identity  # noqa: PLC0415

    payload = {
        "request": request.model_dump(mode="json"),
        "strategy": dict(strategy or {}),
        "media": media_identity(dict(media_snapshot)),
        "length_plan": dict(media_snapshot).get("length_plan"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ResearchInputs:
    """Everything one research preview reads, serializable for the background task."""

    creator_id: str
    thread_id: str
    item_id: str
    edit_format: str | None
    brief: CreativeBrief
    strategy: dict
    media_snapshot: dict
    creator_request: str

    @property
    def found(self) -> tuple[BriefRequirement, ResearchRequest]:
        found = research_requirement(self.brief)
        if found is None:
            raise ValueError("The brief has no research requirement")
        return found

    @property
    def basis(self) -> str:
        return basis_digest(self.found[1], self.strategy, self.media_snapshot)

    def payload(self) -> dict[str, Any]:
        return {
            "creator_id": self.creator_id,
            "thread_id": self.thread_id,
            "item_id": self.item_id,
            "edit_format": self.edit_format,
            "brief": self.brief.model_dump(mode="json"),
            "strategy": self.strategy,
            "media_snapshot": self.media_snapshot,
            "creator_request": self.creator_request,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> ResearchInputs:
        return cls(
            creator_id=str(raw["creator_id"]),
            thread_id=str(raw["thread_id"]),
            item_id=str(raw["item_id"]),
            edit_format=raw.get("edit_format"),
            brief=CreativeBrief.model_validate(raw["brief"]),
            strategy=dict(raw.get("strategy") or {}),
            media_snapshot=dict(raw.get("media_snapshot") or {}),
            creator_request=str(raw.get("creator_request") or ""),
        )


def _failed(
    request: ResearchRequest, requirement_id: str, basis: str, reason: str
) -> PreparedResearch:
    return PreparedResearch(
        status="failed",
        requirement_id=requirement_id,
        topic=request.topic,
        request_quote=request.request_quote,
        basis_digest=basis,
        reason=reason.strip()[:500] or "The sources could not be checked.",
    )


def _schema_failure(exc: BaseException) -> BaseException | None:
    from app.agents._runtime import SchemaError  # noqa: PLC0415

    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, SchemaError):
            return seen
        seen = seen.__cause__
    return None


def prepare(inputs: ResearchInputs, *, visuals: Any, ctx: Any = None) -> PreparedResearch | None:
    """Research the draft ``inputs`` describe, exactly as the render would.

    ``None`` when the draft does not render through the unified phone montage (the render
    keeps today's path). Raises ``ResearchNotFinished`` when the turn ran out of time;
    every other limit is a ``failed`` result with the precise reason.
    """
    from app.agents._runtime import AgentError  # noqa: PLC0415
    from app.pipeline.guided_story import compile_proposal_execution_plan  # noqa: PLC0415
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan  # noqa: PLC0415
    from app.services.creation_text_composition import (  # noqa: PLC0415
        _lanes,
        creation_model_snapshot,
        snapshot_copy_constraints,
    )
    from app.services.phone_montage_preflight import preflight_montage_plan  # noqa: PLC0415
    from app.services.researched_text import (  # noqa: PLC0415
        ResearchUnavailable,
        prepared_research_texts,
        run_researched_revision,
    )

    requirement, request = inputs.found
    basis = inputs.basis
    try:
        planned = preflight_montage_plan(
            creator_id=inputs.creator_id,
            edit_format=inputs.edit_format,
            brief=inputs.brief,
            strategy=inputs.strategy,
            media_snapshot=inputs.media_snapshot,
            visuals=visuals,
        )
    except UnsupportedPhonePlan:
        return None  # the planner declines; the KRI-775 check and the worker say why
    if planned is None:
        return None
    plan, _aliases = planned
    snapshot = plan.snapshot
    execution = compile_proposal_execution_plan(snapshot)
    texts, slots = _lanes(execution)
    model_snapshot = creation_model_snapshot(
        snapshot,
        texts=texts,
        slots=slots,
        constraints=snapshot_copy_constraints(snapshot, execution),
        text_requirements=inputs.brief.live(),
    )
    try:
        output = run_researched_revision(
            request, utterance=inputs.creator_request, snapshot=model_snapshot, ctx=ctx
        )
    except ResearchUnavailable as exc:
        return _failed(request, requirement.id, basis, str(exc))
    except AgentError as exc:
        if _schema_failure(exc) is not None:
            return _failed(
                request,
                requirement.id,
                basis,
                "The source search returned no usable cited facts from institutional sources.",
            )
        raise ResearchNotFinished(str(exc)) from exc
    unmet = [str(row.get("reason") or "") for row in output.unmet_requests if row.get("reason")]
    if unmet:
        return _failed(request, requirement.id, basis, "; ".join(unmet))
    texts_out = prepared_research_texts(output.ops)
    if not texts_out:
        return _failed(
            request,
            requirement.id,
            basis,
            "No sourced explanation could be matched to the footage.",
        )
    first = next(op["research"] for op in output.ops if isinstance(op.get("research"), dict))
    # `run_researched_revision` prefixes its own note; only the copy writer's limits remain.
    limitations = output.reply_notes.removeprefix("Sources are attached to the explanations.")
    return PreparedResearch(
        status="ready",
        requirement_id=requirement.id,
        topic=request.topic,
        request_quote=request.request_quote,
        basis_digest=basis,
        texts=texts_out,
        retrieved_at=str(first.get("retrieved_at") or ""),
        search_entry_html=str(first.get("search_entry_html") or ""),
        limitations=limitations.strip()[:1000],
    )


# -- cache (best effort: a miss only means researching again) ------------------------------


def _cache_key(thread_id: str, basis: str) -> str:
    return f"{_CACHE_PREFIX}{thread_id}:{basis}"


def _redis():  # noqa: ANN202
    import redis  # noqa: PLC0415

    return redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)


def cache_get(thread_id: str, basis: str) -> PreparedResearch | None:
    try:
        raw = _redis().get(_cache_key(thread_id, basis))
    except Exception:  # noqa: BLE001 - the cache is an optimization, never a dependency
        log.warning("kria_research_cache_read_failed", exc_info=True)
        return None
    if not raw:
        return None
    try:
        prepared = PreparedResearch.model_validate_json(raw)
    except ValueError:
        return None
    return prepared if prepared.basis_digest == basis else None


def cache_put(thread_id: str, prepared: PreparedResearch) -> None:
    try:
        _redis().set(
            _cache_key(thread_id, prepared.basis_digest),
            prepared.model_dump_json(),
            ex=_CACHE_TTL_S,
        )
    except Exception:  # noqa: BLE001
        log.warning("kria_research_cache_write_failed", exc_info=True)


# -- creator-facing copy ---------------------------------------------------------------------


def _clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or url).lower()
    return host.removeprefix("www.")


def _source_label(source: Any) -> str:
    # Grounding redirect links carry the source's domain in the provider-owned title.
    host = _domain(source.url)
    if host == "vertexaisearch.cloud.google.com":
        return source.title
    return host


def preview_lines(prepared: PreparedResearch) -> str:
    """The sourced wording and its sources, for the reply before the approval card."""
    lines = [
        say(
            en="Sourced on-screen explanations (shown only if you approve this draft):",
            tr="Kaynaklı ekran açıklamaları (yalnızca bu taslağı onaylarsan eklenir):",
        )
    ]
    for row in prepared.texts:
        sources = ", ".join(dict.fromkeys(_source_label(source) for source in row.sources))
        lines.append(
            f"- {_clock(row.start_s)}–{_clock(row.end_s)} · “{row.text}” · "
            + say(en="Source", tr="Kaynak")
            + f": {sources}"
        )
    if prepared.limitations:
        lines.append(say(en="Note: ", tr="Not: ") + prepared.limitations)
    return "\n".join(lines)


def preview_payload(prepared: PreparedResearch) -> dict[str, Any]:
    """Structured preview for clients that draw a sources card."""
    return {
        "version": 1,
        "retrieved_at": prepared.retrieved_at,
        "explanations": [
            {
                "text": row.text,
                "start_s": row.start_s,
                "end_s": row.end_s,
                "sources": [source.model_dump(mode="json") for source in row.sources],
            }
            for row in prepared.texts
        ],
    }


def blocker_receipt(prepared: PreparedResearch, requirement: BriefRequirement) -> Any:
    """A checked ``not_possible`` receipt naming the research limit (KRI-775 hold-back)."""
    from app.kria.contracts import RequirementReceipt  # noqa: PLC0415

    return RequirementReceipt(
        requirement_id=requirement.id,
        status="not_possible",
        verification="checked",
        # The research step ran and its own checks decided (no deterministic checker owns
        # a free-text ask), so the reply lists the reason (`brief_checks.is_judged`).
        stage="applied",
        reason=say(
            en=f"Sources could not be confirmed before approval: {prepared.reason}",
            tr=f"Kaynaklar onaydan önce doğrulanamadı: {prepared.reason}",
        )[:500],
    )


def pending_reply() -> str:
    return say(
        en=(
            "I'm checking sources for the historical facts so you can see the exact sourced "
            "wording before you approve. If this takes too long, send your message again, "
            "or ask for a version without sources."
        ),
        tr=(
            "Onaylamadan önce kaynaklı metni görebilmen için tarihi bilgilerin kaynaklarını "
            "kontrol ediyorum. Uzun sürerse mesajını tekrar gönder ya da kaynaksız bir sürüm iste."
        ),
    )


# -- the planning step -----------------------------------------------------------------------


@dataclass(frozen=True)
class DraftResearch:
    """What the draft turn does with a prepared result."""

    prepared: PreparedResearch
    requirement: BriefRequirement

    @property
    def ready(self) -> bool:
        return self.prepared.status == "ready"


def for_draft(
    prepared: Mapping[str, Any] | None,
    *,
    brief: CreativeBrief | None,
    strategy: Mapping[str, Any] | None,
    media_snapshot: Mapping[str, Any],
) -> DraftResearch | None:
    """The planning step's result for the brief the draft turn actually persisted.

    A result researched for anything else (the brief or draft changed in between) is
    reported as a precise limit, never pinned to the wrong clips.
    """
    if prepared is None:
        return None
    found = research_requirement(brief)
    if found is None:
        return None
    requirement, request = found
    result = PreparedResearch.model_validate(prepared)
    if result.basis_digest != basis_digest(request, strategy, media_snapshot):
        result = _failed(
            request,
            requirement.id,
            basis_digest(request, strategy, media_snapshot),
            "The draft changed while its sources were checked.",
        )
    return DraftResearch(
        prepared=result.model_copy(update={"requirement_id": requirement.id}),
        requirement=requirement,
    )


def load_inputs_sync(planned: Any, snapshot: Mapping[str, Any], *, turn_id: uuid.UUID):  # noqa: ANN201
    """Read (no lock, no write) what research needs for this planned strategy draft.

    Returns ``(ResearchInputs, visuals)`` or ``None`` when the draft carries no research
    requirement or the writer is not brief-bound (research rides the approved binding).
    """
    from app.database import sync_session  # noqa: PLC0415
    from app.kria.brief import (  # noqa: PLC0415
        _creator_turn_text_sync,
        apply_updates,
        load_latest_brief_sync,
    )
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
    from app.kria.registry import KRIA_TOOLS  # noqa: PLC0415
    from app.models import CreationThreadEvent, CreatorAgentTurn, PlanItem  # noqa: PLC0415
    from app.services.choice_questions import answered_brief  # noqa: PLC0415
    from app.services.phone_montage_preflight import _ready_visuals  # noqa: PLC0415
    from app.tasks.kria_runtime import _full_creator_request_sync  # noqa: PLC0415

    creator_id = snapshot.get("creator_id")
    if (
        not settings.brief_binding_for(creator_id)
        or planned.brief_route is None
        # KRI-721 readiness drafts are judged by their own assessment; they keep the
        # render-time research path (see docs/pipelines/kria-agent-runtime.md).
        or settings.approval_readiness_for(creator_id)
    ):
        return None
    intent = planned.plan.intents[0]
    arguments = KRIA_TOOLS.get(intent.tool_name, intent.tool_version).arguments_model
    # Exactly the strategy the draft turn stores (`_complete_draft_turn`).
    strategy = arguments.model_validate(intent.arguments).strategy.model_dump(
        mode="json", exclude_none=True
    )
    thread_id = uuid.UUID(str(snapshot["thread_id"]))
    with sync_session() as db:
        brief = apply_updates(
            load_latest_brief_sync(db, thread_id),
            planned.brief_updates,
            source_turn_id=str(turn_id),
            creator_text=_creator_turn_text_sync(db, thread_id, turn_id),
        )
        if settings.kria_choice_questions_enabled:
            brief = answered_brief(brief, strategy)
        if research_requirement(brief) is None:
            return None
        item = db.get(PlanItem, uuid.UUID(str(snapshot["item_id"])))
        turn = db.get(CreatorAgentTurn, turn_id)
        source = db.get(CreationThreadEvent, turn.source_event_id) if turn is not None else None
        if item is None or source is None:
            return None
        request = _full_creator_request_sync(
            db, thread_id=thread_id, through_sequence=int(source.sequence)
        )
        media = (
            dict(planned.media_snapshot)
            if planned.media_snapshot is not None
            else snapshot_media(item)
        )
        visuals = _ready_visuals(db, item=item, user_id=_uuid(creator_id), strategy=strategy)
        inputs = ResearchInputs(
            creator_id=str(creator_id),
            thread_id=str(thread_id),
            item_id=str(item.id),
            edit_format=strategy.get("edit_format") or item.edit_format,
            brief=brief,
            strategy=strategy,
            media_snapshot=media,
            creator_request=request or "",
        )
    return inputs, visuals


def _uuid(value: object) -> object:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return value


def visuals_for_task(inputs: ResearchInputs) -> list:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import PlanItem  # noqa: PLC0415
    from app.services.phone_montage_preflight import _ready_visuals  # noqa: PLC0415

    with sync_session() as db:
        item = db.get(PlanItem, uuid.UUID(inputs.item_id))
        if item is None:
            return []
        return _ready_visuals(
            db,
            item=item,
            user_id=_uuid(inputs.creator_id),
            strategy=inputs.strategy,
        )


def run_task_body(payload: Mapping[str, Any]) -> str:
    """`tasks.prepare_kria_research`: research one draft with a full budget and cache it."""
    from app.agents._runtime import RunContext  # noqa: PLC0415

    inputs = ResearchInputs.from_payload(payload)
    basis = inputs.basis
    if cache_get(inputs.thread_id, basis) is not None:
        return "cached"
    ctx = RunContext(
        creator_id=inputs.creator_id,
        plan_item_id=inputs.item_id,
        request_id=f"research-preview:{basis[:24]}",
        usage_purpose="kria_research_preview",
    )
    try:
        prepared = prepare(inputs, visuals=visuals_for_task(inputs), ctx=ctx)
    except ResearchNotFinished as exc:
        requirement, request = inputs.found
        prepared = _failed(
            request, requirement.id, basis, f"Checking the sources timed out ({exc})"[:500]
        )
    if prepared is None:
        return "not_applicable"
    cache_put(inputs.thread_id, prepared)
    log.info("kria_research_preview_prepared", status=prepared.status, basis=basis[:12])
    return prepared.status


def enqueue(inputs: ResearchInputs) -> None:
    from app.tasks.kria_runtime import prepare_kria_research  # noqa: PLC0415

    try:
        prepare_kria_research.apply_async(
            args=[inputs.payload()],
            task_id=f"kria-research-{inputs.basis[:40]}",
            queue="agent-control",
        )
    except Exception:  # noqa: BLE001 - the next turn run enqueues again
        log.warning("kria_research_enqueue_failed", exc_info=True)


def with_research(
    planned: Any, snapshot: Mapping[str, Any], *, turn_id: uuid.UUID, deadline: float
):  # noqa: ANN201
    """Attach research to a planned strategy draft, or turn it into "checking sources".

    Fails open on an unexpected error (logged): the draft goes on and the render keeps
    today's research path.
    """
    if not settings.kria_research_preview_enabled or planned.plan.mode != "act":
        return planned
    intents = planned.plan.intents
    if len(intents) != 2 or intents[0].tool_name != "draft.apply_strategy":
        return planned
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.kria.contracts import KriaTurnPlan  # noqa: PLC0415

    try:
        loaded = load_inputs_sync(planned, snapshot, turn_id=turn_id)
    except Exception:  # noqa: BLE001
        log.exception("kria_research_preview_inputs_failed", turn_id=str(turn_id))
        return planned
    if loaded is None:
        return planned
    inputs, visuals = loaded
    basis = inputs.basis
    prepared = cache_get(inputs.thread_id, basis)
    if prepared is None:
        remaining = deadline - time.monotonic()
        try:
            if remaining < INLINE_MIN_S:
                raise ResearchNotFinished("not enough of the turn left")
            prepared = prepare(
                inputs,
                visuals=visuals,
                ctx=RunContext(
                    creator_id=inputs.creator_id,
                    plan_item_id=inputs.item_id,
                    request_id=f"research-preview:{basis[:24]}",
                    usage_purpose="kria_research_preview",
                    deadline_monotonic=deadline - _COMPLETION_RESERVE_S,
                ),
            )
        except ResearchNotFinished as exc:
            log.info("kria_research_preview_deferred", turn_id=str(turn_id), reason=str(exc))
            enqueue(inputs)
            return replace(
                planned,
                plan=KriaTurnPlan(mode="respond", turn_value="recovery", response=pending_reply()),
                continuable=True,
            )
        except Exception:  # noqa: BLE001
            log.exception("kria_research_preview_failed", turn_id=str(turn_id))
            return planned
        if prepared is None:
            return planned
        cache_put(inputs.thread_id, prepared)
    log.info(
        "kria_research_preview",
        turn_id=str(turn_id),
        status=prepared.status,
        explanations=len(prepared.texts),
    )
    return replace(planned, prepared_research=prepared.model_dump(mode="json"))
