"""Reusable, owner-scoped facts for immutable media-library source bytes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, replace
from typing import Any

import structlog
from sqlalchemy import text

from app.config import settings
from app.database import sync_engine
from app.pipeline.clip_cache import (
    MediaAssetFactKey,
    cache_versioned_media_result,
    claim_media_asset_fact,
    complete_media_asset_fact,
    get_media_asset_fact,
)

log = structlog.get_logger()
_FACT_SCHEMA_SUFFIX = "source-facts/filter-policy-v1"
_SEMANTIC_SCHEMA_SUFFIX = "semantic-complete-v2"


@dataclass(frozen=True, slots=True)
class _Source:
    asset_id: str
    source_id: str
    owner_id: str
    kind: str
    role: str
    path: str
    generation: str
    uploaded_sha256: str
    status: str
    attempt_token: str | None = None


def _spec(kind: str, analyzer: str | None) -> tuple[str, str, str, str]:
    if analyzer == "transcript" or kind == "audio":
        from app.pipeline.transcribe import _TRANSCRIPT_CACHE_VERSION
        from app.schemas.user_song import SONG_ANALYSIS_VERSION

        whisper_model = (
            "whisper-1" if settings.whisper_backend == "openai-api" else settings.whisper_model
        )
        return (
            "nova.media.transcript" if analyzer == "transcript" else "nova.media.song_source",
            f"whisper:{settings.whisper_backend}:{whisper_model}",
            f"source-transcription/{_TRANSCRIPT_CACHE_VERSION}",
            f"words-v1/beat-v{SONG_ANALYSIS_VERSION}/{_FACT_SCHEMA_SUFFIX}",
        )
    if analyzer not in (None, "video", "image"):
        raise ValueError("unsupported media-library analyzer")
    from app.tasks.autoplace import _MIN_FRESH_ANALYSIS_VERSION_BY_KIND, ANALYSIS_VERSION

    if (analyzer == "image") or (analyzer is None and kind == "image"):
        from app.agents.image_metadata import ImageMetadataAgent

        version = _MIN_FRESH_ANALYSIS_VERSION_BY_KIND.get("image", ANALYSIS_VERSION)
        return (
            ImageMetadataAgent.spec.name,
            ImageMetadataAgent.spec.model,
            ImageMetadataAgent.spec.prompt_version,
            f"image-v{version}/{_FACT_SCHEMA_SUFFIX}/{_SEMANTIC_SCHEMA_SUFFIX}",
        )
    from app.agents.clip_metadata import ClipMetadataAgent

    return (
        ClipMetadataAgent.spec.name,
        ClipMetadataAgent.spec.model,
        ClipMetadataAgent.spec.prompt_version,
        f"video-v{ANALYSIS_VERSION}/{_FACT_SCHEMA_SUFFIX}/{_SEMANTIC_SCHEMA_SUFFIX}",
    )


def _row_to_source(row: Any) -> _Source:
    return _Source(
        **{field: str(row[field]) for field in _Source.__dataclass_fields__ if field in row}
    )


def _requested_source(asset_id: str, owner_id: str, source_id: str | None) -> _Source | None:
    predicate = (
        "s.id = CAST(:source_id AS uuid)" if source_id else "s.asset_id = CAST(:asset_id AS uuid)"
    )
    with sync_engine.begin() as conn:
        row = (
            conn.execute(
                text(f"""
            SELECT s.asset_id, s.id AS source_id, s.user_id AS owner_id, a.kind,
                   s.role, s.object_path AS path,
                   s.object_generation AS generation, s.uploaded_sha256, s.status
            FROM media_asset_sources s JOIN media_assets a
              ON a.id = s.asset_id AND a.user_id = s.user_id
            WHERE s.user_id = CAST(:owner_id AS uuid) AND {predicate}
              AND s.asset_id = CAST(:asset_id AS uuid)
              AND s.role IN ('original', 'analysis_proxy')
              AND s.uploaded_sha256 ~ '^[0-9A-Fa-f]{{64}}$'
            ORDER BY CASE WHEN s.status = 'available' THEN 0 ELSE 1 END,
                     s.created_at DESC, s.id DESC LIMIT 1
        """),
                {"asset_id": asset_id, "owner_id": owner_id, "source_id": source_id},
            )
            .mappings()
            .one_or_none()
        )
    return _row_to_source(row) if row else None


def _canonical_source(source: _Source) -> _Source | None:
    with sync_engine.begin() as conn:
        row = (
            conn.execute(
                text("""
            SELECT s.asset_id, s.id AS source_id, s.user_id AS owner_id, a.kind,
                   s.role, s.object_path AS path,
                   s.object_generation AS generation, s.uploaded_sha256, s.status
            FROM media_asset_sources s JOIN media_assets a
              ON a.id = s.asset_id AND a.user_id = s.user_id
            WHERE s.user_id = CAST(:owner_id AS uuid) AND lower(s.uploaded_sha256) = lower(:sha256)
              AND a.kind = :kind AND s.role = :role
            ORDER BY s.created_at, s.id LIMIT 1
        """),
                {
                    "owner_id": source.owner_id,
                    "sha256": source.uploaded_sha256,
                    "kind": source.kind,
                    "role": source.role,
                },
            )
            .mappings()
            .one_or_none()
        )
    return _row_to_source(row) if row else None


def _key(source: _Source, analyzer: str | None) -> MediaAssetFactKey:
    name, model, prompt, schema = _spec(source.kind, analyzer)
    identity = (
        f"sha256:{source.uploaded_sha256.lower()}:kind:{source.kind}:"
        f"role:{source.role}:filter:{_FACT_SCHEMA_SUFFIX}"
    )
    return MediaAssetFactKey(source.owner_id, identity, name, model, prompt, schema)


def get_media_asset_analysis(
    *,
    asset_id: str,
    owner_id: str,
    source_id: str | None = None,
    analyzer: str | None = None,
    required_generation: str | None = None,
) -> dict[str, Any] | None:
    """Return compatible durable facts without reading cloud storage."""
    source = _requested_source(asset_id, owner_id, source_id)
    if source is None or (
        required_generation is not None and source.generation != str(required_generation)
    ):
        return None
    return get_media_asset_fact(_key(source, analyzer))


def media_asset_analysis_status(*, asset_id: str, owner_id: str) -> str:
    """Distinguish another worker's live lease from a failed analysis."""
    source = _requested_source(asset_id, owner_id, None)
    if source is None:
        return "failed"
    key = _key(source, None)
    with sync_engine.begin() as conn:
        state = conn.scalar(
            text("""
            SELECT status FROM media_asset_analyses
            WHERE user_id=CAST(:owner AS uuid) AND source_identity=:identity
              AND analyzer=:analyzer AND model=:model AND prompt_version=:prompt
              AND schema_version=:schema ORDER BY updated_at DESC LIMIT 1
        """),
            {
                "owner": owner_id,
                "identity": key.source_identity,
                "analyzer": key.analyzer,
                "model": key.model,
                "prompt": key.prompt_version,
                "schema": key.schema_version,
            },
        )
    return "processing" if state in {"queued", "analyzing"} else str(state or "failed")


