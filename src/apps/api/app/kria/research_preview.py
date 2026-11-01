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

Budget: research runs outside every lock, under the turn's lease heartbeat. With enough
of the turn left it runs inline; otherwise it runs in its own task
(`tasks.prepare_kria_research`) and the turn re-runs (KRI-709 continuation). The deferring
run anchors its drafted strategy to the turn, so the re-run drafts exactly what is being
researched (a re-plan's wording never matches) and waits for the task instead of paying
for the same research twice. Results are cached by everything they were researched for
(the ask, the full creator request, the live requirements, the reply language, the draft,
its media, the ready Visuals and the slot windows the clip analysis planned). Only finished
results are cached, first one wins: a timeout is never cached, so the next run researches
again.

Kill switch: ``KRIA_RESEARCH_PREVIEW_ENABLED=false`` + restart (api + worker). Off, nothing
here runs and the render researches after approval exactly as before.
"""

from __future__ import annotations

import dataclasses
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
_CACHE_PREFIX = "kria:research-preview:v2:"
# The background task's own hard limit plus slack: an in-flight marker never outlives it.
_INFLIGHT_TTL_S = 210
# A continued turn runs within minutes of the run that deferred it.
_ANCHOR_TTL_S = 1800
_WAIT_POLL_S = 2.0


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


def _requirement_terms(brief: CreativeBrief | None) -> list[dict[str, Any]]:
    """The live requirements' content (the research copy writer reads every one)."""
    if brief is None:
        return []
    return [
        req.model_dump(mode="json", include={"kind", "scope", "literal", "description", "facts"})
        for req in brief.live()
    ]


def basis_digest(
    request: ResearchRequest,
    strategy: Mapping[str, Any] | None,
    media_snapshot: Mapping[str, Any],
    *,
    creator_request: str,
    brief: CreativeBrief | None,
    language: str | None,
    analysis: Mapping[str, Any] | None = None,
) -> str:
    """What a prepared result was researched for.

    The ask, the full creator request (the copy writer's instruction), every live
    requirement (a later "keep it under 6 words" changes the wording), the reply language,
    the draft, its media, and ``analysis`` (`analysis_basis`): the ready Visuals the plan
    places and the slot windows the clip analysis planned, which change without the draft
    or its media changing. Anything else changing means researching again.
    """
    from app.kria.brief_binding import media_identity  # noqa: PLC0415

    payload = {
        "request": request.model_dump(mode="json"),
        "creator_request": creator_request,
        "requirements": _requirement_terms(brief),
        "language": language,
        "strategy": dict(strategy or {}),
        "media": media_identity(dict(media_snapshot)),
        "length_plan": dict(media_snapshot).get("length_plan"),
        "analysis": dict(analysis) if analysis is not None else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _visual_identity(visual: Any) -> str:
    """One ready Visual as the plan reads it (id, generation, length, analysis, context)."""
    if dataclasses.is_dataclass(visual) and not isinstance(visual, type):
        raw: Any = dataclasses.asdict(visual)
    else:
        raw = getattr(visual, "__dict__", visual)
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def analysis_basis(
    *,
    creator_id: object,
    edit_format: object,
    brief: CreativeBrief | None,
    strategy: Mapping[str, Any] | None,
    media_snapshot: Mapping[str, Any],
    visuals: Any,
    client_protocol: int | None = None,
) -> dict[str, Any]:
    """The research inputs the request and the draft do not name.

    The ready Visuals the plan places and the slot windows the worker's own montage plan
    (`phone_montage_preflight.preflight_montage_plan`) cuts from the clip analysis: the
    windows research binds each explanation to. A Visual added or re-analysed, or clip
    analysis that moves a window, changes this, so a cached result researched for the
    old windows is never pinned (the render would refuse it after approval). ``slots`` is
    ``None`` when the draft does not render through the unified phone montage.
    """
    from app.pipeline.guided_story import compile_proposal_execution_plan  # noqa: PLC0415
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan  # noqa: PLC0415
    from app.services.creation_text_composition import _lanes  # noqa: PLC0415
    from app.services.phone_montage_preflight import (  # noqa: PLC0415
        MontagePreflightSkip,
        preflight_montage_plan,
    )

    rows = list(visuals() if callable(visuals) else visuals or [])
    try:
        planned = preflight_montage_plan(
            creator_id=creator_id,
            edit_format=edit_format,
            brief=brief,
            strategy=strategy,
            media_snapshot=media_snapshot,
            visuals=rows,
            client_protocol=client_protocol,
        )
    except UnsupportedPhonePlan:
        planned = None
    slots = None
    if planned is not None and planned is not MontagePreflightSkip.OTHER_WRITER:
        _texts, slots = _lanes(compile_proposal_execution_plan(planned[0].snapshot))
    return {"visuals": [_visual_identity(row) for row in rows], "slots": slots}


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
    language: str | None = None
    # KRI-898: the app build that sent the message (the plan holds a freeze only for it).
    client_protocol: int | None = None
    # `analysis_basis` for the Visuals this research reads; part of the basis.
    analysis: dict | None = None

    @property
    def found(self) -> tuple[BriefRequirement, ResearchRequest]:
        found = research_requirement(self.brief)
        if found is None:
            raise ValueError("The brief has no research requirement")
        return found

    @property
    def basis(self) -> str:
        return basis_digest(
            self.found[1],
            self.strategy,
            self.media_snapshot,
            creator_request=self.creator_request,
            brief=self.brief,
            language=self.language,
            analysis=self.analysis,
        )

    def with_visuals(self, visuals: Any) -> ResearchInputs:
        """These inputs with ``analysis`` read from ``visuals`` (the ones research reads)."""
        return replace(
            self,
            analysis=analysis_basis(
                creator_id=self.creator_id,
                edit_format=self.edit_format,
                brief=self.brief,
                strategy=self.strategy,
                media_snapshot=self.media_snapshot,
                visuals=visuals,
                client_protocol=self.client_protocol,
            ),
        )

    @property
    def terms(self) -> dict[str, Any]:
        """The basis inputs the draft turn cannot re-derive from what it persists."""
        return {"creator_request": self.creator_request, "language": self.language}

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
            "language": self.language,
            "client_protocol": self.client_protocol,
            "analysis": self.analysis,
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
            language=raw.get("language"),
            client_protocol=raw.get("client_protocol"),
            analysis=raw.get("analysis"),
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


def _handled_elsewhere(brief: CreativeBrief | None) -> tuple[str, ...]:
    """The live asks another step applies to the researched text (not research itself)."""
    from app.kria.animation_ask import animation_targets, requested_text_position  # noqa: PLC0415

    live = brief.live() if brief is not None else []
    handled = []
    if animation_targets(live):
        handled.append("animation")
    if requested_text_position(live) is not None:
        handled.append("text position")
    return tuple(handled)


def prepare(inputs: ResearchInputs, *, visuals: Any, ctx: Any = None) -> PreparedResearch | None:
    """Research the draft ``inputs`` describe, exactly as the render would.

    A selected different writer returns a precise failed result. An unresolved preflight
    returns ``None``. Raises ``ResearchNotFinished`` when the turn ran out of time.
    """
    from app.agents._runtime import AgentError  # noqa: PLC0415
    from app.pipeline.guided_story import compile_proposal_execution_plan  # noqa: PLC0415
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan  # noqa: PLC0415
    from app.services.creation_text_composition import (  # noqa: PLC0415
        _lanes,
        creation_model_snapshot,
        snapshot_copy_constraints,
    )
    from app.services.phone_montage_preflight import (  # noqa: PLC0415
        MontagePreflightSkip,
        preflight_montage_plan,
    )
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
            client_protocol=inputs.client_protocol,
        )
    except UnsupportedPhonePlan:
        return None  # the planner declines; the KRI-775 check and the worker say why
    if planned is None:
        return None
    if planned is MontagePreflightSkip.OTHER_WRITER:
        return _failed(
            request,
            requirement.id,
            basis,
            say(
                en=(
                    "Before approval, this edit format cannot prepare sourced research text. "
                    "I can show you sourced facts in a standard montage instead, or keep "
                    "this format without the facts if you choose."
                ),
                tr=(
                    "Bu kurgu türü onaydan önce kaynaklı araştırma metni hazırlayamıyor. "
                    "İstersen kaynaklı bilgileri standart bir montajda gösterebilirim ya da "
                    "bu türü bilgiler olmadan koruyabilirim."
                ),
            ),
        )
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
            request,
            utterance=inputs.creator_request,
            snapshot=model_snapshot,
            ctx=ctx,
            handled_elsewhere=_handled_elsewhere(inputs.brief),
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
    texts_out = prepared_research_texts(output.ops)
    if unmet and not (texts_out and settings.research_unmet_is_note_enabled):
        return _failed(request, requirement.id, basis, "; ".join(unmet))
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
    if unmet:
        # Sourced wording exists: the part the writer could not source is shown to the creator
        # next to it before they approve, instead of holding the whole draft back.
        limitations = f"{limitations.strip()} Not covered: {'; '.join(unmet)}"
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
    """Keep a finished result. The first one wins: a late duplicate never replaces it.

    Only finished research reaches here (ready, or a deterministic limit); a timeout is
    never cached, so the next run researches again.
    """
    try:
        _redis().set(
            _cache_key(thread_id, prepared.basis_digest),
            prepared.model_dump_json(),
            ex=_CACHE_TTL_S,
            nx=True,
        )
    except Exception:  # noqa: BLE001
        log.warning("kria_research_cache_write_failed", exc_info=True)


def _inflight_key(thread_id: str, basis: str) -> str:
    return f"{_CACHE_PREFIX}inflight:{thread_id}:{basis}"


def claim_inflight(thread_id: str, basis: str) -> bool:
    """Mark the background task for this basis as running; False when one already is."""
    try:
        return bool(_redis().set(_inflight_key(thread_id, basis), "1", ex=_INFLIGHT_TTL_S, nx=True))
    except Exception:  # noqa: BLE001 - without Redis, enqueue (the task id still names it)
        log.warning("kria_research_inflight_claim_failed", exc_info=True)
        return True


def is_inflight(thread_id: str, basis: str) -> bool:
    try:
        return bool(_redis().exists(_inflight_key(thread_id, basis)))
    except Exception:  # noqa: BLE001
        return False


def release_inflight(thread_id: str, basis: str) -> None:
    try:
        _redis().delete(_inflight_key(thread_id, basis))
    except Exception:  # noqa: BLE001
        log.warning("kria_research_inflight_release_failed", exc_info=True)


def _anchor_key(turn_id: uuid.UUID) -> str:
    return f"{_CACHE_PREFIX}anchor:{turn_id}"


def anchor_get(turn_id: uuid.UUID) -> dict[str, Any] | None:
    """The strategy draft a deferring run of this turn sent to research, if any."""
    try:
        raw = _redis().get(_anchor_key(turn_id))
    except Exception:  # noqa: BLE001
        log.warning("kria_research_anchor_read_failed", exc_info=True)
        return None
    if not raw:
        return None
    try:
        anchor = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(anchor, dict) or not isinstance(anchor.get("arguments"), dict):
        return None
    return anchor


def anchor_put(turn_id: uuid.UUID, *, arguments: Mapping[str, Any], basis: str) -> None:
    try:
        _redis().set(
            _anchor_key(turn_id),
            json.dumps({"arguments": dict(arguments), "basis": basis}, default=str),
            ex=_ANCHOR_TTL_S,
        )
    except Exception:  # noqa: BLE001
        log.warning("kria_research_anchor_write_failed", exc_info=True)


def anchor_clear(turn_id: uuid.UUID) -> None:
    try:
        _redis().delete(_anchor_key(turn_id))
    except Exception:  # noqa: BLE001
        log.warning("kria_research_anchor_clear_failed", exc_info=True)


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
            "I'm checking sources for the facts you asked me to source, so you can see the "
            "exact sourced wording before you approve. If this takes too long, send your "
            "message again, or ask for a version without sources."
        ),
        tr=(
            "Onaylamadan önce kaynaklı metni görebilmen için kaynak istediğin bilgileri "
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
    terms: Mapping[str, Any] | None,
    creator_id: object,
    edit_format: object,
    visuals: Any,
    client_protocol: int | None = None,
) -> DraftResearch | None:
    """The planning step's result for the brief the draft turn actually persisted.

    A result researched for anything else (the brief, the draft, its ready Visuals or the
    slot windows the clip analysis plans changed in between) is reported as a precise
    limit, never pinned to the wrong clips. ``terms`` carries the basis inputs the
    planning step read and the persisted draft does not hold (the full creator request,
    the reply language). ``visuals`` are the item's ready Visuals now, read the way the
    planning step read them, so the basis is recomputed from this commit's own inputs.
    """
    if prepared is None:
        return None
    found = research_requirement(brief)
    if found is None:
        return None
    requirement, request = found
    result = PreparedResearch.model_validate(prepared)
    expected = basis_digest(
        request,
        strategy,
        media_snapshot,
        creator_request=str((terms or {}).get("creator_request") or ""),
        brief=brief,
        language=(terms or {}).get("language"),
        analysis=analysis_basis(
            creator_id=creator_id,
            edit_format=edit_format,
            brief=brief,
            strategy=strategy,
            media_snapshot=media_snapshot,
            visuals=visuals,
            client_protocol=client_protocol,
        ),
    )
    if result.basis_digest != expected:
        result = _failed(
            request,
            requirement.id,
            expected,
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
    from app.kria.reply_language import current_reply_language  # noqa: PLC0415
    from app.models import CreationThreadEvent, CreatorAgentTurn, PlanItem  # noqa: PLC0415
    from app.services.choice_questions import answered_brief  # noqa: PLC0415
    from app.services.phone_montage_preflight import _ready_visuals  # noqa: PLC0415
    from app.services.phone_rollout import stamped_client_protocol  # noqa: PLC0415
    from app.tasks.kria_runtime import _full_creator_request_sync  # noqa: PLC0415

    creator_id = snapshot.get("creator_id")
    if not settings.brief_binding_for(creator_id) or planned.brief_route is None:
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
            language=current_reply_language(),
            client_protocol=stamped_client_protocol(snapshot.get("client_protocol")),
        ).with_visuals(visuals)
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
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415

    queued = ResearchInputs.from_payload(payload)
    try:
        # Research the Visuals as they are now, and cache under the basis they make: a
        # Visual that changed since the turn queued this is a new basis, which the turn's
        # next run computes the same way.
        visuals = visuals_for_task(queued)
        inputs = queued.with_visuals(visuals)
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
            with reply_language_for(inputs.language):
                prepared = prepare(inputs, visuals=visuals, ctx=ctx)
        except ResearchNotFinished as exc:
            # Transient (provider slow or outcome unknown): never cached, so the waiting
            # turn run (or the creator's resend) researches again.
            log.info("kria_research_preview_timed_out", basis=basis[:12], reason=str(exc)[:200])
            return "timed_out"
        if prepared is None:
            return "not_applicable"
        cache_put(inputs.thread_id, prepared)
        log.info("kria_research_preview_prepared", status=prepared.status, basis=basis[:12])
        return prepared.status
    finally:
        release_inflight(queued.thread_id, queued.basis)


