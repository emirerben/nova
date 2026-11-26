"""KRI-1612: the blind-compare tool (`app/cli/compare_intro_scenes.py`).

Ways it could mislead Emir, each a test:
C1 a file name, the page or an image reveals which side is the model;
C2 the side of the model is the same every pair, or the order is the fixture order;
C3 the key can be edited after the picks without the score noticing;
C4 the tally counts a pair whose two sides are the same frame, or hides that the model drew
   today's title (a decline or a failure) instead of a scene;
C5 the tally credits the wrong arm (side mix-up);
C6 a demo run (synthetic plates, hand-authored answers) is mistaken for the compare.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import compare_intro_scenes as cmp


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("compare")
    result = cmp.build(cmp._demo_fixtures(), out, model="replay", seed=11)
    return out, result, json.loads((out / "key.json").read_text())


def test_nothing_visible_names_the_model(built):
    out, _result, key = built
    page = (out / "index.html").read_text().lower()
    names = [p.name.lower() for p in (out / "images").iterdir()]
    assert len(names) == 2 * len(key["pairs"])
    for text in [page, *names]:
        for word in ("model", "seeded", "seed", "gemini", "ai-pick"):
            assert word not in text.replace("key-sha256", "").replace("key.json", ""), word
    assert all(name.startswith("item-") and name.endswith(("-a.png", "-b.png")) for name in names)


def test_sides_and_order_are_shuffled(built):
    _out, _result, key = built
    sides = {pair["model_side"] for pair in key["pairs"]}
    assert sides == {"L", "R"}
    order = [pair["fixture"] for pair in key["pairs"]]
    assert order != [f["id"] for f in cmp._demo_fixtures()]
    assert [pair["n"] for pair in key["pairs"]] == list(range(1, len(order) + 1))


def test_the_key_is_sealed_by_the_page_fingerprint(built, tmp_path):
    out, result, _key = built
    assert result["key_sha256"] in (out / "index.html").read_text()
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps({str(n): "tie" for n in range(1, 7)}))
    cmp.score(out / "key.json", picks)  # untouched: fine
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "index.html").write_text((out / "index.html").read_text())
    data = json.loads((out / "key.json").read_text())
    data["pairs"][0]["model_side"] = "L" if data["pairs"][0]["model_side"] == "R" else "R"
    (forged / "key.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    with pytest.raises(SystemExit):
        cmp.score(forged / "key.json", picks)


def test_the_key_records_what_each_side_really_was(built):
    _out, _result, key = built
    by = {p["fixture"]: p for p in key["pairs"]}
    assert by["demo-6"]["model_no_scene"] and by["demo-6"]["model_outcome"] == "decline"
    assert not by["demo-1"]["model_no_scene"] and not by["demo-1"]["identical"]
    assert all(p["source"] == "synthetic_plate" and p["mode"] == "replay" for p in key["pairs"])


def test_score_credits_the_right_arm_and_names_the_no_scene_pairs(built, tmp_path):
    out, _result, key = built
    picks = {}
    for pair in key["pairs"]:
        picks[str(pair["n"])] = pair["model_side"]  # always choose the model's side
    path = tmp_path / "all_model.json"
    path.write_text(json.dumps(picks))
    tally = cmp.score(out / "key.json", path)
    assert tally["model"] == len(key["pairs"]) - tally["identical_excluded"]
    assert tally["seeded"] == 0 and tally["demo"] is True
    assert "demo-6" in tally["model_no_scene_pairs"]
    opposite = {
        n: ("L" if side == "R" else "R")
        for n, side in ((p["n"], p["model_side"]) for p in key["pairs"])
    }
    path.write_text(json.dumps({str(n): s for n, s in opposite.items()}))
    flipped = cmp.score(out / "key.json", path)
    assert flipped["seeded"] == tally["model"] and flipped["model"] == 0


def test_identical_pairs_are_left_out_and_unanswered_are_counted(built, tmp_path):
    out, _result, key = built
    forced = json.loads((out / "key.json").read_text())
    forced["pairs"][0]["identical"] = True
    work = tmp_path / "w"
    work.mkdir()
    (work / "key.json").write_text(json.dumps(forced))
    picks = work / "picks.json"
    picks.write_text(json.dumps({str(p["n"]): None for p in forced["pairs"]}))
    tally = cmp.score(work / "key.json", picks)  # no index.html beside it: no seal to check
    assert tally["identical_excluded"] == 1
    assert tally["unanswered"] == len(forced["pairs"]) - 1
    assert tally["enough_pairs"] is False


def test_the_enough_pairs_bar_is_the_tickets_five(built, tmp_path):
    out, _result, key = built
    picks = tmp_path / "p.json"
    picks.write_text(json.dumps({str(p["n"]): "tie" for p in key["pairs"]}))
    assert cmp.score(out / "key.json", picks)["enough_pairs"] is True
    assert cmp.MIN_PAIRS == 5


def test_the_demo_cannot_run_live(capsys):
    with pytest.raises(SystemExit):
        cmp.main(["build", "--demo", "--model", "live", "--out", "x"])
    assert Path("x").exists() is False
