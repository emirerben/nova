#!/usr/bin/env python3
"""Unit tests that render, export and read back real video (stdlib only).

These XCTest cases build a live preview, export an MP4 through the device
exporter and read frames back, several with Vision text recognition on every
word. On the hosted simulator they are the slowest part of the unit suite:
after KRI-1060 (#1952) and the reference-style save proof (#1960) landed on
2026-10-30, the PR unit leg's test execution went from a median of ~12 min to
~22 min while compilation stayed at ~6 min and the suite grew ~3%.

Pull requests skip them unless the diff touches the render, export or text
code they prove (or the tests and fixtures themselves). Every push to main
(ios.yml) and the nightly runs still execute the whole unit suite, so a
regression these proofs catch surfaces on the merge that caused it.

  render_evidence.py skip-args       -skip-testing lines for xcodebuild, minus
                                     tests named by the journeys in
                                     KRIA_JOURNEY_IDS
  render_evidence.py check           fail if a listed test no longer exists
"""

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO / "src/apps/ios/Tests/KriaTests"
JOURNEY_MANIFEST = REPO / "scripts/ci/journey-manifest.json"

# Class or Class/method identifiers inside the KriaTests bundle.
TESTS = (
    "DurationTextPreservationTests",
    "ReferenceStyleSaveTests",
    "RenderedOriginalClockTests",
    "SavedDraftPreviewRangeTests",
    "CaptionAnimationTests/testScopedCaptionsMatchSyntheticPreviewAndExportPixels",
)

# A pull request that changes any of these runs the tests above as well.
TRIGGER_PREFIXES = (
    "src/apps/ios/Packages/KriaMediaEngine/",
    # Native editor, text, font and glyph code (NativeEditor*, NativeText*, ...).
    "src/apps/ios/Kria/Core/Native",
    "src/apps/ios/Kria/Features/Native",
    "src/apps/ios/Kria/Core/DeviceRender",
    "src/apps/ios/Kria/Features/DeviceRender",
    "src/apps/ios/Kria/Features/TextRole",
    "src/apps/ios/Kria/Resources/",
    "src/apps/ios/project.yml",
    "src/apps/ios/Tests/KriaTests/DurationTextPreservationTests.swift",
    "src/apps/ios/Tests/KriaTests/ReferenceStyleSaveTests.swift",
    "src/apps/ios/Tests/KriaTests/RenderedOriginalClockTests.swift",
    "src/apps/ios/Tests/KriaTests/SavedDraftPreviewRangeTests.swift",
    "src/apps/ios/Tests/KriaTests/CaptionAnimationTests.swift",
    "src/apps/ios/Tests/Fixtures/DurationTextPreservation.json",
    "src/apps/ios/Tests/Fixtures/ReferenceStyleSave.json",
    "src/apps/web/public/fonts/",
)


def triggered(paths):
    """True when any changed path can change what these proofs observe."""
    return any(path.startswith(TRIGGER_PREFIXES) for path in paths)


def named_by_journeys(journey_ids, manifest=JOURNEY_MANIFEST):
    """Listed tests that a selected journey names: those must still run.

    journey-gate.py native-units requires every named journey test in the unit
    result bundle, so a selected journey keeps its proofs on the PR.
    """
    selected = {i for i in journey_ids.split(",") if i}
    if not selected:
        return set()
    journeys = json.loads(Path(manifest).read_text())["journeys"]
    named = {
        test.removeprefix("KriaTests/")
        for row in journeys
        if row["id"] in selected
        for test in row.get("xctests", [])
    }
    return {
        test
        for test in TESTS
        if any(name == test or name.startswith(test + "/") for name in named)
    }


def skip_args(journey_ids=""):
    keep = named_by_journeys(journey_ids)
    return [f"-skip-testing:KriaTests/{test}" for test in TESTS if test not in keep]


def missing(tests_dir=TESTS_DIR):
    """Listed identifiers that no longer name a class/method in KriaTests."""
    sources = {p.stem: p.read_text() for p in Path(tests_dir).glob("*.swift")}
    everything = "\n".join(sources.values())
    gone = []
    for test in TESTS:
        cls, _, method = test.partition("/")
        has_class = re.search(rf"\bclass {re.escape(cls)}\b", everything)
        has_method = not method or re.search(rf"\bfunc {re.escape(method)}\(", everything)
        if not (has_class and has_method):
            gone.append(test)
    return gone


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "skip-args":
        args = skip_args(os.environ.get("KRIA_JOURNEY_IDS", ""))
        if args:
            print("\n".join(args))
        return 0
    if command == "check":
        gone = missing()
        for test in gone:
            print(f"render_evidence.py lists {test}, which KriaTests no longer has", file=sys.stderr)
        return 1 if gone else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
