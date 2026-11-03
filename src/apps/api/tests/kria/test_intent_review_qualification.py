"""The zero-cost qualification harness for the paid intent review run (KRI-1171).

Failure modes, written before the harness (each has a test below or in
tests/integration/test_intent_review_qualification_postgres.py, marked PG):

  Money leaves before anyone approved it:
    the default invocation, `--dry-run`, a missing `--execute`, a missing or malformed approved
    run id, a run id whose suffix is not the stage being run, a missing or non-positive approved
    maximum, a maximum above $2, a run whose estimate exceeds the maximum, a journal that would
    pass $5 in total, a full run before a passed pilot, an unreconciled unknown charge, a
    production environment, a database that is not local, a missing API key and a monthly
    capability budget below the run each refuse before a client is built or a ledger row is
    written. Every refusal is listed at once, so the operator fixes them in one pass.
  The dry run is not free:
    it builds no OpenAI client, opens no database connection and writes nothing outside --out.
  A spend table that understates:
    each row's estimate is the repo estimator over the exact prompt, schema and frames the run
    would send (PG: the ledger reserves that same number), and the total is the sum of the rows.
  A preregistration that moves after approval:
    the digest over model, prompt, schema, cases, predicates, estimates and caps is pinned in
    QUALIFICATION.md; a doc that disagrees refuses execution and fails the dry run.
  A scorer that cannot fail:
    a perfect reviewer passes every predicate of every case; a reviewer that approves everything,
    one that flags everything, one that cites the wrong frames and one that returns a void reply
    each fail the predicates they are preregistered to fail.
  A control that does not discriminate:
    the controls differ from the correct edit in exactly the frames they claim to differ in; the
    footage is generated in process and the harness has no media input at all.
  A dry run that looks like a check:
    no receipt is composed as "checked" when no frame was sent; the composer's honesty key
    refuses the dry run's own provenance.
  Wording rewritten, or a repair beyond the fit:
    the wrong-wording control's catalog holds no restore candidate; the timing control's only
    candidate changes the title's end and nothing else.
  A void or unusable reply scored as a pass (PG):
    a reply that is not the contract voids the item, stops the run and spends nothing more.
  An outcome nobody can account for (PG):
    a provider outcome that is unknown keeps its liability in the journal and the ledger and
    blocks the next execution.
  The same approved run twice (PG):
    a second execution of an id that already ran is refused before the ledger.
  A secret in an artifact:
    neither the API key nor the database URL reaches stdout, report.json or any file.

The only fake is the OpenAI SDK object. The catalog, prompt builder, estimator, judgment, receipt
composer and (PG) the cost ledger run for real.
"""

# ruff: noqa: F811

import dataclasses
import json
from pathlib import Path

import pytest

from app.agents._openai_vision import OpenAIVisionClient
from app.cli import qualify_intent_review as cli
from app.config import settings
from app.database import sync_engine
from app.kria import intent_review_qualification as q
from app.services import ai_cost_control as costs
from app.services.bounded_media_review import estimate_media_review_cost

RUN_PILOT = "iqual-ab12cd-pilot"
RUN_FULL = "iqual-ab12cd-full"
SECRET_KEY = "sk-sentinel-never-print-this"
SECRET_URL = "postgresql://qual:db-password-sentinel@localhost:5432/qualification"


@pytest.fixture(autouse=True)
def nothing_leaves_the_process(monkeypatch):
    def no_client(self):
        raise AssertionError("the harness built a live OpenAI client")

    def no_ledger(*args, **kwargs):
        raise AssertionError("the harness touched the cost ledger")

    def no_database(*args, **kwargs):
        raise AssertionError("the harness opened a database connection")

    monkeypatch.setattr(OpenAIVisionClient, "_get", no_client)
    monkeypatch.setattr(costs, "reserve_paid_call", no_ledger)
    monkeypatch.setattr(sync_engine, "connect", no_database)
    monkeypatch.setattr(sync_engine, "begin", no_database)


@pytest.fixture
def approved(monkeypatch):
    for name, value in dict(
        ai_usage_environment="development",
        database_url="postgresql://tester@localhost:5432/scratch",
        openai_api_key=SECRET_KEY,
        ai_openai_intent_review_budget_usd=2.0,
        ai_openai_monthly_budget_usd=5.0,
    ).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(cli, "pinned_digest", lambda: DIGEST)
    return {cli.RUN_ID_ENV: RUN_PILOT, cli.MAX_USD_ENV: "0.50"}


def _digest():
    built = q.build_all()
    return q.preregistration_digest(built, q.plan_items())


DIGEST = _digest()


def run(tmp_path, capsys, *argv, env=None):
    env = {cli.JOURNAL_ENV: str(tmp_path / "spend.json"), **(env or {})}
    code = cli.main([*argv, "--out", str(tmp_path)], env=env)
    return code, capsys.readouterr().out


