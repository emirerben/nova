"""German twins of the model-layer parity pairs (KRI-1453).

``test_model_parity.py`` asks whether the agents that turn a chat message into a decision
reach the same structured decision for an English message and its Turkish twin. Every pair
now also carries ``de``: the same ask in natural German. Under the language policy
(DECISIONS 2026-10-21) the model is the only reader a German message gets, so these pairs are
the German understanding suite for the model layer.

Free and offline, like the rest of ``tests/evals`` by default:

* ``test_german_wiring_dry`` runs every pair's German input through the real agent with
  German replies switched on: the input validates, the German message reaches the prompt,
  an agent that answers the creator is told to answer in German, and a canned answer parses
  and projects. Nothing is sent to a model.
* ``test_german_decides_like_english`` replays a recorded German answer
  (``cassettes/<id>.de.json``) against the English one when both exist. No German cassette
  is recorded yet: recording is a paid live run (``--eval-mode=live``), so every pair skips
  and says so until one is approved and recorded. Mismatches are printed, never failed,
  exactly like the Turkish layer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ..runners.eval_runner import CassetteModelClient
from .test_model_parity import (
    _DRY_RAW,
    AGENTS,
    PAIRS,
    _base_fixture,
    _replay_run,
    agent_class,
    build_input,
    field_diff,
    meets_expect,
    project,
    replay_lang,
)


@pytest.fixture
def german_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)


def test_every_pair_has_a_german_message() -> None:
    missing = [pair["id"] for pair in PAIRS if not str(pair.get("de") or "").strip()]
    assert not missing, f"add a natural German message (`de`) to: {missing}"
    same = [pair["id"] for pair in PAIRS if pair["de"] in {pair["en"], pair["tr"]}]
    assert not same, f"`de` must be German, not a copy of `en`/`tr`: {same}"


def test_german_messages_read_as_german(german_on: None) -> None:
    """Production picks the reply language from the creator's words; a pair whose German
    message is not detected as German would be dry-run as a different chat."""
    from app.kria.reply_language import creator_text_language

    wrong = {
        pair["id"]: creator_text_language(pair["de"])
        for pair in PAIRS
        if creator_text_language(pair["de"]) != "de"
    }
    # Short asks with no German function word ("Mach alle Texte #FFD400.") are allowed to be
    # undecided, never another language.
    assert all(language is None for language in wrong.values()), wrong


def test_german_wiring_dry(prod_profile, german_on: None) -> None:  # noqa: ARG001
    from app.kria.reply_language import prompt_language_line

    german_line = prompt_language_line("de")
    assert german_line, "German replies are on: the German language line must exist"
    problems: list[str] = []
    for pair in PAIRS:
        cls = agent_class(pair["agent"])
        fixture = _base_fixture(pair)
        raw = fixture["raw_text"] if fixture else _DRY_RAW[pair["agent"]]
        try:
            input_dict = build_input(pair, "de")
            agent = cls(CassetteModelClient(raw))
            prompt = agent.render_prompt(agent.Input.model_validate(input_dict))
            assert pair["de"] in prompt, "German message missing from the prompt"
            if AGENTS[pair["agent"]].get("reply_language") and input_dict.get("reply_language"):
                assert input_dict["reply_language"] == "de", input_dict["reply_language"]
                assert german_line in prompt, "agent is not told to answer in German"
            output = _replay_run(agent, input_dict, f"parity-dry/{pair['id']}/de")
            decision = project(pair["agent"], output.model_dump(mode="json"), pair["compare"])
            assert isinstance(decision, dict) and decision
            meets_expect(decision, pair["expect"])
        except Exception as exc:  # noqa: BLE001 — collect every broken pair
            problems.append(f"{pair['id']}/de: {type(exc).__name__}: {exc}")
    assert not problems, "\n".join(problems)


def test_reply_language_agents_get_german(german_on: None) -> None:
    """Every agent that answers the creator gets ``reply_language="de"`` for at least one
    German pair, so the German language line is exercised for each of them."""
    agents = {name for name, spec in AGENTS.items() if spec.get("reply_language")}
    told = {
        pair["agent"]
        for pair in PAIRS
        if pair["agent"] in agents and build_input(pair, "de").get("reply_language") == "de"
    }
    assert told == agents, sorted(agents - told)


@pytest.mark.parametrize("pair", PAIRS, ids=[pair["id"] for pair in PAIRS])
def test_german_decides_like_english(pair: dict[str, Any], german_on: None, prod_profile) -> None:  # noqa: ARG001
    english = replay_lang(pair, "en")
    german = replay_lang(pair, "de")
    if english is None or german is None:
        pytest.skip(f"{pair['id']}: no recorded German answer yet (paid live recording)")
    diff = field_diff(english["decision"], german["decision"])
    print(
        f"\n{pair['id']} [{pair['capability']}] de "
        + ("MATCH" if not diff else f"MISMATCH {json.dumps(diff, ensure_ascii=False)}")
    )
