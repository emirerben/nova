"""The paid path of the qualification harness, driven end to end with a fake provider (KRI-1171).

Failure modes (each has a test below; the gate refusals and the dry run are in
tests/kria/test_intent_review_qualification.py):

  The paid path is not the path that was priced:
    the ledger reserves each call at the table's estimate, the provider receives exactly the
    frames, prompt and schema the dry run wrote, and the settled cost is the provider's usage.
  A passing run that did not earn it:
    a reviewer that approves everything fails the pilot, and the full run stays closed.
  A void reply scored or retried:
    one reply that is not the contract stops the run; only the call that was made is paid.
  An outcome nobody can account for:
    a 5xx keeps the reservation as a liability in the ledger and the journal, nothing is
    retried, and every later execution is refused until it is reconciled; a definite 4xx
    releases it.
  The same approved run id twice:
    refused by the journal; with the journal lost, the ledger's idempotency still stops it
    before the provider.
  A secret in an artifact:
    the API key reaches no file of an executed run.

The only fake is the OpenAI SDK object. Footage, catalog, prompt, estimate, reservation,
settlement, judgment and receipts run for real on PostgreSQL.
"""

import base64
import hashlib
import json

import pytest
from sqlalchemy import text

from app.agents._openai_vision import OpenAIVisionClient
from app.cli import qualify_intent_review as cli
from app.config import settings
from app.database import sync_engine
from app.kria import intent_review_qualification as q
from tests.services.test_openai_vision_transport import HttpError, Sdk, response

PILOT = "iqual-pg01-pilot"
FULL = "iqual-pg01-full"
KEY = "sk-sentinel-postgres-run"
SETTLED_USD = (9100 - 32) * 2e-6 + 32 * 0.10e-6 + 700 * 10e-6


class Reviewer(Sdk):
    def __init__(self, reply_for):
        super().__init__(None)
        self.reply_for = reply_for
        self.cases = []
        self.built = {tuple(f.sha256 for f in case.frames): case for case in q.build_all().values()}

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        images = kwargs["input"][0]["content"][1:]
        digests = tuple(
            hashlib.sha256(base64.b64decode(part["image_url"].split(",", 1)[1])).hexdigest()
            for part in images
        )
        case = self.built[digests]
        self.cases.append(case.case.name)
        assert kwargs["input"][0]["content"][0]["text"] == case.built.prompt
        assert kwargs["text"]["format"]["schema"] == case.built.response_schema
        return response(text=self.reply_for(case))

    @property
    def client(self):
        return OpenAIVisionClient(client=self)


class Failing(Sdk):
    @property
    def client(self):
        return OpenAIVisionClient(client=self)


@pytest.fixture(autouse=True)
def no_live_openai(monkeypatch):
    def refuse(self):
        if self._client is None:
            raise AssertionError("a test constructed a live OpenAI client")
        return self._client

    monkeypatch.setattr(OpenAIVisionClient, "_get", refuse)


def _clean():
    with sync_engine.begin() as db:
        db.execute(text("DELETE FROM ai_cost_reservations WHERE test_run_id LIKE 'iqual-pg%'"))


@pytest.fixture
def ledger(monkeypatch):
    _clean()
    for name, value in dict(
        ai_usage_environment="development",
        openai_api_key=KEY,
        ai_openai_intent_review_budget_usd=2.0,
        ai_openai_monthly_budget_usd=5.0,
        ai_development_monthly_budget_usd=25.0,
        ai_paid_test_attribution_required=True,
    ).items():
        monkeypatch.setattr(settings, name, value)
    yield
    _clean()


def rows(run_id):
    with sync_engine.begin() as db:
        return [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT feature, usage_purpose, test_run_id, status, provider, requested_model,"
                    " estimated_cost_usd, settled_cost_usd FROM ai_cost_reservations"
                    " WHERE test_run_id = :run ORDER BY created_at"
                ),
                {"run": run_id},
            ).mappings()
        ]


def execute(tmp_path, run_id, stage, sdk, *, maximum="0.50"):
    env = {cli.RUN_ID_ENV: run_id, cli.MAX_USD_ENV: maximum}
    return cli.main(
        ["--execute", "--stage", stage, "--out", str(tmp_path)], client=sdk.client, env=env
    )


def report(tmp_path, run_id):
    return json.loads((tmp_path / "runs" / run_id / "report.json").read_text())


def journal(tmp_path):
    return cli.Journal(tmp_path / "spend.json")