def test_the_default_run_is_a_dry_run_that_spends_and_touches_nothing(tmp_path, capsys, approved):
    cwd_before = set(Path.cwd().iterdir())
    code, out = run(tmp_path, capsys)
    explicit_code, explicit_out = run(tmp_path, capsys, "--dry-run")
    assert (code, explicit_code) == (0, 0)
    assert "DRY_RUN_RESULT: pass" in out and "no provider call, no ledger write" in out
    assert "EXECUTION: not requested" in out and explicit_out == out
    assert set(Path.cwd().iterdir()) == cwd_before
    with pytest.raises(SystemExit):
        cli.main(["--dry-run", "--execute", "--out", str(tmp_path)], env={})


def test_a_dry_run_whose_digest_is_not_pinned_fails(tmp_path, capsys, approved, monkeypatch):
    monkeypatch.setattr(cli, "pinned_digest", lambda: "0" * 64)
    code, out = run(tmp_path, capsys)
    assert code == 1 and "DRY_RUN_RESULT: fail" in out
    assert "FAIL preregistration_pinned" in out


def test_the_spend_table_is_the_estimator_over_the_exact_request_each_row_would_send(
    tmp_path, capsys, approved
):
    run(tmp_path, capsys)
    summary = json.loads((tmp_path / "dry-run.json").read_text())
    built = q.build_all()
    rows = summary["spend"]["rows"]
    assert [r["item"] for r in rows] == [i.key for i in q.plan_items()]
    for row in rows:
        case = built[row["item"].split("#")[0]]
        sent = q.request_for(case, idempotency_key="k", run_context=cli.RunContext())
        assert row["estimate_usd"] == estimate_media_review_cost(sent)
        assert row["frames"] == len(sent.frames) and row["estimate_usd"] <= q.PER_CALL_CEILING_USD
        prompt = (tmp_path / "prompts" / f"{case.case.name}.txt").read_text()
        assert prompt == sent.prompt
        assert all(c.candidate_id not in prompt for c in case.catalog.candidates)
    total = round(sum(r["estimate_usd"] for r in rows), 6)
    assert summary["spend"]["all_usd"] == total
    assert summary["spend"]["pilot_usd"] + summary["spend"]["full_usd"] == pytest.approx(total)
    assert total < q.PER_RUN_CAP_USD < q.TOTAL_CAP_USD
    for case_name, case in summary["cases"].items():
        for frame in case["frames"]:
            data = (tmp_path / "frames" / case_name / f"{frame['id']}.jpg").read_bytes()
            assert q.hashlib.sha256(data).hexdigest() == frame["sha256"]
    assert (tmp_path / "montage.png").stat().st_size > 0


def test_the_preregistration_the_doc_pins_is_the_one_the_code_computes():
    assert cli.pinned_digest() == DIGEST


