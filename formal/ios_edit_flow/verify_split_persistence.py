"""Check the bounded split persistence contract without clearing lifecycle drift."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import verify

HERE = Path(__file__).resolve().parent
SOURCE_MAP = HERE / "split-persistence-source-map.json"
SPEC = HERE / "SplitScreenPersistence.tla"
PROPERTIES = [
    "RequestPreservesSubmission",
    "DurableMatchesSubmission",
    "PostSubmitEditPreserved",
    "ValidCommit",
    "ValidAdoption",
    "MergeIsCoupled",
    "AuthoritativeMembership",
    "AssetProvenancePreserved",
    "CleanLoad",
    "LayoutEditIsDirty",
    "OrdinaryFallbackClearsDefinitions",
]
MUTATIONS = {
    "OmitDefinitions": "RequestPreservesSubmission",
    "DropEmptyDefinitions": "RequestPreservesSubmission",
    "IgnoreLayoutDirty": "LayoutEditIsDirty",
    "SplitMergeBundle": "MergeIsCoupled",
    "AppendRemovedMembers": "AuthoritativeMembership",
    "AckCurrent": "PostSubmitEditPreserved",
    "OverwriteCurrentOnAck": "PostSubmitEditPreserved",
    "AllowInvalidSave": "ValidCommit",
    "AllowInvalidAdoption": "ValidAdoption",
    "OverwriteCanonicalAsset": "AssetProvenancePreserved",
    "DropLegacyAsset": "AssetProvenancePreserved",
    "RetainFallbackDefinitions": "OrdinaryFallbackClearsDefinitions",
    "DirtyOnLoad": "CleanLoad",
    "WrongSourceOnLoad": "CleanLoad",
}
WITNESSES = [
    "NeverSplitSave",
    "NeverSplitReopen",
    "NeverUnstack",
    "NeverLayoutSave",
    "NeverConcurrentEdit",
    "NeverAIRemoval",
    "NeverMergeResolution",
    "NeverMergeAdoption",
    "NeverCanonicalSlotLoad",
    "NeverCanonicalAliasLoad",
    "NeverFallbackLoad",
    "NeverLegacyLoad",
    "NeverUnrepresentedLoad",
    "NeverOrdinaryFallback",
]


def check_source() -> dict:
    mapping = json.loads(SOURCE_MAP.read_text())
    if not mapping["files"]:
        raise SystemExit("FAIL split persistence: empty source map")
    drift = []
    for entry in mapping["files"]:
        path = verify.REPO / entry["path"]
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]
        ):
            drift.append(entry["path"])
    if drift:
        raise SystemExit(
            "DRIFT split persistence: review source correspondence before repinning:\n  "
            + "\n  ".join(drift)
        )
    return mapping


def run(engine=verify) -> dict:
    mapping = check_source()
    engine.OUT.mkdir(parents=True, exist_ok=True)
    constants = {name: "FALSE" for name in MUTATIONS}
    current = engine.tlc(
        SPEC,
        constants,
        [("INVARIANT", name) for name in PROPERTIES],
        "Spec",
        "split-persistence-current",
        expect_pass=True,
    )
    if current["broken"]:
        raise SystemExit("FAIL split persistence: " + str(current))
    mutations = {}
    controls = [(name, name, invariant) for name, invariant in MUTATIONS.items()]
    controls.append(
        ("OmitDefinitionsAtAck", "OmitDefinitions", "DurableMatchesSubmission")
    )
    for name, switch, invariant in controls:
        result = engine.tlc(
            SPEC,
            {**constants, switch: "TRUE"},
            [("INVARIANT", invariant)],
            "Spec",
            "split-persistence-mutant-" + name,
        )
        if result["broken"] != [invariant]:
            raise SystemExit("FAIL uncaught split persistence mutation: " + name)
        mutations[name] = result
        print(f"caught {name}: {invariant}", flush=True)
    witnesses = {}
    for invariant in WITNESSES:
        result = engine.tlc(
            SPEC,
            constants,
            [("INVARIANT", invariant)],
            "Spec",
            "split-persistence-witness-" + invariant,
        )
        if result["broken"] != [invariant]:
            raise SystemExit("FAIL unreachable split persistence witness: " + invariant)
        witnesses[invariant] = result
        print(f"reachable {invariant.removeprefix('Never')}", flush=True)
    report = {
        "scope": mapping["scope"],
        "reviewed_source_revision": mapping["reviewed_source_revision"],
        "observed_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=engine.REPO,
            text=True,
        ).strip(),
        "source_map_sha256": hashlib.sha256(SOURCE_MAP.read_bytes()).hexdigest(),
        "model_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
        "current": current,
        "mutations": mutations,
        "witnesses": witnesses,
        "aggregate_lifecycle_admission": False,
    }
    (engine.OUT / "split-persistence-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(
        f"PASS bounded split persistence: {len(PROPERTIES)} invariants, "
        f"{len(mutations)} independent mutations, {len(witnesses)} reachability controls. "
        "Inherited lifecycle drift is unchanged.",
        flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-source", action="store_true")
    args = parser.parse_args()
    mapping = check_source()
    if args.check_source:
        print(
            f"PASS scoped source map: {len(mapping['files'])} files match the reviewed source"
        )
    else:
        verify.JAR = verify.tlc_jar()
        verify.JAVA = verify.java()
        run()