def test_a_perfect_reviewer_passes_the_pilot_and_then_the_full_run_and_every_number_reconciles(
    tmp_path, capsys, ledger
):
    built = q.build_all()
    sdk = Reviewer(q.perfect_reply)
    assert execute(tmp_path, PILOT, "pilot", sdk) == 0
    assert sdk.cases == list(q.PILOT_CASES)
    pilot_rows = rows(PILOT)
    assert [r["status"] for r in pilot_rows] == ["settled"] * 3
    for row, name in zip(pilot_rows, q.PILOT_CASES, strict=True):
        assert (row["feature"], row["usage_purpose"], row["provider"]) == (
            "intent_review",
            "live_eval",
            "openai",
        )
        assert row["requested_model"] == "gpt-6.1-sol"
        assert float(row["estimated_cost_usd"]) == pytest.approx(built[name].estimate_usd, abs=1e-6)
        assert float(row["settled_cost_usd"]) == pytest.approx(SETTLED_USD, abs=1e-6)
    first = report(tmp_path, PILOT)
    assert first["verdict"] == "pass" and first["failed_predicates"] == [] and not first["stopped"]
    assert first["preregistration_digest"] == cli.pinned_digest()
    assert len(first["code_revision"]["commit"]) == 40
    assert first["spend"]["settled_usd"] == pytest.approx(3 * SETTLED_USD, abs=1e-5)
    assert first["spend"]["settled_over_estimate"] < 1
    projection = first["projection"]
    full_estimate = q.subtotal(q.spend_table(built, q.plan_items()), "full")
    assert projection["items_remaining"] == 7 and projection["ceiling_usd"] == full_estimate
    assert projection["projected_full_usd"] == pytest.approx(7 * SETTLED_USD, abs=1e-4)
    assert (
        projection["band95_usd"][0] <= projection["projected_full_usd"] <= projection["ceiling_usd"]
    )
    for item in first["items"]:
        name = item["item"].split("#")[0]
        provenance = item["provenance"]
        assert provenance["visual_input_received"] is True
        assert provenance["frames_sent"] == len(built[name].frames)
        assert provenance["frame_hashes"] == [f.sha256 for f in built[name].frames]
        reply = json.loads((tmp_path / item["reply_file"]).read_text())
        assert json.loads(reply["raw_text"])["schema_version"] == 1
        receipts = json.loads((tmp_path / item["receipts_file"]).read_text())
        assert receipts["en"] and receipts["tr"]
    assert all(call["store"] is False and call["model"] == "gpt-6.1-sol" for call in sdk.calls)

    assert execute(tmp_path, FULL, "full", sdk, maximum="1.00") == 0
    out = capsys.readouterr().out
    assert "QUALIFICATION: pass" in out
    assert sorted(sdk.cases) == sorted(name for case in q.CASES for name in (case.name, case.name))
    assert len(rows(FULL)) == 7 and all(r["status"] == "settled" for r in rows(FULL))
    ledgered = journal(tmp_path)
    assert ledgered.committed_usd() == pytest.approx(10 * SETTLED_USD, abs=1e-4)
    assert ledgered.unknown() == [] and ledgered.committed_usd() < q.TOTAL_CAP_USD
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".txt"}:
            assert KEY not in path.read_text()


def test_a_reviewer_that_approves_everything_fails_the_pilot_and_the_full_run_stays_closed(
    tmp_path, capsys, ledger
):
    sdk = Reviewer(q.approves_everything)
    assert execute(tmp_path, PILOT, "pilot", sdk) == 1
    result = report(tmp_path, PILOT)
    assert result["verdict"] == "fail"
    assert "wrong_content_control#1:b.flagged" in result["failed_predicates"]
    assert "timing_miss_control#1:d.flagged" in result["failed_predicates"]
    assert not [f for f in result["failed_predicates"] if f.startswith("correct_edit")]
    assert journal(tmp_path).runs[PILOT]["verdict"] == "fail"

    capsys.readouterr()
    assert execute(tmp_path, FULL, "full", sdk, maximum="1.00") == cli.EXIT_REFUSED
    assert "FAIL pilot_passed:" in capsys.readouterr().out
    assert len(sdk.calls) == 3 and rows(FULL) == []


def test_a_void_reply_stops_the_run_and_only_the_call_that_was_made_is_paid(tmp_path, ledger):
    sdk = Reviewer(lambda case: "this is not the contract")
    assert execute(tmp_path, PILOT, "pilot", sdk) == 1
    result = report(tmp_path, PILOT)
    assert len(sdk.calls) == 1 and len(rows(PILOT)) == 1
    assert result["items"][0]["void"] == "not_json"
    assert "void review" in result["stopped"]
    assert result["not_run"] == ["wrong_content_control#1", "timing_miss_control#1"]
    assert rows(PILOT)[0]["status"] == "settled"


def test_an_unknown_outcome_keeps_the_liability_and_blocks_every_later_run(
    tmp_path, capsys, ledger
):
    sdk = Failing(HttpError(500))
    assert execute(tmp_path, PILOT, "pilot", sdk) == 1
    assert len(sdk.calls) == 1
    (row,) = rows(PILOT)
    assert row["status"] == "unknown" and row["settled_cost_usd"] is None
    ledgered = journal(tmp_path)
    assert ledgered.unknown() == [f"{PILOT}/correct_edit#1"]
    assert ledgered.committed_usd() == pytest.approx(float(row["estimated_cost_usd"]), abs=1e-6)

    capsys.readouterr()
    assert execute(tmp_path, "iqual-pg02-pilot", "pilot", sdk) == cli.EXIT_REFUSED
    assert "FAIL no_unreconciled_charge:" in capsys.readouterr().out
    assert len(sdk.calls) == 1


def test_a_definite_rejection_releases_the_reservation_and_leaves_no_liability(tmp_path, ledger):
    sdk = Failing(HttpError(400))
    assert execute(tmp_path, PILOT, "pilot", sdk) == 1
    (row,) = rows(PILOT)
    assert row["status"] == "released"
    ledgered = journal(tmp_path)
    assert ledgered.unknown() == [] and ledgered.committed_usd() == 0
    assert "provider_rejected" in report(tmp_path, PILOT)["stopped"]


def test_the_same_approved_run_never_executes_twice_even_without_the_journal(
    tmp_path, capsys, ledger
):
    sdk = Reviewer(q.perfect_reply)
    assert execute(tmp_path, PILOT, "pilot", sdk) == 0
    assert len(sdk.calls) == 3

    capsys.readouterr()
    assert execute(tmp_path, PILOT, "pilot", sdk) == cli.EXIT_REFUSED
    assert "FAIL run_not_yet_executed:" in capsys.readouterr().out

    (tmp_path / "spend.json").unlink()
    assert execute(tmp_path, PILOT, "pilot", sdk) == 1
    assert len(sdk.calls) == 3
    assert "provider_unknown_outcome" in report(tmp_path, PILOT)["stopped"]
    assert len(rows(PILOT)) == 3
