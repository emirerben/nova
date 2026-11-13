"""Old (merge base) vs new: price, estimate, provider and ladder over a model x usage grid.

Run from src/apps/api with the merge base's files exported into OLD_DIR:
  mkdir -p /tmp/kri1384-old
  git show origin/main:src/apps/api/app/services/ai_cost_control.py > /tmp/kri1384-old/old_ai_cost_control.py
  git show origin/main:src/apps/api/app/agents/_runtime.py > /tmp/kri1384-old/old_runtime_src.py
  git show origin/main:src/apps/api/app/agents/_text_providers.py > /tmp/kri1384-old/old_text_providers_src.py
  PYTHONPATH=. .venv/bin/python ../../../docs/reviews/kri-1384/scripts/registry_equivalence.py /tmp/kri1384-old
"""

import importlib.util
import itertools
import sys
from pathlib import Path

OLD = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("old_acc", OLD / "old_ai_cost_control.py")
old_acc = importlib.util.module_from_spec(spec)
sys.modules["old_acc"] = old_acc
spec.loader.exec_module(old_acc)

old_rt_src = (OLD / "old_runtime_src.py").read_text()
snippet = old_rt_src[
    old_rt_src.index("_DEGRADABLE_THINKING_LEVELS") : old_rt_src.index("class TerminalError")
]
old_rt = {}
exec(snippet, old_rt)
old_tp_src = (OLD / "old_text_providers_src.py").read_text()
tp = old_tp_src[old_tp_src.index("TEXT_MODELS =") : old_tp_src.index("def _preflight")]
old_tp = {}
exec(tp, old_tp)

import app.agents._text_providers as _new_tp
from app.agents import _runtime as new_rt

_new_tp.paid_provider = old_tp["paid_provider"]  # the old module's lazy import, old semantics
from app.agents._runtime import AiCostControlPolicyError
from app.agents.model_registry import provider_of
from app.services import ai_cost_control as new_acc

MODELS = [
    "gpt-6.1-sol",
    "claude-sonnet-5-5",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro",
    "gemini-3.6-flash",
    "gemini-3.7-flash-preview",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.9-ultra",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-image",
    "gemini-2.5-ultra",
    "gemini-omni-flash-preview",
    "gemini",
    "gemini-1.5-pro",
    "gemini-exp",
    "whisper-1",
    "gpt-4o",
    "image-model",
    "unknown-model",
]
DETAILS = [
    {},
    {
        "prompt_tokens_details": [
            {"modality": "AUDIO", "token_count": 4000},
            {"modality": "TEXT", "token_count": 900},
        ],
        "cache_tokens_details": [{"modality": "AUDIO", "token_count": 1500}],
    },
    {"candidates_tokens_details": [{"modality": "VIDEO", "token_count": 3000}]},
    {"cache_creation_input_tokens": 5000, "cache_creation_1h_input_tokens": 1200},
    {"grounded_search_requests": 3},
]
TOKENS = [
    (0, 0, 0, 0, 0),
    (1000, 200, 80, 100, 0),
    (250_000, 4000, 9000, 50_000, 10),
    (280_000, 16_384, 0, 0, 0),
    (199_999, 1, 1, 199_999, 0),
]


def outcome(fn):
    try:
        return ("ok", fn())
    except AiCostControlPolicyError as exc:
        return ("policy", exc.reason)


checked = 0
for model, (ti, to, tt, tc, tool), details in itertools.product(MODELS, TOKENS, DETAILS):
    usage_old = old_acc.UsageMeter(ti, to, tt, tc, tool, token_details=dict(details))
    usage_new = new_acc.UsageMeter(ti, to, tt, tc, tool, token_details=dict(details))
    a = outcome(lambda: old_acc.calculate_cost_usd(model, usage_old))
    b = outcome(lambda: new_acc.calculate_cost_usd(model, usage_new))
    assert a == b, (model, ti, to, details, a, b)
    checked += 1
for model in MODELS:
    for kwargs in (
        {"media_mime": None},
        {"media_mime": "video/mp4", "media_duration_s": 30},
        {"media_mime": "audio/wav", "media_duration_s": 60},
        {"media_mime": "image/jpeg", "media_width_px": 1920, "media_height_px": 1080},
        {"media_mime": "image/jpeg", "media_dims": [(1080, 1920), (640, 480)]},
    ):
        a = outcome(
            lambda: old_acc.estimate_call_cost_usd(
                model=model, prompt="x" * 30_000, max_output_tokens=16_384, **kwargs
            )
        )
        b = outcome(
            lambda: new_acc.estimate_call_cost_usd(
                model=model, prompt="x" * 30_000, max_output_tokens=16_384, **kwargs
            )
        )
        assert a == b, (model, kwargs, a, b)
        checked += 1
    old_provider = old_tp["paid_provider"](model)
    assert old_provider == provider_of(model), (model, old_provider, provider_of(model))
    checked += 1

ladder_cases = 0
for model in MODELS:
    for level in (
        None,
        "minimal",
        "low",
        "medium",
        "high",
        "HIGH",
        " high ",
        "xhigh",
        "max",
        "bogus",
    ):
        a = old_rt["_degraded_thinking_level"](model, level)
        b = new_rt._degraded_thinking_level(model, level)
        old_deadline = old_rt["_deadline_degraded_level"](model, level)
        new_deadline = new_rt._deadline_degraded_level(model, level)
        if model.lower().startswith("gemini"):
            assert a == b, ("truncation", model, level, a, b)
            # Gemini: a deadline still steps high -> medium; medium -> low is new and only
            # reachable once per model (the runtime's `deadline_stepped` fence).
            if old_deadline is not None:
                assert old_deadline == new_deadline, (
                    "deadline",
                    model,
                    level,
                    old_deadline,
                    new_deadline,
                )
        ladder_cases += 1

print(
    f"cost/estimate/provider checks: {checked} equal; ladder cases: {ladder_cases} (Gemini truncation identical, deadline steps preserved)"
)
print("intended differences (non-Gemini):")
for model in ("gpt-6.1-sol", "claude-sonnet-5-5"):
    for level in ("xhigh", "high", "medium", "low"):
        print(
            f"  {model} {level}: truncation {old_rt['_degraded_thinking_level'](model, level)} -> {new_rt._degraded_thinking_level(model, level)}, "
            f"deadline {old_rt['_deadline_degraded_level'](model, level)} -> {new_rt._deadline_degraded_level(model, level)}"
        )
for model in ("gpt-6.1-sol-2026-11-01", "claude-sonnet-5-5-20261101", "GEMINI-3.1-PRO-PREVIEW"):
    print(
        f"  {model}: provider {old_tp['paid_provider'](model)} -> {provider_of(model)}, price {outcome(lambda: old_acc.calculate_cost_usd(model, old_acc.UsageMeter(1000)))} -> {outcome(lambda: new_acc.calculate_cost_usd(model, new_acc.UsageMeter(1000)))}"
    )
