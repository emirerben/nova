#!/usr/bin/env python3
"""Rebuild a route replay fixture (tests/fixtures/kria_replay/<route>/fixture.json) from read-only
prod captures (KRI-1097 wave 2). Same scrub rules as `build_assos_fixture.py`.

Dump the raw responses first (never commit them), for thread T and job J:

    python3 scripts/admin.py --prod GET "creation-threads/T/events?limit=200" > raw/events.json
    python3 scripts/admin.py --prod GET "creation-threads/T/turns?limit=50"   > raw/turns.json
    python3 scripts/admin.py --prod GET jobs/J/debug                          > raw/job.json

then

    python3 scripts/kria/build_route_fixture.py raw/ --route talking_head [--source-sha SHA]

The draft is the job's approved turn: its planner strategy and summary, the bound Creative Brief,
the media snapshot's clips (analyses kept), the user messages up to it and the draft card prod
showed. ``coverage.json`` beside the fixture (or ``--coverage``) adds reconstructed fields or asks
the recording lacks, so the fixture makes the planner choose what the route drops, and holds the
fixture's ``checks``; each addition is listed under ``source.reconstructed``.
Stdlib only. Media ids become ``analysis-proxy-<route>-NN``; every other id, path, URL and hash is
scrubbed, and the builder refuses to write a file that still holds a path, a URL or a UUID.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parents[1] / "src/apps/api/tests/fixtures/kria_replay"


def _assos_builder():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(
        "build_assos_fixture", HERE / "build_assos_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ASSOS = _assos_builder()
_URL, _UUID, _check_clean = _ASSOS._URL, _ASSOS._UUID, _ASSOS._check_clean
_ZERO = "00000000-0000-4000-8000-000000000000"
_HASH = re.compile(r"\b[0-9a-f]{32,64}\b")
# Library asset ids embed the asset UUID ("asset-<uuid>").
_ASSET = re.compile(r"asset-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _load(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:  # the admin CLI prints a status line after the JSON
        return json.JSONDecoder().raw_decode(raw)[0]


# Render fields worth keeping per route: what prod's recipe did with the plan (informational).
def _talking_head_outcome(ap: dict) -> dict:
    v = ap["variants"][0]
    framing = v.get("speaker_framing") or {}
    beats = v.get("phone_beat_receipt") or {}
    overlays = v.get("phone_overlay_receipt") or {}
    return {
        "resolved_archetype": v.get("resolved_archetype"),
        "duration_s": v.get("duration_s"),
        "caption_language": v.get("caption_language"),
        "caption_cue_count": len(v.get("caption_cues") or []),
        "opening_cut": {k: (v.get("opening_cut") or {}).get(k) for k in ("status", "reason")},
        "end_cut": {k: (v.get("end_cut") or {}).get(k) for k in ("status", "reason")},
        "speaker_framing": {k: framing.get(k) for k in ("mode", "eligible", "reason")},
        "beats_unplaced": [
            {k: b.get(k) for k in ("beat_id", "trigger", "reason")}
            for b in beats.get("unplaced") or []
        ],
        "overlay_matcher": overlays.get("matcher"),
        "overlays_placed": [
            {k: o.get(k) for k in ("kind", "start_s", "end_s", "reason")}
            for o in overlays.get("placed") or []
        ],
        "text_elements": [
            {k: t.get(k) for k in ("text", "start_s", "end_s", "effect")}
            for t in v.get("text_elements") or []
        ],
        "creator_length_policy": {
            k: (ap.get("creator_length_policy") or {}).get(k)
            for k in ("owner", "check_mode", "requested_duration_s")
        },
    }


def _narrated_outcome(ap: dict) -> dict:
    v = ap["variants"][0]
    return {
        "resolved_archetype": v.get("resolved_archetype"),
        "duration_s": v.get("duration_s"),
        "edit_duration_s": v.get("edit_duration_s"),
        "brand_tail_s": v.get("brand_tail_s"),
        "captions_enabled": v.get("captions_enabled"),
        "voiceover_caption_style": v.get("voiceover_caption_style"),
        "title_elements": [
            {
                "text": t.get("text"),
                "role": t.get("role"),
                "source_params": t.get("source_params"),
                "animation_phases": t.get("animation_phases"),
            }
            for t in v.get("narrated_title_text_elements") or []
        ],
        "editor_draft_present": bool(v.get("editor_draft")),
        "creator_length_policy": {
            k: (ap.get("creator_length_policy") or {}).get(k)
            for k in ("owner", "natural_owner", "check_mode", "requested_duration_s")
        },
        "render_requirements_duration_s": (ap.get("creator_render_requirements") or {}).get(
            "duration_s"
        ),
    }


def _lipsync_outcome(ap: dict) -> dict:
    v = ap["variants"][0]
    plan = ap.get("guided_story_execution_plan") or {}
    song = (ap.get("unified_montage") or {}).get("user_song") or {}
    return {
        "route": (ap.get("creator_route") or {}).get("route"),
        "pace": plan.get("pace"),
        "direction": plan.get("direction"),
        "resolved_duration_s": plan.get("resolved_duration_s"),
        "beat_window_count": len(plan.get("beat_windows") or []),
        "song_windows_s": [
            [p.get("song_start_s"), p.get("song_end_s"), p.get("position_basis")]
            for p in song.get("placed") or []
        ],
        "captions": (ap.get("unified_montage") or {}).get("captions"),
        "editor_caption_meta_enabled": (plan.get("editor_caption_meta") or {}).get("enabled"),
        "typography_font": (plan.get("typography") or {}).get("font"),
        "text_element_count": len(v.get("text_elements") or []),
    }


OUTCOMES = {
    "talking_head": _talking_head_outcome,
    "narrated": _narrated_outcome,
    "lipsync_song": _lipsync_outcome,
}


def build(raw: Path, *, route: str, source_sha: str, coverage: dict | None) -> dict:
    job, turns, events = (
        _load(raw / "job.json"),
        _load(raw / "turns.json"),
        _load(raw / "events.json"),
    )
    ap = job["job"]["assembly_plan"]
    binding = ap["creator_brief_binding"]
    assignments = binding["media_snapshot"]["clip_assignments"]
    turn_id = job["kria_turn_id"]
    turn = next(t for t in turns["turns"] if t["id"] == turn_id)
    arguments = next(
        i["arguments"] for i in turn["plan"]["intents"] if "strategy" in (i.get("arguments") or {})
    )
    alias = {
        row["media_id"]: f"analysis-proxy-{route}-{i + 1:02d}" for i, row in enumerate(assignments)
    }
    assets: dict[str, str] = {}

    def scrub_text(text: str) -> str:
        for mid, name in alias.items():
            text = text.replace(mid, name)
        text = _ASSET.sub(
            lambda m: assets.setdefault(m.group(0), f"asset-synthetic-{len(assets) + 1:02d}"), text
        )
        text = _URL.sub("https://scrubbed.invalid/", text)
        text = _UUID.sub(_ZERO, text)
        return _HASH.sub(lambda m: "h" * len(m.group(0)), text)

    def scrub(value):  # noqa: ANN001, ANN202
        return json.loads(scrub_text(json.dumps(value, ensure_ascii=False)))

    clips = [
        {
            "id": alias[row["media_id"]],
            "kind": row["kind"],
            "duration_s": row["duration_s"],
            "has_audio": row.get("has_audio", True),
            "source_filename": f"clip-{i + 1:02d}.mp4",
            "analysis": scrub(row.get("analysis") or {}),
        }
        for i, row in enumerate(assignments)
    ]
    brief = scrub(binding["brief"])
    for req in brief["requirements"]:
        req["source_turn_id"] = "synthetic-turn"
    strategy = scrub(arguments["strategy"])

    by_seq = sorted(events["events"], key=lambda e: e["sequence"])
    stop = next(
        e["sequence"]
        for e in by_seq
        if e["kind"] == "approval_approved" and e["payload"].get("turn_id") == turn_id
    )
    messages = [
        scrub_text(e.get("content") or "")
        for e in by_seq
        if e["kind"] == "user_message" and e["sequence"] < stop
    ]
    draft = [
        e for e in by_seq if e["kind"] == "draft_applied" and e["payload"].get("turn_id") == turn_id
    ][-1]
    protocol = next(
        (
            e["payload"].get("client_protocol")
            for e in reversed(by_seq)
            if e["kind"] == "user_message" and e["sequence"] < stop
        ),
        job["job"]["all_candidates"].get("creator_client_protocol"),
    )
    prod_outcome = {
        "recorded_from": "job assembly_plan.variants[0] and route fields, thread events",
        "creator_route": (ap.get("creator_route") or {}).get("route"),
        "approval_card": {
            "draft_applied_content": scrub_text(draft.get("content") or ""),
            "changes": draft["payload"].get("changes"),
            "requirement_receipts": [
                {k: r.get(k) for k in ("requirement_id", "status", "verification", "reason")}
                for r in scrub(draft["payload"].get("requirement_receipts") or [])
            ],
        },
        "render": scrub(OUTCOMES[route](ap)),
    }
    reconstructed = [
        "the approval card is assembled offline from the real pure functions the draft turn "
        "calls (scripts/kria/score_route.py approval_card); the DB-bound turn body is not run",
        "phone source bindings: synthetic proxy paths (score_route.media_rows)",
        "capability manifest (score_route.manifest)",
    ]
    if coverage:
        for key, value in (coverage.get("strategy") or {}).items():
            strategy[key] = value
        for req in coverage.get("requirements") or []:
            brief["requirements"].append(req)
        reconstructed += list(coverage.get("notes") or [])
    fixture = {
        "fixture_id": route,
        "route": route,
        "schema_version": 1,
        "source": {
            "kind": "recorded",
            "what": f"Prod Kria thread, approved draft and its render (origin/main {source_sha}).",
            "captured": "2026-11-01 via read-only admin GETs (saved copies, no network now)",
            "recorded": [
                "creator messages up to the approved draft (thread events)",
                "the planner's post-parse strategy and summary (turns plan intents[].arguments)",
                "the Creative Brief bound to the job (assembly_plan.creator_brief_binding)",
                "clip analyses, durations, audio (media_snapshot.clip_assignments)",
                "the draft card prod showed and what the render did (prod_outcome)",
            ],
            "reconstructed": reconstructed,
            "redacted": [
                "user, thread, item, job, turn and draft ids",
                "storage paths and signed URLs",
                "library asset ids (now asset-synthetic-NN)",
                "file names and content hashes",
                f"media ids (now analysis-proxy-{route}-NN)",
            ],
            "see": f"tests/fixtures/kria_replay/{route}/README.md",
        },
        "message": "\n".join(m for m in messages if m),
        "messages": messages,
        "client_protocol": protocol,
        "planner_summary": scrub_text(arguments.get("summary") or ""),
        "brief": brief,
        "strategy": strategy,
        "clips": clips,
        "prod_outcome": prod_outcome,
    }
    if coverage and coverage.get("checks"):
        fixture["checks"] = coverage["checks"]
    return fixture


def main(
    raw_dir: str, *, route: str, out_dir: Path | None, source_sha: str, coverage: Path | None
) -> None:
    out = (out_dir or FIXTURES / route) / "fixture.json"
    coverage = coverage or (out.parent / "coverage.json")
    extra = json.loads(coverage.read_text(encoding="utf-8")) if coverage.is_file() else None
    fixture = build(Path(raw_dir), route=route, source_sha=source_sha, coverage=extra)
    _check_clean("fixture", fixture)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(copy.deepcopy(fixture), ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("raw_dir", help="directory holding events/turns/job .json")
    parser.add_argument("--route", required=True, choices=sorted(OUTCOMES))
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--source-sha", default="ff5f2adbe", help="commit the capture audits")
    parser.add_argument(
        "--coverage",
        type=Path,
        help="reconstructed additions (default: coverage.json beside the fixture)",
    )
    args = parser.parse_args()
    main(
        args.raw_dir,
        route=args.route,
        out_dir=args.out_dir,
        source_sha=args.source_sha,
        coverage=args.coverage,
    )
