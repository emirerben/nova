"""Seed-provenance matcher for Bring-Your-Own-Ideas (M1 T5).

Maps generated PlanItemSpec objects back to the idea seeds that the prompt
asked the model to honour, so `PlanItem.source_idea_seed_id` can be populated
at persist time.

Design:
- Token-set scoring with containment applied for ANY seed size (no 3-token
  guard).  This differs intentionally from content_plan_dedup.idea_similarity
  which guards containment to sets >= 3 to avoid false-positive dedup hits.
  For provenance we want the opposite error bias: prefer false-positives
  (over-attribute) over false-negatives (miss a seeded item).  Short seeds
  like "Fenerbahce game" (2 content tokens) correctly score ~0.50 against a
  spec that preserves the key subject.
- Greedy 1-to-1 matching: sort all (spec, seed) pairs by score descending,
  assign while score >= threshold and neither side is already claimed.
- Unmatched specs → absent from the dict, so the caller leaves
  `source_idea_seed_id=None` (preserving the NULL sentinel from migration 0054).

Usage:
    from app.services.seed_provenance import match_specs_to_seeds
    seed_by_index = match_specs_to_seeds(output_specs, pending_seeds)
    for i, spec in enumerate(output_specs):
        item = PlanItem(..., source_idea_seed_id=seed_by_index.get(i))
"""

from __future__ import annotations

import re
import unicodedata

from app.agents._schemas.content_plan import PlanItemSpec

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Same stopword set as content_plan_dedup — kept in sync manually since both
# modules need it independently and a shared import would couple the two.
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


def _tokens(s: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return {t for t in _TOKEN_RE.findall(folded.lower()) if t not in _STOPWORDS}


def _provenance_score(spec_text: str, seed_text: str) -> float:
    """max(Jaccard, containment) without the 3-token guard.

    Containment = inter / min(|A|, |B|) captures "the spec honours the seed's
    core subject" regardless of how much filming detail the model added around
    it.  No minimum-size guard: a 2-token seed like "Fenerbahce game" correctly
    scores ~0.50 when the spec preserves "fenerbahce".
    """
    ta, tb = _tokens(spec_text), _tokens(seed_text)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb)
    containment = inter / min(len(ta), len(tb))
    return max(jaccard, containment)


# A matched spec must score at or above this to be attributed.  0.30 is
# permissive but bounded by the greedy 1:1 assignment — the second-best
# overlapping seed can't steal a match once the first is claimed.
_DEFAULT_THRESHOLD = 0.30


def match_specs_to_seeds(
    specs: list[PlanItemSpec],
    seeds: list[dict],
    *,
    threshold: float = _DEFAULT_THRESHOLD,
) -> dict[int, str]:
    """Return {spec_index: seed_id} for the best greedy 1:1 match above threshold.

    Parameters
    ----------
    specs:
        The PlanItemSpec list exactly as it will be iterated in the persist loop.
    seeds:
        List of idea-seed dicts: each must have "id" (str) and "text" (str).
        Dicts missing either key are silently skipped.
    threshold:
        Minimum score to consider a match (default 0.30).

    Returns
    -------
    dict mapping spec_index → seed_id (str hex).  Absent key = no match found.
    """
    if not specs or not seeds:
        return {}

    valid_seeds = [s for s in seeds if isinstance(s, dict) and s.get("id") and s.get("text")]
    if not valid_seeds:
        return {}

    candidates: list[tuple[float, int, int]] = []
    for si, spec in enumerate(specs):
        spec_text = f"{spec.theme or ''} {spec.idea or ''}".strip()
        for ki, seed in enumerate(valid_seeds):
            score = _provenance_score(spec_text, str(seed["text"]))
            if score >= threshold:
                candidates.append((score, si, ki))

    candidates.sort(key=lambda t: t[0], reverse=True)
    assigned_specs: set[int] = set()
    assigned_seeds: set[int] = set()
    result: dict[int, str] = {}

    for _score, si, ki in candidates:
        if si in assigned_specs or ki in assigned_seeds:
            continue
        assigned_specs.add(si)
        assigned_seeds.add(ki)
        result[si] = str(valid_seeds[ki]["id"])

    return result
