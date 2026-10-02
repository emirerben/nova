from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from scripts import repair_kri244_creative_brief as repair


def _v1(**changes):  # noqa: ANN003, ANN202
    values = {
        "thread_id": repair.THREAD_ID,
        "version": 1,
        "source_turn_id": repair.SOURCE_TURN_ID,
        "requirements": deepcopy(repair.EXPECTED_V1_REQUIREMENTS),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _v2(**changes):  # noqa: ANN003, ANN202
    values = {
        "thread_id": repair.THREAD_ID,
        "version": 2,
        "source_turn_id": None,
        "requirements": repair._version_two_requirements(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _turn(**changes):  # noqa: ANN003, ANN202
    values = {
        "id": repair.SOURCE_TURN_ID,
        "thread_id": repair.THREAD_ID,
        "status": repair.EXPECTED_TURN_STATUS,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_exact_version_one_is_the_only_accepted_precondition() -> None:
    row = _v1()
    assert repair._assert_initial_state([row]) is row

    with pytest.raises(RuntimeError, match="exactly one"):
        repair._assert_initial_state([])
    with pytest.raises(RuntimeError, match="exactly one"):
        repair._assert_initial_state([row, SimpleNamespace(version=2)])
    with pytest.raises(RuntimeError, match="source-turn identity"):
        repair._assert_initial_state([_v1(source_turn_id=None)])


def test_any_requirement_drift_aborts_without_mutating_version_one() -> None:
    row = _v1()
    original = deepcopy(row.requirements)
    row.requirements[0]["facts"]["end"] = "changed"

    with pytest.raises(RuntimeError, match="requirements changed"):
        repair._assert_initial_state([row])

    assert original == repair.EXPECTED_V1_REQUIREMENTS


def test_exact_completed_source_turn_is_the_only_accepted_turn_state() -> None:
    turn = _turn()
    assert repair._assert_turn_state([turn]) is turn

    with pytest.raises(RuntimeError, match="exactly one creator turn"):
        repair._assert_turn_state([])
    with pytest.raises(RuntimeError, match="exactly one creator turn"):
        repair._assert_turn_state([turn, _turn(id=SimpleNamespace())])
    with pytest.raises(RuntimeError, match="creator-turn identity changed"):
        repair._assert_turn_state([_turn(id=SimpleNamespace())])
    with pytest.raises(RuntimeError, match="expected source turn status completed"):
        repair._assert_turn_state([_turn(status="planning")])


def test_version_two_only_supersedes_the_known_requirement() -> None:
    repaired = repair._version_two_requirements()

    assert repaired[0]["status"] == "superseded"
    assert repair.EXPECTED_V1_REQUIREMENTS[0]["status"] == "open"
    assert {**repaired[0], "status": "open"} == repair.EXPECTED_V1_REQUIREMENTS[0]


def test_apply_requires_exact_thread_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    apply = MagicMock()
    monkeypatch.setattr(repair, "apply", apply)
    monkeypatch.setattr("sys.argv", ["repair_kri244_creative_brief.py", "--apply"])

    with pytest.raises(RuntimeError, match="exact --confirm-thread"):
        repair.main()

    apply.assert_not_called()


def test_main_defaults_to_read_only_inspection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect = MagicMock(return_value={"thread_id": str(repair.THREAD_ID), "versions": [1]})
    apply = MagicMock()
    monkeypatch.setattr(repair, "inspect", inspect)
    monkeypatch.setattr(repair, "apply", apply)
    monkeypatch.setattr("sys.argv", ["repair_kri244_creative_brief.py"])

    repair.main()

    assert json.loads(capsys.readouterr().out) == {
        "dry_run": True,
        "thread_id": str(repair.THREAD_ID),
        "versions": [1],
    }
    inspect.assert_called_once_with()
    apply.assert_not_called()


def test_apply_aborts_before_reading_brief_when_thread_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_db = MagicMock()
    write_db.begin.return_value = nullcontext()
    write_db.get.return_value = None
    rows = MagicMock()
    monkeypatch.setattr(repair, "sync_session", lambda: nullcontext(write_db))
    monkeypatch.setattr(repair, "_rows", rows)
    turns = MagicMock()
    monkeypatch.setattr(repair, "_turns", turns)

    with pytest.raises(RuntimeError, match="target creation thread does not exist"):
        repair.apply()

    write_db.get.assert_called_once_with(
        repair.CreationThread,
        repair.THREAD_ID,
        with_for_update=True,
    )
    rows.assert_not_called()
    turns.assert_not_called()
    write_db.add.assert_not_called()


def test_apply_locks_appends_and_verifies_version_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_one = _v1()
    original_requirements = deepcopy(version_one.requirements)
    version_two = _v2()
    write_db = MagicMock()
    write_db.begin.return_value = nullcontext()
    write_db.get.return_value = SimpleNamespace(id=repair.THREAD_ID)
    session_factory = MagicMock(return_value=nullcontext(write_db))

    def selected_rows(db, *, lock: bool = False):  # noqa: ANN001, ANN202
        assert db is write_db
        assert lock is True
        return [version_one] if write_db.add.call_count == 0 else [version_one, version_two]

    rows = MagicMock(side_effect=selected_rows)
    monkeypatch.setattr(repair, "sync_session", session_factory)
    monkeypatch.setattr(repair, "_rows", rows)
    monkeypatch.setattr(repair, "_turns", lambda db: [_turn()])

    result = repair.apply()

    write_db.get.assert_called_once_with(
        repair.CreationThread,
        repair.THREAD_ID,
        with_for_update=True,
    )
    session_factory.assert_called_once_with()
    rows.assert_has_calls([call(write_db, lock=True), call(write_db, lock=True)])
    write_db.flush.assert_called_once_with()
    added = write_db.add.call_args.args[0]
    assert added.thread_id == repair.THREAD_ID
    assert added.version == 2
    assert added.source_turn_id is None
    assert added.requirements == repair._version_two_requirements()
    assert version_one.requirements == original_requirements
    assert result["versions"] == [1, 2]
    assert result["latest_live_requirements"] == 0


def test_apply_aborts_if_version_two_appears_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_db = MagicMock()
    write_db.begin.return_value = nullcontext()
    write_db.get.return_value = SimpleNamespace(id=repair.THREAD_ID)
    monkeypatch.setattr(repair, "sync_session", lambda: nullcontext(write_db))
    monkeypatch.setattr(repair, "_rows", lambda _db, *, lock=False: [_v1(), _v2()])
    monkeypatch.setattr(repair, "_turns", lambda db: [_turn()])

    with pytest.raises(RuntimeError, match="expected exactly one brief version"):
        repair.apply()

    write_db.add.assert_not_called()


@pytest.mark.parametrize(
    "turns",
    [
        [_turn(status="planning")],
        [_turn(), _turn(id=SimpleNamespace())],
    ],
)
def test_apply_aborts_on_nonterminal_or_additional_creator_turn(
    monkeypatch: pytest.MonkeyPatch,
    turns: list[SimpleNamespace],
) -> None:
    write_db = MagicMock()
    write_db.begin.return_value = nullcontext()
    write_db.get.return_value = SimpleNamespace(id=repair.THREAD_ID)
    rows = MagicMock()
    monkeypatch.setattr(repair, "sync_session", lambda: nullcontext(write_db))
    monkeypatch.setattr(repair, "_turns", lambda db: turns)
    monkeypatch.setattr(repair, "_rows", rows)

    with pytest.raises(RuntimeError, match="creator turn|source turn status"):
        repair.apply()

    rows.assert_not_called()
    write_db.add.assert_not_called()
    write_db.flush.assert_not_called()


def test_apply_detects_in_transaction_verification_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_one = _v1()
    malformed_v2 = _v2(requirements=deepcopy(repair.EXPECTED_V1_REQUIREMENTS))
    write_db = MagicMock()
    write_db.begin.return_value = nullcontext()
    write_db.get.return_value = SimpleNamespace(id=repair.THREAD_ID)
    session_factory = MagicMock(return_value=nullcontext(write_db))
    rows = iter([[version_one], [version_one, malformed_v2]])
    monkeypatch.setattr(repair, "sync_session", session_factory)
    monkeypatch.setattr(repair, "_turns", lambda db: [_turn()])
    monkeypatch.setattr(repair, "_rows", lambda db, *, lock=False: next(rows))

    with pytest.raises(RuntimeError, match="does not exactly supersede"):
        repair.apply()

    session_factory.assert_called_once_with()
    write_db.flush.assert_called_once_with()
