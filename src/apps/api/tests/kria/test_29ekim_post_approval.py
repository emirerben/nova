"""29 Ekim, after approval: the approved job renders its words one at a time.

Scorecard checks 7 and 8 (`scripts/kria/score_29_ekim.py`). With the approval gate fixed
(check 6), the approved job failed every render as `creation_text_failed`: the brief's
`text_sequence` seeded no title bar (`unified_montage.brief_view` read only `text` titles), so
the creation text composer had nothing to split and answered "the text must already be
established" / "I couldn't find a title".

Check 7 drives the real phone worker to the composer's input (no model): one bar, the exact
sentence. Check 8 replays every recorded live composer answer (`29-ekim/live_post_approval.json`:
pro and flash fallback, Turkish and English) through the worker, the phone text style pass and
the device pin: the seeded shuffle, one-word pieces of at least 0.25 s that rejoin to the
sentence, no static copy, and r3's receipt met. One recorded answer (an 8.1 s window) was declined
at capture by a microsecond rounding of the 0.25 s stretch; it is kept as that regression's guard.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[5]


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_29_ekim", REPO / "scripts" / "kria" / "score_29_ekim.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_composer_receives_the_title_bar_to_split() -> None:
    card = _scorecard()
    with card._prod_flags():
        result = card.check_composer_input()
    assert result["pass"], result["why_it_fails"]
    assert result["evidence"]["title_source_exists"] is True
    assert result["evidence"]["words"] == 27


@pytest.mark.parametrize("compose_flag", [True, False])
def test_recorded_live_splits_render_to_the_device_pin(monkeypatch, compose_flag) -> None:
    from app.config import settings

    card = _scorecard()
    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", compose_flag)
    with card._prod_flags():
        result = card.check_post_approval_replay()
    assert result["pass"], result["why_it_fails"]
    runs = result["evidence"]["runs"]
    assert {(run["language"], run["path"]) for run in runs} >= {
        ("tr", "pro"),
        ("tr", "flash_fallback"),
        ("en", "pro"),
        ("en", "flash_fallback"),
    }
    assert all(run["min_piece_s"] >= 0.25 - 1e-4 for run in runs)
