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
# One id for the controller and every xdist worker: the controller imports this first and the
# workers it spawns inherit the variable (``PYTEST_XDIST_TESTRUNUID`` is set in workers only).
RUN_ID = os.environ.setdefault(
    "CHIP_EVAL_RUN_ID", os.environ.get("PYTEST_XDIST_TESTRUNUID") or uuid.uuid4().hex
)
PARTIALS = OUT_DIR / "e2e-partials"

# Column order of the per-kind table: (key, header).
CHECKS: tuple[tuple[str, str], ...] = (
    ("served", "served"),
    ("stored", "in ring"),
    ("accepted", "accepted"),
    ("change", "real change"),
    ("one_change", "one change"),
    ("revision", "new revision"),
    ("receipts", "receipts"),
    ("render_kick", "render kick"),
    ("chat", "chat = prompt"),
    ("consumed", "consumed"),
    ("second_tap", "2nd tap refused"),
    ("refreshed", "refreshed set"),
    ("undo", "Undo draft"),
    ("undo_brief", "Undo brief"),
    ("undo_setting", "Undo setting"),
)
# Scenario columns of the coverage matrix: (key, header).
SCENARIO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("nermin-matcha-update", "nermin matcha (en)"),
    ("nermin-matcha-update/tr", "nermin matcha (tr)"),
    ("nermin-travel-diary", "nermin travel (en)"),
    ("east-run-brief", "east run + brief (tr)"),
    ("east-run-brief/footage-only", "east run bare (tr)"),
    ("east-run-brief/footage-only/en", "east run bare (en)"),
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
    # The state the thread was in when the card was served: ``baseline`` (a ready render, nothing
    # unusual) or what a round-5 scenario did first (a Save, a denied render, a pulled switch ...).
    state: str = "baseline"
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
            self.checks[name] = {"status": status, "detail": text, "gap": gap.wire()}
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

    def cell(
        self, scenario: str, profile: str, kind: str, language: str = "en", state: str = "baseline"
    ) -> Cell:
        cell = Cell(scenario, profile, kind, language, state=state)
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
        marker = gap.wire()
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


# What the first word of a test's name says about which part of the proof it belongs to.
FAMILIES: dict[str, str] = {
    "tap-every-card": "baseline matrix: every kind x footage x language",
    "negative": "negative cases: stale base, switches, KRI-129, copy, mute dark, Undo, ring",
    "first": "first prompt: chips only, planner-read",
    "save": "native-editor Save and Undo, before and after the serve, in every order (R5-F, R6-O)",
    "lifecycle": "a staged render that never happens (R5-G)",
    "kill": "switches pulled after the freeze (R5-E)",
    "copy": "the on-screen title's decision, every state (R5-A)",
    "turkish": "the creator's own words, Turkish and English (R5-D)",
    "matrix": "mute matrix, held sets, queued taps, coverage (R5-M)",
    "mark": "the protected-region (mark) gate (R6-M)",
    "readiness": "approval readiness with an ordinary length ask (R6-R)",
    "ring": "the ring write is busy (R6-B)",
    "length": "duplicates and a stated video length (R6-D)",
    "reoffer": "a tap taken back is an offer again, with and without the brief (R6-U)",
}


def family_of(name: str) -> str:
    return FAMILIES.get(name.split(" ", 1)[0], "other")


def write_partial(rec: Recorder) -> None:
    folder = PARTIALS / RUN_ID
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in rec.name)
    family = family_of(rec.name)
    payload = {
        "name": rec.name,
        "family": family,
        "cells": [{**c.__dict__, "family": family} for c in rec.cells],
        "negatives": [{**n.__dict__, "family": family} for n in rec.negatives],
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
        out["sections"].append(
            {"test": data["name"], "family": data.get("family", "other"), **data["sections"]}
        )
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
            "languages": sorted({c["language"] for c in served}),
            "refusals": [
                f"{c['scenario']}/{c['profile']}: "
                f"{c['checks'].get('accepted', {}).get('detail', '')[:140]}"
                for c, ok in zip(served, accepted, strict=True)
                if not ok
            ],
            "unshipped": [
                {"cell": f"{c['scenario']}/{c['profile']}", "verdict": _verdict(c)}
                for c in cells
                if c["kind"] == kind and not c.get("shipped", True)
            ],
            # The state the thread was in when each served card was tapped (a Save, a denied
            # render, a pulled switch ...), with how many of those cells held.
            "states": _states(served),
            "scenarios": sorted({f"{c['scenario']}/{c['profile']}" for c in served}),
        }
    return summary


