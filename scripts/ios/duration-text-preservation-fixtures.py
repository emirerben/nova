#!/usr/bin/env python3
"""Regenerate synthetic native timing fixtures through the backend compiler."""

import json
import os
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src/apps/api"))
    os.environ.setdefault("STORAGE_BUCKET", "synthetic")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql://synthetic:synthetic@localhost/synthetic"
    )

    import pytest

    from app.services.kria_editor_ops import (
        build_editor_snapshot,
        compile_editor_ops,
        project_editor_draft,
    )
    from app.kria.editor_sequence_timing import sequence_timing_plan
    from tests._prod_profile import apply_prod_profile
    from tests.services.test_duration_text_preservation import sequence

    words = (
        "apple beach candle dawn eagle forest garden harbor island jungle kettle lemon meadow "
        "nectar ocean paper quartz river sunset tulip velvet willow yellow zebra amber cloud silver"
    ).split()
    cases = []
    for name, target, end in (
        ("duration20", 20, 10.8),
        ("duration12", 12, 10.8),
        ("retime12", 12, 12.876),
        ("retimeUneven20", 20, 12.876),
        ("retimeAfterResize20", 20, 12.876),
    ):
        with pytest.MonkeyPatch.context() as patch:
            apply_prod_profile(patch)
            job, variant = sequence(patch, end_s=end)
            for row, word in zip(variant["text_elements"], words, strict=True):
                row["text"] = word
            if name.startswith("retimeUneven") or name.startswith("retimeAfter"):
                cursor = 0.0
                for i, row in enumerate(variant["text_elements"]):
                    row["start_s"] = round(cursor, 6)
                    cursor += 0.2 + 0.05 * (i % 4)
                    row["end_s"] = round(cursor, 6)
                    cursor += 0.2
            from copy import deepcopy

            before = deepcopy(variant["text_elements"])
            if name == "retimeAfterResize20":
                initial_plan = sequence_timing_plan(
                    "Make the video 12 seconds and fill the text across the whole video",
                    build_editor_snapshot(job, variant),
                )
                initial = compile_editor_ops(
                    job, variant, list(initial_plan.operations)
                )
                variant = project_editor_draft(
                    variant,
                    initial.payload.model_dump(mode="json", exclude_none=True),
                    job,
                )
            operations = [{"op": "set_total_duration", "target_s": target}]
            if name.startswith("retime"):
                plan = sequence_timing_plan(
                    f"Make the video {target} seconds and fill the text across the whole video",
                    build_editor_snapshot(job, variant),
                )
                if plan is None or not plan.operations:
                    raise AssertionError(
                        "Synthetic natural-language route must produce a plan"
                    )
                operations = list(plan.operations)
            result = compile_editor_ops(job, variant, operations)
            cases.append(
                dict(
                    name=name,
                    duration=target,
                    before=before,
                    operations=operations,
                    payload=result.payload.model_dump(mode="json", exclude_none=True),
                )
            )
    output = root / "src/apps/ios/Tests/Fixtures/DurationTextPreservation.json"
    output.write_text(json.dumps(cases, indent=2) + "\n")


if __name__ == "__main__":
    main()
