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
from app.schemas.research import stable_source_url
from app.services.editorial_taste import MAX_CONTEXT_CHARS, TasteStore, digest, word_tokens
from app.services.library_variant_selection import selected_variant_id
from app.services.private_text import normalize_private_text

FEEDBACK_POOL = 200
JUDGMENTS_PER_SIDE = 2
REJECTED_POOL = 50
# These reasons say the idea was wrong for that footage or those facts, not that the
# angle is bad, so they never count toward a pattern or suppress a similar idea later.
CONTEXTUAL_REASONS = frozenset({"footage_mismatch", "wrong_facts", "invented_experience"})
# Jaccard over 3+ letter words. 0.8 means four of five words shared: a reword that
# adds or swaps one word still matches; a new angle on the same footage does not.
# Texts with fewer than four distinct words only match when they are identical.
# Failure modes: paraphrase with new vocabulary escapes; a short generic opening
# shared by two different ideas can wrongly match (the best idea is always kept).
NEAR_REPEAT = 0.8
NEAR_REPEAT_MIN_WORDS = 4
# Ids live in the 16 kB revision payload beside up to 12 kB of rules; ten stay far inside it.
# Retrieval only ever shows three examples, so older confirmations simply age out.
IDEA_EXAMPLES_KEPT = 10


def signals_enabled():
    """Kill switch for what acts on idea feedback (ranking, repeat filter, wrong-facts)."""
    return settings.creator_editorial_taste_feedback_signals_enabled


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


def idea_example_content(row):
    """What the creator confirmed, frozen from the exact proposal they judged.

    A rewrite is the positive example and carries the rejected angle as context;
    otherwise only a would_post idea qualifies. None means nothing worth keeping.
    """
    idea = (row.proposal or {}).get("idea") or {}
    plan = idea.get("content_plan") or {}
    title = short(idea.get("title"), 200)
    if row.rewrite.strip():
        words = [short(row.rewrite, 800)]
        angle = short(plan.get("angle"), 280)
        note = f"The creator rewrote an idea they did not accept as offered: {angle}"[:1500]
    elif row.verdict == "would_post":
        candidates = [
            idea.get("hook"),
            plan.get("opening_text"),
            plan.get("angle"),
            plan.get("closing_text"),
            plan.get("caption"),
            *(idea.get("on_screen_copy") or []),
        ]
        words = list(dict.fromkeys(v for v in (short(c, 300) for c in candidates) if v))[:12]
        note = ""
    else:
        return None
    if not title or not words:
        return None
    return {
        "project_id": str(row.id),
        "owner_id": str(row.user_id),
        "title": title,
        "kind": "idea",
        "words": words,
        "transcript": "",
        "creator_requests": [],
        "source_descriptions": [],
        "creator_editorial_note": note,
    }


async def idea_sources(db, owner, version):
    payload = version.payload if version else {}
    ids = [uuid.UUID(v) for v in payload.get("idea_example_ids", [])]
    if not ids:
        return []
    rows = (
        await db.scalars(
            select(CreatorIdeaFeedback).where(
                CreatorIdeaFeedback.user_id == owner, CreatorIdeaFeedback.id.in_(ids)
            )
        )
    ).all()
    found = []
    for row in sorted(rows, key=lambda r: ids.index(r.id)):
        if content := idea_example_content(row):
            found.append({**content, "source_sha256": digest(content)})
    return found


