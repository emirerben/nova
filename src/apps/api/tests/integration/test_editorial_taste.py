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


async def test_full_confirmed_project_feedback_keeps_earlier_notes_and_reaches_generation(
    collection_world, monkeypatch
):
    """A full positive proposal must fit alongside earlier scoped judgments.

    Failure catalogue: the old 500-character note ceiling forced dropping prior
    feedback or keeping a full judgment only in the CLI. Preserve both, keep the
    generation context bounded, and erase all words through the existing flow.
    """
    from app.routes import editorial_taste
    from app.services.creator_editorial_taste import generation_context

    client, _assets, _sources, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    ids = await projects(owner)
    earlier = "Earlier exact feedback: this felt too generic. " * 8
    accepted = (
        "Full editorial proposal: Would post. Opening: a clear observation about this outing. "
        "Caption: explain what actually happened. Footage: the walking and wide views. "
        "Reasoning: give the viewer one concrete useful detail. This is a taste judgment, "
        "not factual certification or render approval. "
    ) * 2
    note = (earlier + accepted).strip()
    assert 500 < len(note) <= 1500
    body = {**profile_body(ids), "project_notes": {ids[0]: note}}
    saved = await client.put("/me/editorial-taste", json=body)
    assert saved.status_code == 200, saved.text
    assert (
        next(e for e in saved.json()["examples"] if e["project_id"] == ids[0])[
            "creator_editorial_note"
        ]
        == note
    )
    async with AsyncSessionLocal() as db:
        context = await generation_context(db, owner.id, "My race day")
    payload = json.loads(context.split("\n", 1)[1])
    assert (
        next(e for e in payload["examples"] if e["id"] == ids[0])["contextual_creator_note"] == note
    )
    assert len(context) <= 8000
    oversized = {
        **body,
        "expected_revision": 1,
        "idempotency_key": "oversized-note",
        "project_notes": {ids[0]: "x" * 1501},
    }
    assert (await client.put("/me/editorial-taste", json=oversized)).status_code == 422
    forgotten = await client.request(
        "DELETE",
        "/me/editorial-taste",
        json={"expected_revision": 1, "idempotency_key": "erase-full-note"},
    )
    assert forgotten.status_code == 200
    async with AsyncSessionLocal() as db:
        assert await generation_context(db, owner.id, "My race day") == ""


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


async def test_wildcard_admits_everyone_but_personalizes_only_after_confirmation(
    collection_world, monkeypatch
):  # noqa: F811
    """Failure catalogue (written before the wildcard): "*" must not outrank the kill
    switch, [] must still admit nobody, a literal "*" must not match as a user id
    substring, and admission must not feed an unconfirmed account's footage prompt.
    """
    from app.routes import editorial_taste
    from app.services.creator_editorial_taste import generation_context

    client, _assets, _sources, owner, app = collection_world
    app.include_router(editorial_taste.router)
    route = "/me/editorial-taste"
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [])
    assert (await client.get(route)).status_code == 404

    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", ["*"])
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", False)
    assert (await client.get(route)).status_code == 404

    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    fresh = await client.get(route)
    assert fresh.status_code == 200
    assert fresh.json()["examples"] == []
    async with AsyncSessionLocal() as db:
        assert await generation_context(db, owner.id, "race") == ""
        assert await generation_context(db, uuid.uuid4(), "race") == ""

    ids = await projects(owner)
    unconfirmed = {**profile_body(ids), "enabled": False}
    assert (await client.put(route, json=unconfirmed)).status_code == 200
    async with AsyncSessionLocal() as db:
        assert await generation_context(db, owner.id, "race") == ""
    assert (
        await client.put(route, json=profile_body(ids, revision=1, key="calibrate-2"))
    ).status_code == 200
    async with AsyncSessionLocal() as db:
        assert "38 km" in await generation_context(db, owner.id, "race")


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


