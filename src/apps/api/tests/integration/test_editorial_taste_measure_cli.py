"""Seeded real-Postgres journey for scripts/editorial-taste-measure.py.

Failure catalogue, written before the script:
- reads another account's rows, or counts a would_post that has negative reasons;
- prints a raw owner id, idea words, notes or the query (only hashes/counts/enums);
- runs against a non-local database without the explicit flag, or writes anything;
- counts a reason in the same row as "earlier", or lets an unreasoned row recur;
- crashes or divides by zero for an owner with no feedback / flag off;
- reports a creator's history when the app would not use it (flag off, personalization
  paused, profile off): the whole report must fail closed, not just the context diff;
- calls a repeat "the prompt dropped the reason" when the earlier judgment was made
  after the later story was already offered (the receipt cannot contain it);
- hides that this is one small account (limiter, run count and spread must print).
"""

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, update

from app.database import AsyncSessionLocal
from app.models import (
    CreatorEditorialTasteRevision,
    CreatorIdeaFeedback,
    MediaCollection,
    MediaCollectionStory,
    User,
)

SCRIPT = Path(__file__).resolve().parents[5] / "scripts" / "editorial-taste-measure.py"
WEEK1 = datetime(2026, 10, 5, 12, tzinfo=UTC)
WEEK2 = datetime(2026, 10, 12, 12, tzinfo=UTC)
SECRET = "SECRETWORDXYZ"

# (key, week start, hour offset, verdict, reasons, receipt feedback keys or None=legacy)
ROWS = [
    ("f1", WEEK1, 0, "needs_work", ["too_generic"], []),
    ("f2", WEEK1, 1, "not_for_me", ["too_generic", "invented_experience"], None),
    ("f3", WEEK1, 2, "would_post", ["good_observation"], ["f1", "f2"]),
    ("f4", WEEK2, 0, "needs_work", ["too_generic"], ["f2", "f3"]),
    ("f5", WEEK2, 1, "not_for_me", ["footage_mismatch"], ["f3", "f4"]),
    ("f6", WEEK2, 2, "needs_work", ["footage_mismatch"], []),
    ("f7", WEEK2, 3, "would_post", [], ["f5", "f6"]),
]


def story(owner, collection, at, receipt_ids, ids):
    offered = {"type": "offered", "story_revision": 1}
    if receipt_ids is not None:
        offered["editorial_taste"] = {
            "enabled": True,
            "feedback_ids": [str(ids[k]) for k in receipt_ids],
        }
    return MediaCollectionStory(
        id=uuid.uuid4(),
        collection_id=collection,
        user_id=owner,
        collection_revision=1,
        story_json={"title": SECRET, "content_plan": {"angle": SECRET}},
        moments_json=[],
        events_json=[offered],
        created_at=at,
    )


async def seed(owner, rows, *, with_profile=True):
    ids = {key: uuid.uuid4() for key, *_ in rows}
    async with AsyncSessionLocal() as db:
        db.add(User(id=owner, email=f"{owner}@measure.test"))
        await db.flush()
        collection = MediaCollection(user_id=owner, name="c", revision=1)
        db.add(collection)
        await db.flush()
        if with_profile:
            db.add(
                CreatorEditorialTasteRevision(
                    user_id=owner,
                    revision=1,
                    idempotency_key="profile:seed",
                    request_sha256="0" * 64,
                    payload={"enabled": True, "rules": [SECRET], "example_ids": []},
                )
            )
        for key, week, hour, verdict, reasons, receipt in rows:
            at = week + timedelta(hours=hour)
            row = story(owner, collection.id, at, receipt, ids)
            db.add(row)
            await db.flush()
            db.add(
                CreatorIdeaFeedback(
                    id=ids[key],
                    user_id=owner,
                    story_id=row.id,
                    story_revision=1,
                    client_event_id=key,
                    request_sha256="0" * 64,
                    verdict=verdict,
                    reasons=reasons,
                    note=SECRET,
                    rewrite=SECRET,
                    proposal={"idea": {"title": SECRET}, "moments": []},
                    created_at=at + timedelta(minutes=5),
                )
            )
        for hour in (5, 6):
            db.add(story(owner, collection.id, WEEK2 + timedelta(hours=hour), [], ids))
        await db.commit()


async def cleanup(*owners):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id.in_(owners)))
        await db.commit()


def run(*args, env_extra=None, db_url=None):
    import os

    env = {**os.environ, **(env_extra or {})}
    if db_url:
        env["DATABASE_URL"] = db_url
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env
    )


