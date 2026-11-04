#!/usr/bin/env python3
"""OLD vs NEW suggestion chips per fixture, as a markdown artifact (KRI-756).

Free, offline, no network, no model calls: it runs the deterministic chip engine over the
replay fixtures (``tests/fixtures/agent_evals/suggestion_chips`` plus the ``nermin-*`` turns and
``east-run-brief``), scores every fixture on the structural gates G1-G8 and G11, replays each
recorded planner answer through the real v2 adapter + compiler (G9), replays the hand-authored
suggestion-agent cassettes (``tests/fixtures/agent_evals/suggestion_agent``) through the real
task body, fuzzes arbitrary model output (G10), and writes::

    <repo>/.chip-eval/report.md

Usage (from ``src/apps/api``)::

    .venv/bin/python scripts/suggestion_chip_report.py            # write + print the summary
    .venv/bin/python scripts/suggestion_chip_report.py --stdout   # print the whole report
    .venv/bin/python scripts/suggestion_chip_report.py --write-contract
        # regenerate tests/fixtures/agent_evals/suggestion_chips/contract_example_*.json

Exit code 1 when any gate fails outside the documented known gaps.

What "OLD" means: the chips a creator saw before KRI-756 in the iOS app. There were no starter
chips at first prompt; iOS ``ReadyStage`` showed two hardcoded ``PromptChip`` strings after a
render. They are read from the base commit so the comparison does not move when an iOS lane
edits the working tree. (The web client is out of scope for this feature.)

What the numbers do NOT say: whether a creator prefers the new chips (needs the blind pairwise
judge over real sessions) or whether today's Main Creator / copilot prompts choose the recorded
answers (needs a live, paid run). Both are listed as TODOs in tests/evals/README.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

# Settings needs a few env vars to import; tests/conftest.py sets the same defaults.
import os  # noqa: E402

for _key, _value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "report-not-used",
    "WHISPER_BACKEND": "local",
    "WAITLIST_ADMIN_SECRET": "report-not-used",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
    "INTERNAL_API_KEY": "report-not-used",
}.items():
    os.environ.setdefault(_key, _value)

from structlog.testing import capture_logs  # noqa: E402

from tests.evals import suggestion_agent_harness as cs  # noqa: E402
from tests.evals import suggestion_chips_harness as h  # noqa: E402

REPO_ROOT = API_ROOT.parents[2]
DEFAULT_OUT = REPO_ROOT / ".chip-eval" / "report.md"
IOS_CHIPS_FILE = "src/apps/ios/Kria/Features/CanonicalChatComponents.swift"
FALLBACK_OLD_READY = ["Try a stronger opening", "Make it warmer"]


def _ios_source() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{IOS_CHIPS_FILE}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        path = REPO_ROOT / IOS_CHIPS_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""


def old_chips(state: str) -> list[str]:
    """The chips the creator saw before KRI-756, per state.

    ``first_prompt`` / ``none``: none (web had clarifying-question options only; iOS had no
    starter chips). ``ready``: the two hardcoded iOS ``ReadyStage`` ``PromptChip`` strings.
    """
    if state != "ready":
        return []
    source = _ios_source()
    start = source.find("struct ReadyStage")
    block = source[start : start + 6000] if start >= 0 else ""
    found = re.findall(r'PromptChip\(text:\s*"([^"]+)"', block)
    return found or list(FALLBACK_OLD_READY)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def evaluate() -> dict:
    cases = h.all_cases()
    by_name = {c.name: c for c in cases}
    rows = []
    gate_totals = {g: [0, 0] for g in h.GATES}
    expect_total = [0, 0]
    chip_count = 0
    gap_hits: list[tuple[str, str, str]] = []
    for case in cases:
        result = h.evaluate_case(case)
        out = result["out"]
        chip_count += len(out.chips)
        expect_problems = h.expectation_failures(case, out)
        row_failures: dict[str, list[str]] = {}
        for gate, problems in result["failures"].items():
            if (case.name, gate) in h.KNOWN_GAPS:
                if problems:
                    gap_hits.append((case.name, gate, h.KNOWN_GAPS[(case.name, gate)]))
                continue
            gate_totals[gate][1] += 1
            gate_totals[gate][0] += 0 if problems else 1
            if problems:
                row_failures[gate] = problems
        if (case.name, "expect") in h.KNOWN_GAPS:
            if expect_problems:
                gap_hits.append((case.name, "expect", h.KNOWN_GAPS[(case.name, "expect")]))
        else:
            expect_total[1] += 1
            expect_total[0] += 0 if expect_problems else 1
            if expect_problems:
                row_failures["expect"] = expect_problems
        rows.append({"case": case, "out": out, "failures": row_failures})
    tiers: dict[tuple[str, str], int] = {}
    for case in cases:
        out = next(r["out"] for r in rows if r["case"] is case)
        tier_of = h.witness_tiers(case)
        for chip in out.chips:
            key = (chip.kind, tier_of.get(chip.kind, "policy_only"))
            tiers[key] = tiers.get(key, 0) + 1
    cassettes = h.load_cassettes()
    exec_rows = []
    for cassette in cassettes:
        try:
            problems = h.cassette_failures(cassette, by_name)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the artifact
            problems = [f"{type(exc).__name__}: {exc}"]
        exec_rows.append((cassette, problems))
    return {
        "rows": rows,
        "specificity": h.specificity_stats([r["out"] for r in rows]),
        "tiers": tiers,
        "agent": evaluate_agent(by_name),
        "gate_totals": gate_totals,
        "expect_total": expect_total,
        "chip_count": chip_count,
        "gap_hits": gap_hits,
        "exec_rows": exec_rows,
    }


FUZZ_N = 30


def evaluate_agent(by_name: dict) -> dict:
    """E2 / E3: cassette replays and the G10 fuzz (all offline, no model)."""
    cass_rows = []
    for cassette in cs.load_cassettes():
        try:
            rep = cs.replay_cassette(cassette, by_name)
            problems = (
                cs.expectation_failures(cassette, rep)
                + cs.check_agent_set(rep.case, rep.served, rep.plan, rep.ctx)
                + cs.check_cards(rep.ctx, rep.plan, rep.cards, rep.served.suggestions)
            )
        except Exception as exc:  # noqa: BLE001 - report, do not crash the artifact
            rep, problems = None, [f"{type(exc).__name__}: {exc}"]
        cass_rows.append((cassette, rep, problems))
    runs = violations = 0
    outcomes: dict[str, int] = {}
    examples: list[str] = []
    for case in cs.fuzz_cases():
        for i, raw in cs.fuzz_stream(case, FUZZ_N):
            rep = cs.replay_raw(case, lambda plan, raw=raw: raw)
            problems = cs.check_agent_set(case, rep.served, rep.plan, rep.ctx) + cs.check_cards(
                rep.ctx, rep.plan, rep.cards, rep.served.suggestions
            )
            runs += 1
            if problems:
                violations += 1
                examples.append(f"{case.name} #{i}: {problems[0]}")
            run = rep.agent_run
            if run.get("fallback_reason") == "schema_error":
                outcomes["schema_error fallback"] = outcomes.get("schema_error fallback", 0) + 1
            if rep.served.suggestions:
                outcomes["v2 cards served"] = outcomes.get("v2 cards served", 0) + 1
            if any(
                c.why and c.why != h.expected_evidence(case, c, rep.served.state)
                for c in rep.served.chips
            ):
                outcomes["agent why shown"] = outcomes.get("agent why shown", 0) + 1
            if rep.served.wording == "fallback":
                outcomes["deterministic set (wording=fallback)"] = (
                    outcomes.get("deterministic set (wording=fallback)", 0) + 1
                )
    return {
        "cassettes": cass_rows,
        "fuzz_runs": runs,
        "fuzz_violations": violations,
        "fuzz_examples": examples[:5],
        "fuzz_outcomes": outcomes,
        "fuzz_cases": len(cs.fuzz_cases()),
    }


def _pct(ok: int, total: int) -> str:
    return "n/a" if total == 0 else f"{ok}/{total} ({100 * ok / total:.0f}%)"


def add_agent_section(add, agent: dict) -> None:
    cass = agent["cassettes"]
    ok = sum(1 for _, _, p in cass if not p)
    add("## Suggestion agent (`nova.chat.suggestion_agent`): replay and G10 fuzz")
    add("")
    add(
        "Hand-authored raw model answers through the REAL agent parse, task body, registry "
        "builders and witnesses, validators and serve join. No model is ever called. These are "
        "NOT recorded model outputs: they measure what the safeguards do with answers I wrote, "
        "not what gpt-6.1-sol picks."
    )
    add("")
    add("| check | result | bar |")
    add("| --- | --- | --- |")
    add(f"| cassettes (natural + adversarial) | {_pct(ok, len(cass))} | 100% |")
    add(
        f"| G10 fuzz: served chips are live candidates, served cards are witnessed registry "
        f"candidates | "
        f"{_pct(agent['fuzz_runs'] - agent['fuzz_violations'], agent['fuzz_runs'])} "
        f"({agent['fuzz_cases']} contexts x {FUZZ_N} seeded answers) | 100% |"
    )
    add("")
    add(
        "Fuzz outcomes (counts of runs): "
        + ", ".join(f"{k}: {v}" for k, v in sorted(agent["fuzz_outcomes"].items()))
    )
    if agent["fuzz_examples"]:
        add("")
        add("**G10 VIOLATIONS:** " + "; ".join(agent["fuzz_examples"]))
    add("")
    add("### Cassettes")
    add("")
    add("| cassette | context | wording | served chips | v2 cards | result |")
    add("| --- | --- | --- | --- | --- | --- |")
    for cassette, rep, problems in cass:
        if rep is None:
            served, wording, cards = "-", "-", "-"
        else:
            served = ", ".join(f"{c.kind}{'+why' if c.why else ''}" for c in rep.served.chips)
            wording = rep.served.wording
            cards = ", ".join(s.kind for s in rep.served.suggestions) or "-"
        verdict = "pass" if not problems else "FAIL: " + _cell("; ".join(problems))
        add(
            f"| {cassette['id']} | {cassette['context']} | {wording} | {served} | {cards} | "
            f"{verdict} |"
        )
    add("")


def render(data: dict) -> str:
    rows = data["rows"]
    lines: list[str] = []
    add = lines.append
    add("# Suggestion chips: OLD vs NEW (KRI-756 replay eval)")
    add("")
    add(
        "Deterministic engine, real planner dry-run witnesses, recorded (hand-authored) planner "
        "answers. Free, offline, no model call. Generated by "
        "`src/apps/api/scripts/suggestion_chip_report.py`."
    )
    add("")
    add("## Pass/fail numbers")
    add("")
    add("| gate | passed | note |")
    add("| --- | --- | --- |")
    notes = {
        "G1 grounding": "bar: 100%",
        "G2 capability matrix": "bar: 0 chips for an unavailable capability",
        "G3 not already in edit": "bar: 0 repeats of what is on screen",
        "G4 diversity": "4 chips => 3+ kinds, no duplicate target",
        "G5 chip #1 is default_edit": "first prompt only",
        "G6 hygiene": "length, language, quotes, stoplist",
        "G7 closed template set": "no input text in any message",
        "G8 determinism": "byte-identical reruns",
        "G11 wire contract": "rank 1..n, status/state/retry, closed kind enum, dark kinds absent",
    }
    for gate, (ok, total) in data["gate_totals"].items():
        add(f"| {gate} | {_pct(ok, total)} | {notes.get(gate, '')} |")
    ok, total = data["expect_total"]
    add(f"| fixture expectation pins | {_pct(ok, total)} | human-reviewed `expect` blocks |")
    exec_ok = sum(1 for _, p in data["exec_rows"] if not p)
    add(
        f"| G9 executability (compile only) | {_pct(exec_ok, len(data['exec_rows']))} | "
        "bar: >=95%; recorded answer through replay_v2_strategy / replay_v2_editor |"
    )
    add("")
    add(
        f"{len(rows)} fixtures, {data['chip_count']} chips shown. "
        f"Gates exclude {len(data['gap_hits'])} known-gap cell(s) (listed below; strict xfails "
        "in the test)."
    )
    add("")
    add(
        "G9 proves the COMPILE path only: a recorded answer to the chip message parses, "
        "adapts and compiles to a non-empty change. It does not prove that today's prompts "
        "produce that answer. The first-prompt `hook` is graded as 'reaches wording approval "
        "with a candidate', not 'executes an op'."
    )
    add("")
    sp = data["specificity"]
    add("### What G1 does NOT prove: footage specificity of the template chips")
    add("")
    add(
        f"{sp['chips']} chips use only {sp['distinct_labels']} distinct labels; "
        f"{sp['anchored']} carry any anchor (media id, ordinal or text bar), "
        f"{sp['moment_anchored']} name a moment and {sp['with_why']} carry a `why`. "
        "G1 only validates an anchor a chip actually carries, so a 100% G1 is NOT evidence "
        "that a chip fits THIS footage, and no gate here measures that: the template engine "
        "emits a closed deck of generic labels by design. Footage-specific wording comes only "
        "from the `nova.chat.suggestion_agent` (hand-authored cassettes here, never run live) "
        "and `opener_clip` (dark). The specificity bar is the paid live replay plus judge "
        "(`specificity_to_this_footage` in the rubric), which has NOT been run."
    )
    add("")
    add("### Witness tier of every chip shown (how its executability was proven)")
    add("")
    add("| kind | strategy (planner policy dry run) | editor (parser + compiler) | policy_only |")
    add("| --- | --- | --- | --- |")
    kinds = sorted({k for k, _ in data["tiers"]})
    for kind in kinds:
        add(
            f"| {kind} | {data['tiers'].get((kind, 'strategy'), 0)} | "
            f"{data['tiers'].get((kind, 'editor'), 0)} | "
            f"{data['tiers'].get((kind, 'policy_only'), 0)} |"
        )
    add("")
    add(
        "`policy_only` = no planner dry run exists for it: `default_edit` IS the open request, "
        "`title_animation` is allow-listed (the compiler cannot prove an entrance animation)."
    )
    add("")
    add_agent_section(add, data["agent"])
    if data["gap_hits"]:
        add("## Known gaps (engine behaviour that contradicts the ticket, pinned as xfail)")
        add("")
        for name, gate, reason in data["gap_hits"]:
            add(f"* `{name}` ({gate}): {reason}")
        add("")
    add("## G9 executability detail")
    add("")
    add("| cassette | kind | state | engine | result |")
    add("| --- | --- | --- | --- | --- |")
    for cassette, problems in data["exec_rows"]:
        verdict = "pass" if not problems else "FAIL: " + _cell("; ".join(problems))
        add(
            f"| {cassette['id']} | {cassette['kind']} | {cassette['state']} | "
            f"{cassette['engine']} | {verdict} |"
        )
    add("")
    add("## Per fixture")
    add("")
    for row in rows:
        case, out = row["case"], row["out"]
        add(f"### {case.name}")
        add("")
        add(f"{case.description}".strip() or "(no description)")
        add("")
        footage = case.context_dict.get("clips") or []
        subjects = ", ".join(c.get("subject") or "(not analysed)" for c in footage[:6])
        if len(footage) > 6:
            subjects += f", +{len(footage) - 6} more"
        add(
            f"* state `{out.state}` / status `{out.status}` / understanding "
            f"`{out.understanding}` / language `{case.language}` / {len(footage)} clip(s)"
            + (f": {subjects}" if subjects else "")
        )
        old = old_chips(out.state)
        add(
            "* OLD: "
            + (", ".join(f'"{c}"' for c in old) if old else "none (nothing was suggested)")
            + (" (hardcoded, same for every video)" if old else "")
        )
        if out.chips:
            add("* NEW:")
            add("")
            add("| # | label (shown) | message (sent) | kind | gated by | witness |")
            add("| --- | --- | --- | --- | --- | --- |")
            tier_of = h.witness_tiers(case)
            for chip in out.chips:
                add(
                    f"| {chip.rank} | {_cell(chip.label)} | {_cell(chip.message)} | "
                    f"{chip.kind} | {chip.capability} | {tier_of.get(chip.kind, 'policy_only')} |"
                )
        else:
            add("* NEW: no chips (render nothing)")
        if out.retry_after_s:
            add(f"* client re-GETs in {out.retry_after_s}s while understanding is pending")
        if row["failures"]:
            add("")
            add("**FAILING:**")
            for gate, problems in row["failures"].items():
                add(f"* {gate}: " + "; ".join(problems))
        add("")
    return "\n".join(lines) + "\n"


def write_contract_examples() -> list[Path]:
    by_name = {c.name: c for c in h.all_cases()}
    written = []
    for filename, fixture in h.CONTRACT.items():
        path = h.FIXTURE_DIR / filename
        body = json.loads(h.run_case(by_name[fixture]).model_dump_json())
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    from app.services import suggestion_chips as sc  # noqa: PLC0415

    unavailable = h.FIXTURE_DIR / "contract_example_unavailable.json"
    unavailable.write_text(
        json.dumps(json.loads(sc.unavailable_set().model_dump_json()), indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(unavailable)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--stdout", action="store_true", help="print the full report")
    parser.add_argument("--write-contract", action="store_true")
    args = parser.parse_args(argv)
    if args.write_contract:
        for path in write_contract_examples():
            print(f"wrote {path}")
        return 0
    # Swallow the framework's per-call info logs (nested capture_logs in the harness still work).
    with capture_logs():
        data = evaluate()
    text = render(data)
    if args.stdout:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
        head = text.split("## G9 executability detail")[0]
        sys.stdout.write(head)
    failed = (
        [(r["case"].name, g) for r in data["rows"] for g in r["failures"]]
        + [(c["id"], "G9") for c, p in data["exec_rows"] if p]
        + [(c["id"], "cassette") for c, _, p in data["agent"]["cassettes"] if p]
        + ([("fuzz", "G10")] if data["agent"]["fuzz_violations"] else [])
    )
    if failed:
        print(f"FAILING: {failed}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
