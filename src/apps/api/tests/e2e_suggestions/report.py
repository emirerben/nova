"""Collect what the end-to-end run proved and write ``.chip-eval/e2e-report.{md,json}``.

Every test records into a ``Recorder`` and writes one partial file when it finishes; the session
hook in ``conftest`` merges the partials of this run (so it works under xdist, where tests run in
other processes). A check has one of four statuses:

* ``pass``  the assertion held;
* ``fail``  it did not (the test fails too);
* ``gap``   it did not, and a named gap says why. The test still passes, the report prints the gap,
            and a gap whose check starts passing FAILS the test ("remove the marker"), so a marker
            can never outlive the bug it names;
* ``na``    the check does not apply to this kind (for example Undo of a refused tap).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.e2e_suggestions.gaps import Gap

REPO_ROOT = Path(__file__).resolve().parents[5]
OUT_DIR = Path(os.environ.get("CHIP_EVAL_DIR") or REPO_ROOT / ".chip-eval")
RUN_ID = os.environ.get("PYTEST_XDIST_TESTRUNUID") or uuid.uuid4().hex
PARTIALS = OUT_DIR / "e2e-partials"

# Column order of the per-kind table: (key, header).
CHECKS: tuple[tuple[str, str], ...] = (
    ("served", "served"),
    ("stored", "in ring"),
    ("accepted", "accepted"),
    ("change", "real change"),
    ("revision", "new revision"),
    ("receipts", "receipts"),
    ("chat", "chat = prompt"),
    ("undo", "Undo"),
    ("consumed", "consumed"),
    ("second_tap", "2nd tap refused"),
    ("refreshed", "refreshed set"),
)
KIND_ORDER = (
    "captions",
    "sfx",
    "transition",
    "music",
    "hook",
    "mute_clip",
    "filler_words",
    "duplicates",
)


@dataclass
class Cell:
    """One kind on one footage profile of one scenario."""

    scenario: str
    profile: str
    kind: str
    language: str = "en"
    served: bool = False
    # False for a cell run under a configuration that is not what ships (a patched dark constant).
    shipped: bool = True
    rank: int | None = None
    action_type: str = ""
    title: str = ""
    prompt: str = ""
    checks: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "", *, gap: Gap | None = None) -> bool:
        if gap is not None:
            status = "fail" if ok else "gap"
            text = f"gap {gap.id} closed, remove the marker" if ok else detail
            self.checks[name] = {
                "status": status,
                "detail": text,
                "gap": {"id": gap.id, "title": gap.title, "owner": gap.owner, "text": gap.text},
            }
        else:
            status = "pass" if ok else "fail"
            self.checks[name] = {"status": status, "detail": detail}
        return status != "fail"

    def na(self, name: str, why: str) -> None:
        self.checks[name] = {"status": "na", "detail": why}

    @property
    def failures(self) -> list[str]:
        return [
            f"{self.scenario}/{self.profile}/{self.kind}: {name}: {c['detail']}"
            for name, c in self.checks.items()
            if c["status"] == "fail"
        ]

    @property
    def verdict(self) -> str:
        statuses = {c["status"] for c in self.checks.values()}
        if not self.served:
            return "not served"
        if "fail" in statuses:
            return "FAIL"
        return "pass (gap)" if "gap" in statuses else "pass"


@dataclass
class Negative:
    case: str
    scenario: str
    expect: str
    ok: bool
    detail: str = ""
    # ``gap`` when the expectation fails because of a named gap (the run stays green).
    status: str = ""
    gap: dict[str, str] | None = None


@dataclass
class Recorder:
    """What one test proved. ``finish`` writes the partial and raises on any failed check."""

    name: str
    cells: list[Cell] = field(default_factory=list)
    negatives: list[Negative] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)

    def cell(self, scenario: str, profile: str, kind: str, language: str = "en") -> Cell:
        cell = Cell(scenario, profile, kind, language)
        self.cells.append(cell)
        return cell

    def negative(
        self,
        case: str,
        scenario: str,
        expect: str,
        ok: bool,
        detail: str = "",
        *,
        gap: Gap | None = None,
    ) -> bool:
        if gap is None:
            self.negatives.append(
                Negative(case, scenario, expect, ok, detail, "pass" if ok else "fail")
            )
            return ok
        status = "fail" if ok else "gap"
        marker = {"id": gap.id, "title": gap.title, "owner": gap.owner, "text": gap.text}
        shown = f"gap {gap.id} closed, remove the marker" if ok else detail
        self.negatives.append(
            Negative(case, scenario, expect, status != "fail", shown, status, marker)
        )
        return status != "fail"

    def finish(self) -> None:
        write_partial(self)
        problems = [f for c in self.cells for f in c.failures]
        problems += [f"{n.scenario}: {n.case}: {n.detail}" for n in self.negatives if not n.ok]
        assert not problems, "end-to-end proof failed:\n  " + "\n  ".join(problems)


def write_partial(rec: Recorder) -> None:
    folder = PARTIALS / RUN_ID
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in rec.name)
    payload = {
        "name": rec.name,
        "cells": [c.__dict__ for c in rec.cells],
        "negatives": [n.__dict__ for n in rec.negatives],
        "sections": rec.sections,
    }
    (folder / f"{safe}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


# ── Merging ───────────────────────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_run() -> dict[str, list[dict[str, Any]]]:
    folder = PARTIALS / RUN_ID
    out: dict[str, list[dict[str, Any]]] = {"cells": [], "negatives": [], "sections": []}
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out["cells"] += data["cells"]
        out["negatives"] += data["negatives"]
        out["sections"].append({"test": data["name"], **data["sections"]})
    return out


def kind_summary(cells: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per kind: how many cells were tapped, which checks held, and the honest verdict."""
    summary: dict[str, dict[str, Any]] = {}
    for kind in KIND_ORDER:
        mine = [c for c in cells if c["kind"] == kind and c.get("shipped", True)]
        served = [c for c in mine if c["served"]]
        per_check: dict[str, str] = {}
        for key, _header in CHECKS:
            statuses = [c["checks"].get(key, {}).get("status", "na") for c in served]
            real = [s for s in statuses if s != "na"]
            ok = sum(1 for s in real if s in {"pass", "gap"})
            per_check[key] = f"{ok}/{len(real)}" if real else "n/a"
            if "gap" in real:
                per_check[key] += "*"
        failed = [c for c in served if any(v["status"] == "fail" for v in c["checks"].values())]
        accepted = [c["checks"].get("accepted", {}).get("status") == "pass" for c in served]
        stored = [c["checks"].get("stored", {}).get("status") == "pass" for c in served]
        summary[kind] = {
            # The seam applied every served card (with the ring holding it) ...
            "seam_executes": bool(served) and all(accepted) and not failed,
            # ... and the unmodified GET put the card where the seam looks for it.
            "executes_on_unmodified_get": bool(served)
            and all(accepted)
            and all(stored)
            and not failed,
            "refused_cells": sum(1 for a in accepted if not a),
            "cells": len(mine),
            "served": len(served),
            "checks": per_check,
            "failed_cells": len(failed),
            "gaps": sorted(
                {
                    v["gap"]["id"]
                    for c in served
                    for v in c["checks"].values()
                    if v["status"] == "gap" and "gap" in v
                }
            ),
            "action_types": sorted({c["action_type"] for c in served if c["action_type"]}),
            "unshipped": [
                {"cell": f"{c['scenario']}/{c['profile']}", "verdict": _verdict(c)}
                for c in cells
                if c["kind"] == kind and not c.get("shipped", True)
            ],
            "scenarios": sorted({f"{c['scenario']}/{c['profile']}" for c in served}),
        }
    return summary


