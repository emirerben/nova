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
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import MediaAsset, MediaAssetAnalysis, MediaAssetSource, MediaSourceMoment
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

INDEX_VERSION = "source-intervals-v3"
MAX_SOURCE_MOMENTS = 2000
SOURCE_CHECK_BATCH_SIZE = 10
WHOLE_SOURCE_CONTEXT_VERSION = "whole-source-context-v1"


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
        # Visual analysis timestamps are approximate. Preserve the portion that
        # actually exists instead of dropping a short clip for an overlong end.
        # Speech stays exact: shortening a word or sentence would change it.
        if kind == "visual" and end is not None:
            end = min(end, duration)
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
        *(analysis.get("best_moments") or []),
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


def _whole_source_context(analysis: dict, duration: float) -> dict | None:
    """Whole analysed source bounds, never an inferred scene/highlight window.

    Keep the existing 1–20s candidate admission; longer clips need a creator
    window, not a silently shortened 'whole clip'. One 30fps quantization frame
    accommodates probe rounding, while both bounds must remain finite/positive.
    """
    if analysis.get("source") in {"probe_only", "stub"}:
        return None
    probed = _number(analysis.get("duration_s"))
    if probed is None or probed <= 0 or abs(probed - duration) > 1 / 30:
        return None
    end = min(probed, duration)
    if not 1 <= end <= 20:
        return None
    understanding = analysis.get("understanding")
    understanding = understanding if isinstance(understanding, dict) else {}
    description = _clean_text(analysis.get("description"), 1600) or _clean_text(
        understanding.get("summary"), 1600
    )
    return {
        "kind": "visual",
        "start_s": 0.0,
        "end_s": end,
        "text": "Whole analysed clip; no localized highlight. "
        + (
            "Cached overall description: " + description
            if description
            else "No visual description is available; review the footage."
        ),
        "uncertain": True,
    }


def _valid_context_bounds(start: float, end: float, duration: Any) -> bool:
    duration = _number(duration)
    return (
        duration is not None
        and math.isfinite(end)
        and start == 0
        and 1 <= end <= 20
        and end <= duration
        and abs(end - duration) <= 1 / 30
    )


def _interval_quality(analysis: dict, duration: float, interval: dict) -> dict:
    raw_quality_spans = analysis.get("unusable_spans")
    if not isinstance(raw_quality_spans, list):
        raw_quality_spans = []
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
    return {
        "source_visibility": clip_record(analysis, kind="video").visibility[:160],
        "overlapping_unusable_spans": quality_spans[:3],
        "verification": "cached_evidence_only",
    }


