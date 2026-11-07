#!/usr/bin/env python3
"""Separate bounded companion. Explicit provisioned tools; never installs tools.

Run only with an allocated formal lane. Existing verification remains unchanged.
Exit 0 means bounded policy checks and named negative controls passed.
This does not prove app lifecycle, arbitrary retries, or JSON merge fidelity.
"""

import hashlib
import json
import os
from pathlib import Path
import sys

import verify

MODEL = Path(__file__).with_name("MergeDecision.tla")
BASE = dict(
    Mutation='"None"',
    Origin='"rendered"',
    Proven="FALSE",
    Overlap="TRUE",
    CrossAuthority="TRUE",
    SongChanges="FALSE",
    AllowReplacement="TRUE",
    AllowOwnerChange="TRUE",
    QueuePoint='"none"',
    ReadKind='"latest"',
)
SAFETY = [
    "TypeOK",
    "HistoricalSnapshot",
    "NoReviewSave",
    "NoReviewPlay",
    "NoReviewExport",
    "NoReviewMutation",
    "ExecutableAuthority",
    "NoStaleDecision",
    "NoStaleObservation",
    "NoUnprovenOverwrite",
]


def main():
    # Check existence BEFORE invoking the shared helper, which otherwise downloads.
    for name in ("JAVA", "TLA2TOOLS"):
        if not os.environ.get(name) or not Path(os.environ[name]).is_file():
            sys.exit(
                f"FAIL: set {name} to an existing approved tool; no installation attempted"
            )
    verify.OUT = verify.REPO / ".dev/formal-merge-decision-owner"
    verify.OUT.mkdir(parents=True, exist_ok=True)
    verify.JAR = verify.tlc_jar()
    verify.JAVA = verify.java()
    results = []

    def check(tag, overrides, rule=None, expected=None, progress=False):
        constants = {**BASE, **overrides}
        checks = (
            [("PROPERTY" if progress else "INVARIANT", rule)]
            if rule
            else [("INVARIANT", item) for item in SAFETY]
        )
        result = verify.tlc(
            MODEL,
            constants,
            checks,
            "SpecProgress" if progress else "Spec",
            tag,
            expect_pass=expected is None,
        )
        results.append(
            dict(
                tag=tag,
                constants=constants,
                checks=checks,
                expected=expected,
                result=result,
            )
        )
        report = dict(
            model_sha256=hashlib.sha256(MODEL.read_bytes()).hexdigest(),
            scope="bounded policy; opaque documents; not JSON merge or app lifecycle proof",
            source_head="5075deb1cc36975e6aef0f224d60b4ffb7f6854e",
            scope_limit="one read; retry/catch and actual app lifecycle require native evidence",
            results=results,
        )
        (verify.OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        wanted = [] if expected is None else [expected]
        if result["broken"] != wanted:
            sys.exit(f"FAIL: {tag}: expected {wanted}, got {result['broken']}")
        print(f"PASS {tag}", flush=True)

    for tag, overrides in [
        ("rendered", {}),
        ("rendered-song", {"SongChanges": "TRUE"}),
        ("same-authority", {"CrossAuthority": "FALSE"}),
        (
            "chat-proven",
            {
                "Origin": '"chat"',
                "Proven": "TRUE",
                "Overlap": "FALSE",
                "CrossAuthority": "FALSE",
            },
        ),
        (
            "chat-unproven",
            {"Origin": '"chat"', "Overlap": "FALSE", "CrossAuthority": "FALSE"},
        ),
        ("prompt-fences", {"ReadKind": '"prompt"'}),
    ]:
        check(tag, overrides)
    mutations = {
        "ReviewSave": "NoReviewSave",
        "ReviewPlay": "NoReviewPlay",
        "ReviewExport": "NoReviewExport",
        "ReviewMutation": "NoReviewMutation",
        "PartialUndo": "HistoricalSnapshot",
        "NakedRechoice": "ExecutableAuthority",
        "IgnoreDecisionOwner": "NoStaleDecision",
        "IgnoreRedoOwner": "NoStaleDecision",
        "StaleObservation": "NoStaleObservation",
        "UnprovenAuto": "NoUnprovenOverwrite",
    }
    for mutation, rule in mutations.items():
        overrides = {"Mutation": json.dumps(mutation)}
        if mutation == "UnprovenAuto":
            overrides.update(Origin='"chat"', CrossAuthority="FALSE", Overlap="FALSE")
        check("mutant-" + mutation, overrides, rule, rule)
    witnesses = {
        "NeverResolved": {},
        "NeverReviewedAndRedone": {},
        "NeverNormalSave": {},
        "NeverNormalPlay": {},
        "NeverNormalExport": {},
        "NeverNormalMutation": {},
        "NeverReplacementExport": {},
        "NeverDrainedAfterPending": {"QueuePoint": '"pending"'},
        "NeverDrainedAfterReview": {"QueuePoint": '"review"'},
    }
    for rule, overrides in witnesses.items():
        check("witness-" + rule, overrides, rule, rule)
    for point in ("pending", "review"):
        stable = dict(
            QueuePoint=json.dumps(point),
            AllowReplacement="FALSE",
            AllowOwnerChange="FALSE",
        )
        check("progress-" + point, stable, "QueuedObservationConverges", progress=True)
        check(
            "mutant-NoRearm-" + point,
            {**stable, "Mutation": '"NoRearm"'},
            "QueuedObservationConverges",
            "QueuedObservationConverges",
            progress=True,
        )
    check("latest-owner-fence", {}, "NoOldOwnerCompletion")
    check(
        "mutant-LatestOwnerFence",
        {"Mutation": '"LatestOwnerFence"'},
        "NoOldOwnerCompletion",
        "NoOldOwnerCompletion",
    )
    check("prompt-owner-fence", {"ReadKind": '"prompt"'}, "NoOldOwnerCompletion")
    check(
        "mutant-PromptOwnerFence",
        {"ReadKind": '"prompt"', "Mutation": '"PromptOwnerFence"'},
        "NoOldOwnerCompletion",
        "NoOldOwnerCompletion",
    )
    print("PASS: bounded merge decision policy and owner fence controls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
