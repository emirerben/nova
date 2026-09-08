from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[5] / "scripts" / "check_gcs_lifecycle_drift.py"
SPEC = importlib.util.spec_from_file_location("check_gcs_lifecycle_drift", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
POLICY = SCRIPT.parents[1] / "infra" / "gcs-lifecycle.json"


def test_lifecycle_normalization_ignores_rule_and_prefix_order() -> None:
    left = {
        "lifecycle": {
            "rule": [
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 1, "matchesPrefix": ["staging/", "uploads/"]},
                },
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 30, "matchesPrefix": ["jobs/"]},
                },
            ]
        }
    }
    right = {
        "lifecycle": {
            "rule": [
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 30, "matchesPrefix": ["jobs/"]},
                },
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 1, "matchesPrefix": ["uploads/", "staging/"]},
                },
            ]
        }
    }
    assert MODULE._normalized(left) == MODULE._normalized(right)


def test_lifecycle_normalization_detects_age_or_prefix_drift() -> None:
    expected = {
        "lifecycle": {
            "rule": [
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 1, "matchesPrefix": ["staging/"]},
                }
            ]
        }
    }
    live = {
        "lifecycle": {
            "rule": [
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 2, "matchesPrefix": ["staging/"]},
                }
            ]
        }
    }
    assert MODULE._normalized(expected) != MODULE._normalized(live)


def test_product_output_prefixes_are_not_deleted_by_bucket_age() -> None:
    policy = json.loads(POLICY.read_text())
    configured_prefixes = {
        prefix
        for rule in policy["lifecycle"]["rule"]
        for prefix in rule["condition"].get("matchesPrefix", [])
    }

    assert configured_prefixes.isdisjoint({"jobs/", "music-jobs/", "auto-music-jobs/"})
