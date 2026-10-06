"""Incident corpus: every production request-following failure as a permanent fixture.

HONEST SCOPE. This module asserts only what pytest CI can prove with the real code:

- ``contract``: the approved strategy + brief + media build the expected
  ``CreatorRenderContract`` (real ``build_render_contract``, rebuilt from a real
  ``BriefBinding`` the way dispatch does), and the real verifiers
  (``verify_phone_recipe`` on a recipe compiled by the real speech-montage
  compiler, or ``preflight_cloud_contract``) decline what the record says they must.
- ``question``: the real creator-planner turn asks (or does not ask) the recorded
  question over the recorded conversation.
- ``output``: the recorded output evidence (what the creator actually got, later a
  post-fix observation with named proof) is judged against the expected output facts.
  Nothing here renders a video or listens to audio. Double audio, voice presence and
  on-screen order are only as proven as the observation's ``proof``: the incident
  observations are plan fields and creator reports, and the ``repro`` command names
  the swift / make check that a later PR (KRI-478) owns. The corpus never claims
  output coverage it does not run.

Records owned by later PRs are ``xfail(strict=True)`` with ``KRI-47x / PR-x`` as the
reason. How each flips, honestly:

- ``contract`` / ``refusal`` / ``question`` records exercise REAL code, so the owning PR's
  product change turns them into XPASS and strict mode forces it to delete the xfail.
- ``output`` records compare recorded evidence to recorded evidence. Nothing runs, so they
  flip only when the owning PR APPENDS a ``post-fix`` observation whose ``proof`` resolves
  to a real repro (a pytest node id or script that exists, never free text) and removes
  the xfail. A hand edit without that proof is rejected by ``test_post_fix_proofs_are_real``.

``raises=AssertionError`` keeps a crashing harness from hiding behind an xfail.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.incidents import harness, loader
from tests.incidents.models import IncidentRecord, Observation, OutputFacts, Scope
from tests.incidents.synthetic import ffmpeg_argv, suffix

RECORDS = loader.load_records()


def _params(scope: Scope | None, wanted) -> list:
    out = []
    for record in RECORDS:
        if not wanted(record):
            continue
        marks = []
        failing = record.xfail_for(scope) if scope else None
        if failing:
            marks.append(
                pytest.mark.xfail(strict=True, reason=failing.reason, raises=AssertionError)
            )
        out.append(pytest.param(record, id=record.id, marks=marks))
    return out


def test_corpus_is_not_empty_and_ids_are_unique() -> None:
    ids = [record.id for record in RECORDS]
    assert len(ids) >= 8
    assert len(set(ids)) == len(ids)
    assert {"output", "clarification"} <= {record.kind for record in RECORDS}


@pytest.mark.parametrize("record", [pytest.param(r, id=r.id) for r in RECORDS])
def test_record_matches_current_schemas(record: IncidentRecord) -> None:
    # (The model itself rejects an xfail on a scope the record never asserts.)
    loader.validate_against_current_schemas(record)


@pytest.mark.parametrize("record", [pytest.param(r, id=r.id) for r in RECORDS])
def test_repro_command_points_at_something_real(record: IncidentRecord) -> None:
    repro = record.repro
    if repro.status == "pending":
        assert repro.owner
        return
    assert loader.unresolved_reference(repro.command) is None, repro.command


@pytest.mark.parametrize(
    "command,resolves",
    [
        (
            "pytest tests/incidents/test_incident_corpus.py::test_post_fix_proofs_are_real",
            True,
        ),
        ("pytest tests/incidents/test_incident_corpus.py -k kri469", True),
        ("make verify-kria", True),
        ("pytest tests/incidents/test_incident_corpus.py::test_that_does_not_exist", False),
        ("pytest tests/incidents/missing.py", False),
        ("pytest tests/incidents/test_incident_corpus.py -k no-such-record", False),
        ("make no-such-target", False),
        ("swift test --filter NoSuchSwiftSuiteAnywhere", False),
        ("verified it by hand on a device", False),
    ],
)
def test_reference_resolver_rejects_free_text_and_missing_targets(command, resolves) -> None:
    assert (loader.unresolved_reference(command) is None) is resolves


def test_post_fix_proofs_are_real() -> None:
    for record in RECORDS:
        for seen in record.observations:
            if seen.label == "post-fix":
                assert loader.unresolved_reference(seen.proof) is None, (record.id, seen.proof)


@pytest.mark.parametrize("record", _params("contract", lambda r: r.expect.contract is not None))
def test_approved_plan_builds_the_expected_contract(record: IncidentRecord) -> None:
    contract = loader.build_contract(record)
    assert contract is not None, "no contract was built from the approved plan"
    want = record.expect.contract
    given = want.model_fields_set
    if "duration_s" in given:
        assert contract.duration_s == want.duration_s
    if "audio_source_ids" in given:
        assert contract.audio_source_ids == tuple(want.audio_source_ids)
    if "original_audio" in given:
        assert contract.original_audio == want.original_audio
    if "require_voiceover" in given:
        assert contract.require_voiceover is want.require_voiceover
    if "order_required" in given:
        assert contract.order_required is want.order_required
    if "order_basis" in given:
        assert contract.order_basis == want.order_basis
    if "order_ids" in given:
        expected = (
            loader.capture_sorted_ids(record)
            if want.order_ids == "capture_time"
            else list(want.order_ids)
        )
        assert list(contract.order_ids) == expected
    if "exact_texts" in given:
        assert [(t.role, t.text) for t in contract.exact_texts] == [
            (t.role, t.text) for t in want.exact_texts
        ]
    if want.unresolved_nonempty:
        assert contract.unresolved, "expected an unresolved requirement"
    if want.resolved:
        assert not contract.unresolved, contract.unresolved


@pytest.mark.parametrize("record", [pytest.param(r, id=r.id) for r in RECORDS if r.expect.refusal])
def test_real_verifier_declines_what_the_record_says(record: IncidentRecord) -> None:
    contract = loader.build_contract(record)
    assert contract is not None
    # Raises AssertionError if the verifier accepted the plan. Copy is not asserted.
    harness.refusal_message(record, contract)


@pytest.mark.parametrize(
    "record",
    _params(
        "refusal",
        lambda r: bool(
            r.expect.refusal and (r.expect.refusal.reason or r.expect.refusal.field_path)
        ),
    ),
)
def test_decline_carries_its_typed_reason_and_field(record: IncidentRecord) -> None:
    """Typed declines (KRI-476 / PR-A): the error MUST expose the attribute and match."""
    contract = loader.build_contract(record)
    assert contract is not None
    exc = harness.refusal_message(record, contract)
    want = record.expect.refusal
    if want.reason:
        assert getattr(exc, "decline_reason", None) == want.reason, "no matching decline_reason"
    if want.field_path:
        assert getattr(exc, "field_path", None) == want.field_path, "no matching field_path"


@pytest.mark.parametrize("record", _params("question", lambda r: r.expect.question is not None))
async def test_planner_asks_or_stays_quiet_as_recorded(
    record: IncidentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await harness.planner_turn(record, monkeypatch)
    plan = result.plan
    want = record.expect.question
    if want.no_question:
        assert plan.turn_value != "question", f"unnecessary question: {plan.response!r}"
        return
    assert plan.turn_value == "question" and plan.choice_question, "the planner asked nothing"
    question = plan.choice_question
    kind = question.get("kind") or question.get("conflict")
    assert kind == want.kind, kind
    keys = [option["key"] for option in question["options"]]
    if want.option_keys:
        assert keys == want.option_keys
    assert len(keys) >= want.min_options


def output_mismatches(record: IncidentRecord, want: OutputFacts, seen: Observation) -> list[str]:
    """Differences between expected output facts and the latest recorded evidence."""
    facts, out = seen.facts, []
    if want.duration_s is not None:
        if facts.duration_s is None:
            out.append("duration not observed")
        elif abs(facts.duration_s - want.duration_s) / want.duration_s > want.duration_tol_frac:
            out.append(f"duration {facts.duration_s}s, expected {want.duration_s}s")
    for name in ("voice_present", "audio_plays_once"):
        expected = getattr(want, name)
        if expected is not None and getattr(facts, name) is not expected:
            out.append(f"{name} is {getattr(facts, name)}, expected {expected}")
    if want.voice_source_ids is not None and set(facts.voice_source_ids or ()) != set(
        want.voice_source_ids
    ):
        out.append(f"voice from {facts.voice_source_ids}, expected {want.voice_source_ids}")
    if want.order_ids is not None:
        seen_order = facts.order_ids if isinstance(facts.order_ids, list) else None
        if seen_order is None:
            out.append("order not observed")
        elif want.order_ids == "chronological":
            stamps = {m.id: m.capture_time for m in record.inputs.media}
            ordered = [stamps[i] for i in seen_order]
            if ordered != sorted(ordered):
                out.append("clips are not in capture order")
        elif seen_order != want.order_ids:
            out.append("clip order differs")
    if want.exact_texts is not None:
        missing = [t for t in want.exact_texts if t not in (facts.exact_texts or [])]
        if missing:
            out.append(f"exact text not shown: {missing}")
    return out


@pytest.mark.parametrize("record", _params("output", lambda r: r.expect.output_facts is not None))
def test_latest_recorded_output_meets_the_expected_facts(record: IncidentRecord) -> None:
    latest = record.observations[-1]
    if latest.label == "post-fix":
        assert loader.unresolved_reference(latest.proof) is None, "proof is not a real repro"
    mismatches = output_mismatches(record, record.expect.output_facts, latest)
    assert not mismatches, f"[{latest.label}] " + "; ".join(mismatches)


@pytest.mark.parametrize(
    "record", [pytest.param(r, id=r.id) for r in RECORDS if r.inputs.synthetic]
)
def test_synthetic_substitutes_regenerate_with_the_declared_shape(
    record: IncidentRecord, tmp_path
) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")
    for clip in record.inputs.synthetic:
        out = tmp_path / f"{clip.id}{suffix(clip)}"
        subprocess.run(ffmpeg_argv(clip, out), check=True, capture_output=True)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1",
             str(out)],
            check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip
        duration = float(re.search(r"duration=([\d.]+)", probe).group(1))
        assert abs(duration - clip.duration_s) < 0.3, (clip.id, duration)
        levels = subprocess.run(
            ["ffmpeg", "-i", str(out), "-af", "volumedetect", "-vn", "-f", "null", "-"],
            check=True, capture_output=True, text=True,
        ).stderr  # fmt: skip
        mean = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", levels).group(1)
        audible = mean != "-inf" and float(mean) > -50
        assert audible == (clip.tone_hz is not None), (clip.id, mean)


def test_a_post_fix_observation_flips_the_output_verdict() -> None:
    """The path a fixing PR takes: add a proven post-fix observation, drop the xfail."""
    record = next(r for r in RECORDS if r.id == "kri469-voice-clip-ignored")
    want = record.expect.output_facts
    fixed = Observation(
        label="post-fix",
        source="synthetic render of the same plan",
        proof="make verify-kria",
        facts=OutputFacts(duration_s=30.0, voice_source_ids=want.voice_source_ids),
    )
    assert output_mismatches(record, want, record.observations[-1])
    assert not output_mismatches(record, want, fixed)
    assert loader.unresolved_reference(fixed.proof) is None


def test_the_schema_refuses_unproven_or_ambiguous_claims() -> None:
    from pydantic import ValidationError

    from tests.incidents.models import QuestionExpect, Repro

    with pytest.raises(ValidationError):  # a post-fix claim must name its proof
        Observation(label="post-fix", source="x", facts=OutputFacts(duration_s=30))
    with pytest.raises(ValidationError):  # "no question" with a question shape
        QuestionExpect(no_question=True, kind="order_basis")
    with pytest.raises(ValidationError):  # an unrunnable repro that is not marked pending
        Repro(command="swift test --filter X")
    with pytest.raises(ValidationError):  # a pending repro needs an owner
        Repro(command="swift test --filter X", status="pending")
