"""Explicit bounded semantic retrieval using the shared agent, ledger and fact leases."""

import asyncio
import hashlib
import json
import time

from fastapi import HTTPException

from app.agents._model_client import default_client
from app.agents.media_moment_relevance import (
    MomentCandidate,
    MomentRelevanceAgent,
    MomentRelevanceInput,
    MomentRelevanceOutput,
)
from app.pipeline.clip_cache import (
    MediaAssetFactKey,
    complete_media_asset_fact,
)
from app.services import media_moments
from app.services.media_collection_analysis import claim_collection_analysis, collection_run_context


async def semantic_search(db, owner, identifier, *, revision: int, query: str, cache_only=False):
    # One hundred current intervals is the declared v1 evidence budget. If the collection
    # exceeds it the response explicitly remains partial; no false no-match/recall claim.
    baseline = await media_moments.search_moments(
        db, owner, identifier, revision=revision, query=query, limit=100, all_candidates=True
    )
    if not baseline.moments:
        return baseline.model_copy(update={"method": "semantic-rerank-v1"})
    input = MomentRelevanceInput(
        query=query,
        candidates=[
            MomentCandidate(
                id=str(value.id),
                source=str(value.source_id),
                kind=value.kind,
                evidence_scope=value.evidence_scope,
                text=value.text[:800],
                quality_notes=json.dumps(value.evidence.get("quality") or {}, ensure_ascii=False)[
                    :600
                ],
                start_s=value.start_s,
                end_s=value.end_s,
            )
            for value in baseline.moments
        ],
    )
    spec = MomentRelevanceAgent.spec
    # The full input includes moment IDs and ordering; provenance includes immutable bytes,
    # source facts and index versions. No cache can cross owner or candidate revisions.
    digest = hashlib.sha256(
        json.dumps(
            [
                input.model_dump(),
                [
                    {k: v for k, v in value.evidence.items() if k != "source_checked_at"}
                    for value in baseline.moments
                ],
                revision,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    first = baseline.moments[0]
    key = MediaAssetFactKey(
        str(owner), digest, spec.name, spec.model, spec.prompt_version, "semantic-rerank-v1"
    )
    claim = await asyncio.to_thread(
        claim_collection_analysis,
        key,
        cache_only=cache_only,
        canonical_asset_id=str(first.asset_id),
        canonical_source_id=str(first.source_id),
        lease_seconds=90,
    )
    if claim.status == "ready":
        output = MomentRelevanceOutput.model_validate(claim.result_json)
    elif claim.status == "claimed":
        try:
            output = await asyncio.to_thread(
                MomentRelevanceAgent(default_client()).run,
                input,
                ctx=collection_run_context(
                    creator_id=str(owner),
                    request_id=f"moment-search:{digest}",
                    deadline_monotonic=time.monotonic() + 20,
                    estimated_max_cost_usd=0.05,
                ),
            )
            if not await asyncio.to_thread(complete_media_asset_fact, claim, output.model_dump()):
                raise HTTPException(409, "Search result changed; retry")
        except Exception:
            await asyncio.to_thread(complete_media_asset_fact, claim, None, failed=True)
            raise
    else:
        raise HTTPException(
            409 if claim.status == "busy" else 503, "Search is pending; retry shortly"
        )
    # Validate again at this boundary even for replay/substitute providers.
    # Revalidate after provider work: permission, ownership and collection revisions may
    # change while the call is running. Cached ranks never grant source authority.
    baseline = await media_moments.search_moments(
        db, owner, identifier, revision=revision, query=query, limit=100, all_candidates=True
    )
    by_id = {str(value.id): value for value in baseline.moments}
    ordered = list(dict.fromkeys(output.moment_ids))
    return baseline.model_copy(
        update={
            "method": "semantic-rerank-v1",
            "moments": [by_id[value] for value in ordered if value in by_id][:10],
        }
    )
