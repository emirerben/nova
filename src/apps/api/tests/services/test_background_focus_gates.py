"""Qendresa item 18: the `backgroundFocus` rollout gate family (mirror of item 16's).

Flag `BACKGROUND_FOCUS_ENABLED` AND the base speaker-background gate (NOT the windows gate: a
single full-video background may be aimed too) AND `backgroundFocus` verified AND client >= 39.
Failure modes: docs/reviews/background-focus-failure-modes.md.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.client_protocol import (
    bind_sending_build,
    clear_request_context,
    set_client_protocol,
    unbind_sending_build,
)
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    background_focus_client_supported,
    background_focus_planned_for,
    background_focus_planned_reason,
    background_focus_supported,
    background_focus_unavailable_reason,
    validate_phone_pilot_recipe,
)

_ALL = ["speakerBackground", "backgroundFocus"]


@pytest.fixture(autouse=True)
def _clean_context():
    clear_request_context()
    yield
    clear_request_context()


@pytest.mark.parametrize(
    ("enabled", "flag", "features", "protocol", "supported", "reason"),
    [
        (True, True, _ALL, 39, True, None),
        (True, True, _ALL, 40, True, None),
        # a single full-video background needs no windows token
        (True, True, ["speakerBackground", "backgroundFocus"], 39, True, None),
        (False, True, _ALL, 39, False, "disabled"),
        (True, True, ["backgroundFocus"], 39, False, "token_not_verified"),
        (True, True, _ALL, 31, False, "client_protocol_below_min"),
        (True, False, _ALL, 39, False, "focus_disabled"),
        (True, True, ["speakerBackground"], 39, False, "focus_token_not_verified"),
        (True, True, _ALL, 38, False, "client_protocol_below_min"),
        (True, True, _ALL, None, False, "client_protocol_below_min"),
    ],
)
def test_gate_truth_table(monkeypatch, enabled, flag, features, protocol, supported, reason):
    set_client_protocol(protocol)
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    monkeypatch.setattr(settings, "background_focus_enabled", flag)
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    assert background_focus_supported(require_client=True) is supported
    assert background_focus_unavailable_reason(require_client=True) == reason


def test_client_half_only_applies_inside_a_request(monkeypatch):
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", _ALL)
    # A worker has no app build: the protocol half never applies without a request.
    assert background_focus_client_supported(require_client=False) is True
    assert background_focus_supported(require_client=False) is True
    assert background_focus_unavailable_reason(require_client=False) is None
    set_client_protocol(38)
    assert background_focus_client_supported(require_client=True) is False
    assert background_focus_client_supported() is False
    assert background_focus_client_supported(require_client=False) is True
    assert background_focus_unavailable_reason() == "client_protocol_below_min"
    set_client_protocol(39)
    assert background_focus_client_supported() is True
    assert background_focus_supported() is True


def test_a_worker_acting_for_a_sending_build_does_not_apply_the_request_gate(monkeypatch):
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", _ALL)
    for protocol, planned in ((38, False), (39, True)):
        token = bind_sending_build(protocol)
        try:
            # bound protocol, but not an HTTP request: the 426 half stays out of workers
            assert background_focus_supported() is True
            assert background_focus_planned_for(protocol) is planned
        finally:
            unbind_sending_build(token)


@pytest.mark.parametrize(
    ("protocol", "planned", "reason"),
    [(38, False, "app_too_old"), (39, True, None), (40, True, None), (None, False, "app_too_old")],
)
def test_planned_for_the_approving_build(monkeypatch, protocol, planned, reason):
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", _ALL)
    assert background_focus_planned_for(protocol) is planned
    assert background_focus_planned_reason(protocol) == reason


def test_planned_reasons_for_a_closed_lane(monkeypatch):
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", _ALL)
    monkeypatch.setattr(settings, "background_focus_enabled", False)
    assert background_focus_planned_reason(39) == "disabled"
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["speakerBackground"])
    assert background_focus_planned_reason(39) == "unavailable"
    monkeypatch.setattr(settings, "phone_render_verified_features", ["backgroundFocus"])
    assert background_focus_planned_reason(39) == "unavailable"
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", _ALL)
    assert background_focus_planned_reason(39) == "unavailable"


def test_dark_by_default():
    from app.config import Settings
    from app.services.client_protocol import BACKGROUND_FOCUS_PROTOCOL

    assert Settings.model_fields["background_focus_enabled"].default is False
    assert Settings.model_fields["background_focus_min_client_protocol"].default == 39
    assert BACKGROUND_FOCUS_PROTOCOL == 39
    assert "backgroundFocus" not in Settings.model_fields[
        "phone_render_verified_features"
    ].get_default(call_default_factory=True)


def test_pilot_validation_rejects_a_focus_recipe_without_the_verified_token(monkeypatch):
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding

    recipe = compile_phone_subtitled_plan((_binding(duration_s=10.0),), caption_cues=_CUES)
    stub = SimpleNamespace(
        uses_windows=False, uses_full_screen=False, uses_color=False, uses_focus=True
    )
    focused = recipe.model_copy(update={"speaker_background": stub})
    base = [*recipe.required_capabilities, "speakerBackground"]
    monkeypatch.setattr(settings, "phone_render_verified_features", base)
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(focused)
    assert refused.value.capability == "backgroundFocus"
    monkeypatch.setattr(settings, "phone_render_verified_features", [*base, "backgroundFocus"])
    validate_phone_pilot_recipe(focused)
