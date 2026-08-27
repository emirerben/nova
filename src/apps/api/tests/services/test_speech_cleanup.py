from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.speech_cleanup import (
    capability_for_item,
    cleanup_inputs,
    contract_for_item,
    main_footage_identity,
    reconcile_consent,
    reconcile_item_policy_change,
    renderer_enabled_for_item,
)


def _item(**overrides):
    values = {
        "clip_assignments": [{"media_id": "m1", "gcs_path": "users/u/a.mp4"}],
        "clip_gcs_paths": ["users/u/a.mp4"],
        "edit_format": "subtitled",
        "voiceover_gcs_path": None,
        "audio_mode": "kria",
        "speech_cleanup_enabled": False,
        "speech_cleanup_notice": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_capability_is_pure_and_closed_for_unsupported_items():
    item = _item(edit_format="montage")
    result = capability_for_item(item, mode="opt_in", engine_enabled=True)
    assert result.available is False
    assert result.reason == "unsupported_format"


def test_required_contract_requires_engine_and_consent():
    item = _item(speech_cleanup_enabled=True)
    assert contract_for_item(item, mode="opt_in", engine_enabled=True) == "required_v1"
    item.speech_cleanup_enabled = False
    assert contract_for_item(item, mode="opt_in", engine_enabled=True) == "off_v1"


def test_live_config_defaults_to_opt_in_and_rejects_legacy_auto():
    required = {
        "storage_bucket": "test-bucket",
        "database_url": "postgresql://localhost/nova_test",
    }

    assert Settings(**required).speech_cleanup_mode == "opt_in"
    with pytest.raises(ValidationError):
        Settings(**required, speech_cleanup_mode="legacy_auto")


def test_contract_mapper_rejects_legacy_auto_as_a_live_mode():
    with pytest.raises(ValueError, match="unsupported_speech_cleanup_mode:legacy_auto"):
        contract_for_item(  # type: ignore[arg-type] - runtime boundary regression
            _item(), mode="legacy_auto", engine_enabled=True
        )


def test_reconcile_only_revokes_on_ordered_identity_change():
    item = _item(speech_cleanup_enabled=True)
    previous = main_footage_identity(item)
    item.clip_assignments[0]["user_note"] = "metadata only"
    assert reconcile_consent(item, previous) is False
    item.clip_assignments = [
        {"media_id": "m2", "gcs_path": "users/u/b.mp4"},
    ]
    assert reconcile_consent(item, previous) is True
    assert item.speech_cleanup_enabled is False
    assert item.speech_cleanup_notice["reason"] == "main_footage_changed"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("edit_format", "montage", "unsupported_format"),
        ("audio_mode", "voiceover", "replacement_voiceover"),
        ("voiceover_gcs_path", "users/u/voiceover.wav", "replacement_voiceover"),
    ],
)
def test_content_changes_revoke_opt_in_with_actionable_notice(field, value, reason):
    item = _item(speech_cleanup_enabled=True)
    previous = cleanup_inputs(item)
    setattr(item, field, value)

    assert reconcile_item_policy_change(item, previous) is True
    assert item.speech_cleanup_enabled is False
    assert item.speech_cleanup_notice["reason"] == reason


def test_operational_outage_preserves_explicit_on_preference():
    item = _item(speech_cleanup_enabled=True)
    previous = cleanup_inputs(item)

    assert reconcile_item_policy_change(item, previous) is False
    assert item.speech_cleanup_enabled is True
    with pytest.raises(ValueError, match="speech_cleanup_unavailable:rollout_disabled"):
        contract_for_item(item, mode="disabled", engine_enabled=True)


def test_disabled_rollout_still_allows_explicit_off_contract():
    item = _item(speech_cleanup_enabled=False)
    assert contract_for_item(item, mode="disabled", engine_enabled=True) == "off_v1"


def test_renderer_gate_prevents_advertising_a_format_that_would_fallback():
    item = _item(edit_format="subtitled")
    assert (
        renderer_enabled_for_item(
            item,
            subtitled_enabled=False,
            talking_head_enabled=True,
            narrated_self_narration_enabled=True,
        )
        is False
    )
    capability = capability_for_item(
        item,
        mode="opt_in",
        engine_enabled=True,
        renderer_enabled=False,
    )
    assert capability.reason == "renderer_disabled"
