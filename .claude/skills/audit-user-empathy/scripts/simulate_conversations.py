#!/usr/bin/env python3
"""Mode B: drive the REAL InterviewerAgent and StyleIntentAgent against the persona bank.

For each persona:
  - InterviewerAgent: runs a full turn-by-turn interview. A persona-responder LLM
    (Claude Sonnet) answers each question in character so the interviewer generates
    real follow-ups. Every generated `question` is captured.
  - StyleIntentAgent: sends 4 vague style utterances from `style_probes`. Captures
    the `reply` on turns where `needs_clarification=True` (the clarifying questions
    the agent asks back), plus the greeting copy.

Run from src/apps/api with the shared test venv + GEMINI_API_KEY + ANTHROPIC_API_KEY
(both auto-loaded from repo-root .env):

    cd src/apps/api && /Users/.../.venv-test/bin/python \\
      ../../../.claude/skills/audit-user-empathy/scripts/simulate_conversations.py \\
      --personas <bank.json> --agents interviewer,style_intent \\
      --out /tmp/empathy-audit/conversations.json

Output shape:
  {"mode": "live", "gemini_model": "...", "model_matches_prod": bool,
   "personas": [
     {"persona_id": "...", "is_control": bool,
      "interviewer": {"questions": [...], "turns": [...]},
      "style_intent": {"probes": [{"utterance":..., "reply":..., "needs_clarification":bool}], ...},
      "error": null}
   ]}
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envload import load_dotenv  # noqa: E402

PROD_DEFAULT_MODEL = "gemini-2.5-flash"
RESPONDER_MODEL = "claude-sonnet-4-6"


def main() -> int:
    ap = argparse.ArgumentParser(description="Simulate interviews and style-agent conversations.")
    ap.add_argument("--personas", required=True, help="Path to target_personas.json bank.")
    ap.add_argument(
        "--agents",
        default="interviewer,style_intent",
        help="Comma-separated agents to run: interviewer, style_intent (default: both).",
    )
    ap.add_argument("--out", required=True, help="Where to write conversations.json.")
    ap.add_argument("--limit", type=int, default=0, help="Only first N personas (0 = all).")
    args = ap.parse_args()

    sys.path.insert(0, os.getcwd())
    load_dotenv()

    agents_to_run = {a.strip() for a in args.agents.split(",")}

    if not os.environ.get("GEMINI_API_KEY") and "interviewer" in agents_to_run:
        print("ERROR: GEMINI_API_KEY not set. InterviewerAgent needs real Gemini.", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set. Persona responder + style grader need it.",
            file=sys.stderr,
        )
        return 2

    try:
        import anthropic as _anthropic
    except ImportError:
        print("ERROR: anthropic SDK not installed in this python.", file=sys.stderr)
        return 2

    # Model-parity check.
    try:
        from app.config import settings as _settings

        active_model = getattr(_settings, "gemini_model", None) or PROD_DEFAULT_MODEL
    except Exception:  # noqa: BLE001
        active_model = os.environ.get("GEMINI_MODEL", PROD_DEFAULT_MODEL)
    print(
        f"active gemini model: {active_model} (prod default: {PROD_DEFAULT_MODEL})", file=sys.stderr
    )
    if active_model != PROD_DEFAULT_MODEL:
        print(
            f"WARNING: GEMINI_MODEL={active_model} ≠ prod default {PROD_DEFAULT_MODEL}. "
            f"Re-run with GEMINI_MODEL={PROD_DEFAULT_MODEL} for a prod-faithful read.",
            file=sys.stderr,
        )

    bank = json.loads(Path(args.personas).read_text())
    personas = bank["personas"] if isinstance(bank, dict) else bank
    style_probes = (
        bank.get("style_probes", {}).get("utterances", []) if isinstance(bank, dict) else []
    )
    if args.limit:
        personas = personas[: args.limit]

    from app.agents._model_client import default_client
    from app.agents._runtime import RunContext

    ctx = RunContext(extra={"skip_agent_run_persist": True, "skip_langfuse_trace": True})
    anth_client = _anthropic.Anthropic()

    results = []
    for i, p in enumerate(personas, 1):
        pid = p.get("id", f"persona-{i}")
        print(f"\n[{i}/{len(personas)}] simulating {pid} ...", file=sys.stderr)
        record: dict = {
            "persona_id": pid,
            "is_control": bool(p.get("is_control", False)),
            "persona": p,
            "error": None,
        }
        try:
            if "interviewer" in agents_to_run:
                record["interviewer"] = _run_interviewer(p, default_client(), ctx, anth_client)
            if "style_intent" in agents_to_run:
                record["style_intent"] = _run_style_intent(
                    p, style_probes, default_client(), ctx, anth_client
                )
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  FAILED: {record['error']}", file=sys.stderr)
        results.append(record)

    out = {
        "mode": "live",
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "gemini_model": active_model,
        "prod_default_model": PROD_DEFAULT_MODEL,
        "model_matches_prod": active_model == PROD_DEFAULT_MODEL,
        "personas": results,
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    n_qs = sum(
        len(r.get("interviewer", {}).get("questions", [])) for r in results if not r.get("error")
    )
    print(
        f"\nWrote {len(results)} personas, {n_qs} interviewer questions captured → {out_path}",
        file=sys.stderr,
    )
    return 0


# ── Interviewer simulation ────────────────────────────────────────────────────


def _persona_respond(anth_client, responder_desc: str, question: str, history: list[dict]) -> str:
    """Role-play the persona answering the interviewer's question.

    Returns a short, natural answer (1-3 sentences). The persona does NOT use
    marketing vocabulary — they answer from their real life.
    """
    history_text = ""
    if history:
        lines = []
        for t in history[-4:]:  # last 4 turns for context
            role = t.get("role", "").upper()
            lines.append(f"{role}: {t.get('content', '')}")
        history_text = "\nPrior exchange:\n" + "\n".join(lines) + "\n"

    system = (
        "You are role-playing as a real person described below. "
        "Answer naturally and briefly (1-3 sentences) as that person would. "
        "Do NOT use marketing jargon — no 'target audience', 'content pillars', 'niche', "
        "'tone of voice', 'brand'. Just answer from your real life, concretely. "
        "If the question makes you hesitate or feel judged, say so briefly"
        " and give your best answer.\n\n"
        f"Persona: {responder_desc}"
    )
    response = anth_client.messages.create(
        model=RESPONDER_MODEL,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": f"{history_text}INTERVIEWER: {question}\nYOU:"}],
    )
    return _extract_text(response).strip()


def _run_interviewer(persona: dict, gemini_client, ctx, anth_client) -> dict:
    """Run a full InterviewerAgent conversation and return all generated questions."""
    from app.agents.interviewer_agent import ConversationTurn, InterviewerAgent, InterviewerInput

    agent = InterviewerAgent(gemini_client)
    responder_desc = persona.get("responder_description", "You are an everyday creator.")
    pid = persona.get("id", "?")

    turns: list[ConversationTurn] = []
    questions: list[dict] = []
    n = 0

    while n < 8:
        inp = InterviewerInput(turns=turns, tiktok_summary=None, turn_count=n)
        try:
            out = agent.run(inp, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"  interviewer turn {n} error: {exc}", file=sys.stderr)
            break

        q = out.question
        questions.append(
            {
                "question_index": n,
                "question": q,
                "turn_label": out.turn_label,
                "is_final": out.is_final,
                "suggestions": out.suggestions,
            }
        )
        print(f"  [{pid}] interviewer turn {n}: {q[:80]}...", file=sys.stderr)

        if out.is_final:
            break

        # Persona responds.
        history_dicts = [{"role": t.role, "content": t.content} for t in turns]
        answer = _persona_respond(anth_client, responder_desc, q, history_dicts)
        print(f"  [{pid}] persona answer: {answer[:60]}...", file=sys.stderr)

        turns.append(ConversationTurn(role="agent", content=q))
        turns.append(ConversationTurn(role="user", content=answer))
        n += 1

    return {
        "questions": questions,
        "turns": [{"role": t.role, "content": t.content} for t in turns],
    }


# ── Style-intent simulation ───────────────────────────────────────────────────


def _run_style_intent(
    persona: dict, utterances: list[str], gemini_client, ctx, anth_client
) -> dict:  # noqa: ARG001
    """Send vague style utterances to StyleIntentAgent; capture clarifying replies."""
    from app.agents.style_intent import StyleIntentAgent, StyleIntentInput

    agent = StyleIntentAgent(gemini_client)
    pid = persona.get("id", "?")

    if not utterances:
        utterances = [
            "I want it to look more aesthetic",
            "make it feel warmer somehow",
            "something more minimal maybe?",
            "I don't know, just make it nicer",
        ]

    probes: list[dict] = []
    prior_turns: list[dict] = []

    for utt in utterances:
        inp = StyleIntentInput(
            utterance=utt,
            prior_turns=prior_turns,
            current_style_snapshot=None,
        )
        try:
            out = agent.run(inp, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{pid}] style_intent error on '{utt[:40]}': {exc}", file=sys.stderr)
            probes.append(
                {
                    "utterance": utt,
                    "reply": None,
                    "needs_clarification": None,
                    "intent": None,
                    "error": str(exc),
                }
            )
            continue

        probe = {
            "utterance": utt,
            "reply": out.reply,
            "needs_clarification": out.needs_clarification,
            "intent": out.intent,
            "suggestions": out.suggestions,
        }
        probes.append(probe)
        label = "CLARIFY" if out.needs_clarification else out.intent
        print(f"  [{pid}] style '{utt[:30]}' → {label}: {out.reply[:60]}...", file=sys.stderr)

        # Update prior_turns for next probe so the agent sees context.
        prior_turns.append({"role": "user", "content": utt})
        prior_turns.append({"role": "agent", "content": out.reply})

    return {"probes": probes}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_text(response) -> str:
    content = getattr(response, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return text
        if isinstance(block, dict) and block.get("text"):
            return block["text"]
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
