"""Bounded companion checks for saved-render receipt and late-save isolation."""

import json
from pathlib import Path
import verify

PROPERTIES = ["ExportMatchesSaved", "NoUnresolvedExport", "ReplacementEditorUntouched"]


def run(engine=verify):
    constants = {
        "LegacyReceipt": "FALSE",
        "AllowUnresolved": "FALSE",
        "LateSaveWrite": "FALSE",
        "LegacyWindow": '"cancel"',
    }
    spec = Path(__file__).with_name("SavedRenderReceipt.tla")
    result = engine.tlc(
        spec,
        constants,
        [("INVARIANT", p) for p in PROPERTIES],
        "Spec",
        "receipt-current",
        expect_pass=True,
    )
    if result["broken"]:
        raise SystemExit("FAIL saved receipt: " + str(result))
    controls = {}
    for name, changes, prop in [
        ("LegacyCancel", {"LegacyReceipt": "TRUE"}, "ExportMatchesSaved"),
        (
            "LegacySnapshot",
            {"LegacyReceipt": "TRUE", "LegacyWindow": '"snapshot"'},
            "ExportMatchesSaved",
        ),
        ("AllowUnresolved", {"AllowUnresolved": "TRUE"}, "NoUnresolvedExport"),
        ("LateSaveWrite", {"LateSaveWrite": "TRUE"}, "ReplacementEditorUntouched"),
    ]:
        control = engine.tlc(
            spec,
            {**constants, **changes},
            [("INVARIANT", prop)],
            "Spec",
            "receipt-mutant-" + name,
        )
        if prop not in control["broken"]:
            raise SystemExit("FAIL uncaught saved receipt mutation: " + name)
        controls[name] = control
    reachable = engine.tlc(
        spec,
        constants,
        [("INVARIANT", "NeverNewExport")],
        "Spec",
        "receipt-new-export-control",
    )
    if "NeverNewExport" not in reachable["broken"]:
        raise SystemExit("FAIL saved receipt model never exported the replacement")
    report = {
        "current": result,
        "mutations": controls,
        "replacement_export_reachable": reachable,
    }
    (engine.OUT / "saved-receipt-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(
        "PASS saved receipt: 3 invariants, 4 separate mutation runs caught, replacement export reachable"
    )
    return report


if __name__ == "__main__":
    verify.JAR = verify.tlc_jar()
    verify.JAVA = verify.java()
    run()
