#!/usr/bin/env python3
"""Check the phone-render TLA+ model (PhoneRender.tla) against the code it describes.

The model is the iPhone render lifecycle behind the "killer" screens: "Kria couldn't
prepare this edit on your iPhone" (S1), "Couldn't refresh this edit" (S2), "This edit
needs your attention" (S3) and the "couldn't render" family (S4).

Steps (all must pass):
  0. happy    - with no faults the model publishes a video and every rule holds there
  1. drift    - the covered Swift/Python functions still hash to what the model was
                last synced to (coverage.json); a change means re-read the model
  2. model    - PhoneRender.tla keeps every rule in PhoneRender.cfg, at 1 save with every
                optional event and at 2 saves (editor + another device)
  3. findings - PhoneRender.tla breaks exactly the rules in PhoneRenderFindings.cfg
                (today's code; each break is a finding written up in findings.md)
  4. teeth    - each passing rule fails on a copy of PhoneRender.tla with its guard removed

Once the code is fixed, move a rule from FINDINGS to PASSING (and its cfg line from
PhoneRenderFindings.cfg to PhoneRender.cfg). Never weaken a rule to make it pass.

Usage: python3 formal/ios_edit_flow/verify.py [--resync] [--explore]
  --resync   rewrite coverage.json hashes + sync_commit (only after re-reading the model)
  --explore  also run every finding rule per starting scenario and event set, and write
             .dev/formal-ios-edit-flow/explore.json (used for findings.md; ~5 min)
Env:   TLA2TOOLS (tla2tools.jar v1.8.0), JAVA (java 11+ binary)
Exit:  0 PASS, 1 FAIL, 3 PASS with drift to review
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / ".dev/formal-ios-edit-flow"
SPEC = HERE / "PhoneRender.tla"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
TLC_SHA256 = "dae639a1f51a9e13ce60c369b15f32124cf1a78017aca366b6de0ae2b5169439"
LOCAL_TOOLS = Path("/private/tmp/nova-formal-tools")

ALL_EVENTS = '{"save", "other", "stop", "kill", "background", "reaper", "sync"}'
BASE = {
    "MaxFaults": "1",
    "MaxTicks": "1",
    "Conds": '{"none", "hot", "unsupported", "missing"}',
    "Approvals": "{TRUE, FALSE}",
}
# Bounds for the passing (safety) rules. Both runs together take ~15 s.
MODEL_RUNS = {
    "one-save-all-events": {**BASE, "MaxSaves": "1", "MaxId": "3", "Events": ALL_EVENTS},
    "two-saves": {**BASE, "MaxSaves": "2", "MaxId": "4", "Events": '{"save", "other"}'},
}
LIVE = {**BASE, "MaxSaves": "1", "MaxId": "3", "Events": '{"save", "stop"}'}
# Happy path: no faults, no events, nothing wrong with the phone. Every rule must hold
# here and a video must get published, or the model is broken (not the code).
HAPPY = {**BASE, "MaxFaults": "0", "MaxSaves": "1", "MaxId": "3", "Conds": '{"none"}',
         "Approvals": "{FALSE}", "Events": "{}"}

PASSING = ["NoStalePublish", "PublishedStaysPublished", "SyncedMeansPublished",
           "NoStaleRequestReinstalled"]
# rule -> (kind, spec, constants). Invariants run with every optional event; the
# liveness rules run under a patient user (SpecUser) with saves and Stop.
FINDINGS = {
    "NoSpuriousAttention": ("INVARIANT", "Spec", MODEL_RUNS["one-save-all-events"]),
    "CloudDecisionReported": ("INVARIANT", "Spec", MODEL_RUNS["one-save-all-events"]),
    "AttentionHasWayOut": ("PROPERTY", "SpecUser", LIVE),
    "ClientServerAgree": ("PROPERTY", "SpecUser", LIVE),
    "PreviewConverges": ("PROPERTY", "SpecUser", LIVE),
}

# Each mutation removes one guard; TLC must then break the rule it protects.
MUTATIONS = {
    # R:1091-1097 complete only for the current identity in phase syncing.
    "NoStalePublish": [('CompleteOK == sId = coord.id /\\ sPhase = "syncing"',
                        'CompleteOK == sPhase \\in {"awaiting", "syncing"}')],
    # V:592-608 mark_device_failed only from awaiting/syncing (R:830 409 when published).
    "PublishedStaysPublished": [('FailableForReport == sPhase \\in {"awaiting", "syncing"}',
                                 'FailableForReport == sPhase # "attention"')],
    # P:237-240 a 409 on complete is "superseded", never "published".
    "SyncedMeansPublished": [('LateComplete == [coord EXCEPT !.ph = "superseded"',
                              'LateComplete == [coord EXCEPT !.ph = "synced"')],
    # S:122 / S:171 tickets[key] == ticket after the fetch.
    "NoStaleRequestReinstalled": [("/\\ Apply(oldRec, FALSE)", "/\\ Apply(oldRec, TRUE)")],
}
TEETH_RUN = {
    "NoStalePublish": "one-save-all-events",
    "PublishedStaysPublished": "one-save-all-events",
    "SyncedMeansPublished": "one-save-all-events",
    "NoStaleRequestReinstalled": "one-save-all-events",
}


def tlc_jar() -> Path:
    if os.environ.get("TLA2TOOLS"):
        jar = Path(os.environ["TLA2TOOLS"])
    elif (LOCAL_TOOLS / "tla2tools.jar").exists():
        jar = LOCAL_TOOLS / "tla2tools.jar"
    else:
        jar = OUT / "tla2tools.jar"
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {TLC_URL}")
        urllib.request.urlretrieve(TLC_URL, jar)  # noqa: S310 - pinned URL + sha256 below
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    if digest != TLC_SHA256:
        sys.exit(f"FAIL: {jar} sha256 {digest} is not TLC v1.8.0 ({TLC_SHA256})")
    return jar


def java() -> str:
    local = LOCAL_TOOLS / "jdk-21.0.8+9-jre/Contents/Home/bin/java"
    found = os.environ.get("JAVA") or (str(local) if local.exists() else shutil.which("java"))
    if not found or subprocess.run([found, "-version"], capture_output=True).returncode:
        sys.exit("FAIL: no Java runtime. Install one (brew install openjdk@21) and set JAVA.")
    return found


def tlc(spec: Path, constants: dict[str, str], checks: list[tuple[str, str]],
        specification: str, tag: str) -> dict:
    """Run TLC once. Returns {"broken": rule|None, "trace": n, "states": n, "seconds": s}."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "run.cfg"
        lines = [f"SPECIFICATION {specification}", "CONSTANTS"]
        lines += [f"  {k} = {v}" for k, v in constants.items()]
        lines += [f"{kind} {rule}" for kind, rule in checks]
        cfg.write_text("\n".join(lines) + "\n")
        shutil.copy(spec, Path(tmp) / "PhoneRender.tla")
        start = time.time()
        run = subprocess.run(
            [JAVA, "-XX:+UseParallelGC", "-cp", str(JAR), "tlc2.TLC", "-workers", "auto",
             "-deadlock", "-metadir", str(Path(tmp) / "states"), "-config", str(cfg),
             "PhoneRender.tla"],
            cwd=tmp, capture_output=True, text=True, timeout=900,
        )
        seconds = round(time.time() - start, 1)
    log = OUT / f"{tag}.log"
    log.write_text(run.stdout + run.stderr)
    broken = None
    m = re.search(r"(?:Invariant|Temporal property) (\w+) (?:is|was) violated", run.stdout)
    if m:
        broken = m.group(1)
    elif "No error has been found" not in run.stdout:
        sys.exit(f"FAIL: TLC did not finish ({tag}); see {log}")
    states = re.findall(r"([\d,]+) distinct states found", run.stdout)
    return {
        "broken": broken,
        "trace": len(re.findall(r"^State \d+: ", run.stdout, re.M)),
        "states": int(states[-1].replace(",", "")) if states else 0,
        "seconds": seconds,
        "log": str(log.relative_to(REPO)),
    }


