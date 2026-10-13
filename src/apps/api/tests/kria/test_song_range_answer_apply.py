"""KRI-588: answering "which part of the song did you film?" stores the range, or refuses."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.kria import runtime
from app.kria.runtime import RuntimeFailure
from app.models import PlanItem
from app.schemas.user_song import SongRangeAnswerIn

ITEM_ID = uuid.uuid4()


def _thread() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        revision=7,
        active_plan_item_id=ITEM_ID,
        state={"media": [{"role": "song", "media_id": "s"}]},
    )


def _item() -> SimpleNamespace:
    return SimpleNamespace(
        song_gcs_path="songs/x.mp3", song_range_start_s=None, song_range_end_s=None
    )


def _db(events: list[tuple[str, dict]], item: SimpleNamespace | None) -> Mock:
    db = Mock()
    db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: events))

    async def _get(model, _id, **_kw):  # noqa: ANN001, ANN202
        assert model is PlanItem
        return item

    db.get = _get
    return db


def _asked(duration: float | None = 200.0) -> tuple[str, dict]:
    return (
        "assistant",
        {
            "song_range_question": {
                "question_id": "q1",
                "song_generation": 2,
                "song_duration_s": duration,
            }
        },
    )


async def test_a_valid_answer_is_stored_on_the_item_and_the_thread_song_entry() -> None:
    thread, item = _thread(), _item()
    answer = SongRangeAnswerIn(question_id="q1", start_s=40.0, end_s=80.0)
    await runtime._apply_song_range_answer(_db([_asked()], item), thread, answer)
    assert (item.song_range_start_s, item.song_range_end_s) == (40.0, 80.0)
    assert thread.state["media"][0]["song_range"] == {"start_s": 40.0, "end_s": 80.0}


async def test_the_whole_song_answer_clears_any_earlier_range() -> None:
    thread, item = _thread(), _item()
    item.song_range_start_s, item.song_range_end_s = 10.0, 50.0
    thread.state["media"][0]["song_range"] = {"start_s": 10.0, "end_s": 50.0}
    await runtime._apply_song_range_answer(
        _db([_asked()], item), thread, SongRangeAnswerIn(question_id="q1")
    )
    assert (item.song_range_start_s, item.song_range_end_s) == (None, None)
    assert "song_range" not in thread.state["media"][0]


async def test_an_answer_to_a_question_that_is_not_open_is_a_409_and_changes_nothing() -> None:
    thread, item = _thread(), _item()
    answer = SongRangeAnswerIn(question_id="old", start_s=40.0, end_s=80.0)
    with pytest.raises(RuntimeFailure) as err:
        await runtime._apply_song_range_answer(_db([_asked()], item), thread, answer)
    assert err.value.status_code == 409 and err.value.code == "song_range_stale"
    assert item.song_range_start_s is None


async def test_a_too_short_range_is_a_422_and_changes_nothing() -> None:
    thread, item = _thread(), _item()
    answer = SongRangeAnswerIn(question_id="q1", start_s=40.0, end_s=43.0)
    with pytest.raises(RuntimeFailure) as err:
        await runtime._apply_song_range_answer(_db([_asked()], item), thread, answer)
    assert err.value.status_code == 422 and err.value.code == "song_range_invalid"
    assert item.song_range_start_s is None


async def test_an_end_past_the_song_is_clamped_to_the_song() -> None:
    thread, item = _thread(), _item()
    answer = SongRangeAnswerIn(question_id="q1", start_s=150.0, end_s=999.0)
    await runtime._apply_song_range_answer(_db([_asked(200.0)], item), thread, answer)
    assert (item.song_range_start_s, item.song_range_end_s) == (150.0, 200.0)
