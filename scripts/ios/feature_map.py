"""The verification skill's bounded, offline native journey selection."""

from fnmatch import fnmatchcase
import json
from pathlib import Path

import ui_tests

ROOT = Path(__file__).resolve().parents[2]
MAP = ".cursor/skills/verify-kria-ios/features/journeys.json"
FEATURES = {"projects", "creation", "teleprompter", "editor", "slides"}


def load_map(root=ROOT):
    data = json.loads((root / MAP).read_text())
    entries = data["features"]
    if (
        data["version"] != 1
        or len(entries) != 5
        or {entry["id"] for entry in entries} != FEATURES
    ):
        raise ValueError("The map must cover all five native features")
    inventory = ui_tests.discovered(root)
    for entry in entries:
        if (
            not entry["paths"]
            or not entry["tests"]
            or len(entry["tests"]) != len(set(entry["tests"]))
        ):
            raise ValueError(f"Empty verification feature: {entry['id']}")
        if {"KriaUITests/" + test for test in entry["tests"]} - inventory:
            raise ValueError(f"Verification map names missing tests: {entry['id']}")
    return entries


def is_ui_input(path):
    if path.endswith(".md") or path.startswith(
        ("src/apps/ios/Tests/KriaTests/", "scripts/ios/tests/")
    ):
        return False
    return path.startswith(
        ("src/apps/ios/", "scripts/ios/", ".cursor/skills/verify-kria-ios/")
    )


def features_for_paths(paths, entries=None):
    entries = load_map() if entries is None else entries
    selected = set()
    for path in filter(is_ui_input, paths):
        matches = {
            entry["id"]
            for entry in entries
            if any(fnmatchcase(path, pattern) for pattern in entry["paths"])
        }
        selected |= matches or FEATURES
    return [entry["id"] for entry in entries if entry["id"] in selected]


def tests_for_features(features, entries=None):
    entries = load_map() if entries is None else entries
    if not features or set(features) - FEATURES:
        raise ValueError("Unknown or empty verification features")
    return sorted(
        {
            test
            for entry in entries
            if entry["id"] in features
            for test in entry["tests"]
        }
    )
