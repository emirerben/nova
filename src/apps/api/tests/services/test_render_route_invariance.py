"""KRI-470 PR-D: with the approved plan held fixed, rewording the conversation changes nothing.

Two corpora, both through the real contract builder and the persisted-job adapter:

* every incident-corpus record that carries an approved strategy or brief (both platforms);
* every ``tests/fixtures/request_following`` thread, with the approved plan derived from the
  thread's own checkable requirements (those threads record user messages and final edits,
  not strategies).

For each, the user messages are rewritten / reversed / blanked and the brief's free text is
rewritten to different non-empty prose.  The resolution (outcome, route, reason, field path,
drivers) must be identical, because nothing text-bearing is an input.  The wording variants
are injected into EVERY place the job persists them (``creator_request``, the binding's
``creator_request``), so a future reader of any of them would be caught.
"""

from __future__ import annotations

import json

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import CreativeBrief
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.render_route import resolve_route, route_inputs_from_job
from tests.evals.request_following.runner import THREAD_DIR, load_fixture
from tests.incidents import loader
from tests.incidents.models import IncidentRecord

RECORDS = [r for r in loader.load_records() if r.approved.strategy or r.approved.brief]
THREADS = sorted(THREAD_DIR.glob("*.json"))
PLATFORMS = ("phone", "cloud")


def _signature(resolution) -> tuple:  # noqa: ANN001
    return (
        resolution.outcome,
        resolution.route,
        resolution.reason,
        resolution.field_path,
        resolution.choice_kind,
        resolution.drivers,
    )


def _reworded(record: IncidentRecord, text: str) -> IncidentRecord:
    data = record.model_dump(mode="json")
    data["approved"]["creator_request"] = text
    for turn in data["inputs"]["turns"]:
        turn["text"] = text
    if data["approved"].get("brief"):
        for requirement in data["approved"]["brief"]["requirements"]:
            if requirement.get("description"):
                requirement["description"] = f"{text or 'x'} (reworded)"
    data["incident"]["summary"] = text or "blank"
    return IncidentRecord.model_validate(data)


def _incident_signature(record: IncidentRecord, platform: str) -> tuple:
    assembly, candidates = loader.route_job(record, platform)
    # The raw text the job carries today, in every place it is persisted.
    candidates["creator_request"] = record.approved.creator_request
    assembly["creator_brief_binding"]["creator_request"] = record.approved.creator_request
    inputs = route_inputs_from_job(assembly, candidates, platform=platform)
    assert inputs is not None
    return _signature(resolve_route(inputs))


WORDINGS = ("", "completely different words", "USE MY VOICE, not the clip audio. 30 seconds!")


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("record", [pytest.param(r, id=r.id) for r in RECORDS])
def test_incident_routes_ignore_the_conversation(record: IncidentRecord, platform: str) -> None:
    baseline = _incident_signature(record, platform)
    for text in WORDINGS:
        assert _incident_signature(_reworded(record, text), platform) == baseline, text


def test_the_corpus_has_plans_to_vary() -> None:
    assert len(RECORDS) >= 14
    assert len(THREADS) >= 30


def _thread_plan(path) -> tuple[dict, CreativeBrief | None]:  # noqa: ANN001
    """A strategy for what the thread's requirements pin: order, length, exact title/labels."""
    fixture = load_fixture(path)
    fields: dict = {}
    for requirement in fixture.requirements:
        params = requirement.params
        if requirement.checker == "order_by_key" and params.get("key") in {
            "capture_time",
            "chronological",
        }:
            fields["ordering_choice"] = "chronological"
        if requirement.checker == "duration_within":
            fields["target_duration_s"] = float(params["target_s"])
            fields["target_duration_requested"] = True
        if requirement.checker == "title_exact":
            fields["opening_title"] = str(params["literal"])[:200]
        if requirement.checker == "label_exact":
            fields["shot_labels"] = [str(label) for label in params["labels"]][:12]
    strategy = CreativeStrategy.model_validate(fields).model_dump(mode="json", exclude_none=True)
    return strategy, None


def _thread_signature(path, platform: str, messages: list[str]) -> tuple:  # noqa: ANN001
    strategy, _brief = _thread_plan(path)
    contract = build_render_contract(strategy, generation_id="gen-1")
    assert contract is not None
    assembly: dict = {
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        "creator_brief_binding": {
            "creator_request": "\n".join(messages),
            "latest_message": messages[-1] if messages else "",
            "media_snapshot": {"clip_assignments": []},
        },
    }
    if platform == "phone":
        assembly[PHONE_SOURCES_FIELD] = []
    candidates = {
        REQUIREMENT_VERSION_FIELD: 1,
        PLAN_AUTHORITY_FIELD: 1,
        "edit_format": "montage",
        "creator_strategy": strategy,
        "clip_paths": ["a", "b", "c"],
        "creator_request": "\n".join(messages),
        "brief": {"creator_request": "\n".join(messages)},
    }
    inputs = route_inputs_from_job(assembly, candidates, platform=platform)
    assert inputs is not None
    return _signature(resolve_route(inputs))


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("path", [pytest.param(p, id=p.stem) for p in THREADS])
def test_request_following_threads_route_the_same_whatever_was_said(path, platform: str) -> None:
    original = [turn.user_message for turn in load_fixture(path).turns]
    baseline = _thread_signature(path, platform, original)
    variants = [
        list(reversed(original)),
        [""] * len(original),
        [WORDINGS[1]] * len(original),
        [WORDINGS[2], *original],
        [json.dumps(original, ensure_ascii=False)],
    ]
    for messages in variants:
        assert _thread_signature(path, platform, messages) == baseline