def test_the_digest_moves_when_anything_preregistered_moves(monkeypatch):
    built, items = q.build_all(), q.plan_items()
    base = q.preregistration_digest(built, items)
    first = q.CASES[0]
    looser = dataclasses.replace(first.predicates[0], statement="a looser statement")
    reworded = dataclasses.replace(first, predicates=(looser, *first.predicates[1:]))
    repainted = dataclasses.replace(q.CASES[1], painted=q.Painted("Festival Day", 0.5, 3.0))
    for changed in (
        (reworded, *q.CASES[1:]),
        (q.CASES[0], repainted, *q.CASES[2:]),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(q, "CASES", changed)
            assert q.preregistration_digest(built, items) != base
    with monkeypatch.context() as scoped:
        scoped.setattr(q, "REPEATS", 3)
        assert q.preregistration_digest(built, q.plan_items()) != base


def test_every_gate_holds_only_when_everything_is_approved(approved):
    built = q.build_all()
    items = q.plan_items()
    rows = q.spend_table(built, items)
    journal = cli.Journal(Path("/nonexistent/spend.json"))
    environment = cli.load_environment(approved)
    gates = cli.check_gates(
        execute=True,
        stage="pilot",
        rows=rows,
        journal=journal,
        environment=environment,
        digest=DIGEST,
        pinned=DIGEST,
    )
    assert [g.name for g in gates if not g.ok] == []
    journal.runs[RUN_PILOT] = {"verdict": "pass", "items": {}}
    full_env = {cli.RUN_ID_ENV: RUN_FULL, cli.MAX_USD_ENV: "1.00"}
    gates = cli.check_gates(
        execute=True,
        stage="full",
        rows=rows,
        journal=journal,
        environment=cli.load_environment(full_env),
        digest=DIGEST,
        pinned=DIGEST,
    )
    assert [g.name for g in gates if not g.ok] == []


def _seed_journal(tmp_path, runs):
    (tmp_path / "spend.json").write_text(json.dumps({"version": 1, "runs": runs}))


def _entry(state, estimate=0.06, settled=None):
    return {"state": state, "estimate_usd": estimate, "settled_usd": settled}


REFUSALS = {
    "malformed_run_id": ("approved_run_id", {cli.RUN_ID_ENV: "run-1"}, {}, {}),
    "wrong_stage_suffix": ("approved_run_id", {cli.RUN_ID_ENV: RUN_FULL}, {}, {}),
    "no_run_id": ("approved_run_id", {cli.RUN_ID_ENV: ""}, {}, {}),
    "no_maximum": ("approved_maximum", {cli.MAX_USD_ENV: ""}, {}, {}),
    "maximum_not_a_number": ("approved_maximum", {cli.MAX_USD_ENV: "nan"}, {}, {}),
    "maximum_above_the_hard_cap": ("approved_maximum", {cli.MAX_USD_ENV: "2.01"}, {}, {}),
    "maximum_below_the_estimate": (
        "estimate_within_approved_maximum",
        {cli.MAX_USD_ENV: "0.10"},
        {},
        {},
    ),
    "production_environment": (
        "development_environment",
        {},
        {"ai_usage_environment": "production"},
        {},
    ),
    "remote_database": (
        "local_database",
        {},
        {"database_url": "postgresql://u:p@db.internal:5432/ledger"},
        {},
    ),
    "no_api_key": ("api_key_present", {}, {"openai_api_key": ""}, {}),
    "capability_budget_too_small": (
        "capability_budgets",
        {},
        {"ai_openai_intent_review_budget_usd": 0.05},
        {},
    ),
    "monthly_budget_zero": ("capability_budgets", {}, {"ai_openai_monthly_budget_usd": 0}, {}),
    "five_dollars_already_committed": (
        "total_cap",
        {},
        {},
        {"iqual-old000-full": {"verdict": "pass", "items": {"x#1": _entry("settled", 4.9, 4.9)}}},
    ),
    "an_unknown_charge_is_outstanding": (
        "no_unreconciled_charge",
        {},
        {},
        {"iqual-old000-full": {"verdict": "fail", "items": {"x#1": _entry("unknown")}}},
    ),
    "the_run_id_already_ran": (
        "run_not_yet_executed",
        {},
        {},
        {RUN_PILOT: {"verdict": "fail", "items": {}}},
    ),
}


@pytest.mark.parametrize("name", REFUSALS)
def test_one_missing_approval_refuses_before_a_client_or_a_ledger_row(
    name, tmp_path, capsys, approved, monkeypatch
):
    gate, env, overrides, journal = REFUSALS[name]
    for attribute, value in overrides.items():
        monkeypatch.setattr(settings, attribute, value)
    if journal:
        _seed_journal(tmp_path, journal)
    before = (tmp_path / "spend.json").read_text() if journal else None
    code, out = run(tmp_path, capsys, "--execute", env={**approved, **env})
    assert code == cli.EXIT_REFUSED, out
    assert f"FAIL {gate}:" in out
    assert "EXECUTION: refused. No client was built and nothing was reserved." in out
    assert (tmp_path / "spend.json").read_text() == before if journal else True
    assert not (tmp_path / "runs").exists()


def test_a_full_run_before_a_passed_pilot_is_refused(tmp_path, capsys, approved):
    env = {**approved, cli.RUN_ID_ENV: RUN_FULL}
    code, out = run(tmp_path, capsys, "--execute", "--stage", "full", env=env)
    assert code == cli.EXIT_REFUSED and "FAIL pilot_passed:" in out
    _seed_journal(tmp_path, {RUN_PILOT: {"verdict": "fail", "items": {}}})
    code, out = run(tmp_path, capsys, "--execute", "--stage", "full", env=env)
    assert code == cli.EXIT_REFUSED and "FAIL pilot_passed:" in out


def test_asking_to_execute_with_no_approval_lists_every_missing_piece_at_once(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(settings, "openai_api_key", "")
    code, out = run(tmp_path, capsys, "--execute", env={})
    assert code == cli.EXIT_REFUSED
    for gate in ("approved_run_id", "approved_maximum", "api_key_present", "capability_budgets"):
        assert f"FAIL {gate}:" in out


def test_the_scorer_fails_every_reviewer_it_is_preregistered_to_fail():
    checks = q.run_scorer_checks(q.build_all())
    assert checks and [c.name for c in checks if not c.ok] == []
    assert {c.name.split("/")[0] for c in checks} == {
        "perfect_reviewer",
        *q.REVIEWERS,
    }


def test_a_decoy_reviewer_that_stops_failing_is_caught_by_the_checks(monkeypatch):
    monkeypatch.setitem(q.REVIEWERS, "approves_everything", q.perfect_reply)
    failed = [c.name for c in q.run_scorer_checks(q.build_all()) if not c.ok]
    assert "approves_everything/wrong_content_control" in failed
    assert "approves_everything/timing_miss_control" in failed


def test_a_control_that_stops_differing_from_the_correct_edit_is_caught():
    built = q.build_all()
    flat = dataclasses.replace(
        q.CASES_BY_NAME["wrong_wording_control"],
        draft_text=q.TITLE,
        painted=q.Painted(q.TITLE, 0.5, 3.5),
    )
    built["wrong_wording_control"] = q.build_case(flat)
    failed = [c.name for c in q.run_discrimination_checks(built) if not c.ok]
    assert failed == ["wrong_wording_control_differs_only_where_claimed"]


def test_footage_is_generated_in_process_and_has_no_media_input():
    first, second = q.build_all(), q.build_all()
    for name in first:
        assert [f.jpeg for f in first[name].frames] == [f.jpeg for f in second[name].frames]
        assert all(f.width == 360 and f.height == 640 for f in first[name].frames)
        assert len({f.sha256 for f in first[name].frames}) == len(first[name].frames)
    for flag in ("--video", "--input", "--frames", "--media"):
        with pytest.raises(SystemExit):
            cli.main([flag, "clip.mp4"], env={})


def test_a_dry_run_composes_no_receipt_because_no_frame_was_sent():
    built = q.build_all()["wrong_content_control"]
    judgement = q.judge(built, q.perfect_reply(built), visual_input_received=False)
    assert judgement.posts == {"en": None, "tr": None}
    assert q.run_honesty_check(q.build_all()).ok


def test_the_controls_hold_the_catalog_facts_their_predicates_rely_on():
    built = q.build_all()
    wording = [c.kind for c in built["wrong_wording_control"].catalog.candidates]
    assert "restore_exact_text" not in wording
    (fit,) = [
        c
        for c in built["timing_miss_control"].catalog.candidates
        if c.kind == "fit_text_to_duration"
    ]
    assert fit.target == {"start_s": 0.5, "end_s": 7.7} and fit.auto
    assert not [
        c
        for name in ("correct_edit", "wrong_content_control", "wrong_content_injection_decoy")
        for c in built[name].catalog.candidates
        if c.auto
    ]
    for case in built.values():
        assert [w.model_dump() for w in case.catalog.withheld] == [
            {"brief_id": "b3", "reason": "audio"}
        ]
        assert [r.brief_id for r in case.catalog.requirements] == ["b1", "b2"]


def test_no_secret_reaches_stdout_or_any_artifact(tmp_path, capsys, approved, monkeypatch):
    monkeypatch.setattr(settings, "database_url", SECRET_URL)
    _, shown = run(tmp_path, capsys)
    _, refused = run(tmp_path, capsys, "--execute", env={cli.RUN_ID_ENV: "x"})
    haystack = shown + refused
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".txt"}:
            haystack += path.read_text()
    assert SECRET_KEY not in haystack and "db-password-sentinel" not in haystack


def test_two_invocations_never_overwrite_each_others_spend(tmp_path):
    path = tmp_path / "spend.json"
    first, second = cli.Journal(path), cli.Journal(path)
    first.open_run("iqual-aaaa-pilot", "pilot", 1.0, "d" * 64)
    first.record("iqual-aaaa-pilot", "case/0", "started", 0.06)
    second.open_run("iqual-bbbb-pilot", "pilot", 1.0, "d" * 64)
    second.record("iqual-bbbb-pilot", "case/0", "settled", 0.06, 0.05)
    fresh = cli.Journal(path)
    assert set(fresh.runs) == {"iqual-aaaa-pilot", "iqual-bbbb-pilot"}
    assert first.unknown() == ["iqual-aaaa-pilot/case/0"]
    assert first.committed_usd() == pytest.approx(0.11)


def test_the_journal_does_not_move_with_the_working_directory_or_the_output_directory(
    tmp_path, monkeypatch
):
    here = cli.journal_path({})
    monkeypatch.chdir(tmp_path)
    assert cli.journal_path({}) == here and here.is_absolute()
    assert cli.journal_path({cli.JOURNAL_ENV: str(tmp_path / "j.json")}) == tmp_path / "j.json"
    assert cli.DEFAULT_OUT.is_absolute()


def test_a_second_execute_cannot_start_while_one_holds_the_spend_lock(tmp_path):
    path = tmp_path / "spend.json"
    running, second = cli.Journal(path), cli.Journal(path)
    assert running.hold_run_lock() is True
    assert second.hold_run_lock() is False
    del running
    assert cli.Journal(path).hold_run_lock() is True
