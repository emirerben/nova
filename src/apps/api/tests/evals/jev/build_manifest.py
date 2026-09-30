"""Build the deterministic pilot Jev manifest from request-following fixtures.

Run from ``src/apps/api``: ``python -m tests.evals.jev.build_manifest``.
This creates review seeds only; provider predictions and human adjudication are
intentionally never invented.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
FIXTURES = ROOT / "tests/fixtures/request_following"
SCENARIOS = ("good", "flawed", "ambiguous", "non_english")


def _load_fixture_groups() -> dict[str, list[dict[str, Any]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread_path in sorted((FIXTURES / "threads").glob("*.json")):
        thread = json.loads(thread_path.read_text(encoding="utf-8"))
        footage_id = str(thread["footage"])
        footage_path = FIXTURES / "footage" / f"{footage_id}.json"
        footage = json.loads(footage_path.read_text(encoding="utf-8"))
        grouped[footage_id].append({"thread": thread, "footage": footage})
    if len(grouped) < 6:
        raise ValueError("pilot needs at least six footage lineages")
    return dict(grouped)


def _text(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())[:240]
    return fallback


def _case_payload(
    fixture: dict[str, Any], scenario: str, index: int
) -> tuple[dict[str, Any], list[bool], list[bool]]:
    thread = fixture["thread"]
    footage = fixture["footage"]
    turn = (thread.get("turns") or [{}])[0]
    request = _text(turn.get("user_message"), "Tell the creator's story clearly.")
    direction = _text(thread.get("description"), "Follow the creator's requested direction.")
    media = [
        subject for clip in footage.get("clips", []) if (subject := _text(clip.get("subject"), ""))
    ][:12]

    if scenario == "non_english":
        requirement_texts = [
            "Açılışta yaratıcı isteğini açıkça anlat.",
            "Görüntülerdeki ana hikâyeyi Türkçe olarak sürdür.",
        ]
        intro_hook, opening_title = requirement_texts
        required_truth = [True, True]
        unsupported_truth = [False, False]
    elif scenario == "flawed":
        requirement_texts = [request, direction]
        intro_hook = request
        opening_title = f"Unrelated claim {index}: filmed on Mars"
        required_truth = [True, False]
        unsupported_truth = [False, True]
    elif scenario == "ambiguous":
        requirement_texts = [request, direction]
        intro_hook = "Maybe include some of what the creator asked for."
        opening_title = "A general day out"
        required_truth = [False, False]
        unsupported_truth = [True, True]
    else:
        requirement_texts = [request, direction]
        intro_hook, opening_title = requirement_texts
        required_truth = [True, True]
        unsupported_truth = [False, False]

    payload = {
        "brief": {
            "requirements": [
                {"id": f"r{index}_0", "text": requirement_texts[0], "kind": "text"},
                {"id": f"r{index}_1", "text": requirement_texts[1], "kind": "style"},
            ]
        },
        "proposed_script": {
            "intro_hook": intro_hook,
            "opening_title": opening_title,
            "story_structure": [intro_hook, opening_title],
            "shot_labels": [],
        },
        "plan": {
            "direction": "request_following",
            "edit_format": "montage",
            "archetype": None,
            "pacing": None,
            "caption_style": None,
            "audio_strategy": None,
        },
        "media": media,
        "candidate_claims": [
            {"id": "claim_0", "text": intro_hook},
            {"id": "claim_1", "text": opening_title},
        ],
    }
    return payload, required_truth, unsupported_truth


def build() -> list[dict[str, Any]]:
    groups = _load_fixture_groups()
    lineages = sorted(groups)
    split_lineages = {
        "calibration": lineages[:3],
        "held_out": lineages[3:6],
    }
    rows: list[dict[str, Any]] = []
    for index in range(25):
        split = "calibration" if index < 15 else "held_out"
        split_index = index if split == "calibration" else index - 15
        pool = split_lineages[split]
        lineage = pool[split_index % len(pool)]
        fixture_rows = groups[lineage]
        fixture = fixture_rows[(split_index // len(pool)) % len(fixture_rows)]
        scenario = SCENARIOS[index % len(SCENARIOS)]
        payload, required_truth, unsupported_truth = _case_payload(fixture, scenario, index)
        rows.append(
            {
                "case_id": f"jev-pilot-{index:03d}",
                "split": split,
                "provenance": "derived",
                "group_id": f"footage-{lineage}",
                "language": "tr" if scenario == "non_english" else "en",
                "scenario": scenario,
                "payload": payload,
                "required_items": [
                    {"item_id": item["id"], "required": truth}
                    for item, truth in zip(
                        payload["brief"]["requirements"], required_truth, strict=True
                    )
                ],
                "unsupported_claims": [
                    {"claim_id": item["id"], "unsupported": truth}
                    for item, truth in zip(
                        payload["candidate_claims"], unsupported_truth, strict=True
                    )
                ],
                "label_metadata": {
                    "label_source": "derived",
                    "human_adjudicated": False,
                    "notes": (
                        "Deterministic review seed from request-following fixture lineage; "
                        "human adjudication required."
                    ),
                },
            }
        )
    return rows


def main() -> None:
    rows = build()
    path = OUT / "pilot_manifest.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"wrote {path} ({len(rows)} cases, {len(rows) * 4} decisions)")


if __name__ == "__main__":
    main()
