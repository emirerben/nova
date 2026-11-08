"""Qendresa case 5: a background recording whose phrase is never heard never shows, and the
finished video's background receipt must say so (not "met").

Ported from the Qendresa kit (`test_q2_unheard_recording.py`). Her real failure was a label the
take never says exactly ("Claude code game studio" vs the spoken "Claude Code Game Studios");
since #2110 the worker hears plurals, so this guard uses a phrase the Cafe T2 take never says
("chocolate fountain"). On the word, an entry whose phrase is not heard gets NO window (S52) and
the only trace was `phone_lane_receipt.dropped` `{"reason": "phrase_not_heard"}`, which nothing
in `app/kria` read: `_check_speaker_background` answered "3 photos or videos take turns behind
you" (met).

Failure modes: `docs/reviews/says-done-receipts-failure-modes.md` (case 5). Runs the real phone
Talking worker on the repo's Cafe T2 harness (no model, no network, no DB), then judges the
finished variant the way the render-ready review does.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, is_judged, plan_facts_from_phone_variant
from app.kria.reply_language import reply_language_for
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    BITE,
    COTOPAXI,
    OBLEA,
    _offline,  # noqa: F401 - autouse fixture the harness needs
)
from tests.tasks.test_speaker_background_strategy_worker import _dropped, _windows, _windows_job

NEVER_SAID = "chocolate fountain"

GREEN_SCREEN = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description=(
        "My screen recordings full screen behind me, me small in the bottom right corner, "
        "show the right one when I talk about each one"
    ),
    facts={"speaker_background": True, "background_changes": True, "speaker_small": True},
)


def _job(monkeypatch):
    return _windows_job(
        monkeypatch,
        [
            {"media_id": f"asset-{COTOPAXI}", "spoken_phrase": "Ecuador"},
            {"media_id": f"asset-{OBLEA}", "spoken_phrase": "Oblea de la Casa"},
            {"media_id": f"asset-{BITE}", "spoken_phrase": NEVER_SAID},
        ],
        speaker_frame="small_bottom_right",
    )


def _receipt(variant, req=GREEN_SCREEN):
    (receipt,) = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


def test_the_third_recording_really_never_shows(monkeypatch):
    # Precondition: the worker drops it with a receipt note only.
    job = _job(monkeypatch)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    shown = {asset for asset, _start, _end in _windows(job)}
    assert f"visual-{BITE}" not in shown
    assert {"lane": "background", "window": 2, "reason": "phrase_not_heard"} in _dropped(job)


def test_a_recording_that_never_shows_is_not_reported_as_met(monkeypatch):
    job = _job(monkeypatch)
    gb._run_generative_job(str(job.id))
    receipt = _receipt(job.assembly_plan["variants"][0])
    assert receipt.status != "met", (
        f"background ask answered {receipt.status!r}: {receipt.reason!r} "
        "although one asked recording never shows (phrase_not_heard)"
    )
    assert is_judged(GREEN_SCREEN, receipt)
    # Named, so she knows which one to fix.
    assert f"“{NEVER_SAID}”" in receipt.reason


def test_unheard_phrase_that_still_rotates_in_is_met(monkeypatch):
    """5.1: an unheard phrase is not "never shown". With one entry unphrased the layout falls
    back to a rotation and both recordings play, so nothing is missing."""
    job = _windows_job(
        monkeypatch,
        [
            {"media_id": f"asset-{COTOPAXI}", "spoken_phrase": "unicorn parade"},
            {"media_id": f"asset-{OBLEA}"},
        ],
        cadence_s=6.0,
    )
    gb._run_generative_job(str(job.id))
    assert {"lane": "background", "window": 0, "reason": "phrase_not_heard"} in _dropped(job)
    req = BriefRequirement(
        id="r1",
        kind="style",
        scope="global",
        description="my photos behind me, changing",
        facts={"speaker_background": True, "background_changes": True},
    )
    receipt = _receipt(job.assembly_plan["variants"][0], req)
    # Nothing is missing (no "never shown" sentence). Two photos over the talk cannot change every
    # 6 s without a repeat, so the reply says that (Çiğdem S2) and the ask is partial for it.
    assert "unicorn parade" not in receipt.reason, receipt.reason
    assert "you asked for a change about every 6 s" in receipt.reason, receipt.reason
    assert receipt.status == "partial", receipt.reason


def _variant(background: dict) -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "edit_format": "subtitled",
        "phone_lane_receipt": {
            "applied": ["background"],
            "dropped": [{"lane": "background", "window": 2, "reason": "phrase_not_heard"}],
            "background": background,
        },
    }


def test_receipt_without_unshown_key_is_unchanged():
    """5.5: a render recorded before the worker wrote `unshown` claims nothing new."""
    receipt = _receipt(_variant({"windows": 2, "media": 2, "speaker_frame": "small_bottom_right"}))
    assert receipt.status == "met"


def test_an_unnamed_missing_recording_is_counted():
    receipt = _receipt(
        _variant(
            {
                "windows": 2,
                "media": 2,
                "speaker_frame": "small_bottom_right",
                "unshown": [{"window": 2, "reason": "not_in_ready_pool"}],
            }
        )
    )
    assert receipt.status == "partial"
    assert "1 of your recordings never shows" in receipt.reason


def test_unshown_entries_compare_photos_not_positions():
    """5.2: an unheard entry whose photo another (heard) entry shows is not missing; with no
    windows at all the fill is the background, so its entry shows."""
    window = SimpleNamespace(media_id="photo-a")
    entries = [
        {"window": 0, "phrase": "first", "media_id": "photo-a"},
        {"window": 1, "phrase": NEVER_SAID, "media_id": "photo-a"},
        {"window": 2, "phrase": "gone", "media_id": None},
    ]
    drops = [
        {"lane": "background", "window": 1, "reason": "phrase_not_heard"},
        {"lane": "background", "window": 2, "reason": "not_in_ready_pool"},
        {"lane": "background", "part": "full_screen_opening", "reason": "take_too_short"},
    ]
    background = SimpleNamespace(windows=(window,), media_id="photo-a")
    assert gb._unshown_background_entries(entries, background, drops) == [
        {"window": 2, "phrase": "gone", "reason": "not_in_ready_pool"}
    ]
    fill_only = SimpleNamespace(windows=(), media_id="photo-b")
    assert (
        gb._unshown_background_entries(
            [{"window": 0, "phrase": NEVER_SAID, "media_id": "photo-b"}], fill_only, drops
        )
        == []
    )


def test_turkish_reason_names_the_recording():
    with reply_language_for("tr"):
        receipt = _receipt(
            _variant(
                {
                    "windows": 2,
                    "media": 2,
                    "speaker_frame": "small_bottom_right",
                    "unshown": [{"window": 2, "phrase": NEVER_SAID, "reason": "phrase_not_heard"}],
                }
            )
        )
    assert receipt.status == "partial"
    assert f"“{NEVER_SAID}”" in receipt.reason
    assert "duymadım" in receipt.reason
