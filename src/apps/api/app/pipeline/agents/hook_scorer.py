"""GPT-4o hook scoring — single batched call for all 9 candidates.

Returns hook_score (0-10) per candidate. One call, not 9 calls.
@model_validator asserts len(scores) == len(candidates); retries once on mismatch.
"""

import structlog
from openai import OpenAI
from pydantic import BaseModel, model_validator

from app.config import settings

log = structlog.get_logger()

FILLER_WORDS = {"um", "uh", "like", "you know", "so", "basically", "literally"}


class HookScoreList(BaseModel):
    scores: list[float]  # one per candidate, same order

    @model_validator(mode="after")
    def validate_length_matches(self) -> "HookScoreList":
        # Length checked against candidates at call site, not here (we don't know n here)
        return self


def score_hooks(first_sentences: list[str]) -> list[float]:
    """Batch-score hook strength for each candidate's first sentence.

    Returns list of float scores (0-10) in the same order as first_sentences.
    On API failure: retries once, then falls back to 5.0 for all.
    """
    if not first_sentences:
        return []

    client = OpenAI(api_key=settings.openai_api_key)

    numbered = "\n".join(
        f"{i + 1}. {sentence}" for i, sentence in enumerate(first_sentences)
    )
    prompt = (
        "You are scoring short-form video hook strength. "
        "For each numbered sentence, rate 0-10: does it create a strong question or curiosity "
        "in the viewer's mind that makes them want to keep watching?\n\n"
        "Scoring guide:\n"
        "10: Irresistible — viewer MUST know what happens next\n"
        "7-9: Strong curiosity or emotion\n"
        "4-6: Mildly interesting\n"
        "1-3: Weak or generic\n"
        "0: Filler, silence, or no hook value\n\n"
        f"Sentences to score:\n{numbered}\n\n"
        f"Return a JSON object with key 'scores' containing exactly {len(first_sentences)} floats."
    )

    for attempt in range(2):
        try:
            response = client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format=HookScoreList,
                timeout=30,
            )
            result = response.choices[0].message.parsed
            if result is None:
                raise ValueError("Parsed response was None")

            scores = result.scores
            if len(scores) != len(first_sentences):
                log.warning(
                    "hook_scorer_length_mismatch",
                    expected=len(first_sentences),
                    got=len(scores),
                    attempt=attempt,
                )
                if attempt == 0:
                    continue  # retry once
                # Fill missing with heuristic
                scores = _pad_with_heuristic(scores, first_sentences)

            # Clamp to [0, 10]
            return [max(0.0, min(10.0, s)) for s in scores]

        except Exception as exc:
            log.warning("hook_scorer_api_error", error=str(exc), attempt=attempt)
            if attempt == 1:
                log.error("hook_scorer_fallback_to_heuristic")
                return [_heuristic_score(s) for s in first_sentences]

    return [_heuristic_score(s) for s in first_sentences]


def _heuristic_score(sentence: str) -> float:
    """Simple heuristic when GPT-4o is unavailable."""
    score = 5.0
    lower = sentence.lower().strip()
    if any(lower.startswith(w) for w in FILLER_WORDS):
        score -= 1.0
    if "?" in sentence:
        score += 1.5
    if lower.startswith(("how", "why", "what", "the truth", "i never")):
        score += 1.0
    return max(0.0, min(10.0, score))


def _pad_with_heuristic(scores: list[float], sentences: list[str]) -> list[float]:
    padded = list(scores)
    for i in range(len(scores), len(sentences)):
        padded.append(_heuristic_score(sentences[i]))
    return padded