async def index_collection(
    db: AsyncSession, owner: uuid.UUID, identifier: uuid.UUID, body: CollectionIndexBody
) -> CollectionIndexOut:
    await lock_library_owner(db, owner)
    row = await owned_collection(db, owner, identifier, revision=body.expected_revision)
    selected = members(row)
    batch = selected[body.offset : body.offset + body.limit]
    available = await eligible_sources(db, owner, batch)
    unavailable, missing, truncated, untimed, context, unusable_timing = [], [], [], [], [], []
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
        kwargs = dict(
            asset_id=str(asset.id),
            owner_id=str(owner),
            source_id=str(source.id),
            required_generation=source.object_generation,
        )
        analysis = await asyncio.to_thread(facts.get_media_asset_analysis, **kwargs)
        transcript = await asyncio.to_thread(
            facts.get_media_asset_analysis, **kwargs, analyzer="transcript"
        )
        if duration is None or duration <= 0:
            (missing if analysis is None or transcript is None else unusable_timing).append(
                asset.id
            )
            continue
        spec = _index_spec(asset.kind)
        version = hashlib.sha256(
            json.dumps(
                [spec, analysis or {}, transcript or {}], sort_keys=True, separators=(",", ":")
            ).encode()
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
        if (
            existing is not None
            and existing.evidence_json.get("evidence_scope") == "whole_source_context"
        ):
            candidate = (
                _whole_source_context(analysis, duration)
                if analysis is not None and transcript is not None
                else None
            )
            if (
                candidate is None
                or existing.evidence_json.get("context_contract") != WHOLE_SOURCE_CONTEXT_VERSION
                or existing.start_s != candidate["start_s"]
                or existing.end_s != candidate["end_s"]
            ):
                existing = None
        if existing is not None:
            if existing.evidence_json.get("intervals_truncated"):
                truncated.append(asset.id)
            indexed += 1
            if existing.evidence_json.get("context_contract") == WHOLE_SOURCE_CONTEXT_VERSION:
                context.append(asset.id)
            continue
        intervals, capped = source_intervals(analysis or {}, transcript or {}, duration)
        whole_context = False
        if not intervals:
            if analysis is None or transcript is None:
                missing.append(asset.id)
            elif candidate := _whole_source_context(analysis, duration):
                intervals = [candidate]
                whole_context = True
                context.append(asset.id)
            else:
                unusable_timing.append(asset.id)
        if capped:
            truncated.append(asset.id)
        await db.execute(
            delete(MediaSourceMoment).where(
                MediaSourceMoment.user_id == owner, MediaSourceMoment.source_id == source.id
            )
        )
        for interval in intervals:
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
                        **(
                            {
                                "evidence_scope": "whole_source_context",
                                "context_contract": WHOLE_SOURCE_CONTEXT_VERSION,
                                "semantic_localization": "none",
                                "timing_basis": "verified_source_bounds",
                            }
                            if whole_context
                            else {}
                        ),
                        "quality": _interval_quality(analysis or {}, duration, interval),
                        "complete_timed_speech": isinstance((transcript or {}).get("words"), list)
                        and all(
                            _timed_word(word, duration) is not None
                            for word in (transcript or {})["words"]
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
        context_asset_ids=context,
        unusable_timing_asset_ids=unusable_timing,
    )


def _retained_timed_spec() -> tuple[str, list[tuple[str, str, str, str]]] | None:
    # KRI-845 adds optional props/actions/visual-quality facts. It does not revoke
    # already reviewed intervals from this exact predecessor. Future contracts
    # need a separate compatibility decision; new searches always use _index_spec.
    video = (
        "nova.video.clip_metadata",
        "gemini-2.5-flash",
        "2026-10-23.1",
        "video-v8/source-facts/filter-policy-v1/semantic-complete-v2",
    )
    transcript = (
        "nova.media.transcript",
        "whisper:openai-api:whisper-1",
        "source-transcription/v2",
        "words-v1/beat-v1/source-facts/filter-policy-v1",
    )
    if facts._spec("video", None) != video or facts._spec("video", "transcript") != transcript:
        return None
    previous = (*video[:2], "2026-10-13.1", video[3])
    versions = [previous, transcript]
    spec = hashlib.sha256(json.dumps([INDEX_VERSION, *versions]).encode()).hexdigest()
    return spec, versions


def _stable_moment(raw: dict) -> dict:
    value = MediaMomentOut.model_validate(raw).model_dump(mode="json")
    value["evidence"].pop("source_checked_at", None)
    return value


async def _retained_timed_ids(db, owner, collection, story, available) -> list[uuid.UUID]:
    """Re-prove exact persisted timed choices; never admit generic stale search rows."""
    contract = _retained_timed_spec()
    if (
        contract is None
        or story is None
        or story.user_id != owner
        or story.collection_id != collection.id
        or story.collection_revision != collection.revision
        or not 1 <= len(story.moments_json) <= 8
    ):
        return []
    spec, versions = contract
    result = []
    for frozen in story.moments_json:
        if (frozen.get("evidence") or {}).get("index_spec") != spec:
            continue
        moment = await db.scalar(
            select(MediaSourceMoment).where(
                MediaSourceMoment.id == uuid.UUID(frozen["id"]),
                MediaSourceMoment.user_id == owner,
            )
        )
        if moment is None:
            continue
        pair = available.get(moment.asset_id)
        if pair is None:
            continue
        asset, source = pair
        evidence = moment.evidence_json
        if (
            source.id != moment.source_id
            or asset.kind != "video"
            or source.role != "analysis_proxy"
            or evidence.get("index_spec") != spec
            or evidence.get("analyzer_versions") != [list(v) for v in versions]
            or evidence.get("generation") != source.object_generation
            or evidence.get("uploaded_sha256") != source.uploaded_sha256
            or evidence.get("evidence_scope", "timed_moment") != "timed_moment"
            or evidence.get("fact_version") != moment.fact_version
        ):
            continue
        current = MediaMomentOut(
            id=moment.id,
            asset_id=asset.id,
            source_id=source.id,
            generation=source.object_generation,
            start_s=moment.start_s,
            end_s=moment.end_s,
            kind=moment.kind,
            text=moment.text,
            uncertain=moment.uncertain,
            evidence_scope="timed_moment",
            evidence=evidence,
        ).model_dump(mode="json")
        if _stable_moment(current) != _stable_moment(frozen):
            continue
        source_ref = facts._Source(
            str(asset.id),
            str(source.id),
            str(owner),
            asset.kind,
            source.role,
            source.object_path,
            source.object_generation,
            source.uploaded_sha256,
            source.status,
        )
        identity = facts._key(source_ref, None).source_identity
        retained = []
        for name, model, prompt, schema in versions:
            fact = await db.scalar(
                select(MediaAssetAnalysis).where(
                    MediaAssetAnalysis.user_id == owner,
                    MediaAssetAnalysis.source_identity == identity,
                    MediaAssetAnalysis.analyzer == name,
                    MediaAssetAnalysis.model == model,
                    MediaAssetAnalysis.prompt_version == prompt,
                    MediaAssetAnalysis.schema_version == schema,
                    MediaAssetAnalysis.status == "ready",
                )
            )
            if fact is None or not isinstance(fact.result_json, dict):
                break
            retained.append(fact.result_json)
        if len(retained) != 2:
            continue
        version = hashlib.sha256(
            json.dumps([spec, *retained], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        duration = _number((asset.metadata_json or {}).get("duration_s"))
        if version != moment.fact_version or duration is None or duration <= 0:
            continue
        intervals, capped = source_intervals(*retained, duration)
        # The deterministic UUID binds the original fact digest, source and full
        # interval (including wording/uncertainty), not just supplied bounds.
        interval = dict(
            kind=moment.kind,
            start_s=moment.start_s,
            end_s=moment.end_s,
            text=moment.text,
            uncertain=moment.uncertain,
        )
        complete_speech = isinstance(retained[1].get("words"), list) and all(
            _timed_word(word, duration) is not None for word in retained[1]["words"]
        )
        if (
            evidence.get("quality") != _interval_quality(retained[0], duration, interval)
            or evidence.get("complete_timed_speech") != complete_speech
            or evidence.get("intervals_truncated") != capped
            or evidence.get("time_basis") != "source_seconds"
            or evidence.get("verification") != "cached_analysis_only"
        ):
            continue
        if (
            interval in intervals
            and uuid.uuid5(
                uuid.NAMESPACE_URL, json.dumps([str(source.id), version, interval], sort_keys=True)
            )
            == moment.id
        ):
            result.append(moment.id)
    return result


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
    retained_story=None,
) -> MomentSearchOut:
    row = await owned_collection(db, owner, identifier, revision=revision)
    selected = members(row)
    available = await eligible_sources(db, owner, selected)
    source_durations = {
        source.id: (asset.metadata_json or {}).get("duration_s")
        for asset, source in available.values()
    }
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
                MediaSourceMoment.evidence_json["evidence_scope"].astext,
                MediaSourceMoment.start_s,
                MediaSourceMoment.end_s,
                MediaSourceMoment.evidence_json["context_contract"].astext,
            )
            .where(
                MediaSourceMoment.user_id == owner,
                MediaSourceMoment.source_id.in_(sources),
                MediaSourceMoment.evidence_json["index_spec"].astext == _index_spec("video"),
            )
            .distinct()
        )
    ).all()
    coverage = [
        value
        for value in coverage
        if value[3] != "whole_source_context"
        or (
            value[6] == WHOLE_SOURCE_CONTEXT_VERSION
            and _valid_context_bounds(value[4], value[5], source_durations.get(value[0]))
        )
    ]
    indexed = {value[0] for value in coverage}
    full_speech = {value[0] for value in coverage if value[1] == "true"}
    capped_sources = {value[0] for value in coverage if value[2] == "true"}
    unindexed = [
        member.asset_id
        for member in selected
        if member.source_id in sources and member.source_id not in indexed
    ]
    retained_ids = (
        await _retained_timed_ids(db, owner, row, retained_story, available)
        if retained_story is not None and moment_ids is not None
        else []
    )
    current_or_retained = MediaSourceMoment.evidence_json["index_spec"].astext == _index_spec(
        "video"
    )
    if retained_ids:
        current_or_retained = or_(
            current_or_retained,
            and_(
                MediaSourceMoment.id.in_(retained_ids),
                MediaSourceMoment.evidence_json["index_spec"].astext == _retained_timed_spec()[0],
            ),
        )
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
            current_or_retained,
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
        if moment.evidence_json.get("evidence_scope") == "whole_source_context" and (
            moment.evidence_json.get("context_contract") != WHOLE_SOURCE_CONTEXT_VERSION
            or not _valid_context_bounds(
                moment.start_s, moment.end_s, source_durations.get(source.id)
            )
        ):
            continue
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
                evidence_scope=moment.evidence_json.get("evidence_scope", "timed_moment"),
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
