"""English <-> Turkish parity for the MODELS that turn a chat message into a decision.

KRI-773, phase 2, layer 2. Layer 1 (``tests/parity/test_decision_parity.py``) measures the
free rule-based decision points. This layer asks whether the real agents (real prompt
rendering, real ``parse``) reach the same structured decision for an English creator
message and its natural Turkish twin.

Pairs live in ``tests/fixtures/parity_pairs/pairs.json``. Each pair names the agent whose
output carries the decision, a base input (a realistic ``agent_evals`` fixture input), the
two messages, the output fields to compare (``compare``) and the intended decision
(``expect``). Everything but the message is identical across the two languages.

Modes (same flags as the rest of ``tests/evals``):

  replay (default)  Reads recorded model answers from ``parity_pairs/cassettes/`` and
                    re-parses them through the real agent. Free, offline, deterministic,
                    CI-safe. A pair without both cassettes is skipped.
  live              ``--eval-mode=live`` plus the mandatory paid-run flags. Calls the real
                    model only for languages that have no cassette yet, records every
                    successful answer as a cassette, and replays the rest. Each call is
                    reserved and settled in the AI cost ledger under ``--test-run-id``,
                    which hard-caps the run at ``--max-cost-usd`` (<= $2).

Mismatches are reported, never failed: this is a baseline. Parse/schema errors fail.

``test_parity_wiring_dry`` is always free: it validates every pair's input, renders every
prompt, prices the per-call ledger reservation, and parses a canned answer through each
agent and the comparison code, with no network.

Every run writes ``test-results/parity-model.json`` (relative to ``src/apps/api``) from
the cassettes on disk: per-pair decisions, field-level diffs, intended-decision hits, and
parity % overall, per agent and per capability.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ..runners.eval_runner import (
    CassetteModelClient,
    RecordingModelClient,
    build_eval_run_context,
)

PAIRS_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "parity_pairs"
PAIRS_PATH = PAIRS_ROOT / "pairs.json"
CASSETTE_ROOT = PAIRS_ROOT / "cassettes"
AGENT_EVALS_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "agent_evals"
REPORT_PATH = Path("test-results") / "parity-model.json"
LANGS = ("en", "tr")

PAIRS_DOC: dict[str, Any] = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
PAIRS: list[dict[str, Any]] = PAIRS_DOC["pairs"]
AGENTS: dict[str, dict[str, Any]] = PAIRS_DOC["agents"]
BASES: dict[str, dict[str, Any]] = PAIRS_DOC.get("bases", {})

# Canned answers for the free dry run: they exercise parse + projection, not the model.
_DRY_RAW: dict[str, str] = {
    "brief_extractor": json.dumps(
        {
            "brief_updates": [
                {
                    "operation": "add",
                    "kind": "timing",
                    "scope": "global",
                    "literal": None,
                    "description": "dry run",
                    "facts": {"duration_s": 30},
                }
            ],
            "request_scope": "edit",
        }
    ),
    "clip_intent_planner": json.dumps({"intents": [], "question": None}),
    "creator_memory_extractor": json.dumps(
        {
            "operation": "activate_explicit",
            "instruction": "Always use the Inter font",
            "category": "video_style",
            "enforcement": "constraint",
            "normalized_key": "font_family",
            "structured_value": {"font_family": "Inter"},
            "target_item_id": None,
            "confidence": 0.95,
            "reason_code": "explicit_durable",
        }
    ),
    "slide_post_composer": json.dumps(
        {
            "order": ["a1", "a2", "a3", "a4", "a5"],
            "cover_id": "a5",
            "caption": "dry",
            "alt_text": {},
        }
    ),
}


# ── input building ───────────────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _base_fixture(pair: dict[str, Any]) -> dict[str, Any] | None:
    rel = pair.get("base_fixture")
    if not rel:
        return None
    return json.loads((AGENT_EVALS_ROOT / rel).read_text(encoding="utf-8"))


def build_input(pair: dict[str, Any], lang: str) -> dict[str, Any]:
    """The agent input for one language: base + overrides + the message (+ reply_language)."""
    from app.kria.reply_language import detect_chat_language

    fixture = _base_fixture(pair)
    data: dict[str, Any] = copy.deepcopy(fixture["input"]) if fixture else {}
    if pair.get("base"):
        data = _deep_merge(data, BASES[pair["base"]])
    data = _deep_merge(data, pair.get("base_input") or {})
    message = pair[lang]
    spec = AGENTS[pair["agent"]]
    for field in spec["message_fields"]:
        data[field] = message
    if spec.get("reply_language"):
        data.pop("reply_language", None)
        if detect_chat_language(message) == "tr":
            data["reply_language"] = "tr"
    return data


def agent_class(agent_key: str) -> type:
    module_name, cls_name = AGENTS[agent_key]["class"].split(":")
    return getattr(importlib.import_module(module_name), cls_name)


# ── projection: output -> the compared decision ──────────────────────────────

_CMP_RE = re.compile(r"^(?P<path>.+?)(?P<op>>=|<=|>|<)(?P<num>-?\d+(?:\.\d+)?)$")


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def probe(obj: Any, expr: str) -> Any:
    """Evaluate one compare expression against a dumped output.

    ``a.b`` value | ``a.b|cf`` casefolded string | ``a.b>1`` numeric test |
    ``a.b?`` truthiness | ``has:a.b`` present | ``any:a,b`` any present |
    ``first:a,b`` first present value | ``len:a`` / ``count:a`` list length.
    """
    if expr.startswith(("len:", "count:")):
        value = _get(obj, expr.split(":", 1)[1])
        return len(value) if isinstance(value, list) else (0 if expr.startswith("count:") else None)
    if expr.startswith("has:"):
        return _present(_get(obj, expr[4:]))
    if expr.startswith("any:"):
        return any(_present(_get(obj, p)) for p in expr[4:].split(","))
    if expr.startswith("first:"):
        for path in expr[6:].split(","):
            value = _get(obj, path)
            if _present(value):
                return value.casefold() if isinstance(value, str) else value
        return None
    if expr.endswith("?"):
        return _present(_get(obj, expr[:-1]))
    if expr.endswith("|cf"):
        value = _get(obj, expr[:-3])
        return value.strip().casefold() if isinstance(value, str) else value
    match = _CMP_RE.match(expr)
    if match:
        value = _get(obj, match["path"])
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        num = float(match["num"])
        return {">": value > num, "<": value < num, ">=": value >= num, "<=": value <= num}[
            match["op"]
        ]
    return _get(obj, expr)


def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))


def _brief_rows(dump: dict[str, Any], compare: dict[str, Any]) -> list[dict[str, Any]]:
    kinds = compare.get("kinds")
    rows = []
    for update in dump.get("brief_updates") or []:
        if kinds is not None and update.get("kind") not in kinds:
            continue
        row = copy.deepcopy(update)
        facts = dict(row.get("facts") or {})
        intent = facts.get("style_intent")
        if isinstance(intent, dict):
            for item in intent.get("set") or []:
                if isinstance(item, dict) and item.get("field"):
                    facts[f"si_{item['field']}"] = item.get("value")
            if intent.get("target"):
                facts["si_target"] = intent["target"]
        row["facts"] = facts
        scope = str(row.get("scope") or "")
        row["scope"] = "clip" if scope.startswith("clip:") else scope
        rows.append({expr: probe(row, expr) for expr in compare["req"]})
    return _sorted_rows(rows)


def project(agent_key: str, dump: dict[str, Any], compare: dict[str, Any]) -> dict[str, Any]:
    """The normalized decision a pair compares across languages."""
    decision = {expr: probe(dump, expr) for expr in compare.get("probes", [])}
    if "req" in compare:
        decision["reqs"] = _brief_rows(dump, compare)
    if "ops" in compare:
        fields = compare["ops"]
        decision["ops"] = _sorted_rows(
            [
                {"op": op.get("op"), **{e: probe(op, e) for e in fields.get(op.get("op"), [])}}
                for op in dump.get("ops") or []
                if isinstance(op, dict)
            ]
        )
    if "intents" in compare:
        decision["intents"] = _sorted_rows(
            [
                {e: probe(row, e) for e in compare["intents"]}
                for row in dump.get("intents") or []
                if isinstance(row, dict)
            ]
        )
    return decision


def meets_expect(decision: dict[str, Any], expect: dict[str, Any]) -> bool:
    """Every expected key matches; a list of rows needs each row to be a subset of one row."""
    for key, want in expect.items():
        got = decision.get(key)
        if isinstance(want, list) and want and isinstance(want[0], dict):
            rows = got if isinstance(got, list) else []
            for item in want:
                if not any(all(row.get(k) == v for k, v in item.items()) for row in rows):
                    return False
        elif got != want:
            return False
    return True


def field_diff(en: dict[str, Any], tr: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {"en": en.get(key), "tr": tr.get(key)}
        for key in sorted(set(en) | set(tr))
        if en.get(key) != tr.get(key)
    }


# ── cassettes ────────────────────────────────────────────────────────────────


def cassette_path(pair_id: str, lang: str) -> Path:
    return CASSETTE_ROOT / f"{pair_id}.{lang}.json"


def load_cassette(pair_id: str, lang: str) -> dict[str, Any] | None:
    path = cassette_path(pair_id, lang)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if data.get("raw_text") else None


def _prompt_sha(agent: Any, input_dict: dict[str, Any]) -> str:
    prompt = agent.render_prompt(agent.Input.model_validate(input_dict))
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _run_agent(agent: Any, input_dict: dict[str, Any], ctx_id: str, *, live: bool) -> Any:
    return agent.run(input_dict, ctx=build_eval_run_context(ctx_id, is_live=live))


def _replay_run(agent: Any, input_dict: dict[str, Any], ctx_id: str) -> Any:
    """Replay never contacts a provider, so the paid-call ledger is off for it even
    inside a live session (a replay context carries no paid-run attribution)."""
    from app.config import settings

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "ai_cost_control_enabled", False)
        return _run_agent(agent, input_dict, ctx_id, live=False)


def replay_lang(pair: dict[str, Any], lang: str) -> dict[str, Any] | None:
    """Re-parse a recorded answer through the real agent; None when no cassette."""
    cassette = load_cassette(pair["id"], lang)
    if cassette is None:
        return None
    cls = agent_class(pair["agent"])
    input_dict = build_input(pair, lang)
    agent = cls(CassetteModelClient(cassette["raw_text"]))
    output = _replay_run(agent, input_dict, f"parity/{pair['id']}/{lang}")
    dump = output.model_dump(mode="json")
    return {
        "decision": project(pair["agent"], dump, pair["compare"]),
        "model_used": cassette.get("model_used"),
        "cost_usd": cassette.get("computed_cost_usd"),
        "prompt_drift": cassette.get("prompt_sha256") not in (None, _prompt_sha(agent, input_dict)),
    }


def _invocation_cost(invocation: Any, model: str) -> float | None:
    try:
        from app.services.ai_cost_control import UsageMeter, calculate_cost_usd

        return calculate_cost_usd(
            invocation.model_used or model,
            UsageMeter(
                tokens_in=invocation.tokens_in,
                tokens_out=invocation.tokens_out,
                tokens_thoughts=invocation.tokens_thoughts,
                tokens_cached=invocation.tokens_cached,
                tokens_tool=invocation.tokens_tool,
                token_details=invocation.token_details,
            ),
        )
    except Exception:  # noqa: BLE001 — cost bookkeeping never breaks a run
        return None


def _reservation_estimate(agent: Any, input_dict: dict[str, Any]) -> float:
    from app.services.ai_cost_control import effective_max_output_tokens, estimate_call_cost_usd

    prompt = agent.render_prompt(agent.Input.model_validate(input_dict))
    return estimate_call_cost_usd(
        model=agent.spec.model,
        prompt=prompt,
        max_output_tokens=effective_max_output_tokens(agent.max_output_tokens),
        media_mime=None,
    )


def record_live(pair: dict[str, Any], lang: str, client: Any) -> None:
    """One paid call; writes a cassette on success, a diagnostic file on failure."""
    from app.agents._runtime import (
        AiBudgetExceededError,
        AiCostControlPolicyError,
        RefusalError,
        SchemaError,
        TerminalSchemaError,
    )

    cls = agent_class(pair["agent"])
    input_dict = build_input(pair, lang)
    recorder = RecordingModelClient(client)
    agent = cls(recorder)
    cap = float(os.environ.get("NOVA_EVAL_MAX_COST_USD") or 0)
    reservation = _reservation_estimate(agent, input_dict)
    if reservation > cap:
        pytest.skip(
            f"{pair['id']}/{lang}: one call reserves ${reservation:.3f} in the ledger, "
            f"above the approved run cap ${cap:.2f}"
        )
    base = {
        "pair_id": pair["id"],
        "lang": lang,
        "agent": cls.spec.name,
        "prompt_version": cls.spec.prompt_version,
        "message": pair[lang],
        "input": input_dict,
        "prompt_sha256": _prompt_sha(agent, input_dict),
        "test_run_id": os.environ.get("NOVA_EVAL_TEST_RUN_ID"),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    CASSETTE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        _run_agent(agent, input_dict, f"parity/{pair['id']}/{lang}", live=True)
    except (AiBudgetExceededError, AiCostControlPolicyError) as exc:
        pytest.skip(f"{pair['id']}/{lang}: ledger refused the call ({exc})")
    except (SchemaError, TerminalSchemaError, RefusalError) as exc:
        failed = cassette_path(pair["id"], lang).with_suffix(".failed.json")
        failed.write_text(
            json.dumps(
                {
                    **base,
                    "error": f"{type(exc).__name__}: {exc}",
                    "raw_texts": [inv.raw_text for inv in recorder.invocations],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        pytest.fail(f"{pair['id']}/{lang}: parse/schema error: {exc}")
    invocations = recorder.invocations
    last = invocations[-1]
    costs = [_invocation_cost(inv, cls.spec.model) for inv in invocations]
    cassette = {
        **base,
        "raw_text": last.raw_text,
        "raw_texts": [inv.raw_text for inv in invocations],
        "model_used": last.model_used or cls.spec.model,
        "tokens": {
            "in": sum(inv.tokens_in for inv in invocations),
            "out": sum(inv.tokens_out for inv in invocations),
            "thoughts": sum(inv.tokens_thoughts for inv in invocations),
        },
        "computed_cost_usd": round(sum(c for c in costs if c is not None), 6),
    }
    cassette_path(pair["id"], lang).write_text(
        json.dumps(cassette, indent=2, ensure_ascii=False) + "\n"
    )


# ── report ───────────────────────────────────────────────────────────────────


def _ledger(test_run_id: str | None) -> dict[str, Any] | None:
    if not test_run_id:
        return None
    try:
        from sqlalchemy import text

        from app.database import sync_engine

        with sync_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT feature, status, COUNT(*) AS n, "
                    "COALESCE(SUM(settled_cost_usd), 0) AS settled, "
                    "COALESCE(SUM(estimated_cost_usd), 0) AS reserved "
                    "FROM ai_cost_reservations WHERE test_run_id = :id GROUP BY 1, 2"
                ),
                {"id": test_run_id},
            ).mappings()
            by = [
                {
                    "feature": r["feature"],
                    "status": r["status"],
                    "calls": int(r["n"]),
                    "settled_usd": float(r["settled"]),
                    "reserved_usd": float(r["reserved"]),
                }
                for r in rows
            ]
    except Exception as exc:  # noqa: BLE001 — report must still be written
        return {"test_run_id": test_run_id, "error": str(exc)}
    return {
        "test_run_id": test_run_id,
        "settled_usd": round(sum(r["settled_usd"] for r in by if r["status"] == "settled"), 6),
        "rows": by,
    }


def _pct(hits: int, total: int) -> float | None:
    return round(100.0 * hits / total, 1) if total else None


def build_report(mode: str) -> dict[str, Any]:
    from app.services.creator_memory_learning import classify_memory_candidate

    results = []
    for pair in PAIRS:
        entry: dict[str, Any] = {
            "id": pair["id"],
            "capability": pair["capability"],
            "agent": pair["agent"],
            "expect": pair["expect"],
        }
        langs: dict[str, Any] = {}
        for lang in LANGS:
            try:
                replayed = replay_lang(pair, lang)
            except Exception as exc:  # noqa: BLE001 — surfaced as status=error
                replayed = {"error": f"{type(exc).__name__}: {exc}"}
            if replayed is None:
                continue
            if "decision" in replayed:
                replayed["intended"] = meets_expect(replayed["decision"], pair["expect"])
            langs[lang] = {"message": pair[lang], **replayed}
        entry.update(langs)
        if pair["agent"] == "creator_memory_extractor":
            entry["gate"] = {lang: classify_memory_candidate(pair[lang]) for lang in LANGS}
        if any("error" in langs.get(lang, {}) for lang in LANGS):
            entry["status"] = "error"
        elif all(lang in langs for lang in LANGS):
            diff = field_diff(langs["en"]["decision"], langs["tr"]["decision"])
            entry["status"] = "match" if not diff else "mismatch"
            entry["diff"] = diff
            entry["both_missed_intent"] = (
                not langs["en"]["intended"] and not langs["tr"]["intended"]
            )
        else:
            entry["status"] = "missing_cassette"
        results.append(entry)

    scored = [r for r in results if r["status"] in {"match", "mismatch"}]

    def _group(key: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in scored:
            groups[row[key]].append(row)
        return {
            name: {
                "pairs": len(rows),
                "match": sum(r["status"] == "match" for r in rows),
                "parity_pct": _pct(sum(r["status"] == "match" for r in rows), len(rows)),
                "en_intended_pct": _pct(sum(r["en"]["intended"] for r in rows), len(rows)),
                "tr_intended_pct": _pct(sum(r["tr"]["intended"] for r in rows), len(rows)),
            }
            for name, rows in sorted(groups.items())
        }

    costs = [r[lang].get("cost_usd") or 0.0 for r in results for lang in LANGS if lang in r]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "pairs_total": len(PAIRS),
        "pairs_scored": len(scored),
        "pairs_missing_cassette": sum(r["status"] == "missing_cassette" for r in results),
        "pairs_error": sum(r["status"] == "error" for r in results),
        "parity_pct": _pct(sum(r["status"] == "match" for r in scored), len(scored)),
        "en_intended_pct": _pct(sum(r["en"]["intended"] for r in scored), len(scored)),
        "tr_intended_pct": _pct(sum(r["tr"]["intended"] for r in scored), len(scored)),
        "both_missed_intent": [r["id"] for r in scored if r["both_missed_intent"]],
        "recorded_cost_usd": round(sum(costs), 6),
        "ledger": _ledger(os.environ.get("NOVA_EVAL_TEST_RUN_ID")) if mode == "live" else None,
        "by_agent": _group("agent"),
        "by_capability": _group("capability"),
        "results": results,
    }


@pytest.fixture(scope="module", autouse=True)
def _write_report(eval_mode: str):
    yield
    from tests._prod_profile import apply_prod_profile

    with pytest.MonkeyPatch.context() as mp:
        apply_prod_profile(mp)
        report = build_report(eval_mode)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        f"\nparity-model: {report['parity_pct']}% parity over {report['pairs_scored']} pairs "
        f"(missing {report['pairs_missing_cassette']}, error {report['pairs_error']}) "
        f"-> {REPORT_PATH}"
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_parity_wiring_dry(prod_profile) -> None:  # noqa: ARG001
    """Free: every input validates, every prompt renders, parse + projection run."""
    problems: list[str] = []
    reservations: dict[str, float] = defaultdict(float)
    ids = [pair["id"] for pair in PAIRS]
    assert len(ids) == len(set(ids)), "duplicate pair ids"
    for pair in PAIRS:
        cls = agent_class(pair["agent"])
        fixture = _base_fixture(pair)
        raw = fixture["raw_text"] if fixture else _DRY_RAW[pair["agent"]]
        for lang in LANGS:
            try:
                input_dict = build_input(pair, lang)
                agent = cls(CassetteModelClient(raw))
                assert pair[lang] in agent.render_prompt(agent.Input.model_validate(input_dict))
                reservations[pair["agent"]] += _reservation_estimate(agent, input_dict)
                output = _replay_run(agent, input_dict, f"parity-dry/{pair['id']}/{lang}")
                decision = project(pair["agent"], output.model_dump(mode="json"), pair["compare"])
                assert isinstance(decision, dict) and decision
                meets_expect(decision, pair["expect"])
            except Exception as exc:  # noqa: BLE001 — collect every broken pair
                problems.append(f"{pair['id']}/{lang}: {type(exc).__name__}: {exc}")
    print("\nper-agent ledger reservation sum (pessimistic, all pairs x 2 languages):")
    for name, total in sorted(reservations.items()):
        print(f"  {name}: ${total:.3f}")
    assert not problems, "\n".join(problems)


@pytest.mark.timeout(900)
@pytest.mark.parametrize("pair", PAIRS, ids=[pair["id"] for pair in PAIRS])
def test_model_parity(
    pair: dict[str, Any], eval_mode: str, live_model_client, prod_profile
) -> None:  # noqa: ARG001
    """Run both languages (live for missing cassettes), then report; never fail a mismatch."""
    if eval_mode == "live":
        for lang in LANGS:
            if load_cassette(pair["id"], lang) is None or os.environ.get("PARITY_RERECORD") == "1":
                record_live(pair, lang, live_model_client)
    decisions = {}
    for lang in LANGS:
        replayed = replay_lang(pair, lang)
        if replayed is None:
            pytest.skip(f"{pair['id']}: no {lang} cassette (record with --eval-mode=live)")
        decisions[lang] = replayed["decision"]
    diff = field_diff(decisions["en"], decisions["tr"])
    print(
        f"\n{pair['id']} [{pair['capability']}] "
        + ("MATCH" if not diff else f"MISMATCH {json.dumps(diff, ensure_ascii=False)}")
    )
