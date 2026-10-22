"""KRI-756 replay + property eval for the chip wording agent ``nova.chat.suggester``.

Free, offline, no network, no skia, no paid call: every model answer is a hand-authored
cassette or a seeded random string. The harness
(``chip_suggester_harness``) runs them through the REAL agent parse, the REAL task body
(``kria_suggester._run_agent``), the REAL label / ``why`` validators and the REAL serve join.

* E2 / G10  the served set obeys the contract for ARBITRARY model output: every chip is a live
            candidate whose message, kind, capability and anchor are copied from it, no
            flag-off / already-in-edit / dark capability appears, label and ``why`` obey the
            validator, Gemini text never reaches a label or message.
* E3        25 hand-authored cassettes (natural and adversarial) over the nermin-*, east-run and
            hand-authored contexts, plus a label-acceptance measurement.
* E4        the structlog events of contract section 8 (shown, agent_run, tap), end to end into
            the funnel.

What this does NOT prove: that Flash actually writes labels the validator accepts (the corpus
below is MY guess of the model), or what it costs. See tests/evals/README.md for the paid TODOs.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from types import SimpleNamespace
from unittest import mock

import pytest
from structlog.testing import capture_logs

from app.config import settings
from app.services import suggestion_chip_copy as rules
from app.services import suggestion_chips as sc

from . import chip_suggester_harness as cs
from . import suggestion_chips_harness as h

CASES = {c.name: c for c in h.all_cases()}
CASSETTES = cs.load_cassettes()
FUZZ_CASES = cs.fuzz_cases()
FUZZ_PER_CASE = 30
# Real cids from every context: a model that "remembers" an id from another thread.
FOREIGN_CIDS = sorted({c.cid for case in FUZZ_CASES for c in h.plan_for(case).pool})


# ── Inventory ────────────────────────────────────────────────────────────────────────


def test_cassette_inventory_is_well_formed():
    ids = [c["id"] for c in CASSETTES]
    assert len(ids) == len(set(ids)), "duplicate cassette ids"
    assert len(CASSETTES) >= 20
    for c in CASSETTES:
        assert c["description"].strip(), c["id"]
        assert c["context"] in CASES, f"{c['id']}: unknown context {c['context']!r}"
        assert ("raw_text" in c) != ("model_output" in c), f"{c['id']}: exactly one of raw/model"
        assert c["expect"], f"{c['id']}: add an expectation (a regression pin)"
        assert set(c["tags"]) & {"natural", "adversarial"}, c["id"]
    contexts = [CASES[c["context"]] for c in CASSETTES]
    assert {"first_prompt", "ready"} <= {x.state for x in contexts}
    assert {"en", "tr"} <= {x.language for x in contexts}
    assert any("nermin" in x.tags for x in contexts) and any("east_run" in x.tags for x in contexts)


def test_the_fuzz_matrix_spans_the_capability_matrix():
    tags = {t for c in FUZZ_CASES for t in c.tags}
    assert {"flag_off", "phone", "cloud", "tr", "speech", "music", "sfx", "transition"} <= tags
    assert {"first_prompt", "ready", "title_present", "captions", "v1", "nermin"} <= tags
    states = {h.plan_for(c).state for c in FUZZ_CASES}
    assert states == {"first_prompt", "ready"}
    kinds = {str(x.kind) for c in FUZZ_CASES for x in h.plan_for(c).pool}
    assert kinds >= {"hook", "captions", "sfx", "transition", "music", "title_animation"}


# ── E3: cassettes through the real task ──────────────────────────────────────────────


@pytest.mark.parametrize("cassette", [pytest.param(c, id=c["id"]) for c in CASSETTES])
def test_cassette_replay(cassette):
    rep = cs.replay_cassette(cassette, CASES)
    problems = cs.expectation_failures(cassette, rep)
    problems += cs.check_agent_set(rep.case, rep.served, rep.plan, rep.ctx)
    assert not problems, f"{cassette['id']}: " + "; ".join(problems)


@pytest.mark.parametrize("cassette", [pytest.param(c, id=c["id"]) for c in CASSETTES])
def test_cassette_is_exactly_one_agent_run_event_with_the_contract_fields(cassette):
    rep = cs.replay_cassette(cassette, CASES)
    run = rep.agent_run
    for key in ("agent_run_id", "thread_id", "set_id", "model", "prompt_version", "latency_ms"):
        assert key in run, f"agent_run lacks {key}"
    assert run["prompt_version"] == sc.PROMPT_VERSION
    assert run["n_in"] == len(rep.plan.pool)
    assert run["set_id"] == rep.plan.set_id
    if "fallback_reason" in run:
        assert run["fallback_reason"] in {
            "schema_error",
            "label_rejected",
            "timeout",
            "budget",
            "stale_digest",
            "flag_off",
            "run_cap",
        }
    else:
        for key in ("n_out", "n_dropped", "drop_reasons", "cost_usd"):
            assert key in run, f"a successful run must report {key}"


def test_natural_cassettes_mostly_use_the_agent_wording():
    """The happy path actually reaches the screen (guards against a validator that rejects
    everything: then wording would be 'fallback' for every natural answer)."""
    natural = [
        c for c in CASSETTES if "natural" in c["tags"] and c["expect"].get("wording") != "fallback"
    ]
    agent = [cs.replay_cassette(c, CASES).served for c in natural]
    assert len(agent) >= 3 and all(s.wording == "agent" for s in agent)
    sources = Counter(chip.label_source for s in agent for chip in s.chips)
    assert sources["agent"] >= 8, sources


def test_every_adversarial_class_falls_back_to_the_template_label_not_to_nothing():
    """A rejected label must cost the chip its WORDING only: the chip itself stays."""
    for cassette in CASSETTES:
        if "adversarial" not in cassette["tags"]:
            continue
        rep = cs.replay_cassette(cassette, CASES)
        template = {c.id: c for c in rep.template.chips}
        for chip in rep.served.chips:
            if chip.label_source == "template":
                assert chip.label == template[chip.id].label, (cassette["id"], chip.id)


# ── E2 / G10: the property over arbitrary model output ───────────────────────────────


@pytest.mark.parametrize("case", [pytest.param(c, id=c.name) for c in FUZZ_CASES])
def test_g10_fuzz_arbitrary_model_output_never_breaks_the_contract(case):
    bad: list[str] = []
    for i, raw in cs.fuzz_stream(case, FUZZ_PER_CASE, FOREIGN_CIDS):
        rep = cs.replay_raw(case, lambda plan, raw=raw: raw)
        problems = cs.check_agent_set(case, rep.served, rep.plan, rep.ctx)
        if problems:
            bad.append(f"#{i} {problems[:3]} raw={raw[:160]!r}")
    assert not bad, f"{case.name}: {len(bad)} violating answers\n" + "\n".join(bad[:5])


def test_g10_fuzz_is_not_vacuous():
    """The generator must reach the interesting branches or a green fuzz run means nothing."""
    outcomes: Counter[str] = Counter()
    drops: Counter[str] = Counter()
    for case in FUZZ_CASES:
        for _, raw in cs.fuzz_stream(case, 12, FOREIGN_CIDS):
            rep = cs.replay_raw(case, lambda plan, raw=raw: raw)
            run = rep.agent_run
            if run.get("fallback_reason") == "schema_error":
                outcomes["schema_error"] += 1
            if any(c.label_source == "agent" for c in rep.served.chips):
                outcomes["agent_label_shown"] += 1
            if any(c.why for c in rep.served.chips):
                outcomes["why_shown"] += 1
            if rep.served.wording == "fallback" and rep.served.chips:
                outcomes["template_set"] += 1
            for reason, n in (run.get("drop_reasons") or {}).items():
                drops[reason] += n
    total = len(FUZZ_CASES) * 12
    assert outcomes["schema_error"] >= total * 0.02, outcomes
    assert outcomes["agent_label_shown"] >= total * 0.10, outcomes
    assert outcomes["why_shown"] >= total * 0.03, outcomes
    assert outcomes["template_set"] >= total * 0.10, outcomes
    for reason in ("unknown_cid", "duplicate_kind", "over_limit"):
        assert drops[reason] > 0, f"fuzz never produced a {reason!r} drop: {drops}"
    assert any(k.startswith("label_") for k in drops) and any(k.startswith("why_") for k in drops)


def test_g10_resolve_wording_property_without_the_agent():
    """The same property one layer down, 4000 random rankings straight into ``resolve_wording``
    + ``assemble_set`` (no JSON, no agent): shapes the parser would never forward still cannot
    mint, rename or retarget a chip."""
    rng = random.Random("kri756-resolve")
    checked = 0
    for case in FUZZ_CASES:
        ctx = h.build_context(case)
        plan = sc.build_candidates(ctx)
        pool = [c.cid for c in plan.pool]
        for _ in range(90):
            ranked = [
                (
                    rng.choice(pool + [f"sc_{rng.getrandbits(48):012x}", "", "x" * 40]),
                    rng.choice(cs.LABELS_JUNK),
                    rng.choice(cs.WHYS_JUNK),
                )
                for _ in range(rng.randint(0, 9))
            ]
            default = rng.choice([None, *cs.LABELS_JUNK])
            wording, _stats = sc.resolve_wording(ctx, plan, default_label=default, ranked=ranked)
            out = sc.assemble_set(ctx, plan, wording)
            problems = cs.check_agent_set(case, out, plan, ctx)
            assert not problems, f"{case.name}: {problems[:3]} ranked={ranked}"
            assert out.model_dump_json() == sc.assemble_set(ctx, plan, wording).model_dump_json()
            checked += 1
    assert checked >= 3500


def test_g10_a_stored_wording_is_revalidated_at_serve_time():
    """Never trust the store: a poisoned Redis entry (label with a digit, ungrounded why, a cid
    that is not live, a duplicate kind) is cleaned again by ``assemble_set``."""
    case = CASES["nermin-matcha-update::first_prompt"]
    ctx = h.build_context(case)
    plan = sc.build_candidates(ctx)
    live = {str(c.kind): c.cid for c in plan.pool}
    poisoned = sc.wording_from_dict(
        {
            "status": "agent",
            "default_label": "Make it viral 3x",
            "ranked": [
                {
                    "cid": live["hook"],
                    "label_template": "Add 3 hooks",
                    "why": "Skateboards at dusk",
                },
                {"cid": "sc_ffffffffffff", "label_template": "Add captions", "why": None},
                {"cid": live["hook"], "label_template": "Open with a hook", "why": None},
                {"cid": live["sfx"], "label_template": "Add sound effects", "why": "c1 at 0:04"},
            ],
        }
    )
    out = sc.assemble_set(ctx, plan, poisoned)
    assert not cs.check_agent_set(case, out, plan, ctx)
    by_kind = cs.chips_by_kind(out)
    assert by_kind["hook"].label == "Add a hook to the opening"
    # The agent's ungrounded why is dropped; only the deterministic evidence line remains.
    assert by_kind["hook"].why == h.expected_evidence(case, by_kind["hook"], out.state)
    assert by_kind["default_edit"].label_source == "template"
    assert [c.kind for c in out.chips] == ["default_edit", "hook", "sfx"]


# Negative controls: the G10 oracle must be able to fail, one violation per invariant.
def _served(case_name="nermin-matcha-update::first_prompt"):
    case = CASES[case_name]
    ctx = h.build_context(case)
    plan = sc.build_candidates(ctx)
    return case, ctx, plan, sc.assemble_set(ctx, plan)


@pytest.mark.parametrize(
    "name,mutate",
    [
        ("message", lambda o, p: setattr(o.chips[1], "message", "Delete my project")),
        ("cid", lambda o, p: setattr(o.chips[1], "id", "sc_ffffffffffff")),
        ("kind", lambda o, p: setattr(o.chips[1], "kind", "pinned_text")),
        ("capability", lambda o, p: setattr(o.chips[1], "capability", "everything")),
        ("anchor", lambda o, p: setattr(o.chips[1].anchor, "media_id", "ghost")),
        ("requires_protocol", lambda o, p: setattr(o.chips[1], "requires_protocol", 99)),
        (
            "agent_label_digit",
            lambda o, p: (
                setattr(o.chips[1], "label_source", "agent"),
                setattr(o.chips[1], "label", "Add 3 hooks"),
            ),
        ),
        (
            "agent_label_wrong_kind",
            lambda o, p: (
                setattr(o.chips[1], "label_source", "agent"),
                setattr(o.chips[1], "label", "Add captions"),
            ),
        ),
        ("ungrounded_why", lambda o, p: setattr(o.chips[1], "why", "Skateboards at dusk")),
        ("long_why", lambda o, p: setattr(o.chips[1], "why", "whisking " + "x" * 60)),
        ("label_leak", lambda o, p: setattr(o.chips[1], "label", "Hook on whisking close-up")),
        ("duplicate_kind", lambda o, p: o.chips.append(o.chips[1].model_copy(update={"rank": 4}))),
        (
            "too_many_levers",
            lambda o, p: o.chips.extend(
                [
                    o.chips[1].model_copy(
                        update={"rank": 4 + i, "id": f"sc_x{i}", "kind": f"hook{i}"}
                    )
                    for i in range(3)
                ]
            ),
        ),
        ("rank_gap", lambda o, p: setattr(o.chips[2], "rank", 9)),
        ("default_edit_moved", lambda o, p: o.chips.reverse()),
    ],
)
def test_g10_oracle_catches_each_violation(name, mutate):
    case, ctx, plan, out = _served()
    assert not cs.check_agent_set(case, out, plan, ctx), "the baseline must be clean"
    mutate(out, plan)
    assert cs.check_agent_set(case, out, plan, ctx), f"G10 let a {name!r} violation through"


def test_g10_oracle_catches_a_flag_off_kind_that_was_never_a_candidate():
    """A chip for sfx in a context where sfx is gated off: the id is not live, so G10 fails."""
    case = CASES["fp_sfx_flag_off"]
    ctx = h.build_context(case)
    plan = sc.build_candidates(ctx)
    out = sc.assemble_set(ctx, plan)
    sfx_cid = guessed = cs.guessed_cid(plan, "sfx")
    out.chips.append(
        sc.SuggestionChipOut(
            id=sfx_cid,
            kind="sfx",
            label="Add sound effects",
            message="Add sound effects",
            rank=len(out.chips) + 1,
        )
    )
    problems = cs.check_agent_set(case, out, plan, ctx)
    assert any("not a live candidate" in p for p in problems) and guessed


def _fuzz_violations(n_per_case=25, cases=None):
    found = 0
    for case in cases or FUZZ_CASES:
        for _, raw in cs.fuzz_stream(case, n_per_case, FOREIGN_CIDS):
            rep = cs.replay_raw(case, lambda plan, raw=raw: raw)
            found += bool(cs.check_agent_set(case, rep.served, rep.plan, rep.ctx))
    return found


def test_g10_fuzz_would_catch_a_validator_that_stopped_validating(monkeypatch):
    """Mutation controls on the ENGINE (not the oracle): break each safeguard in turn and the
    fuzz must go red. Without these a green fuzz run could just mean the generator is weak."""
    assert _fuzz_violations(8) == 0
    monkeypatch.setattr(sc, "validate_label", lambda template, **kw: (template or None, None))
    assert _fuzz_violations(8) > 0, "label validation was disabled and nothing noticed"
    monkeypatch.undo()
    monkeypatch.setattr(sc, "validate_why", lambda why, **kw: (why or None, None))
    assert _fuzz_violations(8) > 0, "why validation was disabled and nothing noticed"


def test_g10_fuzz_would_catch_an_assembler_that_trusts_the_model(monkeypatch):
    """If the pool check were removed, an invented cid could become a chip."""
    real = sc.resolve_wording

    def trusting(ctx, plan, *, default_label, ranked):
        wording, stats = real(ctx, plan, default_label=default_label, ranked=ranked)
        extra = sc.RankedWording(cid="sc_deadbeef0000", label_template="Add captions", why=None)
        return sc.Wording(status="agent", ranked=(*wording.ranked, extra)), stats

    monkeypatch.setattr(sc, "resolve_wording", trusting)
    # cs.replay_raw reaches resolve_wording through the task module's own import
    from app.tasks import kria_suggester

    monkeypatch.setattr(kria_suggester, "resolve_wording", trusting)
    found = 0
    case = CASES["nermin-matcha-update::first_prompt"]
    for _, raw in cs.fuzz_stream(case, 20, FOREIGN_CIDS):
        rep = cs.replay_raw(case, lambda plan, raw=raw: raw)
        # the served join re-checks cids against the pool, so a trusting task is contained...
        assert not cs.check_agent_set(case, rep.served, rep.plan, rep.ctx)
        # ...but the stored wording is polluted, which is exactly what the oracle on the store sees
        found += any(
            r["cid"] == "sc_deadbeef0000"
            for r in (rep.stored or {}).get("wording", {}).get("ranked", [])
        )
    assert found > 0


# ── E3: label acceptance (how strict is the closed lexicon?) ─────────────────────────


def test_label_corpus_acceptance_rate_and_the_strictness_tradeoff():
    """Recall of plausible creator-voiced labels vs. rejection of cringe / wrong / hostile ones.

    The bars are a floor for "the agent is worth enabling": if recall drops, the wording layer
    mostly falls back to template labels and only the ranking and ``why`` remain. The corpus
    is hand-authored by the eval lane (NOT recorded model output)."""
    result = cs.run_label_corpus()
    assert result.n_accept >= 80 and result.n_reject >= 40
    assert result.rejection_rate == 1.0, [
        r["label"] for r in result.rows if r["want"] == "reject" and r["accepted"]
    ]
    assert result.recall >= 0.80, (
        f"label recall {result.recall:.0%}: the lexicon rejects too many plausible labels; "
        "loosen it (NOT to stoplist-only) or do not enable the agent"
    )
    for lang, (ok, total) in result.by("language").items():
        assert ok / total >= 0.75, (lang, ok, total)
    # Loosening to stoplist-only would let a large share of bad labels through: the lexicon
    # layer is what stops wrong-kind, content-leaking and wrong-language labels.
    assert result.lexicon_only_stops >= 0.4 * result.n_reject


# ── Known gaps in the validators (strict xfails: they fail the day the gap closes) ───

_WHY_GAP = (
    "KNOWN GAP (backend, KRI-756): validate_why only needs ONE footage word, so any call to "
    "action or off-topic clause that mentions a footage word and fits 48 chars is shown as the "
    "chip subtitle. Display-only (never sent, never an overlay), but it is a free-text channel "
    "under a model's control. Fix: also require most content words to come from the footage "
    "vocabulary or a closed function-word list."
)


@pytest.mark.parametrize(
    "why",
    [
        "pouring matcha, tap Buy Now at example dot com",
        "The pour, plus your password please",
        "Tap to win a free iPhone while pouring matcha",
    ],
)
@pytest.mark.xfail(strict=True, reason=_WHY_GAP)
def test_a_why_cannot_carry_a_call_to_action(why):
    vocab = rules.footage_vocabulary(
        [{"subject": "pouring matcha into a glass", "notable_moments": [{"label": "the pour"}]}]
    )
    assert rules.validate_why(why, language="en", vocabulary=vocab)[0] is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP (backend, KRI-756): the why language check is one-directional (it rejects "
        "non-ASCII text on an English thread but accepts English text on a Turkish thread), so "
        "a Turkish creator can be shown an English subtitle."
    ),
)
def test_a_why_is_in_the_thread_language():
    vocab = rules.footage_vocabulary([{"subject": "pouring matcha into a glass"}])
    assert (
        rules.validate_why("Pouring matcha at the counter", language="tr", vocabulary=vocab)[0]
        is None
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP (backend, KRI-756): _worded labels a chip label_source='agent' whenever the "
        "agent's label validates, even when it is word for word the template label, so the "
        "Flash-vs-template split of taps and kept over-counts the agent arm with identical chips."
    ),
)
def test_an_agent_label_identical_to_the_template_is_attributed_to_the_template():
    cassette = next(c for c in CASSETTES if c["id"] == "reorder_follows_model")
    rep = cs.replay_cassette(cassette, CASES)
    template = {c.id: c for c in rep.template.chips}
    for chip in rep.served.chips:
        if chip.label == template[chip.id].label:
            assert chip.label_source == "template", (chip.kind, chip.label)


# ── The serve layer never lets the agent contradict the wire contract ────────────────


def test_the_default_edit_message_is_never_agent_text():
    for cassette in CASSETTES:
        rep = cs.replay_cassette(cassette, CASES)
        first = rep.served.chips[0] if rep.served.chips else None
        if first is not None and rep.served.state == "first_prompt":
            assert first.kind == "default_edit" and first.message == "Suggest an edit."
            assert first.rank == 1 and first.default_edit is True


def test_ready_state_never_carries_a_default_edit_and_first_prompt_never_loses_it():
    for case in FUZZ_CASES:
        for _, raw in cs.fuzz_stream(case, 5, FOREIGN_CIDS):
            rep = cs.replay_raw(case, lambda plan, raw=raw: raw)
            kinds = [c.kind for c in rep.served.chips]
            if rep.served.state == "ready":
                assert "default_edit" not in kinds
            elif rep.plan.default is not None:
                assert kinds[0] == "default_edit"


def test_no_skia_is_imported_by_the_chip_eval():
    """tests/evals runs in the structural CI job, which has no libEGL. Checked in a fresh
    interpreter: other eval modules in the same pytest process do import skia."""
    import os
    import subprocess

    code = (
        "import sys;"
        "from tests.evals import chip_suggester_harness as cs, suggestion_chips_harness as h;"
        "cases={c.name:c for c in h.all_cases()};"
        "cs.replay_cassette(cs.load_cassettes()[0], cases);"
        "[h.run_case(c) for c in cases.values()];"
        "sys.exit(1 if 'skia' in sys.modules else 0)"
    )
    env = {
        **os.environ,
        "STORAGE_BUCKET": "nova-test",
        "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
    }
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=240
    )
    assert done.returncode == 0, (done.stdout + done.stderr)[-800:]


# ── E4: telemetry events, end to end ─────────────────────────────────────────────────


class _Rows:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _RingDb:
    """Just enough of an AsyncSession for ``persist_suggestion_set`` (one thread row)."""

    def __init__(self, state=None):
        self.state = dict(state or {})
        self.commits = 0

    async def execute(self, stmt):
        compiled = stmt.compile()
        if "state" in compiled.params and type(stmt).__name__ == "Update":
            self.state = json.loads(json.dumps(compiled.params["state"], default=str))
            return _Rows(None)
        return _Rows((self.state,))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def get(self, model, key):
        return None


def _event(events, name):
    return [e for e in events if e.get("event") == f"suggestion_chips.{name}"]


@pytest.fixture
def chips_on(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])


async def test_shown_is_logged_once_per_final_set_and_not_for_the_pending_one(chips_on):
    case = CASES["fp_understanding_pending"]
    pending = sc.build_suggestion_set(h.build_context(case))
    assert pending.status == "pending"
    final_case = CASES["fp_speech_and_broll"]
    final = sc.build_suggestion_set(h.build_context(final_case))
    db = _RingDb()
    with capture_logs() as events:
        wrote_pending = await sc.persist_suggestion_set(
            db, "t1", pending, media_digest="d", protocol=13
        )
        wrote_final = await sc.persist_suggestion_set(
            db,
            "t1",
            final,
            media_digest="d",
            protocol=13,
            witness_tiers=h.witness_tiers(final_case),
        )
        wrote_again = await sc.persist_suggestion_set(
            db, "t1", final, media_digest="d", protocol=13
        )
    shown = _event(events, "shown")
    assert wrote_pending and wrote_final and not wrote_again
    assert len(shown) == 1, "shown means SERVED, and only once per final set_id"
    row = shown[0]
    assert row["set_id"] == final.set_id and row["state"] == "first_prompt"
    assert row["wording"] == final.wording and row["n"] == len(final.chips)
    assert row["kinds"] == [c.kind for c in final.chips]
    assert row["thread_id"] == "t1"
    assert set(row["witness_tiers"].values()) <= {"strategy", "editor", "policy_only"}
    assert row["witness_tiers"]["default_edit"] == "policy_only"
    ring = {e["set_id"]: e for e in db.state[sc.RING_KEY]}
    assert ring[final.set_id]["final"] is True and ring[pending.set_id]["final"] is False


async def test_a_pending_set_is_replaced_in_place_by_the_final_one_with_the_same_set_id(chips_on):
    case = CASES["fp_speech_and_broll"]
    ctx = h.build_context(case)
    plan = sc.build_candidates(ctx)
    pending = sc.assemble_set(ctx, plan, None, pending=True, retry_after_s=2)
    final = sc.assemble_set(ctx, plan, None)
    assert pending.set_id == final.set_id and pending.status == "pending" and final.status == "ok"
    db = _RingDb()
    with capture_logs() as events:
        await sc.persist_suggestion_set(db, "t1", pending, media_digest="d", protocol=13)
        assert db.state[sc.RING_KEY][0]["final"] is False
        await sc.persist_suggestion_set(db, "t1", final, media_digest="d", protocol=13)
    assert len(db.state[sc.RING_KEY]) == 1 and db.state[sc.RING_KEY][0]["final"] is True
    assert len(_event(events, "shown")) == 1


async def test_tap_is_stamped_with_the_eight_contract_keys_and_logged(chips_on):
    case = CASES["fp_speech_and_broll"]
    out = sc.build_suggestion_set(h.build_context(case))
    db = _RingDb()
    await sc.persist_suggestion_set(db, "t1", out, media_digest="d", protocol=13)
    thread = SimpleNamespace(id="t1", creator_id="u", state=db.state, active_plan_item_id=None)
    captions = next(c for c in out.chips if c.kind == "captions")
    with capture_logs() as events:
        stamped = await sc.suggestion_tap_payload(
            db, thread, captions.message, suggestion={"set_id": out.set_id, "chip_id": captions.id}
        )
    stamp = stamped["suggestion"]
    assert set(stamp) == {
        "set_id",
        "chip_id",
        "kind",
        "rank",
        "state",
        "label_source",
        "prompt_version",
        "default_edit",
    }
    assert stamp["set_id"] == out.set_id and stamp["chip_id"] == captions.id
    assert (stamp["kind"], stamp["rank"], stamp["state"]) == (
        "captions",
        captions.rank,
        "first_prompt",
    )
    assert stamp["label_source"] == captions.label_source and stamp["default_edit"] is False
    assert stamp["prompt_version"] == sc.PROMPT_VERSION
    tap = _event(events, "tap")
    assert len(tap) == 1 and {k: tap[0][k] for k in stamp} == stamp
    assert tap[0]["thread_id"] == "t1"


async def test_the_stamp_survives_a_json_round_trip_into_the_funnel(chips_on):
    """GET -> persist -> tap -> user_message payload -> derive_funnel: the numbers a PM reads."""
    from scripts import suggestion_chip_funnel as funnel

    case = CASES["fp_speech_and_broll"]
    out = sc.build_suggestion_set(h.build_context(case))
    db = _RingDb()
    await sc.persist_suggestion_set(db, "t1", out, media_digest="d", protocol=13)
    thread = SimpleNamespace(id="t1", creator_id="u", state=db.state, active_plan_item_id=None)
    first = out.chips[0]
    stamped = await sc.suggestion_tap_payload(
        db, thread, first.message, suggestion={"set_id": out.set_id, "chip_id": first.id}
    )
    events = [
        {
            "sequence": 1,
            "event_type": "user_message",
            "payload": {"message": first.message, **stamped},
        },
        {"sequence": 2, "event_type": "draft_applied", "payload": {}},
    ]
    result = funnel.derive_funnel([funnel.ThreadRecord("t1", db.state[sc.RING_KEY], events)])
    cell = result.cells[("first_prompt", "default_edit", 1)]
    assert (cell.shown, cell.tapped, cell.completed, cell.kept) == (1, 1, 1, 1)
    assert result.orphan_taps == 0
    assert result.by_source["template"].tapped == 1 and result.by_wording["fallback"].tapped == 1


async def test_a_typed_lookalike_without_the_suggestion_field_is_still_attributed_by_text(chips_on):
    """Documented bias: the stamp has no explicit-vs-text flag (the wire contract is fixed), so
    a typed 'Suggest an edit.' and a media-only send both count as a tap of chip 1."""
    case = CASES["fp_speech_and_broll"]
    out = sc.build_suggestion_set(h.build_context(case))
    db = _RingDb()
    await sc.persist_suggestion_set(db, "t1", out, media_digest="d", protocol=13)
    thread = SimpleNamespace(id="t1", creator_id="u", state=db.state, active_plan_item_id=None)
    stamped = await sc.suggestion_tap_payload(db, thread, "suggest an edit.")
    assert stamped["suggestion"]["default_edit"] is True and stamped["suggestion"]["rank"] == 1
    assert await sc.suggestion_tap_payload(db, thread, "make it moodier") == {}


async def test_flag_off_emits_no_event_and_no_payload_key(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])
    case = CASES["fp_speech_and_broll"]
    out = sc.build_suggestion_set(h.build_context(case))
    thread = SimpleNamespace(
        id="t1",
        creator_id="u",
        state={sc.RING_KEY: [{"set_id": out.set_id, "chips": []}]},
        active_plan_item_id=None,
    )
    from app.services import suggestion_serve

    with capture_logs() as events:
        assert await sc.suggestion_tap_payload(_RingDb(), thread, "Suggest an edit.") == {}
        with mock.patch("app.tasks.kria_suggester.kria_suggester.apply_async") as apply:
            assert suggestion_serve.enqueue_suggester("t1", trigger="lazy", creator_id="u") is False
            assert suggestion_serve.enqueue_for_item("item-1", trigger="render_ready") == 0
            apply.assert_not_called()
    assert not [e for e in events if str(e.get("event", "")).startswith("suggestion_chips.")]
    assert settings.suggestion_chips_for("u") is False


# ── agent_run fallback reasons, each from its real cause ─────────────────────────────


def _ctx_plan(case_name="nermin-matcha-update::first_prompt"):
    case = CASES[case_name]
    ctx = h.build_context(case)
    return case, ctx, sc.build_candidates(ctx)


class _Raising:
    def __init__(self, exc):
        self.exc = exc
        self.supports_clarification_retry = False

    def invoke(self, **kwargs):
        raise self.exc


def _run_with_client(client, case_name="nermin-matcha-update::first_prompt"):
    from app.tasks import kria_suggester

    _case, ctx, plan = _ctx_plan(case_name)
    puts: list[dict] = []
    with (
        mock.patch("app.agents._model_client.default_client", lambda: client),
        mock.patch.object(kria_suggester.store, "put_result", lambda t, d, v: puts.append(v)),
        capture_logs() as events,
    ):
        result = kria_suggester._run_agent(ctx.thread_id, ctx, plan, "eval")
    return result, puts, _event(events, "agent_run")


@pytest.mark.parametrize(
    "exc_factory,reason",
    [
        (
            lambda: __import__("app.agents._runtime", fromlist=["x"]).AiBudgetExceededError(
                scope="user", reset_at="2026-10-20T00:00:00Z", cached_behavior_available=False
            ),
            "budget",
        ),
        (lambda: TimeoutError("model timed out"), "timeout"),
        (lambda: RuntimeError("provider exploded"), "timeout"),
    ],
)
def test_agent_run_fallback_reason_budget_and_timeout(exc_factory, reason):
    result, puts, runs = _run_with_client(_Raising(exc_factory()))
    assert result == {"status": "fallback", "fallback_reason": reason}
    assert len(puts) == 1 and puts[0]["status"] == "fallback" and puts[0]["reason"] == reason
    assert len(runs) == 1 and runs[0]["fallback_reason"] == reason
    assert runs[0]["prompt_version"] == sc.PROMPT_VERSION and "latency_ms" in runs[0]


def test_a_failed_run_still_serves_a_complete_template_set():
    _result, puts, _runs = _run_with_client(_Raising(RuntimeError("boom")))
    from app.services import suggestion_serve

    case, ctx, plan = _ctx_plan()
    served = suggestion_serve._final(ctx, plan, puts[0])
    assert served.status == "ok" and served.wording == "fallback"
    assert [c.kind for c in served.chips] == ["default_edit", "hook", "sfx", "transition"]
    assert not cs.check_agent_set(case, served, plan, ctx)


def test_label_rejected_is_reported_when_nothing_usable_survives():
    cassette = next(c for c in CASSETTES if c["id"] == "hype_labels")
    run = cs.replay_cassette(cassette, CASES).agent_run
    assert run["fallback_reason"] == "label_rejected" and run["n_dropped"] >= 3


def test_run_cap_and_stale_digest_and_flag_off_come_from_the_task_entry(monkeypatch):
    """The three reasons that are decided BEFORE a model call, through the real Celery body."""
    from app.tasks import kria_suggester

    _case, ctx, plan = _ctx_plan()
    puts: list[dict] = []

    async def prepared(thread_id, language, protocol):
        return ctx, plan

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggester_max_runs_per_thread", 12)
    store = kria_suggester.store
    monkeypatch.setattr(kria_suggester, "_prepare", prepared)
    monkeypatch.setattr(store, "get_hint", lambda t: {})
    monkeypatch.setattr(store, "get_result", lambda t, d: None)
    monkeypatch.setattr(store, "try_lock", lambda *a, **k: True)
    monkeypatch.setattr(store, "release_lock", lambda *a, **k: None)
    monkeypatch.setattr(store, "put_result", lambda t, d, v: puts.append(v))
    monkeypatch.setattr(store, "bump_runs", lambda t: 13)  # over the cap of 12
    boom = mock.Mock(side_effect=AssertionError("no model call may happen"))
    monkeypatch.setattr(kria_suggester, "_run_agent", boom)
    with capture_logs() as events:
        run_cap = kria_suggester.kria_suggester.run(ctx.thread_id)
        stale = kria_suggester.kria_suggester.run(ctx.thread_id, expected_digest="not-this-one")
    reasons = [e.get("fallback_reason") for e in _event(events, "agent_run")]
    assert run_cap["status"] == "run_cap" and stale["status"] == "stale_digest"
    assert reasons == ["run_cap", "stale_digest"]
    assert puts and puts[0]["status"] == "fallback" and puts[0]["reason"] == "run_cap"
    boom.assert_not_called()

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])
    with capture_logs() as events:
        off = kria_suggester.kria_suggester.run(ctx.thread_id)
    assert off["status"] == "flag_off" and not _event(events, "agent_run")
    boom.assert_not_called()


def test_the_zero_run_lever_disables_the_agent_only(monkeypatch):
    from app.services import suggestion_serve
    from app.tasks import kria_suggester

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggester_max_runs_per_thread", 0)
    assert suggestion_serve.agent_enabled() is False
    assert kria_suggester.kria_suggester.run("t")["status"] == "run_cap"
    # the deterministic set is untouched by the lever
    _case, ctx, plan = _ctx_plan()
    assert [c.kind for c in sc.assemble_set(ctx, plan).chips][0] == "default_edit"
