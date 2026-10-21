"""Real provider-free CLI journey; synthetic judgments prove mechanics, not taste."""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
CLI = ROOT / "scripts/editorial-taste-eval.py"
TASTE = ROOT / "scripts/editorial-taste.py"
OWNER = "11111111-1111-1111-1111-111111111111"
TRAIN = "22222222-2222-2222-2222-222222222222"
HELD = "33333333-3333-3333-3333-333333333333"
CASE = "44444444-4444-4444-4444-444444444444"
MOMENT = "55555555-5555-5555-5555-555555555555"


def file(path, value):
    path.write_text(json.dumps(value))
    return str(path)


def run(store, *args, ok=True, cli=CLI):
    result = subprocess.run(
        [sys.executable, str(cli), "--store", str(store), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == ok, result.stderr + result.stdout
    return json.loads(result.stdout) if ok else result.stderr


@pytest.fixture
def world(tmp_path):
    # /private/tmp avoids macOS /tmp symlink parents forbidden by the private store.
    tmp_path = tmp_path.resolve()
    corpus = tmp_path / "corpus"
    capture = {
        "owner_id": OWNER,
        "captured_at": "2026-10-21T00:00:00+00:00",
        "examples": [
            {
                "owner_id": OWNER,
                "project_id": TRAIN,
                "title": "A real outing",
                "kind": "video",
                "words": ["A short observation"],
                "artifact": {"availability": "saved_metadata"},
            }
        ],
    }
    run(
        corpus,
        "import",
        "--input",
        file(tmp_path / "capture.json", capture),
        "--owner",
        OWNER,
        "--reviewer",
        "Fixture creator",
        "--approval",
        "Use this example",
        cli=TASTE,
    )
    run(corpus, "holdout", "--project", HELD, cli=TASTE)
    head = json.loads((corpus / "HEAD.json").read_text())
    value = json.loads((corpus / "revisions" / head["file"]).read_text())
    pack = {
        "id": str(uuid.uuid4()),
        "owner_id": OWNER,
        "purpose": "synthetic_workflow_check",
        "corpus_sha256": head["sha256"],
        "arms": {
            "baseline": {"model": "recorded", "prompt_sha256": "a" * 64},
            "personalized": {"model": "recorded", "prompt_sha256": "b" * 64},
        },
        "source_registry": [
            {
                "project_id": TRAIN,
                "source_sha256": value["examples"][0]["source_sha256"],
                "coverage": "complete",
                "footage_ids": ["c" * 64],
                "group_ids": ["first-outing"],
            }
        ],
        "cases": [
            {
                "id": CASE,
                "project_id": HELD,
                "footage_ids": ["d" * 64],
                "group_ids": ["new-outing"],
                "query": "A short outing idea",
                "candidates": [
                    {
                        "id": MOMENT,
                        "source": "66666666-6666-6666-6666-666666666666",
                        "kind": "visual",
                        "text": "An arrival",
                        "start_s": 0,
                        "end_s": 5,
                    }
                ],
            }
        ],
        "criteria": {
            "primary": "creator_preference",
            "secondary": ["would_post", "unsupported_claims", "footage_mismatch"],
        },
    }
    return tmp_path, corpus, tmp_path / "evaluation", pack


def output(title="A simple arrival"):
    return {
        "status": "ready",
        "output": {
            "stories": [
                {
                    "title": title,
                    "hook": "A morning arrival",
                    "payoff": "A small outing",
                    "rationale": "A specific everyday moment.",
                    "moment_ids": [MOMENT],
                    "content_plan": {
                        "audience": "Travelers",
                        "angle": "An arrival",
                        "why_it_works": "A small observation.",
                        "opening_text": "A morning arrival",
                        "beat_reasons": ["Shows the arrival"],
                    },
                }
            ]
        },
    }


def init(world, *, pack=None, ok=True):
    tmp, corpus, store, default = world
    return run(
        store,
        "preregister",
        "--input",
        file(tmp / "pack.json", pack or default),
        "--corpus",
        str(corpus),
        ok=ok,
    )


def test_blind_comparison_retry_fences_and_complete_denominator(world):
    tmp, corpus, store, pack = world
    registered = init(world)
    assert registered["cases"] == 1 and registered["provider_calls"] == 0
    for arm in ("baseline", "personalized"):
        run(
            store,
            "output",
            "--case",
            CASE,
            "--arm",
            arm,
            "--input",
            file(tmp / (arm + ".json"), output(arm + " title")),
        )
    cards = run(store, "cards", "--case", CASE)
    assert {v["label"] for v in cards["options"]} == {"A", "B"}
    assert "model" not in json.dumps(cards) and "prompt_sha256" not in json.dumps(cards)
    # Titles above deliberately identify their arm so this fixture can verify mapping;
    # real provider outputs are never rewritten to insert an identity.
    chosen = next(
        v["label"] for v in cards["options"] if v["stories"][0]["title"] == "personalized title"
    )
    judgment = {
        "cards_sha256": cards["cards_sha256"],
        "selected": [chosen],
        "would_post": {"A": True, "B": True},
        "problems": {"A": [], "B": []},
        "reason": "This is more specific",
        "reviewer": "Fixture creator",
        "creator_statement": "I would post this one",
    }
    before = run(store, "report", "--corpus", str(corpus))
    assert before["pending_judgments"] == 1 and not before["quality_evaluated"]
    path = file(tmp / "judgment.json", judgment)
    run(store, "judge", "--case", CASE, "--input", path)
    repeated = run(store, "judge", "--case", CASE, "--input", path)
    report = run(store, "report", "--corpus", str(corpus))
    assert repeated["recorded"] and report["judged_cases"] == 1
    assert report["preference_counts"]["personalized"] == 1
    assert report["total_cases"] == 1 and report["pending_judgments"] == 0
    assert not report["independent_quality_pass"] and not report["fine_tuning_ready"]
    altered = {**judgment, "reason": "Changed afterward"}
    run(store, "judge", "--case", CASE, "--input", file(tmp / "changed.json", altered), ok=False)
    run(
        store,
        "output",
        "--case",
        CASE,
        "--arm",
        "personalized",
        "--input",
        file(tmp / "different-output.json", output("Changed afterward")),
        ok=False,
    )


def test_overlap_partial_registry_and_wrong_owner_cannot_register(world):
    _, _, _, pack = world
    for changed in (
        {**pack, "owner_id": str(uuid.uuid4())},
        {**pack, "source_registry": []},
        {**pack, "source_registry": [{**pack["source_registry"][0], "coverage": "partial"}]},
        {**pack, "cases": [{**pack["cases"][0], "project_id": TRAIN}]},
        {**pack, "cases": [{**pack["cases"][0], "footage_ids": ["c" * 64]}]},
        {**pack, "cases": [{**pack["cases"][0], "group_ids": ["first-outing"]}]},
    ):
        init(world, pack=changed, ok=False)


def test_provider_failure_neither_and_changed_corpus_are_reported(world):
    tmp, corpus, store, _ = world
    init(world)
    for arm in ("baseline", "personalized"):
        run(
            store,
            "output",
            "--case",
            CASE,
            "--arm",
            arm,
            "--input",
            file(tmp / (arm + ".json"), {"status": "failed", "error_class": "ProviderTimeout"}),
        )
    cards = run(store, "cards", "--case", CASE)
    judgment = {
        "cards_sha256": cards["cards_sha256"],
        "selected": [],
        "would_post": {"A": False, "B": False},
        "problems": {"A": [], "B": []},
        "reason": "Neither returned an idea",
        "reviewer": "Fixture creator",
        "creator_statement": "Neither",
    }
    run(store, "judge", "--case", CASE, "--input", file(tmp / "judge.json", judgment))
    report = run(store, "report", "--corpus", str(corpus))
    assert report["preference_counts"]["neither"] == 1
    assert report["failed_outputs"] == {"baseline": 1, "personalized": 1}
    run(corpus, "holdout", "--project", str(uuid.uuid4()), cli=TASTE)
    report = run(store, "report", "--corpus", str(corpus))
    assert not report["corpus_unchanged"] and not report["independent_quality_pass"]


def test_unknown_source_and_stale_cards_cannot_be_judged(world):
    tmp, _, store, _ = world
    init(world)
    bad = output()
    bad["output"]["stories"][0]["moment_ids"] = [str(uuid.uuid4())]
    run(
        store,
        "output",
        "--case",
        CASE,
        "--arm",
        "baseline",
        "--input",
        file(tmp / "bad.json", bad),
        ok=False,
    )
    for arm in ("baseline", "personalized"):
        run(
            store,
            "output",
            "--case",
            CASE,
            "--arm",
            arm,
            "--input",
            file(tmp / (arm + ".json"), output()),
        )
    judgment = {
        "cards_sha256": "f" * 64,
        "selected": ["A"],
        "would_post": {"A": True, "B": True},
        "problems": {"A": [], "B": []},
        "reason": "Wrong cards",
        "reviewer": "Fixture creator",
        "creator_statement": "A",
    }
    run(store, "judge", "--case", CASE, "--input", file(tmp / "judge.json", judgment), ok=False)
