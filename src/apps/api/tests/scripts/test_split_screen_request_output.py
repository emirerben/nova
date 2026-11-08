"""Bridge checks use byte fixtures; only the native consumer can establish pixels."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "scripts/verify-split-screen-request-output.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("split_replay_bridge", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def receipts(tmp_path, bridge):
    scenario_bytes = bridge.SCENARIO.read_bytes()
    scenario = json.loads(scenario_bytes)
    rows = []
    for source in scenario["sources"]:
        path = tmp_path / (source["id"] + ".fixture")
        data = ("Parser-only source bytes " + source["id"]).encode()
        path.write_bytes(data)
        rows.append(
            {
                "media_id": source["id"],
                "path": path.name,
                "kind": source["kind"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
                "width": source["width"],
                "height": source["height"],
                "duration_s": source.get("duration_s"),
                "has_audio": source["kind"] == "video",
            }
        )
    payload = {
        "schema_version": 1,
        "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "sources": rows,
    }
    path = tmp_path / "source-receipts.json"
    path.write_text(json.dumps(payload))
    return path, payload


def test_authored_parser_policy_and_phone_compiler_keep_sources_and_boundaries(tmp_path):
    bridge = load_bridge()
    receipts(tmp_path, bridge)
    index = bridge.compile_replay(tmp_path)
    assert len(index["cases"]) == 4
    for case in index["cases"]:
        recipe = json.loads((tmp_path / case["recipe_path"]).read_bytes())
        count = int(case["id"][0])
        section = recipe["split_screen_sections"][0]
        assert (section["start"], section["end"], section["panel_weights"]) == (
            0.5,
            2.5,
            [1] * count,
        )
        clips = [clip for track in recipe["tracks"] for clip in track["clips"]]
        panels = sorted(
            (clip for clip in clips if clip.get("split_screen")),
            key=lambda clip: clip["split_screen"]["panel_index"],
        )
        expected = [("clock-a", 0.5)]
        if count == 3:
            expected.append(("clock-c", 1))
        expected.append(("visual-still-b", 0))
        if case["control"]:
            expected[0], expected[1] = expected[1], expected[0]
        assert [(clip["source_asset_id"], clip["source_start"]) for clip in panels] == expected
        ordinary = [clip for clip in clips if not clip.get("split_screen")]
        assert [(clip["timeline_start"], clip["source_duration"]) for clip in ordinary] == [
            (0, 0.5),
            (2.5, 1.5),
        ]
        assert all(clip["source_asset_id"] == "ordinary" for clip in ordinary)
        assert (
            hashlib.sha256((tmp_path / case["recipe_path"]).read_bytes()).hexdigest()
            == case["recipe_sha256"]
        )


@pytest.mark.parametrize("corruption", ["bytes", "scenario", "missing_source"])
def test_bridge_refuses_stale_receipts_before_writing_recipes(tmp_path, corruption):
    bridge = load_bridge()
    path, payload = receipts(tmp_path, bridge)
    if corruption == "bytes":
        (tmp_path / payload["sources"][0]["path"]).write_bytes(b"replaced")
    elif corruption == "scenario":
        payload["scenario_sha256"] = "0" * 64
    else:
        payload["sources"].pop()
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        bridge.compile_replay(tmp_path)
    assert not (tmp_path / "compiled-index.json").exists()
    assert not list(tmp_path.glob("*-recipe.json"))


def test_bridge_never_overwrites_a_prior_compilation(tmp_path):
    bridge = load_bridge()
    receipts(tmp_path, bridge)
    existing = tmp_path / "compiled-index.json"
    existing.write_text("retained evidence")
    with pytest.raises(FileExistsError):
        bridge.compile_replay(tmp_path)
    assert existing.read_text() == "retained evidence"
