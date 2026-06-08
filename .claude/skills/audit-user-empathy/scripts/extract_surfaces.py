#!/usr/bin/env python3
"""Mode A: catalog fixed questions/copy strings from prompts + UI (no LLM required).

Reads:
  - prompts/interviewer.txt         — example questions from the interview arc
  - OnboardingStep.tsx FIELDS       — static questionnaire labels
  - personas.py greeting strings    — hardcoded greeting/fallback copy

Run from src/apps/api (or anywhere in the repo):

    /Users/.../.venv-test/bin/python \\
      ../../../.claude/skills/audit-user-empathy/scripts/extract_surfaces.py \\
      --out /tmp/empathy-audit/surfaces.json

No API keys needed. Stdlib only.

Output shape:
  {"mode": "static", "sources": {...path map...},
   "surfaces": [{"surface_id": "...", "question": "...", "source": "...", "context": "..."}]}
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path


def _git_root() -> Path:
    """Walk up to the repo root; fall back to cwd if git isn't available."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    # Walk up looking for a .git directory.
    cwd = Path.cwd()
    for p in (cwd, *cwd.parents):
        if (p / ".git").exists():
            return p
    return cwd


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Catalog fixed user-facing questions/copy from prompts + UI.",
    )
    ap.add_argument("--out", required=True, help="Where to write surfaces.json.")
    args = ap.parse_args()

    repo = _git_root()
    surfaces: list[dict] = []
    sources: dict[str, str] = {}

    # ── Source 1: prompts/interviewer.txt — example questions ────────────────
    interviewer_path = repo / "src/apps/api/prompts/interviewer.txt"
    sources["interviewer"] = str(interviewer_path)
    if interviewer_path.exists():
        surfaces.extend(_extract_interviewer(interviewer_path))
    else:
        print(f"WARNING: not found: {interviewer_path}", file=sys.stderr)

    # ── Source 2: OnboardingStep.tsx — static form FIELDS ────────────────────
    onboarding_path = repo / "src/apps/web/src/app/plan/_components/OnboardingStep.tsx"
    sources["onboarding_form"] = str(onboarding_path)
    if onboarding_path.exists():
        surfaces.extend(_extract_onboarding(onboarding_path))
    else:
        print(f"WARNING: not found: {onboarding_path}", file=sys.stderr)

    # ── Source 3: personas.py — greeting/fallback copy ───────────────────────
    personas_path = repo / "src/apps/api/app/routes/personas.py"
    sources["route_greetings"] = str(personas_path)
    if personas_path.exists():
        surfaces.extend(_extract_route_greetings(personas_path))
    else:
        print(f"WARNING: not found: {personas_path}", file=sys.stderr)

    # ── Source 4: TikTok pre-screen (static copy) ────────────────────────────
    tiktok_screen_path = repo / "src/apps/web/src/app/plan/_components/TikTokPreScreen.tsx"
    sources["tiktok_prescreen"] = str(tiktok_screen_path)
    if tiktok_screen_path.exists():
        surfaces.extend(_extract_tsx_strings(tiktok_screen_path, source="tiktok_prescreen"))

    out = {
        "mode": "static",
        "extracted_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "sources": sources,
        "surfaces": surfaces,
    }
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Extracted {len(surfaces)} surfaces → {out_path}", file=sys.stderr)
    return 0


# ── Extractors ────────────────────────────────────────────────────────────────


