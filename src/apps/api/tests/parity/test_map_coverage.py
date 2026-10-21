"""KRI-786 guard: every language-sensitive decision in the parity map has a parity-table row.

The parity map (``docs/reviews/turkish-parity-map-2026-10.md``, section 3) lists every
rule-based decision that reads creator wording (map rows ``D01``..). The rule-based parity
table (``test_decision_parity.CASES``) runs English and Turkish phrasings through the real
functions. A map row with a status other than ``neutral`` must be either:

- covered by table cases (``COVERAGE``: map row -> table case ids; NOTE the map and the table
  number their D rows independently, map ``D02`` is table ``D12``), or
- marked ``NOT_UNIT_REACHABLE`` with a reason (needs a DB, a render, or a long fixture), or
- listed in ``NO_ROW_YET``: a shrink-only backlog of reachable decisions still waiting for a
  table row. Add the row when you fix (or touch) the decision, then move the map id to
  ``COVERAGE``.

So a decision added to the map later fails here until someone decides how it is tested.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.parity.test_decision_parity import CASES

_REPO = Path(__file__).resolve().parents[5]
MAP_PATH = _REPO / "docs" / "reviews" / "turkish-parity-map-2026-10.md"
_ROW = re.compile(r"^\|\s*(D\d{2})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|")

# map row -> parity-table case ids that exercise it.
COVERAGE: dict[str, tuple[str, ...]] = {
    "D01": ("D01",),
    "D02": ("D12",),
    "D03": ("D15", "D16"),
    "D06": ("D14",),
    "D08": ("D18",),
    "D09": ("D17",),
    "D10": ("D13",),
    "D13": ("D24", "D25"),
    "D14": ("D19", "D20"),
    "D15": ("D19", "D20", "D21", "D22", "D23"),
    "D16": ("D05", "D06", "N04"),
    "D17": ("N01", "N02"),
    "D18": ("D02", "D03"),
    "D19": ("D07",),
    "D23": ("D10", "D11"),
    "D27": ("D35",),
    "D30": ("D26",),
    "D35": ("D29",),
    "D37": ("D27", "D28"),
    "D41": ("D38", "D39"),
    "D42": ("D40",),
    "D48": ("D36", "D37"),
    "D49": ("D31", "D32", "D33"),
    "D50": ("D34",),
    "D51": ("D30",),
}

NOT_UNIT_REACHABLE: dict[str, str] = {
    "D44": "closures inside _narrated_storyboard_text_elements; needs a full storyboard fixture",
}

# Reachable, no table row yet. Shrink-only.
NO_ROW_YET: frozenset[str] = frozenset(
    {
        "D04",  # creator_agent font cue words
        "D05",  # creator_agent text colour words
        "D07",  # creator_agent _SECONDS_WORDS
        "D11",  # plan_items _review_mixed_media_timing
        "D20",  # main_creator._explicit_media_scope_from_request
        "D21",  # creator_policy stated media count
        "D22",  # smart_edit planner list/ordinal events (TR-only)
        "D24",  # kria *_ask readers
        "D25",  # strategy_policy refusal
        "D26",  # editor_receipts timing cue
        "D28",  # edit_copilot guards
        "D29",  # _copilot honest-reply guard
        "D31",  # edit_proposal ungrounded-caption guard
        "D32",  # creator_sessions generic-SFX notice
        "D33",  # brief_checks unquoted pop-in trigger
        "D34",  # brief_checks whole-video title fallback
        "D36",  # brief_checks leading article
        "D38",  # semantic_edit_proposal quote harvest
        "D39",  # spoken_roles phrases
        "D40",  # spoken_quotes speech cues
        "D43",  # narration_labels score words
        "D45",  # generative_build _creator_requests_narrated_treatment
        "D46",  # visual_treatment_planner section numbers
        "D47",  # intro_writer / voiceover_script_writer refusal patterns
    }
)


def _map_rows() -> dict[str, tuple[str, str]]:
    """``map id -> (file column, status column)`` for every section-3 decision row."""
    rows = {}
    for line in MAP_PATH.read_text(encoding="utf-8").splitlines():
        if match := _ROW.match(line):
            rows[match.group(1)] = (match.group(2).strip(), match.group(5).strip())
    return rows


def _language_sensitive(status: str) -> bool:
    return not status.lower().startswith("neutral")


def test_every_language_sensitive_map_decision_is_tabled_or_explained():
    rows = _map_rows()
    assert len(rows) >= 51, f"parsed only {len(rows)} map D rows; did the table format change?"
    handled = set(COVERAGE) | set(NOT_UNIT_REACHABLE) | NO_ROW_YET
    missing = sorted(
        rid
        for rid, (_, status) in rows.items()
        if _language_sensitive(status) and rid not in handled
    )
    assert not missing, (
        f"KRI-786: parity-map decisions with no parity-table row: {missing}. Add a Case to "
        "tests/parity/test_decision_parity.py (gap variants as strict xfail) and map it in "
        "COVERAGE, or record why it cannot be unit-tested in NOT_UNIT_REACHABLE."
    )


def test_coverage_points_at_real_table_rows_in_the_right_module():
    rows = _map_rows()
    cases = {case.id: case for case in CASES}
    problems = []
    for rid, case_ids in COVERAGE.items():
        if rid not in rows:
            problems.append(f"{rid}: not in the map")
            continue
        file_column = rows[rid][0]
        for cid in case_ids:
            case = cases.get(cid)
            if case is None:
                problems.append(f"{rid}: no table case {cid}")
                continue
            module = "/".join(case.decision.split(".")[:-1]) + ".py"
            if module not in file_column:
                problems.append(f"{rid}: case {cid} tests {module}, map row is {file_column}")
    assert not problems, problems


def test_buckets_do_not_overlap():
    buckets = [set(COVERAGE), set(NOT_UNIT_REACHABLE), set(NO_ROW_YET)]
    for i, a in enumerate(buckets):
        for b in buckets[i + 1 :]:
            assert not a & b, sorted(a & b)
