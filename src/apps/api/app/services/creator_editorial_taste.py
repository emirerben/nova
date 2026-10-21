"""Owned server history and explicit judgments feeding the real story proposer.

No provider calls, automatic preference inference, publication claims or render
approval. Project-cascading source rows hold words; revision ledgers hold only
confirmed rules and opaque references. Erasure keeps minimal retry fences.
"""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select, update

from app.config import settings
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorEditorialTasteExample,
    CreatorEditorialTasteRevision,
    CreatorIdeaFeedback,
    Job,
    MediaCollectionStory,
    PlanItem,
    PlanItemAsset,
    User,
)
from app.services.editorial_taste import MAX_CONTEXT_CHARS, TasteStore, digest
from app.services.private_text import normalize_private_text


def enabled(owner):
    if not settings.creator_editorial_taste_for(owner):
        raise HTTPException(404, "Editorial taste is not available")


async def latest(db, owner):
    return await db.scalar(
        select(CreatorEditorialTasteRevision)
        .where(CreatorEditorialTasteRevision.user_id == owner)
        .order_by(CreatorEditorialTasteRevision.revision.desc())
        .limit(1)
    )


async def lock_owner(db, owner):
    user = await db.scalar(select(User).where(User.id == owner).with_for_update())
    if user is None:
        raise HTTPException(404, "Account not found")
    return user


async def sources(db, owner, version):
    ids = [uuid.UUID(v) for v in (version.payload or {}).get("example_ids", [])] if version else []
    if not ids:
        return []
    # Recheck the live ownership/quarantine boundary. A deleted project removes
    # its frozen words via FK cascade, including references in older revisions.
    return list(
        (
            await db.scalars(
                select(CreatorEditorialTasteExample)
                .join(PlanItem, PlanItem.id == CreatorEditorialTasteExample.plan_item_id)
                .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
                .where(
                    CreatorEditorialTasteExample.user_id == owner,
                    CreatorEditorialTasteExample.id.in_(ids),
                    ContentPlan.user_id == owner,
                    ContentPlan.ownership_quarantined_at.is_(None),
                )
            )
        ).all()
    )


async def read_profile(db, owner):
    version = await latest(db, owner)
    payload = version.payload if version else {}
    rows = await sources(db, owner, version)
    user = await db.get(User, owner)
    return {
        "revision": version.revision if version else 0,
        "enabled": bool(payload.get("enabled", False)),
        "memory_paused": not bool(user and user.creator_memory_enabled),
        "rules": payload.get("rules", []),
        "examples": [
            {"id": str(row.id), "source_sha256": row.source_sha256, **row.content} for row in rows
        ],
    }


def short(value, maximum):
    return normalize_private_text(str(value or "")).strip()[:maximum]


def context_receipt(context):
    """Opaque provenance only; never duplicate private example words in story history."""
    if not context:
        return {"enabled": False}
    payload = json.loads(context.split("\n", 1)[1])
    return {
        "enabled": True,
        "revision": payload["corpus_revision"],
        "profile_revision_id": payload["profile_revision_id"],
        "context_sha256": digest(context),
        "examples": [
            {"id": value["id"], "source_sha256": value["source_sha256"]}
            for value in payload["examples"]
        ],
        "feedback_ids": [value["id"] for value in payload.get("reviewed_ideas", [])],
    }


