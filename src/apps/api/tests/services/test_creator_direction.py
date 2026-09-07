from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.creator_direction import (
    MAX_TOTAL_INSTRUCTION_CHARS,
    CreatorDirectionResolver,
    CreatorDirectionSnapshot,
    infer_typed_direction,
    normalize_instruction,
    normalize_key,
)


def test_never_shadow_becomes_enforced_renderer_policy():
    key, value, enforcement = infer_typed_direction(
        "Never add shadows to my videos", None, None, "default"
    )
    assert (key, value, enforcement) == (
        "shadow_enabled",
        {"shadow_enabled": False},
        "constraint",
    )


def test_direction_normalization_removes_all_unicode_format_controls():
    assert normalize_instruction("Never\ufeff add\u200b shadows\u2066") == "Never add shadows"
    assert normalize_key("font\u2066 family") == "font_family"


def test_always_use_font_becomes_enforced_renderer_policy():
    key, value, enforcement = infer_typed_direction(
        "Always use the Inter font", None, None, "default"
    )

    assert (key, value, enforcement) == (
        "font_family",
        {"font_family": "Inter"},
        "constraint",
    )


def test_explicit_reserved_profile_key_is_not_reinterpreted():
    key, value, enforcement = infer_typed_direction(
        "Always make founder stories",
        "creator_profile_summary",
        None,
        "constraint",
    )

    assert (key, value, enforcement) == (
        "creator_profile_summary",
        None,
        "constraint",
    )


def test_existing_font_key_retypes_an_edited_instruction():
    key, value, enforcement = infer_typed_direction(
        "Use Inter",
        "font_family",
        None,
        "constraint",
    )

    assert (key, value, enforcement) == (
        "font_family",
        {"font_family": "Inter"},
        "constraint",
    )


def test_unbundled_font_stays_prompt_only_instead_of_claiming_enforcement():
    key, value, enforcement = infer_typed_direction(
        "Always use Comic Sans font", None, None, "default"
    )

    assert (key, value, enforcement) == (None, None, "default")


def test_direction_prompt_budget_matches_agent_contract():
    assert MAX_TOTAL_INSTRUCTION_CHARS == 4_000
    snapshot = CreatorDirectionSnapshot(
        enabled=False,
        revision=1,
        items=({"instruction": "Never use shadows"},),
    )

    assert snapshot.prompt_block == ""


def test_project_override_replaces_account_prompt_and_typed_value() -> None:
    snapshot = CreatorDirectionSnapshot(
        enabled=True,
        revision=2,
        items=(
            {
                "instruction": "Always use Inter font",
                "normalized_key": "font_family",
                "structured_value": {"font_family": "Inter"},
            },
            {
                "instruction": "Keep the tone warm",
                "normalized_key": "tone",
                "structured_value": {"tone": "warm"},
            },
        ),
        overrides=(
            {
                "instruction": "Use a lighter serif for this launch",
                "normalized_key": "font_family",
                "structured_value": None,
            },
        ),
    )

    assert "Always use Inter font" not in snapshot.prompt_block
    assert "Use a lighter serif for this launch (this project only)" in snapshot.prompt_block
    assert snapshot.typed_overrides == {"tone": "warm"}


async def test_resolver_projects_persona_style_below_active_memory():
    user_id = "user-1"
    persona = SimpleNamespace(style={"style_set_id": "default", "knobs": {"font_family": "Inter"}})
    active = SimpleNamespace(
        id="memory-1",
        category="video_style",
        normalized_key="font_family",
        instruction="Always use Playfair Display",
        enforcement="constraint",
        structured_value={"font_family": "Playfair Display"},
        source_kind="creation_thread",
        source_thread_id=None,
        state="active",
        user_locked=True,
        confidence=1.0,
        updated_at=None,
    )

    def result(rows):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: rows,
        )

    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                creator_memory_enabled=True,
                creator_memory_revision=3,
            )
        ),
        execute=AsyncMock(side_effect=[result([active]), result(persona)]),
    )

    snapshot = await CreatorDirectionResolver().snapshot(db, user_id)

    assert snapshot.compatibility_input_version == "persona-style-v1"
    assert snapshot.typed_overrides["font_family"] == "Playfair Display"
    assert snapshot.compatibility_items[0]["structured_value"] == {"font_family": "Inter"}
    assert "Existing style preference: font_family=Inter" not in snapshot.prompt_block
    assert "Always use Playfair Display" in snapshot.prompt_block
