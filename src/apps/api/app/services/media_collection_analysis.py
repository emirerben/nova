"""A cache-only request cannot acquire work or reach a provider."""

import hashlib
from dataclasses import replace

from fastapi import HTTPException

from app.agents._runtime import RunContext
from app.config import settings
from app.pipeline.clip_cache import (
    MediaAssetFactClaim,
    claim_media_asset_fact,
    get_media_asset_fact,
)

REQUEST_SCHEMA_TAG = "/request:"


def _request_key(key):
    identity = hashlib.sha256(key.source_identity.encode()).hexdigest()
    return replace(key, schema_version=f"{key.schema_version}{REQUEST_SCHEMA_TAG}{identity}")


def get_collection_analysis(key):
    cached = get_media_asset_fact(key)
    return cached if cached is not None else get_media_asset_fact(_request_key(key))


def claim_collection_analysis(key, *, cache_only=False, **kwargs):
    # Preserve eligible pre-fix results. The source-fact uniqueness constraint
    # has one row per source/analyzer/version, so query facts also need their
    # immutable request identity in that version slot.
    cached = get_collection_analysis(key)
    if cached is not None:
        return MediaAssetFactClaim(status="ready", result_json=cached)
    if cache_only or settings.media_collection_cache_only:
        raise HTTPException(
            409,
            {
                "code": "collection_analysis_unavailable",
                "message": "No eligible cached result; new model calls are disabled",
            },
        )
    return claim_media_asset_fact(_request_key(key), **kwargs)


def collection_run_context(**kwargs):
    # Keep the shared atomic development-run cap across ranking, stories,
    # inspections and ordinary library analysis. A per-call estimate must not
    # accidentally disable the operator-approved manual-QA attribution.
    if (
        settings.ai_usage_environment == "development"
        and settings.ai_manual_qa_reservation_approved
        and settings.ai_manual_qa_test_run_id.strip()
        and settings.ai_manual_qa_max_cost_usd is not None
    ):
        kwargs.update(
            usage_purpose="manual_qa",
            test_run_id=settings.ai_manual_qa_test_run_id.strip(),
            estimated_max_cost_usd=settings.ai_manual_qa_max_cost_usd,
            reservation_approved=True,
        )
    return RunContext(**kwargs)
