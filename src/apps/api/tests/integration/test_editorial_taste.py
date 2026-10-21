# ruff: noqa: F811
"""Owned history -> private taste -> real proposal cache -> feedback/forget.

Failure catalogue was recorded before implementing this HTTP/DB boundary. A
recorded provider response proves plumbing; it cannot prove editorial quality.
"""

import json
import uuid

from sqlalchemy import delete, select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import ContentPlan, Job, Persona, PlanItem, User
from tests.integration.test_media_collections import collection_world, selection  # noqa: F401


async def projects(owner):
    async with AsyncSessionLocal() as db:
        persona = Persona(user_id=owner.id, persona_status="ready", persona={})
        db.add(persona)
        await db.flush()
        plan = ContentPlan(user_id=owner.id, persona_id=persona.id)
        db.add(plan)
        await db.flush()
        video = PlanItem(content_plan_id=plan.id, position=1, idea="My race day")
        slides = PlanItem(
            content_plan_id=plan.id,
            position=2,
            idea="An honest restaurant review",
            edit_format="slides",
            slide_post={
                "version": 1,
                "slides": [{"edits": {"text": {"text": "Soft but too salty — 4/5"}}}],
            },
        )
        db.add_all([video, slides])
        await db.flush()
        job = Job(
            user_id=owner.id,
            raw_storage_path="unused-test-only",
            status="variants_ready",
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "guided_story",
                        "render_status": "ready",
                        "text_elements": [{"text": "I ran 38 km for an empty snack stand"}],
                    }
                ]
            },
        )
        db.add(job)
        await db.flush()
        video.current_job_id = job.id
        await db.commit()
        return [str(video.id), str(slides.id)]


def profile_body(ids, revision=0, key="calibrate-1", enabled=True):
    return {
        "expected_revision": revision,
        "idempotency_key": key,
        "project_ids": ids,
        "rules": ["Specific real observations are good; simple atmosphere posts are also valid."],
        "project_notes": {
            ids[0]: (
                "For this race footage I prefer a practical race-day guide; "
                "other forms remain valid."
            )
        },
        "confirmed": True,
        "enabled": enabled,
    }


def record_provider(monkeypatch):
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )

    calls = []

    def ideas(self, input, *, ctx):
        calls.append(input)
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title="A quiet crossing",
                    hook="The morning ferry",
                    payoff="An arrival",
                    rationale="A small atmosphere post grounded in the crossing.",
                    moment_ids=[v.id for v in input.candidates[:2]],
                    content_plan={
                        "audience": "Travelers",
                        "angle": "A quiet crossing",
                        "why_it_works": "A specific everyday moment; it does not need a question.",
                        "opening_text": "The morning ferry",
                        "beat_reasons": ["Shows the journey"] * 2,
                    },
                )
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    return calls


