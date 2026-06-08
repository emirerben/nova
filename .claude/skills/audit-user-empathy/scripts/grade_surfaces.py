#!/usr/bin/env python3
"""Grade user-facing questions/copy against the user-empathy rubric with Claude Sonnet.

Mirrors grade_plan_items.py: Claude (different family from Gemini, the agent under test)
gives an independent quality signal; the rubric is sent as a cached system block. One
call per surface group (a persona's interview = one group; static catalog = one group) so
the judge can detect redundancy within a conversation.

Stdlib + `anthropic` only — runs with any python that has the SDK (shared test venv).
Needs ANTHROPIC_API_KEY (auto-loaded from repo-root .env).

    python grade_surfaces.py \\
      --inputs /tmp/empathy-audit/conversations.json /tmp/empathy-audit/surfaces.json \\
      --out /tmp/empathy-audit/graded.json \\
      --report /tmp/empathy-audit/report.md

Per-surface verdict: {surface_id, question, score (1-5), flags: [...], reason, flagged}.
Writes graded.json + a report.md skeleton with the quantitative half filled in (you fill
the two <!-- FILL --> sections: Root cause and Proposed edits).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envload import load_dotenv  # noqa: E402

JUDGE_MODEL = "claude-sonnet-4-6"
FLAG_SCORE_THRESHOLD = 4  # score < 4 OR any flag => flagged surface


_SYSTEM_INSTRUCTION = (
    "You are a strict but fair user-empathy judge for Nova's conversational AI. "
    "Nova's user is a real everyday creator — a café owner, student, solo traveler — "
    "NOT a marketer. They should never have to think about who their target audience is, "
    "know content-strategy jargon, or answer questions that the product should answer for them. "
    "Score each question/copy string against the rubric exactly as written. "
    "You may also catch redundancy within a conversation — if the same information was already "
    "established, asking again is the 'redundant' flag. "
    "Return ONLY a JSON object of the form "
    '{"surfaces": [{"surface_id": "<str>", "score": <int 1-5>, '
    '"flags": ["<flag>", ...], "reason": "<one sentence>"}]}. '
    "flags must be drawn ONLY from the rubric's failure-flag names. No prose outside the JSON."
)


def _extract_text(response) -> str:
    content = getattr(response, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return text
        if isinstance(block, dict) and block.get("text"):
            return block["text"]
    return ""


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in judge response: {raw[:200]!r}")
    return json.loads(match.group(0))


def _grade_group(client, rubric: str, surfaces: list[dict], context: str) -> list[dict]:
    """Grade a group of surfaces in one call. Returns graded surface records."""
    if not surfaces:
        return []
    user_text = (
        f"Context (who this AI is talking to / which surface these come from):\n{context}\n\n"
        "Questions/copy strings to grade (grade each, and flag redundancy within this list):\n"
        + json.dumps(
            [{"surface_id": s["surface_id"], "question": s["question"]} for s in surfaces],
            indent=2,
            ensure_ascii=False,
        )
    )
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=3000,
        system=[
            {"type": "text", "text": _SYSTEM_INSTRUCTION, "cache_control": {"type": "ephemeral"}},
            {
                "type": "text",
                "text": f"Rubric:\n\n{rubric}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_text}],
    )
    data = _parse_json(_extract_text(response))
    verdicts = data.get("surfaces", [])
    by_id = {v.get("surface_id"): v for v in verdicts}

    graded = []
    for s in surfaces:
        v = by_id.get(s["surface_id"], {})
        score = v.get("score")
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = None
        flags = [str(f) for f in v.get("flags", []) if f]
        graded.append(
            {
                "surface_id": s["surface_id"],
                "question": s["question"],
                "source": s.get("source", ""),
                "context": s.get("context", ""),
                "persona_id": s.get("persona_id"),
                "is_control": s.get("is_control"),
                "score": score,
                "flags": flags,
                "reason": str(v.get("reason", "")),
                "flagged": (score is not None and score < FLAG_SCORE_THRESHOLD) or bool(flags),
            }
        )
    return graded


# ── Input loaders ─────────────────────────────────────────────────────────────


def _load_live_groups(path: Path) -> list[dict]:
    """Extract grading groups from conversations.json (Mode B output).

    One group per persona: interviewer questions + style clarifying replies.
    Returns list of {"group_id", "is_control", "context", "surfaces": [...]}.
    """
    data = json.loads(path.read_text())
    personas = data.get("personas", [])
    groups = []
    for p in personas:
        pid = p.get("persona_id", "?")
        is_control = bool(p.get("is_control", False))
        persona_desc = p.get("persona", {}).get("responder_description", "")
        surfaces: list[dict] = []

        # Interviewer questions.
        for q_rec in p.get("interviewer", {}).get("questions", []):
            if q_rec.get("question"):
                surfaces.append(
                    {
                        "surface_id": (
                            f"{pid}__interviewer_q{q_rec.get('question_index', len(surfaces))}"
                        ),
                        "question": q_rec["question"],
                        "source": "InterviewerAgent (live)",
                        "context": f"Interviewer — {q_rec.get('turn_label', '')}, persona: {pid}",
                        "persona_id": pid,
                        "is_control": is_control,
                    }
                )

        # Style-intent clarifying replies (needs_clarification=True).
        for probe in p.get("style_intent", {}).get("probes", []):
            if probe.get("needs_clarification") and probe.get("reply"):
                surfaces.append(
                    {
                        "surface_id": f"{pid}__style_clarify_{len(surfaces)}",
                        "question": probe["reply"],
                        "source": "StyleIntentAgent clarify reply (live)",
                        "context": (
                            f"StyleIntentAgent clarify in response to "
                            f"'{probe.get('utterance', '')}', persona: {pid}"
                        ),
                        "persona_id": pid,
                        "is_control": is_control,
                    }
                )

        if surfaces:
            groups.append(
                {
                    "group_id": f"live_{pid}",
                    "is_control": is_control,
                    "context": f"Persona: {pid} — {persona_desc}",
                    "surfaces": surfaces,
                }
            )
    return groups


def _load_static_groups(path: Path) -> list[dict]:
    """Load static surfaces.json (Mode A output) as one grading group."""
    data = json.loads(path.read_text())
    surfaces = data.get("surfaces", [])
    # Skip the informational banned-list entry.
    real = [s for s in surfaces if not s.get("question", "").startswith("(informational")]
    if not real:
        return []
    for s in real:
        s.setdefault("persona_id", None)
        s.setdefault("is_control", None)
    return [
        {
            "group_id": "static_catalog",
            "is_control": None,
            "context": (
                "Static questions/copy extracted from prompt files and UI components (Mode A)."
            ),
            "surfaces": real,
        }
    ]


# ── Report renderer ───────────────────────────────────────────────────────────


def _pct(x) -> str:
    return f"{round(x * 100, 1)}%" if isinstance(x, (int, float)) else "—"


def _offenders(graded_groups: list[dict], mode_name: str, *, limit: int = 3) -> list[dict]:
    hits = []
    for g in graded_groups:
        for it in g.get("items", []):
            if mode_name in it.get("flags", []):
                hits.append(
                    {
                        "persona": it.get("persona_id") or g.get("group_id", "?"),
                        "source": it.get("source", ""),
                        "question": (it.get("question") or "").replace("\n", " ").strip(),
                        "score": it.get("score") if it.get("score") is not None else 99,
                    }
                )
    hits.sort(key=lambda h: h["score"])
    return hits[:limit]


def _render_report(summary: dict, graded_groups: list[dict], meta: dict) -> str:
    mode_label = meta.get("mode_label", "mixed")
    n_surfaces = summary.get("surfaces_graded", 0)
    live_n = summary.get("live_surfaces", 0)
    static_n = summary.get("static_surfaces", 0)
    flag_rate = summary.get("flag_rate")
    mean = summary.get("mean_score")
    flag_counts = summary.get("flag_counts", {})
    flag_rates = summary.get("flag_rates", {})
    top_flag = next(iter(flag_counts), None)

    # Everyday vs control split.
    everyday_items, everyday_flagged, everyday_scores = 0, 0, []
    control_items, control_flagged, control_scores = 0, 0, []
    for g in graded_groups:
        is_c = g.get("is_control")
        for it in g.get("items", []):
            score = it.get("score")
            flagged = it.get("flagged", False)
            if is_c is True:
                control_items += 1
                if score is not None:
                    control_scores.append(score)
                if flagged:
                    control_flagged += 1
            elif is_c is False:
                everyday_items += 1
                if score is not None:
                    everyday_scores.append(score)
                if flagged:
                    everyday_flagged += 1

    L = []
    L.append(f"# User-empathy audit — {mode_label} — (fill date)\n")

    model = meta.get("gemini_model")
    if model and not meta.get("model_matches_prod", True):
        L.append(
            f"> ⚠️ Generated on **{model}**, but prod runs **{meta.get('prod_default_model')}**. "
            f"These grades likely UNDER-estimate what real users experience — re-run with "
            f"`GEMINI_MODEL={meta.get('prod_default_model')}` for a prod-faithful read.\n"
        )

    L.append("## Summary")
    L.append(
        f"- {n_surfaces} surfaces graded "
        f"({live_n} from live simulation, {static_n} from static catalog)"
    )
    L.append(
        f"- **{_pct(flag_rate)} of surfaces flagged** (score < 4 or any flag),"
        f" mean score **{mean} / 5** (pass threshold ≥ 4.0)"
    )
    if top_flag:
        L.append(
            f"- Top failure flag: **{top_flag}** ({_pct(flag_rates.get(top_flag))} of surfaces)"
        )

    if everyday_items and control_items:
        em = round(sum(everyday_scores) / len(everyday_scores), 2) if everyday_scores else "—"
        cm = round(sum(control_scores) / len(control_scores), 2) if control_scores else "—"
        er = round(everyday_flagged / everyday_items, 3) if everyday_items else None
        cr = round(control_flagged / control_items, 3) if control_items else None
        L.append(
            f"- **Control canary:** everyday personas flag at {_pct(er)} (mean {em})"
            f" vs marketer-savvy {_pct(cr)} (mean {cm}). "
            f"A flagged question to an *everyday* persona is the product's fault."
        )
    elif everyday_items:
        er = round(everyday_flagged / everyday_items, 3) if everyday_items else None
        L.append(
            f"- Everyday-persona surfaces: {_pct(er)} flagged (no control personas in this run)."
        )

    # Per-group breakdown table.
    L.append("\n| group | kind | surfaces | flagged | mean |")
    L.append("|---|---|---|---|---|")
    for g in graded_groups:
        items = g.get("items", [])
        scores = [it["score"] for it in items if it.get("score") is not None]
        fl = sum(1 for it in items if it.get("flagged"))
        pm = round(sum(scores) / len(scores), 2) if scores else "—"
        kind = (
            "control"
            if g.get("is_control") is True
            else ("everyday" if g.get("is_control") is False else "static")
        )
        L.append(f"| {g['group_id']} | {kind} | {len(items)} | {fl} | {pm} |")

    L.append("\n## Failure modes")
    if not flag_counts:
        L.append("_No failure flags fired._")
    for flag_name, count in flag_counts.items():
        L.append(f"\n### {flag_name} — {count} surfaces, {_pct(flag_rates.get(flag_name))}")
        for off in _offenders(graded_groups, flag_name, limit=3):
            L.append(f'> {off["persona"]} ({off["source"]}): "{off["question"]}"')

    L.append("\n## Root cause")
    L.append(
        "<!-- FILL: for each dominant flag (≥10% of surfaces, or any that hit an everyday "
        "persona), name the specific clause/line in prompts/interviewer.txt or "
        "style_intent.txt that should have caught it and explain why it didn't. "
        "For the flagship assumes_targeting_knowledge flag, start with "
        "interviewer.txt:33-36 (AUDIENCE turn) and line 6 (Target audience output field). "
        "Open the prompt files and cite real line content. -->"
    )

    L.append("\n## Proposed edits")
    L.append(
        "<!-- FILL: concrete minimal edits — quote the current line and the proposed line. "
        "For the AUDIENCE turn: replace the audience-extraction framing with a question "
        "about the user's own reactions/life that lets Nova infer the audience. "
        "Remove 'Target audience' as an explicit output field or make it inferred. "
        "End with the prompt-change rule: bump INTERVIEWER_PROMPT_VERSION "
        "(app/agents/interviewer_agent.py) and/or STYLE_INTENT_PROMPT_VERSION "
        "(app/agents/style_intent.py) to a fresh date string + re-run the live eval:\n"
        "  NOVA_EVAL_MODE=live GEMINI_API_KEY=... ANTHROPIC_API_KEY=... \\\n"
        "    pytest tests/evals/ -v --eval-mode=live --with-judge -k interviewer\n"
        "Do NOT apply any edit. -->"
    )
    return "\n".join(L) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Grade user-facing questions against the empathy rubric."
    )
    ap.add_argument(
        "--inputs",
        nargs="+",
        required=True,
        help="One or more input files: conversations.json (Mode B) and/or surfaces.json (Mode A).",
    )
    ap.add_argument("--out", required=True, help="Where to write graded.json.")
    ap.add_argument("--report", default="", help="Where to write report.md.")
    default_rubric = (
        Path(__file__).resolve().parent.parent / "references" / "user_empathy_rubric.md"
    )
    ap.add_argument("--rubric", default=str(default_rubric), help="Path to user_empathy_rubric.md.")
    args = ap.parse_args()

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (repo-root .env).", file=sys.stderr)
        return 2
    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic SDK not installed in this python.", file=sys.stderr)
        return 2

    rubric = Path(args.rubric).read_text()
    client = anthropic.Anthropic()

    # Load all input files, routing by mode field.
    all_groups: list[dict] = []
    meta: dict = {}
    for inp_path_str in args.inputs:
        inp_path = Path(inp_path_str)
        if not inp_path.exists():
            print(f"WARNING: input not found, skipping: {inp_path}", file=sys.stderr)
            continue
        data = json.loads(inp_path.read_text())
        mode = data.get("mode", "")
        if mode == "live":
            all_groups.extend(_load_live_groups(inp_path))
            meta.update(
                {
                    k: data.get(k)
                    for k in ("gemini_model", "prod_default_model", "model_matches_prod")
                }
            )
        elif mode == "static":
            all_groups.extend(_load_static_groups(inp_path))
        else:
            print(f"WARNING: unknown mode '{mode}' in {inp_path} — skipping.", file=sys.stderr)

    if not all_groups:
        print(
            "ERROR: no surfaces to grade. "
            "Run simulate_conversations.py and/or extract_surfaces.py first.",
            file=sys.stderr,
        )
        return 2

    # Grade each group.
    all_scores: list[int] = []
    flag_counter: Counter = Counter()
    total_surfaces = 0
    flagged_surfaces = 0
    graded_groups: list[dict] = []
    live_surfaces = 0
    static_surfaces = 0

    for g in all_groups:
        gid = g["group_id"]
        print(f"grading group {gid} ({len(g['surfaces'])} surfaces) ...", file=sys.stderr)
        try:
            graded = _grade_group(client, rubric, g["surfaces"], g["context"])
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED {gid}: {type(exc).__name__}: {exc}", file=sys.stderr)
            graded_groups.append({**g, "items": [], "error": f"{type(exc).__name__}: {exc}"})
            continue
        for it in graded:
            total_surfaces += 1
            if it["score"] is not None:
                all_scores.append(it["score"])
            for f in it["flags"]:
                flag_counter[f] += 1
            if it["flagged"]:
                flagged_surfaces += 1
            if "live" in gid:
                live_surfaces += 1
            else:
                static_surfaces += 1
        graded_groups.append({**g, "items": graded, "error": None})

    mean = round(sum(all_scores) / len(all_scores), 2) if all_scores else None
    summary = {
        "surfaces_graded": total_surfaces,
        "live_surfaces": live_surfaces,
        "static_surfaces": static_surfaces,
        "surfaces_flagged": flagged_surfaces,
        "flag_rate": round(flagged_surfaces / total_surfaces, 3) if total_surfaces else None,
        "mean_score": mean,
        "flag_counts": dict(flag_counter.most_common()),
        "flag_rates": {k: round(c / total_surfaces, 3) for k, c in flag_counter.most_common()}
        if total_surfaces
        else {},
    }
    mode_label = (
        "mixed (live + static)"
        if live_surfaces and static_surfaces
        else "live"
        if live_surfaces
        else "static"
    )
    meta["mode_label"] = mode_label

    out = {"summary": summary, "groups": graded_groups}
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    report_path = (Path(args.report) if args.report else out_path.parent / "report.md").resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(summary, graded_groups, meta))
    print(f"report skeleton → {report_path} (fill the two <!-- FILL --> sections)", file=sys.stderr)

    print(
        f"\nGraded {total_surfaces} surfaces ({live_surfaces} live, {static_surfaces} static). "
        f"flagged={flagged_surfaces} ({summary['flag_rate']}), mean={mean}\n"
        f"top flags: {dict(flag_counter.most_common(5))}\n→ {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
