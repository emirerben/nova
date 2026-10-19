"""A cache-only request cannot acquire work or reach a provider."""

from fastapi import HTTPException

from app.agents._runtime import RunContext
from app.config import settings
from app.pipeline.clip_cache import (
    MediaAssetFactClaim,
    claim_media_asset_fact,
    get_media_asset_fact,
)


def claim_collection_analysis(key, *, cache_only=False, **kwargs):
    if cache_only or settings.media_collection_cache_only:
        cached = get_media_asset_fact(key)
        if cached is None:
            raise HTTPException(409, "No eligible cached result; new model calls are disabled")
        return MediaAssetFactClaim(status="ready", result_json=cached)
    return claim_media_asset_fact(key, **kwargs)


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
