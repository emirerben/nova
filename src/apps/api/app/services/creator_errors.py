"""Typed errors emitted at the authenticated Main Creator policy boundary."""

from __future__ import annotations


class CreatorPolicyError(ValueError):
    """A safe, classified failure while compiling an inert Creator strategy."""

    code = "creator_policy_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        edit_format: str | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.edit_format = edit_format


class CreatorCapabilityError(CreatorPolicyError):
    """The requested renderer/capability is unavailable for this snapshot."""

    code = "edit_format_unavailable"


class CreatorStrategyError(CreatorPolicyError):
    """The strategy cannot be represented by the selected renderer contract."""

    code = "strategy_invalid"


__all__ = ["CreatorCapabilityError", "CreatorPolicyError", "CreatorStrategyError"]
