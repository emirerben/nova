"""Scorer, thread roll-up, KPI and the wrong-landmark rate."""

from __future__ import annotations

from .models import (
    ClipFactRecord,
    ClipRecord,
    FinalPlan,
    Footage,
    PlanClip,
    PlanText,
    Requirement,
    RFFixture,
    ThreadResult,
    Turn,
    TurnResult,
)
from .scorer import compute_kpi, reply_overclaims, score, score_thread, wrong_landmark_rate

FOOT = Footage(
    footage_id="f",
    clips=[ClipRecord(clip_id=c, duration_s=3.0) for c in ("a", "b", "c")],
)


def _req(rid: str, checker: str, params=None, *, turn: int = 0, claims=None, rtype="selection"):
    return Requirement(
        id=rid,
        kind="select",
        request_type=rtype,
        checker=checker,
        params=params or {},
        introduced_in_turn=turn,
        claim_terms=claims or [],
    )


def _plan(*ids: str, title: str | None = None) -> FinalPlan:
    clips = [PlanClip(clip_id=c, start_s=i * 2.0, end_s=i * 2.0 + 2.0) for i, c in enumerate(ids)]
    texts = [PlanText(id="t", role="title", text=title, start_s=0, end_s=2)] if title else []
    return FinalPlan(clips=clips, texts=texts)


# ── reply honesty ────────────────────────────────────────────────────────────


def test_overclaim_needs_a_claim_term_a_non_met_status_and_no_disclaimer():
    req = _req("r", "select_include", claims=["everything else is unchanged"])
    lie = "Edit text. Everything else is unchanged."
    assert reply_overclaims(req, "unmet", lie)
    assert reply_overclaims(req, "partial", lie)
    assert not reply_overclaims(req, "met", lie)
    assert not reply_overclaims(req, "unmet", None)
    assert not reply_overclaims(_req("r", "select_include"), "unmet", lie)


def test_a_not_done_marker_clears_the_overclaim():
    req = _req("r", "select_include", claims=["everything else is unchanged"])
    honest = "Edit text. Not done: reorder the clips. Everything else is unchanged."
    assert not reply_overclaims(req, "unmet", honest)
    assert not reply_overclaims(req, "unmet", "I couldn't do that. Everything else is unchanged.")


def test_unchanged_alone_is_not_a_disclaimer():
    req = _req("r", "select_include", claims=["15.second"])
    assert reply_overclaims(req, "unmet", "Made it a 15-second cut. Everything else is unchanged.")


# ── score ────────────────────────────────────────────────────────────────────


def test_score_reports_status_reason_and_honesty_per_requirement():
    reqs = [
        _req("all", "select_include", {"all": True}, claims=["everything"]),
        _req("skip", "select_exclude", {"clip_ids": ["c"]}),
    ]
    out = score(reqs, _plan("a", "b"), "Done. Everything is unchanged.", footage=FOOT)
    by_id = {s.requirement_id: s for s in out}
    assert by_id["all"].status == "partial" and by_id["all"].reply_overclaims
    assert by_id["skip"].status == "met" and not by_id["skip"].reply_overclaims


def test_addressed_limits_the_honesty_audit():
    reqs = [_req("all", "select_include", {"all": True}, claims=["everything"])]
    out = score(reqs, _plan("a"), "everything is fine", footage=FOOT, addressed=set())
    assert out[0].status == "unmet" and not out[0].reply_overclaims


# ── thread roll-up ───────────────────────────────────────────────────────────


def _thread_fixture(requirements: list[Requirement], n_turns: int = 3) -> RFFixture:
    return RFFixture(
        fixture_id="t",
        provenance="prod_capture",
        footage="f",
        turns=[Turn(turn_id=f"t{i}", user_message=f"m{i}") for i in range(n_turns)],
        requirements=requirements,
    )


def _results(plans_and_replies: list[tuple[FinalPlan, str | None]]) -> list[TurnResult]:
    return [
        TurnResult(turn_id=f"t{i}", engine="recorded", plan_after=p, reply=r)
        for i, (p, r) in enumerate(plans_and_replies)
    ]


