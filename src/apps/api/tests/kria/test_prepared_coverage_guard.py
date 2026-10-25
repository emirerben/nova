"""Guard: the prepared-coverage matrix and its documented table cannot drift (KRI-725).

The import-time check in ``app.kria.prepared_coverage`` already fails when an adapter
or a decision key has no row; this keeps the published table equal to the code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.kria.prepared_coverage import DECISION_KEYS, Coverage, coverage_table

DOC = Path(__file__).parents[5] / "docs" / "pipelines" / "creator-render-contract.md"


def test_documented_table_matches_the_code():
    text = DOC.read_text()
    match = re.search(
        r"<!-- prepared-coverage:start -->\n(.*?)\n<!-- prepared-coverage:end -->", text, re.S
    )
    assert match, "creator-render-contract.md lost its prepared-coverage table markers"
    assert match.group(1) == coverage_table()


@pytest.mark.parametrize(
    "decisions",
    [
        {k: "executed" for k in DECISION_KEYS if k != "audio"},
        {**{k: "executed" for k in DECISION_KEYS}, "audio": "unsupported"},
        {**{k: "executed" for k in DECISION_KEYS}, "audio": "gap:later"},
        {**{k: "executed" for k in DECISION_KEYS}, "text": "declined_before_render"},
    ],
    ids=["missing-key", "unknown-state", "gap-without-ticket", "decline-without-ticket"],
)
def test_a_row_cannot_hide_an_unaccounted_decision(decisions):
    with pytest.raises(ValueError):
        Coverage(
            adapter="probe",
            approval_path="prepared",
            reason="probe",
            pinned_inputs={},
            worker_fence="w",
            publication_fence="p",
            decisions=decisions,
        )
