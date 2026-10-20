"""KRI-756: the shown/tapped/kept derivation (scripts/suggestion_chip_funnel.py), offline.

``kept`` is derived at query time and nothing emits it, so the derivation itself is the
contract: tapped AND the turn completed AND no undo before the next user_message.
"""

from __future__ import annotations

import json

from scripts import suggestion_chip_funnel as f


def _ev(seq, event_type, payload=None):
    return {"sequence": seq, "event_type": event_type, "payload": payload or {}}


def _tap(kind="captions", rank=2, state="first_prompt", set_id="sg_x", source="template"):
    return {
        "suggestion": {
            "set_id": set_id,
            "chip_id": f"sc_{kind}",
            "kind": kind,
            "rank": rank,
            "state": state,
            "label_source": source,
            "prompt_version": "2026-10-19.1",
            "default_edit": kind == "default_edit",
        }
    }


def _ring(
    set_id="sg_x", state="first_prompt", kinds=("default_edit", "captions"), wording="fallback"
):
    return {
        "set_id": set_id,
        "state": state,
        "wording": wording,
        "chips": [
            {"id": f"sc_{k}", "kind": k, "rank": i + 1, "message": k, "label_source": "template"}
            for i, k in enumerate(kinds)
        ],
    }


def _cell(funnel, state, kind, rank):
    return funnel.cells[(state, kind, rank)]


def test_synthetic_fixture_numbers_are_exactly_the_documented_derivation():
    funnel = f.derive_funnel(f.synthetic_fixture())
    assert funnel.threads == 4
    assert funnel.user_messages == 8 and funnel.chip_taps == 4
    assert funnel.threads_first_message_was_tap == 2
    assert funnel.threads_completed == 4 and funnel.threads_with_follow_up_edit == 2
    assert funnel.orphan_taps == 0
    default = _cell(funnel, "first_prompt", "default_edit", 1)
    assert (default.shown, default.tapped, default.completed, default.kept) == (4, 1, 1, 1)
    captions = _cell(funnel, "first_prompt", "captions", 2)
    # completed, then undone before the next message: tapped but NOT kept
    assert (captions.tapped, captions.completed, captions.undone, captions.kept) == (1, 1, 1, 0)
    sfx = _cell(funnel, "ready", "sfx", 1)
    assert (sfx.tapped, sfx.asked_back, sfx.completed, sfx.kept) == (1, 1, 0, 0)
    music = _cell(funnel, "ready", "music", 2)
    assert (music.tapped, music.failed, music.kept) == (1, 1, 0)
    assert _cell(funnel, "first_prompt", "hook", 3).tapped == 0
    assert funnel.totals().kept == 1


def test_an_undo_after_the_next_user_message_does_not_unkeep():
    thread = f.ThreadRecord(
        "t",
        [_ring()],
        [
            _ev(1, "user_message", _tap()),
            _ev(2, "draft_applied"),
            _ev(3, "user_message"),  # the creator moved on...
            _ev(4, "draft_undone"),  # ...and undid something later
        ],
    )
    cell = _cell(f.derive_funnel([thread]), "first_prompt", "captions", 2)
    assert (cell.completed, cell.undone, cell.kept) == (1, 0, 1)


def test_each_undo_event_type_unkeeps():
    for undo in ("plan_section_undo", "plan_undo_all", "draft_undone"):
        thread = f.ThreadRecord(
            "t",
            [_ring()],
            [_ev(1, "user_message", _tap()), _ev(2, "draft_applied"), _ev(3, undo)],
        )
        cell = _cell(f.derive_funnel([thread]), "first_prompt", "captions", 2)
        assert (cell.completed, cell.undone, cell.kept) == (1, 1, 0), undo


def test_a_failure_in_the_window_beats_a_completion_and_a_question_is_not_kept():
    failed = f.ThreadRecord(
        "t",
        [_ring()],
        [_ev(1, "user_message", _tap()), _ev(2, "draft_applied"), _ev(3, "assistant_error")],
    )
    cell = _cell(f.derive_funnel([failed]), "first_prompt", "captions", 2)
    assert (cell.failed, cell.completed, cell.kept) == (1, 0, 0)
    asked = f.ThreadRecord(
        "t",
        [_ring()],
        [
            _ev(1, "user_message", _tap()),
            _ev(2, "assistant_response", {"turn_value": "question"}),
        ],
    )
    cell = _cell(f.derive_funnel([asked]), "first_prompt", "captions", 2)
    assert (cell.asked_back, cell.completed, cell.kept) == (1, 0, 0)


def test_a_plain_v2_answer_counts_as_completed():
    thread = f.ThreadRecord(
        "t",
        [_ring()],
        [_ev(1, "user_message", _tap()), _ev(2, "assistant_response", {"turn_value": "decision"})],
    )
    assert _cell(f.derive_funnel([thread]), "first_prompt", "captions", 2).kept == 1


