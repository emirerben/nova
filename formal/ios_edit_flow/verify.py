#!/usr/bin/env python3
"""Check the phone-render TLA+ model (PhoneRender.tla) against the code it describes.

The model is the iPhone render lifecycle behind the "killer" screens: "Kria couldn't
prepare this edit on your iPhone" (S1), "Couldn't refresh this edit" (S2), "This edit
needs your attention" (S3) and the "couldn't render" family (S4). PhoneRender.tla is the
code on this branch (the fixes); PhoneRenderBefore.tla is origin/main ff27b0a65.

Steps (all must pass):
  0. happy    - with no faults the model publishes a video and every rule holds there
  1. drift    - the covered Swift/Python functions still hash to what the model was
                last synced to (coverage.json); a change means re-read the model
  2. model    - PhoneRender.tla keeps every PASSING rule in each of its runs
  3. residual - PhoneRender.tla still breaks each RESIDUAL rule in its scoped run (a real
                finding on today's code; see findings.md)
  4. before   - PhoneRenderBefore.tla breaks every rule the fixes made pass (history)
  5. teeth    - each passing rule fails on a copy of PhoneRender.tla with its guard removed

When the code changes so a residual rule holds, move it from RESIDUAL to PASSING (and
widen its runs to the scenario it was scoped out of), add teeth, rerun. Never weaken a
rule to make it pass.

Usage: python3 formal/ios_edit_flow/verify.py [--resync] [--full] [--explore]
  --resync   rewrite coverage.json hashes + sync_commit (only after re-reading the model)
  --full     also run the liveness rules for Save with a missing original and for Save
             with Stop (about 3 more minutes); the default runs split those events
  --explore  also run every residual rule per starting scenario and event set, and write
             .dev/formal-ios-edit-flow/explore.json (used for findings.md)
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
BEFORE = HERE / "PhoneRenderBefore.tla"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
TLC_SHA256 = "dae639a1f51a9e13ce60c369b15f32124cf1a78017aca366b6de0ae2b5169439"
LOCAL_TOOLS = Path("/private/tmp/nova-formal-tools")

ALL_EVENTS = '{"save", "other", "stop", "kill", "background", "reaper", "sync"}'
ALL_CONDS = '{"none", "hot", "unsupported", "missing"}'
# Every starting condition except a recipe this iPhone cannot render. That one is a
# residual finding (RESIDUAL below) for the rules that are scoped to SCOPED.
SCOPED = '{"none", "hot", "missing"}'
UNSUPPORTED = '{"unsupported"}'
# Approval no longer routes anything in the fixed code (the server always sends
# retry_requires_approval = false, R:535; nothing in PhoneRender.tla reads `approval`
# except the AttentionHasWayOut teeth), so the fixed runs pin it to FALSE.
BASE = {"MaxFaults": "1", "MaxTicks": "1", "Conds": ALL_CONDS, "Approvals": "{FALSE}"}
ONE_SAVE = {**BASE, "MaxSaves": "1", "MaxId": "3", "Events": ALL_EVENTS}
TWO_SAVES = {**BASE, "MaxSaves": "2", "MaxId": "4", "Events": '{"save", "other"}'}
# Liveness runs: a patient user (SpecUser). Two answered preview polls, so the editor can
# see a newer generation twice (E:7666-7682); the real budget is 300.
LIVE = {**BASE, "MaxTicks": "2", "MaxSaves": "1", "MaxId": "3"}

SAFETY = ["NoStalePublish", "PublishedStaysPublished", "SyncedMeansPublished",
          "NoStaleRequestReinstalled", "CloudDecisionReported"]
WAY_OUT = [("PROPERTY", "AttentionHasWayOut"), ("PROPERTY", "PreviewConverges")]
# run name -> (specification, constants, [(kind, rule)]). Every PASSING rule appears in
# at least one run; TLC checks the rules of a run together.
MODEL_RUNS = {
    "one-save-all-events": ("Spec", {**ONE_SAVE, "Conds": SCOPED},
                            [("INVARIANT", r) for r in SAFETY + ["NoSpuriousAttention"]]),
    "one-save-all-events-unsupported": ("Spec", {**ONE_SAVE, "Conds": UNSUPPORTED},
                                        [("INVARIANT", r) for r in SAFETY]),
    "two-saves": ("Spec", TWO_SAVES, [("INVARIANT", r) for r in SAFETY]),
    # Liveness is slow in TLC (~90 us per graph node), so the default splits the events:
    # Save on a phone with its originals (cool or hot), Stop with every scoped condition
    # (including a missing original and the faults-only runs), and kills.
    "live-save": ("SpecUser", {**LIVE, "Conds": '{"none", "hot"}', "Events": '{"save"}'}, WAY_OUT),
    "live-stop": ("SpecUser", {**LIVE, "Conds": SCOPED, "Events": '{"stop"}'},
                  [("PROPERTY", "AttentionHasWayOut")]),
    # "Without anyone tapping anything": no user fairness, every condition.
    "agree": ("Spec", {**LIVE, "Conds": ALL_CONDS, "Events": '{"stop", "kill"}'},
              [("PROPERTY", "ClientServerAgree")]),
}
# --full: the pairs the default leaves out (about 420K and 290K states, ~3 min together).
# Save + Stop + a missing original at once is ~4M states and is not run; the safety runs
# cover that combination.
FULL_RUNS = {
    "live-save-missing": ("SpecUser", {**LIVE, "Conds": '{"missing"}', "Events": '{"save"}'},
                          WAY_OUT),
    "live-save-stop": ("SpecUser",
                       {**LIVE, "Conds": '{"none", "hot"}', "Events": '{"save", "stop"}'},
                       WAY_OUT + [("PROPERTY", "ClientServerAgree")]),
}
PASSING = sorted({rule for _, _, checks in MODEL_RUNS.values() for _, rule in checks})

# Rules today's code still breaks, each in the scenario it was scoped out of above.
RESIDUAL = {
    "NoSpuriousAttention": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": UNSUPPORTED}),
    "AttentionHasWayOut": ("PROPERTY", "SpecUser", {**LIVE, "Conds": UNSUPPORTED, "Events": "{}"}),
    "PreviewConverges": ("PROPERTY", "SpecUser",
                         {**LIVE, "Conds": UNSUPPORTED, "Events": '{"save"}'}),
}

# The old code (PhoneRenderBefore.tla) broke each of these; the fixes made them pass.
# Same constants as the old findings run (approval routed retries then).
OLD_LIVE = {**BASE, "Approvals": "{TRUE, FALSE}", "MaxSaves": "1", "MaxId": "3",
            "Events": '{"save", "stop"}'}
OLD_ONE_SAVE = {**ONE_SAVE, "Approvals": "{TRUE, FALSE}"}
BEFORE_BREAKS = {
    "NoSpuriousAttention": ("INVARIANT", "Spec", OLD_ONE_SAVE),
    "CloudDecisionReported": ("INVARIANT", "Spec", OLD_ONE_SAVE),
    "AttentionHasWayOut": ("PROPERTY", "SpecUser", OLD_LIVE),
    "ClientServerAgree": ("PROPERTY", "Spec", OLD_LIVE),
    "PreviewConverges": ("PROPERTY", "SpecUser", OLD_LIVE),
}

# Each mutation removes one guard from PhoneRender.tla; TLC must then break the rule it
# protects in the given run: (kind, specification, constants).
MUTATIONS = {
    # complete only for the current identity in phase syncing.
    "NoStalePublish": [('CompleteOK == sId = coord.id /\\ sPhase = "syncing"',
                        'CompleteOK == sPhase \\in {"awaiting", "syncing"}')],
    # V:626 mark_device_failed only from awaiting/syncing (409 when published).
    "PublishedStaysPublished": [('FailableForReport == sPhase \\in {"awaiting", "syncing"}',
                                 'FailableForReport == sPhase # "attention"')],
    # P:320-328 a 409 on complete is "superseded", never "published".
    "SyncedMeansPublished": [('LateComplete == [coord EXCEPT !.ph = "superseded"',
                              'LateComplete == [coord EXCEPT !.ph = "synced"')],
    # S:346 / S:351 tickets[key] == ticket after the fetch.
    "NoStaleRequestReinstalled": [("/\\ Apply(oldRec, FALSE)", "/\\ Apply(oldRec, TRUE)")],
    # S:476-480 a failed status read waits; the old code showed "couldn't prepare" (S1).
    "NoSpuriousAttention": [('ReadFailedPres ==\n  IF pres.ph = "synced" THEN pres',
                             'ReadFailedPres ==\n  IF TRUE THEN '
                             '[NoPres EXCEPT !.ph = "attention", !.cause = "prepFail"]')],
    # S:481 sendDecline; the old code returned before telling the server.
    "CloudDecisionReported": [("/\\ decl' = IF keeps \\/ decl = id THEN decl ELSE id",
                               "/\\ decl' = decl")],
    # R:1050-1072 re-delivery needs no approval; the old route answered 409 for runtime-v2.
    "AttentionHasWayOut": [('RetryOK == sPhase = "attention"',
                            'RetryOK == ~approval /\\ sPhase = "attention"')],
    # C:354-366 a report is re-sent until the server takes it; the old one was dropped.
    "ClientServerAgree": [("/\\ faults' = faults - 1 /\\ rep' = rep",
                           "/\\ faults' = faults - 1 /\\ rep' = NoRep")],
    # E:7666-7682 follow a newer generation read twice; the old poll waited it out.
    "PreviewConverges": [("THEN IF e.seen + 1 >= 2 THEN Saved(e)", "THEN IF FALSE THEN Saved(e)")],
}
TEETH_RUN = {
    "NoStalePublish": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": SCOPED}),
    "PublishedStaysPublished": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": SCOPED}),
    "SyncedMeansPublished": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": SCOPED}),
    "NoStaleRequestReinstalled": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": SCOPED}),
    "NoSpuriousAttention": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": SCOPED}),
    "CloudDecisionReported": ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": UNSUPPORTED}),
    "AttentionHasWayOut": ("PROPERTY", "SpecUser",
                           {**LIVE, "Conds": '{"missing"}', "Approvals": "{TRUE}", "Events": "{}"}),
    "ClientServerAgree": ("PROPERTY", "Spec", {**LIVE, "Conds": '{"none"}', "Events": '{"stop"}'}),
    "PreviewConverges": ("PROPERTY", "SpecUser",
                         {**LIVE, "Conds": '{"none"}', "Events": '{"save"}'}),
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
        specification: str, tag: str, expect_pass: bool = False) -> dict:
    """Run TLC once. Returns {"broken": [rules], "trace": n, "states": n, "seconds": s}.

    A run expected to pass checks liveness once, on the complete graph; one expected to
    break keeps TLC's periodic checks so a violation surfaces early."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "run.cfg"
        lines = [f"SPECIFICATION {specification}", "CONSTANTS"]
        lines += [f"  {k} = {v}" for k, v in constants.items()]
        lines += [f"{kind} {rule}" for kind, rule in checks]
        cfg.write_text("\n".join(lines) + "\n")
        shutil.copy(spec, Path(tmp) / spec.name)
        start = time.time()
        run = subprocess.run(
            [JAVA, "-XX:+UseParallelGC", "-cp", str(JAR), "tlc2.TLC", "-workers", "auto",
             *(["-lncheck", "final"] if expect_pass else []),
             "-deadlock", "-metadir", str(Path(tmp) / "states"), "-config", str(cfg), spec.name],
            cwd=tmp, capture_output=True, text=True, timeout=900,
        )
        seconds = round(time.time() - start, 1)
    log = OUT / f"{tag}.log"
    log.write_text(run.stdout + run.stderr)
    broken: list[str] = []
    m = re.search(r"Invariant (\w+) is violated", run.stdout)
    if m:
        broken = [m.group(1)]
    m = re.search(r"Temporal propert(?:y|ies) (.+?) (?:is|was|were) violated", run.stdout)
    if m:
        broken = re.findall(r"\w+", m.group(1).replace(" and ", " "))
    if not broken and "No error has been found" not in run.stdout:
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
    """Run every residual rule per starting scenario and event set: how many ways break."""
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
    for rule, (kind, specification, consts) in RESIDUAL.items():
        rows = []
        for ev_name, events in event_sets.items():
            for cond in ("none", "hot", "unsupported", "missing"):
                c = {**consts, "Events": events, "Conds": f'{{"{cond}"}}'}
                if ev_name == "save+other device":
                    c.update(MaxSaves="2", MaxId="4")
                tag = f"explore-{rule}-{ev_name.replace(' ', '_').replace('+', '_')}-{cond}"
                r = tlc(SPEC, c, [(kind, rule)], specification, tag)
                rows.append({"events": ev_name, "cond": cond, "broken": rule in r["broken"],
                             "trace": r["trace"]})
        broken = [r for r in rows if r["broken"]]
        results[rule] = {
            "scenarios_checked": len(rows),
            "scenarios_broken": len(broken),
            "broken_conditions": sorted({r["cond"] for r in broken}),
            "shortest_trace": min((r["trace"] for r in broken), default=None),
            "rows": rows,
        }
        print(f"explore {rule}: broken in {len(broken)}/{len(rows)} scenarios, "
              f"conditions {results[rule]['broken_conditions']}")
    (OUT / "explore.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def run_model(runs: dict, report: dict) -> None:
    for name, (specification, consts, checks) in runs.items():
        r = tlc(SPEC, consts, checks, specification, f"model-{name}", expect_pass=True)
        report[name] = r
        rules = ", ".join(rule for _, rule in checks)
        print(f"model, {name}: " + ("holds" if not r["broken"] else f"BROKEN {r['broken']}")
              + f" [{rules}] ({r['states']:,} states, {r['seconds']}s)")
        if r["broken"]:
            sys.exit(f"FAIL: the code model breaks {r['broken']}; see {r['log']}")


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

    happy = {**BASE, "MaxFaults": "0", "MaxSaves": "1", "MaxId": "3", "Conds": '{"none"}',
             "Events": "{}"}
    r = tlc(SPEC, happy, [("INVARIANT", "NeverPublished")], "Spec", "happy-publishes")
    if r["broken"] != ["NeverPublished"]:
        sys.exit("FAIL: with no faults the model never publishes a video; the model is wrong")
    every = [("INVARIANT", rule) for rule in SAFETY + ["NoSpuriousAttention"]]
    every += [("PROPERTY", rule) for rule in ("AttentionHasWayOut", "PreviewConverges")]
    r = tlc(SPEC, {**happy, "MaxTicks": "2"}, every, "SpecUser", "happy-rules")
    if r["broken"]:
        sys.exit(f"FAIL: {r['broken']} break on the happy path; the model is wrong ({r['log']})")
    r = tlc(SPEC, happy, [("PROPERTY", "ClientServerAgree")], "Spec", "happy-agree")
    if r["broken"]:
        sys.exit(f"FAIL: ClientServerAgree breaks on the happy path; the model is wrong "
                 f"({r['log']})")
    report["happy_path"] = "publishes; every rule holds"
    print("happy path (no faults, no events): publishes, every rule holds")

    model: dict[str, object] = {}
    run_model(MODEL_RUNS, model)
    if "--full" in sys.argv:
        run_model(FULL_RUNS, model)
    report["model"] = model

    residual = {}
    for rule, (kind, specification, consts) in RESIDUAL.items():
        r = tlc(SPEC, consts, [(kind, rule)], specification, f"residual-{rule}")
        residual[rule] = r
        print(f"residual {rule} (unsupported recipe): "
              + (f"breaks (trace of {r['trace']} states, {r['seconds']}s)"
                 if rule in r["broken"] else "HOLDS"))
    report["residual"] = residual
    held = sorted(rule for rule, r in residual.items() if rule not in r["broken"])
    if held:
        sys.exit(f"FAIL: {held} no longer break on the unsupported recipe. If the code was "
                 "fixed, move them from RESIDUAL to PASSING (widen their runs), add teeth, rerun.")

    before = {}
    for rule, (kind, specification, consts) in BEFORE_BREAKS.items():
        r = tlc(BEFORE, consts, [(kind, rule)], specification, f"before-{rule}")
        before[rule] = r
        print(f"before the fix, {rule}: "
              + (f"breaks (trace of {r['trace']} states, {r['seconds']}s)"
                 if rule in r["broken"] else "HOLDS"))
    report["before"] = before
    held = sorted(rule for rule, r in before.items() if rule not in r["broken"])
    if held:
        sys.exit(f"FAIL: PhoneRenderBefore.tla should break {held} (the old code's findings)")

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
        kind, specification, consts = TEETH_RUN[rule]
        r = tlc(mutant, consts, [(kind, rule)], specification, f"teeth-{rule}")
        teeth[rule] = rule in r["broken"]
        print(f"teeth, {rule} without its guard: " + ("caught" if teeth[rule] else "NOT CAUGHT")
              + f" ({r['seconds']}s)")
        if not teeth[rule]:
            sys.exit(f"FAIL: {rule} cannot fail, so it proves nothing")
    missing = sorted(set(PASSING) - set(MUTATIONS))
    if missing:
        sys.exit(f"FAIL: passing rules without teeth: {missing}")
    report["teeth"] = teeth

    if "--explore" in sys.argv:
        report["explore"] = {k: {kk: vv for kk, vv in v.items() if kk != "rows"}
                             for k, v in explore().items()}

    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS (residual findings recorded)" + (" with DRIFT to review" if drift else ""))
    sys.exit(3 if drift else 0)


if __name__ == "__main__":
    JAR = tlc_jar()
    JAVA = java()
    main()
