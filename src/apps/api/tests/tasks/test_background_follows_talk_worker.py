"""Qendresa item 17 (S66): the planner's `keep_changing` flag reaches the layout through the real
worker. Green 3 ("changing every 3-5 seconds") sets it and lays out exactly as flag off; since
#2118 (S57, no photo twice) that means no rotation after the last mention either, so with or
without it the sequence ends on the last visual talked about (S65). The flag
`BACKGROUND_FOLLOWS_TALK_ENABLED` off = main's windows for both."""

from __future__ import annotations

import pytest

from app.config import settings
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)
from tests.tasks.test_speaker_background_on_the_word import (
    DRONE_PHOTO,
    DRONE_VIDEO,
    ON_THE_WORD_BACKGROUND,
    TAKE_FULL_S,
    WORD_TOLERANCE_S,
    _describe,
    _green3_job,
    _word_source_s,
)


def _worker_windows(monkeypatch, background, *, flag: bool):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", flag)
    job = _green3_job(monkeypatch, background)
    gb._run_generative_job(str(job.id))
    layer = _recipe(job).speaker_background
    assert layer is not None and layer.windows
    return [(w.asset_id, round(w.start, 3), round(w.end, 3)) for w in layer.windows]


KEEP = {**ON_THE_WORD_BACKGROUND, "keep_changing": True}


def test_the_request_carries_keep_changing_only_when_the_planner_set_it():
    plain = gb._strategy_background_request(
        {"creator_strategy": {"background_media": {"media_id": "asset-a"}}}
    )
    assert plain == {"media_id": "asset-a", "source_start_s": 0.0}
    on = gb._strategy_background_request({"creator_strategy": {"background_media": KEEP}})
    assert on["keep_changing"] is True
    off = gb._strategy_background_request(
        {"creator_strategy": {"background_media": ON_THE_WORD_BACKGROUND}}
    )
    assert "keep_changing" not in off
    no_entries = gb._strategy_background_request(
        {"creator_strategy": {"background_media": {"media_id": "asset-a", "keep_changing": True}}}
    )
    assert "keep_changing" not in no_entries
    not_bool = gb._strategy_background_request(
        {
            "creator_strategy": {
                "background_media": {**ON_THE_WORD_BACKGROUND, "keep_changing": "yes"}
            }
        }
    )
    assert "keep_changing" not in not_bool


def test_flag_on_with_keep_changing_matches_flag_off(monkeypatch):
    off = _worker_windows(monkeypatch, KEEP, flag=False)
    on = _worker_windows(monkeypatch, KEEP, flag=True)
    assert on == off


def test_flag_off_ignores_keep_changing_entirely(monkeypatch):
    plain = _worker_windows(monkeypatch, ON_THE_WORD_BACKGROUND, flag=False)
    kept = _worker_windows(monkeypatch, KEEP, flag=False)
    assert plain == kept


def test_flag_on_without_keep_changing_holds_the_last_visual_to_the_end(monkeypatch):
    """Against the Green 3 answer key: the last visual talked about is the drone VIDEO (it plays
    right after the drone photo on the one mention of "drones"); it then stays to the take end."""
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    job = _green3_job(monkeypatch, ON_THE_WORD_BACKGROUND)
    gb._run_generative_job(str(job.id))
    recipe = _recipe(job)
    layer = recipe.speaker_background
    assert layer is not None and layer.windows
    windows = [(w.asset_id.removeprefix("visual-"), w.start, w.end) for w in layer.windows]
    # No speech cut in this worker run: recipe time is the take's own (original) time.
    take_end = TAKE_FULL_S
    assert recipe.duration == pytest.approx(take_end, abs=0.05)
    drones_at = _word_source_s("drones")

    last_media, last_start, last_end = windows[-1]
    assert last_media == DRONE_VIDEO, _describe(windows)
    assert last_end == pytest.approx(take_end, abs=0.05), _describe(windows)
    photo = [w for w in windows if w[0] == DRONE_PHOTO]
    assert photo, _describe(windows)
    assert last_start == pytest.approx(photo[0][2], abs=0.05), _describe(windows)
    # Nothing starts after the "drones" mention except that one last visual (and the photo that
    # plays first on the same mention).
    late = [w for w in windows if w[1] > drones_at + WORD_TOLERANCE_S]
    assert [w[0] for w in late] == [DRONE_VIDEO], _describe(windows)
