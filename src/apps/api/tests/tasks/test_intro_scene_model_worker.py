"""KRI-1612: the model arm through the REAL `_run_phone_subtitled_job` and the approval check.

The harness of `test_intro_scene_worker.py`: the real worker, the real agent, its real parser and
the real compiler and placement. Only the provider transport is replayed (a stub answers the
intro-scene prompt and passes every other prompt through).

Ways it could fail (written before the wiring; a test each):
M1 the model arm is not stamped, yet the model is asked, or the seeded scene changes;
M2 a compose answer is asked more than once per render (the title is placed twice), or not pinned;
M3 a decline / failure / void answer still draws a scene, or breaks the render;
M4 a re-render of a pinned job asks again or draws something else;
M5 the scene she approved is not found in the drawn layers (the approval check refuses it);
M6 a pinned text rule or a logo above the title still costs a paid call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents import _model_client
from app.agents._runtime import ModelClient, ModelInvocation
from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_intro_scene_pick import POLICY
from tests.tasks.test_intro_scene_worker import EN, TR, _check, _scene_job, _title_rows

MARK = "You set a creator's approved title as an intro scene"
COMPOSE = {
    "decision": "compose",
    "voices": ["solid", "solid", "solid", "solid", "accent", "accent"],
    "font_pair": "serif_italic",
    "ink": "cream",
    "zone": "above_head",
    "entrance": "fade",
    "sound": "none",
}


class Routing(ModelClient):
    def __init__(self, delegate: ModelClient, answer: str | Exception) -> None:
        self.delegate = delegate
        self.answer = answer
        self.intro_calls = 0
        self.prompts: list[str] = []

    def invoke(self, **kwargs: Any) -> ModelInvocation:
        if MARK not in kwargs["prompt"]:
            return self.delegate.invoke(**kwargs)
        self.intro_calls += 1
        self.prompts.append(kwargs["prompt"])
        if isinstance(self.answer, Exception):
            raise self.answer
        return ModelInvocation(raw_text=self.answer, tokens_in=0, tokens_out=0, raw_response=None)


@pytest.fixture
def route(monkeypatch):
    def install(answer: str | Exception) -> Routing:
        client = Routing(_model_client.default_client(), answer)
        monkeypatch.setattr(_model_client, "default_client", lambda: client)
        return client

    return install


def _model_job(monkeypatch, title: str, language: str):
    job = _scene_job(monkeypatch, title, "grotesk_hand", language)
    ask = dict(job.assembly_plan[pick.INTRO_SCENE_ASK_FIELD])
    ask[pick.MODEL_ASK_FIELD] = {"version": pick.MODEL_VERSION}
    job.assembly_plan = {**job.assembly_plan, pick.INTRO_SCENE_ASK_FIELD: ask}
    return job


def _pin(job) -> dict[str, Any] | None:
    return job.assembly_plan[pick.INTRO_SCENE_ASK_FIELD].get(pick.MODEL_PICK_FIELD)


def test_the_seeded_arm_never_calls_the_model(monkeypatch, route):
    client = route(json.dumps(COMPOSE))
    job = _scene_job(monkeypatch, EN, "grotesk_hand", "en")  # stamped by KRI-1611: no marker
    gb._run_generative_job(str(job.id))
    assert client.intro_calls == 0 and _pin(job) is None
    rows = _title_rows(job)
    assert len(rows) >= 2 and {r["font_family"] for r in rows} == {"Hanken Grotesk", "Zeyada"}


@pytest.mark.parametrize(("title", "language"), [(EN, "en"), (TR, "tr")], ids=["en", "tr"])
def test_a_compose_answer_is_asked_once_drawn_and_pinned(monkeypatch, route, title, language):
    answer = {**COMPOSE, "voices": list(pick.accent_splits(title)[0])}
    client = route(json.dumps(answer))
    job = _model_job(monkeypatch, title, language)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    assert client.intro_calls == 1  # the title is placed more than once; the call is not
    assert _pin(job)["outcome"] == "compose"
    rows = _title_rows(job)
    assert len(rows) >= 2
    assert {r["font_family"] for r in rows} == {"Instrument Serif", "Instrument Serif Italic"}
    assert " ".join(r["text"] for r in rows) == title
    assert len({r["color"] for r in rows}) == 1
    _check(job, title)  # the approval check finds her words


def test_the_prompt_carries_her_words_only_as_untrusted_data(monkeypatch, route):
    client = route(json.dumps(COMPOSE))
    job = _model_job(monkeypatch, EN, "en")
    gb._run_generative_job(str(job.id))
    head, _, payload = client.prompts[0].partition("Input:\n")
    assert EN not in head and json.loads(payload)["untrusted"]["title_words"] == EN.split()


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"decision": "decline", "decline_reason": "title_unsuited"}),
        json.dumps({**COMPOSE, "ink": "hot_pink"}),
        json.dumps({**COMPOSE, "sound": "accent-pop"}),
        "not json",
        RuntimeError("provider 503"),
    ],
    ids=["decline", "void_ink", "void_sound", "not_json", "provider_error"],
)
def test_a_decline_or_failure_is_todays_title_and_the_render_completes(monkeypatch, route, answer):
    client = route(answer)
    job = _model_job(monkeypatch, EN, "en")
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    assert client.intro_calls == 1
    assert _pin(job)["outcome"] in ("decline", "failed")
    rows = _title_rows(job)
    assert [r["text"] for r in rows] == [EN]  # today's single row, not the seed's scene
    _check(job, EN)


def test_a_pinned_answer_is_drawn_again_without_a_call(monkeypatch, route):
    client = route(RuntimeError("must not be asked"))
    job = _model_job(monkeypatch, EN, "en")
    ask = dict(job.assembly_plan[pick.INTRO_SCENE_ASK_FIELD])
    ask[pick.MODEL_PICK_FIELD] = {
        "version": pick.MODEL_VERSION,
        "outcome": "compose",
        "scene": {k: COMPOSE[k] for k in ("voices", "font_pair", "ink", "zone", "entrance")},
    }
    job.assembly_plan = {**job.assembly_plan, pick.INTRO_SCENE_ASK_FIELD: ask}
    gb._run_generative_job(str(job.id))
    assert client.intro_calls == 0
    assert {r["font_family"] for r in _title_rows(job)} == {
        "Instrument Serif",
        "Instrument Serif Italic",
    }
    assert _pin(job) == ask[pick.MODEL_PICK_FIELD]  # kept, not rewritten


def test_a_pinned_text_rule_costs_no_call_and_keeps_todays_title(monkeypatch, route):
    client = route(json.dumps(COMPOSE))
    job = _model_job(monkeypatch, EN, "en")
    policy = {**POLICY, "requested": {"title": {"color": "#ff0000"}}}
    job.assembly_plan = {**job.assembly_plan, PHONE_TEXT_STYLE_FIELD: policy}
    gb._run_generative_job(str(job.id))
    assert client.intro_calls == 0 and _pin(job) is None
    assert [r["text"] for r in _title_rows(job)] == [EN]


def test_every_model_font_pair_draws_a_title_the_approval_check_accepts(monkeypatch, route):
    for pair in sorted(scene_mod.issue_menu(EN).font_pairs):
        answer = {**COMPOSE, "font_pair": pair, "entrance": "ink", "ink": "white"}
        route(json.dumps(answer))
        job = _model_job(monkeypatch, EN, "en")
        gb._run_generative_job(str(job.id))
        assert job.status == "awaiting_device", (pair, job.error_detail)
        assert _pin(job)["outcome"] == "compose", pair
        _check(job, EN)
