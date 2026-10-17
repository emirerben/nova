#!/usr/bin/env python3
"""Fetch a private-safe human export review report with a read-only credential."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

URL = "https://nova-video.fly.dev/request-following-report"
CATEGORIES = {
    "title": "Requested title",
    "labels": "Per-clip labels",
    "captions": "Captions",
    "order": "Clip order",
    "selection": "Included / excluded footage",
    "duration": "Video length",
    "pacing": "Pacing",
    "readability": "Text readability",
    "audio_source": "Requested voice / audio",
    "audio_mix": "Audio mix",
    "style": "Visual style",
    "preservation": "Keep unrelated edits",
    "other": "Other instruction",
}
COUNT_KEYS = (
    "captured_sessions",
    "request_sessions",
    "recent_request_sessions",
    "intent_reviewed_sessions",
    "export_reviewed_sessions",
    "stale_output_reviews",
    "unavailable_output_reviews",
    "invalid_output_reviews",
)
VERDICTS = ("met", "partial", "missed", "unclear", "creator_changed", "total")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def count(value):
    if type(value) is not int or not 0 <= value <= 50000:
        raise ValueError("invalid_count")
    return value


def timestamp(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timezone_required")
    return at.isoformat()


def safe_report(raw):
    """Rebuild from a fixed vocabulary; never copy free-form strings to CI."""
    if (
        raw["schema_version"] != 1
        or raw["window_days"] != 14
        or type(raw["truncated"]) is not bool
    ):
        raise ValueError("unsupported_report")
    generated = timestamp(raw["generated_at"])
    age = (
        datetime.now(timezone.utc) - datetime.fromisoformat(generated)
    ).total_seconds()
    if not -60 <= age <= 3600:
        raise ValueError("stale_report")
    result = {
        "schema_version": 1,
        "generated_at": generated,
        "window_days": 14,
        "truncated": raw["truncated"],
        "counts": {k: count(raw["counts"][k]) for k in COUNT_KEYS},
        "requirements": {k: count(raw["requirements"][k]) for k in VERDICTS},
        "sessions": [],
        "next_actions": [],
    }
    if result["requirements"]["total"] != sum(
        result["requirements"][k] for k in VERDICTS[:-1]
    ):
        raise ValueError("inconsistent_total")
    for row in raw["sessions"]:
        verdicts = {k: count(row["verdicts"].get(k, 0)) for k in VERDICTS[:-1]}
        findings = []
        for finding in row["findings"]:
            if finding["category"] not in CATEGORIES or finding["verdict"] not in {
                "partial",
                "missed",
            }:
                raise ValueError("invalid_finding")
            findings.append(
                {"category": finding["category"], "verdict": finding["verdict"]}
            )
        result["sessions"].append(
            {
                "case_id": str(uuid.UUID(row["case_id"])),
                "activity_at": timestamp(row["activity_at"]),
                "reviewed_at": timestamp(row["reviewed_at"]),
                "verdicts": verdicts,
                "findings": findings,
            }
        )
    for action in raw["next_actions"]:
        if action["category"] not in CATEGORIES:
            raise ValueError("invalid_category")
        result["next_actions"].append(
            {
                "category": action["category"],
                "sessions": count(action["sessions"]),
                "missed_requirements": count(action["missed_requirements"]),
                "case_ids": [str(uuid.UUID(v)) for v in action["case_ids"]],
            }
        )
    return result


def markdown(report):
    c, r = report["counts"], report["requirements"]
    lines = [
        "# Reviewed recent exports",
        "",
        f"Generated: {report['generated_at']} · activity window: 14 days",
        "",
        "Human reviews of selected eligible sessions. This is not a population success rate or a live model evaluation.",
        "",
        f"**{c['export_reviewed_sessions']} exports reviewed** / {c['recent_request_sessions']} recent sessions with requests.",
        f"Captured: {c['captured_sessions']} · with requests: {c['request_sessions']} · intent approved: {c['intent_reviewed_sessions']}",
        "",
        f"Instructions: {r['met']} met · {r['partial']} partial · {r['missed']} missed · {r['unclear']} unclear · {r['creator_changed']} changed by creator (excluded) · {r['total']} total.",
        f"Excluded reviews: {c['stale_output_reviews']} stale · {c['unavailable_output_reviews']} storage unavailable · {c['invalid_output_reviews']} invalid.",
        "",
        "Unreviewed sessions and unavailable exports are not successes. Only current source and object versions count.",
        "",
    ]
    if report["truncated"]:
        lines += ["**Coverage truncated at 1,000 current captures.**", ""]
    lines += ["## What to fix next", ""]
    if not report["next_actions"]:
        lines += [
            "No confirmed misses in the current reviewed sample. Complete more reviews before drawing a quality conclusion.",
            "",
        ]
    for action in report["next_actions"]:
        lines += [
            f"- **{CATEGORIES[action['category']]}**: {action['missed_requirements']} partial/missed instructions in {action['sessions']} sessions. Open these private reviews, reproduce the miss, and add an export regression before changing behavior.",
            "  Cases: " + ", ".join(f"`{v}`" for v in action["case_ids"]),
        ]
    lines += [
        "",
        "## Reviewed evidence",
        "",
        "| Case | Request activity | Review saved | Met / partial / missed / unclear / creator changed |",
        "|---|---|---|---|",
    ]
    for row in report["sessions"]:
        lines.append(
            f"| `{row['case_id']}` | {row['activity_at']} | {row['reviewed_at']} | "
            + " / ".join(str(row["verdicts"][k]) for k in VERDICTS[:-1])
            + " |"
        )
    lines += [
        "",
        "Open a case in the local review app with `/?case=CASE_ID`. Private conversations, notes, media URLs and account identities are omitted from this download.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        token = os.environ.get("REQUEST_FOLLOWING_REPORT_TOKEN", "")
        if not token:
            raise ValueError("missing_report_configuration")
        request = urllib.request.Request(URL, headers={"X-Report-Token": token})
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=90
        ) as response:
            payload = response.read(1_000_001)
        if len(payload) > 1_000_000:
            raise ValueError("oversized_report")
        report = safe_report(json.loads(payload))
        status = 0
        text = markdown(report)
    except Exception:
        # Do not serialize exception messages (could include tokens/URLs/payload).
        report = {"status": "unavailable", "reason": "live_review_report_unavailable"}
        text = "# Reviewed recent exports\n\n**Unavailable.** Check the read-only report credential, API deployment and storage health. No current quality claim is available. Offline results remain separate.\n"
        status = 1
    (args.output / "reviewed-sessions.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    (args.output / "reviewed-sessions.md").write_text(text)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
