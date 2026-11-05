"""Item 20: the title-anchor rule is taught only while TITLE_LOOK_ENABLED is on."""

from __future__ import annotations

import pytest

from app.agents import main_creator
from app.agents._schemas.creator_agent import ResolvedCreatorManifest


def _manifest() -> ResolvedCreatorManifest:
    return ResolvedCreatorManifest(
        item_id="00000000-0000-0000-0000-000000000001",
        edit_format="subtitled",
        render_program="guided",
        media=[],
        capabilities={},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def test_prompt_version_is_bumped() -> None:
    assert main_creator.MAIN_CREATOR_PROMPT_VERSION == "2026-11-05-v129"


def test_flag_off_section_is_the_old_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_creator, "_title_look_on", lambda: False)
    section = main_creator._reaction_beats_section(_manifest())
    assert section == main_creator._REACTION_BEATS_PROMPT_SECTION
    assert '"anchor"' not in section


def test_flag_on_teaches_the_title_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_creator, "_title_look_on", lambda: True)
    section = main_creator._reaction_beats_section(_manifest())
    assert section.startswith(main_creator._REACTION_BEATS_PROMPT_SECTION)
    assert '"anchor": "title"' in section
    assert "opening_title" in section.split('"anchor": "title"', 1)[1]