def get_media_asset_facts_batch(sources: list[_Source]) -> dict[str, dict]:
    """Read a bounded catalogue page with one query and no cache writes or providers."""
    if not sources:
        return {}
    owner = sources[0].owner_id
    if any(source.owner_id != owner for source in sources):
        raise ValueError("Mixed-owner catalogue page")
    keys = {_key(source, None) for source in sources}
    params = {"owner": owner}
    conditions = []
    for index, key in enumerate(keys):
        conditions.append(
            f"(source_identity=:i{index} AND analyzer=:a{index} AND model=:m{index} "
            f"AND prompt_version=:p{index} AND schema_version=:s{index})"
        )
        params.update(
            {
                f"i{index}": key.source_identity,
                f"a{index}": key.analyzer,
                f"m{index}": key.model,
                f"p{index}": key.prompt_version,
                f"s{index}": key.schema_version,
            }
        )
    with sync_engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT source_identity, analyzer, model, prompt_version, "
                    "schema_version, result_json "
                    "FROM media_asset_analyses WHERE user_id=CAST(:owner AS uuid) "
                    "AND status='ready' "
                    "AND (" + " OR ".join(conditions) + ")"
                ),
                params,
            )
            .mappings()
            .all()
        )
    results = {
        MediaAssetFactKey(
            owner,
            row["source_identity"],
            row["analyzer"],
            row["model"],
            row["prompt_version"],
            row["schema_version"],
        ): row["result_json"]
        for row in rows
    }
    return {
        source.asset_id: results[_key(source, None)]
        for source in sources
        if _key(source, None) in results
    }


def source_for_path(*, owner_id: str, path: str, generation: str) -> _Source | None:
    """Owner-bound bridge for existing thread, pool and song projections."""
    with sync_engine.begin() as conn:
        row = conn.execute(
            text("""SELECT asset_id, id FROM media_asset_sources
            WHERE user_id=CAST(:owner AS uuid) AND object_path=:path
              AND object_generation=:generation LIMIT 1"""),
            {"owner": owner_id, "path": path, "generation": str(generation)},
        ).first()
    return _requested_source(str(row.asset_id), owner_id, str(row.id)) if row else None