def test_a_tap_whose_set_fell_out_of_the_ring_is_counted_as_orphan_not_dropped():
    thread = f.ThreadRecord(
        "t",
        [_ring("sg_new")],
        [_ev(1, "user_message", _tap(set_id="sg_old")), _ev(2, "draft_applied")],
    )
    funnel = f.derive_funnel([thread])
    assert funnel.orphan_taps == 1
    assert _cell(funnel, "first_prompt", "captions", 2).tapped == 1


def test_structlog_shown_lines_correct_the_ring_undercount_without_double_counting():
    ring = _ring("sg_ring")
    lines = [
        {  # already in the ring: must not count twice
            "event": "suggestion_chips.shown",
            "thread_id": "t",
            "set_id": "sg_ring",
            "state": "first_prompt",
            "wording": "fallback",
            "chip_ids": ["sc_default_edit", "sc_captions"],
            "kinds": ["default_edit", "captions"],
        },
        {  # a set the 5-set ring already forgot
            "event": "suggestion_chips.shown",
            "thread_id": "t",
            "set_id": "sg_forgotten",
            "state": "ready",
            "wording": "agent",
            "chip_ids": ["sc_sfx"],
            "kinds": ["sfx"],
        },
    ]
    funnel = f.derive_funnel([f.ThreadRecord("t", [ring], [])], extra_shown=lines)
    assert _cell(funnel, "first_prompt", "default_edit", 1).shown == 1
    assert _cell(funnel, "ready", "sfx", 1).shown == 1


def test_read_shown_log_keeps_only_the_shown_event(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"event": "suggestion_chips.shown", "set_id": "a"}),
                json.dumps({"event": "suggestion_chips.tapped", "set_id": "a"}),
                "not json",
                json.dumps({"event": "other"}),
            ]
        )
    )
    assert [r["set_id"] for r in f.read_shown_log(path)] == ["a"]


def test_fixture_cli_prints_the_baseline_caveat_and_json(capsys):
    assert f.main(["--fixture"]) == 0
    out = capsys.readouterr().out
    assert "NOT a tap rate" in out and "only filled the composer" in out
    assert f.main(["--fixture", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["chip_taps"] == 4 and data["cells"]["first_prompt|default_edit|1"]["kept"] == 1


def test_taps_and_shown_are_split_by_label_source_and_wording():
    ring = _ring("sg_a", wording="agent")
    ring["chips"][1]["label_source"] = "agent"
    thread = f.ThreadRecord(
        "t",
        [ring],
        [
            _ev(1, "user_message", _tap(source="agent", set_id="sg_a")),
            _ev(2, "draft_applied"),
            _ev(3, "user_message", _tap("default_edit", 1, set_id="sg_a")),
            _ev(4, "draft_applied"),
        ],
    )
    funnel = f.derive_funnel([thread])
    agent, template = funnel.by_source["agent"], funnel.by_source["template"]
    assert (agent.shown, agent.tapped, agent.kept) == (1, 1, 1)
    assert (template.shown, template.tapped, template.kept) == (1, 1, 1)
    assert funnel.by_wording["agent"].shown == 2 and funnel.by_wording["agent"].tapped == 2


def test_a_tap_whose_set_left_the_ring_reports_unknown_wording():
    thread = f.ThreadRecord(
        "t",
        [_ring("sg_new")],
        [_ev(1, "user_message", _tap(set_id="sg_old")), _ev(2, "draft_applied")],
    )
    funnel = f.derive_funnel([thread])
    assert funnel.by_wording["unknown"].tapped == 1 and funnel.orphan_taps == 1


def test_chip_one_taps_are_counted_apart_so_the_report_can_discount_media_only_sends():
    taps = [
        _ev(1, "user_message", _tap("default_edit", 1)),
        _ev(2, "draft_applied"),
        _ev(3, "user_message", _tap()),
        _ev(4, "draft_applied"),
    ]
    funnel = f.derive_funnel([f.ThreadRecord("t", [_ring()], taps)])
    assert funnel.chip_one_taps == 1 and funnel.chip_taps == 2
    text = f.render(funnel)
    assert "chip #1 tap bias" in text and "upper bound" in text
    assert "taps on lever chips (rank >= 2, unambiguous): 1" in text
    assert "split by label_source" in text and "split by wording" in text


def test_shown_log_lines_carry_wording_and_positional_one_based_ranks():
    line = {
        "event": "suggestion_chips.shown",
        "thread_id": "t",
        "set_id": "sg_log",
        "state": "ready",
        "wording": "agent",
        "kinds": ["hook", "sfx"],
        "n": 2,
    }
    funnel = f.derive_funnel([f.ThreadRecord("t", [], [])], extra_shown=[line])
    assert funnel.cells[("ready", "hook", 1)].shown == 1
    assert funnel.cells[("ready", "sfx", 2)].shown == 1
    assert funnel.by_wording["agent"].shown == 2
