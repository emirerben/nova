"""Saved cards remain readable without indexing, generation or database writes."""

import uuid

import pytest
from sqlalchemy import event

from app.database import AsyncSessionLocal, engine
from app.models import MediaCollectionStory
from tests.integration.test_media_collections import collection_world, selection  # noqa: F401


@pytest.mark.parametrize(
    "case",
    [
        "owned",
        "rejected",
        "wrong-owner",
        "stale-collection",
        "foreign-id",
        "old-snapshot",
        "bounded",
        "too-many",
    ],
)
async def test_saved_cards_are_owned_frozen_read_only_history(collection_world, monkeypatch, case):  # noqa: F811
    client, assets, sources, owner, app = collection_world
    from app.services import media_collection_stories as stories
    from app.services import media_collections, media_moments

    collection_id = uuid.uuid4()
    path = f"/me/media-collections/{collection_id}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    story_id = uuid.uuid4()
    moment = dict(
        id=str(uuid.uuid4()),
        asset_id=assets[0],
        source_id=sources[0],
        generation="42",
        start_s=155,
        end_s=162,
        kind="visual",
        text="Saved harbor view",
        uncertain=True,
        evidence={},
    )
    details = dict(
        title="Saved harbor",
        hook="A crossing",
        payoff="Arrival",
        rationale="An observed journey",
        content_plan=dict(
            version=1,
            format="text_story",
            audience="Friends",
            angle="An actual crossing",
            why_it_works="The ending completes the journey",
            opening_text="Saved  opening",
            closing_text=None,
            caption="Saved  caption.\n\nExact spacing.",
            beat_reasons=["The retained source window"],
        ),
    )
    async with AsyncSessionLocal() as db:
        for index in range(25 if case == "bounded" else 1):
            db.add(
                MediaCollectionStory(
                    id=story_id if index == 0 else uuid.uuid4(),
                    collection_id=collection_id,
                    user_id=owner.id,
                    collection_revision=2 if case == "old-snapshot" else 1,
                    revision=3,
                    story_json=details,
                    moments_json=[moment],
                    events_json=[dict(type="rejection", story_revision=3)]
                    if case == "rejected"
                    else [],
                )
            )
        await db.commit()
    if case == "wrong-owner":
        from app.auth import get_current_user
        from app.models import User

        app.dependency_overrides[get_current_user] = lambda: User(
            id=uuid.uuid4(), email="other@saved.test"
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("Saved history must not index, resolve sources, or generate")

    for module, name in (
        (stories, "generate_stories"),
        (stories, "candidate_evidence"),
        (media_moments, "index_collection"),
        (media_moments, "search_moments"),
        (media_collections, "eligible_sources"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    params = [("expected_revision", 2 if case == "stale-collection" else 1)]
    if case not in {"bounded", "old-snapshot"}:
        params += [("story_ids", str(uuid.uuid4() if case == "foreign-id" else story_id))] * (
            21 if case == "too-many" else 1
        )
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get(f"{path}/stories", params=params)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert all(value == "SELECT" for value in statements), statements
    expected = {
        "wrong-owner": 404,
        "stale-collection": 409,
        "foreign-id": 409,
        "too-many": 422,
    }.get(case, 200)
    assert response.status_code == expected, response.text
    if expected != 200:
        return
    result = response.json()
    assert "coverage" not in result  # No invented claim about current collection coverage.
    assert result["revision"] == 1
    if case == "old-snapshot":
        assert result["stories"] == []
        return
    assert len(result["stories"]) == (20 if case == "bounded" else 1)
    for value in result["stories"]:
        assert all(value["moments"][0][key] == item for key, item in moment.items())
        assert value["content_plan"]["caption"] == details["content_plan"]["caption"]
        assert value["revision"] == 3
        assert value["rejected"] == (case == "rejected")
