"""Private frozen comparisons; no provider calls, media reads or training uploads.

Source identities/coverage must come from an audited owner-scoped capture. This
boundary detects contamination and changed evidence; it cannot authenticate an
operator or turn a declared fingerprint into proof of its underlying footage.
"""

import math
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.agents.camera_roll_stories import CameraRollStoriesOutput
from app.agents.media_moment_relevance import MomentCandidate
from app.services.editorial_taste import TasteStore, digest, text


class Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Arm(Data):
    model: str = Field(min_length=1, max_length=100)
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Source(Data):
    project_id: UUID
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage: Literal["complete", "partial"]
    footage_ids: list[str] = Field(min_length=1, max_length=100)
    group_ids: list[str] = Field(min_length=1, max_length=100)


class Case(Data):
    id: UUID
    project_id: UUID
    footage_ids: list[str] = Field(min_length=1, max_length=100)
    group_ids: list[str] = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=500)
    candidates: list[MomentCandidate] = Field(min_length=1, max_length=100)


class Acceptance(Data):
    minimum_cases: int = Field(ge=3, le=100)
    minimum_preference_lower_bound: float = Field(ge=0.5, lt=1)
    minimum_would_post_rate: float = Field(gt=0, le=1)
    maximum_problem_rate: float = Field(ge=0, lt=1)


class Pack(Data):
    id: UUID
    owner_id: UUID
    purpose: Literal[
        "synthetic_workflow_check", "development_comparison", "independent_quality_evaluation"
    ]
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    arms: dict[str, Arm]
    source_registry: list[Source] = Field(min_length=1, max_length=50)
    cases: list[Case] = Field(min_length=1, max_length=20)
    criteria: dict
    acceptance: Acceptance | None = None


class Outcome(Data):
    status: Literal["ready", "failed"]
    output: CameraRollStoriesOutput | None = None
    error_class: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_]*$")


class Judgment(Data):
    cards_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected: list[Literal["A", "B"]] = Field(max_length=2)
    would_post: dict[str, StrictBool]
    problems: dict[
        str,
        list[
            Literal[
                "unsupported_claims",
                "footage_mismatch",
                "not_executable",
                "invented_experience",
                "generic",
                "forced_question",
                "other",
            ]
        ],
    ]
    reason: str = Field(min_length=1, max_length=1000)
    reviewer: str = Field(min_length=1, max_length=120)
    creator_statement: str = Field(min_length=1, max_length=4000)


def identity_set(values, *, hashes=False):
    import re

    if len(set(values)) != len(values) or any(
        not re.fullmatch(r"[a-f0-9]{64}" if hashes else r"[A-Za-z0-9_-]{1,100}", value)
        for value in values
    ):
        raise ValueError("Source identities must be unique, bounded hashes/groups, never URLs")
    return set(values)


def bounds(case):
    known = {value.id: value for value in case.candidates}
    if len(known) != len(case.candidates) or any(
        not 0 <= value.start_s < value.end_s
        or not 1 <= value.end_s - value.start_s <= 20
        or not math.isfinite(value.start_s + value.end_s)
        for value in case.candidates
    ):
        raise ValueError("Use unique, finite supported source intervals")
    return known


