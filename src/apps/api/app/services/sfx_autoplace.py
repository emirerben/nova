"""sfx_autoplace — server-side validation for SFX auto-suggestions (dark-flagged).

The `sfx_placement` agent proposes; THIS module disposes. `resolve_sfx_suggestions`
is a pure function so tests can drive every drop rule without a DB or an LLM:

  - unknown effect_id → dropped (catalog is the allowlist);
  - effects analyzed as voice-bearing → dropped (legacy NULL passes — only
    an affirmative contains_voice=True bans; analysis backfill tightens this);
  - at_s outside [0, duration - END_KEEPOUT_S] or non-finite → dropped;
  - closer than MIN_SPACING_S to an accepted earlier suggestion → dropped;
  - more than MAX_SUGGESTIONS accepted → tail dropped.

Accepted suggestions carry `transcript_hash` so the read path
(`_variants_for_response`) can stale-filter them after clip re-cuts or
re-transcription — an unverifiable suggestion is never served.
"""

from __future__ import annotations

import math
import uuid

MAX_SUGGESTIONS = 6
MIN_SPACING_S = 1.5
END_KEEPOUT_S = 0.5
# A suggestion's at_s must sit on a supplied mark (word start/end, pause start,
# moment edge) — the agent is instructed to copy marks verbatim; anything
# further than this epsilon from every mark is an invented time and is dropped.
MARK_EPSILON_S = 0.25
_GAIN_MIN = 0.1
_GAIN_MAX = 1.5
_GAIN_DEFAULT = 0.8


def resolve_sfx_suggestions(
    raw: list,
    glossary: list[dict],
    duration_s: float,
    transcript_hash: str,
    allowed_marks: list[float] | None = None,
) -> list[dict]:
    """Validate agent output into persistable `pending_sfx_suggestions` records.

    `raw` items expose effect_id / at_s / gain / anchor / reason (attribute or
    dict access). `glossary` rows are `_load_glossary` records (id, name,
    audio_gcs_path, contains_voice). When `allowed_marks` is provided, each
    accepted at_s is snapped to the nearest mark within MARK_EPSILON_S and
    anything not near ANY mark is dropped — the verbatim-copy discipline is
    enforced here, not just in the prompt.
    """
    marks = sorted(
        m for m in (allowed_marks or []) if isinstance(m, (int, float)) and math.isfinite(float(m))
    )
    by_id = {
        str(g.get("id")): g
        for g in glossary
        if g.get("audio_gcs_path") and not g.get("contains_voice")
    }

    def _get(item, key, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    candidates = []
    for item in raw or []:
        effect_id = str(_get(item, "effect_id") or "")
        if effect_id not in by_id:
            continue
        try:
            at_s = float(_get(item, "at_s", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(at_s) or at_s < 0.0:
            continue
        if at_s > max(0.0, float(duration_s) - END_KEEPOUT_S):
            continue
        if marks:
            nearest = min(marks, key=lambda m: abs(float(m) - at_s))
            if abs(float(nearest) - at_s) > MARK_EPSILON_S:
                continue  # invented time — not near any supplied mark
            at_s = float(nearest)
            if at_s > max(0.0, float(duration_s) - END_KEEPOUT_S):
                continue
        try:
            gain = float(_get(item, "gain", _GAIN_DEFAULT))
        except (TypeError, ValueError):
            gain = _GAIN_DEFAULT
        if not math.isfinite(gain):
            gain = _GAIN_DEFAULT
        gain = min(_GAIN_MAX, max(_GAIN_MIN, gain))
        candidates.append(
            {
                "effect_id": effect_id,
                "at_s": round(at_s, 2),
                "gain": round(gain, 2),
                # anchor is debug/trace provenance only (which mark type the
                # agent copied from) — no runtime consumer reads it back.
                "anchor": str(_get(item, "anchor") or "pause")[:20],
                "reason": str(_get(item, "reason") or "")[:160],
            }
        )

    candidates.sort(key=lambda c: c["at_s"])
    out: list[dict] = []
    for c in candidates:
        if out and c["at_s"] - out[-1]["at_s"] < MIN_SPACING_S:
            continue
        effect = by_id[c["effect_id"]]
        out.append(
            {
                "id": uuid.uuid4().hex,
                **c,
                "name": str(effect.get("name") or "")[:60],
                "transcript_hash": transcript_hash,
            }
        )
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out
