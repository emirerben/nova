"""Source-pinned interval index from complete reusable facts, not prompt excerpts.

This is the deterministic text baseline. No implicit analysis, downloads, paid
calls or semantic-quality claims. The collection's index cursor bounds each pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from google.api_core.exceptions import NotFound
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import MediaAsset, MediaAssetSource, MediaSourceMoment
from app.schemas.clip_understanding import _clean_text
from app.schemas.media_collections import (
    CollectionIndexBody,
    CollectionIndexOut,
    MediaMomentOut,
    MomentSearchOut,
)
from app.services import media_library_analysis as facts
from app.services.clip_understanding import clip_record
from app.services.media_collections import eligible_sources, members, owned_collection
from app.services.media_library_ingest import lock_library_owner

INDEX_VERSION = "source-intervals-v2"
MAX_SOURCE_MOMENTS = 2000
SOURCE_CHECK_BATCH_SIZE = 10


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _index_spec(kind: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [INDEX_VERSION, facts._spec(kind, None), facts._spec(kind, "transcript")]
        ).encode()
    ).hexdigest()


def _timed_word(raw: Any, duration: float) -> tuple[float, float, str] | None:
    if not isinstance(raw, dict):
        return None
    start, end = _number(raw.get("start")), _number(raw.get("end"))
    text = raw.get("word", raw.get("text"))
    if start is None or end is None or not (0 <= start < end <= duration):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return start, end, text.strip()


def source_intervals(analysis: dict, transcript: dict, duration: float) -> tuple[list[dict], bool]:
    """Retain full timed speech and actual visual windows; never infer timestamps."""
    result = []
    seen = set()

    def add(kind, start, end, text, uncertain):
        start, end = _number(start), _number(end)
        text = _clean_text(text, 2000)
        if start is None or end is None or not (0 <= start < end <= duration) or not text:
            return
        key = (kind, start, end, text)
        if key not in seen:
            seen.add(key)
            result.append(
                {"kind": kind, "start_s": start, "end_s": end, "text": text, "uncertain": uncertain}
            )

    understanding = analysis.get("understanding") or {}
    if not isinstance(understanding, dict):
        understanding = {}
    for value in [
        *(understanding.get("notable_moments") or []),
        *(analysis.get("important_moments") or []),
    ]:
        if isinstance(value, dict):
            add(
                "visual",
                value.get("start_s", value.get("start")),
                value.get("end_s", value.get("end")),
                value.get("description"),
                True,
            )
    # Full word-timed facts preserve lines long after the prompt excerpt boundary.
    sentence = []
    previous_end = 0.0

    def finish():
        if sentence:
            add(
                "speech",
                sentence[0][0],
                max(word[1] for word in sentence),
                " ".join(word[2] for word in sentence),
                True,  # Timed ASR is evidence; it has not been checked against the audio.
            )
            sentence.clear()

    for raw in transcript.get("words") or []:
        word = _timed_word(raw, duration)
        if word is None:
            finish()
            continue
        start, end, text = word
        if start < previous_end:
            # Word aligners can overlap adjacent words. Keep all the actual words;
            # uncertainty is explicit rather than dropping one from the quote.
            if not sentence or start < sentence[-1][0]:
                finish()
                add("speech", start, end, text, True)
                previous_end = end
                continue
        if sentence and (
            start - previous_end > 1 or end - sentence[0][0] > 20 or len(sentence) >= 40
        ):
            finish()
        sentence.append((start, end, text.strip()))
        previous_end = end
        if text.rstrip().endswith((".", "?", "!", "…")):
            finish()
    finish()
    if not transcript.get("words"):
        speech = understanding.get("speech") or {}
        if not isinstance(speech, dict):
            speech = {}
        for raw in speech.get("segments") or []:
            if isinstance(raw, dict):
                add("speech", raw.get("start_s"), raw.get("end_s"), raw.get("text"), True)
    return result[:MAX_SOURCE_MOMENTS], len(result) > MAX_SOURCE_MOMENTS


async def index_collection(
    db: AsyncSession, owner: uuid.UUID, identifier: uuid.UUID, body: CollectionIndexBody
) -> CollectionIndexOut:
    await lock_library_owner(db, owner)
    row = await owned_collection(db, owner, identifier, revision=body.expected_revision)
    selected = members(row)
    batch = selected[body.offset : body.offset + body.limit]
    available = await eligible_sources(db, owner, batch)
    unavailable, missing, truncated, untimed = [], [], [], []
    indexed = 0
    for member in batch:
        pair = available.get(member.asset_id)
        if pair is None:
            unavailable.append(member.asset_id)
            continue
        asset, source = pair
        if asset.kind != "video":
            # A still image has no source interval. Do not invent a duration.
            untimed.append(asset.id)
            continue
        duration = _number((asset.metadata_json or {}).get("duration_s"))
        if duration is None or duration <= 0:
            missing.append(asset.id)
            continue
        kwargs = dict(
            asset_id=str(asset.id),
            owner_id=str(owner),
            source_id=str(source.id),
            required_generation=source.object_generation,
        )
        analysis = await asyncio.to_thread(facts.get_media_asset_analysis, **kwargs) or {}
        transcript = (
            await asyncio.to_thread(facts.get_media_asset_analysis, **kwargs, analyzer="transcript")
            or {}
        )
        spec = _index_spec(asset.kind)
        version = hashlib.sha256(
            json.dumps([spec, analysis, transcript], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = await db.scalar(
            select(MediaSourceMoment)
            .where(
                MediaSourceMoment.user_id == owner,
                MediaSourceMoment.source_id == source.id,
                MediaSourceMoment.fact_version == version,
            )
            .limit(1)
        )
        if existing is not None:
            if existing.evidence_json.get("intervals_truncated"):
                truncated.append(asset.id)
            indexed += 1
            continue
        intervals, capped = source_intervals(analysis, transcript, duration)
        visibility = clip_record(analysis, kind="video").visibility
        raw_quality_spans = analysis.get("unusable_spans")
        if not isinstance(raw_quality_spans, list):
            raw_quality_spans = []
        if not intervals:
            missing.append(asset.id)
        if capped:
            truncated.append(asset.id)
        await db.execute(
            delete(MediaSourceMoment).where(
                MediaSourceMoment.user_id == owner, MediaSourceMoment.source_id == source.id
            )
        )
        for interval in intervals:
            quality_spans = []
            for raw in raw_quality_spans[:20]:
                if not isinstance(raw, dict):
                    continue
                start, end = _number(raw.get("start_s")), _number(raw.get("end_s"))
                if (
                    start is not None
                    and end is not None
                    and 0 <= start < end <= duration
                    and start < interval["end_s"]
                    and end > interval["start_s"]
                ):
                    quality_spans.append(
                        {
                            "start_s": start,
                            "end_s": end,
                            "reason": _clean_text(raw.get("reason"), 80),
                        }
                    )
            identity = json.dumps([str(source.id), version, interval], sort_keys=True)
            db.add(
                MediaSourceMoment(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, identity),
                    user_id=owner,
                    asset_id=asset.id,
                    source_id=source.id,
                    fact_version=version,
                    **interval,
                    evidence_json={
                        "index_spec": spec,
                        "generation": source.object_generation,
                        "uploaded_sha256": source.uploaded_sha256,
                        "fact_version": version,
                        "time_basis": "source_seconds",
                        "verification": "cached_analysis_only",
                        "quality": {
                            "source_visibility": visibility[:160],
                            "overlapping_unusable_spans": quality_spans[:3],
                            "verification": "cached_evidence_only",
                        },
                        "complete_timed_speech": isinstance(transcript.get("words"), list)
                        and all(
                            _timed_word(word, duration) is not None for word in transcript["words"]
                        ),
                        "intervals_truncated": capped,
                        "analyzer_versions": [
                            list(facts._spec(asset.kind, None)),
                            list(facts._spec(asset.kind, "transcript")),
                        ],
                    },
                )
            )
        indexed += bool(intervals)
    await db.commit()
    next_offset = body.offset + len(batch)
    return CollectionIndexOut(
        revision=row.revision,
        next_offset=next_offset if next_offset < len(selected) else None,
        indexed_sources=indexed,
        missing_analysis_asset_ids=missing,
        unavailable_asset_ids=unavailable,
        truncated_asset_ids=truncated,
        untimed_asset_ids=untimed,
    )


async def search_moments(
    db: AsyncSession,
    owner: uuid.UUID,
    identifier: uuid.UUID,
    *,
    revision: int,
    query: str,
    limit: int,
    source_id: uuid.UUID | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
    all_candidates: bool = False,
    moment_ids: list[uuid.UUID] | None = None,
) -> MomentSearchOut:
    row = await owned_collection(db, owner, identifier, revision=revision)
    selected = members(row)
    available = await eligible_sources(db, owner, selected)
    unavailable = [member.asset_id for member in selected if member.asset_id not in available]
    sources = [source.id for _, source in available.values()]
    if source_id is not None:
        if source_id not in [member.source_id for member in selected]:
            raise HTTPException(404, "Source not found in this collection")
        sources = [source_id] if source_id in sources else []
    if not sources:
        return MomentSearchOut(revision=row.revision, moments=[], unavailable_asset_ids=unavailable)
    coverage = (
        await db.execute(
            select(
                MediaSourceMoment.source_id,
                MediaSourceMoment.evidence_json["complete_timed_speech"].astext,
                MediaSourceMoment.evidence_json["intervals_truncated"].astext,
            )
            .where(
                MediaSourceMoment.user_id == owner,
                MediaSourceMoment.source_id.in_(sources),
                MediaSourceMoment.evidence_json["index_spec"].astext == _index_spec("video"),
            )
            .distinct()
        )
    ).all()
    indexed = {value[0] for value in coverage}
    full_speech = {value[0] for value in coverage if value[1] == "true"}
    capped_sources = {value[0] for value in coverage if value[2] == "true"}
    unindexed = [
        member.asset_id
        for member in selected
        if member.source_id in sources and member.source_id not in indexed
    ]
    vector = func.to_tsvector("simple", MediaSourceMoment.text)
    terms = func.plainto_tsquery("simple", query)
    statement = (
        select(MediaSourceMoment, MediaAssetSource)
        .join(MediaAssetSource, MediaAssetSource.id == MediaSourceMoment.source_id)
        .join(MediaAsset, MediaAsset.id == MediaSourceMoment.asset_id)
        .where(
            MediaSourceMoment.user_id == owner,
            MediaSourceMoment.source_id.in_(sources),
            MediaAsset.user_id == owner,
            MediaAsset.archived_at.is_(None),
            MediaAssetSource.user_id == owner,
            MediaAssetSource.status == "available",
            MediaSourceMoment.evidence_json["index_spec"].astext == _index_spec("video"),
        )
        .limit(101)
    )
    ordering = (
        func.ts_rank_cd(vector, terms).desc(),
        MediaSourceMoment.start_s,
        MediaSourceMoment.id,
    )
    if all_candidates:
        # Round-robin the relevance-ranked intervals of every selected source.
        # Fifty sources can each contribute before any source takes a third slot.
        statement = statement.order_by(
            func.row_number().over(partition_by=MediaSourceMoment.source_id, order_by=ordering),
            *ordering,
        )
    else:
        statement = statement.order_by(*ordering)
    if moment_ids is not None:
        if len(moment_ids) > 8:
            raise HTTPException(422, "At most eight exact story intervals may be checked")
        statement = statement.where(MediaSourceMoment.id.in_(moment_ids))
    if source_id is not None:
        statement = statement.where(
            MediaSourceMoment.kind == "speech",
            MediaSourceMoment.end_s > start_s,
            MediaSourceMoment.start_s < end_s,
        )
    elif not all_candidates:
        statement = statement.where(vector.op("@@")(terms))
    results = []
    source_counts = {}
    checked_sources = {}
    capped = False
    rows = (await db.execute(statement)).all()
    capped = len(rows) > 100
    rows = rows[:100]
    current_sources = {
        source.id: source
        for moment, source in rows
        if moment.evidence_json.get("generation") == source.object_generation
        and moment.evidence_json.get("uploaded_sha256") == source.uploaded_sha256
    }

    async def check_source(source):
        try:
            metadata = await asyncio.wait_for(
                asyncio.to_thread(storage.object_metadata_once, source.object_path, timeout_s=5),
                timeout=5,
            )
            return source.id, str(
                metadata.generation
            ) == source.object_generation and metadata.size == source.byte_count
        except (NotFound, FileNotFoundError):
            return source.id, False
        except Exception as exc:
            raise HTTPException(503, "Source availability could not be checked; retry.") from exc

    # All added sources remain eligible. Network/memory are bounded per pass,
    # rather than asking the creator to select their content a second time.
    pending = list(current_sources.values())
    for offset in range(0, len(pending), SOURCE_CHECK_BATCH_SIZE):
        checked_sources.update(
            await asyncio.gather(
                *(
                    check_source(source)
                    for source in pending[offset : offset + SOURCE_CHECK_BATCH_SIZE]
                )
            )
        )
    for moment, source in rows:
        if source_id is None and not all_candidates and source_counts.get(source.id, 0) >= 3:
            continue
        if (
            moment.evidence_json.get("generation") != source.object_generation
            or moment.evidence_json.get("uploaded_sha256") != source.uploaded_sha256
        ):
            continue
        if not checked_sources[source.id] and source.asset_id not in unavailable:
            unavailable.append(source.asset_id)
        if not checked_sources[source.id]:
            continue
        source_counts[source.id] = source_counts.get(source.id, 0) + 1
        results.append(
            MediaMomentOut(
                id=moment.id,
                asset_id=moment.asset_id,
                source_id=moment.source_id,
                generation=source.object_generation,
                start_s=moment.start_s,
                end_s=moment.end_s,
                kind=moment.kind,
                text=moment.text,
                uncertain=moment.uncertain,
                evidence={
                    **moment.evidence_json,
                    "source_checked_at": datetime.now(UTC).isoformat(),
                },
            )
        )
        if len(results) >= limit:
            break
    return MomentSearchOut(
        revision=row.revision,
        moments=results,
        unavailable_asset_ids=unavailable,
        unindexed_asset_ids=unindexed,
        incomplete_speech_asset_ids=[
            member.asset_id
            for member in selected
            if member.source_id in indexed and member.source_id not in full_speech
        ],
        truncated_asset_ids=[
            member.asset_id for member in selected if member.source_id in capped_sources
        ],
        candidate_limit_reached=capped,
    )
