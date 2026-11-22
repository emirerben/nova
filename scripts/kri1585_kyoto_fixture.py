#!/usr/bin/env python3
"""Kyoto Torokko informative-text fixture (KRI-1585), production job 3930c822.

    python3 scripts/kri1585_kyoto_fixture.py freeze   # GET-only: rebuild the checked-in JSON
    src/apps/api/.venv/bin/python scripts/kri1585_kyoto_fixture.py cache   # footage + frames

``freeze`` reads the job debug, the plan item's proposal trace and the thread events from
production with ``scripts/admin.py --prod GET`` and writes the no-network fixture under
``src/apps/api/tests/fixtures/kri1585_kyoto/``. The repository is public, so it keeps fields
by allowlist: no owner or thread id, no bucket path, no signed URL, no clip transcript or
people notes. The media digests it records were computed over the unscrubbed paths, so they do
not recompute from the fixture.

``cache`` fetches the edit's analysis proxies through ``replay_job.py --fetch-only`` (GET-only,
owner-prefix guarded), checks each against the sha256 the fixture pins, and writes frames into
the gitignored ``.cache/kri1585-kyoto/``: the pipeline's own ``sample_background`` grid
(108x192 gray) at the anchors ``_ensure_generation_environment`` uses (2/25/50/75/98 % of each
text window, and of each clip for one-line-per-clip designs), plus a full-size PNG of each
anchor. Creator footage never enters git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

JOB_ID = "3930c822-2d44-4251-bcf6-d45ef865c5ef"
REPO = Path(__file__).resolve().parent.parent
API = REPO / "src" / "apps" / "api"
FIXTURE = API / "tests" / "fixtures" / "kri1585_kyoto"
CACHE = REPO / ".cache" / "kri1585-kyoto"
REPLAY_INPUTS = Path("/private/tmp/nova-replay-job") / JOB_ID / "inputs"
ANCHOR_FRACTIONS = (0.02, 0.25, 0.5, 0.75, 0.98)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MEDIA_KEEP = ("kind", "lane", "aspect", "media_id", "duration_s", "generation")
ANALYSIS_KEEP = ("width", "height", "aspect", "subject", "exposure", "duration_s")


def _get(path: str) -> dict:
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "admin.py"), "--prod", "GET", path],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    return json.loads(out.stdout)


def _all_pages(path: str, key: str) -> list[dict]:
    items, cursor = [], None
    while True:
        page = _get(path + (f"?cursor={cursor}" if cursor else ""))
        items.extend(page[key])
        cursor = page.get("next_cursor")
        if not cursor or page.get(f"{key}_has_more") is False:
            return items


def _scrub(value, ids: dict[str, str]):
    """Owner and thread ids become placeholders; signed URL query strings are dropped."""
    if isinstance(value, dict):
        return {k: _scrub(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, ids) for v in value]
    if isinstance(value, str):
        for real, placeholder in ids.items():
            value = value.replace(real, placeholder)
        if "X-Goog-" in value or "Signature=" in value:
            return value.split("?", 1)[0]
    return value


def _media(entry: dict) -> dict:
    kept = {k: entry[k] for k in MEDIA_KEEP if k in entry}
    analysis = entry.get("analysis") or {}
    kept["analysis"] = {k: analysis[k] for k in ANALYSIS_KEEP if k in analysis}
    return kept


def _write(name: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    (FIXTURE / name).write_text(text, encoding="utf-8")


def freeze(args: argparse.Namespace) -> int:
    live = _get(f"jobs/{JOB_ID}/debug")
    # The creator kept editing this job in the iPhone editor after it rendered (11:53 UTC added
    # a hook row and moved row 0). The fixture pins the render as produced, from the job debug
    # pulled before that save; the live rows are kept beside it as the editor's evidence.
    debug = json.loads(Path(args.job_debug).read_text(encoding="utf-8")) if args.job_debug else live
    job = debug["job"]
    item_id = job["content_plan_item_id"]
    thread_id = debug["thread_id"]
    ids = {job["user_id"]: "<owner>", thread_id: "<thread>"}
    plan = job["assembly_plan"]
    variant = plan["variants"][0]
    proposal = dict(plan["guided_edit"]["approved_proposal"])
    proposal["media"] = [_media(m) for m in proposal["media"]]
    for drop in ("output_url", "video_path", "poster_path"):
        variant.pop(drop, None)
    trace = _all_pages(f"plan-items/{item_id}/proposal-trace", "agent_runs")
    events = _all_pages(f"creation-threads/{thread_id}/events", "events")
    requests = [
        {
            "created_at": e["created_at"],
            "content": e["content"],
            "client_protocol": e.get("client_protocol")
            or (e.get("payload") or {}).get("client_protocol"),
        }
        for e in events
        if e.get("kind") == "user_message" and e["created_at"] <= job["created_at"]
    ]
    retrieved_at = variant["text_elements"][0]["source_params"]["research"]["retrieved_at"]
    # The rows' retrieved_at names the research pass that produced them (10:15 UTC, after the
    # creator's second message); the 08:01 pass belongs to the earlier draft.
    pass_start = retrieved_at[:15]
    runs = [
        r
        for r in trace
        if r["created_at"] >= pass_start and r["agent_name"].startswith("nova.research.")
    ]
    facts_run = next(r for r in runs if r["agent_name"] == "nova.research.public_facts")
    raw = json.loads(facts_run["raw_text"])
    cited = {
        row["id"]: [f["id"] for f in row["source_params"]["research"]["facts"]]
        for row in variant["text_elements"]
    }
    research = {
        "public_facts": {
            "created_at": facts_run["created_at"],
            "model": facts_run["model"],
            "prompt_version": facts_run["prompt_version"],
            "input": facts_run["input_json"],
            "retrieved_at": raw.get("retrieved_at"),
            "all_facts": raw["facts"],
            "facts_given_to_writer": [f["id"] for f in facts_run["output_json"]["facts"]],
            "search_entry_html": raw.get("search_entry_html"),
        },
        "caption_binding_attempts": [
            {
                "created_at": r["created_at"],
                "model": r["model"],
                "prompt_version": r["prompt_version"],
                "input": r["input_json"],
                "output": r["output_json"],
            }
            for r in sorted(runs, key=lambda r: r["created_at"])
            if r["agent_name"] == "nova.research.caption_binding"
        ],
        "claim_review": [
            {
                "created_at": r["created_at"],
                "prompt_version": r["prompt_version"],
                "output": r["output_json"],
            }
            for r in runs
            if r["agent_name"] == "nova.research.claim_review"
        ],
        "cited_fact_ids_by_row": cited,
    }
    FIXTURE.mkdir(parents=True, exist_ok=True)
    _write(
        "plan.json",
        _scrub(
            {
                "_source": (
                    f"KRI-1585 Phase 0: prod job {JOB_ID} (Kyoto Torokko, guided_story, rendered "
                    "on iPhone), frozen read-only by scripts/kri1585_kyoto_fixture.py freeze. "
                    "Owner/thread ids replaced, bucket paths and signed URLs kept out, clip "
                    "transcripts and people notes dropped; recorded digests do not recompute."
                ),
                "job": {
                    k: job[k]
                    for k in (
                        "id",
                        "content_plan_item_id",
                        "status",
                        "job_type",
                        "mode",
                        "created_at",
                        "finished_at",
                    )
                },
                "runtime_version": debug["runtime_version"],
                "language": (job.get("all_candidates") or {}).get("language"),
                "creator_messages": requests,
                "creator_route": plan.get("creator_route"),
                "approved_proposal": proposal,
                "variant": {k: v for k, v in variant.items() if k != "text_elements"},
                "unified_montage": {
                    k: plan["unified_montage"].get(k)
                    for k in (
                        "story",
                        "clip_ids",
                        "final_source_cuts",
                        "clip_durations_s",
                        "wants_per_clip_text",
                        "labels",
                        "duration_s",
                    )
                },
            },
            ids,
        ),
    )
    _write("text_rows.json", _scrub({"text_elements": variant["text_elements"]}, ids))
    _write("research.json", _scrub(research, ids))
    live_rows = live["job"]["assembly_plan"]["variants"][0]["text_elements"]
    if live_rows != variant["text_elements"]:
        _write(
            "text_rows_after_editor_save.json",
            _scrub({"updated_at": live["job"]["updated_at"], "text_elements": live_rows}, ids),
        )
    manifest = []
    for entry in variant["story_timeline"]:
        local = REPLAY_INPUTS / "media" / entry["media_id"]
        manifest.append(
            {
                "media_id": entry["media_id"],
                "sha256": hashlib.sha256(local.read_bytes()).hexdigest()
                if local.exists()
                else None,
                "bytes": local.stat().st_size if local.exists() else None,
            }
        )
    _write("media_manifest.json", {"media": manifest})
    leaks = [
        p.name
        for p in FIXTURE.glob("*.json")
        if any(
            s in p.read_text(encoding="utf-8")
            for s in (*ids, "X-Goog-", "Signature=", '"transcript"')
        )
    ]
    if leaks:
        print(f"scrub failed in {leaks}", file=sys.stderr)
        return 1
    print(f"wrote {FIXTURE}")
    return 0


def _anchors(start: float, end: float) -> list[float]:
    return [round(start + (end - start) * f, 3) for f in ANCHOR_FRACTIONS]


def _source_at(timeline: list[dict], at: float) -> tuple[dict, float] | None:
    for clip in timeline:
        if clip["output_start_s"] <= at < clip["output_end_s"] or at == clip["output_end_s"]:
            return clip, clip["source_start_s"] + (at - clip["output_start_s"])
    return None


def cache(_: argparse.Namespace) -> int:
    sys.path.insert(0, str(API))
    from app.pipeline.phone_text_background import HEIGHT, WIDTH, sample_background

    manifest = json.loads((FIXTURE / "media_manifest.json").read_text(encoding="utf-8"))["media"]
    media_dir = CACHE / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    missing = [m for m in manifest if not (media_dir / m["media_id"]).exists()]
    if missing:
        subprocess.run(
            [
                str(API / ".venv" / "bin" / "python"),
                "scripts/replay_job.py",
                JOB_ID,
                "--fetch-only",
            ],
            cwd=API,
            check=True,
        )
        for m in missing:
            shutil.copy2(REPLAY_INPUTS / "media" / m["media_id"], media_dir / m["media_id"])
    for m in manifest:
        digest = hashlib.sha256((media_dir / m["media_id"]).read_bytes()).hexdigest()
        if digest != m["sha256"]:
            print(f"{m['media_id']}: sha256 {digest} != pinned {m['sha256']}", file=sys.stderr)
            return 1
    plan = json.loads((FIXTURE / "plan.json").read_text(encoding="utf-8"))
    rows = json.loads((FIXTURE / "text_rows.json").read_text(encoding="utf-8"))["text_elements"]
    timeline = plan["variant"]["story_timeline"]
    windows = [
        {"window": row["id"], "start_s": row["start_s"], "end_s": row["end_s"]} for row in rows
    ]
    windows += [
        {"window": f"clip-{i}", "start_s": c["output_start_s"], "end_s": c["output_end_s"]}
        for i, c in enumerate(timeline)
    ]
    frames_dir = CACHE / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for window in windows:
        for at in _anchors(window["start_s"], window["end_s"]):
            found = _source_at(timeline, at)
            if found is None:
                continue
            clip, source_s = found
            path = str(media_dir / clip["media_id"])
            stem = f"{window['window']}@{at:.3f}"
            pixels = sample_background(path, source_s, crop=clip.get("source_crop"))
            if pixels is None:
                print(f"{stem}: no frame", file=sys.stderr)
                return 1
            (frames_dir / f"{stem}.gray").write_bytes(pixels)
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(source_s),
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    str(frames_dir / f"{stem}.png"),
                ],
                check=True,
            )
            index.append(
                {
                    **window,
                    "at_s": at,
                    "media_id": clip["media_id"],
                    "source_s": round(source_s, 3),
                    "gray": f"frames/{stem}.gray",
                    "png": f"frames/{stem}.png",
                }
            )
    (CACHE / "frames.json").write_text(
        json.dumps({"grid": [WIDTH, HEIGHT], "frames": index}, indent=1) + "\n", encoding="utf-8"
    )
    print(f"{len(index)} frames in {CACHE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    frozen = sub.add_parser("freeze")
    frozen.add_argument(
        "--job-debug", help="a saved job debug JSON to freeze instead of the live one"
    )
    frozen.set_defaults(run=freeze)
    sub.add_parser("cache").set_defaults(run=cache)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