async def test_confirmed_profile_reaches_proposals_and_forget_fences_retries(
    collection_world, monkeypatch
):  # noqa: F811
    from app.routes import editorial_taste

    client, assets, sources, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    ids = await projects(owner)
    body = profile_body(ids)
    route = "/me/editorial-taste"
    empty = await client.get(route)
    assert empty.status_code == 200 and empty.json()["revision"] == 0
    saved = await client.put(route, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1 and len(saved.json()["examples"]) == 2
    assert "38 km" in json.dumps(saved.json())
    assert (await client.put(route, json=body)).json()["revision"] == 1
    assert (await client.put(route, json={**body, "rules": ["Changed"]})).status_code == 409
    assert (await client.put(route, json=profile_body(ids, key="stale"))).status_code == 409

    calls = record_provider(monkeypatch)
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    request = {"expected_revision": 1, "query": "An honest travel idea from this outing"}
    response = await client.post(f"{path}/stories", json=request)
    assert response.status_code == 200, response.text
    offered = response.json()["stories"][0]
    context = calls[-1].editorial_taste_context
    assert "38 km" in context and "Soft but too salty" in context
    assert "Specific real observations" in context
    assert "For this race footage" in context
    from app.models import MediaCollectionStory

    async with AsyncSessionLocal() as db:
        story = await db.get(MediaCollectionStory, uuid.UUID(offered["id"]))
        receipt = story.events_json[0]["editorial_taste"]
        assert receipt["revision"] == 1 and len(receipt["examples"]) == 2
        assert "38 km" not in json.dumps(receipt)
    assert "private corpus" not in response.text.casefold()
    await client.post(f"{path}/stories", json={**request, "cache_only": True})
    assert len(calls) == 1

    feedback = {
        "collection_id": path.rsplit("/", 1)[1],
        "story_id": offered["id"],
        "expected_story_revision": offered["revision"],
        "client_event_id": "judgment-1",
        "verdict": "not_for_me",
        "reasons": ["too_generic"],
        "note": "Make the observation more specific.",
        "rewrite": "A real moment from the crossing",
    }
    assert (
        await client.post(
            f"{route}/feedback",
            json={**feedback, "expected_story_revision": offered["revision"] + 1},
        )
    ).status_code == 409
    rated = await client.post(f"{route}/feedback", json=feedback)
    assert rated.status_code == 200, rated.text
    assert (await client.post(f"{route}/feedback", json=feedback)).json()["id"] == rated.json()[
        "id"
    ]
    assert (
        await client.post(f"{route}/feedback", json={**feedback, "note": "Different"})
    ).status_code == 409
    fresh = await client.post(f"{path}/stories", json=request)
    assert fresh.status_code == 200 and len(calls) == 2
    assert "Make the observation more specific" in calls[-1].editorial_taste_context
    assert fresh.json()["stories"][0]["id"] != offered["id"]

    paused = await client.put(route, json=profile_body(ids, revision=2, key="pause", enabled=False))
    assert paused.status_code == 200
    await client.post(f"{path}/stories", json=request)
    assert calls[-1].editorial_taste_context == ""
    erased = await client.request(
        "DELETE", route, json={"expected_revision": 3, "idempotency_key": "forget"}
    )
    assert erased.status_code == 200, erased.text
    assert erased.json()["examples"] == [] and erased.json()["rules"] == []
    assert (
        await client.put(route, json=body)
    ).status_code == 409  # Old retry cannot revive memory.
    assert (
        await client.put(route, json=profile_body(ids, revision=4, key="new-consent"))
    ).status_code == 200
    assert (await client.post(f"{route}/feedback", json=feedback)).status_code == 409
    # Clear again before checking that all historical bodies are scrubbed.
    assert (
        await client.request(
            "DELETE", route, json={"expected_revision": 5, "idempotency_key": "forget-again"}
        )
    ).status_code == 200
    from app.models import (
        CreatorEditorialTasteExample,
        CreatorEditorialTasteRevision,
        CreatorIdeaFeedback,
    )

    async with AsyncSessionLocal() as db:
        assert not (
            await db.scalars(
                select(CreatorEditorialTasteExample).where(
                    CreatorEditorialTasteExample.user_id == owner.id
                )
            )
        ).all()
        assert not (
            await db.scalars(
                select(CreatorIdeaFeedback).where(CreatorIdeaFeedback.user_id == owner.id)
            )
        ).all()
        versions = (
            await db.scalars(
                select(CreatorEditorialTasteRevision).where(
                    CreatorEditorialTasteRevision.user_id == owner.id
                )
            )
        ).all()
        assert "Soft but too salty" not in json.dumps([v.payload for v in versions])


async def test_owner_source_deletion_account_pause_and_feature_gate(collection_world, monkeypatch):  # noqa: F811
    from app.routes import editorial_taste

    client, assets, _, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    ids = await projects(owner)
    route = "/me/editorial-taste"
    from app.models import MediaAsset

    async with AsyncSessionLocal() as db:
        other = await db.get(User, (await db.get(MediaAsset, uuid.UUID(assets[-1]))).user_id)
    oversized = {**profile_body(ids), "rules": ["🎥" * 500] * 8}
    assert (await client.put(route, json=oversized)).status_code == 422
    foreign = await projects(other)
    assert (await client.put(route, json=profile_body(foreign))).status_code == 404
    assert (await client.put(route, json=profile_body([str(uuid.uuid4())]))).status_code == 404
    assert (
        await client.put(route, json={**profile_body(ids), "confirmed": False})
    ).status_code == 422
    assert (
        await client.put(route, json={**profile_body(ids), "owner_id": str(uuid.uuid4())})
    ).status_code == 422
    assert (await client.put(route, json=profile_body(ids))).status_code == 200
    from app.services.creator_editorial_taste import generation_context

    async with AsyncSessionLocal() as db:
        await db.execute(delete(PlanItem).where(PlanItem.id == uuid.UUID(ids[1])))
        await db.commit()
    current = (await client.get(route)).json()
    assert len(current["examples"]) == 1
    async with AsyncSessionLocal() as db:
        before = await generation_context(db, owner.id, "race")
        assert "38 km" in before and "Soft but too salty" not in before
        await db.execute(
            update(User).where(User.id == owner.id).values(creator_memory_enabled=False)
        )
        await db.commit()
        assert await generation_context(db, owner.id, "race") == ""
        assert await generation_context(db, uuid.uuid4(), "race") == ""
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [])
    assert (await client.get(route)).status_code == 404
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", False)
    assert (await client.get(route)).status_code == 404


