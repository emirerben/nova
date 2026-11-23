"""KRI-1563 ask-ledger cases: one recorded or reconstructed chat-edit turn per row.

Each row of ``tests/fixtures/kria_replay/kri1563_ask_ledger/fixture.json`` is a turn on an
existing phone Talking draft: the requirements the brief extractor wrote, the ops the copilot
returned (taken from the production record where there is one) and what the reply must and
must not say. ``run_case`` drives the REAL chain with no model: the editor compiler, the diff,
the brief checkers, the ask ledger and the reply builder. Only the model's outputs are given.

Used by ``tests/kria/test_kri1563_ask_ledger.py`` (CI) and by the replay scorecard
``scripts/kria/score_kri1563_ask_ledger.py`` (``make kria-replay FIXTURE=kri1563_ask_ledger``).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.kria.ask_ledger import AskLedger, editor_ledger, no_op_ledger, no_op_reply
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_editor_payload
from app.kria.editor_receipts import reply_from_editor_receipts
from app.kria.reply_language import reply_language_for
from app.services.kria_editor_ops import compile_editor_ops

FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "kria_replay" / "kri1563_ask_ledger" / "fixture.json"
)

# Text rows of the plain (non-Talking) harness: a title and a label, as the KRI-558 suite uses.
TEXT_ROWS = [
    {
        "id": "title-1",
        "text": "Renk Kavgası",
        "start_s": 0.0,
        "end_s": 3.0,
        "role": "title",
        "font_family": "Playfair Display",
        "size_px": 64,
        "color": "#FFFFFF",
        "effect": "static",
        "position": "top",
    },
    {
        "id": "label-1",
        "text": "Part 1",
        "start_s": 1.0,
        "end_s": 9.0,
        "role": "generative_intro",
        "font_family": "Playfair Display",
        "size_px": 52,
        "color": "#FFFFFF",
        "effect": "static",
        "position": "custom",
        "alignment": "left",
        "x_frac": 0.0856,
        "y_frac": 0.91,
    },
]


def load_cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    reply: str
    ledger: AskLedger
    receipts: dict[str, Any]


def _reqs(case: dict[str, Any]) -> list[BriefRequirement]:
    return [
        BriefRequirement(
            id=row["id"],
            kind=row["kind"],
            scope=row.get("scope", "global"),
            description=row["description"],
            literal=row.get("literal"),
            facts=row.get("facts") or {},
        )
        for row in case["requirements"]
    ]


def _talking(monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]) -> tuple[Any, dict, dict]:
    from app.services.kria_editor_ops import project_editor_draft
    from tests.routes.test_speaker_background_chat_ops import _job, _open_windows
    from tests.routes.test_speaker_background_editor import PHOTO, VIDEO

    job = _job(monkeypatch)
    _open_windows(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    base = project_editor_draft(variant, {}, job)
    for key, value in (case.get("variant") or {}).items():
        base[key] = copy.deepcopy(value)
    ids = {"PHOTO": PHOTO.media_id, "VIDEO": VIDEO.media_id}
    kinds = {"PHOTO": "image", "VIDEO": "video"}
    if case.get("start_background"):
        start = case["start_background"]
        base["speaker_background"] = {
            "media_id": ids[start["fill"]],
            "kind": kinds[start["fill"]],
            "source_start_s": 0.0,
            **({"speaker_frame": start["speaker_frame"]} if start.get("speaker_frame") else {}),
        }
    return job, base, {"ids": ids, "kinds": kinds}


def _resolve_ops(ops: list[dict[str, Any]], names: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for op in ops:
        op = copy.deepcopy(op)
        if op.get("op") == "set_background_sequence":
            op["kinds"] = [names["kinds"][a] for a in op["asset_ids"]]
            op["asset_ids"] = [names["ids"][a] for a in op["asset_ids"]]
            op.setdefault("cadence_s", 4.0)
        out.append(op)
    return out


def run_case(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> CaseResult:
    language = case.get("language", "en")
    reqs = _reqs(case)
    with reply_language_for(language):
        if not case.get("ops"):
            outcome = case["copilot"]
            ledger = no_op_ledger(
                reqs,
                reply=outcome["reply"],
                asks_question=outcome.get("outcome") == "clarification",
                unmet=outcome.get("unmet_requests") or (),
            )
            reply = no_op_reply(reqs, ledger, question=outcome["reply"])
            return CaseResult(
                case["id"], reply, ledger, {r.requirement_id: r for r in ledger.receipts}
            )
        if case.get("harness") == "talking":
            job, variant, names = _talking(monkeypatch, case)
            ops = _resolve_ops(case["ops"], names)
        else:
            job = SimpleNamespace(all_candidates={"clip_paths": []}, assembly_plan={}, id="job")
            variant = {
                "variant_id": "v",
                "text_elements": copy.deepcopy(TEXT_ROWS),
                **copy.deepcopy(case.get("variant") or {}),
            }
            ops = copy.deepcopy(case["ops"])
        compiled = compile_editor_ops(job, variant, ops)
        payload = compiled.payload.model_dump(mode="json", exclude_none=True)
        facts = plan_facts_from_editor_payload(
            payload, compiled.text_diff or None, compiled.changes
        )
        facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
        receipts = build_receipts(reqs, facts, include_unchecked=True)
        unmet = case.get("unmet_requests") or []
        rows = facts.text_styles or ()
        ledger = editor_ledger(reqs, receipts, compiled.diff, rows, unmet=unmet)
        reply = reply_from_editor_receipts(reqs, list(ledger.receipts), unbound=ledger.extras)
    return CaseResult(case["id"], reply, ledger, {r.requirement_id: r for r in ledger.receipts})


def check_case(case: dict[str, Any], result: CaseResult) -> list[str]:
    """What the case expects that the result does not do (empty = the row passes)."""
    problems: list[str] = []
    expect = case["expect"]
    ids = [row["id"] for row in case["requirements"]]
    gaps = result.ledger.gaps(ids, result.reply)
    if gaps:
        problems.append(f"ledger gaps: {gaps}")
    for rid, state in (expect.get("states") or {}).items():
        row = result.ledger.row(rid)
        got = row.state if row else None
        if got != state:
            problems.append(f"{rid}: state {got!r}, expected {state!r}")
    for rid, status in (expect.get("status") or {}).items():
        receipt = result.receipts.get(rid)
        got = receipt.status if receipt else None
        if got != status:
            problems.append(f"{rid}: receipt {got!r}, expected {status!r}")
    for text in expect.get("reply_has") or ():
        if text not in result.reply:
            problems.append(f"reply lacks {text!r}")
    for text in expect.get("reply_lacks") or ():
        if text in result.reply:
            problems.append(f"reply still says {text!r}")
    for rid, texts in (expect.get("line_has") or {}).items():
        line = _line(result.reply, case, rid)
        for text in texts:
            if text not in line:
                problems.append(f"{rid} line lacks {text!r}: {line!r}")
    for rid, texts in (expect.get("line_lacks") or {}).items():
        line = _line(result.reply, case, rid)
        for text in texts:
            if text in line:
                problems.append(f"{rid} line still says {text!r}: {line!r}")
    owners = {change.key: list(change.owners) for change in result.ledger.changes}
    for key, want in (expect.get("changes") or {}).items():
        # The op applied (its change is in the diff) and is named under the asks it served.
        if owners.get(key) != want:
            problems.append(f"change {key}: owners {owners.get(key)!r}, expected {want!r}")
    if expect.get("prod_reply_gaps"):
        # Negative control: the reply production actually sent for this turn must fail the
        # ledger check, naming the changes it left out.
        found = result.ledger.gaps(ids, case["prod_reply"])
        for key in expect["prod_reply_gaps"]:
            if f"unnamed_change:{key}" not in found:
                problems.append(f"control: production's reply not flagged for {key}: {found}")
    return problems


def _line(reply: str, case: dict[str, Any], rid: str) -> str:
    """The reply line of requirement ``rid``: lines follow requirement order (one line, or one
    "- " line each), and an "Also changed" line comes after them."""
    body = [line for line in reply.splitlines() if line.startswith("- ")] or reply.splitlines()[:1]
    order = [r["id"] for r in case["requirements"]]
    index = order.index(rid)
    return body[index] if index < len(body) else ""