def _states(cells: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for c in cells:
        row = found.setdefault(c.get("state", "baseline"), {"cells": 0, "pass": 0})
        row["cells"] += 1
        row["pass"] += 1 if _verdict(c) in {"pass", "pass (gap)"} else 0
    return dict(sorted(found.items()))


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
    add("## Coverage matrix (kind x scenario; every cell is a real thread tapped end to end)")
    add("")
    heads = ["kind", *[h for _k, h in SCENARIO_COLUMNS], "first prompt"]
    add("| " + " | ".join(heads) + " |")
    add("| " + " | ".join("---" for _ in heads) + " |")
    first = first_prompt_kinds(run["sections"])
    for kind in KIND_ORDER:
        row = [f"`{kind}`"]
        for key, _h in SCENARIO_COLUMNS:
            row.append(coverage_cell(cells, kind, key))
        row.append(first.get(kind, "not offered"))
        add("| " + " | ".join(row) + " |")
    add("")
    add(
        "`pass xN`: N profiles served the card and every check held. `gap`: held except a named "
        "gap. `-`: that footage never served the kind (its evidence, or the creator's brief, does "
        "not support it). `chip`: offered at first prompt as a KRI-756 chip only (prompt-only; its "
        "tap is the planner's, not a pinned card)."
    )
    add("")
    add("## What was run (the families of the proof)")
    add("")
    add("| family | tests | cells tapped | negative checks | pass | gap | fail |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    tests_by_family: dict[str, int] = {}
    for section in run["sections"]:
        fam = section.get("family", "other")
        tests_by_family[fam] = tests_by_family.get(fam, 0) + 1
    for family in sorted(
        set(tests_by_family)
        | {c.get("family", "other") for c in cells}
        | {n.get("family", "other") for n in negatives}
    ):
        mine_n = [n for n in negatives if n.get("family", "other") == family]
        mine_c = [c for c in cells if c.get("family", "other") == family]
        add(
            f"| {family} | {tests_by_family.get(family, 0)} | {len(mine_c)} | {len(mine_n)} | "
            f"{sum(1 for n in mine_n if n.get('status') == 'pass')} | "
            f"{sum(1 for n in mine_n if n.get('status') == 'gap')} | "
            f"{sum(1 for n in mine_n if n.get('status') == 'fail')} |"
        )
    add("")
    add("## Cells by the state the thread was in (kind x state; shipped configuration only)")
    add("")
    states = sorted({s for k in KIND_ORDER for s in summary[k]["states"]})
    add("| kind | " + " | ".join(states) + " |")
    add("| --- | " + " | ".join("---" for _ in states) + " |")
    for kind in KIND_ORDER:
        row = [f"`{kind}`"]
        for state in states:
            found = summary[kind]["states"].get(state)
            row.append(f"{found['pass']}/{found['cells']}" if found else "-")
        add("| " + " | ".join(row) + " |")
    add("")
    add(
        "`n/m`: n of m served cells held every check. `-`: that state never served the kind. "
        "A state that serves nothing (a decided title, an open question) is proven by its "
        "negative checks, not by a cell."
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
                line += (
                    " Under a patched configuration (not shipped): "
                    + "; ".join(f"{n} cells {v}" for v, n in _patched(side)["verdicts"].items())
                    + " (see Per cell)."
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
        "| scenario | profile | state | lang | kind | rank | chat line (the card's prompt) "
        "| verdict | gaps / failures |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for c in sorted(cells, key=_cell_order):
        marks = []
        for name, v in c["checks"].items():
            if v["status"] == "gap":
                marks.append(f"{v['gap']['id']} ({name})")
            elif v["status"] == "fail":
                marks.append(f"FAIL {name}: {v['detail']}")
        verdict = _verdict(c) + ("" if c.get("shipped", True) else " [not shipped]")
        add(
            f"| {c['scenario']} | {c['profile']} | {c.get('state', 'baseline')} | "
            f"{c['language']} | `{c['kind']}` | {c['rank'] or '-'} | {_cell(c['prompt'])} | "
            f"{verdict} | {_cell(', '.join(marks))} |"
        )
    add("")
    add("## Negative cases, by family")
    add("")
    for family in sorted({n.get("family", "other") for n in negatives}):
        rows = [n for n in negatives if n.get("family", "other") == family]
        counts = _negative_counts(rows)
        add(f"<details><summary>{family}: {counts}</summary>")
        add("")
        add("| case | scenario | expected | result | detail |")
        add("| --- | --- | --- | --- | --- |")
        for n in rows:
            add(
                f"| {n['case']} | {n['scenario']} | {_cell(n['expect'])} | "
                f"{_negative_status(n)} | {_cell(n['detail'])} |"
            )
        add("")
        add("</details>")
        add("")
    firsts = [sec["first_prompt"] for sec in run["sections"] if sec.get("first_prompt")]
    if firsts:
        add("## First prompt (footage uploaded, nothing said, no render)")
        add("")
        add(
            "No v2 card exists here by design (a card applies to an edit, and there is none). The "
            "thread serves KRI-756 chips; each was tapped on its own thread and must reach the "
            "planner once with its own words, unpinned, writing nothing behind it. The planner is "
            "a stand-in (the real one is an LLM)."
        )
        add("")
        add("| scenario | lang | chips served | tap results |")
        add("| --- | --- | --- | --- |")
        for data in sorted(firsts, key=lambda x: x["scenario"]):
            taps = "; ".join(
                f"{kind}: {'ok' if t['ok'] else 'FAIL'} (planner x{t['planner_calls']}, "
                f"pinned {t['pinned']})"
                for kind, t in data["taps"].items()
            )
            add(
                f"| {data['scenario']} | {data['language']} | {', '.join(data['chips'])} | "
                f"{_cell(taps)} |"
            )
        add("")
    for section in run["sections"]:
        body = {k_: v for k_, v in section.items() if k_ not in {"test", "family", "first_prompt"}}
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


def coverage_cell(cells: list[dict[str, Any]], kind: str, key: str) -> str:
    mine = [
        c
        for c in cells
        if c["kind"] == kind
        and c["scenario"] == key
        and c["served"]
        and c.get("shipped", True)
        and c.get("state", "baseline") == "baseline"
    ]
    if not mine:
        return "-"
    verdicts = [_verdict(c) for c in mine]
    if any(v == "FAIL" for v in verdicts):
        return "FAIL"
    if any(v == "pass (gap)" for v in verdicts):
        return f"gap x{len(mine)}"
    return f"pass x{len(mine)}"


def first_prompt_kinds(sections: list[dict[str, Any]]) -> dict[str, str]:
    """Kind -> how the first prompt offers it, over every first-prompt thread of the run."""
    seen: dict[str, list[bool]] = {}
    for section in sections:
        data = section.get("first_prompt")
        if not data:
            continue
        for kind in data["chips"]:
            if kind == "default_edit":
                continue
            seen.setdefault(kind, []).append(bool(data["taps"].get(kind, {}).get("ok")))
    return {
        kind: ("chip" if all(oks) else "chip (tap FAILS)") + f" x{len(oks)}"
        for kind, oks in seen.items()
    }


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
    "Stand-ins of the round-5 states: the editor Save is the real `editor-commit` route with the "
    "render kick, the durable-sources prefix and the storage existence check replaced (there is no "
    "bucket); the render worker's completion write is `finish_render`; the agent session's pointer "
    "following a Kria render is written by hand (`agent_reads_video`); an approval's time running "
    "out is its `expires_at` moved into the past; a typed message is answered by the legacy "
    "planner double.",
    "The mute kind is dark as shipped (`PINNED_TAP_APPLIES_CLIP_SOUND = False`). Every mute cell "
    "runs with that constant patched on and is marked `[not shipped]`; it proves what the seam "
    "would do, not that a phone would then mute the clip.",
    "Not exercised: a device, the render itself (a tap stages a draft or an approval; the render "
    "worker and the phone export are not run), a live model, and any paid call.",
)


def _negative_counts(rows: list[dict[str, Any]]) -> str:
    ok = sum(1 for n in rows if n.get("status") == "pass")
    gap = sum(1 for n in rows if n.get("status") == "gap")
    bad = sum(1 for n in rows if n.get("status") == "fail")
    return f"{len(rows)} checks, {ok} pass, {gap} gap, {bad} fail"


def _negative_status(n: dict[str, Any]) -> str:
    if n.get("status") == "gap":
        return f"gap {n['gap']['id']}"
    return "pass" if n["ok"] else "FAIL"


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def kinds_proven(
    summary: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    first = first_prompt_kinds(sections or [])
    out = []
    for kind in KIND_ORDER:
        row = summary[kind]
        # An open gap that lets a card of this kind be served and then refused at the tap is a
        # served card that does not execute, whatever the cells that were tapped did.
        dead = sorted(
            g["id"] for g in gaps or [] if g.get("serves_dead_card") and kind in g.get("kinds", [])
        )
        about = sorted(g["id"] for g in gaps or [] if kind in g.get("kinds", []))
        cells_ok = row["executes_on_unmodified_get"]
        executes = cells_ok and not dead
        out.append(
            {
                "kind": kind,
                # Literal: EVERY shipped cell of the kind, served by the UNMODIFIED GET and tapped,
                # was applied. One refused cell, or a kind that is not served, makes it false.
                "tap_executes": executes,
                "why": _why(kind, row, cells_ok) + _dead_note(dead),
                "open_gaps_about_the_kind": about,
                "serves_a_card_the_tap_refuses": dead,
                # The pinned seam applies the card once the ring holds it.
                "seam_executes": row["seam_executes"],
                "cells_tapped": row["served"],
                "refused_cells": row["refused_cells"],
                "languages": row["languages"],
                "first_prompt": first.get(kind, "not offered"),
                # Cells run under a configuration that is not what ships (a patched dark constant).
                "unshipped": row["unshipped"],
                "patched": _patched(row["unshipped"]),
                # Every state a thread was in when a served card of this kind was tapped.
                "states": row["states"],
                "checks": row["checks"],
                "gaps": row["gaps"],
                "scenarios": row["scenarios"],
            }
        )
    return out


def _dead_note(dead: list[str]) -> str:
    return (
        f"; BUT a served card is refused at the tap under a named gap ({', '.join(dead)})"
        if dead
        else ""
    )


def _patched(unshipped: list[dict[str, str]]) -> dict[str, Any]:
    verdicts: dict[str, int] = {}
    for item in unshipped:
        verdicts[item["verdict"]] = verdicts.get(item["verdict"], 0) + 1
    return {"cells": len(unshipped), "verdicts": verdicts}


def _why(kind: str, row: dict[str, Any], executes: bool) -> str:
    if executes:
        gaps = f"; named gaps on other checks: {', '.join(row['gaps'])}" if row["gaps"] else ""
        return (
            f"{row['served']} served cells in {'+'.join(row['languages'])}, every one accepted and "
            f"applied{gaps}"
        )
    if row["served"] == 0:
        side = row["unshipped"]
        patched = _patched(side)
        return "never served as shipped (dark by design)" + (
            f"; with the constant patched on: {patched['cells']} cells, "
            + ", ".join(f"{n} {v}" for v, n in patched["verdicts"].items())
            if side
            else ""
        )
    return (
        f"{row['refused_cells']} of {row['served']} served cells refused at the tap: "
        + " | ".join(row["refusals"])
    )


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
                "kinds_proven": kinds_proven(
                    summary, run["sections"], _collect_gaps(run["cells"], run["negatives"])
                ),
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
    # Only this run's partials: another session (or an earlier one that crashed) may share the
    # directory, and wiping its files would drop cells from the report it is still collecting.
    shutil.rmtree(PARTIALS / RUN_ID, ignore_errors=True)
    try:
        PARTIALS.rmdir()
    except OSError:
        pass
    return OUT_DIR / "e2e-report.md"
