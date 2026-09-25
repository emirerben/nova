"""Rebuild the East Run fixture (KRI-185 fixture #1) from prod, read-only.

    cd src/apps/api && python -m tests.evals.request_following.capture_east_run

Reads two jobs of one plan item through `scripts/admin.py --prod GET` (GET only; the admin
token stays in the repo-root `.env`) and writes:

    tests/fixtures/request_following/footage/east_run.json
    tests/fixtures/request_following/threads/east_run.json

What is recorded is what production actually did: the copilot's *input snapshot and raw model
text* for every chat turn (`agent_run` rows), and the clip timeline / text lanes of the two
renders. The creator's brief is the copilot `utterance`. Nothing is invented, with three
labelled exceptions: `route_rank` (a hand-labelled estimate of geography, marked as an
annotation), the requirement list (distilled from the brief by hand), and the turn-4 message
(the admin API does not expose it).

Media ids are shortened to the first 8 hex chars of the upload UUID; storage paths and user
ids are dropped.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .runner import FOOTAGE_DIR, THREAD_DIR, stamp_baseline

REPO_ROOT = Path(__file__).resolve().parents[6]
ITEM_ID = "a55956c7-9948-4a7f-a840-0e4e1613180e"
JOB_RENDER_1 = "a5d1ef9d-4cf0-4871-a026-687f345d7351"  # guided story, 19.6s, 6 of 11 clips
JOB_RENDER_2 = "bd0b32aa-2601-4ef6-a2d5-864e56edd469"  # phone montage, 15.0s, all 11 clips

# Geographic order along the route Arnavutkoy -> Eminonu, estimated by hand from each clip's
# analysed subject. Only unambiguous subjects are ranked. CONFIRM WITH THE CREATOR.
ROUTE_RANK_BY_SUBJECT = {
    "waterfront promenade and yachts": 1,
    "sea and distant shore": 2,
    "Bosphorus Strait with boats and bridge": 3,
    "ornate palace gate": 4,
    "clock tower": 5,
    "Galata Tower and Istanbul skyline": 6,
}


def _admin_get(path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "admin.py"), "--prod", "GET", path],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return json.loads(proc.stdout)


def _clip_id(media_id: str) -> str:
    match = re.search(r"([0-9A-Fa-f]{8})-[0-9A-Fa-f]{4}-", media_id)
    return (match.group(1) if match else media_id[:8]).upper()


def _plan_clips(variant: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "clip_id": _clip_id(slot["media_id"]),
            "start_s": round(float(slot["output_start_s"]), 3),
            "end_s": round(float(slot["output_end_s"]), 3),
        }
        for slot in variant["story_timeline"]
    ]


def _plan_texts(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": bar["id"],
            "role": "title" if bar["id"] == "guided-title" else "label",
            "text": bar["text"],
            "start_s": round(float(bar["start_s"]), 3),
            "end_s": round(float(bar["end_s"]), 3),
            "font_family": bar.get("font_family"),
        }
        for bar in bars
    ]


def _copilot_runs(*debugs: dict[str, Any]) -> list[dict[str, Any]]:
    runs = [
        run
        for debug in debugs
        for run in debug["agent_runs"]
        if run["agent_name"] == "nova.edit.copilot"
    ]
    return sorted(runs, key=lambda run: run["created_at"])


def _copilot_turn(turn_id: str, run: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "user_message": run["input_json"]["utterance"],
        "engine": "v1_copilot",
        "copilot": {
            "agent_input": run["input_json"],
            "raw_text": run["raw_text"],
            "prompt_version": run["prompt_version"],
            "recorded_at": run["created_at"],
        },
    }


def build() -> tuple[dict[str, Any], dict[str, Any]]:
    item = _admin_get(f"/admin/plan-items/{ITEM_ID}/debug")
    debug_1 = _admin_get(f"/admin/jobs/{JOB_RENDER_1}/debug")
    debug_2 = _admin_get(f"/admin/jobs/{JOB_RENDER_2}/debug")
    variant_1 = debug_1["job"]["assembly_plan"]["variants"][0]
    variant_2 = debug_2["job"]["assembly_plan"]["variants"][0]
    runs = _copilot_runs(debug_1, debug_2)
    by_message = {run["input_json"]["utterance"]: run for run in runs}
    brief = next(m for m in by_message if m.startswith("This was a 20k run"))
    brief_runs = [run for run in runs if run["input_json"]["utterance"] == brief]
    run_recreate = by_message["Create the video again based on my prompt"]
    run_keep15 = by_message["Keep 15 seconds and include everything with faster pacing"]
    run_font = by_message["Never use fraunces again in my edits"]

    subjects: dict[str, str] = {}
    for debug in (debug_1, debug_2):
        for run in debug["agent_runs"]:
            if run["agent_name"].endswith("music_matcher"):
                for clip in run["input_json"]["clip_summaries"]:
                    subjects[_clip_id(clip["clip_id"])] = clip["subject"]

    footage = {
        "footage_id": "east_run",
        "description": (
            "Creator's 20k run along the Bosphorus, Arnavutkoy to Eminonu. 11 phone clips. "
            "Captured read-only from prod plan item a55956c7 (KRI-185). No capture_time / "
            "place facts exist for these clips: the system had none. route_rank is a "
            "hand-labelled estimate and must be confirmed with the creator."
        ),
        "clips": [
            {
                "clip_id": _clip_id(asset["media_id"]),
                "duration_s": round(float(asset["duration_s"]), 3),
                "subject": subjects.get(_clip_id(asset["media_id"]), ""),
                "route_rank": ROUTE_RANK_BY_SUBJECT.get(
                    subjects.get(_clip_id(asset["media_id"]), "")
                ),
            }
            for asset in item["clip_assignments"]
        ],
    }

    start_1 = brief_runs[0]["input_json"]["variant_snapshot"]["text_bars"]
    start_2 = brief_runs[1]["input_json"]["variant_snapshot"]["text_bars"]
    turns = [
        {
            "turn_id": "t0-suggest",
            "user_message": "Suggest an edit.",
            "engine": "recorded",
            "recorded": {
                "plan_after": {
                    "clips": _plan_clips(variant_1),
                    "texts": _plan_texts(start_1),
                    "total_duration_s": variant_1["duration_s"],
                },
                "reply": None,
            },
        },
        _copilot_turn("t1-brief", brief_runs[0]),
        _copilot_turn("t2-recreate", run_recreate),
        _copilot_turn("t3-keep15", run_keep15),
        {
            "turn_id": "t4-replan",
            "user_message": "[re-plan approved; the message text is not exposed by the admin API]",
            "engine": "recorded",
            "recorded": {
                "plan_after": {
                    "clips": _plan_clips(variant_2),
                    "texts": _plan_texts(start_2),
                    "total_duration_s": variant_2["duration_s"],
                },
                "reply": None,
            },
        },
        {
            **_copilot_turn("t5-brief-again", brief_runs[1]),
            # The creator repeated the brief because the re-plan dropped it, so this reply
            # answers those requirements again.
            "addresses": ["labels-per-clip", "route-order", "fast-paced", "time-to-read"],
        },
        _copilot_turn("t6-no-fraunces", run_font),
    ]

    everything_else = ["everything else is unchanged"]
    requirements = [
        {
            "id": "facts-in-edit",
            "kind": "text",
            "scope": "global",
            "request_type": "creator_facts",
            "checker": "text_contains",
            "params": {"terms": ["20k", "Arnavutköy", "Eminönü"], "scope": "any"},
            "source": "This was a 20k run ... I ran from Arnavutkoy to Eminonu",
            "introduced_in_turn": 1,
        },
        {
            "id": "labels-per-clip",
            "kind": "text",
            "scope": "per_clip",
            "request_type": "label_described",
            "checker": "label_coverage",
            "params": {"min_frac": 1.0},
            "source": "Do an edit highlighting the landmarks and locations of each spot.",
            "introduced_in_turn": 1,
            "claim_terms": everything_else,
        },
        {
            "id": "route-order",
            "kind": "order",
            "scope": "global",
            "request_type": "order_route",
            "checker": "order_by_key",
            "params": {"key": "route_rank"},
            "source": "I ran from Arnavutkoy to Eminonu (clips follow the route)",
            "introduced_in_turn": 1,
            "claim_terms": everything_else,
        },
        {
            "id": "fast-paced",
            "kind": "timing",
            "scope": "global",
            "request_type": "pacing",
            "checker": "pacing_max_avg_clip",
            "params": {"max_avg_clip_s": 2.5},
            "source": "Do it fast paced",
            "introduced_in_turn": 1,
            "claim_terms": everything_else,
        },
        {
            "id": "time-to-read",
            "kind": "timing",
            "scope": "per_clip",
            "request_type": "readability",
            "checker": "readability",
            "params": {},
            "source": "leave enough time for the users to read where it was from",
            "introduced_in_turn": 1,
            "claim_terms": everything_else,
        },
        {
            "id": "redo-from-prompt",
            "kind": "select",
            "scope": "global",
            "request_type": "restructure",
            "checker": "restructure_changed",
            "params": {},
            "source": "Create the video again based on my prompt",
            "introduced_in_turn": 2,
        },
        {
            "id": "keep-15s",
            "kind": "timing",
            "scope": "global",
            "request_type": "duration",
            "checker": "duration_within",
            "params": {"target_s": 15.0, "tol_frac": 0.10},
            "source": "Keep 15 seconds",
            "introduced_in_turn": 3,
        },
        {
            "id": "use-every-clip",
            "kind": "select",
            "scope": "global",
            "request_type": "selection",
            "checker": "select_include",
            "params": {"all": True},
            "source": "include everything",
            "introduced_in_turn": 3,
        },
        {
            "id": "no-fraunces",
            "kind": "style",
            "scope": "global",
            "request_type": "style",
            "checker": "font_forbidden",
            "params": {"fonts": ["Fraunces"]},
            "source": "Never use fraunces again in my edits",
            "introduced_in_turn": 6,
        },
    ]
    thread = {
        "fixture_id": "east_run",
        "provenance": "prod_capture",
        "footage": "east_run",
        "description": (
            "KRI-185 as it happened (plan item a55956c7, thread bf8459ae): brief typed after the "
            "first render, three chat turns on render 1, a re-plan that dropped the brief "
            "(phone montage, render 2), the brief again, then a style rule."
        ),
        "turns": turns,
        "requirements": requirements,
    }
    return footage, thread


def main() -> None:
    footage, thread = build()
    FOOTAGE_DIR.mkdir(parents=True, exist_ok=True)
    THREAD_DIR.mkdir(parents=True, exist_ok=True)
    (FOOTAGE_DIR / "east_run.json").write_text(
        json.dumps(footage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    thread_path = THREAD_DIR / "east_run.json"
    thread_path.write_text(
        json.dumps(thread, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    stamp_baseline(thread_path)
    print(f"wrote {FOOTAGE_DIR / 'east_run.json'} and {THREAD_DIR / 'east_run.json'}")


if __name__ == "__main__":
    main()
