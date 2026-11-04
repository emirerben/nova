"""A suggestion pin joins the request digest only under KRIA_SUGGESTION_PINNED_TAPS_ENABLED.

Failure modes: flag off drifting from KRI-756 (every stored client_event_id digest would stop
matching its replay), a pin-less body moving under the flag, and two different pins collapsing to
one request (a replay of the same client_event_id would silently run the wrong card).
The expected digests were captured from the tree before the pin existed.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.api_schemas import SubmitTurnBody
from app.kria.runtime import request_digest

_PLAIN = "1e3e02312a4b2c010d66f7821b05a383dbd0e6b2a5a3c818b2ca1e92bb121a26"
_SCOPED = "d440a1d02e8aef05d8498962a9eec55a0a1e96595aec9ce693ed96c0c283fc3c"
_PINNED_BEFORE = "e65c9d88c8dd79b65734ad58ddc570d29b41e941c17125d80d4d61b41e718454"


def _body(**extra):
    return SubmitTurnBody(
        message=extra.pop("message", "Suggest an edit."),
        client_event_id=extra.pop("client_event_id", "e1"),
        expected_thread_revision=1,
        **extra,
    )


def _pinned(set_id="sg_a", chip_id="sc_b"):
    return _body(
        message="Add captions",
        client_event_id="e2",
        suggestion={"set_id": set_id, "chip_id": chip_id},
    )


def test_flag_off_leaves_every_digest_as_kria756_had_it(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    assert request_digest(_body()) == _PLAIN
    assert request_digest(_body(message="hi", client_event_id="e3", scope=["title"])) == _SCOPED
    assert request_digest(_pinned()) == _PINNED_BEFORE
    assert request_digest(_pinned("sg_z")) == _PINNED_BEFORE, "off: the pin is telemetry only"


def test_flag_on_leaves_pin_less_digests_alone(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    assert request_digest(_body()) == _PLAIN
    assert request_digest(_body(message="hi", client_event_id="e3", scope=["title"])) == _SCOPED


@pytest.mark.parametrize(
    "other",
    [_pinned("sg_z"), _pinned(chip_id="sc_z")],
    ids=["other_set", "other_chip"],
)
def test_flag_on_makes_a_different_pin_a_different_request(monkeypatch, other):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    assert request_digest(_pinned()) != request_digest(other)
    assert request_digest(_pinned()) != _PINNED_BEFORE
    assert request_digest(_pinned()) == request_digest(_pinned()), "a retry stays idempotent"
