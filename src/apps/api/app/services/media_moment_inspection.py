"""Targeted reads of owned immutable bytes with durable leases and hard resource bounds."""

import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from app import storage
from app.agents._model_client import default_client
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionInput
from app.models import MediaAssetAnalysis, MediaSourceMoment
from app.pipeline.clip_cache import (
    MediaAssetFactKey,
    complete_media_asset_fact,
)
from app.services import media_collections, media_moments
from app.services.media_collection_analysis import (
    REQUEST_SCHEMA_TAG,
    claim_collection_analysis,
    collection_run_context,
    get_collection_analysis,
)

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_INSPECTIONS = 3
CROP_VERSION = "targeted-interval-640-v1"


def inspect_interval(source, moment, question, *, ctx, agent_factory=None):
    """Keep temporary bytes and provider work in one thread, even if the caller disconnects."""

    with tempfile.TemporaryDirectory(prefix="moment-inspection-") as directory:
        original = Path(directory) / "source.mp4"
        sample = Path(directory) / "interval.mp4"
        storage.download_generation_to_file(
            source.object_path,
            str(original),
            generation=source.object_generation,
            timeout_s=min(15, max(0.1, ctx.deadline_monotonic - time.monotonic())),
        )
        if (
            original.stat().st_size != source.byte_count
            or original.stat().st_size > MAX_SOURCE_BYTES
        ):
            raise ValueError("Source size changed")
        with original.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != source.uploaded_sha256:
            raise ValueError("Source bytes changed")
        remaining = ctx.deadline_monotonic - time.monotonic()
        if remaining <= 2:
            raise TimeoutError("Inspection deadline expired")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(moment.start_s),
                "-i",
                str(original),
                "-t",
                str(moment.end_s - moment.start_s),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                "scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "24",
                "-c:a",
                "aac",
                str(sample),
            ],
            check=True,
            capture_output=True,
            timeout=min(15, remaining),
        )
        if time.monotonic() >= ctx.deadline_monotonic - 2:
            raise TimeoutError("Inspection deadline expired")
        client, ref = _upload_interval(str(sample), deadline=ctx.deadline_monotonic)
        try:
            agent = (agent_factory or ClipQuestionAgent)(default_client())
            agent.spec = replace(
                agent.spec, max_attempts=1, enable_clarification_retries=False, sensitive_io=True
            )
            result = agent.run(
                ClipQuestionInput(file_uri=ref.uri, file_mime="video/mp4", question=question),
                ctx=ctx,
            )
        finally:
            from google.genai import types

            try:
                client.files.delete(
                    name=ref.name,
                    config=types.DeleteFileConfig(
                        http_options=types.HttpOptions(
                            timeout=1000, retry_options=types.HttpRetryOptions(attempts=1)
                        )
                    ),
                )
            except Exception:
                pass
            client.close()
        with sample.open("rb") as handle:
            sample_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        return {**result.model_dump(), "sample_sha256": sample_hash}


async def _target(db, owner, identifier, revision, moment_id):
    collection = await media_collections.owned_collection(db, owner, identifier, revision=revision)
    selected = media_collections.members(collection)
    eligible = await media_collections.eligible_sources(db, owner, selected)
    moment = await db.get(MediaSourceMoment, moment_id, populate_existing=True)
    if moment is None or moment.user_id != owner or moment.asset_id not in eligible:
        raise HTTPException(404, "Moment not found in this collection")
    _asset, source = eligible[moment.asset_id]
    if moment.source_id != source.id or moment.evidence_json.get(
        "index_spec"
    ) != media_moments._index_spec("video"):
        raise HTTPException(409, "Moment analysis changed; refresh the index")
    if (
        moment.evidence_json.get("generation") != source.object_generation
        or moment.evidence_json.get("uploaded_sha256") != source.uploaded_sha256
    ):
        raise HTTPException(409, "Moment source changed")
    if source.byte_count > MAX_SOURCE_BYTES or moment.end_s - moment.start_s > 20:
        raise HTTPException(
            422, "Choose an interval up to 20 seconds from an analysis copy up to 64 MiB"
        )
    try:
        metadata = await asyncio.wait_for(
            asyncio.to_thread(storage.object_metadata_once, source.object_path, timeout_s=5),
            timeout=5,
        )
    except Exception as exc:
        raise HTTPException(503, "Source availability could not be checked") from exc
    if str(metadata.generation) != source.object_generation or metadata.size != source.byte_count:
        raise HTTPException(409, "Moment source is unavailable or replaced")
    return moment, source