def _extract_interviewer(path: Path) -> list[dict]:
    """Pull 'Good:' example questions and arc turn labels from interviewer.txt."""
    text = path.read_text()
    surfaces = []
    current_arc = "unknown"
    idx = 0

    for line in text.splitlines():
        # Track the arc turn we're in ("Turn 1 — ACCESS", etc.)
        arc_m = re.match(r"Turn\s+(\d+)\s+—\s+([A-Z]+):", line.strip())
        if arc_m:
            current_arc = f"turn_{arc_m.group(1)}_{arc_m.group(2).lower()}"

        # Pull "Good: ..." example questions.
        good_m = re.match(r'\s+Good:\s+"(.+)"', line)
        if good_m:
            q = good_m.group(1).strip()
            surfaces.append(
                {
                    "surface_id": f"interviewer_example_{idx}",
                    "question": q,
                    "source": "interviewer.txt",
                    "context": f"Example question from {current_arc} arc",
                }
            )
            idx += 1

    # Also extract the hard-banned list (lines starting with "Never ask:") so we can
    # confirm they're not in the live output.
    banned: list[str] = []
    in_never = False
    for line in text.splitlines():
        if re.search(r"Never ask:", line):
            in_never = True
        if in_never:
            m = re.findall(r"'([^']+\?)'", line)
            banned.extend(m)
            if line.strip() == "" and in_never:
                in_never = False

    if banned:
        surfaces.append(
            {
                "surface_id": "interviewer_banned_list",
                "question": "(informational — banned list, should NOT appear in live output)",
                "source": "interviewer.txt",
                "context": (
                    "BANNED questions that interviewer.txt explicitly forbids: " + "; ".join(banned)
                ),
            }
        )

    return surfaces


def _extract_onboarding(path: Path) -> list[dict]:
    """Pull question/label strings from the OnboardingStep.tsx FIELDS array."""
    text = path.read_text()
    surfaces = []
    idx = 0

    # Match 'label' or 'question' string properties in the FIELDS array.
    # Handles: label: "What do you do for work?",  or  placeholder: "..."
    for pattern in [
        r'label:\s*["\']([^"\'?]+\??)["\']',
        r'question:\s*["\']([^"\'?]+\??)["\']',
        r'placeholder:\s*["\']([^"\'?]+\??)["\']',
    ]:
        for m in re.finditer(pattern, text):
            q = m.group(1).strip()
            if len(q) < 5 or q.lower() in {"field name", "e.g.", "example"}:
                continue
            surfaces.append(
                {
                    "surface_id": f"onboarding_form_{idx}",
                    "question": q,
                    "source": "OnboardingStep.tsx",
                    "context": "Static questionnaire FIELDS array (fallback onboarding form)",
                }
            )
            idx += 1

    # Deduplicate by question text (multiple patterns may match the same string).
    seen: set[str] = set()
    deduped = []
    for s in surfaces:
        if s["question"] not in seen:
            seen.add(s["question"])
            deduped.append(s)
    # Re-index after dedup.
    for i, s in enumerate(deduped):
        s["surface_id"] = f"onboarding_form_{i}"
    return deduped


def _extract_route_greetings(path: Path) -> list[dict]:
    """Pull hardcoded greeting/fallback/error strings from personas.py."""
    text = path.read_text()
    surfaces = []

    # The style agent greeting strings (from style_agent_start route).
    # These are triple-quoted or concatenated f-strings — extract multi-word sentences.
    sentence_re = re.compile(r'"([A-Z][^"]{15,}[.!?])"')
    seen: set[str] = set()
    idx = 0
    for m in sentence_re.finditer(text):
        s = m.group(1).strip()
        if s in seen:
            continue
        seen.add(s)
        # Only include sentences that the user would actually read — heuristically,
        # those that contain second-person copy ("Tell me", "Your style", "you can").
        if any(kw in s for kw in ("Tell me", "Your style", "You can", "tell me", "you can")):
            surfaces.append(
                {
                    "surface_id": f"route_greeting_{idx}",
                    "question": s,
                    "source": "routes/personas.py",
                    "context": "Hardcoded greeting/instruction copy shown to the user",
                }
            )
            idx += 1

    return surfaces


def _extract_tsx_strings(path: Path, source: str) -> list[dict]:
    """Generic extractor for human-readable strings in a TSX file."""
    text = path.read_text()
    surfaces = []
    idx = 0
    # Extract JSX text content and string literals that look like user-facing copy.
    for m in re.finditer(r'"([A-Z][^"]{10,}\??)"', text):
        s = m.group(1).strip()
        if any(kw in s for kw in ("TikTok", "your", "you ", "Your", "You ")):
            surfaces.append(
                {
                    "surface_id": f"{source}_{idx}",
                    "question": s,
                    "source": source,
                    "context": f"Static copy from {path.name}",
                }
            )
            idx += 1
    return surfaces


if __name__ == "__main__":
    raise SystemExit(main())