async def test_text_free_finished_post_is_valid_and_unselected_words_are_not_examples(
    collection_world, monkeypatch
):
    from app.models import CreationThread
    from app.routes import editorial_taste
    from app.services.creator_editorial_taste import generation_context

    client, _, _, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    project_id = uuid.UUID((await projects(owner))[0])
    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, project_id)
        item.idea = "Untitled video"
        job = await db.get(Job, item.current_job_id)
        job.assembly_plan = {
            "variants": [{"variant_id": "song", "render_status": "ready", "text_elements": []}],
            "guided_story_execution_plan": {"user_song": {"mode": "lipsync"}},
        }
        thread = CreationThread(
            creator_id=owner.id,
            title="A simple lip-sync",
            active_plan_item_id=item.id,
            active_job_id=job.id,
            state={"selected_variant_id": "song"},
        )
        db.add(thread)
        await db.commit()
        job_id, thread_id = job.id, thread.id
    body = {**profile_body([str(project_id)]), "project_notes": {}}
    response = await client.put("/me/editorial-taste", json=body)
    assert response.status_code == 200, response.text
    example = response.json()["examples"][0]
    assert example["words"] == [] and example["post_form"] == "lipsync"
    assert example["title"] == "A simple lip-sync"
    source_hash = example["source_sha256"]
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        job.assembly_plan = {
            **job.assembly_plan,
            "variants": [
                *job.assembly_plan["variants"],
                {
                    "variant_id": "failed",
                    "render_status": "failed",
                    "text_elements": [{"text": "Failed experiment"}],
                },
                {
                    "variant_id": "other",
                    "render_status": "ready",
                    "text_elements": [{"text": "Unselected alternative"}],
                },
            ],
        }
        await db.commit()
    response = await client.put(
        "/me/editorial-taste", json={**body, "expected_revision": 1, "idempotency_key": "second"}
    )
    assert response.status_code == 200, response.text
    example = response.json()["examples"][0]
    assert example["words"] == [] and example["source_sha256"] == source_hash
    async with AsyncSessionLocal() as db:
        context = await generation_context(db, owner.id, "a lip-sync")
        assert '"post_form": "lipsync"' in context
        assert "Unselected alternative" not in context and "Failed experiment" not in context
        thread = await db.get(CreationThread, thread_id)
        thread.state = {}
        await db.commit()
    ambiguous = await client.put(
        "/me/editorial-taste", json={**body, "expected_revision": 2, "idempotency_key": "ambiguous"}
    )
    assert ambiguous.status_code == 409  # Never silently endorse both ready alternatives.
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        thread.state = {"selected_variant_id": "failed"}
        await db.commit()
    stale = await client.put(
        "/me/editorial-taste",
        json={**body, "expected_revision": 2, "idempotency_key": "failed-choice"},
    )
    assert stale.status_code == 422


async def test_named_unfinished_video_is_not_a_finished_positive(collection_world, monkeypatch):
    from app.routes import editorial_taste

    client, _, _, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    ids = await projects(owner)
    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, uuid.UUID(ids[0]))
        job = await db.get(Job, item.current_job_id)
        job.assembly_plan = {
            "variants": [
                {
                    "variant_id": "draft",
                    "render_status": "failed",
                    "text_elements": [{"text": "Never finished this"}],
                }
            ]
        }
        await db.commit()
    response = await client.put("/me/editorial-taste", json=profile_body([ids[0]]))
    assert response.status_code == 422
    profile = (await client.get("/me/editorial-taste")).json()
    assert profile["revision"] == 0 and profile["examples"] == []