def test_state_requirement_is_judged_on_the_final_edit_not_the_turn_it_was_asked():
    fx = _thread_fixture([_req("all", "select_include", {"all": True}, turn=0)])
    turns = _results([(_plan("a"), None), (_plan("a", "b"), None), (_plan("a", "b", "c"), None)])
    assert score_thread(fx, FOOT, turns)[0].status == "met"
    worse = _results(
        [(_plan("a", "b", "c"), None), (_plan("a", "b", "c"), None), (_plan("a"), None)]
    )
    assert score_thread(fx, FOOT, worse)[0].status == "unmet"


def test_event_requirement_is_judged_once_on_the_turn_it_was_asked():
    """`restructure_changed` compares with the edit before THAT turn. A later change must not
    launder an ignored request into a success, and an unchanged later turn must not undo one."""
    fx = _thread_fixture([_req("redo", "restructure_changed", turn=1, rtype="restructure")])
    ignored_then_changed = _results(
        [(_plan("a"), None), (_plan("a"), None), (_plan("b", "a"), None)]
    )
    assert score_thread(fx, FOOT, ignored_then_changed)[0].status == "unmet"
    done_then_idle = _results(
        [(_plan("a"), None), (_plan("b", "a"), None), (_plan("b", "a"), None)]
    )
    assert score_thread(fx, FOOT, done_then_idle)[0].status == "met"


def test_requirements_start_counting_at_their_introduction_turn():
    fx = _thread_fixture([_req("late", "select_include", {"all": True}, turn=2)])
    turns = _results([(_plan("a"), None), (_plan("a"), None), (_plan("a", "b", "c"), None)])
    (item,) = score_thread(fx, FOOT, turns)
    assert item.status == "met"


def test_reply_honesty_accumulates_over_addressing_turns_only():
    req = _req("all", "select_include", {"all": True}, turn=0, claims=["everything else"])
    fx = _thread_fixture([req])
    fx.turns[2].addresses = ["all"]
    lie = "Edit text. Everything else is unchanged."
    # turn 0 replies honestly and turn 1 is not responsible; turn 2 lies.
    turns = _results([(_plan("a"), "Not done: the rest."), (_plan("a"), lie), (_plan("a"), lie)])
    assert score_thread(fx, FOOT, turns)[0].reply_overclaims
    turns = _results([(_plan("a"), "Not done: the rest."), (_plan("a"), lie), (_plan("a"), "ok")])
    assert not score_thread(fx, FOOT, turns)[0].reply_overclaims


# ── KPI ──────────────────────────────────────────────────────────────────────


def _result(statuses: dict[str, str], *, unrecorded=False, overclaim: set[str] = frozenset()):
    from .models import RequirementScore

    scores = [
        RequirementScore(
            requirement_id=f"r{i}",
            request_type=rtype,
            status=status,
            reply_overclaims=f"r{i}" in overclaim,
        )
        for i, (rtype, status) in enumerate(statuses.items())
    ]
    return ThreadResult(
        fixture_id="x", provenance="prod_capture", turns=[], scores=scores, unrecorded=unrecorded
    )


def test_kpi_is_strict_met_share_per_request_type():
    a = _result({"duration": "met", "pacing": "partial", "selection": "unmet"}, overclaim={"r2"})
    b = _result({"duration": "unmet", "pacing": "met", "selection": "met"})
    kpi = compute_kpi([a, b])
    assert kpi.overall.total == 6 and kpi.overall.met == 3 and kpi.overall.partial == 1
    assert kpi.met_share == 0.5
    assert kpi.by_type["duration"].met_share == 0.5
    assert kpi.by_type["pacing"].partial == 1
    assert kpi.overall.overclaims == 1
    assert "title_exact" not in kpi.by_type  # only measured types are reported


