"""KRI-688: "End on the short arrival clip" is an ordering ask, never a closing photo.

Thread 1390E686 (Tigre night boat ride, 2026-10-15, iPhone montage). The brief ledger filed
"Replace the engine noise with calm lo-fi music. End on the short arrival clip with its
own sound" as ONE `audio/global` requirement (r5), next to a separate `order` requirement
(r6, `last_clip`) that already carried the ending. The closing-shot check
(`_wants_closing`) fired on the words "End on", found no closing photo in the draft (there
never was one to find: the ending is a VIDEO clip, and a montage cannot place a closing
photo at all - the `reaction_beats` capability is Talking-only) and answered "This draft
doesn't end on the photo you asked for". That checked "Couldn't" made the first draft a
hold-back ("different approach, or simpler version?"), and the simpler version would have
dropped the ending along with the music.

Failure modes pinned here, through the real path (brief -> `requirements_to_check_at_draft`
-> `build_receipts` -> `needs_creator_choice` -> `reply_from_receipts`):

1. an "End on <clip>" sentence inside a style/audio/select requirement is judged as a closing
   photo on an edit that cannot place one;
2. that verdict is checked + unmet, so `needs_creator_choice` holds the whole first draft back;
3. the reason says "photo" although nothing named a photo (English and Turkish);
4. an ending that names a photo, sticker or badge must still be judged as a closing photo, on
   every edit format;
5. the model's own `closing` fact still starts the closing check, and when nothing names a
   picture its reason never claims "photo";
6. a pop-in ask or "keep the original sound" ask glued to a clip ending is judged on its own
   half (the pop-ins, the sound), not blamed for the missing closing photo;
7. the picture word counts only inside the ending sentence: a sticker in another sentence of
   the same requirement does not turn "End on the arrival clip" into a photo ask;
8. a Talking edit (the one format that places a closing photo) keeps judging an unnamed
   ending, except when the model recorded the ending as a `last_clip`;
9. the music half on its own stays unchecked: no verdict, no hold-back (music is
   recommend-only).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ResolvedCreatorManifest,
)
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (  # noqa: PLC2701
    PlanFacts,
    _closing_photo_ask,
    build_receipts,
    check_requirement,
    needs_creator_choice,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.reply_language import reply_language_for

USER = uuid.uuid4()
PROXY = "users/u/creation-threads/t/analysis-proxy-ios-A.mp4"


def _req(rid: str, kind: str, scope: str, description: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope=scope, description=description, facts=facts)


# The M2 brief as the ledger wrote it (prod thread 1390E686, brief v1).
R1 = BriefRequirement(
    id="r1",
    kind="text",
    scope="title",
    description="20 minutes in the dark",
    literal="20 minutes in the dark",
)
R2 = _req("r2", "timing", "global", "20 seconds", duration_s=20)
R3 = _req(
    "r3",
    "select",
    "global",
    "pick the moments where a lit house, the ferry or the amusement park passes "
    "- eight of them, about 2 seconds each, no black-sky stretches",
)
R4 = _req("r4", "style", "global", "Speed the river shots up 2x")
R5 = _req(
    "r5",
    "audio",
    "global",
    "Replace the engine noise with calm lo-fi music. "
    "End on the short arrival clip with its own sound",
)
R6 = _req(
    "r6",
    "order",
    "global",
    "End on the short arrival clip",
    last_clip="the short clip where we arrive",
)
M2 = [R1, R2, R3, R4, R5, R6]


def _manifest(*, talking: bool = False, with_photo: bool = False) -> ResolvedCreatorManifest:
    """A montage manifest as prod built it; ``talking`` turns the Talking-only capability on."""
    media: list[dict[str, Any]] = [
        {"media_id": f"analysis-proxy-ios-{n}.mp4", "kind": "video"} for n in "ABCD"
    ]
    if with_photo:
        media.append({"media_id": "asset-bomba.png", "kind": "image", "label": "La Bomba"})
    return ResolvedCreatorManifest(
        item_id=str(uuid.uuid4()),
        edit_format="subtitled" if talking else "montage",
        render_program="guided",
        media=media,
        capabilities={
            "dispatch_render": CapabilityAvailability(available=True),
            "reaction_beats": (
                CapabilityAvailability(available=True)
                if talking
                else CapabilityAvailability(
                    available=False,
                    reason="reaction beats are only available on an iPhone talking-to-camera edit",
                    reason_code="phone_talking_only",
                )
            ),
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _strategy(audio_strategy: str = "original_audio", **extra: Any) -> dict[str, Any]:
    data = {"edit_format": "montage", "audio_strategy": audio_strategy, **extra}
    return CreativeStrategy.model_validate(data).model_dump(mode="json", exclude_none=True)


def _facts(strategy: dict[str, Any] | None = None, **manifest_kw: Any) -> PlanFacts:
    return plan_facts_from_strategy(strategy or _strategy(), manifest=_manifest(**manifest_kw))


@pytest.fixture
def unified_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])


def _draft_receipts(strategy: dict[str, Any]):
    """What the runtime does with a first draft: filter, judge, drop the unchecked."""
    kept = requirements_to_check_at_draft(
        M2,
        creator_id=USER,
        strategy=strategy,
        item_edit_format="montage",
        clip_paths=(PROXY,),
    )
    facts = plan_facts_from_strategy(strategy, manifest=_manifest())
    receipts = build_receipts(kept, facts, include_unchecked=True)
    return [r for r in receipts if r.verification != "unchecked"]


# ------------------------------------------------------------ the M2 draft, end to end


@pytest.mark.parametrize("audio", ["original_audio", "licensed_music"])
def test_the_m2_first_draft_is_not_held_back_for_a_photo_nobody_asked_for(unified_on, audio):
    receipts = _draft_receipts(_strategy(audio))

    assert [r.requirement_id for r in receipts if needs_creator_choice(r)] == []
    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=M2), receipts, summary="Here's your draft."
    )
    assert "photo" not in reply
    assert "Couldn't" not in reply
    assert reply.startswith("Here's your draft.")


def test_the_m2_draft_reply_in_turkish_never_says_photo(unified_on):
    with reply_language_for("tr"):
        receipts = _draft_receipts(_strategy())
        reply = reply_from_receipts(CreativeBrief(version=1, requirements=M2), receipts)

    assert "fotoğraf" not in reply
    assert [r.requirement_id for r in receipts if needs_creator_choice(r)] == []


# ------------------------------------- a clip ending is not a closing photo on a montage


_CLIP_ENDINGS_EN = [
    ("audio", R5.description),
    ("audio", "End on the short arrival clip with its own sound"),
    ("style", "Finish with the arrival video"),
    ("select", "Wrap up on the last take, the one where we dock"),
    ("audio", "Calm music under it all. Closing on the ferry footage."),
    ("audio", "Replace the engine noise with calm lo-fi music, and end on the arrival clip"),
    # No kind named at all: on an edit that cannot place a closing photo it is an ordering ask.
    ("style", "End on the sunset shot"),
]
_CLIP_ENDINGS_TR = [
    ("audio", "Motor sesini sakin bir lo-fi müzikle değiştir. Kısa varış klibiyle bitir."),
    ("style", "Videoyu varış klibiyle bitir"),
    ("audio", "En sonda varış çekimiyle bitir, kendi sesi de duyulsun"),
]


@pytest.mark.parametrize(("kind", "text"), [*_CLIP_ENDINGS_EN, *_CLIP_ENDINGS_TR])
def test_an_ending_on_an_edit_that_cannot_place_a_photo_is_not_a_photo_ask(kind, text):
    req = _req("r5", kind, "global", text)
    facts = _facts()

    assert _closing_photo_ask(req, facts) is False
    receipt = check_requirement(req, facts)
    assert "photo" not in (receipt.reason or "")
    assert "fotoğraf" not in (receipt.reason or "")
    assert receipt.status == "partial"  # nothing judges it: neutral, never a failure


@pytest.mark.parametrize("kind", ["audio", "style", "select"])
def test_a_clip_ending_never_reaches_the_creator_as_a_failed_photo(kind):
    req = _req("r5", kind, "global", R5.description)

    receipts = build_receipts([req], _facts(), include_unchecked=True)

    assert not any(needs_creator_choice(r) for r in receipts)


# ------------------------------------------------ a photo ending is still judged as one


_PHOTO_ENDINGS = [
    ("style", "Finish on the La Bomba photo with the MY PICK badge, no extra clip after it"),
    ("audio", "Replace the engine noise with calm lo-fi music. End on the La Bomba photo."),
    ("style", "End with my medal picture"),
    ("style", "Close with the GOAT badge"),
    ("style", "Fotoğrafla bitir"),
    ("style", "Videoyu La Bomba fotoğrafıyla bitir"),
    # A photo named beside a clip in the ending sentence is still a closing photo ask.
    ("style", "End on the La Bomba photo, not on a video clip"),
]


@pytest.mark.parametrize("talking", [False, True])
@pytest.mark.parametrize(("kind", "text"), _PHOTO_ENDINGS)
def test_an_ending_that_names_a_photo_is_still_a_closing_photo_ask(kind, text, talking):
    req = _req("r7", kind, "global", text)
    facts = _facts(talking=talking)

    assert _closing_photo_ask(req, facts) is True
    receipt = check_requirement(req, facts)
    assert receipt.status == "not_possible"
    assert needs_creator_choice(receipt.model_copy(update={"verification": "checked"}))


def test_a_photo_ending_says_photo_and_a_met_one_is_met():
    req = _req("r7", "style", "global", "End on the La Bomba photo")
    missing = check_requirement(req, _facts())
    assert missing.reason == "This draft doesn't end on the photo you asked for."

    placed = _facts(
        _strategy(closing_media={"visual_id": "La Bomba"}), talking=True, with_photo=True
    )
    assert check_requirement(req, placed).status == "met"

    with reply_language_for("tr"):
        assert check_requirement(req, _facts()).reason == (
            "Bu taslak istediğin fotoğrafla bitmiyor."
        )


# ------------------------------ the model's own `closing` fact still starts the closing check


def test_the_closing_fact_without_a_picture_word_is_checked_but_never_called_a_photo():
    req = _req("r7", "style", "global", "End on La Bomba", closing="La Bomba")
    facts = _facts()

    assert _closing_photo_ask(req, facts) is True
    receipt = check_requirement(req, facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == "This draft doesn't end the way you asked."
    with reply_language_for("tr"):
        assert check_requirement(req, facts).reason == "Bu taslak istediğin gibi bitmiyor."


def test_the_closing_fact_naming_a_photo_says_photo():
    req = _req("r7", "style", "global", "End on La Bomba", closing="the La Bomba photo")

    assert check_requirement(req, _facts()).reason == (
        "This draft doesn't end on the photo you asked for."
    )


# ----------------------- a Talking edit keeps judging an unnamed ending, unless it is a clip


def test_a_talking_edit_still_judges_an_unnamed_ending_without_calling_it_a_photo():
    req = _req("r7", "style", "global", "End on the sunset shot")
    facts = _facts(talking=True)

    assert _closing_photo_ask(req, facts) is True
    receipt = check_requirement(req, facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == "This draft doesn't end the way you asked."


def test_a_last_clip_fact_says_the_ending_is_a_clip_even_on_a_talking_edit():
    req = _req("r7", "audio", "global", "End on La Bomba", last_clip="the La Bomba clip")

    assert _closing_photo_ask(req, _facts(talking=True)) is False


# ------------------------------- the other half of a glued ask is judged on its own terms


def test_a_pop_in_ask_glued_to_a_clip_ending_is_not_blamed_for_the_missing_photo():
    req = _req(
        "r8",
        "style",
        "global",
        "Pop up the pasta sticker when I say pasta. End on the short arrival clip.",
        triggers=["pasta"],
    )

    receipt = check_requirement(req, _facts())

    assert "end on the photo" not in (receipt.reason or "").lower()
    assert "end the way" not in (receipt.reason or "").lower()
    assert "closing" not in (receipt.reason or "").lower()


def test_keep_the_original_sound_glued_to_a_clip_ending_is_judged_on_the_sound():
    """KRI-624: the sound half used to be sunk by the photo check on the ending half."""
    req = _req(
        "r9",
        "audio",
        "global",
        "Keep the original sound of the clips. End on the short arrival clip.",
    )
    facts = PlanFacts(
        rendered_montage=True,
        source_audio_kept=True,
        song_bed=False,
        reaction_beats=(),
        reaction_beats_available=False,
        clip_ids=("a", "b"),
    )

    receipt = check_requirement(req, facts)

    assert receipt.status == "met"
    assert "photo" not in (receipt.reason or "")


def test_a_sticker_in_another_sentence_does_not_make_a_clip_ending_a_photo_ask():
    req = _req(
        "r8",
        "style",
        "global",
        "Add a pasta sticker when I say pasta. End on the short arrival clip.",
    )

    assert _closing_photo_ask(req, _facts()) is False


# ------------------------------------------------ the music half on its own stays neutral


def test_a_music_ask_on_its_own_is_unchecked_and_never_holds_a_draft_back():
    req = _req("r5", "audio", "global", "Replace the engine noise with calm lo-fi music")

    receipts = build_receipts([req], _facts(), include_unchecked=True)

    assert [r.verification for r in receipts] == ["unchecked"]
    assert not any(needs_creator_choice(r) for r in receipts)
