"""Every agent has an eval, or a written reason it has none.

An agent has an eval when replayable fixtures name it (`tests/fixtures/agent_evals/`) or a
`tests/evals/test_*.py` refers to it. Anything else must be listed in no_eval_reasons.json with why.
The list may only shrink: adding an agent without an eval, or keeping an entry once the agent
gains one, fails here. Traffic figures are production `agent_run` calls over the 30 days to
2026-11-15 (`python scripts/admin.py --prod GET "agents/report?days=30"`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.evals.runners.eval_runner import EVAL_FIXTURES_ROOT

API_ROOT = Path(__file__).resolve().parents[2]

NO_EVAL_REASONS: dict[str, str] = json.loads(
    (Path(__file__).parent / "no_eval_reasons.json").read_text()
)


def _agent_specs() -> dict[str, str]:
    """Spec name -> agent class name, for every AgentSpec under app/."""
    specs: dict[str, str] = {}
    for path in sorted((API_ROOT / "app").rglob("*.py")):
        text = path.read_text(errors="ignore")
        for match in re.finditer(r'AgentSpec\(\s*name="(nova\.[\w.]+)"', text):
            classes = re.findall(r"class (\w+)\(Agent\[", text[: match.start()])
            specs[match.group(1)] = classes[-1] if classes else ""
    return specs


def _agents_with_evals(specs: dict[str, str]) -> set[str]:
    fixtures: set[str] = set()
    for path in EVAL_FIXTURES_ROOT.rglob("*.json"):
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        if isinstance(data, dict) and {"agent", "input", "raw_text"} <= data.keys():
            fixtures.add(data["agent"])
    tests = [p.read_text(errors="ignore") for p in (API_ROOT / "tests" / "evals").glob("test_*.py")]
    tests = [text for text in tests if "NO_EVAL_REASONS" not in text]
    covered = set(fixtures)
    for name, cls in specs.items():
        if any(f'"{name}"' in text or (cls and re.search(rf"\b{cls}\b", text)) for text in tests):
            covered.add(name)
    return covered


def test_every_agent_has_an_eval_or_a_reason() -> None:
    specs = _agent_specs()
    assert len(specs) > 60, "the AgentSpec scan found too few agents; the regex drifted"
    missing = sorted(set(specs) - _agents_with_evals(specs) - set(NO_EVAL_REASONS))
    assert not missing, (
        "agents with no eval and no entry in NO_EVAL_REASONS (add a fixture set under "
        "tests/fixtures/agent_evals/ or write why):\n" + "\n".join(missing)
    )


def test_no_eval_reasons_only_name_agents_without_one() -> None:
    specs = _agent_specs()
    covered = _agents_with_evals(specs)
    stale = sorted(name for name in NO_EVAL_REASONS if name not in specs or name in covered)
    assert not stale, "drop these from NO_EVAL_REASONS (deleted or now evaluated):\n" + "\n".join(
        stale
    )
    assert all(reason.strip() for reason in NO_EVAL_REASONS.values())
