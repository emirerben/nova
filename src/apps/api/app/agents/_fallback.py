"""A behavioural fallback: the agent answered, and part of the answer was not used.

An agent run can end ``ok`` while its parse boundary quietly kept less than the model said (an
editorial plan that cannot run is dropped, a track id the library does not hold is skipped).
The outcome says nothing about it, so the run counted as a clean success. An output model that
carries ``fallbacks`` makes that visible: the runtime stores the kinds on the ``agent_run`` row
(``fallback_kind``) and the fleet report shows the rate per agent (KRI-1429).

This is not the provider-model fallback (``ok_fallback``: the model chain advanced) and it is
not a failure (``outcome`` outside ``SUCCESS_OUTCOMES``). The kinds are a closed vocabulary so a
rate can be read per kind; add one when a site is converted.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FallbackKind = Literal[
    # Main Creator's editorial plan cannot run on this edit and was dropped at parse.
    "editorial_plan_dropped",
    # The music matcher's answer held entries the library or the schema refused.
    "matcher_entries_dropped",
]


class FallbackOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FallbackKind
    # Counts and typed reason codes only: never creator wording, titles or transcripts.
    detail: str = Field(default="", max_length=120)


def fallback_kind_of(output: Any) -> str | None:
    """The kinds an agent output reports, sorted and comma-joined for the ``agent_run`` row."""
    rows: Iterable[Any] = getattr(output, "fallbacks", None) or ()
    kinds = sorted({row.kind for row in rows if isinstance(row, FallbackOutcome)})
    return ",".join(kinds) if kinds else None