# ---------------------------------------------------------------- drift

def swift_functions(text: str, name: str) -> list[str]:
    """Every `func name` body (brace matched) in a Swift file."""
    out = []
    for m in re.finditer(r"\bfunc\s+`?" + re.escape(name) + r"`?\s*[(<]", text):
        start = text.rfind("\n", 0, m.start()) + 1
        brace = text.find("{", m.end())
        if brace < 0:
            continue
        depth, i = 0, brace
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(text[start : i + 1])
    return out


def python_functions(text: str, name: str) -> list[str]:
    tree = ast.parse(text)
    source = text.splitlines()
    return [
        "\n".join(source[n.lineno - 1 : n.end_lineno])
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]


def function_hashes() -> dict[str, str]:
    coverage = json.loads((HERE / "coverage.json").read_text())
    out = {}
    for rel, names in coverage["functions"].items():
        text = (REPO / rel).read_text()
        for name in names:
            bodies = (python_functions if rel.endswith(".py") else swift_functions)(text, name)
            if not bodies:
                sys.exit(f"DRIFT: {rel}:{name} no longer exists; update the model and coverage.json")
            out[f"{rel}:{name}"] = hashlib.sha256("\n".join(bodies).encode()).hexdigest()[:16]
    return out


# ---------------------------------------------------------------- explore

