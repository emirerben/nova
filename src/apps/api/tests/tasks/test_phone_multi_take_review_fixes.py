"""KRI-720 review fixes (ba0d89eaa..HEAD), each written as a failing case first:

1. the cleanup card says tight on several takes while the tight flag is off (render: long only);
2. a take whose tight pass did not run (noisy, unmeasured, failed) gets neither the long-pause
   pass nor a join trim: worse than before, and the "only the long pauses" line is false;
3. the first take's tail trim is missing from `take_join_trims` (plans snapshotted too late);
4. a named line alone makes the outcome "applied" while the count is 0 and the ms is > 0;
5. the named-line receipt counts only cut takes ("take 2" for the 3rd take in the video), and a
   line also said in an uncut take is invisible to the "said in several takes" check.
"""

from __future__ import annotations

import pytest

from app.pipeline import tight_pause_cut as tpc
from app.pipeline.phone_opening_cut import apply_named_line_cuts_across_takes
from app.pipeline.silence_cut import no_op_plan
from tests.tasks import test_phone_multi_take_named_line as named
from tests.tasks.test_phone_multi_take_tight_pauses import TAKE_GAPS, _run, _setup  # noqa: F401


def _fail_tight_for(monkeypatch, *names):
    real = tpc.apply_tight_pauses

    def fake(plan, path, words, duration, **kwargs):
        if any(path.endswith(f"{n}.wav") for n in names):
            raise OSError("simulated")
        return real(plan, path, words, duration, **kwargs)

    monkeypatch.setattr(tpc, "apply_tight_pauses", fake)


def test_a_later_take_whose_tight_pass_failed_falls_back_to_long_pass_and_join_trim(
    monkeypatch, tmp_path
):
    job, snapshot, _files = _setup(monkeypatch, tmp_path, long=True)
    _fail_tight_for(monkeypatch, "take2")
    variant = _run(job, snapshot)

    (row,) = [r for r in variant["tight_pauses_other_takes"] if r["take"] == 1]
    assert row["status"] == "unavailable"
    # The long pass ran on that take (its own row), and its join edges were trimmed.
    assert [r["take"] for r in variant["long_pauses_other_takes"]] == [1]
    trims = {(r["take"], r["edge"]) for r in variant["take_join_trims"]}
    assert {(1, "start"), (1, "end")} <= trims
    # The takes whose tight pass ran are not touched by the fallback.
    assert variant["tight_pauses"]["status"] == "applied"
    assert all(g["gap_s"] is not None for g in variant["take_join_gaps"])


def test_a_first_take_whose_tight_pass_failed_gets_the_long_pass_even_on_a_tight_ask(
    monkeypatch, tmp_path
):
    from tests.tasks.test_phone_tight_pauses import TIGHT_ASK

    job, snapshot, _files = _setup(monkeypatch, tmp_path, long=True, brief=TIGHT_ASK)
    _fail_tight_for(monkeypatch, "take1")
    variant = _run(job, snapshot)
    assert variant["tight_pauses"]["status"] == "unavailable"
    assert variant["long_pauses"]["mode"] == "long"
    assert (0, "end") in {(r["take"], r["edge"]) for r in variant["take_join_trims"]}


def test_the_first_takes_tail_trim_is_a_join_row(monkeypatch, tmp_path):
    job, snapshot, _files = _setup(monkeypatch, tmp_path)
    variant = _run(job, snapshot)
    rows = {(r["take"], r["edge"]) for r in variant["take_join_trims"]}
    assert (0, "end") in rows and (1, "start") in rows and (1, "end") in rows
    assert (2, "start") in rows


def test_a_named_line_alone_is_not_cleanup(monkeypatch):
    job = named._render(monkeypatch, [named.OKAY])
    variant = job.assembly_plan["variants"][0]
    context = variant["_speech_cleanup_outcome_context"]
    assert variant["silence_cut_outcome"] == "no_change"
    assert context["output_removal_count"] == 0 and context["output_removed_ms"] == 0


# --- 5 ------------------------------------------------------------------------------------


def _take(words):
    return ("m", no_op_plan(6.5), words, 6.5)


def test_named_line_receipts_use_the_real_places_in_the_video():
    # Cut takes are 1st and 3rd of the video (the 2nd is uncut): "okay" is said in the 3rd.
    takes = [_take(named.TAKE_B), _take(named.TAKE_A)]
    _plans, (row,) = apply_named_line_cuts_across_takes(
        takes, [("okay", ["r1"], None, None)], positions=[1, 3]
    )
    assert row["take"]["index"] == 3
    both = [_take(named.TAKE_A), _take(named.TAKE_A)]
    _plans, (many,) = apply_named_line_cuts_across_takes(
        both, [("okay", ["r1"], None, None)], positions=[1, 3]
    )
    assert many["reason"] == "said_in_several_takes" and many["takes"] == [1, 3]


def test_a_line_also_said_in_an_uncut_take_is_refused_not_guessed():
    takes = [_take(named.TAKE_A)]
    plans, (row,) = apply_named_line_cuts_across_takes(
        takes,
        [("okay", ["r1"], None, None)],
        positions=[1],
        witnesses=[(2, named.TAKE_A, 6.5)],
    )
    assert row["reason"] == "said_in_several_takes" and row["takes"] == [1, 2]
    assert plans[0].removed == []
    # A witness that does not say it changes nothing.
    plans, (ok,) = apply_named_line_cuts_across_takes(
        takes,
        [("okay", ["r1"], None, None)],
        positions=[1],
        witnesses=[(2, named.TAKE_B, 6.5)],
    )
    assert ok["status"] == "applied" and ok["take"]["index"] == 1


@pytest.mark.asyncio
async def test_card_is_not_tight_on_several_takes_when_the_tight_flag_is_off(monkeypatch):
    from app.config import settings
    from tests.routes import test_creation_thread_cleanup_requested as card

    monkeypatch.setattr(
        "app.services.phone_rollout.phone_talking_multi_take_cleanup_supported", lambda: True
    )
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False, raising=False)
    result = await card.routes._cleanup_ask_projection(
        card._db(card._snapshot("cut the pauses")),
        card._thread(),
        card._item(paths=card.TWO_TAKES),
    )
    assert result == (True, False)
