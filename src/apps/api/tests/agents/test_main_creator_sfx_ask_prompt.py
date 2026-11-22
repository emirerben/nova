"""KRI-1561 PR 5: the planner is taught `sfx_ask` only where the manifest advertises it.

Failure modes, written before the tests:

1. The section shows on a manifest with no `sfx_ask` (every other prompt must stay byte-identical).
2. It is missing where the capability is open, so a general ask still gets "only sounds tied to
   named moments are added".
3. The beats sections still tell the model a bare ask adds nothing, with no way out, next to a
   section that says to set `sfx_ask`.
4. The section lets the model invent a time, an effect or a beat, or sets the ask for "no effects".
"""

from __future__ import annotations

from app.agents.main_creator import MainCreatorAgent
from app.services.creator_capabilities import CAPABILITY_SFX_ASK, CapabilityAvailability
from tests.agents.test_main_creator_agent import (
    _input,
    _manifest_with_reaction_beats_available,
)


def _prompt(manifest) -> str:
    agent_input = _input().model_copy(update={"capability_manifest": manifest})
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


def _with_ask(manifest):
    return manifest.model_copy(
        update={
            "capabilities": {
                **manifest.capabilities,
                CAPABILITY_SFX_ASK: CapabilityAvailability(available=True),
            }
        }
    )


def test_the_section_is_absent_without_the_capability() -> None:
    prompt = _prompt(_manifest_with_reaction_beats_available())
    assert "SOUND EFFECTS CHOSEN FOR THE EDIT" not in prompt
    assert '"request_text"' not in prompt


def test_the_section_is_present_and_bounded_where_the_capability_is_open() -> None:
    prompt = _prompt(_with_ask(_manifest_with_reaction_beats_available()))

    assert "SOUND EFFECTS CHOSEN FOR THE EDIT" in prompt
    assert "`sfx_ask`" in prompt and '"request_text"' in prompt
    assert "uygun yerlere ses efekti ekle" in prompt
    # The server chooses: no invented time, effect, beat or licensed effect for this ask.
    assert "never invent a time" in prompt
    assert "no `licensed_sfx` and no reaction beat" in prompt
    # A named moment stays a beat, and "no effects" never sets the ask.
    assert "still a reaction beat" in prompt
    assert "never sets `sfx_ask`" in prompt


def test_the_section_overrides_the_beats_line_and_a_closed_manifest_keeps_it_untouched() -> None:
    opened = " ".join(_prompt(_with_ask(_manifest_with_reaction_beats_available())).split())
    assert "This section overrides any earlier line" in opened
    shut = " ".join(_prompt(_manifest_with_reaction_beats_available()).split())
    # Without the capability the beats wording is exactly what it was: nothing mentions the ask.
    assert "only sounds tied to the moments the creator named are added on iPhone" in shut
    assert "sfx_ask" not in shut


def test_the_capability_alone_teaches_the_ask_without_reaction_beats() -> None:
    """A montage with a recorded voiceover advertises `sfx_ask` and no `reaction_beats`."""
    manifest = _with_ask(_manifest_with_reaction_beats_available())
    manifest = manifest.model_copy(
        update={
            "edit_format": "montage",
            "capabilities": {
                **{k: v for k, v in manifest.capabilities.items() if k != "reaction_beats"},
                "reaction_beats": CapabilityAvailability(
                    available=False, reason_code="phone_talking_only"
                ),
            },
        }
    )
    prompt = _prompt(manifest)
    assert "SOUND EFFECTS CHOSEN FOR THE EDIT" in prompt
    assert "REACTION BEATS" not in prompt
