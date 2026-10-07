"""Record schema for the request-following incident corpus (KRI-470 / KRI-480).

One JSON file per incident under ``tests/fixtures/incidents/``. A record is a
redacted, reusable regression fixture: what the creator approved, what the media
looked like, what we expect, what actually happened, and the command a human runs
to reproduce or verify it. Explicit over clever: every field is optional only
when a record genuinely has nothing to say about it.

What a record can honestly prove in pytest is decided by the runner, not here --
see ``test_incident_corpus.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kria.brief import CreativeBrief

# ``contract_pin`` is NOT incident replay: it pins what the contract builder does for a
# plan shaped like a past incident, so a later change to it is noticed.
Kind = Literal["output", "clarification", "routing", "contract_pin"]
# One dimension per kind of claim the pytest corpus can check. A record's xfail must name
# a scope it actually asserts (validated below), so a scope can never be silently ignored.
Scope = Literal["contract", "refusal", "question", "output", "route"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncidentRef(_Strict):
    ticket: str = Field(description="Linear id, e.g. KRI-469")
    summary: str = Field(description="One line: what went wrong")
    url: str | None = None


class SpeechFact(_Strict):
    has_speech: bool
    to_camera: bool | None = None


class MediaFact(_Strict):
    """Redacted media identity: an opaque id plus the facts planning used."""

    id: str
    kind: Literal["video", "image"] = "video"
    duration_s: float = Field(gt=0)
    capture_time: datetime | None = None
    has_audio: bool | None = None
    speech: SpeechFact | None = None


class SongFact(_Strict):
    duration_s: float = Field(gt=0)
    mode: str
    window_start_s: float | None = None
    window_end_s: float | None = None


class Turn(_Strict):
    """One conversation turn for clarification records."""

    role: Literal["user", "assistant"]
    text: str = ""
    choice_question: dict[str, Any] | None = None
    choice_selection: dict[str, Any] | None = None


class ClipGroup(_Strict):
    name: str
    media_ids: list[str]


class SyntheticClip(_Strict):
    """Deterministic ffmpeg stand-in for real footage (generated, never committed)."""

    id: str
    role: Literal["picture", "voice", "song"]
    duration_s: float = Field(gt=0, le=30)
    color: str | None = Field(default=None, description="lavfi colour for picture clips")
    tone_hz: int | None = Field(default=None, description="sine tone; absent means silent")


class PhoneSection(_Strict):
    kind: Literal["speech", "montage"]
    media_id: str | None = None
    source_start_s: float = 0.0
    source_end_s: float = 0.0
    visual: Literal["speaker", "cutaways"] = "speaker"
    duration_s: float = 0.0
    cut_s: float | None = None


class PhoneRecipeSpec(_Strict):
    """Build a real EditRecipeV2 with the real speech-montage compiler."""

    builder: Literal["speech_montage"]
    sections: list[PhoneSection]


class GuidedPlanSpec(_Strict):
    order: list[str] = Field(description="Media ids in the order the plan's timeline shows them")
    opening_title: str | None = None
    closing_title: str | None = None


class Inputs(_Strict):
    media: list[MediaFact] = []
    voiceover_id: str | None = None
    song: SongFact | None = None
    turns: list[Turn] = []
    clip_groups: list[ClipGroup] = []
    synthetic: list[SyntheticClip] = []
    phone_recipe: PhoneRecipeSpec | None = None
    cloud_preflight: bool = False
    # KRI-470 / PR-E: the cloud adapter that would render the plan (preflight consults its
    # declaration) and the receipt its renderer reported (the publication verifier's input).
    # The media are iPhone analysis proxies on a phone-enrolled account (the unified phone
    # montage renders it): the clarification gate then knows the draft-time text receipts
    # are deferred to render time (``title_text``).
    phone_proxy_media: bool = False
    cloud_adapter: Literal["cloud_guided_story", "cloud_classic", "cloud_slides"] | None = None
    cloud_receipt: dict[str, Any] | None = None
    # The sibling ``cloud_evidence`` the renderer reported (hand-built records only).
    cloud_evidence: dict[str, Any] | None = None
    # A guided plan compiled by the REAL compiler from this media order; the pre-render
    # gate runs on it and (unless ``cloud_evidence`` is given) the evidence is derived
    # from its timeline by the real evidence builder.
    guided_plan: GuidedPlanSpec | None = None
    notes: list[str] = []


class Approved(_Strict):
    """What the creator approved, as persisted (redacted)."""

    strategy: dict[str, Any] | None = None
    brief: CreativeBrief | None = None
    creator_request: str = ""
    provenance: str = ""


class TextExpect(_Strict):
    role: Literal["opening", "closing", "any", "clip"]
    text: str


class ContractExpect(_Strict):
    """Facts the built contract must carry. Only fields present are asserted."""

    duration_s: float | None = None
    audio_source_ids: list[str] = []
    original_audio: Literal["forbid", "require"] | None = None
    require_voiceover: bool = False
    order_required: bool = False
    order_basis: str | None = None
    order_ids: list[str] | Literal["capture_time"] = []
    exact_texts: list[TextExpect] = []
    resolved: bool = False
    unresolved_nonempty: bool = False


class RefusalExpect(_Strict):
    """A verifier (cloud preflight / phone pin) must decline.

    The decline itself is always asserted. ``reason`` / ``field_path`` are the TYPED
    decline (KRI-476 / PR-A): when set, the exception MUST expose ``decline_reason`` /
    ``field_path`` and match. ``message_advisory`` documents today's creator-facing copy
    and is never asserted.
    """

    reason: str | None = Field(default=None, description="DeclineReason")
    field_path: str | None = None
    message_advisory: str | None = None


class CloudExpect(_Strict):
    """KRI-470 / PR-E: the real cloud preflight + publication verifier, on the recorded receipt.

    ``preflight`` is whether the named adapter lets the plan through before any work;
    ``plan_gate`` is the pre-render verdict on ``inputs.guided_plan``; ``publication`` is the
    verdict on the recorded/derived evidence (``None`` = not asserted).  A declining
    verdict may pin the typed ``reason`` / ``field_path`` (the gate's decline is checked
    against them when no publication verdict is asserted).
    """

    preflight: Literal["passes", "declines"]
    # The pre-render guided plan gate (needs ``inputs.guided_plan``): declined plans never render.
    plan_gate: Literal["passes", "declines"] | None = None
    publication: Literal["accepts", "declines"] | None = None
    reason: str | None = None
    field_path: str | None = None
    gate_reason: str | None = None
    gate_field_path: str | None = None


class QuestionExpect(_Strict):
    kind: str | None = None
    option_keys: list[str] = []
    min_options: int = 0
    no_question: bool = False

    @model_validator(mode="after")
    def _shape(self) -> QuestionExpect:
        if self.no_question and (self.kind or self.option_keys or self.min_options):
            raise ValueError("no_question excludes a question shape")
        if not self.no_question and not self.kind:
            raise ValueError("a question expectation needs kind or no_question")
        return self


class OutputFacts(_Strict):
    """Output-level facts. Pytest compares recorded evidence; it renders nothing."""

    duration_s: float | None = None
    duration_basis: Literal["creator_request", "strategy_choice"] | None = Field(
        default=None, description="Who set duration_s: the creator, or the strategy's own choice"
    )
    duration_tol_frac: float = 0.1
    voice_present: bool | None = None
    voice_source_ids: list[str] | None = None
    audio_plays_once: bool | None = None
    order_ids: list[str] | Literal["chronological"] | None = None
    exact_texts: list[str] | None = None


class RouteExpect(_Strict):
    """KRI-470 / PR-D: what the pure route resolver (``services/render_route``) must return.

    Resolved from the record's approved strategy + contract + media facts on ``platform``;
    no request text is an input.  Exactly one of ``route`` / ``refusal`` / ``choice``.
    """

    platform: Literal["phone", "cloud"]
    route: str | None = None
    refusal: str | None = Field(default=None, description="DeclineReason of a typed refusal")
    choice: str | None = Field(default=None, description="choice_kind of a needs_choice")
    field_path: str | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> RouteExpect:
        if sum(value is not None for value in (self.route, self.refusal, self.choice)) != 1:
            raise ValueError("a route expectation names exactly one of route / refusal / choice")
        return self


class Expect(_Strict):
    failure_reason: str | None = Field(
        default=None,
        description="Typed job failure_reason a replay-backed record stands for; the assertion "
        "itself lives in the record's repro test (see sources)",
    )
    contract: ContractExpect | None = None
    refusal: RefusalExpect | None = None
    cloud: CloudExpect | None = None
    question: QuestionExpect | None = None
    output_facts: OutputFacts | None = None
    route: RouteExpect | None = None


class Observation(_Strict):
    """Output evidence, newest last. The latest one is what the corpus judges.

    ``incident`` is what the creator actually got. A post-fix observation must
    name the ``proof`` (artifact + command) it came from, so editing a record
    cannot quietly claim coverage that nothing ran.
    """

    label: Literal["incident", "post-fix"]
    source: str
    proof: str | None = Field(
        default=None, description="Repro that resolves to a real test/script (loader checks)"
    )
    facts: OutputFacts
    evidence: dict[str, str] = {}

    @model_validator(mode="after")
    def _proof(self) -> Observation:
        if self.label == "post-fix" and not self.proof:
            raise ValueError("a post-fix observation must name its proof")
        return self


class XFail(_Strict):
    reason: str = Field(
        pattern=r"^KRI-\d+ / PR-[A-H]$", description="Owning ticket / PR letter that flips it"
    )
    scope: Scope


class Repro(_Strict):
    command: str
    status: Literal["available", "pending"] = "available"
    owner: str | None = Field(default=None, description="Ticket owning a pending repro")

    @model_validator(mode="after")
    def _shape(self) -> Repro:
        if self.status == "pending" and not self.owner:
            raise ValueError("a pending repro names its owning ticket")
        if self.status == "available" and not (
            self.command.startswith(("pytest ", "make kria-replay FIXTURE=", "python scripts/"))
        ):
            raise ValueError(
                "an available repro must be a pytest id, make kria-replay or a repo script"
            )
        return self


class IncidentRecord(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    incident: IncidentRef
    kind: Kind
    approved: Approved = Approved()
    inputs: Inputs = Inputs()
    expect: Expect = Expect()
    observations: list[Observation] = []
    xfail: list[XFail] = []
    repro: Repro
    sources: list[str] = Field(
        default=[], description="Existing in-repo fixtures this record was derived from"
    )

    @model_validator(mode="before")
    @classmethod
    def _xfail_forms(cls, data: Any) -> Any:
        # The brief allows ``xfail: null`` or one object; a record may need one per scope.
        if isinstance(data, dict):
            value = data.get("xfail")
            if value is None:
                data = {**data, "xfail": []}
            elif isinstance(value, dict):
                data = {**data, "xfail": [value]}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> IncidentRecord:
        scopes = [item.scope for item in self.xfail]
        if len(scopes) != len(set(scopes)):
            raise ValueError("at most one xfail per scope")
        asserted = {
            "contract": self.expect.contract is not None,
            "refusal": bool(
                self.expect.refusal
                and (self.expect.refusal.reason or self.expect.refusal.field_path)
            ),
            "question": self.expect.question is not None,
            "output": self.expect.output_facts is not None,
            "route": self.expect.route is not None,
        }
        for scope in scopes:
            if not asserted[scope]:
                raise ValueError(
                    f"xfail scope {scope!r} names an expectation the record never asserts"
                )
        if self.expect.output_facts and not self.observations:
            raise ValueError("output expectations need at least the incident observation")
        if self.kind == "clarification" and not self.inputs.turns:
            raise ValueError("a clarification record carries its conversation turns")
        return self

    def xfail_for(self, scope: Scope) -> XFail | None:
        return next((item for item in self.xfail if item.scope == scope), None)
