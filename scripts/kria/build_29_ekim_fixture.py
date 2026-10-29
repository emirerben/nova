#!/usr/bin/env python3
"""Rebuild tests/fixtures/kria_replay/29-ekim/fixture.json from read-only prod GETs.

Dump the raw responses first (never commit them), for thread T, plan item P, job J:

    python3 scripts/admin.py --prod GET plan-items/P/debug              > raw/item.json
    python3 scripts/admin.py --prod GET "creation-threads/T/events?limit=200" > raw/events.json
    python3 scripts/admin.py --prod GET "creation-threads/T/turns?limit=50"   > raw/turns.json
    python3 scripts/admin.py --prod GET jobs/J/debug                    > raw/job.json
    python3 scripts/admin.py --prod GET "plan-items/P/proposal-trace?limit=50" > raw/prop.json

then `python3 scripts/kria/build_29_ekim_fixture.py raw/`. Stdlib only. Redacts user ids, storage
paths, signed URLs, library ids, file names and hashes; media ids become clip-NN.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

OUT = (
    Path(__file__).resolve().parents[2]
    / "src/apps/api/tests/fixtures/kria_replay/29-ekim/fixture.json"
)
ROLE_BY_TURN = {0: "T1", 1: "T2", 2: "T3", 3: "T4", 4: "T5", 5: "T6"}
_SIGNED = re.compile(r"https?://\S+|X-Goog-\S+", re.I)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ms(a: str, b: str) -> int:
    return round((_ts(b) - _ts(a)).total_seconds() * 1000)


def main(raw_dir: str) -> None:
    raw = Path(raw_dir)
    load = lambda n: json.loads((raw / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
    events, turns, job, item, prop = (load(n) for n in ("events", "turns", "job", "item", "prop"))
    turns = turns["turns"]
    ap = job["job"]["assembly_plan"]
    ids = [
        m["media_id"]
        for e in events["events"]
        if e["kind"] == "media_added"
        for m in e["payload"]["media"]
    ]
    alias = {mid: f"clip-{i + 1:02d}" for i, mid in enumerate(ids)}
    music_run = next(r for r in prop["agent_runs"] if r["agent_name"] == "nova.audio.music_matcher")
    subjects = {c["clip_id"]: c["subject"] for c in music_run["input_json"]["clip_summaries"]}
    durations = ap["unified_montage"]["clip_durations_s"]
    clips = [
        {
            "id": alias[mid],
            "kind": "video",
            "duration_s": durations[mid],
            "subject": subjects.get(mid, ""),
            "best_part": next(
                (
                    {"source_start_s": p["source_start_s"], "source_end_s": p["source_end_s"]}
                    for p in ap["unified_montage"]["best_parts"]
                    if p["media_id"] == mid
                ),
                None,
            ),
        }
        for mid in ids
    ]

    def scrub(value):  # recursive: aliases, no urls
        text = json.dumps(value, ensure_ascii=False)
        for mid, name in alias.items():
            text = text.replace(mid, name)
        return json.loads(_SIGNED.sub("REDACTED", text))

    by_event = {e["id"]: e for e in events["events"]}
    messages, replies = [], {}
    for i, turn in enumerate(turns):
        src = by_event[turn["source_event_id"]]
        sel = src["payload"].get("choice_selection")
        messages.append(
            {
                "turn": ROLE_BY_TURN[i],
                "turn_id": turn["id"],
                "text": src["content"],
                "choice_selection": {"option_key": sel["option_key"]} if sel else None,
            }
        )
    for e in events["events"]:
        if e["kind"] in {
            "assistant_response",
            "assistant_error",
            "assistant_render_failed",
            "draft_applied",
        } and e["payload"].get("turn_id"):
            cq = e["payload"].get("choice_question")
            replies[e["payload"]["turn_id"]] = {
                "kind": e["kind"],
                "text": e["content"],
                "code": e["payload"].get("code"),
                "choice_options": [
                    {"key": o["key"], "label": o["label"]} for o in (cq or {}).get("options", [])
                ],
                "conflict": (cq or {}).get("conflict"),
            }
    runs = []
    for r in sorted(prop["agent_runs"], key=lambda r: r["created_at"]):
        for i, turn in enumerate(turns):
            if turn["created_at"] <= r["created_at"].replace("Z", "+00:00") <= turn["completed_at"]:
                runs.append(
                    {
                        "turn": ROLE_BY_TURN[i],
                        "agent": r["agent_name"],
                        "model": r["model"],
                        "prompt_version": r["prompt_version"],
                        "outcome": r["outcome"],
                        "attempts": r["attempts"],
                        "tokens_in": r["tokens_in"],
                        "tokens_out": r["tokens_out"],
                        "latency_ms": r["latency_ms"],
                        "error": r["error_message"],
                        "output_keys": (r["output_json"] or {}).get("keys")
                        if r["agent_name"] == "nova.creator.main"
                        else None,
                    }
                )
    timeline = [
        {
            "turn": ROLE_BY_TURN[i],
            "status": t["status"],
            "wall_ms": _ms(t["created_at"], t["completed_at"]),
            "error": t["error"],
        }
        for i, t in enumerate(turns)
    ]
    strategy = dict(turns[3]["plan"]["intents"][0]["arguments"]["strategy"])
    section = dict(strategy.get("selected_music_section") or {})
    section.pop("beat_timestamps_s", None)
    strategy["selected_music_section"] = section
    binding = ap["creator_brief_binding"]["brief"]
    trace = job["job"]["pipeline_trace"]
    failure = next(e for e in trace if e["event"] == "phone_plan_failed")["data"]
    guided = ap["guided_story_execution_plan"]
    fixture = {
        "fixture_id": "29-ekim",
        "schema_version": 1,
        "source": {
            "thread_id": events["thread_id"],
            "plan_item_id": item["item"]["id"],
            "job_id": job["job"]["id"],
            "prod_main_sha": "4e06062dc",
            "captured": "2026-10-29 via read-only admin GETs",
            "redacted": [
                "user ids",
                "storage paths",
                "signed urls",
                "library ids",
                "file names",
                "hashes",
                "media ids (now clip-NN)",
                "music beat timestamps",
            ],
            "model_outputs": "nova.creator.main outputs are stored by prod as key names only; "
            "T4 (recorded strategy) is exact, T1 to T3 are reconstructed from the persisted "
            "ledger and replies, T5 and T6 never returned (provider deadline).",
        },
        "messages": messages,
        "clips": clips,
        "timeline": timeline,
        "agent_runs": runs,
        "replies": replies,
        "brief_ledger_v3": scrub(binding["requirements"]),
        "strategy_t4": scrub(strategy),
        "title_row": scrub(guided["text_elements"][0]),
        "phone_text_style_policy": ap["phone_text_style"],
        "render_failure": {
            "code": failure["failure_reason"],
            "error_class": failure["error_class"],
            "error_tail": failure["error_tail"],
            "field_path": ap["creator_decline"]["field_path"],
            "decline_reason": ap["creator_decline"]["decline_reason"],
        },
        "exact_texts": scrub(ap["creator_render_requirements"]["exact_texts"]),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1])
