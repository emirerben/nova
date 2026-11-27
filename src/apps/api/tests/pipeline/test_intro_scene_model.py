"""KRI-1612: the model arm of the intro scene (`intro_scene_pick` + `agents/intro_scene_designer`).

Written before the wiring was trusted. Each test is a way the arm could go wrong (the same list
is in `app/pipeline/intro_scene_pick.py`, "the model arm"):

* A1: the kill switch is off but the model runs, or flips after dispatch (`test_*_stamp_*`,
  `test_no_call_*`).
* A2: the model writes text or numbers that reach the screen (the schema walk in
  `tests/evals/test_intro_scene_agent_evals.py` and `test_the_pin_holds_ids_only`).
* A3: a decline, a failed call or a void answer draws a scene anyway (`test_decline_*`,
  `test_a_failed_call_*`, `test_a_void_answer_*`).
* A4: a re-render asks again or picks differently (`test_a_pinned_answer_*`, `test_pin_*`).
* A5: the model's pick cannot sit clear on the footage (`test_the_model_pick_that_*`).
* A6: its ink is unreadable on the take (`test_the_model_ink_*`).
* A7: a sound nobody issued, or a cue with no sound to play (the eval's void-answer table and
  `test_a_failed_call_*`); whether one plays is `test_intro_scene_sound_worker.py`.
* A8: it asks for a scene that would not apply (`test_no_call_when_the_scene_would_not_apply`).

Real compiler, real `place_title_look`, real Skia; only the provider transport is replayed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.config import settings
from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from tests.pipeline.test_intro_scene_pick import (
    EN,
    TR,
    FakeJob,
    _fit,
    _frames,
    _place_with,
)

COMPOSE = {
    "decision": "compose",
    "voices": ["solid", "solid", "solid", "solid", "accent", "accent"],
    "font_pair": "grotesk_hand",
    "ink": "oxblood",
    "zone": "above_head",
    "entrance": "ink",
    "sound": "none",
}


class StubClient(ModelClient):
    """The provider transport, replayed: returns ``raw`` and counts the calls."""

    def __init__(self, raw: str | Exception) -> None:
        self.raw = raw
        self.calls = 0

    def invoke(self, **_kwargs: Any) -> ModelInvocation:
        self.calls += 1
        if isinstance(self.raw, Exception):
            raise self.raw
        return ModelInvocation(raw_text=self.raw, tokens_in=0, tokens_out=0, raw_response=None)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_enabled", True)
    monkeypatch.setattr(settings, "intro_scene_user_ids", ["6f1c0d52-4a4e-4f6e-9b7c-0d6a3c5f1a11"])
    monkeypatch.setattr(settings, "title_look_enabled", True)
    monkeypatch.setattr(settings, "intro_scene_model_enabled", True)


def _stamped(title: str = EN) -> dict[str, Any]:
    job = FakeJob(title)
    pick.stamp_intro_scene(job)
    return job.assembly_plan


def _seeded(title: str = EN) -> dict[str, Any]:
    job = FakeJob(title)
    pick.stamp_seeded_intro_scene(job)
    return job.assembly_plan


def _resolve(plan, client, *, title: str = EN, applies: bool = True, frames=()):
    return pick.resolve_for_render(
        plan,
        title,
        applies=applies,
        language=None,
        footage_notes="a woman at a desk",
        frames=frames,
        job_id=None,
        client=client,
    )


# --- A1: the kill switch ------------------------------------------------------------------


def test_the_model_stamp_is_pinned_at_dispatch_and_adds_only_its_marker(monkeypatch):
    on = pick.pinned_intro_scene(_stamped())
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    off = pick.pinned_intro_scene(_seeded())
    assert on is not None and off is not None
    assert pick.model_requested(on) and not pick.model_requested(off)
    assert {k: v for k, v in on.items() if k != pick.MODEL_ASK_FIELD} == off  # the seed is the same


def test_with_its_own_switch_off_the_stamp_is_exactly_the_seeded_one(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    ask = pick.pinned_intro_scene(_seeded())
    assert ask is not None and pick.MODEL_ASK_FIELD not in ask
    assert set(ask) == {"version", "seed", "look", "inks", "candidates", "excluded"}  # KRI-1611's


def test_the_model_switch_does_not_stamp_without_the_scene_flag(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_enabled", False)
    assert pick.INTRO_SCENE_ASK_FIELD not in _stamped()


def test_no_call_without_the_model_stamp(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    client = StubClient(json.dumps(COMPOSE))
    plan, new = _resolve(_stamped(), client)
    assert client.calls == 0 and new is None
    # and a flag that flips on AFTER dispatch changes nothing: the worker reads the stamp
    monkeypatch.setattr(settings, "intro_scene_model_enabled", True)
    _plan, new = _resolve(plan, client)
    assert client.calls == 0 and new is None


def test_no_call_when_the_scene_would_not_apply():
    client = StubClient(json.dumps(COMPOSE))
    _plan, new = _resolve(_stamped(), client, applies=False)
    assert client.calls == 0 and new is None


def test_no_call_for_a_job_with_no_stamp():
    client = StubClient(json.dumps(COMPOSE))
    assert _resolve({}, client)[1] is None and _resolve(None, client)[1] is None
    assert client.calls == 0


# --- the answers ----------------------------------------------------------------------------


def test_a_compose_answer_is_asked_once_and_pinned_as_ids_only():
    client = StubClient(json.dumps(COMPOSE))
    plan, new = _resolve(_stamped(), client)
    assert client.calls == 1
    assert new is not None and new["outcome"] == "compose"
    assert new["prompt_version"].startswith("is-")
    assert set(new["scene"]) == {"voices", "font_pair", "ink", "zone", "entrance"}  # no text
    assert pick.pinned_model_pick(pick.pinned_intro_scene(plan) or {}) == new


def test_the_pin_holds_ids_only():
    _plan, new = _resolve(_stamped(), StubClient(json.dumps(COMPOSE)))
    assert new is not None
    for value in new["scene"].values():
        assert isinstance(value, str) or all(v in ("solid", "accent") for v in value)
    assert all(not isinstance(v, int | float) for v in new["scene"].values())


def test_a_decline_is_honoured_and_pinned():
    raw = json.dumps({"decision": "decline", "decline_reason": "title_unsuited"})
    plan, new = _resolve(_stamped(), StubClient(raw))
    assert new is not None and new["outcome"] == "decline"
    ask = pick.pinned_intro_scene(plan)
    assert ask is not None
    fit_ask, info = pick.ask_for_fit(ask)
    assert fit_ask is None and info == {"arm": "model", "model_outcome": "decline"}


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (json.dumps({**COMPOSE, "ink": "hot_pink"}), "unissued ink"),
        (json.dumps({**COMPOSE, "entrance": "pen"}), "the pen entrance, never issued"),
        (json.dumps({**COMPOSE, "sound_cue": "accent"}), "a cue with no sound"),
        (json.dumps({**COMPOSE, "sound": "boom", "sound_cue": "each"}), "a sound nobody issued"),
        (json.dumps({**COMPOSE, "text": "HACKED"}), "smuggled text"),
        (json.dumps({**COMPOSE, "voices": ["accent"] * 6}), "a split nobody issued"),
        ("not json", "unreadable"),
        (RuntimeError("provider 503"), "provider error"),
        (TimeoutError("deadline"), "timeout"),
    ],
)
def test_a_failed_call_or_void_answer_is_todays_title_and_pinned(raw, why):
    client = StubClient(raw)
    plan, new = _resolve(_stamped(), client)
    assert client.calls == 1, why  # one paid call, never a retry
    assert new is not None and new["outcome"] == "failed", why
    assert "scene" not in new
    assert pick.ask_for_fit(pick.pinned_intro_scene(plan) or {})[0] is None


def test_a_title_with_nothing_to_issue_makes_no_call():
    client = StubClient(json.dumps(COMPOSE))
    plan, new = _resolve(_stamped(), client, title="hello")  # one word: no accent split
    assert client.calls == 0 and new is not None and new["outcome"] == "nothing_to_ask"
    assert pick.ask_for_fit(pick.pinned_intro_scene(plan) or {})[0] is None


# --- A4: the pin --------------------------------------------------------------------------


def test_a_pinned_answer_is_never_asked_for_again():
    plan, new = _resolve(_stamped(), StubClient(json.dumps(COMPOSE)))
    second = StubClient(json.dumps({**COMPOSE, "font_pair": "serif_italic"}))
    again_plan, again = _resolve(plan, second)
    assert second.calls == 0 and again is None
    assert pick.pinned_intro_scene(again_plan)[pick.MODEL_PICK_FIELD] == new


def test_pin_writes_into_the_job_once_and_never_overwrites():
    base = _stamped()
    _plan, first = _resolve(base, StubClient(json.dumps(COMPOSE)))
    written = pick.pin_model_pick(base, first)
    assert written[pick.INTRO_SCENE_ASK_FIELD][pick.MODEL_PICK_FIELD] == first
    other = {**first, "outcome": "failed"}
    assert pick.pin_model_pick(written, other) is written  # untouched
    assert pick.pin_model_pick(base, None) is base
    unstamped = {"creator_generation_id": "g"}
    assert pick.pin_model_pick(unstamped, first) is unstamped


def test_an_unreadable_pin_is_todays_title_not_the_seed():
    ask = {**(pick.pinned_intro_scene(_stamped()) or {})}
    for bad in (
        {"version": 99, "outcome": "compose", "scene": COMPOSE},
        {"version": 1, "outcome": "compose", "scene": {"font_pair": "fraunces"}},
        {"version": 1, "outcome": "compose"},
    ):
        fit_ask, info = pick.ask_for_fit({**ask, pick.MODEL_PICK_FIELD: bad})
        assert fit_ask is None, bad
        assert info["arm"] == "model"
    assert pick.ask_for_fit(ask)[0] is None  # stamped but never answered: today's title


# --- the fit: the model arm draws exactly its scene ------------------------------------------


def _model_ask(answer: dict[str, Any], title: str = EN) -> dict[str, Any]:
    plan, new = _resolve(_stamped(title), StubClient(json.dumps(answer)), title=title)
    assert new is not None and new["outcome"] == "compose", new
    fit_ask, _info = pick.ask_for_fit(pick.pinned_intro_scene(plan) or {})
    assert fit_ask is not None
    return dict(fit_ask)


def test_the_model_arm_draws_its_font_split_and_ink_in_her_words():
    ask = _model_ask(COMPOSE)
    rows, receipt, info = _fit(ask, EN, _place_with())
    assert rows is not None and receipt is not None, info
    assert info["chosen"]["source"] == "model"
    assert info["chosen"]["font_pair"] == "grotesk_hand"
    assert " ".join(row["text"] for row in rows) == EN
    assert len({row["color"] for row in rows}) == 1
    assert rows[0]["color"] == scene_mod.INKS["oxblood"]
    assert [row["text"] for row in rows] == ["the biggest design trend", "right now"]


def test_the_seeded_arm_is_unchanged_by_the_model_code():
    ask = pick.pinned_intro_scene(_stamped()) or {}
    seeded = {k: v for k, v in ask.items() if k != pick.MODEL_ASK_FIELD}
    assert pick.ask_for_fit(seeded) == (seeded, {})
    _rows, _receipt, info = _fit(seeded, EN, _place_with())
    assert "source" not in info["chosen"]


def test_the_model_pick_that_cannot_sit_clear_is_todays_title_not_the_seed():
    ask = _model_ask(COMPOSE)

    def place(rows, position):
        return rows, {"status": "best_effort", "face_collision": True}

    rows, receipt, info = _fit(ask, EN, place)
    assert rows is None and receipt is None
    assert [t["font_pair"] for t in info["tried"]] == ["grotesk_hand"] * len(info["tried"])
    assert {t["zone"] for t in info["tried"]} == {"above_head", "top_left", "mid_frame"}


def test_the_model_asks_its_zone_first_then_the_others():
    ask = _model_ask({**COMPOSE, "zone": "top_left"})
    assert ask["candidates"][0]["zones"][0] == "top_left"
    assert set(ask["candidates"][0]["zones"]) == set(scene_mod.ZONES)


@pytest.mark.parametrize(("luma", "expected"), [(240, "oxblood"), (20, "chartreuse")])
def test_the_model_ink_leads_but_the_take_still_decides_legibility(luma, expected):
    # oxblood reads on a light take; on a dark one the engine takes the next ink that reads
    ask = _model_ask(COMPOSE)
    assert ask["inks"][0] == "oxblood" and set(ask["inks"]) == set(scene_mod.INKS)
    rows, _receipt, _info = _fit(ask, EN, _place_with())
    assert rows is not None
    inked = pick.intro_ink_pass(
        rows,
        _frames(luma),
        canvas=__import__("app.pipeline.canvas", fromlist=["PORTRAIT"]).PORTRAIT,
    )
    got = next(n for n, hx in scene_mod.INKS.items() if hx == inked[0]["color"])
    if luma > 128:
        assert got == expected
    else:
        assert got != "oxblood"


def test_turkish_title_model_pick_keeps_her_words_and_language():
    answer = {**COMPOSE, "voices": ["solid"] * 6 + ["accent"] * 2}
    ask = _model_ask(answer, TR)
    rows, _receipt, info = _fit(ask, TR, _place_with(), "tr")
    assert rows is not None, info
    assert " ".join(row["text"] for row in rows) == TR
    assert all(row.get("text_language") == "tr" for row in rows)


# --- A6/measure ---------------------------------------------------------------------------


@pytest.mark.parametrize(("luma", "expected"), [(250, "light"), (128, "mid"), (10, "dark")])
def test_brightness_is_measured_from_the_frames(luma, expected):
    assert pick.take_brightness(_frames(luma)) == expected


def test_brightness_without_frames_is_unknown():
    assert pick.take_brightness([]) == "unknown"
    assert pick.take_brightness([object()]) == "unknown"
