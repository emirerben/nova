"""Random and "any order" clip orders (29 Ekim lane B).

A creator who says "show everything in random order" or "any order is fine" asked for an
order the server can PROVE (a seeded permutation) or one that needs no proof. Neither is the
"ordering rule I cannot verify" the choice gate and the render contract refuse. One module
owns the recognition, the seed and the permutation so the planner, the verifier, the choice
gate and the editor op can never disagree about them.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.text_fold import loose_text

OrderMode = Literal["random", "any"]

RANDOM_ORDER_KEY = "random"
ANY_ORDER_KEY = "any_order"
RANDOM_ORDER_KEYS = frozenset({"random", "shuffle", "shuffled", "randomize", "randomise"})
ANY_ORDER_KEYS = frozenset({"any_order", "any", "unordered", "no_order", "whatever"})

_SEED_BOUND = 2**63

_RANDOM_CUES = re.compile(
    r"\b(?:random(?:ly)?|at random|shuffl\w*|randomi[sz]\w*|jumbled|scrambled|"
    r"rastgele|rasgele|karisik|karistir\w*|gelisiguzel)\b"
)
_ANY_CUES = re.compile(
    r"\b(?:in |with )?(?:any|whatever|whichever) (?:order|sequence)\b|"
    r"\bno (?:particular|specific|fixed|set) (?:order|sequence)\b|"
    r"\border (?:does not|doesn'?t|is not|isn'?t) (?:matter|important)\b|"
    r"\b(?:does not|doesn'?t|don'?t|do not) (?:care|matter)\b.{0,20}\border\b|"
    r"\bherhangi bir (?:sira|siralama)\b|\bsira (?:farketmez|fark etmez|onemli degil)\b|"
    r"\bsiralama (?:farketmez|fark etmez|onemli degil)\b|\bher sirada\b"
)


def _key(facts: Mapping[str, Any]) -> str:
    for name in ("key", "by", "order", "mode"):
        value = facts.get(name)
        if isinstance(value, str) and value.strip():
            return "_".join(value.casefold().replace("-", " ").split())
    return ""


# Words that make a cue sentence something other than a plain "random"/"any order" ask: a
# negation ("not random", "don't shuffle", "olmasin"), a second order rule ("chronological",
# "start with the beach", "the order I added them") or a contrast ("but", "ama"). Checked on
# what is LEFT after the cue itself is removed, so "order doesn't matter" keeps its own "not".
_NOT_PLAIN = re.compile(
    r"\b(?:not|no|never|without|don'?t|do not|doesn'?t|does not|isn'?t|aren'?t|won'?t|"
    r"instead|rather|except|but|however|only|keep|same|reverse\w*|"
    r"chronolog\w*|captur\w*|filmed|shot|recorded|added|attach\w*|upload\w*|oldest|newest|"
    r"latest|earliest|alphabet\w*|sorted|group\w*|first|last|start\w*|begin\w*|end\w*|"
    r"finish\w*|"
    r"olmasin|olmayacak|degil|deme|demiyorum|istemiyorum|yerine|haric|ama|fakat|sadece|"
    r"kronolojik|cekildig\w*|eklendig\w*|ters|ilk|son|basla\w*|bitir\w*|ayni)\b"
)


# A requirement the extractor filed with no ``facts.key`` (stored before the brief prompt asked
# for it, e.g. prod 29 Ekim r1) is read once, here, and the canonical key + seed are stamped
# onto the stored row (`stamp_order_facts`), so the words are never re-read. The brief prompt now
# emits ``facts.key = "random" | "any_order"``; this fallback only exists for those older rows,
# so it is deliberately narrow: any negation, contrast or second order rule beside the cue
# means "not a plain ask" and the requirement stays unknown rather than being guessed.
def _plain_cue(text: str) -> OrderMode | None:
    text = text.replace("\u2019", "'")
    random_hit = _RANDOM_CUES.search(text)
    any_hit = _ANY_CUES.search(text)
    if not (random_hit or any_hit):
        return None
    residue = _ANY_CUES.sub(" ", _RANDOM_CUES.sub(" ", text))
    if _NOT_PLAIN.search(residue):
        return None
    return "random" if random_hit else "any"


def order_mode(req: Any) -> OrderMode | None:
    """``"random"`` / ``"any"`` when an order requirement asks for exactly that, else None.

    An explicit fact key decides. A key-less requirement is judged by the creator's words only
    when they are a plain ask (see `_plain_cue`). Any other key (capture time, attachment, a
    route) is a different rule and returns None.
    """
    if getattr(req, "kind", None) != "order":
        return None
    facts = getattr(req, "facts", None) or {}
    key = _key(facts)
    if key:
        if key in RANDOM_ORDER_KEYS:
            return "random"
        if key in ANY_ORDER_KEYS:
            return "any"
        return None
    if any(facts.get(name) for name in ("first_clip", "last_clip")):
        # Clip pins are the clip-intent resolver's; they are not an order mode.
        return None
    return _plain_cue(
        loose_text(
            f"{getattr(req, 'description', None) or ''} {getattr(req, 'literal', None) or ''}"
        )
    )


def derived_seed(req_id: str | None, turn_id: str | None) -> int:
    """A stable seed for a requirement that carries none: same ask, same permutation."""
    digest = hashlib.sha256(f"random-order|{turn_id or ''}|{req_id or ''}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % _SEED_BOUND


def random_order_seed(req: Any) -> int:
    """The seed stored on the requirement, else the one derived from its identity."""
    seed = (getattr(req, "facts", None) or {}).get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed < _SEED_BOUND:
        return seed
    return derived_seed(getattr(req, "id", None), getattr(req, "source_turn_id", None))


def seeded_permutation(items: Sequence[Any], seed: int) -> list[Any]:
    """``items`` in the seeded random order: identical for the same seed, never the input order.

    Fewer than two items have no other order. When the shuffle lands on the input order the
    result is rotated by one, so "random" is always visibly a change.
    """
    out = list(items)
    random.Random(seed).shuffle(out)
    if len(out) > 1 and out == list(items):
        out = out[1:] + out[:1]
    return out


def stamp_order_facts(
    kind: str | None,
    description: str | None,
    literal: str | None,
    facts: Mapping[str, Any],
    *,
    req_id: str | None,
    turn_id: str | None,
) -> dict[str, Any]:
    """Facts with the canonical key (and, for random, the stored seed) written in.

    The seed is written once, at the turn the creator asked: re-runs and re-renders read it
    back instead of drawing again, which is what makes the shuffle idempotent.
    """
    facts = dict(facts)
    probe = _Probe(kind, description, literal, facts)
    mode = order_mode(probe)
    if mode == "random":
        facts["key"] = RANDOM_ORDER_KEY
        seed = facts.get("seed")
        if not (isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed < _SEED_BOUND):
            facts["seed"] = derived_seed(req_id, turn_id)
    elif mode == "any":
        facts["key"] = ANY_ORDER_KEY
    return facts


class _Probe:
    def __init__(
        self, kind: str | None, description: str | None, literal: str | None, facts: dict[str, Any]
    ) -> None:
        self.kind, self.description, self.literal, self.facts = kind, description, literal, facts
