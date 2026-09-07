from app.services.creator_direction_capabilities import (
    ALL_RENDER_MODES,
    DIRECTION_CAPABILITIES,
    capability_status,
    validate_capability_values,
)


def test_font_and_shadow_are_enforced_in_every_required_mode() -> None:
    for key in ("font_family", "shadow_enabled"):
        assert DIRECTION_CAPABILITIES[key].renderer_modes == ALL_RENDER_MODES
        assert capability_status(key, enforcement="constraint") == "enforced"


def test_prompt_only_and_unknown_keys_are_never_claimed_as_enforced() -> None:
    assert capability_status("tone", enforcement="constraint") == "advisory"
    assert capability_status("future_key", enforcement="constraint") == "unsupported"
    assert capability_status(None, enforcement="advisory") == "advisory"
    assert (
        capability_status("font_family", enforcement="constraint", conflicted=True) == "conflicted"
    )


def test_registry_validates_typed_values() -> None:
    assert validate_capability_values({"font_family": "Playfair Display"})
    assert validate_capability_values({"shadow_enabled": False})
    assert not validate_capability_values({"shadow_enabled": "never"})
    assert not validate_capability_values({"unknown": True})
    assert not validate_capability_values(
        {"font_family": "Playfair Display", "shadow_enabled": False}
    )
    assert not validate_capability_values({"font_family": "<script>"})
    assert not validate_capability_values({"font_family": "Comic Sans"})
    assert not validate_capability_values({"text_color": "red"})
    assert not validate_capability_values({"text_size": 500})
    assert not validate_capability_values({"edit_format_mix": {"montage": 0}})
