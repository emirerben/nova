"""Near-duplicate detection for generated content-plan items.

The content-plan generator (`nova.plan.content_plan_generator`) is asked in its
prompt to "never repeat the same idea twice", but a single whole-plan LLM pass
self-imposes variety poorly: ~1 in 5 plans ships two days that are essentially the
same idea reworded ("5am gym workout motivation routine" vs "early morning gym
workout motivation routine"). Prompt wording alone has not fixed it — it is
already asked.

This module is the pure, deterministic half of the code-enforced fix: it detects
near-duplicate ideas so the task (`tasks/content_plan_build.py`) can replace them
via one constrained regeneration call. No network, no DB, no LLM — every function
here is a pure transform over already-parsed `PlanItemSpec` objects, so it is
cheap to unit-test and safe to call inline.

Similarity is lexical, over the *idea* field only. We compare ideas, not themes:
`theme` is a content-pillar label that legitimately repeats across days as pillars
rotate, so theme overlap would be all false positives. The token set drops a small
stopword list because idea strings are prose ("a perfect day in [your city] told as
a morning-to-night montage"), where function words otherwise inflate overlap.

The score is `max(Jaccard, containment)` over content tokens — Jaccard alone
under-weights the dominant real failure mode (a later idea reuses most of an
earlier idea's content words plus a few new ones: high overlap, but a big union
drags Jaccard down), so containment (`|A∩B| / min(|A|,|B|)`) carries that case.
This is deliberately a LEXICAL signal: it catches reworded repeats, NOT paraphrase
with disjoint vocabulary ("5am gym motivation" vs "early morning workout energy"
share no content tokens and score 0 — catching those needs embeddings, out of
scope for a deterministic pass). (`services/lrclib_client._title_token_set_similarity`
is prior art for the Jaccard shape but is tuned for short song titles and keeps
stopwords, so it is the wrong fit here.)
"""

from __future__ import annotations

import re
import unicodedata

from app.agents._schemas.content_plan import PlanItemSpec

__all__ = [
    "DEFAULT_MAX_FRACTION",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "choose_replacements",
    "flag_replacement_indices",
    "idea_similarity",
]

# Conservative default: a reworded repeat ("5am gym workout motivation routine" vs
# "early morning gym workout motivation routine") scores ~0.8 and is caught;
# ideas that merely share a noun or two ("coffee shop tour" vs "hiking trails")
# stay well below. Tuned with the user toward few false positives.
DEFAULT_SIMILARITY_THRESHOLD = 0.72

# Never flag more than this fraction of a plan, even if the LLM produced a very
# repetitive batch — a runaway flag count would trigger wholesale replacement and
# could make the plan *worse*. Past the cap we keep the remaining items as-is.
DEFAULT_MAX_FRACTION = 0.4

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words + the generic content-plan filler that carries no concept signal.
# Kept deliberately small: over-stripping makes distinct ideas look identical.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "as",
        "is",
        "are",
        "be",
        "your",
        "you",
        "my",
        "me",
        "i",
        "we",
        "our",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "their",
        "they",
        "them",
        "show",
        "showing",
        "video",
        "clip",
        "clips",
        "day",
        "into",
        "about",
        "how",
        "what",
        "why",
        "make",
        "making",
        "do",
        "doing",
        "get",
        "one",
        "some",
        "up",
        "out",
        "off",
        "over",
        "than",
        "then",
        "via",
    }
)


def _idea_tokens(s: str) -> set[str]:
    """Lowercased, accent-folded `[a-z0-9]` tokens with stopwords removed."""
    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return {t for t in _TOKEN_RE.findall(folded.lower()) if t not in _STOPWORDS}


def idea_similarity(a: str, b: str) -> float:
    """max(Jaccard, containment) over content tokens: 1.0 identical, 0.0 disjoint.

    Robust to word reordering and punctuation. Containment (`inter / smaller set`)
    is added so a reworded repeat that adds/drops a few words still scores high
    despite the larger union; it is guarded to sets of >= 3 tokens each so two-word
    ideas can't trivially "contain" one another. Two ideas with no content tokens
    left after stopword removal score 0.0 (we never flag empty-ish ideas as dupes).
    """
    ta, tb = _idea_tokens(a), _idea_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb)
    if min(len(ta), len(tb)) >= 3:
        return max(jaccard, inter / min(len(ta), len(tb)))
    return jaccard


def flag_replacement_indices(
    items: list[PlanItemSpec],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_fraction: float = DEFAULT_MAX_FRACTION,
) -> list[int]:
    """Indices (into `items`) of near-duplicate ideas to replace.

    Walks items in order, keeping the FIRST occurrence of each distinct idea and
    flagging any later item whose idea is >= `threshold` similar to one already
    kept — so each near-dup cluster loses its later member(s), never its first.
    Stops flagging once `floor(len * max_fraction)` items are flagged; everything
    past the cap is kept as-is. Returns flagged indices in ascending order.
    """
    cap = int(len(items) * max_fraction)
    if cap <= 0:
        return []
    kept_ideas: list[str] = []
    flagged: list[int] = []
    for i, item in enumerate(items):
        is_dup = any(idea_similarity(item.idea, k) >= threshold for k in kept_ideas)
        if is_dup and len(flagged) < cap:
            flagged.append(i)
        else:
            kept_ideas.append(item.idea)
    return flagged


def choose_replacements(
    needed: int,
    candidates: list[PlanItemSpec],
    avoid_ideas: list[str],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[PlanItemSpec]:
    """Pick up to `needed` candidate items whose idea is distinct from everything.

    Greedy: accept a candidate only if its idea is < `threshold` similar to every
    idea in `avoid_ideas` AND to every already-accepted candidate, so the chosen
    replacements are distinct both from the kept plan and from each other. Returns
    fewer than `needed` when the candidate pool can't supply enough distinct ideas
    — the caller keeps the originals for any unfilled slot (best-effort).
    """
    if needed <= 0:
        return []
    accepted: list[PlanItemSpec] = []
    blocked = list(avoid_ideas)
    for cand in candidates:
        if len(accepted) >= needed:
            break
        if any(idea_similarity(cand.idea, b) >= threshold for b in blocked):
            continue
        accepted.append(cand)
        blocked.append(cand.idea)
    return accepted
