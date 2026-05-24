"""Cross-engine OCR agreement check.

Two engines, one image: agree on the text or flag for human review.

Decision rule
-------------
- Lowercase + collapse whitespace + sort each engine's token list.
- Join with a single space, compute a Levenshtein-ratio similarity in
  ``[0, 1]`` via ``difflib.SequenceMatcher.ratio()``.
- ``>= threshold`` → status ``"agreed"`` and emit a deduped token list
  (case-folded, sorted) as the canonical truth.
- ``< threshold`` → status ``"disagreed"`` and emit the raw per-engine
  outputs untouched so a human can adjudicate.

Why stdlib ``difflib`` (not ``python-Levenshtein`` or ``rapidfuzz``):
``SequenceMatcher.ratio()`` is the same Gestalt-pattern ratio rapidfuzz
exposes as ``ratio()``, returns a value in ``[0, 1]``, ships with every
Python distribution, and is fast enough at the autobuilder's per-frame
scale (~30 frames × ~10 tokens). Adding a C-extension dep for a few-ms
hot loop that isn't on the request path would be gold-plating.

Sorting before joining means the agreement check is order-invariant —
OCR engines disagree about reading order on stacked overlays more often
than they disagree about the underlying tokens, and the autobuilder
only cares about token-set equivalence (downstream stages re-derive
order from frame timing). The agreement key matches what the eval's
fuzzy-match cares about too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.services.ocr.engines import OCREngine, OCRWord

DEFAULT_AGREEMENT_THRESHOLD = 0.85


@dataclass
class CrossCheckResult:
    """Result of one cross-check between two engines on one image."""

    status: str  # "agreed" | "disagreed"
    agreement: float
    tokens: list[str] = field(default_factory=list)
    engine_a_tokens: list[str] = field(default_factory=list)
    engine_b_tokens: list[str] = field(default_factory=list)
    engine_a_name: str = ""
    engine_b_name: str = ""


def _normalize_tokens(words: list[OCRWord]) -> list[str]:
    """Lowercase + strip + filter empty. Preserves duplicates so
    a refrain isn't silently merged with itself.

    Sorting happens in the agreement-key step (``_agreement_key``); here
    we keep insertion order so the per-engine token lists in a
    disagreement report stay readable in source order.
    """
    out: list[str] = []
    for w in words:
        token = " ".join(w.text.lower().split())
        if token:
            out.append(token)
    return out


def _agreement_key(tokens: list[str]) -> str:
    """Order-invariant text for similarity scoring.

    Sorting is the cheapest way to ignore reading-order disagreements
    while still penalizing different token *sets*. ``" "`` join keeps
    SequenceMatcher's word-aware ratio meaningful on multi-word tokens.
    """
    return " ".join(sorted(tokens))


def cross_check_engines(
    engine_a: OCREngine,
    engine_b: OCREngine,
    image_path: str,
    *,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
) -> CrossCheckResult:
    """Run both engines on ``image_path``; agree or disagree.

    ``threshold`` defaults to 0.85 — the value picked in the T2 plan
    review. ``[0, 1]``; raise ``ValueError`` if out of range so a typo
    in a config doesn't silently relax the trust gate.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0, 1]; got {threshold!r}")

    a_words = engine_a.recognize(image_path)
    b_words = engine_b.recognize(image_path)
    return _cross_check_from_words(
        engine_a.name, a_words, engine_b.name, b_words, threshold=threshold
    )


def _cross_check_from_words(
    engine_a_name: str,
    a_words: list[OCRWord],
    engine_b_name: str,
    b_words: list[OCRWord],
    *,
    threshold: float,
) -> CrossCheckResult:
    """Pure agreement check given already-recognized word lists.

    Split out from :func:`cross_check_engines` so tests can drive the
    decision logic without instantiating engine adapters and so callers
    that already have OCR output cached don't pay for redundant
    ``recognize()`` calls.
    """
    a_tokens = _normalize_tokens(a_words)
    b_tokens = _normalize_tokens(b_words)

    if not a_tokens and not b_tokens:
        # Both engines saw nothing — that's mutual agreement on "no text
        # in frame", which is a legitimate ground-truth outcome for a
        # background-only frame.
        return CrossCheckResult(
            status="agreed",
            agreement=1.0,
            tokens=[],
            engine_a_tokens=[],
            engine_b_tokens=[],
            engine_a_name=engine_a_name,
            engine_b_name=engine_b_name,
        )

    a_key = _agreement_key(a_tokens)
    b_key = _agreement_key(b_tokens)
    ratio = SequenceMatcher(None, a_key, b_key).ratio()

    if ratio >= threshold:
        # The canonical token list is the union of both engines'
        # findings (case-folded). Intersection would be safer but
        # produces empty truth when one engine misses a single noisy
        # token — the threshold gate already guarantees the two are
        # mostly aligned.
        agreed_tokens = sorted(set(a_tokens) | set(b_tokens))
        return CrossCheckResult(
            status="agreed",
            agreement=ratio,
            tokens=agreed_tokens,
            engine_a_tokens=a_tokens,
            engine_b_tokens=b_tokens,
            engine_a_name=engine_a_name,
            engine_b_name=engine_b_name,
        )

    return CrossCheckResult(
        status="disagreed",
        agreement=ratio,
        tokens=[],
        engine_a_tokens=a_tokens,
        engine_b_tokens=b_tokens,
        engine_a_name=engine_a_name,
        engine_b_name=engine_b_name,
    )
