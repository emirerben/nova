"""Typed, inert intent for an admin-curated sound-effect treatment.

The effect identity is deliberately an opaque server-owned catalog id.  The
renderer/path are never representable here; the trusted boundary resolves the
id again before materializing an audio placement.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class LicensedSfxIntent(BaseModel):
    """A bounded request for the funny-moments licensed SFX treatment."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``effect_id`` is the canonical wire name.  The sound_effect_id alias
    # keeps this contract compatible with the existing craft command.
    effect_id: str = Field(
        min_length=1,
        max_length=160,
        validation_alias=AliasChoices("effect_id", "sound_effect_id"),
    )
    semantics: Literal["funny_moments"] = "funny_moments"
    max_placements: int = Field(default=6, ge=1, le=6)

    @field_validator("effect_id")
    @classmethod
    def _validate_effect_id(cls, value: str) -> str:
        value = value.strip()
        if not value or "://" in value or value.startswith(("/", "gs:", "s3:")):
            raise ValueError("effect_id must be an opaque server-owned catalog id")
        return value

    @field_validator("semantics", mode="before")
    @classmethod
    def _normalize_semantics(cls, value: object) -> str:
        return str(value).strip().casefold()

    @property
    def sound_effect_id(self) -> str:
        """Compatibility accessor used by existing placement code."""

        return self.effect_id


class SfxAskIntent(BaseModel):
    """A general "add sound effects where they fit" ask (KRI-1561).

    No effect is named and no moment is named: the server chooses both from the speech, the
    pauses and the visual moments (`app.services.sfx_chooser.choose_sfx`). A named moment is a
    reaction beat, not this."""

    model_config = ConfigDict(extra="forbid")

    request_text: str = Field(min_length=1, max_length=400)
    max_placements: int = Field(default=6, ge=1, le=6)

    @field_validator("request_text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_text must not be blank")
        return value


# Short names make the contract discoverable to callers without duplicating
# the schema (and are intentionally exported from creator_agent.py too).
CreatorSfxIntent = LicensedSfxIntent
SfxIntent = LicensedSfxIntent

__all__ = ["CreatorSfxIntent", "LicensedSfxIntent", "SfxAskIntent", "SfxIntent"]