def test_kpi_leaves_unrecorded_threads_out_and_counts_them():
    kpi = compute_kpi([_result({"duration": "met"}), _result({}, unrecorded=True)])
    assert kpi.threads_scored == 1 and kpi.threads_unrecorded == 1 and kpi.met_share == 1.0
    empty = compute_kpi([])
    assert empty.met_share is None


# ── wrong-landmark rate ──────────────────────────────────────────────────────


def _landmark_clip(cid: str, guess: str | None, truth: str | None, prov="inferred") -> ClipRecord:
    facts = [ClipFactRecord(kind="landmark", value=guess, provenance=prov)] if guess else []
    return ClipRecord(clip_id=cid, duration_s=2.0, facts=facts, true_landmark=truth)


def test_wrong_landmark_rate_is_none_until_guesses_exist():
    foot = Footage(footage_id="f", clips=[_landmark_clip("a", None, "Galata Tower")])
    assert wrong_landmark_rate(foot) == (None, 0)


def test_wrong_landmark_rate_counts_only_inferred_guesses_with_ground_truth():
    foot = Footage(
        footage_id="f",
        clips=[
            _landmark_clip("a", "Galata Tower", "Galata Tower"),
            _landmark_clip("b", "Dolmabahçe Palace", "Beylerbeyi Palace"),
            _landmark_clip("c", "galata kulesi", "Galata Kulesi"),
            _landmark_clip("d", "Maiden's Tower", None),  # no ground truth: not judged
            _landmark_clip("e", "Rumeli Fortress", "Rumeli Fortress", prov="exif"),  # not inferred
        ],
    )
    assert wrong_landmark_rate(foot) == (1 / 3, 3)


def test_wrong_landmark_rate_can_score_an_external_run_against_the_same_footage():
    """A live landmark_guess run supplies `guesses`; they replace the fixture's inferred facts."""
    foot = Footage(
        footage_id="f",
        clips=[
            _landmark_clip("a", "Old Guess", "Galata Tower"),
            _landmark_clip("b", None, "Beylerbeyi Palace"),
            _landmark_clip("c", None, None),
        ],
    )
    assert wrong_landmark_rate(foot) == (1.0, 1)
    live = {"a": "Galata Tower", "b": "Dolmabahçe Palace", "c": "Anything"}
    assert wrong_landmark_rate(foot, live) == (0.5, 2)


def test_wrong_landmark_rate_on_the_synthetic_footage_has_a_denominator():
    """P6b wires the metric: harbor_run and trip carry inferred guesses + ground truth."""
    from .runner import load_footage

    assert wrong_landmark_rate(load_footage("harbor_run")) == (1 / 7, 7)
    assert wrong_landmark_rate(load_footage("trip")) == (1 / 4, 4)
    for footage_id in ("food_day", "sport", "vlog"):
        assert wrong_landmark_rate(load_footage(footage_id)) == (None, 0)


def test_true_landmark_is_never_a_fact_a_planner_could_read():
    """Ground truth lives on the clip record, not in `facts`."""
    from .runner import load_footage

    for footage_id in ("harbor_run", "trip"):
        for clip in load_footage(footage_id).clips:
            if clip.true_landmark:
                assert all(
                    f.value != clip.true_landmark or f.provenance == "inferred" for f in clip.facts
                )
                assert all(f.provenance != "annotation" for f in clip.facts)


def test_reply_requirement_is_judged_on_the_turn_it_was_asked():
    """A receipt requirement reads THAT turn's reply; a later reply cannot launder it."""
    req = Requirement(
        id="receipt",
        kind="order",
        request_type="order_route",
        checker="reply_states",
        params={"all_of": ["reverse"]},
        introduced_in_turn=0,
    )
    fx = _thread_fixture([req], n_turns=2)
    said_it = _results([(_plan("a"), "That is the reverse of your route."), (_plan("a"), "ok")])
    assert score_thread(fx, FOOT, said_it)[0].status == "met"
    said_it_late = _results([(_plan("a"), "ok"), (_plan("a"), "That is the reverse of it.")])
    assert score_thread(fx, FOOT, said_it_late)[0].status == "unmet"