async def test_measures_one_account_without_leaking_or_crossing_accounts():
    owner, other = uuid.uuid4(), uuid.uuid4()
    await seed(owner, ROWS)
    await seed(other, [(f"o{i}", WEEK1, i, "not_for_me", ["too_generic"], []) for i in range(5)])
    try:
        flag = {
            "CREATOR_EDITORIAL_TASTE_ENABLED": "true",
            "CREATOR_EDITORIAL_TASTE_USER_IDS": json.dumps([str(owner)]),
        }
        done = run("--owner", str(owner), "--query", SECRET, "--json", env_extra=flag)
        assert done.returncode == 0, done.stderr
        report = json.loads(done.stdout)

        assert report["feedback"]["judged"] == 7
        assert report["feedback"]["would_post"] == 2
        weeks = {w["week_start"]: w for w in report["would_post_by_week"]}
        assert (weeks["2026-10-05"]["judged"], weeks["2026-10-05"]["would_post"]) == (3, 1)
        assert (weeks["2026-10-12"]["judged"], weeks["2026-10-12"]["would_post"]) == (4, 1)
        assert (weeks["2026-10-05"]["offered"], weeks["2026-10-12"]["offered"]) == (3, 6)
        assert report["offered"]["stories"] == 9

        repeat = report["repeat_rejection"]
        assert (repeat["eligible"], repeat["repeated"]) == (4, 3)
        assert repeat["rate"] == 0.75
        assert repeat["context_carried_earlier_reason"] == 1
        assert repeat["context_did_not_carry_earlier_reason"] == 1
        assert repeat["context_unknown"] == 1
        assert repeat["earlier_judgment_after_offer"] == 0

        wrong = report["wrong_facts"]
        assert (wrong["rows"], wrong["of_rejections"]) == (3, 3)
        assert wrong["rate_of_rejections"] == 0.6

        diff = report["context_diff"]
        assert diff["available"] is True
        assert diff["reviewed_ideas_in_context"] == 4
        assert diff["feedback_rows_not_in_context"] == 3
        assert diff["chars_with_feedback"] > diff["chars_without_feedback"]

        assert report["owner"] == report["owner"].lower() and len(report["owner"]) == 12
        assert report["run_count"]["judgments"] == 7
        assert report["spread"]["weekly_rate_range"] == [0.25, 0.3333]
        assert "one account" in report["limiter"] and "7 judgments" in report["limiter"]
        assert report["read_only"] is True

        text = run("--owner", str(owner), "--query", SECRET, env_extra=flag)
        for output in (done.stdout, text.stdout, done.stderr, text.stderr):
            assert str(owner) not in output
            assert SECRET not in output
        assert "Limiter" in text.stdout
    finally:
        await cleanup(owner, other)


async def test_empty_or_flag_off_owner_reports_instead_of_crashing():
    owner = uuid.uuid4()
    await seed(owner, [], with_profile=True)
    try:
        done = run("--owner", str(owner), "--query", "x", "--json")
        assert done.returncode == 0, done.stderr
        report = json.loads(done.stdout)
        assert report["feedback"]["judged"] == 0
        assert report["feedback"]["would_post_rate"] is None
        assert report["repeat_rejection"]["rate"] is None
        assert report["context_diff"]["available"] is False
        assert report["context_diff"]["reason"] == "flag_off_for_owner"
        assert report["limiter"].startswith("blocked")

        simulated = run("--owner", str(owner), "--query", "x", "--json", "--simulate-flag-on")
        sim = json.loads(simulated.stdout)
        assert sim["context_diff"]["available"] is True
        assert "no judgments" in sim["limiter"]
    finally:
        await cleanup(owner)


def test_refuses_non_local_database_without_explicit_flag():
    owner = str(uuid.uuid4())
    done = run("--owner", owner, db_url="postgresql://nova@remote-db.invalid:5432/nova")
    assert done.returncode == 2
    assert "--allow-prod" in done.stderr
    assert "remote-db" not in done.stderr


async def test_earlier_judgment_made_after_offer_is_not_blamed_on_the_prompt():
    owner = uuid.uuid4()
    # B is offered at 2:01, A (offered 2:00) is judged at 2:05, B is judged at 2:06.
    rows = [
        ("a", WEEK1, 2, "needs_work", ["too_generic"], []),
        ("b", WEEK1, 2 + 1 / 60, "needs_work", ["too_generic"], []),
        ("c", WEEK1, 4, "needs_work", ["too_generic"], []),
    ]
    await seed(owner, rows)
    try:
        flag = {
            "CREATOR_EDITORIAL_TASTE_ENABLED": "true",
            "CREATOR_EDITORIAL_TASTE_USER_IDS": json.dumps([str(owner)]),
        }
        done = run("--owner", str(owner), "--json", env_extra=flag)
        repeat = json.loads(done.stdout)["repeat_rejection"]
        assert (repeat["eligible"], repeat["repeated"]) == (2, 2)
        assert repeat["earlier_judgment_after_offer"] == 1
        assert repeat["context_did_not_carry_earlier_reason"] == 1
        assert repeat["context_carried_earlier_reason"] == 0
    finally:
        await cleanup(owner)


async def test_report_fails_closed_when_the_app_would_not_use_the_judgments():
    owner = uuid.uuid4()
    await seed(owner, ROWS)
    try:
        flag = {
            "CREATOR_EDITORIAL_TASTE_ENABLED": "true",
            "CREATOR_EDITORIAL_TASTE_USER_IDS": json.dumps([str(owner)]),
        }
        off = json.loads(run("--owner", str(owner), "--json").stdout)
        assert off["blocked_by"] == "flag_off_for_owner"

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(User).where(User.id == owner).values(creator_memory_enabled=False)
            )
            await db.commit()
        paused = json.loads(run("--owner", str(owner), "--json", env_extra=flag).stdout)
        assert paused["blocked_by"] == "memory_paused_or_no_account"

        for report in (off, paused):
            assert report["feedback"]["judged"] == 0
            assert report["would_post_by_week"] == []
            assert report["offered"]["stories"] == 0
            assert report["repeat_rejection"]["rate"] is None
            assert report["wrong_facts"]["rate"] is None
            assert report["context_diff"]["available"] is False
            assert report["limiter"].startswith("blocked")
        text = run("--owner", str(owner), env_extra=flag).stdout
        assert "BLOCKED" in text
    finally:
        await cleanup(owner)
