#!/usr/bin/env python3
"""Check Converge.tla: the editor always converges onto the latest version, with no choice state.

Steps (all must pass):
  1. spec    - TypeOK, NoChoiceState, AIEditsNeverCreators, AIEditNeverSilentlyDropped,
               UndoOnLatestBase hold, and the
               liveness rules ShowsLatest and SaveEventuallyEnabled hold under fair reads
  2. control - six mutated copies of the spec each break the rule they are meant to break
               (a pending-decision state, AI-staged edits passed as the creator's, an AI edit
               dropped, Undo restoring the pre-converge document, no fair reads, no end to a
               gesture); a control that
               does not fail means the rule proves nothing
  3. drift   - the Swift functions named in converge-source-map.json still hash to what the
               model was last read against

Usage: python3 formal/ios_edit_flow/verify_converge.py [--resync]
  --resync  rewrite converge-source-map.json (only after re-reading Converge.tla)
Env:   TLA2TOOLS (tla2tools.jar v1.8.0), JAVA (java 11+ binary); same lookup as verify.py
Exit:  0 PASS, 1 FAIL, 3 PASS with drift to review
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import verify

HERE = Path(__file__).resolve().parent
REPO = verify.REPO
SPEC = HERE / "Converge.tla"
SOURCE_MAP = HERE / "converge-source-map.json"
OUT = REPO / ".dev/formal-converge"

CONSTANTS = {"MaxGen": "3", "MaxIdent": "2", "MaxInteract": "2"}
SAFETY = ["TypeOK", "NoChoiceState", "AIEditsNeverCreators", "AIEditNeverSilentlyDropped", "UndoOnLatestBase"]
LIVENESS = ["ShowsLatest", "SaveEventuallyEnabled"]


def renamed_guard(name: str, guard: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Edits that put `guard` in front of action `name` (rename it, add a guarded wrapper)."""
    return [(f"\n{name} ==", f"\n{name}Unguarded ==")], [
        f"{name} == {guard} /\\ {name}Unguarded"
    ]


def pending_decision() -> list[tuple[str, str]]:
    """The old policy: an overlap parks the result in a pending decision that blocks the
    creator's actions and has no way out."""
    edits = [
        (
            "convIn |-> NoConv ]",
            "convIn |-> NoConv,\n        pending |-> FALSE ]",
        ),
        ("\nConvergeTo(g, h, replaced) ==", "\nConvergeApplied(g, h, replaced) =="),
        (
            "\n\\* ---- creator and environment actions ----",
            "\nConvergeTo(g, h, replaced) ==\n"
            "  IF replaced THEN s' = [s EXCEPT !.pending = TRUE, !.read = NoRead]\n"
            "  ELSE ConvergeApplied(g, h, replaced)\n"
            "\n\\* ---- creator and environment actions ----",
        ),
    ]
    wrappers = []
    for name in ("CreatorEdit", "ReadStart", "SaveOK", "SaveConflict", "SaveRefused"):
        rename, wrap = renamed_guard(name, "~s.pending")
        edits += rename
        wrappers += wrap
    edits.append(("\nNext ==", "\n" + "\n".join(wrappers) + "\n\nNext =="))
    return edits


# name -> (rule it must break, kind, edits to Converge.tla)
CONTROLS = {
    "pending-decision": ("NoChoiceState", "INVARIANT", pending_decision()),
    "ai-edits-as-creators": (
        "AIEditsNeverCreators",
        "INVARIANT",
        [
            (
                'OwnerOf(p, h) == IF p = "creator" THEN',
                'OwnerOf(p, h) == IF p # "none" THEN',
            )
        ],
    ),
    "ai-edit-dropped": (
        "AIEditNeverSilentlyDropped",
        "INVARIANT",
        [
            (
                'ELSE IF OwnerOf(s.prov, h) = "ai" /\\ ~replaced THEN "ai"',
                'ELSE IF OwnerOf(s.prov, h) = "ai" /\\ ~replaced THEN "none"',
            )
        ],
    ),
    "undo-restores-pre-converge-document": (
        "UndoOnLatestBase",
        "INVARIANT",
        [
            (
                'IF replaced /\\ OwnerOf(s.prov, h) = "creator" THEN [on |-> TRUE, gen |-> g, prov |-> "creator"]',
                'IF replaced /\\ OwnerOf(s.prov, h) = "creator" THEN [on |-> TRUE, gen |-> s.clean, prov |-> "creator"]',
            )
        ],
    ),
    "reads-never-start": (
        "ShowsLatest",
        "PROPERTY",
        [("WF_s(ReadStart) /\\ ", "")],
    ),
    "gesture-never-ends": (
        "SaveEventuallyEnabled",
        "PROPERTY",
        [(" /\\ WF_s(EndInteract)", "")],
    ),
}


