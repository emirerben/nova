#!/usr/bin/env python3
"""Read-only measurement of editorial-taste feedback for ONE account (zero model cost).

Reports would_post rate by week, repeat-rejection rate, wrong-facts rate and what
generation_context adds with feedback. Prints counts, enums and hashes only: never
idea words, notes, the query or the raw owner id. Refuses a non-local database
unless --allow-prod is given; the session is always opened READ ONLY.

    src/apps/api/.venv/bin/python scripts/editorial-taste-measure.py \
        --owner <uuid> --query 'food review' [--json] [--simulate-flag-on]
"""

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/apps/api"))

from app.config import settings  # noqa: E402

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
REJECTION_REASONS = {
    "too_generic",
    "forced_question",
    "not_my_voice",
    "invented_experience",
    "footage_mismatch",
    "missing_point",
}
WRONG_FACT_REASONS = {"invented_experience", "footage_mismatch"}
MIN_JUDGMENTS = 30
MIN_WEEK = 5
MIN_ELIGIBLE = 10


def is_local(database_url):
    return (urlparse(database_url).hostname or "") in LOCAL_HOSTS


def ratio(part, whole):
    return round(part / whole, 4) if whole else None


def wilson(part, whole, z=1.96):
    if not whole:
        return None
    p = part / whole
    denom = 1 + z * z / whole
    centre = (p + z * z / (2 * whole)) / denom
    half = z * math.sqrt(p * (1 - p) / whole + z * z / (4 * whole * whole)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def week_start(moment):
    day = moment.astimezone(UTC)
    return day.date() - timedelta(days=day.weekday())


def offered_time(story):
    """When the story was offered: the offered event's `at`, else the row's created_at."""
    offered = next((e for e in story.events_json if e.get("type") == "offered"), {})
    try:
        return datetime.fromisoformat(offered["at"])
    except (KeyError, TypeError, ValueError):
        return story.created_at


def offered_receipt(events):
    offered = next((e for e in events if e.get("type") == "offered"), None)
    return None if offered is None else offered.get("editorial_taste")


def repeat_rejections(feedback, receipts, offered_at):
    """A reason recorded on an earlier rejection that recurs on a later rejection.

    Only rejections (verdict != would_post) with a rejection reason count, and a
    reason in the same row is never "earlier". Each repeat is split by whether the
    story's offered-time receipt shows the earlier judgment reached the prompt. The
    receipt is fixed when the story is offered, so an earlier judgment made after
    that moment could not have been in it: those repeats get their own bucket
    instead of counting as the prompt dropping the reason.
    """
    seen, eligible, repeated = {}, 0, 0
    split = Counter()
    by_reason = Counter()
    for row in feedback:
        if row["verdict"] == "would_post":
            continue
        reasons = set(row["reasons"]) & REJECTION_REASONS
        if not reasons:
            continue
        if seen:
            eligible += 1
            recurring = reasons & seen.keys()
            if recurring:
                repeated += 1
                by_reason.update(recurring)
                receipt = receipts.get(row["story_id"])
                offered = offered_at.get(row["story_id"])
                available = [
                    i
                    for r in recurring
                    for i, judged_at in seen[r]
                    if offered is None or judged_at <= offered
                ]
                if receipt is None:
                    split["context_unknown"] += 1
                elif not available:
                    split["earlier_judgment_after_offer"] += 1
                else:
                    shown = set(receipt.get("feedback_ids") or [])
                    carried = any(str(i) in shown for i in available)
                    split[
                        "context_carried_earlier_reason"
                        if carried
                        else "context_did_not_carry_earlier_reason"
                    ] += 1
        for reason in reasons:
            seen.setdefault(reason, []).append((row["id"], row["at"]))
    return {
        "eligible": eligible,
        "repeated": repeated,
        "rate": ratio(repeated, eligible),
        "wilson_95": wilson(repeated, eligible),
        "context_carried_earlier_reason": split["context_carried_earlier_reason"],
        "context_did_not_carry_earlier_reason": split[
            "context_did_not_carry_earlier_reason"
        ],
        "earlier_judgment_after_offer": split["earlier_judgment_after_offer"],
        "context_unknown": split["context_unknown"],
        "recurring_reasons": dict(sorted(by_reason.items())),
    }


def limiter(judged, weeks, eligible, blocked_by=None):
    if blocked_by:
        return f"blocked ({blocked_by}): the app would not use this account's judgments"
    if not judged:
        return "one account, no judgments recorded: nothing to measure"
    parts = [f"one account, {judged} judgments over {len(weeks)} week(s)"]
    if judged < MIN_JUDGMENTS:
        parts.append(
            f"under {MIN_JUDGMENTS} judgments: rates are anecdotes, see the Wilson bands"
        )
    thin = sum(1 for w in weeks if w["judged"] < MIN_WEEK)
    if thin:
        parts.append(f"{thin} of {len(weeks)} weeks have under {MIN_WEEK} judgments")
    if eligible < MIN_ELIGIBLE:
        parts.append(f"repeat-rejection rests on {eligible} eligible rejections")
    parts.append(
        "no control arm: week-to-week change is confounded with prompt, library and mood; "
        "only up to four judgments (two would_post, two others, ranked by similarity) "
        "enter a prompt"
    )
    return "; ".join(parts)


async def gate_reason(db, owner):
    """Why the app would not use this owner's stored judgments, or None if it would.

    Same three gates generation_context applies: the flag for the owner, the
    creator's personalization pause, and the profile's own enabled switch. The whole
    report fails closed on this, not just the context diff.
    """
    from sqlalchemy import select

    from app.models import User
    from app.services import creator_editorial_taste as taste

    if not settings.creator_editorial_taste_for(owner):
        return "flag_off_for_owner"
    if not await db.scalar(select(User.creator_memory_enabled).where(User.id == owner)):
        return "memory_paused_or_no_account"
    version = await taste.latest(db, owner)
    if version is None or not version.payload.get("enabled"):
        return "profile_not_enabled"
    return None


async def context_diff(db, owner, query, simulate, reason):
    from sqlalchemy import func, select

    from app.models import CreatorIdeaFeedback
    from app.services import creator_editorial_taste as taste
    from app.services.editorial_taste import MAX_CONTEXT_CHARS

    base = {
        "query_sha256": hashlib.sha256(query.encode()).hexdigest()[:12],
        "query_chars": len(query),
        "flag_simulated": simulate,
    }
    context = "" if reason else await taste.generation_context(db, owner, query)
    if not context:
        return {**base, "available": False, "reason": reason or "empty_context"}
    intro, raw = context.split("\n", 1)
    payload = json.loads(raw)
    reviewed = payload.get("reviewed_ideas", [])
    without = (
        intro + "\n" + json.dumps({**payload, "reviewed_ideas": []}, ensure_ascii=False)
    )
    total = await db.scalar(
        select(func.count())
        .select_from(CreatorIdeaFeedback)
        .where(CreatorIdeaFeedback.user_id == owner)
    )
    return {
        **base,
        "available": True,
        "chars_with_feedback": len(context),
        "chars_without_feedback": len(without),
        "chars_added": len(context) - len(without),
        "char_budget": MAX_CONTEXT_CHARS,
        "context_sha256_with": hashlib.sha256(context.encode()).hexdigest()[:12],
        "context_sha256_without": hashlib.sha256(without.encode()).hexdigest()[:12],
        "examples_in_context": len(payload.get("examples", [])),
        "confirmed_rules_in_context": len(payload.get("confirmed_rules", [])),
        "reviewed_ideas_in_context": len(reviewed),
        "feedback_rows_total": total,
        "feedback_rows_not_in_context": total - len(reviewed),
        "reviewed_ideas": [
            {
                "verdict": item["verdict"],
                "reasons": item["reasons"],
                "note_chars": len(item.get("note") or ""),
                "rewrite_chars": len(item.get("creator_alternative") or ""),
            }
            for item in reviewed
        ],
    }


async def measure(owner, query, simulate):
    from sqlalchemy import select, text

    from app.database import AsyncSessionLocal
    from app.models import CreatorIdeaFeedback, MediaCollectionStory

    if simulate:
        settings.creator_editorial_taste_enabled = True
        settings.creator_editorial_taste_user_ids = [str(owner)]
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        gate = await gate_reason(db, owner)
        feedback, stories = [], []
        if gate is None:
            # Columns are chosen so private note/rewrite/proposal text is never loaded.
            feedback = [
                {
                    "id": r.id,
                    "story_id": r.story_id,
                    "verdict": r.verdict,
                    "reasons": r.reasons or [],
                    "at": r.created_at,
                }
                for r in (
                    await db.execute(
                        select(
                            CreatorIdeaFeedback.id,
                            CreatorIdeaFeedback.story_id,
                            CreatorIdeaFeedback.verdict,
                            CreatorIdeaFeedback.reasons,
                            CreatorIdeaFeedback.created_at,
                        )
                        .where(CreatorIdeaFeedback.user_id == owner)
                        .order_by(
                            CreatorIdeaFeedback.created_at, CreatorIdeaFeedback.id
                        )
                    )
                ).all()
            ]
            stories = (
                await db.execute(
                    select(
                        MediaCollectionStory.id,
                        MediaCollectionStory.created_at,
                        MediaCollectionStory.events_json,
                    ).where(MediaCollectionStory.user_id == owner)
                )
            ).all()
        diff = await context_diff(db, owner, query, simulate, gate)
        await db.rollback()

    offered = [
        s for s in stories if any(e.get("type") == "offered" for e in s.events_json)
    ]
    receipts = {s.id: offered_receipt(s.events_json) for s in offered}
    offered_at = {s.id: offered_time(s) for s in offered}
    events = Counter(e.get("type") for s in offered for e in s.events_json)
    per_week = {}
    for s in offered:
        per_week.setdefault(
            week_start(s.created_at), {"offered": 0, "judged": 0, "would_post": 0}
        )
        per_week[week_start(s.created_at)]["offered"] += 1
    for row in feedback:
        bucket = per_week.setdefault(
            week_start(row["at"]), {"offered": 0, "judged": 0, "would_post": 0}
        )
        bucket["judged"] += 1
        bucket["would_post"] += row["verdict"] == "would_post"
    weeks = [
        {
            "week_start": day.isoformat(),
            **values,
            "would_post_rate": ratio(values["would_post"], values["judged"]),
            "wilson_95": wilson(values["would_post"], values["judged"]),
        }
        for day, values in sorted(per_week.items())
    ]
    judged = len(feedback)
    would_post = sum(row["verdict"] == "would_post" for row in feedback)
    rejections = judged - would_post
    wrong = sum(bool(set(row["reasons"]) & WRONG_FACT_REASONS) for row in feedback)
    wrong_rej = sum(
        row["verdict"] != "would_post"
        and bool(set(row["reasons"]) & WRONG_FACT_REASONS)
        for row in feedback
    )
    rates = [w["would_post_rate"] for w in weeks if w["judged"]]
    repeat = repeat_rejections(feedback, receipts, offered_at)
    judged_stories = {row["story_id"] for row in feedback}
    return {
        "owner": hashlib.sha256(str(owner).encode()).hexdigest()[:12],
        "read_only": True,
        "blocked_by": gate,
        "run_count": {
            "judgments": judged,
            "offered_stories": len(offered),
            "weeks_with_judgments": sum(1 for w in weeks if w["judged"]),
        },
        "spread": {
            "weekly_rate_range": [min(rates), max(rates)] if len(rates) > 1 else None,
            "weekly_rate_stdev": round(statistics.pstdev(rates), 4)
            if len(rates) > 1
            else None,
            "would_post_wilson_95": wilson(would_post, judged),
        },
        "feedback": {
            "judged": judged,
            "would_post": would_post,
            "would_post_rate": ratio(would_post, judged),
            "rejections": rejections,
        },
        "would_post_by_week": weeks,
        "offered": {
            "stories": len(offered),
            "stories_with_feedback": len(judged_stories & {s.id for s in offered}),
            "feedback_coverage": ratio(
                len(judged_stories & {s.id for s in offered}), len(offered)
            ),
            "events": dict(sorted(events.items())),
        },
        "repeat_rejection": repeat,
        "wrong_facts": {
            "definition": sorted(WRONG_FACT_REASONS),
            "rows": wrong,
            "rate": ratio(wrong, judged),
            "of_rejections": wrong_rej,
            "rate_of_rejections": ratio(wrong_rej, rejections),
        },
        "context_diff": diff,
        "limiter": limiter(
            judged, [w for w in weeks if w["judged"]], repeat["eligible"], gate
        ),
    }


def render(report):
    out = [
        f"owner {report['owner']} (hashed)  read-only",
        *(
            [f"BLOCKED ({report['blocked_by']}): nothing below was read"]
            if report["blocked_by"]
            else []
        ),
        "runs: {judgments} judgments, {offered_stories} offered stories, "
        "{weeks_with_judgments} week(s) with judgments".format(**report["run_count"]),
        f"spread: {report['spread']}",
        "",
        "would_post by week (judged / offered / rate / 95% band)",
    ]
    for w in report["would_post_by_week"]:
        out.append(
            f"  {w['week_start']}  {w['judged']:>3} / {w['offered']:>3}  "
            f"{w['would_post_rate']}  {w['wilson_95']}"
        )
    f = report["feedback"]
    out.append(f"  overall {f['would_post']}/{f['judged']} = {f['would_post_rate']}")
    r = report["repeat_rejection"]
    out += [
        "",
        f"repeat-rejection: {r['repeated']}/{r['eligible']} = {r['rate']}  band {r['wilson_95']}",
        f"  earlier reason was in the prompt: {r['context_carried_earlier_reason']}  "
        f"not in the prompt: {r['context_did_not_carry_earlier_reason']}  "
        f"judged after the story was offered: {r['earlier_judgment_after_offer']}  "
        f"unknown (legacy story): {r['context_unknown']}",
        f"  recurring reasons: {r['recurring_reasons']}",
    ]
    w = report["wrong_facts"]
    out += [
        "",
        f"wrong facts {w['definition']}: {w['rows']}/{f['judged']} = {w['rate']}; "
        f"of rejections {w['of_rejections']}/{f['rejections']} = {w['rate_of_rejections']}",
        f"offered: {report['offered']}",
        "",
        "context diff",
    ]
    d = report["context_diff"]
    if d["available"]:
        out += [
            f"  chars with feedback {d['chars_with_feedback']} / without "
            f"{d['chars_without_feedback']} (+{d['chars_added']}, budget {d['char_budget']})",
            f"  examples {d['examples_in_context']}, rules {d['confirmed_rules_in_context']}, "
            f"reviewed ideas {d['reviewed_ideas_in_context']} of {d['feedback_rows_total']} "
            f"({d['feedback_rows_not_in_context']} not shown to the model)",
            f"  added items: {d['reviewed_ideas']}",
        ]
    else:
        out.append(f"  unavailable: {d['reason']}")
    out += ["", f"Limiter: {report['limiter']}"]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", required=True, type=uuid.UUID)
    parser.add_argument("--query", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--allow-prod", action="store_true", help="allow a non-local database"
    )
    parser.add_argument(
        "--simulate-flag-on",
        action="store_true",
        help="build the context as if the taste flag were on for this owner (in-process only)",
    )
    args = parser.parse_args()
    if not is_local(settings.database_url) and not args.allow_prod:
        print(
            "Refusing a non-local database. Re-run with --allow-prod (read-only).",
            file=sys.stderr,
        )
        sys.exit(2)
    report = asyncio.run(measure(args.owner, args.query, args.simulate_flag_on))
    print(json.dumps(report, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
