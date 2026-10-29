#!/usr/bin/env python3
"""Main Creator input size on the 29 Ekim clip set, full vs compact clip evidence (KRI-1037).

    cd src/apps/api
    .venv/bin/python ../../../scripts/kria/measure_29_ekim_input.py [--count-tokens]

What this measures, and what it does not (read before quoting a number):

* It renders the REAL ``MainCreatorAgent.render_prompt`` for the fixture's 27 clips and the REAL
  ``creator_sessions._chat_evidence`` / fact caps, once with the full tier and once with the
  compact tier. The difference between the two renders is exact (characters) for the inputs used.
* Prod keeps only key names and a hash of each agent input, and the fixture keeps only each
  clip's subject, so the per-clip analysis records are SYNTHETIC: deterministic text sized to
  the existing per-field caps. Two shapes are reported: ``typical`` (about 70% of each cap) and
  ``ceiling`` (every field at its cap, the most the old caps allow).
* The recorded 31,062 input tokens of T1 cannot be decomposed (conversation, brief, creator
  context and the real analysis text are not in the fixture). The absolute token counts below
  are therefore for this synthetic prompt, not a reproduction of T1; the before/after DELTA per
  clip is the transferable number.
* ``--count-tokens`` asks Gemini's count_tokens endpoint (a metadata call, no generation, not
  billed as a model call) for the true tokenizer count; without it tokens are chars / 4.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
FIXTURE = API / "tests" / "fixtures" / "kria_replay" / "29-ekim" / "fixture.json"

_SAFE_ENV = {
    "STORAGE_BUCKET": "offline",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://localhost:5432/offline",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "offline",
    "TOKEN_ENCRYPTION_KEY": "offline",
    "INTERNAL_API_KEY": "offline",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}

_FILLER = (
    "handheld footage in warm daylight with people walking past and the camera panning slowly "
    "across the scene before settling on the main subject while ambient street sound continues"
)


def _text(seed: str, chars: int) -> str:
    out = seed
    while len(out) < chars:
        out += " " + _FILLER
    return out[:chars].rstrip()


def _analysis(clip: dict[str, Any], index: int, shape: str) -> dict[str, Any]:
    """A synthetic stored analysis for one clip; ``shape`` scales every field to its cap."""
    scale = 1.0 if shape == "ceiling" else 0.7

    def n(cap: int) -> int:
        return max(20, int(cap * scale))

    subject = clip["subject"]
    return {
        "understanding": {
            "subject": _text(subject, n(120)),
            "summary": _text(subject + ".", n(200)),
            "setting": _text("outdoor city", n(120)),
            "activity": _text("walking and filming", n(120)),
            "on_screen_text": _text("", n(120)) if index % 5 == 0 else "",
            "speech": {
                "has_speech": index % 4 == 0,
                "transcript": _text("so this is where we", n(160)) if index % 4 == 0 else "",
            },
            "notable_moments": [
                {"start_s": 0.5 + i, "end_s": 1.5 + i, "description": _text("a moment", n(80))}
                for i in range(2)
            ],
            "people": {"count": 2, "speaks_to_camera": False, "note": _text("two people", n(80))},
        },
        "clip_facts": [
            {
                "kind": "capture_time",
                "value": f"2026-10-29T0{index % 9}:12:00Z",
                "provenance": "exif",
            },
            {"kind": "place", "value": "Istanbul, Turkey", "provenance": "geocode"},
            {"kind": "landmark", "value": _text(subject, 40), "provenance": "vision"},
            {"kind": "visible_text", "value": "OPEN", "provenance": "vision"},
        ],
    }


def _media_context(fx: dict, ids: list[str], shape: str, compact: bool) -> list[dict[str, Any]]:
    from app.schemas.clip_understanding import ClipFact
    from app.services.clip_facts import facts_for_prompt
    from app.services.creator_sessions import (
        CHAT_EVIDENCE_COMPACT_MAX_FACTS,
        CHAT_EVIDENCE_MAX_FACTS,
        _chat_evidence,
    )

    rows = []
    for index, (clip, media_id) in enumerate(zip(fx["clips"], ids)):
        analysis = _analysis(clip, index, shape)
        facts = [ClipFact(**f) for f in analysis["clip_facts"]]
        cap = CHAT_EVIDENCE_COMPACT_MAX_FACTS if compact else CHAT_EVIDENCE_MAX_FACTS
        rows.append(
            {
                "media_id": media_id,
                "kind": clip["kind"],
                "duration_s": clip["duration_s"],
                "creator_note": None,
                "analysis_only_not_copy": _chat_evidence(
                    analysis, kind=clip["kind"], include_facts=False, compact=compact
                ),
                "facts": facts_for_prompt(facts[:cap]),
            }
        )
    return rows


def _prompt(fx: dict, shape: str, compact: bool) -> str:
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

    ids = [f"analysis-proxy-ios-{uuid.UUID(int=i)}" for i in range(len(fx["clips"]))]
    available = CapabilityAvailability(available=True)
    manifest = ResolvedCreatorManifest(
        item_id=str(uuid.UUID(int=999)),
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=i, kind=c["kind"], duration_s=c["duration_s"])
            for i, c in zip(ids, fx["clips"])
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    agent_input = MainCreatorInput(
        user_message="Split it into pieces, word by word",
        media_context=_media_context(fx, ids, shape, compact),
        capability_manifest=manifest,
    )
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


def _count_tokens(texts: dict[str, str]) -> dict[str, int]:
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return {
        name: int(
            client.models.count_tokens(model="gemini-3.1-pro-preview", contents=text).total_tokens
        )
        for name, text in texts.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count-tokens", action="store_true")
    args = parser.parse_args()
    for key, value in _SAFE_ENV.items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("GEMINI_API_KEY", "offline")
    sys.path.insert(0, str(API))
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))

    prompts = {
        f"{shape}/{tier}": _prompt(fx, shape, tier == "compact")
        for shape in ("typical", "ceiling")
        for tier in ("full", "compact")
    }
    tokens = _count_tokens(prompts) if args.count_tokens else {}
    clips = len(fx["clips"])
    report: dict[str, Any] = {"clips": clips, "token_basis": "gemini count_tokens"}
    if not tokens:
        report["token_basis"] = "chars / 4 (estimate)"
    for shape in ("typical", "ceiling"):
        full, compact = prompts[f"{shape}/full"], prompts[f"{shape}/compact"]
        row = {
            "chars_full": len(full),
            "chars_compact": len(compact),
            "chars_saved": len(full) - len(compact),
        }
        t_full = tokens.get(f"{shape}/full", len(full) // 4)
        t_compact = tokens.get(f"{shape}/compact", len(compact) // 4)
        row.update(
            tokens_full=t_full,
            tokens_compact=t_compact,
            tokens_saved=t_full - t_compact,
            tokens_saved_per_clip=round((t_full - t_compact) / clips, 1),
            saved_pct=round(100 * (t_full - t_compact) / t_full, 1),
        )
        report[shape] = row
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
