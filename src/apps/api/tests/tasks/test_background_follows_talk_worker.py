"""Qendresa item 17 through the real worker (Green 3 job): with ``BACKGROUND_FOLLOWS_TALK_ENABLED``
a name typed another way ("LumoTablets" for the spoken "Lumo tablets") is heard on its word, and a
sequence already heard as typed renders the same windows as with the flag off (S64, S65)."""

from __future__ import annotations

from app.config import settings
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)
from tests.tasks.test_speaker_background_on_the_word import (
    ON_THE_WORD_BACKGROUND,
    TABLET,
    _green3_job,
)


def _worker_windows(monkeypatch, background, *, flag: bool):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", flag)
    job = _green3_job(monkeypatch, background)
    gb._run_generative_job(str(job.id))
    layer = _recipe(job).speaker_background
    assert layer is not None and layer.windows
    return [(w.asset_id, round(w.start, 3), round(w.end, 3)) for w in layer.windows]


TYPED_JOINED = {
    **ON_THE_WORD_BACKGROUND,
    "sequence": [
        {**entry, "spoken_phrase": "LumoTablets"}
        if entry["media_id"] == f"asset-{TABLET}"
        else entry
        for entry in ON_THE_WORD_BACKGROUND["sequence"]
    ],
}


def test_green3_renders_the_same_windows_with_the_flag_on(monkeypatch):
    off = _worker_windows(monkeypatch, ON_THE_WORD_BACKGROUND, flag=False)
    on = _worker_windows(monkeypatch, ON_THE_WORD_BACKGROUND, flag=True)
    assert on == off


def test_a_name_typed_joined_is_heard_on_its_word(monkeypatch):
    spoken = _worker_windows(monkeypatch, ON_THE_WORD_BACKGROUND, flag=False)
    tablet = f"visual-{TABLET}"
    assert any(w[0] == tablet for w in spoken)
    typed_off = _worker_windows(monkeypatch, TYPED_JOINED, flag=False)
    assert not any(w[0] == tablet for w in typed_off)  # never heard: the tablet never shows
    typed_on = _worker_windows(monkeypatch, TYPED_JOINED, flag=True)
    assert typed_on == spoken