async def test_wrong_facts_feedback_excludes_those_facts_from_the_next_research_run(
    collection_world, monkeypatch
):
    """A creator who says an idea's facts are wrong must not see them again.

    Failure catalogue, written first: (1) fact ids restart at fact-0 on every
    search, so excluding by id would drop innocent facts and keep the bad one;
    (2) the proposal cache is keyed by the request, so an unchanged key would
    replay the rejected ideas without a fresh search or review; (3) Forget,
    pause or another topic must not leak the exclusion; (4) every fact rejected
    must fail closed, never fall back to the rejected evidence; (5) an older
    client's payload, and a server that predates the reason, must stay usable.
    """
    from app.agents.camera_roll_stories import CameraRollStoriesAgent
    from app.routes import editorial_taste
    from app.routes.creation_threads import router as creation_router
    from app.schemas.editorial_taste import IdeaFeedbackWrite
    from app.services import creator_editorial_taste
    from app.services import story_idea_research as research
    from app.services.researched_text import GroundedResearchAgent, ResearchEvidence

    client, assets, sources, owner, app = collection_world
    app.include_router(editorial_taste.router)
    app.include_router(creation_router, prefix="/creation-threads")
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    assert (await client.get("/creation-threads/capabilities")).json()["wrong_fact_feedback"]
    ids = await projects(owner)
    assert (await client.put("/me/editorial-taste", json=profile_body(ids))).status_code == 200

    wrong = "The terminal was rebuilt after a fire."
    right = "The ferry line opened in 1923."
    order = [wrong, right]
    searches, agent_facts, reviews = [], [], []

    def search(self, value, *, ctx):
        searches.append(value.topic)
        return ResearchEvidence(
            facts=[
                dict(
                    id=f"fact-{i}",
                    text=text,
                    sources=[dict(title="Archive", url="https://si.edu/shared-page")],
                )
                for i, text in enumerate(order)
            ],
            retrieved_at="2026-10-21T00:00:00+00:00",
            search_entry_html="<div>Recorded provider attribution</div>",
        )

    def ideas(self, value, *, ctx):
        facts = value.public_research.facts
        agent_facts.append([(fact.id, fact.text) for fact in facts])
        return self.parse(
            json.dumps(
                {
                    "stories": [
                        {
                            "title": "A terminal's story",
                            "moment_ids": ["m1"],
                            "research_fact_ids": [facts[0].id],
                            "content_plan": {
                                "audience": "Ferry passengers",
                                "angle": "The terminal's history",
                                "why_it_works": "History gives this crossing a point.",
                                "opening_text": facts[0].text[:90],
                                "caption": "A piece of terminal history.",
                                "beat_reasons": ["Crossing footage supplies atmosphere"],
                            },
                        }
                    ]
                }
            ),
            value,
        )

    def review(self, value, *, ctx):
        reviews.append(type(self).__name__)
        return research.StoryResearchReviewOutput(approved=True, reason="Recorded review")

    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(CameraRollStoriesAgent, "run", ideas)
    monkeypatch.setattr(research.StoryResearchReviewAgent, "run", review)
    monkeypatch.setattr(research.StorySourceReviewAgent, "run", review)

    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    body = {
        "expected_revision": 1,
        "query": "A ferry outing",
        "research_topic": "History of the public ferry terminal",
    }
    first = await client.post(f"{path}/stories", json=body)
    assert first.status_code == 200, first.text
    story = first.json()["stories"][0]
    assert [f["text"] for f in story["research"]["evidence"]["facts"]] == [wrong]

    def feedback(event, reasons, story=story):
        return {
            "collection_id": path.rsplit("/", 1)[1],
            "story_id": story["id"],
            "expected_story_revision": story["revision"],
            "client_event_id": event,
            "verdict": "needs_work",
            "reasons": reasons,
            "note": "",
            "rewrite": "",
        }

    old_client = feedback("old-client", ["too_generic"])
    assert IdeaFeedbackWrite.model_validate(old_client).reasons == ["too_generic"]
    assert (await client.post("/me/editorial-taste/feedback", json=old_client)).status_code == 200
    assert len(searches) == 1

    rated = await client.post(
        "/me/editorial-taste/feedback", json=feedback("wrong-facts", ["wrong_facts"])
    )
    assert rated.status_code == 200, rated.text

    order[:] = [right, wrong]  # Same facts, new positions: ids cannot be the identity.
    searches.clear(), reviews.clear()
    again = await client.post(f"{path}/stories", json=body)
    assert again.status_code == 200, again.text
    assert searches == [body["research_topic"]]
    assert agent_facts[-1] == [("fact-0", right)]
    assert wrong not in json.dumps(again.json())
    assert [f["text"] for f in again.json()["stories"][0]["research"]["evidence"]["facts"]] == [
        right
    ]
    assert again.json()["stories"][0]["id"] != story["id"]
    assert reviews == ["StorySourceReviewAgent", "StoryResearchReviewAgent"]

    other_topic = await client.post(
        f"{path}/stories", json={**body, "research_topic": "A different public topic"}
    )
    assert other_topic.status_code == 200, other_topic.text
    assert agent_facts[-1] == [("fact-0", right), ("fact-1", wrong)]

    order[:] = ["The terminal was rebuilt following a fire."]  # reworded
    ideas_before = len(agent_facts)
    exhausted = await client.post(f"{path}/stories", json={**body, "query": "Only that fact"})
    assert exhausted.status_code == 503 and len(agent_facts) == ideas_before

    async with AsyncSessionLocal() as db:
        topic = body["research_topic"]
        assert await creator_editorial_taste.rejected_research(db, owner.id, topic)
        assert await creator_editorial_taste.rejected_research(db, owner.id, "Another") is None
    revision = (await client.get("/me/editorial-taste")).json()["revision"]
    erased = await client.request(
        "DELETE",
        "/me/editorial-taste",
        json={"expected_revision": revision, "idempotency_key": "forget"},
    )
    assert erased.status_code == 200, erased.text
    async with AsyncSessionLocal() as db:
        assert await creator_editorial_taste.rejected_research(db, owner.id, topic) is None


