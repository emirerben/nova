"""Per-fixture eval gate for nova.audio.retake_detector (plans/010 T7, KRI-1176).

Structural-only in CI (replay mode, no network). Live mode requires
GEMINI_API_KEY; the judge additionally requires ANTHROPIC_API_KEY.

Fixture coverage (every `raw_text` is a HAND-AUTHORED expected answer, never a
live capture — retake spans are not persisted, so there are no prod snapshots):
  - EN restart ("wait, let me start over") — abandoned span flagged
  - TR restart ("dur baştan alayım") — abandoned span flagged
  - rhetorical-repetition near-miss NEGATIVE — must return NO retakes
  - clean transcript NEGATIVE — must return NO retakes
  - empty transcript NEGATIVE — must return NO retakes
  - KRI-1176 precision set: two restarts on REAL recorded words (Green 3
    `green3_s2_restart`, N3 `n3_marathon_restart`; `meta.must_cut` /
    `meta.must_keep`) and eight restart-free NEGATIVES sliced from real recorded
    word lists in the repo (EN x4, TR x2, ES x2; `meta.source_fixture`).

The precision gate (`test_positive_fixtures_cut_only_the_abandoned_take`) is the
one a paid live run is for: replay only proves the hand-authored answer is safe,
live proves what Gemini actually flags on real takes.

Run modes (see tests/evals/README.md):
  pytest tests/evals/test_retake_detector_evals.py -v
  NOVA_EVAL_MODE=live pytest tests/evals/test_retake_detector_evals.py -v --eval-mode=live \
    --usage-purpose=live_eval --test-run-id=<id> --max-cost-usd=2 --approve-reservation
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.retake_detector import RetakeDetectorAgent

from .runners.eval_runner import (
    EVAL_FIXTURES_ROOT,
    CassetteModelClient,
    build_eval_run_context,
    discover_fixtures,
    load_fixture,
    run_eval,
)

AGENT_DIR = "retake_detector"
AGENT_NAME = "nova.audio.retake_detector"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)

# tests/fixtures — where the recorded sources the goldens were sliced from live.
FIXTURES_ROOT = EVAL_FIXTURES_ROOT.parent
GREEN3_GOLDEN = EVAL_FIXTURES_ROOT / AGENT_DIR / "golden" / "green3_s2_restart.json"
GREEN3_PROD_FIXTURE = FIXTURES_ROOT / "kri1176_green3_restart.json"


def _meta_of(path: Path) -> dict:
    """Read just `meta` so a malformed fixture can't break collection (the
    per-fixture test surfaces the real failure)."""
    try:
        return json.loads(path.read_text()).get("meta", {}) or {}
    except (OSError, ValueError):
        return {}


# Precision-gated positives (carry meta.must_cut / meta.must_keep).
GATED_PATHS = [p for p in FIXTURE_PATHS if "must_cut" in _meta_of(p)]
# Fixtures whose words are a slice of a recorded fixture that is checked in.
SOURCED_PATHS = [p for p in FIXTURE_PATHS if "source_fixture" in _meta_of(p)]


def _fixture_id(p: Path) -> str:
    return f"{p.parent.name}/{p.stem}"


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=_fixture_id)
def test_retake_detector_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")

    judge = judge_for(fixture.agent) if with_judge else None
    client = live_model_client if eval_mode == "live" else None

    result = run_eval(
        fixture,
        model_client=client,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )

    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/",
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=_fixture_id)
def test_negative_fixtures_return_no_retakes(
    fixture_path: Path,
    eval_mode: str,
    live_model_client,
) -> None:
    """Negatives (meta.expect_empty) MUST produce an empty retake list.

    This is the false-positive gate the plan calls out: rhetorical repetition
    and clean transcripts must never be flagged. In replay mode it pins the
    recorded cassette + parse() tolerance; in live mode it actually gates the
    prompt's false-positive discipline against real Gemini.
    """
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")
    if not fixture.meta.get("expect_empty"):
        pytest.skip("positive fixture — empty-output assertion not applicable")

    client = live_model_client if eval_mode == "live" else CassetteModelClient(fixture.raw_text)
    agent = RetakeDetectorAgent(client)
    output = agent.run(
        fixture.input,
        ctx=build_eval_run_context(
            fixture.fixture_id,
            is_live=eval_mode == "live",
            invocation="negative-gate",
        ),
    )

    assert output.retakes == [], (
        f"{fixture.fixture_id}: negative fixture flagged "
        f"{len(output.retakes)} retake span(s): "
        f"{[s.model_dump() for s in output.retakes]}"
    )


@pytest.mark.skipif(not GATED_PATHS, reason="no fixture carries meta.must_cut")
@pytest.mark.parametrize("fixture_path", GATED_PATHS, ids=_fixture_id)
def test_positive_fixtures_cut_only_the_abandoned_take(
    fixture_path: Path,
    eval_mode: str,
    live_model_client,
) -> None:
    """Precision gate on real takes: every `must_cut` word is covered by a
    returned span and NO `must_keep` word is. Cutting kept speech is far worse
    than missing a retake, so bleed into `must_keep` is the headline failure.

    Replay mode runs the hand-authored cassette, so it only proves the expected
    answer in the fixture is itself safe; live mode (paid, explicit approval)
    measures what Gemini actually returns.
    """
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")

    must_cut = set(fixture.meta["must_cut"])
    must_keep = set(fixture.meta["must_keep"])
    words = fixture.input["words"]

    client = live_model_client if eval_mode == "live" else CassetteModelClient(fixture.raw_text)
    agent = RetakeDetectorAgent(client)
    output = agent.run(
        fixture.input,
        ctx=build_eval_run_context(
            fixture.fixture_id,
            is_live=eval_mode == "live",
            invocation="precision-gate",
        ),
    )

    covered = {i for span in output.retakes for i in range(span.start_word, span.end_word + 1)}
    spans = [s.model_dump() for s in output.retakes]

    bled = sorted(covered & must_keep)
    assert not bled, (
        f"{fixture.fixture_id}: spans cut KEPT speech at word(s) {bled} "
        f"({[words[i]['text'] for i in bled]}); spans={spans}"
    )
    missed = sorted(must_cut - covered)
    assert not missed, (
        f"{fixture.fixture_id}: abandoned word(s) {missed} "
        f"({[words[i]['text'] for i in missed]}) not covered by any span; spans={spans}"
    )


@pytest.mark.skipif(not GATED_PATHS, reason="no fixture carries meta.must_cut")
@pytest.mark.parametrize("meta_path", GATED_PATHS, ids=_fixture_id)
def test_must_cut_and_must_keep_partition_every_word(meta_path: Path) -> None:
    """The precision labels must be a clean partition of the word list: disjoint,
    and together covering every index exactly once. A word with no label would
    silently escape the gate."""
    fixture = load_fixture(meta_path)
    n_words = len(fixture.input["words"])
    must_cut = fixture.meta["must_cut"]
    must_keep = fixture.meta["must_keep"]

    assert must_cut, f"{fixture.fixture_id}: must_cut is empty (use expect_empty for negatives)"
    assert all(type(i) is int for i in must_cut + must_keep)
    assert not set(must_cut) & set(must_keep), (
        f"{fixture.fixture_id}: must_cut and must_keep overlap at "
        f"{sorted(set(must_cut) & set(must_keep))}"
    )
    assert sorted(must_cut + must_keep) == list(range(n_words)), (
        f"{fixture.fixture_id}: must_cut + must_keep must list each of the {n_words} word "
        "indices exactly once"
    )
    assert max(must_cut) < n_words - 1, (
        f"{fixture.fixture_id}: must_cut reaches the final word; the detector drops such "
        "spans by design, so no kept re-delivery could follow it"
    )


def _source_words(meta: dict) -> list[dict]:
    node = json.loads((FIXTURES_ROOT / meta["source_fixture"]).read_text())
    for key in meta["source_json_path"]:
        node = node[key]
    start, stop = meta["source_slice"]
    return node[start:stop]


@pytest.mark.skipif(not SOURCED_PATHS, reason="no fixture names a recorded source")
@pytest.mark.parametrize("provenance_path", SOURCED_PATHS, ids=_fixture_id)
def test_fixture_words_are_verbatim_slices_of_their_recorded_source(
    provenance_path: Path,
) -> None:
    """Guard for 'real recorded words only, never invented': a fixture that names
    `meta.source_fixture` must equal that file's words over `meta.source_slice`
    (re-indexed from 0; times rounded to 3 dp)."""
    fixture = load_fixture(provenance_path)
    source = _source_words(fixture.meta)
    got = fixture.input["words"]

    assert source, f"{fixture.fixture_id}: source slice is empty"
    assert len(got) == len(source), (
        f"{fixture.fixture_id}: {len(got)} words vs {len(source)} in "
        f"{fixture.meta['source_fixture']}{fixture.meta['source_slice']}"
    )
    for pos, (word, recorded) in enumerate(zip(got, source, strict=True)):
        expected = {
            "i": pos,
            "text": recorded["text"],
            "start_s": round(float(recorded.get("start_s", recorded.get("start"))), 3),
            "end_s": round(float(recorded.get("end_s", recorded.get("end"))), 3),
        }
        assert word == expected, (
            f"{fixture.fixture_id}: word {pos} is {word}, source says {expected}"
        )


def test_green3_fixture_words_match_the_prod_fixture() -> None:
    """The Green 3 golden must carry exactly the recorded prod words
    (`words_source_time`, index/text/times at 3 dp) and the restart span prod
    recorded for it — so the paid precision run measures the real take."""
    golden = load_fixture(GREEN3_GOLDEN)
    prod = json.loads(GREEN3_PROD_FIXTURE.read_text())
    recorded = prod["words_source_time"]

    expected = [
        {
            "i": w["i"],
            "text": w["text"],
            "start_s": round(w["start_s"], 3),
            "end_s": round(w["end_s"], 3),
        }
        for w in recorded
    ]
    assert golden.input["words"] == expected
    assert golden.input["language"] == prod["take"]["language"] == "en"

    first, last = prod["restart"]["word_span"]
    assert golden.meta["must_cut"] == list(range(first, last + 1))
    assert prod["restart"]["kept_redelivery_first_word"] in golden.meta["must_keep"]
    assert golden.meta["job_id"] == prod["job_id"]