class Evaluation:
    def __init__(self, path):
        self.store = TasteStore(path)

    def load(self):
        value = self.store.load()
        if value is None or value.get("schema_version") != "editorial_evaluation_v1":
            raise ValueError("Preregister this comparison first")
        return value

    def register(self, raw, corpus_path):
        pack = Pack.model_validate(raw)
        corpus = TasteStore(corpus_path).load()
        if (
            corpus is None
            or str(pack.owner_id) != corpus["owner_id"]
            or digest(corpus) != pack.corpus_sha256
        ):
            raise ValueError("Owner or frozen corpus identity does not match")
        if (
            set(pack.arms) != {"baseline", "personalized"}
            or pack.arms["baseline"] == pack.arms["personalized"]
        ):
            raise ValueError("Preregister two distinct baseline/personalized arms")
        if pack.criteria != {
            "primary": "creator_preference",
            "secondary": ["would_post", "unsupported_claims", "footage_mismatch"],
        }:
            raise ValueError("Freeze preference, willingness and factuality as separate outcomes")
        if pack.purpose == "independent_quality_evaluation" and (
            pack.acceptance is None or (corpus.get("profile") or {}).get("status") != "approved"
        ):
            raise ValueError("Independent evaluation needs an approved profile and acceptance bars")
        training = {e["project_id"]: e["source_sha256"] for e in corpus["examples"]}
        registry = {str(e.project_id): e for e in pack.source_registry}
        if set(registry) != set(training) or len(registry) != len(pack.source_registry):
            raise ValueError("Register source coverage for every learning example exactly once")
        footage, groups = set(), set()
        for project, row in registry.items():
            if row.coverage != "complete" or row.source_sha256 != training[project]:
                raise ValueError("Incomplete or changed learning-source coverage")
            footage |= identity_set(row.footage_ids, hashes=True)
            groups |= identity_set(row.group_ids)
        seen_cases, seen_projects = set(), set()
        for case in pack.cases:
            project = str(case.project_id)
            if (
                str(case.id) in seen_cases
                or project in seen_projects
                or project in corpus["exposed_project_ids"]
                or project not in corpus["holdout_project_ids"]
            ):
                raise ValueError("Each case must be an unexposed, reserved held-out project")
            current_footage = identity_set(case.footage_ids, hashes=True)
            current_groups = identity_set(case.group_ids)
            if current_footage & footage or current_groups & groups:
                raise ValueError("Learning/test cases share footage or outing groups")
            footage |= current_footage
            groups |= current_groups
            seen_cases.add(str(case.id))
            seen_projects.add(project)
            bounds(case)
        normalized = pack.model_dump(mode="json")
        with self.store.locked():
            prior = self.store.load()
            if prior is not None:
                if prior.get("pack") != normalized:
                    raise ValueError("Comparison preregistration is immutable")
                return {"cases": len(pack.cases), "provider_calls": 0, "registered": True}
            self.store.save(
                {
                    "schema_version": "editorial_evaluation_v1",
                    "owner_id": str(pack.owner_id),
                    "revision": 1,
                    "pack": normalized,
                    "outcomes": {},
                    "judgments": {},
                    "assignments": {
                        str(c.id): (
                            ["baseline", "personalized"]
                            if int(digest([str(pack.id), str(c.id)]), 16) % 2
                            else ["personalized", "baseline"]
                        )
                        for c in pack.cases
                    },
                }
            )
        return {"cases": len(pack.cases), "provider_calls": 0, "registered": True}

    @staticmethod
    def case(value, case_id):
        return next(
            (
                Case.model_validate(c)
                for c in value["pack"]["cases"]
                if c["id"] == str(UUID(case_id))
            ),
            None,
        )

    def output(self, case_id, arm, raw):
        if arm not in {"baseline", "personalized"}:
            raise ValueError("Unknown comparison arm")
        outcome = Outcome.model_validate(raw)
        if (outcome.status == "ready") != (outcome.output is not None):
            raise ValueError("Ready needs output; failed preserves an empty failure slot")
        with self.store.locked():
            value = self.load()
            case = self.case(value, case_id)
            if case is None:
                raise ValueError("Unknown comparison case")
            known = bounds(case)
            if outcome.output:
                for idea in outcome.output.stories:
                    if (
                        len(set(idea.moment_ids)) != len(idea.moment_ids)
                        or not set(idea.moment_ids) <= known.keys()
                        or sum(known[m].end_s - known[m].start_s for m in idea.moment_ids) > 60
                        or len(idea.content_plan.beat_reasons) != len(idea.moment_ids)
                    ):
                        raise ValueError(
                            "Proposal does not match the frozen supported source windows"
                        )
            key = str(case.id)
            body = outcome.model_dump(mode="json")
            prior = value["outcomes"].get(key, {}).get(arm)
            if prior is not None:
                if prior != body:
                    raise ValueError("Frozen outcome cannot be replaced or retried")
            else:
                value["outcomes"].setdefault(key, {})[arm] = body
                value["revision"] += 1
                self.store.save(value)
        return {"recorded": True}

    def cards(self, case_id, value=None):
        value = value or self.load()
        case = self.case(value, case_id)
        if case is None:
            raise ValueError("Unknown comparison case")
        key = str(case.id)
        outcomes = value["outcomes"].get(key, {})
        if set(outcomes) != {"baseline", "personalized"}:
            raise ValueError("Record both output/failure slots before review")
        cards = {"case_id": key, "query": case.query, "options": []}
        for label, arm in zip(("A", "B"), value["assignments"][key], strict=True):
            result = outcomes[arm]
            cards["options"].append(
                {
                    "label": label,
                    "status": result["status"],
                    "stories": (result.get("output") or {}).get("stories", []),
                }
            )
        return {**cards, "cards_sha256": digest(cards)}

    def judge(self, case_id, raw):
        judgment = Judgment.model_validate(raw)
        if (
            len(judgment.selected) != len(set(judgment.selected))
            or set(judgment.would_post) != {"A", "B"}
            or set(judgment.problems) != {"A", "B"}
            or any(len(v) > 7 or len(v) != len(set(v)) for v in judgment.problems.values())
        ):
            raise ValueError("Review both labels; preserve tie/neither and separate quality checks")
        judgment.reason = text(judgment.reason, 1000)
        judgment.reviewer = text(judgment.reviewer, 120)
        judgment.creator_statement = text(judgment.creator_statement, 4000)
        with self.store.locked():
            value = self.load()
            cards = self.cards(case_id, value)
            if judgment.cards_sha256 != cards["cards_sha256"]:
                raise ValueError("The reviewed cards do not match these frozen outputs")
            for option in cards["options"]:
                if not option["stories"] and (
                    judgment.would_post[option["label"]] or option["label"] in judgment.selected
                ):
                    raise ValueError(
                        "An empty/failed proposal cannot be selected or marked publishable"
                    )
            key = cards["case_id"]
            body = judgment.model_dump(mode="json")
            prior = value["judgments"].get(key)
            if prior is not None:
                if prior != body:
                    raise ValueError("The exact human judgment is already frozen")
            else:
                value["judgments"][key] = body
                value["revision"] += 1
                self.store.save(value)
        return {"recorded": True}

    def report(self, corpus_path):
        value = self.load()
        pack = value["pack"]
        corpus = TasteStore(corpus_path).load()
        unchanged = bool(
            corpus
            and digest(corpus) == pack["corpus_sha256"]
            and corpus["owner_id"] == value["owner_id"]
        )
        counts = {"baseline": 0, "personalized": 0, "tie": 0, "neither": 0}
        failures = {"baseline": 0, "personalized": 0}
        willing = {"baseline": 0, "personalized": 0}
        problems = {"baseline": 0, "personalized": 0}
        for key, outputs in value["outcomes"].items():
            for arm, result in outputs.items():
                failures[arm] += result["status"] == "failed"
        for key, judgment in value["judgments"].items():
            arms = dict(zip(("A", "B"), value["assignments"][key], strict=True))
            selected = judgment["selected"]
            counts[
                "neither" if not selected else "tie" if len(selected) == 2 else arms[selected[0]]
            ] += 1
            for label, arm in arms.items():
                willing[arm] += judgment["would_post"][label]
                problems[arm] += bool(judgment["problems"][label])
        n = counts["baseline"] + counts["personalized"]
        rate = counts["personalized"] / n if n else None
        interval = None
        if n:
            z = 1.959963984540054
            center = (rate + z * z / (2 * n)) / (1 + z * z / n)
            radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
            interval = [max(0, center - radius), min(1, center + radius)]
        total, judged = len(pack["cases"]), len(value["judgments"])
        bars = pack.get("acceptance")
        independent = pack["purpose"] == "independent_quality_evaluation"
        passed = bool(
            independent
            and unchanged
            and judged == total
            and bars
            and total >= bars["minimum_cases"]
            and interval
            and interval[0] > bars["minimum_preference_lower_bound"]
            and willing["personalized"] / total >= bars["minimum_would_post_rate"]
            and problems["personalized"] / total <= bars["maximum_problem_rate"]
        )
        return {
            "purpose": pack["purpose"],
            "total_cases": total,
            "judged_cases": judged,
            "pending_judgments": total - judged,
            "missing_output_slots": 2 * total - sum(len(v) for v in value["outcomes"].values()),
            "preference_counts": counts,
            "failed_outputs": failures,
            "would_post_counts": willing,
            "problem_counts": problems,
            "personalized_preference_rate_excluding_tie_neither": rate,
            "preference_wilson_95": interval,
            "corpus_unchanged": unchanged,
            "quality_evaluated": independent and unchanged and judged == total,
            "independent_quality_pass": passed,
            "fine_tuning_ready": False,
            "training_decision": (
                "Requires human review of repeatable residual errors "
                "and an approved training plan/budget."
            ),
            "limits": (
                "Source coverage is operator-supplied and needs an owner-scoped audit. "
                "Human judgments require genuine creator replies. Preference does not prove "
                "export fidelity or audience performance. No provider calls are performed "
                "by this workflow."
            ),
        }