async def read_profile(db, owner):
    version = await latest(db, owner)
    payload = version.payload if version else {}
    rows = await sources(db, owner, version)
    ideas = await idea_sources(db, owner, version)
    user = await db.get(User, owner)
    return {
        "revision": version.revision if version else 0,
        "enabled": bool(payload.get("enabled", False)),
        "memory_paused": not bool(user and user.creator_memory_enabled),
        "rules": payload.get("rules", []),
        "examples": [
            {"id": str(row.id), "source_sha256": row.source_sha256, **row.content} for row in rows
        ],
        "idea_examples": [{"id": v["project_id"], **v} for v in ideas],
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
    thread = await db.scalar(
        select(CreationThread)
        .where(CreationThread.creator_id == owner, CreationThread.active_plan_item_id == project)
        .order_by(CreationThread.updated_at.desc(), CreationThread.id.desc())
        .limit(1)
    )
    assembly = (job.assembly_plan or {}) if job else {}
    slide_post = item.slide_post if isinstance(item.slide_post, dict) else {}
    is_slides = item.edit_format == "slides" and bool(slide_post.get("slides"))
    variants = []
    if not is_slides:
        ready = [
            v
            for v in assembly.get("variants", [])
            if isinstance(v, dict) and v.get("render_status") == "ready"
        ]
        selected = selected_variant_id(thread.state) if thread else None
        if thread and thread.active_job_id and (job is None or thread.active_job_id != job.id):
            raise HTTPException(409, "The project changed; open its current finished version first")
        if selected:
            ready = [v for v in ready if v.get("variant_id") == selected]
        if not ready:
            raise HTTPException(422, "Choose a project with a finished video or saved slides")
        if len(ready) > 1:
            raise HTTPException(
                409, "Open the version you like before using this project as an example"
            )
        variants = ready
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

    for variant in variants:
        collect(variant.get("text_elements") or [])
        if intro := short(variant.get("intro_text"), 800):
            if intro not in words and len(words) < 60:
                words.append(intro)
    if is_slides:
        collect(slide_post)
    if is_slides and (caption := short(slide_post.get("caption"), 800)):
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
    title = thread.title if thread else None
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
    execution = assembly.get("guided_story_execution_plan") or {}
    song = execution.get("user_song") if isinstance(execution, dict) else None
    post_form = (
        "slides"
        if is_slides
        else "lipsync"
        if isinstance(song, dict) and song.get("mode") == "lipsync"
        else item.edit_format
        if item.edit_format in {"subtitled", "narrated"}
        else "montage"
    )
    return {
        "project_id": str(project),
        "owner_id": str(owner),
        "title": short(title or item.idea, 200),
        "kind": "slides" if is_slides else "video",
        "post_form": post_form,
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
                    "variant_ids": [v.get("variant_id") for v in variants],
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
    prior_ideas = current.payload.get("idea_example_ids", []) if current else []
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
                "idea_example_ids": prior_ideas,
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


def _delete_key(story_id):
    return f"idea-delete:{story_id}"


async def record_deleted_idea(db, owner, story):
    """Deleting an idea is a `not_for_me` judgment on it, so `rejected_ideas` never re-offers it.

    Written only where `rejected_ideas` would read it back: with personalization off nothing
    is stored beyond the soft-deleted idea itself, which generation excludes on its own.
    """
    if not signals_enabled() or await active_version(db, owner) is None:
        return
    key = _delete_key(story.id)
    if await db.scalar(
        select(CreatorIdeaFeedback.id).where(
            CreatorIdeaFeedback.user_id == owner, CreatorIdeaFeedback.client_event_id == key
        )
    ):
        return
    idea = {k: v for k, v in story.story_json.items() if k != "deleted_at"}
    db.add(
        CreatorIdeaFeedback(
            user_id=owner,
            story_id=story.id,
            story_revision=story.revision,
            client_event_id=key,
            request_sha256=digest({"operation": "idea_delete", "story_id": str(story.id)}),
            verdict="not_for_me",
            reasons=[],
            note="",
            rewrite="",
            proposal={"idea": idea, "moments": story.moments_json},
        )
    )


async def forget_deleted_idea(db, owner, story_id):
    """Undo of `record_deleted_idea`: a restored idea was never refused."""
    await db.execute(
        delete(CreatorIdeaFeedback).where(
            CreatorIdeaFeedback.user_id == owner,
            CreatorIdeaFeedback.client_event_id == _delete_key(story_id),
        )
    )


async def confirm_idea_example(db, owner, feedback_id, body):
    """Explicit second tap: only an already-saved judgment can become an example."""
    user = await lock_owner(db, owner)
    if not user.creator_memory_enabled:
        raise HTTPException(409, "Personalization is paused")
    fingerprint = digest({"operation": "idea_example", "feedback_id": str(feedback_id)})
    key = "idea-example:" + digest(body.client_event_id)
    if await operation(db, owner, key, fingerprint):
        return await read_profile(db, owner)
    version = await latest(db, owner)
    if not version or not version.payload.get("enabled"):
        raise HTTPException(409, "Enable editorial personalization before saving examples")
    row = await db.scalar(
        select(CreatorIdeaFeedback).where(
            CreatorIdeaFeedback.id == feedback_id, CreatorIdeaFeedback.user_id == owner
        )
    )
    if row is None:
        raise HTTPException(404, "Judgment not found")
    if idea_example_content(row) is None:
        raise HTTPException(422, "Only a would-post idea or your own rewrite can be an example")
    confirmed = version.payload.get("idea_example_ids", [])
    if str(row.id) not in confirmed:
        db.add(
            CreatorEditorialTasteRevision(
                user_id=owner,
                revision=version.revision + 1,
                idempotency_key=key,
                request_sha256=fingerprint,
                payload={
                    **version.payload,
                    "idea_example_ids": [*confirmed, str(row.id)][-IDEA_EXAMPLES_KEPT:],
                },
            )
        )
        await db.flush()
    return await read_profile(db, owner)


async def active_version(db, owner):
    if not settings.creator_editorial_taste_for(owner):
        return None
    # Fetch a fresh flag: an auth object or a previous identity-map value may be stale.
    paused = await db.scalar(select(User.creator_memory_enabled).where(User.id == owner))
    version = await latest(db, owner)
    if not paused or version is None or not version.payload.get("enabled"):
        return None
    return version


async def rejected_research(db, owner, topic):
    """Facts and stable sources the creator called wrong on an earlier idea about this topic.

    Fact ids restart at fact-0 on every search, so identity is the stored wording
    (matched loosely, `same_claim`) and stable source address. Forget, pause and the
    feature gate all empty the result.
    """
    if not topic or not signals_enabled() or await active_version(db, owner) is None:
        return None
    wanted = " ".join(topic.casefold().split())
    rows = (
        await db.scalars(
            select(CreatorIdeaFeedback)
            .where(
                CreatorIdeaFeedback.user_id == owner,
                CreatorIdeaFeedback.reasons.contains(["wrong_facts"]),
            )
            .order_by(CreatorIdeaFeedback.created_at.desc(), CreatorIdeaFeedback.id)
            .limit(50)
        )
    ).all()
    texts, urls = set(), set()
    for row in rows:
        research = ((row.proposal or {}).get("idea") or {}).get("research") or {}
        if " ".join(str(research.get("topic", "")).casefold().split()) != wanted:
            continue
        for fact in (research.get("evidence") or {}).get("facts", []):
            if fact.get("text"):
                texts.add(fact["text"])
            urls.update(
                s["url"]
                for s in fact.get("sources", [])
                if s.get("url") and stable_source_url(s["url"])
            )
    if not texts:
        return None
    return {"fact_texts": sorted(texts), "source_urls": sorted(urls)}


async def generation_context(db, owner, query):
    version = await active_version(db, owner)
    if version is None:
        return ""
    rows = await sources(db, owner, version)
    ideas = await idea_sources(db, owner, version)
    corpus = {
        "owner_id": str(owner),
        "revision": version.revision,
        "profile_revision_id": str(version.id),
        "profile": {"status": "approved", "rules": version.payload.get("rules", [])},
        "examples": [
            {**row.content, "verdict": "positive", "source_sha256": row.source_sha256}
            for row in rows
        ]
        + [{**v, "verdict": "positive"} for v in ideas],
        "holdout_project_ids": [],
    }
    context = TasteStore.context_for(corpus, str(owner), query)["context"]
    intro, raw = context.split("\n", 1)
    payload = json.loads(raw)
    payload["reviewed_ideas"] = []
    pool = list(
        (
            await db.scalars(
                select(CreatorIdeaFeedback)
                .where(CreatorIdeaFeedback.user_id == owner)
                .order_by(CreatorIdeaFeedback.created_at.desc(), CreatorIdeaFeedback.id)
                .limit(FEEDBACK_POOL)
            )
        ).all()
    )
    if signals_enabled():
        wanted = word_tokens(query)

        def overlap(row):
            idea = (row.proposal or {}).get("idea") or {}
            plan = idea.get("content_plan") or {}
            words = " ".join(
                str(v or "")
                for v in (idea.get("title"), plan.get("angle"), plan.get("opening_text"))
            )
            return len(wanted & word_tokens(f"{words} {row.note} {row.rewrite}"))

        # sorted() is stable: equal overlap keeps the newest-first order of the pool.
        ranked = sorted(pool, key=lambda row: -overlap(row))
        chosen = [
            *[row for row in ranked if row.verdict == "would_post"][:JUDGMENTS_PER_SIDE],
            *[row for row in ranked if row.verdict != "would_post"][:JUDGMENTS_PER_SIDE],
        ]
    else:
        chosen = pool[:2]  # legacy behavior: the two newest judgments
    for row in chosen:
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


async def rejected_ideas(db, owner, collection_id):
    """Angles and opening lines of ideas the creator refused for this same outing.

    A not_for_me verdict is contextual judgment, not a standing rule, so it only
    filters re-offers for the collection it was given on.
    """
    if not signals_enabled() or await active_version(db, owner) is None:
        return []
    rows = (
        await db.scalars(
            select(CreatorIdeaFeedback)
            .join(MediaCollectionStory, MediaCollectionStory.id == CreatorIdeaFeedback.story_id)
            .where(
                CreatorIdeaFeedback.user_id == owner,
                CreatorIdeaFeedback.verdict == "not_for_me",
                MediaCollectionStory.collection_id == collection_id,
            )
            .order_by(CreatorIdeaFeedback.created_at.desc(), CreatorIdeaFeedback.id)
            .limit(REJECTED_POOL)
        )
    ).all()
    found = []
    for row in rows:
        if CONTEXTUAL_REASONS & set(row.reasons or []):
            continue
        plan = ((row.proposal or {}).get("idea") or {}).get("content_plan") or {}
        found.append(
            {
                "id": str(row.id),
                "texts": [str(plan.get(k) or "") for k in ("angle", "opening_text")],
            }
        )
    return found


def _near_identical(a, b):
    left, right = word_tokens(a), word_tokens(b)
    if not left or not right:
        return False
    if " ".join(a.casefold().split()) == " ".join(b.casefold().split()):
        return True
    union = left | right
    return len(union) >= NEAR_REPEAT_MIN_WORDS and len(left & right) / len(union) >= NEAR_REPEAT


def repeated_rejection(angle, opening, rejected):
    """Feedback id of the first rejected idea this angle or opening nearly repeats."""
    for value in rejected:
        old_angle, old_opening = value["texts"]
        if _near_identical(angle, old_angle) or _near_identical(opening, old_opening):
            return value["id"]
    return None
