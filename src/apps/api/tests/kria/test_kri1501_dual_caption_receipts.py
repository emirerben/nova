"""KRI-1501: what Kria promises before approval, and says after the render, about a second
(translated) caption line.

Contract: docs/pipelines/dual-captions.md. The real parser and the real plan / receipt builders
run on the creator's words; only the gate settings are set per test.

Ways this can go wrong, written down before the code:

Before approval (phone Talking draft)
 1. The dual ask is read as a single caption language, so the draft promises "they'll be in
    English" and the whole video is translated.
 2. The gate is off (flag, device check or an old app build) but the draft still promises the
    second line: a promise the render will not keep. With the gate off it must say plainly
    that the line can't be drawn, and the captions stay in the spoken language.
 3. An old app build is told "not yet" with no hint that it is the app version.
 4. A language outside the four (Spanish) is promised as a second line.
 5. The gate-off sentence is treated as a promise (`talking_caption_promise`), so approval is
    not held; or the gate-on sentence is NOT a promise, so a doable ask holds the draft.
 6. The sentence is English in a Turkish or German chat (hard-coded English).
 7. KRI-1015 / single-language asks change ("Turkish captions" stops saying "in Turkish";
    "keep every language as spoken" starts naming a second line).

After the render (the variant's `caption_translation`)
 8. "Done" for a line that was not drawn: no record, outcome "unmet", the switch off in the
    editor, or a record that says "translated" while no cue carries a translation.
 9. "Done" for a line with dropped cues (outcome "partial").
10. "Done" for a line in the wrong language (the record's language is not the one asked, or
    the translated text reads as another language).
11. A creator who speaks the only language they named gets "no line" with no reason.
12. The receipt is English in a Turkish chat.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    _requested_caption_language,
    build_receipts,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    talking_caption_promise,
)
from app.kria.reply_language import reply_language_for

DUAL_EN = "Turkish captions with English under it"
STRATEGY = {
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "caption_style": "clean",
    "target_duration_s": 30,
}
EVERY_VOICE = "Every voice in your take gets captions, off-camera ones too"


def _req(description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="style", scope="global", description=description, facts=dict(facts)
    )


def _draft(protocol: int | None = None) -> PlanFacts:
    """The facts a phone Talking strategy draft is checked against (`kria_runtime`)."""
    if protocol is None:
        protocol = settings.dual_captions_min_client_protocol
    # As `kria_runtime` builds a draft turn's facts: the sending build rides on the facts.
    return dataclasses.replace(
        plan_facts_from_strategy(STRATEGY),
        phone_talking=True,
        caption_words_at_render=True,
        client_protocol=protocol,
    )


def _one(req: BriefRequirement, facts: PlanFacts):
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dual_captions_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "dualCaptions"],
    )


@pytest.fixture
def gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dual_captions_enabled", False)


# -- 1, 5: the gate on: a promise --------------------------------------------------------------

PROMISE_EN = (
    f"{EVERY_VOICE}; they'll be in the language that's spoken, with a smaller line in English "
    "under each one, and I'll check that in the finished video"
)


@pytest.mark.parametrize(
    ("description", "facts"),
    [
        (DUAL_EN, {}),
        ("add an English translation under the captions", {}),
        ("altyazının altına İngilizce çeviri", {}),
        # A German description is a captions ask through the brief's recorded fact (KRI-667).
        ("zweisprachige Untertitel, Englisch darunter", {"captions": "on"}),
    ],
)
def test_gate_on_promises_the_second_line(gate_on, description, facts):
    req = _req(description, **facts)
    assert _requested_caption_language(req) is None  # 1: never one single caption language
    receipt = _one(req, _draft())
    assert receipt.reason == PROMISE_EN
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)
    assert talking_caption_promise(req, _draft()) == PROMISE_EN


def test_gate_on_names_the_first_line_language(gate_on):
    receipt = _one(_req("English captions with a German translation underneath"), _draft())
    assert "with a smaller line in German under each one" in receipt.reason


# -- 2, 3, 4, 5: the gate off / an old build / an unsupported language: no promise -------------


def test_gate_off_says_the_line_cant_be_drawn(gate_off):
    req = _req(DUAL_EN)
    receipt = _one(req, _draft())
    assert receipt.reason == (
        f"{EVERY_VOICE}; I can't add a second line in English under the captions yet, so "
        "they'll only be in the language that's spoken"
    )
    assert talking_caption_promise(req, _draft()) is None
    assert "smaller" not in receipt.reason


def test_an_old_app_build_is_told_it_is_the_app_version(gate_on):
    req = _req(DUAL_EN)
    old = settings.dual_captions_min_client_protocol - 1
    receipt = _one(req, _draft(protocol=old))
    assert receipt.reason == (
        f"{EVERY_VOICE}; this app version can't show a second line in English under the "
        "captions, so they'll only be in the language that's spoken"
    )
    assert talking_caption_promise(req, _draft(protocol=old)) is None


def test_the_device_check_missing_is_gate_off(monkeypatch):
    monkeypatch.setattr(settings, "dual_captions_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "dualCaptions"],
    )
    req = _req(DUAL_EN)
    assert talking_caption_promise(req, _draft()) is None
    assert "can't add a second line in English" in _one(req, _draft()).reason


def test_a_language_outside_the_four_is_never_promised(gate_on):
    req = _req("Turkish captions with Spanish under it")
    receipt = _one(req, _draft())
    assert "I can't add a second line in Spanish under the captions yet" in receipt.reason
    assert talking_caption_promise(req, _draft()) is None


# -- 6: the chat's language --------------------------------------------------------------------


def test_turkish_chat_gets_turkish(gate_on):
    with reply_language_for("tr"):
        receipt = _one(_req(DUAL_EN), _draft())
    assert receipt.reason.endswith(
        "altyazılar konuşulan dilde olacak, her birinin altında daha küçük bir İngilizce satır "
        "olacak, bunu bitmiş videoda kontrol edeceğim"
    )


@pytest.mark.parametrize("on", [True, False])
def test_german_chat_gets_german(monkeypatch, on):
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    monkeypatch.setattr(settings, "dual_captions_enabled", on)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "dualCaptions"],
    )
    with reply_language_for("de"):
        receipt = _one(_req(DUAL_EN), _draft())
    assert receipt.reason.startswith("Jede Stimme in deinem Take bekommt Untertitel")
    assert "Englisch" in receipt.reason
    for english in ("line", "spoken", "captions", "English"):
        assert english not in receipt.reason, receipt.reason


# -- 7: unchanged asks ---------------------------------------------------------------------------


def test_a_single_language_ask_is_unchanged(gate_on):
    receipt = _one(_req("Turkish captions"), _draft())
    assert receipt.reason == (
        f"{EVERY_VOICE}; they'll be in Turkish, and I'll check that in the finished video"
    )


def test_keep_spoken_is_unchanged(gate_on):
    req = _req("English captions for the English, Turkish captions for my Turkish")
    assert "line under" not in (_one(req, _draft()).reason or "")


# -- 8-12: the finished video -------------------------------------------------------------------

_TR_CUES = [
    {"id": "c1", "text": "Merhaba, bugün size en sevdiğim kafeyi göstereceğim.", "start_s": 0.0,
     "end_s": 3.0, "translation": "Hi, today I'll show you my favourite café."},
    {"id": "c2", "text": "Burada her sabah kahve içiyorum.", "start_s": 3.0, "end_s": 5.0,
     "translation": "I have coffee here every morning."},
]  # fmt: skip


def _variant(cues=None, translation=None, language="tr") -> dict:
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_language": language,
        "voiceover_caption_style": "sentence",
        "caption_cues": cues if cues is not None else _TR_CUES,
    }
    if translation is not None:
        variant["caption_translation"] = translation
    return variant


def _record(**changes) -> dict:
    return {
        "enabled": True,
        "language": "en",
        "source_language": "tr",
        "color": "#F5D33C",
        "outcome": "translated",
        "dropped_cue_ids": [],
        **changes,
    }


def _review(variant: dict, description: str = DUAL_EN):
    return _one(_req(description), plan_facts_from_phone_variant(variant))


def test_a_drawn_line_is_done():
    receipt = _review(_variant(translation=_record()))
    assert (receipt.status, receipt.reason) == (
        "met",
        "Each caption has a smaller line in English under it.",
    )


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        (_variant(), "There's no line in English under the captions."),
        (
            _variant(translation=_record(outcome="unmet")),
            "I couldn't translate the captions into English, so there's no line in English "
            "under them.",
        ),
        (
            # "translated" but no cue carries a line: never claimed
            _variant(
                cues=[{k: v for k, v in c.items() if k != "translation"} for c in _TR_CUES],
                translation=_record(),
            ),
            "I couldn't translate the captions into English, so there's no line in English "
            "under them.",
        ),
        (
            _variant(translation=_record(enabled=False)),
            "The line in English under the captions is switched off.",
        ),
        (
            _variant(
                cues=[_TR_CUES[0], {k: v for k, v in _TR_CUES[1].items() if k != "translation"}],
                translation=_record(outcome="partial", dropped_cue_ids=["c2"]),
            ),
            "1 caption has no line in English under it.",
        ),
        (
            _variant(translation=_record(language="de")),
            "The line under the captions is in German, not English.",
        ),
        (
            _variant(
                cues=[
                    {**cue, "translation": cue["text"]}  # the "translation" is still Turkish
                    for cue in _TR_CUES
                ],
                translation=_record(),
            ),
            "The line under the captions is in Turkish, not English.",
        ),
    ],
)
def test_a_line_that_was_not_drawn_is_never_done(variant, reason):
    receipt = _review(variant)
    assert receipt.status == "partial"
    assert receipt.reason == reason


def test_a_creator_who_speaks_the_only_named_language_is_told_why():
    variant = _variant(
        cues=[{"id": "c1", "text": "Today I'll show you my favourite café.", "start_s": 0.0,
               "end_s": 3.0}],
        language="en",
    )  # fmt: skip
    receipt = _review(variant, "add an English translation under the captions")
    assert receipt.status == "partial"
    assert receipt.reason == (
        "You speak English, so there's no other language to put under the captions."
    )


def test_the_receipt_in_a_turkish_chat():
    with reply_language_for("tr"):
        receipt = _review(_variant(translation=_record()))
    assert receipt.reason == "Her altyazının altında daha küçük bir İngilizce satır var."