async def test_rejected_idea_is_not_reoffered_and_relevant_judgments_outrank_recent_ones(
    collection_world, monkeypatch
):
    """Similarity-ranked memory: reuse what the creator liked, never repeat what they refused.

    Failure catalogue, written first: (1) the two newest judgments crowd out an older,
    relevant one; (2) a reworded copy of a rejected idea slips through because the
    prompt is only advisory; (3) the filter empties the result when every idea repeats,
    so the best one must stay and say why; (4) a pause or Forget must silence both
    retrieval and the filter; (5) a different creator's rejections never apply;
    (6) the pattern line and judgments must fit the 8,000 character budget; (7) the
    stored receipt stays opaque ids and hashes. Lexical overlap cannot see paraphrase
    with new vocabulary: that miss is accepted and documented, not hidden.
    """
    from app.agents.camera_roll_stories import (
        CameraRollStoriesAgent,
        CameraRollStoriesOutput,
        StoryIdea,
    )
    from app.models import MediaCollectionStory
    from app.routes import editorial_taste
    from app.services import creator_editorial_taste

    client, assets, sources, owner, app = collection_world
    app.include_router(editorial_taste.router)
    monkeypatch.setattr(settings, "creator_editorial_taste_enabled", True)
    monkeypatch.setattr(settings, "creator_editorial_taste_user_ids", [str(owner.id)])
    ids = await projects(owner)
    assert (await client.put("/me/editorial-taste", json=profile_body(ids))).status_code == 200

    def idea(title, angle, opening):
        return {"title": title, "angle": angle, "opening": opening}

    script = []

    def provider(self, input, *, ctx):
        moments = [v.id for v in input.candidates[:2]]
        return CameraRollStoriesOutput(
            stories=[
                StoryIdea(
                    title=spec["title"],
                    hook="Hook",
                    payoff="Payoff",
                    rationale="Grounded in the footage.",
                    moment_ids=moments,
                    content_plan={
                        "audience": "Travelers",
                        "angle": spec["angle"],
                        "why_it_works": "A specific everyday moment.",
                        "opening_text": spec["opening"],
                        "beat_reasons": ["Shows the journey"] * 2,
                    },
                )
                for spec in script
            ]
        )

    monkeypatch.setattr(CameraRollStoriesAgent, "run", provider)
    path = f"/me/media-collections/{uuid.uuid4()}"
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    collection = path.rsplit("/", 1)[1]
    counter = iter(range(1000))

    async def offer(query, *specs):
        script[:] = list(specs)
        response = await client.post(
            f"{path}/stories", json={"expected_revision": 1, "query": query}
        )
        assert response.status_code == 200, response.text
        return response.json()["stories"]

    async def judge(story, verdict, reasons):
        response = await client.post(
            "/me/editorial-taste/feedback",
            json={
                "collection_id": collection,
                "story_id": story["id"],
                "expected_story_revision": story["revision"],
                "client_event_id": f"event-{next(counter)}",
                "verdict": verdict,
                "reasons": reasons,
                "note": "",
                "rewrite": "",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["id"]

    lighthouse = idea(
        "Harbor lighthouse at dusk",
        "The harbor lighthouse beacon turns on as the fishing boats come home",
        "Lighthouse beacon at dusk",
    )
    sourdough = idea(
        "Sourdough starter",
        "Feeding a sourdough starter every morning before work",
        "Day nine of my sourdough starter",
    )
    marathon = idea(
        "Marathon pacing",
        "Why the first five kilometres of a marathon decide the whole race",
        "Mile five is where marathons are won",
    )
    stories = await offer("memory seed", lighthouse, sourdough, marathon)
    assert len(stories) == 3
    for story in stories:
        await judge(story, "would_post", ["good_observation"])

    async with AsyncSessionLocal() as db:
        context = await creator_editorial_taste.generation_context(
            db, owner.id, "harbor lighthouse beacon boats"
        )
    payload = json.loads(context.split("\n", 1)[1])
    reviewed = [value["idea"] for value in payload["reviewed_ideas"]]
    assert "Harbor lighthouse at dusk" in reviewed and len(reviewed) == 2
    assert reviewed[0] == "Harbor lighthouse at dusk"
    assert payload["feedback_pattern"] == "good_observation x3"
    assert len(context) <= 8000

    refused = idea(
        "A quiet crossing",
        "Why the morning ferry crossing feels quiet before the crowd arrives",
        "The morning ferry before the crowd",
    )
    first = await offer("rejection seed", refused)
    rejected_feedback = await judge(first[0], "not_for_me", ["too_generic"])
    reworded = idea(
        "Quiet ferry morning",
        "Why the morning ferry crossing feels so quiet before the crowd arrives",
        "The morning ferry before the crowd comes",
    )
    fresh = idea(
        "Customs queue",
        "What the customs queue taught me about packing light for a weekend",
        "Pack light or pay for it",
    )
    again = await offer("second pass", reworded, fresh)
    assert [v["title"] for v in again] == ["Customs queue"]

    only_repeat = await offer("third pass", reworded)
    assert [v["title"] for v in only_repeat] == ["Quiet ferry morning"]
    async with AsyncSessionLocal() as db:
        row = await db.get(MediaCollectionStory, uuid.UUID(only_repeat[0]["id"]))
        offered = row.events_json[0]
        assert offered["repeat_of_rejected"] == rejected_feedback
        receipt = json.dumps(offered["editorial_taste"])
        assert "ferry" not in receipt and "harbor" not in receipt.casefold()
        await db.execute(
            update(User).where(User.id == owner.id).values(creator_memory_enabled=False)
        )
        await db.commit()
        assert await creator_editorial_taste.generation_context(db, owner.id, "ferry") == ""
        assert await creator_editorial_taste.rejected_ideas(db, owner.id) == []
    paused = await offer("paused pass", reworded, fresh)
    assert [v["title"] for v in paused] == ["Quiet ferry morning", "Customs queue"]
    async with AsyncSessionLocal() as db:
        assert await creator_editorial_taste.rejected_ideas(db, uuid.uuid4()) == []