def enqueue(inputs: ResearchInputs) -> None:
    """Start the background research for this basis unless one is already running."""
    from app.tasks.kria_runtime import prepare_kria_research  # noqa: PLC0415

    basis = inputs.basis
    if not claim_inflight(inputs.thread_id, basis):
        log.info("kria_research_preview_already_running", basis=basis[:12])
        return
    try:
        prepare_kria_research.apply_async(
            args=[inputs.payload()],
            task_id=f"kria-research-{basis[:40]}-{uuid.uuid4().hex[:8]}",
            queue="agent-control",
        )
    except Exception:  # noqa: BLE001 - the next turn run enqueues again
        release_inflight(inputs.thread_id, basis)
        log.warning("kria_research_enqueue_failed", exc_info=True)


def _with_arguments(planned: Any, arguments: Mapping[str, Any]) -> Any:
    """``planned`` drafting the anchored strategy (and its summary) instead of its own."""
    intents = planned.plan.intents
    plan = planned.plan.model_copy(
        update={
            "intents": [
                intents[0].model_copy(update={"arguments": dict(arguments)}),
                *intents[1:],
            ]
        }
    )
    return replace(planned, plan=plan)


def _wait_for_task(thread_id: str, basis: str, *, until: float) -> PreparedResearch | None:
    """Wait (the turn's lease heartbeat keeps running) for the background task's result."""
    while True:
        prepared = cache_get(thread_id, basis)
        if prepared is not None or not is_inflight(thread_id, basis):
            return prepared
        if time.monotonic() + _WAIT_POLL_S > until:
            return None
        time.sleep(_WAIT_POLL_S)


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
        # Only a draft with a research requirement reaches Redis (or reads anything more).
        loaded = load_inputs_sync(planned, snapshot, turn_id=turn_id)
        anchor = anchor_get(turn_id) if loaded is not None else None
        if anchor is not None:
            # A previous run of this turn sent its draft to research. Re-planning never
            # writes the same wording, so draft exactly what is being researched, as long
            # as everything else it was researched for still holds.
            anchored = _with_arguments(planned, anchor["arguments"])
            reloaded = load_inputs_sync(anchored, snapshot, turn_id=turn_id)
            if reloaded is not None and reloaded[0].basis == anchor.get("basis"):
                planned, loaded = anchored, reloaded
            else:
                log.info("kria_research_preview_anchor_stale", turn_id=str(turn_id))
    except Exception:  # noqa: BLE001
        log.exception("kria_research_preview_inputs_failed", turn_id=str(turn_id))
        return planned
    if loaded is None:
        return planned
    inputs, visuals = loaded
    basis = inputs.basis
    prepared = cache_get(inputs.thread_id, basis)
    if prepared is None and is_inflight(inputs.thread_id, basis):
        # The background task is researching this exact draft: wait for it rather than
        # paying for the same search, copy and review again.
        prepared = _wait_for_task(inputs.thread_id, basis, until=deadline - _COMPLETION_RESERVE_S)
    if prepared is None:
        remaining = deadline - time.monotonic()
        try:
            if is_inflight(inputs.thread_id, basis):
                raise ResearchNotFinished("the background research is still running")
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
            anchor_put(turn_id, arguments=planned.plan.intents[0].arguments, basis=basis)
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
    anchor_clear(turn_id)
    log.info(
        "kria_research_preview",
        turn_id=str(turn_id),
        status=prepared.status,
        explanations=len(prepared.texts),
    )
    return replace(
        planned,
        prepared_research=prepared.model_dump(mode="json"),
        prepared_research_terms=inputs.terms,
    )