async def capture_project(db, owner, project):
    row = (
        await db.execute(
            select(PlanItem, Job)
            .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
            .outerjoin(Job, Job.id == PlanItem.current_job_id)
            .where(
                PlanItem.id == project,
                ContentPlan.user_id == owner,
                ContentPlan.ownership_quarantined_at.is_(None),
            )
        )
    ).first()
    if row is None or (row[1] and row[1].user_id != owner):
        raise HTTPException(404, "Project not found")
    item, job = row
    words = []

    def collect(value):
        if isinstance(value, dict):
            value_text = short(value.get("text"), 800)
            if value_text and value_text not in words and len(words) < 60:
                words.append(value_text)
            for key, field in value.items():
                if key not in {"word_timings", "caption_cues", "overlay_transcript"}:
                    collect(field)
        elif isinstance(value, list):
            for field in value:
                collect(field)

    assembly = job.assembly_plan or {} if job else {}
    variants = [v for v in assembly.get("variants", []) if isinstance(v, dict)]
    for variant in variants:
        collect(variant.get("text_elements") or [])
        if intro := short(variant.get("intro_text"), 800):
            if intro not in words and len(words) < 60:
                words.append(intro)
    slide_post = item.slide_post or {}
    collect(slide_post)
    if caption := short(slide_post.get("caption"), 800):
        if caption not in words and len(words) < 60:
            words.append(caption)
    transcript = short(
        " ".join(
            str(w.get("text") or w.get("word") or "")
            for variant in variants
            for w in (variant.get("overlay_transcript") or [])
            if isinstance(w, dict)
        ),
        20_000,
    )
    title = await db.scalar(
        select(CreationThread.title)
        .where(CreationThread.creator_id == owner, CreationThread.active_plan_item_id == project)
        .limit(1)
    )
    assets = list(
        (
            await db.scalars(
                select(PlanItemAsset)
                .where(PlanItemAsset.plan_item_id == project, PlanItemAsset.user_id == owner)
                .order_by(PlanItemAsset.id)
                .limit(50)
            )
        ).all()
    )
    descriptions = []
    for asset in assets:
        analysis = asset.analysis if isinstance(asset.analysis, dict) else {}
        if description := short(analysis.get("description") or analysis.get("summary"), 800):
            descriptions.append(description)
    if not words and not transcript and not descriptions and item.idea == "Untitled video":
        raise HTTPException(422, "Choose a project with saved content")
    return {
        "project_id": str(project),
        "owner_id": str(owner),
        "title": short(title or item.idea, 200),
        "kind": "slides" if item.edit_format == "slides" else "video",
        "words": words,
        "transcript": transcript,
        "creator_requests": [],
        "source_descriptions": descriptions,
        "artifact": {
            "availability": "saved_metadata_not_pixel_verification",
            "identity": digest(
                {
                    "job_id": str(job.id) if job else None,
                    "generations": [v.get("render_generation_id") for v in variants],
                    "slide_version": slide_post.get("version"),
                }
            ),
        },
    }


async def operation(db, owner, key, request_sha, *, erased_retry=False):
    prior = await db.scalar(
        select(CreatorEditorialTasteRevision).where(
            CreatorEditorialTasteRevision.user_id == owner,
            CreatorEditorialTasteRevision.idempotency_key == key,
        )
    )
    if prior:
        if prior.request_sha256 != request_sha:
            raise HTTPException(409, "Taste request identity reused")
        if prior.payload.get("erased") and not erased_retry:
            raise HTTPException(409, "This preference request was erased; review it again")
    return prior


async def write_profile(db, owner, body):
    await lock_owner(db, owner)
    fingerprint = digest({"operation": "write", **body.model_dump(mode="json")})
    key = "profile:" + digest(body.idempotency_key)
    if await operation(db, owner, key, fingerprint):
        return await read_profile(db, owner)
    current = await latest(db, owner)
    revision = current.revision if current else 0
    if body.expected_revision != revision:
        raise HTTPException(409, "Taste changed; review the current profile")
    references = []
    for project in body.project_ids:
        material = await capture_project(db, owner, project)
        material["creator_editorial_note"] = body.project_notes.get(project, "")
        signature = digest(material)
        row = await db.scalar(
            select(CreatorEditorialTasteExample).where(
                CreatorEditorialTasteExample.user_id == owner,
                CreatorEditorialTasteExample.plan_item_id == project,
                CreatorEditorialTasteExample.source_sha256 == signature,
            )
        )
        if row is None:
            row = CreatorEditorialTasteExample(
                user_id=owner, plan_item_id=project, source_sha256=signature, content=material
            )
            db.add(row)
            await db.flush()
        references.append(str(row.id))
    db.add(
        CreatorEditorialTasteRevision(
            user_id=owner,
            revision=revision + 1,
            idempotency_key=key,
            request_sha256=fingerprint,
            payload={
                "enabled": body.enabled,
                "rules": body.rules,
                "example_ids": references,
                "confirmation": "explicit_authenticated_creator_confirmation",
            },
        )
    )
    await db.flush()
    return await read_profile(db, owner)


async def erase_profile(db, owner, *, key, expected_revision=None, request_sha=None):
    await lock_owner(db, owner)
    fingerprint = request_sha or digest(
        {"operation": "erase", "key": key, "revision": expected_revision}
    )
    if await operation(db, owner, key, fingerprint, erased_retry=True):
        return await read_profile(db, owner)
    current = await latest(db, owner)
    revision = current.revision if current else 0
    if expected_revision is not None and expected_revision != revision:
        raise HTTPException(409, "Taste changed; review the current profile")
    await db.execute(delete(CreatorIdeaFeedback).where(CreatorIdeaFeedback.user_id == owner))
    await db.execute(
        delete(CreatorEditorialTasteExample).where(CreatorEditorialTasteExample.user_id == owner)
    )
    # Retain no old rules or words. Operation identities fence delayed retries.
    await db.execute(
        update(CreatorEditorialTasteRevision)
        .where(CreatorEditorialTasteRevision.user_id == owner)
        .values(payload={"erased": True})
        .execution_options(synchronize_session=False)
    )
    db.add(
        CreatorEditorialTasteRevision(
            user_id=owner,
            revision=revision + 1,
            idempotency_key=key,
            request_sha256=fingerprint,
            payload={"erased": True, "enabled": False},
        )
    )
    await db.flush()
    return await read_profile(db, owner)