def mutate(text: str, edits: list[tuple[str, str]], name: str) -> str:
    for old, new in edits:
        if text.count(old) != 1:
            sys.exit(f"FAIL: control {name} no longer matches Converge.tla: {old!r}")
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------- drift


def swift_members(text: str, name: str) -> list[str]:
    """Every `func name` / `var name` body (brace matched) in a Swift file."""
    out = []
    for m in re.finditer(r"\b(?:func|var)\s+" + re.escape(name) + r"\b", text):
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


def member_hashes() -> dict[str, str]:
    mapped = json.loads(SOURCE_MAP.read_text())["members"]
    out = {}
    for rel, names in mapped.items():
        text = (REPO / rel).read_text()
        for name in names:
            bodies = swift_members(text, name)
            if not bodies:
                sys.exit(f"DRIFT: {rel}:{name} no longer exists; update the model and the map")
            out[f"{rel}:{name}"] = hashlib.sha256("\n".join(bodies).encode()).hexdigest()[:16]
    return out


def check_drift(resync: bool) -> bool:
    current = member_hashes()
    stored = json.loads(SOURCE_MAP.read_text())
    if resync:
        stored["hashes"] = current
        SOURCE_MAP.write_text(json.dumps(stored, indent=2) + "\n")
        print(f"resynced {len(current)} source hashes")
        return False
    changed = sorted(k for k in current if stored.get("hashes", {}).get(k) != current[k])
    for key in changed:
        print(f"DRIFT {key}: re-read Converge.tla against it, then --resync")
    return bool(changed)


# ---------------------------------------------------------------- run


def main() -> int:
    resync = "--resync" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    verify.OUT = OUT
    verify.JAR = verify.tlc_jar()
    verify.JAVA = verify.java()
    report: dict = {"spec": None, "controls": {}}

    checks = [("INVARIANT", r) for r in SAFETY] + [("PROPERTY", r) for r in LIVENESS]
    real = verify.tlc(SPEC, CONSTANTS, checks, "Spec", "spec", expect_pass=True)
    report["spec"] = real
    if real["broken"]:
        sys.exit(f"FAIL: Converge.tla breaks {real['broken']}; trace in {real['log']}")
    print(f"PASS spec: {len(checks)} rules, {real['states']} states, {real['seconds']}s")

    for name, (rule, kind, edits) in CONTROLS.items():
        mutant = OUT / "mutant" / name / "Converge.tla"
        mutant.parent.mkdir(parents=True, exist_ok=True)
        mutant.write_text(mutate(SPEC.read_text(), edits, name))
        result = verify.tlc(mutant, CONSTANTS, [(kind, rule)], "Spec", f"control-{name}")
        report["controls"][name] = {"rule": rule, **result}
        caught = result["broken"] == [rule]
        print(
            f"{'PASS' if caught else 'FAIL'} control {name}: {rule} "
            + ("fails as expected" if caught else f"got {result['broken']}")
            + f" ({result['states']} states, trace {result['trace']}, {result['seconds']}s)"
        )
        if not caught:
            (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            sys.exit(f"FAIL: {rule} does not catch {name}, so it proves nothing there")

    drift = check_drift(resync)
    report["drift"] = drift
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS" + (" with DRIFT to review" if drift else ""))
    return 3 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
