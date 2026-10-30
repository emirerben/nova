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

    from app.services.kria_editor_ops import compile_editor_ops
    from tests._prod_profile import apply_prod_profile
    from tests.services.test_duration_text_preservation import retime, sequence

    words = (
        "apple beach candle dawn eagle forest garden harbor island jungle kettle lemon meadow "
        "nectar ocean paper quartz river sunset tulip velvet willow yellow zebra amber cloud silver"
    ).split()
    cases = []
    for name, target, end in (
        ("duration20", 20, 10.8),
        ("duration12", 12, 10.8),
        ("retime12", 12, 12.876),
    ):
        with pytest.MonkeyPatch.context() as patch:
            apply_prod_profile(patch)
            job, variant = sequence(patch, end_s=end)
            for row, word in zip(variant["text_elements"], words, strict=True):
                row["text"] = word
            before = variant["text_elements"]
            operations = [{"op": "set_total_duration", "target_s": target}]
            if name == "retime12":
                operations.append(retime(before, 12))
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