async def inspect_moment(db, owner, identifier, body):
    moment, source = await _target(db, owner, identifier, body.expected_revision, body.moment_id)
    spec = ClipQuestionAgent.spec
    schema = f"{CROP_VERSION}:{identifier}:{body.expected_revision}"
    digest = hashlib.sha256(
        json.dumps(
            [
                str(source.id),
                source.object_generation,
                source.uploaded_sha256,
                moment.fact_version,
                moment.start_s,
                moment.end_s,
                " ".join(body.question.split()).casefold(),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    key = MediaAssetFactKey(str(owner), digest, spec.name, spec.model, spec.prompt_version, schema)
    # Serialize the per-collection cap with collection writers. The fact lease itself commits
    # independently and prevents repeated simultaneous questions from paying twice.
    await media_collections.lock_library_owner(db, owner)
    count = await db.scalar(
        select(func.count())
        .select_from(MediaAssetAnalysis)
        .where(
            MediaAssetAnalysis.user_id == owner,
            or_(
                MediaAssetAnalysis.schema_version == schema,
                MediaAssetAnalysis.schema_version.startswith(
                    schema + REQUEST_SCHEMA_TAG, autoescape=True
                ),
            ),
        )
    )
    cached = await asyncio.to_thread(get_collection_analysis, key)
    if cached is None and count >= MAX_INSPECTIONS:
        raise HTTPException(429, "This collection's three targeted inspections are used")
    claim = await asyncio.to_thread(
        claim_collection_analysis,
        key,
        cache_only=body.cache_only,
        canonical_asset_id=str(moment.asset_id),
        canonical_source_id=str(source.id),
        lease_seconds=90,
    )
    await db.commit()
    if claim.status == "ready":
        result = claim.result_json
    elif claim.status == "claimed":
        try:
            result = await asyncio.to_thread(
                inspect_interval,
                source,
                moment,
                body.question,
                ctx=collection_run_context(
                    creator_id=str(owner),
                    request_id=f"moment-inspect:{digest}",
                    deadline_monotonic=time.monotonic() + 45,
                    timeout_override_s=12,
                    estimated_max_cost_usd=0.05,
                ),
            )
            if not await asyncio.to_thread(complete_media_asset_fact, claim, result):
                raise HTTPException(409, "Inspection changed; retry")
        except Exception:
            await asyncio.to_thread(complete_media_asset_fact, claim, None, failed=True)
            raise
    else:
        raise HTTPException(
            409 if claim.status == "busy" else 503, "Inspection is pending; retry shortly"
        )
    moment, source = await _target(db, owner, identifier, body.expected_revision, body.moment_id)
    return {
        "moment_id": moment.id,
        "source_id": source.id,
        "generation": source.object_generation,
        "start_s": moment.start_s,
        "end_s": moment.end_s,
        "uncertain": True,
        "verification": "targeted_model_inspection",
        **{key: result[key] for key in ("answer", "confidence", "evidence", "sample_sha256")},
    }


def _upload_interval(path, *, deadline):
    """One upload, short polling, no re-upload/backoff beyond the shared deadline."""
    from google import genai
    from google.genai import types

    from app.config import settings

    client = genai.Client(api_key=settings.gemini_api_key)

    def options():
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise TimeoutError("Inspection upload deadline expired")
        return types.HttpOptions(
            timeout=int(min(5, remaining) * 1000), retry_options=types.HttpRetryOptions(attempts=1)
        )

    ref = None
    try:
        ref = client.files.upload(
            file=path, config=types.UploadFileConfig(mime_type="video/mp4", http_options=options())
        )
        while getattr(ref.state, "name", str(ref.state)) == "PROCESSING":
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            ref = client.files.get(
                name=ref.name, config=types.GetFileConfig(http_options=options())
            )
        if getattr(ref.state, "name", str(ref.state)) != "ACTIVE":
            raise ValueError("Targeted media upload failed")
        return client, ref
    except Exception:
        if ref is not None:
            try:
                client.files.delete(
                    name=ref.name,
                    config=types.DeleteFileConfig(
                        http_options=types.HttpOptions(
                            timeout=1000, retry_options=types.HttpRetryOptions(attempts=1)
                        )
                    ),
                )
            except Exception:
                pass
        client.close()
        raise
