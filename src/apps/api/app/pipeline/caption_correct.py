"""[Stage 2.5] LLM caption correction — fix ASR spelling/grammar, KEEP timing.

whisper-1 transcribes but mishears morphology, especially in agglutinative languages
like Turkish ("Kaçer" for "Kaçar", "nereye" for "nereyi"). This pass sends the caption
cue TEXT (one JSON list) to a small LLM that fixes each line's spelling / grammar / case
endings while preserving the line count + order — so every cue's [start_s, end_s] window
is untouched and captions stay synced.

Contract (the length guard is load-bearing):
  - N cues in → EXACTLY N corrected lines out, same order. Any length mismatch, empty
    output, or API failure → return the ORIGINAL cues unchanged (never break the render).
  - A cue whose text actually changes drops its stale per-word timings (`words`) so the
    word-by-word burn re-synthesizes them over the real cue window (see captions.py E3).
  - Only the text is touched; timings are never moved.

Empirically (real Turkish clip, gpt-4o-mini): "Kaçer→Kaçar", "nereye→nereyi",
"onar→Onar", cue count preserved. See the subtitled-talking-head plan.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.config import settings

log = structlog.get_logger()

# Readable language names for the prompt; fall back to the raw ISO code.
_LANG_NAMES = {"tr": "Turkish", "en": "English"}

# Concrete error-class examples matter: without them gpt-4o-mini fixes obvious
# spelling but leaves CONTEXTUAL grammar errors (wrong case endings) alone. The
# examples teach the error type, they are not hardcoded substitutions.
_SYSTEM_PROMPT = (
    "You fix errors in auto-generated subtitle lines. The spoken language is given. Make "
    "each line grammatically correct, natural {language} as actually spoken. Fix "
    "mishearings and spelling, AND wrong word forms — especially WRONG CASE/SUFFIX endings "
    "that don't fit the sentence. For example, in Turkish fix a wrong case ending like "
    "'nereye' -> 'nereyi' when the sentence needs the object (accusative), or a mis-heard "
    "vowel like 'Kaçer' -> 'Kaçar'. You MAY change words within a line to make it correct, "
    "but you MUST: keep the SAME number of lines in the SAME order, never move words "
    "between lines, never translate, never add commentary or punctuation-only lines. "
    'Return ONLY JSON: {{"lines": [...]}} with exactly the same length as the input.'
)


def _language_name(language: str | None) -> str:
    code = (language or "").strip().lower()
    return _LANG_NAMES.get(code, code or "the spoken language")


def _llm_correct_lines(texts: list[str], language: str | None, *, model: str) -> list[str]:
    """One LLM call: N subtitle lines → N corrected lines. Raises on API error."""
    import openai  # noqa: PLC0415

    client = openai.OpenAI(api_key=settings.openai_api_key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT.format(language=_language_name(language))},
            {
                "role": "user",
                "content": json.dumps(
                    {"language": _language_name(language), "lines": texts}, ensure_ascii=False
                ),
            },
        ],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    lines = data.get("lines")
    if not isinstance(lines, list):
        raise ValueError("correction response missing 'lines' array")
    return [str(x) for x in lines]


def correct_caption_cues(
    cues: list[dict[str, Any]],
    language: str | None,
    *,
    model: str | None = None,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Return cues with ASR-corrected text; timings preserved. Best-effort: any failure
    returns the input cues untouched, so a correction problem never fails a render."""
    if not enabled or not cues:
        return cues
    texts = [str(c.get("text", "")) for c in cues]
    if not any(t.strip() for t in texts):
        return cues

    model = (model or "").strip() or "gpt-4o-mini"
    try:
        corrected = _llm_correct_lines(texts, language, model=model)
    except Exception as exc:  # noqa: BLE001 — correction is best-effort
        log.warning("caption_correction_failed", error=str(exc)[:200], language=language)
        return cues

    if len(corrected) != len(texts):
        # The whole point is 1:1 line mapping; a mismatch would desync timing → discard.
        log.warning("caption_correction_length_mismatch", got=len(corrected), expected=len(texts))
        return cues

    out: list[dict[str, Any]] = []
    changed = 0
    for cue, new_text in zip(cues, corrected):
        new_text = new_text.strip()
        if new_text and new_text != str(cue.get("text", "")).strip():
            cue = {**cue, "text": new_text}
            # Stale per-word timings after a text change → let word-pop re-synthesize (E3).
            cue.pop("words", None)
            changed += 1
        out.append(cue)
    log.info("caption_correction_done", cues=len(out), changed=changed, model=model)
    return out