def explore() -> dict:
    """Run every finding per starting scenario and event set: how many distinct ways break."""
    event_sets = {
        "faults only": "{}",
        "save": '{"save"}',
        "stop": '{"stop"}',
        "background": '{"background"}',
        "kill+reaper": '{"kill", "reaper"}',
        "save+other device": '{"save", "other"}',
        "draft sync": '{"sync"}',
    }
    results = {}
    for rule, (kind, specification, consts) in FINDINGS.items():
        rows = []
        for ev_name, events in event_sets.items():
            for cond in ("none", "hot", "unsupported", "missing"):
                for approval in ("FALSE", "TRUE"):
                    c = {**consts, "Events": events, "Conds": f'{{"{cond}"}}',
                         "Approvals": f"{{{approval}}}"}
                    if ev_name == "save+other device":
                        c.update(MaxSaves="2", MaxId="4")
                    r = tlc(SPEC, c, [(kind, rule)], specification,
                            f"explore-{rule}-{ev_name.replace(' ', '_').replace('+', '_')}-{cond}-{approval}")
                    rows.append({"events": ev_name, "cond": cond, "approval": approval == "TRUE",
                                 "broken": r["broken"] == rule, "trace": r["trace"]})
        broken = [r for r in rows if r["broken"]]
        results[rule] = {
            "scenarios_checked": len(rows),
            "scenarios_broken": len(broken),
            "shortest_trace": min((r["trace"] for r in broken), default=None),
            "rows": rows,
        }
        print(f"explore {rule}: broken in {len(broken)}/{len(rows)} scenarios")
    (OUT / "explore.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {}

    hashes = function_hashes()
    coverage = json.loads((HERE / "coverage.json").read_text())
    if "--resync" in sys.argv:
        coverage["hashes"] = hashes
        coverage["sync_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()
        (HERE / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
        print("resynced coverage.json; commit it only after re-reading the model against the code")
    drift = sorted(k for k, v in hashes.items() if coverage.get("hashes", {}).get(k) != v)
    report["drift"] = drift
    if drift:
        print("DRIFT (review, not a bug): these covered functions changed since "
              f"{coverage.get('sync_commit')}:\n  " + "\n  ".join(drift))

    happy = tlc(SPEC, HAPPY, [("INVARIANT", "NeverPublished")], "Spec", "happy-publishes")
    if happy["broken"] != "NeverPublished":
        sys.exit("FAIL: with no faults the model never publishes a video; the model is wrong")
    for rule, (kind, specification, _) in FINDINGS.items():
        r = tlc(SPEC, HAPPY, [(kind, rule)], specification, f"happy-{rule}")
        if r["broken"]:
            sys.exit(f"FAIL: {rule} breaks on the happy path; the model is wrong ({r['log']})")
    report["happy_path"] = "publishes; every rule holds"
    print("happy path (no faults, no events): publishes, every rule holds")

    model = {}
    for name, consts in MODEL_RUNS.items():
        r = tlc(SPEC, consts, [("INVARIANT", rule) for rule in PASSING], "Spec", f"model-{name}")
        model[name] = r
        print(f"model, {name}: " + ("all rules hold" if not r["broken"] else f"BROKEN {r['broken']}")
              + f" ({r['states']:,} states, {r['seconds']}s)")
        if r["broken"]:
            sys.exit(f"FAIL: the code model breaks {r['broken']}; see {r['log']}")
    report["model"] = model

    findings = {}
    for rule, (kind, specification, consts) in FINDINGS.items():
        r = tlc(SPEC, consts, [(kind, rule)], specification, f"finding-{rule}")
        findings[rule] = r
        print(f"finding {rule}: " + (f"breaks (trace of {r['trace']} states, {r['seconds']}s)"
                                     if r["broken"] == rule else "HOLDS"))
    report["findings"] = findings
    held = sorted(rule for rule, r in findings.items() if r["broken"] != rule)
    if held:
        sys.exit(f"FAIL: {held} no longer break. If the code was fixed, move them from FINDINGS "
                 "to PASSING (and PhoneRenderFindings.cfg to PhoneRender.cfg), add teeth, rerun.")

    teeth = {}
    for rule, edits in MUTATIONS.items():
        text = SPEC.read_text()
        for old, new in edits:
            if text.count(old) != 1:
                sys.exit(f"FAIL: mutation for {rule} no longer matches PhoneRender.tla: {old!r}")
            text = text.replace(old, new)
        mutant = OUT / "mutant" / "PhoneRender.tla"
        mutant.parent.mkdir(exist_ok=True)
        mutant.write_text(text)
        r = tlc(mutant, MODEL_RUNS[TEETH_RUN[rule]], [("INVARIANT", rule)], "Spec", f"teeth-{rule}")
        teeth[rule] = r["broken"] == rule
        print(f"teeth, {rule} without its guard: " + ("caught" if teeth[rule] else "NOT CAUGHT"))
        if not teeth[rule]:
            sys.exit(f"FAIL: {rule} cannot fail, so it proves nothing")
    report["teeth"] = teeth

    if "--explore" in sys.argv:
        report["explore"] = {k: {kk: vv for kk, vv in v.items() if kk != "rows"}
                             for k, v in explore().items()}

    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS (findings recorded)" + (" with DRIFT to review" if drift else ""))
    sys.exit(3 if drift else 0)


if __name__ == "__main__":
    JAR = tlc_jar()
    JAVA = java()
    main()