async def record_feedback(db, owner, body):
    user = await lock_owner(db, owner)
    if not user.creator_memory_enabled:
        raise HTTPException(409, "Personalization is paused")
    fingerprint = digest(body.model_dump(mode="json"))
    key = "feedback:" + digest(body.client_event_id)
    fence = await operation(db, owner, key, fingerprint)
    prior = await db.scalar(
        select(CreatorIdeaFeedback).where(
            CreatorIdeaFeedback.user_id == owner,
            CreatorIdeaFeedback.client_event_id == body.client_event_id,
        )
    )
    if prior:
        if prior.request_sha256 != fingerprint:
            raise HTTPException(409, "Feedback identity reused")
        return {"id": str(prior.id), "recorded": True, "requires_render_approval": True}
    if fence:
        raise HTTPException(409, "This idea's feedback is no longer available; review a new idea")
    version = await latest(db, owner)
    if not version or not version.payload.get("enabled"):
        raise HTTPException(409, "Enable editorial personalization before saving feedback")
    story = await db.scalar(
        select(MediaCollectionStory)
        .where(
            MediaCollectionStory.id == body.story_id,
            MediaCollectionStory.user_id == owner,
            MediaCollectionStory.collection_id == body.collection_id,
        )
        .with_for_update()
    )
    if story is None:
        raise HTTPException(404, "Idea not found")
    if story.revision != body.expected_story_revision:
        raise HTTPException(409, "Idea changed; review its current wording")
    row = CreatorIdeaFeedback(
        user_id=owner,
        story_id=story.id,
        story_revision=story.revision,
        client_event_id=body.client_event_id,
        request_sha256=fingerprint,
        verdict=body.verdict,
        reasons=body.reasons,
        note=short(body.note, 1000),
        rewrite=short(body.rewrite, 1000),
        proposal={"idea": story.story_json, "moments": story.moments_json},
    )
    db.add(row)
    # Preserve an opaque operation fence when private feedback is erased. A late
    # offline retry must never recreate a forgotten judgment after new consent.
    db.add(
        CreatorEditorialTasteRevision(
            user_id=owner,
            revision=version.revision + 1,
            idempotency_key=key,
            request_sha256=fingerprint,
            payload=dict(version.payload),
        )
    )
    await db.flush()
    return {"id": str(row.id), "recorded": True, "requires_render_approval": True}


async def generation_context(db, owner, query):
    if not settings.creator_editorial_taste_for(owner):
        return ""
    # Fetch a fresh flag: an auth object or a previous identity-map value may be stale.
    paused = await db.scalar(select(User.creator_memory_enabled).where(User.id == owner))
    version = await latest(db, owner)
    if not paused or version is None or not version.payload.get("enabled"):
        return ""
    rows = await sources(db, owner, version)
    corpus = {
        "owner_id": str(owner),
        "revision": version.revision,
        "profile_revision_id": str(version.id),
        "profile": {"status": "approved", "rules": version.payload.get("rules", [])},
        "examples": [
            {**row.content, "verdict": "positive", "source_sha256": row.source_sha256}
            for row in rows
        ],
        "holdout_project_ids": [],
    }
    context = TasteStore.context_for(corpus, str(owner), query)["context"]
    intro, raw = context.split("\n", 1)
    payload = json.loads(raw)
    payload["reviewed_ideas"] = []
    recent = list(
        (
            await db.scalars(
                select(CreatorIdeaFeedback)
                .where(CreatorIdeaFeedback.user_id == owner)
                .order_by(CreatorIdeaFeedback.created_at.desc(), CreatorIdeaFeedback.id)
                .limit(2)
            )
        ).all()
    )
    for row in recent:
        idea = (row.proposal or {}).get("idea") or {}
        item = {
            "id": str(row.id),
            "idea": short(idea.get("title"), 100),
            "angle": short((idea.get("content_plan") or {}).get("angle"), 280),
            "verdict": row.verdict,
            "reasons": row.reasons,
            "note": row.note,
            "creator_alternative": row.rewrite,
            "scope": "This is a contextual judgment, not a universal standing rule.",
        }
        trial = {**payload, "reviewed_ideas": [*payload["reviewed_ideas"], item]}
        if len(intro + "\n" + json.dumps(trial, ensure_ascii=False)) <= MAX_CONTEXT_CHARS:
            payload = trial
    return intro + "\n" + json.dumps(payload, ensure_ascii=False)
