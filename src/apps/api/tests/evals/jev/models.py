"""Strict JSON-safe schemas for the Jev shadow evaluation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Split = Literal["calibration", "held_out"]
Provenance = Literal["prod_capture", "human_authored", "derived"]
Scenario = Literal["good", "flawed", "ambiguous", "non_english"]
LabelSource = Literal["human", "derived", "adjudicated"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RequiredItemLabel(Strict):
    item_id: str = Field(min_length=1)
    required: bool


class ClaimLabel(Strict):
    claim_id: str = Field(min_length=1)
    unsupported: bool


class LabelMetadata(Strict):
    label_source: LabelSource
    human_adjudicated: bool
    notes: str | None = None


class Case(Strict):
    case_id: str = Field(min_length=1)
    split: Split
    provenance: Provenance
    group_id: str = Field(min_length=1)
    language: str = Field(min_length=1)
    scenario: Scenario
    payload: dict[str, Any]
    required_items: list[RequiredItemLabel] = Field(min_length=1)
    unsupported_claims: list[ClaimLabel] = Field(min_length=1)
    label_metadata: LabelMetadata

    @model_validator(mode="after")
    def _unique_labels(self) -> Case:
        ids = [x.item_id for x in self.required_items]
        claims = [x.claim_id for x in self.unsupported_claims]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate required item id")
        if len(claims) != len(set(claims)):
            raise ValueError("duplicate unsupported claim id")
        return self


class Probability(Strict):
    item_id: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)


class ClaimProbability(Strict):
    claim_id: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)


class Prediction(Strict):
    case_id: str = Field(min_length=1)
    required_items: list[Probability] = Field(default_factory=list)
    unsupported_claims: list[ClaimProbability] = Field(default_factory=list)
    success: bool = True
    error: bool = False
    fallback: bool = False
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    retries: int = Field(default=0, ge=0)
    error_code: str | None = None

    @model_validator(mode="after")
    def _unique_predictions(self) -> Prediction:
        for values, label in (
            (self.required_items, "required item"),
            (self.unsupported_claims, "claim"),
        ):
            ids = [v.item_id if hasattr(v, "item_id") else v.claim_id for v in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} prediction id")
        return self


class Dataset(Strict):
    cases: list[Case] = Field(min_length=1)
    # Optional for small local fixtures; production decision sets must be 100–200 labels.
    decision_count: int | None = Field(default=None, ge=100, le=200)

    @model_validator(mode="after")
    def _frozen_splits(self) -> Dataset:
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case_id")
        groups: dict[str, set[str]] = {}
        for case in self.cases:
            groups.setdefault(case.group_id, set()).add(case.split)
        leaked = sorted(g for g, splits in groups.items() if len(splits) > 1)
        if leaked:
            raise ValueError(f"calibration/held_out group leakage: {leaked}")
        count = sum(len(c.required_items) + len(c.unsupported_claims) for c in self.cases)
        if self.decision_count is not None and self.decision_count != count:
            raise ValueError("decision_count must equal required-item plus claim labels")
        return self

    def validate_decision_set(self) -> Dataset:
        """Require the production decision set size while allowing tiny unit fixtures."""
        count = sum(len(c.required_items) + len(c.unsupported_claims) for c in self.cases)
        if not 100 <= count <= 200:
            raise ValueError("decision set must contain 100-200 labels")
        return self


JevCase = Case
JevDataset = Dataset
JevPrediction = Prediction
