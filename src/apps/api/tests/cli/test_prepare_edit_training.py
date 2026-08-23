from __future__ import annotations

import json

from app.cli.prepare_edit_training import main
from app.services.edit_training_dataset import DatasetCandidate, canonical_record


def test_prepare_edit_training_writes_generic_rows_without_upload(tmp_path):
    record = canonical_record(
        DatasetCandidate(
            artifact_id="artifact",
            creator_id="creator",
            plan_item_id="item",
            agent="edit_copilot",
            media_summary=[{"media_key": "m1", "kind": "video", "duration_s": 4.0}],
            user_intent="Make it fast",
            proposed={"operations": [{"type": "set_edit_direction"}]},
            execution={"outcome": "applied"},
            labels=[
                {
                    "dimension": "overall_quality",
                    "rating": "good",
                    "rationale": "Works",
                }
            ],
            versions={"prompt": "1.0.0"},
            lineage_parts=("item", "intent", "analysis"),
        ),
        secret="prepare-test-secret-at-least-16",
    )
    source = tmp_path / "dataset.jsonl"
    source.write_text(json.dumps(record.model_dump(mode="json")) + "\n")
    output = tmp_path / "prepared"

    assert (
        main(
            [
                "--dataset",
                str(source),
                "--agent",
                "edit_copilot",
                "--format",
                "generic",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    row = json.loads((output / "edit_copilot-generic.jsonl").read_text())
    assert row["agent"] == "edit_copilot"
    assert row["target"]["operations"][0]["type"] == "set_edit_direction"
    assert (output / "edit_copilot-preferences.jsonl").exists()
    assert (output / "edit_copilot-held-out-replays.jsonl").exists()
