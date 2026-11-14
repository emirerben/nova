"""Every agent fixture is pinned to the prompt it was recorded for, or listed as unpinned.

`run_eval` fails a pinned fixture whose agent now sends a different prompt. This guard
stops a fixture from skipping that check by simply having no pin.
"""

from __future__ import annotations

import json
import re

from tests.evals.runners.eval_runner import (
    EVAL_FIXTURES_ROOT,
    UNPINNED,
    UNPINNED_LIST,
    load_fixture,
)

_SHA = re.compile(r"[0-9a-f]{16}")


def _agent_fixtures() -> dict[str, str]:
    """Relative path -> prompt_sha, for every fixture the agent replay harness runs."""
    found: dict[str, str] = {}
    for path in sorted(EVAL_FIXTURES_ROOT.rglob("*.json")):
        if path == UNPINNED_LIST:
            continue
        try:
            fixture = load_fixture(path)
        except ValueError:
            continue  # another harness's schema
        found[path.relative_to(EVAL_FIXTURES_ROOT).as_posix()] = fixture.prompt_sha
    return found


def test_every_fixture_has_a_pin_or_is_listed_unpinned() -> None:
    listed = set(json.loads(UNPINNED_LIST.read_text()))
    bad = []
    for label, sha in _agent_fixtures().items():
        if sha == UNPINNED:
            if label not in listed:
                bad.append(f"{label}: unpinned but not in unpinned.json")
        elif not _SHA.fullmatch(sha):
            bad.append(f"{label}: prompt_sha {sha!r} is missing; capture it live or stamp it")
    assert not bad, "\n".join(bad)


def test_unpinned_list_names_only_unpinned_fixtures() -> None:
    fixtures = _agent_fixtures()
    stale = [
        label for label in json.loads(UNPINNED_LIST.read_text()) if fixtures.get(label) != UNPINNED
    ]
    listed = "\n".join(stale)
    assert not stale, f"re-recorded or removed fixtures still listed in unpinned.json:\n{listed}"


def test_replay_rejects_a_fixture_recorded_for_another_prompt(tmp_path, monkeypatch) -> None:
    """Red control for the pin itself: a wrong pin must fail replay, a right one must pass."""
    from tests.evals.runners import eval_runner

    source = next(
        path
        for path in sorted((EVAL_FIXTURES_ROOT / "landmark_guess").rglob("*.json"))
        if _SHA.fullmatch(load_fixture(path).prompt_sha)
    )
    data = json.loads(source.read_text())
    monkeypatch.setattr(eval_runner, "EVAL_FIXTURES_ROOT", tmp_path)
    copy = tmp_path / "landmark_guess" / "golden" / "pinned.json"
    copy.parent.mkdir(parents=True)

    copy.write_text(json.dumps(data))
    assert eval_runner.run_eval(load_fixture(copy)).error is None

    copy.write_text(json.dumps({**data, "prompt_sha": "0" * 16}))
    error = eval_runner.run_eval(load_fixture(copy)).error
    assert error is not None and error.startswith("stale fixture")
