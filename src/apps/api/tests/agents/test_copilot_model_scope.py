"""KRI-1384: EDIT_COPILOT_MODEL moves the chat turn and no other caller of the copilot's model.

The settings are read at import, so each case runs the real entrypoints in a fresh interpreter
(docs/reviews/kri-1384/scripts/copilot_model_scope.py) and reads what each one sent to its model
client. Ways this breaks, each caught below:

1. The slide-post chat edit or the prepared slide executor follows EDIT_COPILOT_MODEL and asks
   Sonnet for plain JSON (Sonnet returned invalid copilot JSON in the October pilot).
2. Creation text composition, which inherits the copilot's spec, follows it.
3. The research writers and reviewer follow it.
4. EDIT_COPILOT_THINKING_LEVEL leaks into any of those.
5. The chat turn stops following the setting, runs Sonnet without tool calls, or loses its
   Gemini fallback.
6. The proposal receipt records a model the chat turn did not run.
7. A JSON-mode caller is moved to a non-Google model in code.

Byte equality of every spec and request with the defaults, against origin/main:
docs/reviews/kri-1384/scripts/compare_copilot_model_scope.sh.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.model_registry import provider_of

_API = Path(__file__).resolve().parents[2]
_PROBE = _API.parents[2] / "docs" / "reviews" / "kri-1384" / "scripts" / "copilot_model_scope.py"
_SONNET_TOOLS = {
    "EDIT_COPILOT_MODEL": "claude-sonnet-5-5",
    "EDIT_COPILOT_THINKING_LEVEL": "medium",
    "EDIT_COPILOT_OUTPUT_MODE": "tools",
}
_DEFAULTS = {
    "EDIT_COPILOT_MODEL": "gemini-3.1-pro-preview",
    "EDIT_COPILOT_THINKING_LEVEL": "high",
    "EDIT_COPILOT_OUTPUT_MODE": "json",
}
_JSON_CALLERS = ("slide_post_chat_edit", "slide_post_prepared")


def _observe(tmp_path: Path, name: str, env: dict[str, str]) -> dict:
    out = tmp_path / f"{name}.json"
    result = subprocess.run(
        [sys.executable, str(_PROBE), str(out)],
        cwd=_API,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-4000:]
    return json.loads(out.read_text())


@pytest.fixture(scope="module")
def observed(tmp_path_factory) -> tuple[dict, dict]:
    tmp_path = tmp_path_factory.mktemp("copilot-model-scope")
    return _observe(tmp_path, "defaults", _DEFAULTS), _observe(tmp_path, "flip", _SONNET_TOOLS)


def test_the_chat_turn_runs_sonnet_with_tool_calls_and_falls_back_to_gemini_tools(observed):
    _, flip = observed
    chat = flip["chat_turn"]
    assert chat["first_call"]["model"] == "claude-sonnet-5-5"
    assert chat["first_call"]["thinking_level"] == "medium"
    assert chat["first_call"]["response_json"] is False
    assert "reply" in chat["first_call"]["tools"]
    assert chat["calls"] == [
        {"model": "claude-sonnet-5-5", "with_tools": True},
        {"model": "gemini-3.1-pro-preview", "with_tools": True},
    ]
    assert flip["proposal_receipt"]["model"] == "claude-sonnet-5-5"


def test_every_json_mode_caller_ignores_the_flip(observed):
    defaults, flip = observed
    for caller in _JSON_CALLERS:
        assert flip[caller] == defaults[caller], caller
        call = flip[caller]["first_call"]
        assert call["tools"] is None and call["response_json"] is True, caller
        assert provider_of(call["model"]) == "google", caller
    assert (
        flip["slide_post_prepared"]["manifest_spec"]["model"]
        == (flip["slide_post_prepared"]["first_call"]["model"])
    )
    assert flip["json_mode_specs"] == defaults["json_mode_specs"]
    for name, spec in flip["json_mode_specs"].items():
        assert provider_of(spec["model"]) == "google", name
        assert all(provider_of(model) == "google" for model in spec["fallback_models"]), name


def test_with_the_defaults_the_chat_turn_and_json_callers_share_one_gemini_json_copilot(observed):
    defaults, _ = observed
    chat = defaults["chat_turn"]["first_call"]
    slide = defaults["slide_post_chat_edit"]["first_call"]
    assert chat["tools"] is None and chat["response_json"] is True
    assert (chat["model"], chat["thinking_level"]) == (slide["model"], slide["thinking_level"])
    assert (chat["model"], chat["thinking_level"]) == ("gemini-3.1-pro-preview", "high")
    assert defaults["chat_turn"]["spec"] == defaults["json_mode_specs"]["EditCopilotAgent"]
