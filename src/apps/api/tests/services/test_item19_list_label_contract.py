"""Qendresa item 19: the approved render contract must not pin a list label.

On a phone Talking edit with `SPOKEN_LIST_LABELS_ENABLED`, a whole-video text that carries a list
marker ("1. Spec-kit"), or that IS the name of a planned background recording, is drawn by the
worker only when its item's part of the talk starts. An item never heard is drawn nowhere and its
own receipt says so, so pinning it in `exact_texts` would refuse an honest render (the same trap
KRI-707 closed for spoken-word texts). The title, the closing text and plain global texts stay
pinned exactly as before; a non-Talking edit still pins the label; flag off is unchanged.
"""

from __future__ import annotations

import pytest

import app.services.phone_rollout as rollout
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.creator_render_contract import build_render_contract

TITLE = "5 PROJECTS / the AI builder community"
MARKED = "1. Spec-kit"
NAMED = "Impeccable"  # no marker; the name of a planned recording
CLOSING = "comment REPO"
PLAIN = "Follow for more"


def _req(rid: str, text: str, scope: str = "global") -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind="text", scope=scope, description=f"Text {text}", literal=text, facts={}
    )


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            _req("l1", MARKED),
            _req("l2", NAMED),
            _req("t1", TITLE, scope="title"),
            _req("c1", CLOSING),
            _req("p1", PLAIN),
        ],
    )


def _strategy(edit_format: str = "subtitled") -> dict:
    return {
        "edit_format": edit_format,
        "audio_strategy": "original_audio",
        "render_program": "native",
        "opening_title": TITLE,
        "target_duration_s": 19,
        "selected_media_ids": ["c0"],
        "background_media": {
            "media_id": "asset-a",
            "sequence": [
                {"media_id": "asset-a", "spoken_phrase": "spec kit"},
                {"media_id": "asset-b", "spoken_phrase": "impeccable"},
            ],
        },
    }


def _pinned(
    monkeypatch, *, flag: bool, edit_format: str = "subtitled", follows_talk: bool = True
) -> list[str]:
    monkeypatch.setattr(rollout, "phone_list_labels_supported", lambda: flag)
    # A label follows a recording only with item 17's windows (the worker's rule).
    monkeypatch.setitem(settings.__dict__, "background_follows_talk_enabled", follows_talk)
    contract = build_render_contract(_strategy(edit_format), generation_id="g1", brief=_brief())
    assert contract is not None
    return [t.text for t in contract.exact_texts]


def test_flag_on_talking_does_not_pin_a_marked_or_recording_named_label(monkeypatch):
    pinned = _pinned(monkeypatch, flag=True)
    assert MARKED not in pinned and NAMED not in pinned
    assert CLOSING in pinned and PLAIN in pinned and TITLE in pinned


def test_flag_on_talking_head_edit_format_is_covered_too(monkeypatch):
    pinned = _pinned(monkeypatch, flag=True, edit_format="talking_head")
    assert MARKED not in pinned and NAMED not in pinned


def test_flag_on_other_edit_still_pins_the_label(monkeypatch):
    pinned = _pinned(monkeypatch, flag=True, edit_format="montage")
    assert MARKED in pinned and NAMED in pinned


@pytest.mark.parametrize("edit_format", ["subtitled", "montage"])
def test_flag_off_pins_everything_as_before(monkeypatch, edit_format):
    pinned = _pinned(monkeypatch, flag=False, edit_format=edit_format)
    assert MARKED in pinned and NAMED in pinned
    assert CLOSING in pinned and PLAIN in pinned and TITLE in pinned


def test_flag_off_contract_is_identical_to_the_pre_item19_pin(monkeypatch):
    off = _pinned(monkeypatch, flag=False)
    on = _pinned(monkeypatch, flag=True)
    assert [t for t in off if t not in {MARKED, NAMED}] == on


def test_without_item_17_only_a_marked_label_is_left_unpinned(monkeypatch):
    """The worker follows a recording only with item 17's windows; without them an unmarked
    recording name is never drawn as a label, so the contract keeps pinning it."""
    pinned = _pinned(monkeypatch, flag=True, follows_talk=False)
    assert MARKED not in pinned
    assert NAMED in pinned
