"""Bounded, prompt-scoped confirmation of existing source intervals for a chat draft."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import select

from app import storage
from app.agents.source_moment_location import SourceMomentLocationAgent, SourceMomentLocationOutput
from app.models import ContentPlan, MediaAsset, MediaAssetSource, MediaSourceMoment, PlanItem
from app.pipeline.clip_cache import MediaAssetFactKey, complete_media_asset_fact
from app.services import media_library_analysis as facts
from app.services import media_moments
from app.services.media_collection_analysis import (
    claim_collection_analysis,
    collection_run_context,
    get_collection_analysis,
)
from app.services.media_moment_inspection import MAX_SOURCE_BYTES, inspect_interval
from app.services.named_moments import (
    Grounding,
    InspectedGroundings,
    Interval,
    MomentSet,
    fold_moment_orders,
    ground_all,
)

log = structlog.get_logger()

MAX_INSPECTIONS = 3
DEADLINE_S = 45
SCHEMA = "prompt-moment-location-v1"
_HASH = re.compile(r"[0-9a-f]{64}")


def inspection_question(words: str) -> str:
    return f'Locate this action within the sample: "{words}".'


def inspection_key(owner, source, moment, words: str, context: str) -> MediaAssetFactKey:
    spec = SourceMomentLocationAgent.spec
    identity = hashlib.sha256(
        json.dumps(
            [
                str(source.id),
                source.object_generation,
                source.uploaded_sha256,
                str(moment.id),
                moment.fact_version,
                moment.start_s,
                moment.end_s,
                inspection_question(words),
                context,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return MediaAssetFactKey(
        str(owner), identity, spec.name, spec.model, spec.prompt_version, SCHEMA
    )


def located_windows(result: Any, moment, max_window_s: float):
    """None means inconclusive; an empty list means the model found no action."""
    if not isinstance(result, dict) or not isinstance(result.get("sample_sha256"), str):
        return None
    if _HASH.fullmatch(result["sample_sha256"]) is None:
        return None
    try:
        output = SourceMomentLocationOutput.model_validate(
            {key: value for key, value in result.items() if key != "sample_sha256"}
        )
    except ValueError:
        return None
    if output.status == "uncertain":
        return None
    if any(
        match.confidence < 0.8
        or not match.evidence.strip()
        or match.end_s > moment.end_s - moment.start_s
        or match.end_s - match.start_s > max_window_s
        for match in output.matches
    ):
        return None
    return output.matches


async def _target(db, owner, item_id, named: MomentSet):
    item = await db.scalar(
        select(PlanItem)
        .join(ContentPlan, PlanItem.content_plan_id == ContentPlan.id)
        .where(PlanItem.id == item_id, ContentPlan.user_id == owner)
        .execution_options(populate_existing=True)
    )
    if item is None or not named.source_path or not named.generation:
        return None
    assignments = [
        row
        for row in item.clip_assignments or []
        if isinstance(row, dict) and str(row.get("media_id")) == named.take
    ]
    if len(assignments) != 1:
        return None
    row = assignments[0]
    if (
        row.get("gcs_path") != named.source_path
        or str(row.get("storage_generation") or row.get("generation") or "") != named.generation
        or row.get("kind", "video") != "video"
        or row.get("duration_s") != named.duration_s
    ):
        return None
    pairs = (
        await db.execute(
            select(MediaAsset, MediaAssetSource)
            .join(MediaAssetSource, MediaAssetSource.asset_id == MediaAsset.id)
            .where(
                MediaAsset.user_id == owner,
                MediaAssetSource.user_id == owner,
                MediaAsset.kind == "video",
                MediaAsset.archived_at.is_(None),
                MediaAssetSource.status == "available",
                MediaAssetSource.role.in_(["original", "analysis_proxy"]),
                MediaAssetSource.object_path == named.source_path,
                MediaAssetSource.object_generation == named.generation,
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(pairs) != 1:
        return None
    asset, source = pairs[0]
    if (
        not isinstance(source.uploaded_sha256, str)
        or _HASH.fullmatch(source.uploaded_sha256) is None
        or not 0 < source.byte_count <= MAX_SOURCE_BYTES
        or (row.get("library_asset_id") and str(row["library_asset_id"]) != str(asset.id))
        or (row.get("library_source_id") and str(row["library_source_id"]) != str(source.id))
    ):
        return None
    duration = media_moments._number((asset.metadata_json or {}).get("duration_s"))
    if duration != named.duration_s:
        return None
    kwargs = dict(
        asset_id=str(asset.id),
        owner_id=str(owner),
        source_id=str(source.id),
        required_generation=source.object_generation,
    )
    analysis, transcript = await asyncio.gather(
        asyncio.to_thread(facts.get_media_asset_analysis, **kwargs),
        asyncio.to_thread(facts.get_media_asset_analysis, **kwargs, analyzer="transcript"),
    )
    if analysis is None:
        return None
    spec = media_moments._index_spec("video")
    version = hashlib.sha256(
        json.dumps(
            [spec, analysis or {}, transcript or {}], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    intervals, _ = media_moments.source_intervals(analysis or {}, transcript or {}, duration)
    moments = list(
        (
            await db.scalars(
                select(MediaSourceMoment)
                .where(
                    MediaSourceMoment.user_id == owner,
                    MediaSourceMoment.asset_id == asset.id,
                    MediaSourceMoment.source_id == source.id,
                    MediaSourceMoment.fact_version == version,
                )
                .order_by(MediaSourceMoment.start_s, MediaSourceMoment.id)
                .limit(media_moments.MAX_SOURCE_MOMENTS + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(moments) > media_moments.MAX_SOURCE_MOMENTS:
        return None
    valid = []
    for moment in moments:
        evidence = moment.evidence_json or {}
        interval = dict(
            kind=moment.kind,
            start_s=moment.start_s,
            end_s=moment.end_s,
            text=moment.text,
            uncertain=moment.uncertain,
        )
        if (
            evidence.get("index_spec") != spec
            or evidence.get("generation") != source.object_generation
            or evidence.get("uploaded_sha256") != source.uploaded_sha256
            or evidence.get("fact_version") != version
            or evidence.get("time_basis") != "source_seconds"
            or evidence.get("evidence_scope", "timed_moment") != "timed_moment"
            or evidence.get("sampled_window")
            or not 0 <= moment.start_s < moment.end_s <= duration
            or moment.end_s - moment.start_s > 20
            or interval not in intervals
            or moment.id
            != uuid.uuid5(
                uuid.NAMESPACE_URL, json.dumps([str(source.id), version, interval], sort_keys=True)
            )
        ):
            continue
        valid.append(moment)
    if not valid:
        return None
    metadata = await asyncio.wait_for(
        asyncio.to_thread(storage.object_metadata_once, source.object_path, timeout_s=5), timeout=5
    )
    if str(metadata.generation) != source.object_generation or metadata.size != source.byte_count:
        return None
    return source, valid


def _run_claim(claim, source, moment, question, ctx):
    try:
        result = inspect_interval(
            source, moment, question, ctx=ctx, agent_factory=SourceMomentLocationAgent
        )
        return result if complete_media_asset_fact(claim, result) else None
    except Exception:
        complete_media_asset_fact(claim, None, failed=True)
        raise


async def inspect_named_moments(
    db,
    *,
    creator_id,
    item_id,
    moment_set: MomentSet,
    events,
    allow_provider: bool = False,
    max_window_s: float = 3.0,
    budget_s: float | None = None,
) -> InspectedGroundings:
    answered = fold_moment_orders(events).answers
    grounded = ground_all(moment_set)
    stale = {
        key: Grounding("unlocated")
        for key, value in grounded.items()
        if key not in answered and value.status == "grounded"
    }
    empty = InspectedGroundings(moment_set.digest, stale)
    if not math.isfinite(max_window_s) or max_window_s <= 0:
        return empty
    pending = [
        moment
        for moment in moment_set.moments
        if moment.key not in answered and len(inspection_question(moment.words)) <= 200
    ]
    if not pending:
        return empty
    budget = DEADLINE_S if budget_s is None else min(DEADLINE_S, budget_s)
    if not math.isfinite(budget) or budget <= 0:
        return empty
    deadline = time.monotonic() + budget
    provider_open = allow_provider
    try:
        async with asyncio.timeout(budget):
            target = await _target(db, creator_id, item_id, moment_set)
            if target is None:
                return empty
            source, candidates = target
            original_identity = (
                str(source.id),
                source.object_generation,
                source.uploaded_sha256,
                tuple((str(m.id), m.fact_version, m.start_s, m.end_s) for m in candidates),
            )
            results = dict(stale)
            spent = 0
            for named in pending:
                existing = grounded[named.key].found
                if existing is not None and any(
                    existing.start_s == candidate.start_s
                    and existing.end_s == candidate.end_s
                    and existing.description == candidate.text
                    for candidate in candidates
                ):
                    results.pop(named.key, None)
                    continue
                positives = []
                complete = True
                for moment in candidates:
                    key = inspection_key(creator_id, source, moment, named.words, moment_set.digest)
                    result = await asyncio.to_thread(get_collection_analysis, key)
                    if result is None and provider_open and spent < MAX_INSPECTIONS:
                        spent += 1
                        claim = None
                        try:
                            claim = await asyncio.to_thread(
                                claim_collection_analysis,
                                key,
                                canonical_asset_id=str(source.asset_id),
                                canonical_source_id=str(source.id),
                                lease_seconds=90,
                            )
                        except HTTPException as exc:
                            if exc.status_code != 409:
                                raise
                            provider_open = False
                        status = claim.status if claim is not None else None
                        if status == "ready":
                            result = claim.result_json
                        elif status == "claimed":
                            ctx = collection_run_context(
                                creator_id=str(creator_id),
                                request_id=f"prompt-moment:{key.source_identity}",
                                deadline_monotonic=deadline,
                                timeout_override_s=12,
                                estimated_max_cost_usd=0.05,
                            )
                            result = await asyncio.to_thread(
                                _run_claim,
                                claim,
                                source,
                                moment,
                                inspection_question(named.words),
                                ctx,
                            )
                    if result is None:
                        complete = False
                        continue
                    matches = located_windows(result, moment, max_window_s)
                    if matches is None:
                        complete = False
                        continue
                    for match in matches:
                        positives.append(
                            Interval(
                                moment.start_s + match.start_s,
                                moment.start_s + match.end_s,
                                named.words,
                                "targeted_model_inspection",
                                evidence={
                                    "source_id": str(source.id),
                                    "generation": source.object_generation,
                                    "uploaded_sha256": source.uploaded_sha256,
                                    "fact_version": moment.fact_version,
                                    "moment_id": str(moment.id),
                                    "sample_start_s": moment.start_s,
                                    "sample_end_s": moment.end_s,
                                    "question": inspection_question(named.words),
                                    "context_digest": moment_set.digest,
                                    "cache_key": key.source_identity,
                                    "sample_sha256": result["sample_sha256"],
                                    "answer_evidence": match.evidence,
                                    "verification": "targeted_model_inspection",
                                    "uncertain": True,
                                },
                            )
                        )
                positives = list({(p.start_s, p.end_s): p for p in positives}.values())
                if len(positives) > 1:
                    results[named.key] = Grounding("ambiguous", suggestions=tuple(positives))
                elif len(positives) == 1 and complete:
                    results[named.key] = Grounding("grounded", found=positives[0])
            if not results:
                return InspectedGroundings(moment_set.digest, {})
            refreshed = await _target(db, creator_id, item_id, moment_set)
            if refreshed is None:
                return empty
            current_source, current_moments = refreshed
            current_identity = (
                str(current_source.id),
                current_source.object_generation,
                current_source.uploaded_sha256,
                tuple((str(m.id), m.fact_version, m.start_s, m.end_s) for m in current_moments),
            )
            return (
                InspectedGroundings(moment_set.digest, results)
                if current_identity == original_identity
                else empty
            )
    except Exception as exc:
        log.warning("prompt_moment_inspection_unavailable", error_type=type(exc).__name__)
        return empty