def _cell_order(c: dict[str, Any]) -> tuple:
    return (c["scenario"], c["profile"], c["rank"] or 99, c["kind"])


def _collect_gaps(
    cells: list[dict[str, Any]], negatives: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for c in cells:
        for v in c["checks"].values():
            if v["status"] == "gap" and "gap" in v:
                found[v["gap"]["id"]] = v["gap"]
    for n in negatives or []:
        if n.get("status") == "gap" and n.get("gap"):
            found[n["gap"]["id"]] = n["gap"]
    return [found[k] for k in sorted(found)]


def _verdict(c: dict[str, Any]) -> str:
    if not c["served"]:
        return "not served"
    statuses = {v["status"] for v in c["checks"].values()}
    if "fail" in statuses:
        return "FAIL"
    return "pass (gap)" if "gap" in statuses else "pass"


def render_markdown(run: dict[str, list[dict[str, Any]]], meta: dict[str, str]) -> str:
    cells, negatives = run["cells"], run["negatives"]
    summary = kind_summary(cells)
    lines: list[str] = []
    add = lines.append
    add("# AI suggestion blocks: every card, tapped end to end")
    add("")
    add(
        f"Generated {meta['when']} on `{meta['branch']}` at `{meta['sha']}`. "
        "Free, offline: the model "
        "is a cassette, Redis is in memory, everything else is the real server on Postgres."
    )
    add("")
    add(
        "Each card is served by the real `GET /creation-threads/{id}/suggestions` after a real "
        "`kria_suggester` run, then tapped through `submit_turn` -> `run_kria_turn`, "
        "the pinned path. "
        "`*` marks a check that holds only with a named gap (listed below)."
    )
    add("")
    add("## Per-kind pass table (cells passed / cells tapped)")
    add("")
    headers = ["kind", "action", "cells", *[h for _k, h in CHECKS], "verdict"]
    add("| " + " | ".join(headers) + " |")
    add("| " + " | ".join("---" for _ in headers) + " |")
    for kind in KIND_ORDER:
        row = summary[kind]
        if row["served"] == 0:
            verdict = "NOT SERVED"
        elif row["failed_cells"]:
            verdict = f"FAIL ({row['failed_cells']})"
        elif row["gaps"]:
            verdict = "pass with gap"
        else:
            verdict = "pass"
        add(
            "| "
            + " | ".join(
                [
                    f"`{kind}`",
                    ", ".join(row["action_types"]) or "-",
                    str(row["served"]),
                    *[row["checks"][key] for key, _h in CHECKS],
                    verdict,
                ]
            )
            + " |"
        )
    add("")
    unserved = [k for k in KIND_ORDER if summary[k]["served"] == 0]
    if unserved:
        add("### Kinds that did not execute, and why")
        add("")
        for kind in unserved:
            notes = sorted({n for c in cells if c["kind"] == kind for n in c["notes"]})
            side = summary[kind]["unshipped"]
            line = f"* `{kind}`: " + ("; ".join(notes) if notes else "no cell served it")
            if side:
                line += " Under a patched configuration (not shipped): " + "; ".join(
                    f"{u['cell']} {u['verdict']}" for u in side
                )
            add(line)
        add("")
    gaps = _collect_gaps(cells, negatives)
    if gaps:
        add("### Named gaps (the check fails honestly, the run does not)")
        add("")
        for gap in gaps:
            add(f"* **{gap['id']} {gap['title']}** (`{gap['owner']}`): {gap['text']}")
        add("")
    add("## Per cell")
    add("")
    add(
        "| scenario | profile | lang | kind | rank | chat line (the card's prompt) | verdict "
        "| gaps / failures |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for c in sorted(cells, key=_cell_order):
        marks = []
        for name, v in c["checks"].items():
            if v["status"] == "gap":
                marks.append(f"{v['gap']['id']} ({name})")
            elif v["status"] == "fail":
                marks.append(f"FAIL {name}: {v['detail']}")
        verdict = _verdict(c) + ("" if c.get("shipped", True) else " [not shipped]")
        add(
            f"| {c['scenario']} | {c['profile']} | {c['language']} | `{c['kind']}` | "
            f"{c['rank'] or '-'} | {_cell(c['prompt'])} | {verdict} | {_cell(', '.join(marks))} |"
        )
    add("")
    add("## Negative cases")
    add("")
    add("| case | scenario | expected | result | detail |")
    add("| --- | --- | --- | --- | --- |")
    for n in negatives:
        add(
            f"| {n['case']} | {n['scenario']} | {_cell(n['expect'])} | "
            f"{_negative_status(n)} | {_cell(n['detail'])} |"
        )
    add("")
    for section in run["sections"]:
        body = {k_: v for k_, v in section.items() if k_ != "test"}
        if not body:
            continue
        add(f"## {section['test']}")
        add("")
        add("```json")
        add(json.dumps(body, indent=2, ensure_ascii=False, default=str))
        add("```")
        add("")
    add("## What is real and what stands in")
    add("")
    for line in STAND_INS:
        add(f"* {line}")
    add("")
    return "\n".join(lines) + "\n"


STAND_INS = (
    "Real: the GET route, the `kria_suggester` task body, `SuggestionAgent` (prompt render, strict "
    "schema, parse), the candidate engine, every kind's `build_action` and witness, the pinned "
    "planner, `submit_turn`, `run_kria_turn`, `_complete_draft_turn`, drafts, Undo, receipts, the "
    "ring, Postgres.",
    "Stand-in model: `CassetteClient` answers from a fixed preference list restricted to the "
    "kinds and clip handles of the call's own `response_schema`. It pins what the server does "
    "with an answer, not "
    "what a model would choose (the paid live eval measures that).",
    "Stand-in Redis: an in-memory client for the five commands `suggestion_store` uses.",
    "Stand-in capability map: `_editor_capabilities` is fixed open, as in the lane harnesses; "
    "the real "
    "derivation follows deployment flags and source freshness.",
    "Replay fixtures carry labels, requests and briefs, not pixels or audio. Speech flags and the "
    "render are authored; `audio_quality` is measured from committed recordings and "
    "`near_dup_hash` is "
    "the near-duplicate lane's recorded hashes.",
    "Not exercised: a device, the render itself (a tap stages a draft or an approval; the render "
    "worker and the phone export are not run), a live model, and any paid call.",
)


def _negative_status(n: dict[str, Any]) -> str:
    if n.get("status") == "gap":
        return f"gap {n['gap']['id']}"
    return "pass" if n["ok"] else "FAIL"


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def kinds_proven(summary: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for kind in KIND_ORDER:
        row = summary[kind]
        out.append(
            {
                "kind": kind,
                # Literal: a card served by the UNMODIFIED GET, tapped, is applied.
                "tap_executes": row["executes_on_unmodified_get"],
                # The pinned seam applies the card once the ring holds it (the serve's own helpers
                # fill the ring in the run; see GAP-1 and GAP-2).
                "seam_executes": row["seam_executes"],
                "cells_tapped": row["served"],
                "refused_cells": row["refused_cells"],
                # Cells run under a configuration that is not what ships (a patched dark constant).
                "unshipped": row["unshipped"],
                "checks": row["checks"],
                "gaps": row["gaps"],
                "scenarios": row["scenarios"],
            }
        )
    return out


def write_if_any() -> Path | None:
    run = load_run()
    if not run["cells"] and not run["negatives"]:
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "when": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "sha": _git("rev-parse", "--short", "HEAD"),
    }
    summary = kind_summary(run["cells"])
    (OUT_DIR / "e2e-report.md").write_text(render_markdown(run, meta), encoding="utf-8")
    (OUT_DIR / "e2e-report.json").write_text(
        json.dumps(
            {
                **meta,
                "kinds": summary,
                "kinds_proven": kinds_proven(summary),
                "gaps": _collect_gaps(run["cells"], run["negatives"]),
                "cells": run["cells"],
                "negatives": run["negatives"],
                "sections": run["sections"],
                "stand_ins": list(STAND_INS),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    shutil.rmtree(PARTIALS, ignore_errors=True)
    return OUT_DIR / "e2e-report.md"