def _analyze_source(
    source: _Source, *, analyzer: str | None = None, snapshot_local_path: str | None = None
) -> dict[str, Any]:
    from app import storage
    from app.agents._runtime import RunContext
    from app.tasks import autoplace

    metadata = storage.object_metadata(source.path)
    if str(metadata.generation) != source.generation:
        raise ValueError("registered media source generation changed")
    with tempfile.TemporaryDirectory(prefix="media-library-analysis-") as tmpdir:
        local = snapshot_local_path or os.path.join(tmpdir, "source")
        if snapshot_local_path is None:
            storage.download_generation_to_file(source.path, local, generation=source.generation)
        digest = hashlib.sha256()
        with open(local, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != source.uploaded_sha256.lower():
            raise ValueError("registered source bytes do not match the verified identity")
        ctx = RunContext(
            creator_id=source.owner_id,
            request_id=f"library-fact:{source.uploaded_sha256}:{source.attempt_token}",
            usage_purpose="optional_background",
        )
        if analyzer == "transcript":
            from app.services.speech_segments import transcribe_clip

            words, language = transcribe_clip(local, run_context=ctx)
            analysis = {"source": "source_transcript", "words": words, "language": language}
        elif source.kind == "audio":
            from app.tasks.user_song import _analyze

            with sync_engine.begin() as conn:
                duration = conn.scalar(
                    text(
                        "SELECT metadata_json->>'duration_s' FROM media_assets "
                        "WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": source.asset_id},
                )
            analysis = _analyze(
                {"path": source.path, "duration_s": float(duration or 0)},
                int(source.generation),
                local_path=local,
                run_context=ctx,
                strict_source_facts=True,
            ).model_dump(mode="json")
        elif source.kind == "image":
            result, aspect, dims, has_alpha = autoplace.analyze_pool_image(
                local, source.source_id, run_context=ctx
            )
            if not isinstance(result, dict) or not result:
                raise ValueError("provider returned no compatible semantic analysis")
            analysis = dict(result)
            if dims:
                analysis.update({"width": dims[0], "height": dims[1], "dims_upright": True})
            analysis.update(
                {
                    "aspect": aspect,
                    "has_alpha": has_alpha,
                    "analysis_version": autoplace.ANALYSIS_VERSION,
                }
            )
        else:
            result, aspect, duration, dims = autoplace.analyze_pool_video(local, run_context=ctx)
            if not isinstance(result, dict) or not result:
                raise ValueError("provider returned no compatible semantic analysis")
            analysis = dict(result)
            if dims:
                analysis.update({"width": dims[0], "height": dims[1]})
            analysis.update(
                {
                    "aspect": aspect,
                    "duration_s": duration,
                    "analysis_version": autoplace.ANALYSIS_VERSION,
                }
            )
    if str(storage.object_metadata(source.path).generation) != source.generation:
        raise ValueError("media source generation changed during analysis")
    if not analysis or analysis.get("source") in {"probe_only", "stub"}:
        raise ValueError("provider returned no compatible semantic analysis")
    return analysis


def ensure_media_asset_analysis(
    *,
    asset_id: str,
    owner_id: str,
    source_id: str | None = None,
    analyzer: str | None = None,
    required_generation: str | None = None,
    snapshot_local_path: str | None = None,
) -> dict[str, Any] | None:
    """Reuse or compute a source fact. A lease miss never makes a second paid call."""
    if not settings.media_library_enabled:
        return None
    source = _requested_source(asset_id, owner_id, source_id)
    if source is None:
        return None
    if required_generation is not None and source.generation != str(required_generation):
        return None
    key = _key(source, analyzer)
    cached = get_media_asset_fact(key)
    if cached is not None:
        log.info(
            "media_library_fact_hit",
            asset_id=asset_id,
            source_id=source.source_id,
            analyzer=key.analyzer,
            provider_calls=0,
        )
        return cached
    if source.status != "available":
        return None
    canonical = _canonical_source(source)
    if canonical is None:
        return None
    claim = claim_media_asset_fact(
        key, canonical_asset_id=canonical.asset_id, canonical_source_id=canonical.source_id
    )
    if claim.status == "ready":
        return claim.result_json
    if claim.status != "claimed":
        return None
    log.info(
        "media_library_fact_miss",
        asset_id=asset_id,
        source_id=source.source_id,
        analyzer=key.analyzer,
    )
    try:
        source = replace(source, attempt_token=claim.attempt_token)
        if snapshot_local_path is not None:
            result = _analyze_source(
                source, analyzer=analyzer, snapshot_local_path=snapshot_local_path
            )
        else:
            result = (
                _analyze_source(source, analyzer=analyzer) if analyzer else _analyze_source(source)
            )
    except Exception as exc:
        log.warning("media_asset_analysis_failed", asset_id=asset_id, error_type=type(exc).__name__)
        complete_media_asset_fact(claim, None, failed=True)
        return None
    if not complete_media_asset_fact(claim, result):
        return None
    cache_versioned_media_result(key, result)
    return result