async def test_pausing_while_provider_runs_does_not_publish_stale_taste(
    collection_world, monkeypatch
):  # noqa: F811
    from app.agents.camera_roll_stories import CameraRollStoriesAgent
    from app.database import sync_session
    from app.routes import editorial_taste

    client, assets, sources, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    ids = await projects(owner)
    assert (await client.put("/me/editorial-taste", json=profile_body(ids))).status_code == 200
    record_provider(monkeypatch)
    original = CameraRollStoriesAgent.run
    owner_id = owner.id

    def pause(self, input, *, ctx):
        result = original(self, input, ctx=ctx)
        with sync_session() as db:
            db.execute(update(User).where(User.id == owner_id).values(creator_memory_enabled=False))
            db.commit()
        return result

    monkeypatch.setattr(CameraRollStoriesAgent, "run", pause)
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    response = await client.post(
        f"{path}/stories", json={"expected_revision": 1, "query": "A real outing"}
    )
    assert response.status_code == 409
    from app.models import MediaCollectionStory

    async with AsyncSessionLocal() as db:
        assert not (
            await db.scalars(
                select(MediaCollectionStory).where(
                    MediaCollectionStory.collection_id == uuid.UUID(path.rsplit("/", 1)[1])
                )
            )
        ).all()


async def test_account_clear_erases_taste_with_rollout_off_and_retry_preserves_new_consent(
    collection_world, monkeypatch
):  # noqa: F811
    from app.routes import editorial_taste, memory

    client, _, _, owner, app = collection_world
    app.include_router(editorial_taste.router)
    app.include_router(memory.router, prefix="/me")
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    monkeypatch.setattr(settings, "creator_memory_enabled", True)
    ids = await projects(owner)
    route = "/me/editorial-taste"
    assert (await client.put(route, json=profile_body(ids))).status_code == 200
    async with AsyncSessionLocal() as db:
        revision = await db.scalar(select(User.creator_memory_revision).where(User.id == owner.id))
    clear = {"expected_revision": revision, "idempotency_key": "clear-everything"}
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", False)
    cleared = await client.post("/me/memory/clear", json=clear)
    assert cleared.status_code == 200, cleared.text
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    profile = (await client.get(route)).json()
    assert profile["examples"] == [] and profile["rules"] == []
    assert (
        await client.put(
            route, json=profile_body(ids, revision=profile["revision"], key="consent-again")
        )
    ).status_code == 200
    assert (await client.post("/me/memory/clear", json=clear)).status_code == 200
    assert len((await client.get(route)).json()["examples"]) == 2
