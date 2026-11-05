"""N4 and N5 for every kind and both languages: what the creator said wins, what reaches the screen.

Failure catalogue (written first; each row is a line of "Negative cases" in the report):

  W1  a card undoes what the creator asked for (KRI-129). A live requirement that speaks about the
      dimension (captions, music, sound effects, transitions) withholds the card, in English and in
      Turkish wording, on the real GET, and only that card: the other cards of the thread stay.
      Proven with a precondition: the same thread WITHOUT the requirement does serve the card, so
      the absence is the creator's word at work and not a thread that never offered it.
  W2  hook copy is a way to put words on the creator's video that nobody wrote. Every way around the
      copy gate stays shut: a bare domain, a domain with spaces around the dot, a slash, "dot com",
      a handle, a hashtag, digits, a paraphrase of what the creator said in the clip. A rejected
      text never becomes a card (Gemini metadata never becomes on-screen text).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.kria.brief import BriefUpdate
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

CASSETTES = Path(__file__).resolve().parent / "cassettes"


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def serve_with(world, key, *updates, prefs_override=None):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, EDITOR)
    if updates:
        k.write_brief(seeded, *updates)
    phone = Phone(seeded, world, prefs_override or prefs(key), language=scn.language)
    return await phone.serve()


def said(kind: str, scope: str, text: str, **facts):  # noqa: ANN003, ANN202
    return BriefUpdate(operation="add", kind=kind, scope=scope, description=text, facts=facts)


KEYS = {"en": "nermin-matcha-update", "tr": "nermin-matcha-update/tr"}
# (kind, language, the creator's own words, the requirement they became, a known gap or None).
CREATOR_SAID = [
    (
        "captions",
        "en",
        "typed fact",
        said("style", "global", "no captions on this one", captions="off"),
        None,
    ),
    ("captions", "tr", "own words", said("style", "global", "altyazı istemiyorum"), None),
    (
        "music",
        "en",
        "typed fact",
        said("audio", "global", "drop the music entirely", audio_intent="no_music"),
        None,
    ),
    ("music", "tr", "müzik olmasın", said("audio", "global", "müzik olmasın"), None),
    ("music", "tr", "sesi kıs", said("audio", "global", "sesi kıs lütfen"), None),
    # The accusative of "müzik" is "müziği" (k softens to ğ), the commonest way to say it.
    ("music", "tr", "müziği kıs", said("audio", "global", "müziği çok kıs"), None),
    ("sfx", "en", "own words", said("audio", "global", "no sound effects please"), None),
    ("sfx", "tr", "own words", said("audio", "global", "ses efekti olmasın"), None),
    (
        "transition",
        "en",
        "own words",
        said("style", "global", "hard cuts only, no transitions"),
        None,
    ),
    (
        "transition",
        "tr",
        "own words",
        said("style", "global", "geçiş olmasın, sadece sert kesme"),
        None,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "language", "label", "update", "gap"),
    CREATOR_SAID,
    ids=[f"{k_}-{lang}-{label}" for k_, lang, label, _u, _g in CREATOR_SAID],
)
async def test_w1_the_creators_request_withholds_the_card(
    talking_world, kind, language, label, update, gap
):
    rec = Recorder(f"negative W1 {kind} {language} {label}")
    key = KEYS[language]
    before = [c.kind for c in (await serve_with(talking_world, key)).suggestions]
    after = [c.kind for c in (await serve_with(talking_world, key, update)).suggestions]
    rec.negative(
        f"the creator already spoke about {kind} ({language}, {label}): {kind} is withheld",
        key,
        f"{kind} served without the requirement, absent with it, the other cards untouched",
        kind in before and kind not in after and [x for x in before if x != kind] == after,
        f"without {before}; with {after}",
        gap=gap,
    )
    rec.finish()


COPY = [
    ("a bare domain", "Order at matcha.shop today"),
    ("a domain with spaces around the dot", "Order at matcha . shop today"),
    ("a slash", "Order at matcha/shop today"),
    ("dot com spelled out", "Order at matcha shop dot com"),
    ("a handle", "Follow @matchaclub now"),
    ("a hashtag", "Whisk first #matcha"),
    ("digits", "Matcha in 3 easy steps"),
    ("a paraphrase of the clip's speech", "Restock landed so I am packing orders"),
]
COPY_TR = [
    ("a bare domain", "Matcha için matcha.shop adresine gel"),
    ("a paraphrase of the clip's speech", "Stok geldi ilk siparişleri paketliyorum"),
    ("a handle", "Beni takip et @matchaclub"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "label", "text"),
    [("nermin-matcha-update", label, text) for label, text in COPY]
    + [("nermin-matcha-update/tr", label, text) for label, text in COPY_TR],
)
async def test_w2_copy_that_may_not_reach_the_screen(talking_world, key, label, text):
    rec = Recorder(f"negative W2 hook copy {key} {label}")
    swapped = [{**p, "copy": text} if p["kind"] == "hook" else p for p in copy.deepcopy(prefs(key))]
    out = await serve_with(talking_world, key, prefs_override=swapped)
    kinds = [c.kind for c in out.suggestions]
    rec.negative(
        f"hook copy with {label} never becomes a card ({key})",
        key,
        "no hook card",
        "hook" not in kinds,
        f"served {kinds}",
    )
    rec.finish()
