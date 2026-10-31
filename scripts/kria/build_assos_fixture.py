#!/usr/bin/env python3
"""Rebuild tests/fixtures/kria_replay/assos/{fixture,research}.json from read-only prod captures.

Dump the raw responses first (never commit them), for thread T, plan item P, job J:

    python3 scripts/admin.py --prod GET plan-items/P/debug                    > raw/item.json
    python3 scripts/admin.py --prod GET "creation-threads/T/events?limit=200" > raw/events.json
    python3 scripts/admin.py --prod GET "creation-threads/T/turns?limit=50"   > raw/turns.json
    python3 scripts/admin.py --prod GET jobs/J/debug                          > raw/job.json

then `python3 scripts/kria/build_assos_fixture.py raw/`. Stdlib only. Scrubs user ids, storage
paths, library ids, file names, content hashes and signed or redirect URLs; media ids become
analysis-proxy-assos-NN. Fixtures are JSON only (no media). The builder refuses to write a file
that still contains a path, a URL or a UUID.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[2] / "src/apps/api/tests/fixtures/kria_replay/assos"
SOURCE_SHA = "9c5a453b0"
_URL = re.compile(r"https?://[^\s\"'\\)<>]+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"


def _ids(job: dict) -> list[str]:
    return [
        row["media_id"]
        for row in job["job"]["assembly_plan"]["creator_brief_binding"]["media_snapshot"][
            "clip_assignments"
        ]
    ]


def build(raw: Path) -> tuple[dict, dict]:
    load = lambda n: json.loads((raw / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
    job, turns, events = load("job"), load("turns"), load("events")
    ap = job["job"]["assembly_plan"]
    binding = ap["creator_brief_binding"]
    assignments = binding["media_snapshot"]["clip_assignments"]
    alias = {mid: f"analysis-proxy-assos-{i + 1:02d}" for i, mid in enumerate(_ids(job))}
    redirect: dict[str, str] = {}
    turn1, turn2 = turns["turns"]
    by_seq = {e["sequence"]: e for e in events["events"]}

    def run(name: str, index: int = 0) -> dict:
        return [r for r in job["agent_runs"] if r["agent_name"] == name][index]

    def scrub_text(text: str) -> str:
        for mid, name in alias.items():
            text = text.replace(mid, name)

        def sub(match: re.Match) -> str:
            url = match.group(0)
            if url.startswith(_REDIRECT):
                return _REDIRECT + redirect.setdefault(url, f"SCRUBBED-{len(redirect) + 1:03d}")
            return "https://scrubbed.invalid/"

        return _UUID.sub("00000000-0000-4000-8000-000000000000", _URL.sub(sub, text))

    def scrub(value):  # noqa: ANN001, ANN202
        return json.loads(scrub_text(json.dumps(value, ensure_ascii=False)))

    # ---- clips: full recorded analyses, synthetic identity
    clips = []
    for index, row in enumerate(assignments):
        original = row["upload_contract"]["proxy"]["original"]
        proxy = row["upload_contract"]["proxy"]
        clips.append(
            {
                "id": alias[row["media_id"]],
                "kind": row["kind"],
                "duration_s": row["duration_s"],
                "has_audio": row["has_audio"],
                "source_filename": f"clip-{index + 1:02d}.mp4",
                "analysis": scrub(row["analysis"]),
                "original": {
                    "width": original["width"],
                    "height": original["height"],
                    "orientation_degrees": original["orientation_degrees"],
                    "duration_s": original["duration_s"],
                    "has_audio": original["has_audio"],
                    "byte_count": original["byte_count"],
                    "sha256": f"{index + 1:x}".rjust(64, "a"),
                },
                "proxy": {
                    "width": proxy["width"],
                    "height": proxy["height"],
                    "frame_rate": proxy["frame_rate"],
                    "duration_s": proxy["duration_s"],
                },
            }
        )

    strategy = scrub(job["job"]["all_candidates"]["creator_strategy"])
    section = strategy["selected_music_section"]
    section["track_id"] = "synthetic-catalog-track"
    brief = scrub(binding["brief"])
    for req in brief["requirements"]:
        req["source_turn_id"] = "synthetic-turn-1"
    # Prod ran planner prompt v105 and recorded facts={}; v106 records an unnamed animation ask
    # as facts.animation "any" (the model leg is not replayed offline).
    brief["requirements"][0]["facts"] = {"animation": "any"}

    # ---- recorded prod outcome (what the unchanged code produced)
    variant = ap["variants"][0]
    plan = ap["guided_story_execution_plan"]
    draft = by_seq[21]
    approval = by_seq[22]
    look_block = next(b for b in by_seq[27]["payload"]["blocks"] if b["section_id"] == "look")
    clips_block = next(b for b in by_seq[27]["payload"]["blocks"] if b["section_id"] == "clips")
    followup = by_seq[32]
    suggester = run("nova.chat.suggester", 1)
    copilot_t2 = run("nova.edit.copilot", 1)
    prod_outcome = {
        "recorded_from": "job assembly_plan.variants[0], guided_story_execution_plan, events",
        "recipe": {
            "duration_s": plan["resolved_duration_s"],
            "cut_lengths_s": [round(w["resolved_duration_s"], 4) for w in plan["beat_windows"]],
            "direction": plan["direction"],
            "pace": plan["pace"],
            "output_orientation": plan["output_orientation"],
            "output_orientation_reason": plan["output_orientation_reason"],
            "typography_font_in_plan": plan["typography"]["font"],
            "music_track_id": variant["music_track_id"],
            "music_playback_mode": variant["music_playback_mode"],
            "source_audio_preserved": variant["source_audio_preserved"],
            "exported_duration_s": variant["duration_s"],
            "editor_save_duration_s": variant["edit_duration_s"],
            "brand_tail_s": variant["brand_tail_s"],
            "burned_font": variant["phone_text_style"]["selected_fonts"]["title"],
            "text_elements": [
                {
                    "text": t["text"],
                    "start_s": t["start_s"],
                    "end_s": t["end_s"],
                    "effect": t["effect"],
                    "animation_phases": t.get("animation_phases"),
                    "font_family": t["font_family"],
                }
                for t in variant["text_elements"]
            ],
        },
        "approval_card": {
            "draft_applied_content": draft["content"],
            "changes": draft["payload"]["changes"],
            "requirement_receipts": scrub(draft["payload"]["requirement_receipts"]),
            "consequence_summary": approval["payload"]["consequence_summary"],
            "cost_summary": approval["payload"]["cost_summary"],
            "research_preview_present": "research_preview" in draft["payload"],
        },
        "plan_blocks": {
            "look_chips": look_block["payload"]["chips"],
            "clips_summary": clips_block["summary"],
        },
        "length_owners": {
            "planner_target_duration_s": strategy["target_duration_s"],
            "first_generation": {"owner": "song", "requested_duration_s": None},
            "after_editor_save": {"owner": "creator", "requested_duration_s": 17.0},
        },
        "export_verification": {
            "status": "rejected",
            "reason": "copy_words_missing on authored-text-2",
            "advisory_only": True,
        },
        "turn2": {
            "user_message": by_seq[31]["content"] or "Animate the title",
            "chip_kind": by_seq[31]["payload"]["suggestion"]["kind"],
            "reply": followup["content"],
            "brief_coverage": followup["payload"]["brief_coverage"],
            "copilot_run": {
                "agent_name": copilot_t2["agent_name"],
                "outcome": copilot_t2["outcome"],
                "attempts": copilot_t2["attempts"],
                "tokens_in": copilot_t2["tokens_in"],
                "error_message": copilot_t2["error_message"],
            },
            "suggester_chip_label": next(
                row["label_template"]
                for row in suggester["output_json"]["ranked"]
                if row["label_template"] == "Animate the title"
            ),
        },
    }

    evidence = run("nova.research.public_facts")["output_json"]
    research = {
        "fixture_id": "assos-research",
        "schema_version": 1,
        "source": {
            "recorded": [
                "nova.research.public_facts output (facts, retrieved_at, search_entry_html)",
                "nova.research.caption_binding raw_text (model answer, verbatim)",
                "nova.research.claim_review raw_text (model answer, verbatim)",
            ],
            "reconstructed": [
                "nova.edit.copilot (creation text composer) raw_text: prod redacts output; "
                "only the research_request branch is rebuilt (topic and quote are the recorded "
                "public_facts input)",
                "nova.compose.phone_typography raw_text: prod redacts output; rebuilt from the "
                "recorded phone_text_style.selected_fonts.title",
            ],
            "scrubbed": [
                "grounding-api-redirect URLs (token path replaced by SCRUBBED-n; host and "
                "provider title kept, the institution filter reads them)",
                "media ids -> analysis-proxy-assos-NN",
            ],
        },
        "public_facts_input": run("nova.research.public_facts")["input_json"],
        "evidence": scrub(evidence),
        "caption_binding_raw_text": scrub_text(run("nova.research.caption_binding")["raw_text"]),
        "claim_review_raw_text": scrub_text(run("nova.research.claim_review")["raw_text"]),
        "composer_raw_text": json.dumps(
            {
                "intent": "edit",
                "confidence": 0.9,
                "reply": "I will research Assos and write short sourced explanations.",
                "ops": [],
                "needs_clarification": False,
                "unmet_requests": [],
                "research_request": run("nova.research.public_facts")["input_json"],
            }
        ),
        "typography_raw_text": json.dumps(
            {"fonts": {"title": variant["phone_text_style"]["selected_fonts"]["title"]}}
        ),
    }
    session = turn1["plan"]["intents"][0]["arguments"]
    fixture = {
        "fixture_id": "assos",
        "schema_version": 1,
        "source": {
            "what": (
                "Prod Kria thread 'Why Visit Assos, Turkey? (Animated)': one creation turn and "
                "one chip follow-up. Code under audit: origin/main " + SOURCE_SHA + "."
            ),
            "captured": "2026-10-31 via read-only admin GETs (saved copies, no network now)",
            "recorded": [
                "creator message and Creative Brief v1 (exact, except r1 facts.animation: "
                "see reconstructed)",
                "post-parse strategy of the planner (turns plan intents[0].arguments.strategy), "
                "including the frozen song section",
                "16 clip analyses (job assembly_plan.creator_brief_binding.media_snapshot)",
                "approval card text, requirement receipts, plan-block look chips, render_shape",
                "the recipe prod produced (cut lengths, text effects, fonts, length owners)",
                "turn 2 chip text, assistant reply, brief_coverage and the copilot run outcome",
            ],
            "reconstructed": [
                'Creative Brief v1 r1 facts.animation = "any": prod ran planner prompt v105 and '
                "recorded facts={}; planner prompt v106 (prompt fidelity, animation) records an "
                "unnamed animation ask there. The model leg is not replayed offline; the "
                "structured round trip is pinned by tests/kria/test_text_animation_ask.py",
                "nova.creator.main raw_text: prod redacts model I/O; the parsed strategy is "
                "recorded exactly, but the raw model JSON is not replayed through "
                "MainCreatorAgent.parse",
                "phone source bindings: synthetic proxy paths, synthetic content hashes, and "
                "sizes from the recorded upload contract",
                "capability manifest and the approval request body (render_shape echo vs pick "
                "is inferred from the iOS code, not recorded)",
                "the approval card is assembled from the real pure functions "
                "(_strategy_changes, reply_from_receipts); the DB-bound turn body is not run",
            ],
            "redacted": [
                "user ids",
                "storage paths",
                "signed URLs",
                "library ids",
                "file names",
                "content hashes",
                "media ids (now analysis-proxy-assos-NN)",
                "thread, item, job and turn ids",
            ],
            "see": "tests/fixtures/kria_replay/assos/README.md",
        },
        "message": "With animations, explain why people should visit Assos, Turkey",
        "client_protocol": 25,
        "brief": brief,
        "planner_summary": session["summary"],
        "strategy": strategy,
        "render_shape": {"output_orientation": "landscape", "landscape_fit": "fill"},
        "phone_text_style_policy": scrub(ap["phone_text_style"]),
        "clips": clips,
        "prod_outcome": scrub(prod_outcome),
    }
    return fixture, research


def _check_clean(name: str, value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False)
    bad = [
        pattern.pattern
        for pattern in (re.compile(r"users/"), re.compile(r"gs://"), re.compile(r"X-Goog"))
        if pattern.search(text)
    ]
    urls = {u for u in _URL.findall(text) if "SCRUBBED" not in u and "scrubbed.invalid" not in u}
    urls -= {"http://www.w3.org/2000/svg"}
    uuids = {u for u in _UUID.findall(text) if u != "00000000-0000-4000-8000-000000000000"}
    if bad or urls or uuids:
        raise SystemExit(f"{name} is not clean: {bad} {sorted(urls)[:3]} {sorted(uuids)[:3]}")


def main(raw_dir: str) -> None:
    fixture, research = build(Path(raw_dir))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, value in (("fixture", fixture), ("research", research)):
        _check_clean(name, value)
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(copy.deepcopy(value), ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
