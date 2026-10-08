"""Creative Brief receipts for phone Talking edits: reaction beats, the closing
shot, and "keep my whole take" (follow-up to KRI-188 / KRI-178).

The prod reply for the "best food in Europe" prompt showed two "Partly:" lines
even though the draft carried verifiable beats and a single full-length clip.
These pin that both are now judged from the drafted strategy, deterministically.
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
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    _SOUND_RE,
    MAX_REPLY_CHARS,
    PlanFacts,
    _wants_beats,
    build_receipts,
    check_requirement,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.services.creator_capabilities import (
    CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS,
    repair_creator_reaction_beats,
)

_IMAGES = {
    "asset-badge.png": "BEST FOOD IN EUROPE badge",
    "asset-pasta.png": "Pasta",
    "asset-pizza.png": "Pizza",
    "asset-paella.png": "Paella",
    "asset-labomba.png": "La Bomba",
    "asset-check.png": "green check",
    "asset-x.png": "red X",
    "asset-mypick.png": "MY PICK badge",
}


def _manifest(*, beats_available: bool = True, videos: tuple[str, ...] = ("phone-a",)):
    media: list[dict[str, Any]] = [{"media_id": v, "kind": "video"} for v in videos]
    media += [{"media_id": k, "kind": "image", "label": v} for k, v in _IMAGES.items()]
    return ResolvedCreatorManifest(
        item_id=str(uuid.uuid4()),
        edit_format="subtitled",
        render_program="guided",
        media=media,
        capabilities={
            "dispatch_render": CapabilityAvailability(available=True),
            "reaction_beats": (
                CapabilityAvailability(available=True)
                if beats_available
                else CapabilityAvailability(available=False, reason_code="phone_talking_only")
            ),
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _beat(beat_id: str, trigger: str, visual: str | None = None, sound: str | None = None):
    beat: dict[str, Any] = {"beat_id": beat_id, "trigger": trigger}
    if visual is not None:
        beat["visual_id"] = visual
    if sound is not None:
        beat["sound"] = sound
    return beat


_FOOD_BEATS = [
    _beat("europe", "Europe", "BEST FOOD IN EUROPE badge"),
    _beat("pasta", "pasta", "Pasta"),
    _beat("pasta-ok", "pasta", "green check", sound="ding"),
    _beat("pizza", "pizza", "Pizza"),
    _beat("pizza-no", "pizza", "red X", sound="buzzer"),
    _beat("paella", "Paella", "asset-paella.png"),
    _beat("labomba", "La Bomba", "La Bomba"),
]


def _strategy(beats=None, closing=None, **extra) -> dict[str, Any]:  # noqa: ANN001, ANN003
    data: dict[str, Any] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        **extra,
    }
    if beats is not None:
        data["reaction_beats"] = beats
    if closing is not None:
        data["closing_media"] = closing
    # The draft stores exactly what the tool arguments validated to.
    return CreativeStrategy.model_validate(data).model_dump(mode="json", exclude_none=True)


def _req(kind: str, scope: str = "global", **kw) -> BriefRequirement:  # noqa: ANN003
    return BriefRequirement(id=kw.pop("id", "r1"), kind=kind, scope=scope, **kw)


_POPINS = _req(
    "style",
    description=(
        "Use my uploaded stickers and food photos and pop them up at the exact words, "
        "with sounds and word-by-word karaoke captions"
    ),
    facts={"triggers": ["Europe", "pasta", "pizza", "Paella", "La Bomba"]},
)
_WHOLE_TAKE = _req(
    "timing", id="r2", description="Keep my whole take exactly as recorded, start to finish"
)
_CLOSING = _req(
    "style",
    scope="title",
    id="r3",
    description="Finish on the La Bomba photo with the MY PICK badge, no extra clip after it",
)
_CLOSING_MEDIA = {"visual_id": "La Bomba", "badge_visual_id": "MY PICK badge"}


# ------------------------------------------------------------------ facts


def test_facts_carry_beats_resolved_exactly_as_approval_keeps_them() -> None:
    beats = [*_FOOD_BEATS, _beat("gluhwein", "Glühwein", "Glühwein photo")]
    strategy = _strategy(beats, _CLOSING_MEDIA)
    facts = plan_facts_from_strategy(strategy, manifest=_manifest())

    assert facts.reaction_beats_available is True
    assert facts.reaction_beat_count == len(_FOOD_BEATS)
    assert "asset-pasta.png" in facts.beat_visual_ids  # label resolved to the owned id
    assert "asset-paella.png" in facts.beat_visual_ids  # id kept as-is
    assert facts.beat_sound_triggers == ("pasta", "pizza")
    assert facts.dropped_beat_triggers == ("Glühwein",)  # no owned image by that name
    assert facts.closing_visual_id == "asset-labomba.png"
    assert facts.closing_badge_id == "asset-mypick.png"
    # Parity pin: the same repair approval runs keeps the same beats.
    repaired, _ = repair_creator_reaction_beats(
        _manifest(), CreativeStrategy.model_validate(strategy)
    )
    assert [b.trigger for b in repaired.reaction_beats or []] == [
        b.trigger for b in facts.reaction_beats or ()
    ]


def test_facts_without_a_manifest_claim_nothing_about_beats() -> None:
    facts = plan_facts_from_strategy(_strategy(_FOOD_BEATS))
    assert facts.reaction_beats is None and facts.reaction_beat_count is None
    assert facts.video_clip_count is None
    assert facts.edit_format == "subtitled"


def test_video_clip_count_ignores_visuals_and_honours_selection() -> None:
    manifest = _manifest(videos=("phone-a", "phone-b"))
    assert plan_facts_from_strategy(_strategy(), manifest=manifest).video_clip_count == 2
    picked = _strategy(selected_media_ids=["phone-b", "asset-pasta.png"], media_scope="selected")
    assert plan_facts_from_strategy(picked, manifest=manifest).video_clip_count == 1


# ------------------------------------------------------------- pop-ins


def test_popins_met_when_every_named_word_has_a_beat() -> None:
    facts = plan_facts_from_strategy(_strategy(_FOOD_BEATS), manifest=_manifest())
    receipt = check_requirement(_POPINS, facts)
    assert (receipt.status, receipt.reason) == ("met", None)


def test_popins_partial_names_the_missing_and_unresolved_words() -> None:
    req = _POPINS.model_copy(
        update={"facts": {"triggers": ["pasta", "Currywurst", "Glühwein", "La Bomba"]}}
    )
    beats = [*_FOOD_BEATS, _beat("gluhwein", "Glühwein", "Glühwein photo")]
    facts = plan_facts_from_strategy(_strategy(beats), manifest=_manifest())
    receipt = check_requirement(req, facts)
    assert receipt.status == "partial"
    assert "No pop-in for Currywurst, Glühwein" in (receipt.reason or "")
    # Glühwein is named once, not again as "couldn't find its photo".
    assert (receipt.reason or "").count("Glühwein") == 1


def test_popin_whose_photo_did_not_resolve_is_reported_even_unnamed() -> None:
    req = _POPINS.model_copy(update={"facts": {}})
    beats = [*_FOOD_BEATS, _beat("pork", "pork", "MEH stamp")]
    receipt = check_requirement(
        req, plan_facts_from_strategy(_strategy(beats), manifest=_manifest())
    )
    assert receipt.status == "partial"
    assert receipt.reason == "I couldn't find the photo or sticker for pork."


def test_quoted_words_count_as_named_triggers_when_facts_have_none() -> None:
    req = _req("audio", description='Play a buzzer when I say "pizza" and a ding at "Paella"')
    facts = plan_facts_from_strategy(
        _strategy([_beat("pizza-no", "pizza", sound="buzzer")]), manifest=_manifest()
    )
    receipt = check_requirement(req, facts)
    assert receipt.status == "partial"
    assert receipt.reason == "No pop-in for Paella."


def test_asked_sound_but_no_beat_plays_one() -> None:
    silent = [_beat("pasta", "pasta", "Pasta")]
    req = _req("style", description="When I say pasta, show the photo and play a ding")
    receipt = check_requirement(
        req, plan_facts_from_strategy(_strategy(silent), manifest=_manifest())
    )
    assert receipt.status == "partial"
    assert receipt.reason == "None of the pop-ins plays a sound."


def test_no_beats_or_capability_off_is_not_possible() -> None:
    none = check_requirement(_POPINS, plan_facts_from_strategy(_strategy(), manifest=_manifest()))
    assert none.status == "not_possible"
    assert none.reason == "This draft has no pop-ins timed to your words."
    off = check_requirement(
        _POPINS,
        plan_facts_from_strategy(_strategy(_FOOD_BEATS), manifest=_manifest(beats_available=False)),
    )
    assert off.status == "not_possible"
    assert "aren't available for this edit yet" in (off.reason or "")


def test_turkish_popin_request_is_recognised() -> None:
    req = _req("style", description="Pizza dediğimde kırmızı X çıkartmasını göster")
    facts = plan_facts_from_strategy(_strategy(_FOOD_BEATS), manifest=_manifest())
    assert check_requirement(req, facts).status == "met"


# ------------------------------------------- KRI-540: Turkish passive / word forms

_KADIKOY_VIDEO = "asset-153fa107-90a1-42ac-8dcb-9ec23f4693e7"


def _kadikoy_manifest(*, video_cards: bool = True):
    manifest = _manifest()
    data = manifest.model_dump(mode="json")
    data["media"].append({"media_id": _KADIKOY_VIDEO, "kind": "video", "label": "kahve demleme"})
    if video_cards:
        data["capabilities"][CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS] = {"available": True}
    return ResolvedCreatorManifest.model_validate(data)


_KADIKOY_BEATS = [
    {
        "beat_id": "kahve",
        "trigger": "kahve",
        "occurrence": "every",
        "visual_role": "sticker",
        "sound": "küçük bir fincan sesi",
    },
    {
        "beat_id": "ilk-durak",
        "trigger": "İlk durak",
        "visual_id": _KADIKOY_VIDEO,
        "occurrence": "first",
        "visual_role": "photo",
    },
]
_KADIKOY_SOUND = _req("audio", description="her 'kahve' kelimesinde küçük bir fincan sesi", id="r1")
_KADIKOY_VIDEO_REQ = _req(
    "select",
    scope=f"clip:{_KADIKOY_VIDEO}",
    id="r2",
    description="'İlk durak' dendiğinde köşede küçük gösterilecek kahve demleme videosu",
)


def test_kadikoy_turkish_sound_and_video_beats_are_judged_not_unchecked() -> None:
    manifest = _kadikoy_manifest()
    facts = plan_facts_from_strategy(_strategy(_KADIKOY_BEATS), manifest=manifest)
    assert facts.reaction_beat_count == 2  # the video visual survives approval's repair
    for req in (_KADIKOY_SOUND, _KADIKOY_VIDEO_REQ):
        receipt = check_requirement(req, facts)
        assert receipt.status == "met", (req.id, receipt.reason)
        assert receipt.verification != "unchecked"


def test_kadikoy_video_beat_without_video_cards_is_not_claimed_as_met() -> None:
    # Video cards off: approval drops the İlk durak beat, so the receipt must say so.
    manifest = _kadikoy_manifest(video_cards=False)
    facts = plan_facts_from_strategy(_strategy(_KADIKOY_BEATS), manifest=manifest)
    assert facts.dropped_beat_triggers == ("İlk durak",)
    receipt = check_requirement(_KADIKOY_VIDEO_REQ, facts)
    assert receipt.status != "met"
    assert receipt.verification != "unchecked"


@pytest.mark.parametrize(
    "description",
    [
        "'İlk durak' dendiğinde köşede video",
        "her 'kahve' kelimesinde küçük bir fincan sesi",
        "her 'kahve'de fincan sesi",
        "her \u201ckahve\u201dda fincan sesi",
        "her kahve'de fincan sesi",
        "'kahve' geçtiğinde fincan sesi",
        "kahve derken bir ses",
        "'kahve' denince fincan sesi",
        "KAHVE KELİMESİNİ duyunca zil çal",
        "kahve sözcüğünde bir ses",
        "Ilk durak dendiginde video goster",
    ],
)
def test_turkish_passive_and_word_forms_are_beat_cues(description: str) -> None:
    assert _wants_beats(_req("style", description=description)) is True


@pytest.mark.parametrize(
    "description",
    ["use her photo as the title card", "pop her photo up at the start"],
)
def test_english_her_is_not_a_turkish_every_cue(description: str) -> None:
    assert _wants_beats(_req("style", description=description)) is False


@pytest.mark.parametrize("text", ["fincan sesi", "zil sesini çal", "kuş sesleri", "ses"])
def test_sound_cue_is_suffix_tolerant(text: str) -> None:
    assert _SOUND_RE.search(text)


def test_sound_cue_does_not_match_silent() -> None:
    assert not _SOUND_RE.search("sessiz")
    assert not _SOUND_RE.search("sessiz bir video olsun")


def test_plain_style_requirement_still_has_no_checker() -> None:
    req = _req("style", description="Make it feel warm and cosy")
    facts = plan_facts_from_strategy(_strategy(_FOOD_BEATS), manifest=_manifest())
    receipt = check_requirement(req, facts)
    assert receipt.reason == "I can't verify this one automatically yet."


# -------------------------------------------------------------- closing


def test_closing_shot_met_missing_and_unresolved() -> None:
    manifest = _manifest()
    met = check_requirement(
        _CLOSING, plan_facts_from_strategy(_strategy(closing=_CLOSING_MEDIA), manifest=manifest)
    )
    assert met.status == "met"
    unset = check_requirement(_CLOSING, plan_facts_from_strategy(_strategy(), manifest=manifest))
    assert (unset.status, unset.reason) == (
        "not_possible",
        "This draft doesn't end on the photo you asked for.",
    )
    no_badge = {"visual_id": "La Bomba", "badge_visual_id": "GOAT badge"}
    badge = check_requirement(
        _CLOSING, plan_facts_from_strategy(_strategy(closing=no_badge), manifest=manifest)
    )
    assert (badge.status, badge.reason) == (
        "partial",
        "I couldn't find the closing badge you named.",
    )


# ------------------------------------------------------------ whole take


@pytest.mark.parametrize(
    ("videos", "cleanup", "edit_format", "status"),
    [
        (("phone-a",), False, "subtitled", "met"),
        (("phone-a",), True, "subtitled", "partial"),
        (("phone-a", "phone-b"), False, "subtitled", "partial"),
        (("phone-a",), False, "montage", "partial"),
    ],
)
def test_whole_take_on_single_clip_subtitled_is_met(videos, cleanup, edit_format, status) -> None:  # noqa: ANN001
    facts = plan_facts_from_strategy(
        _strategy(edit_format=edit_format),
        manifest=_manifest(videos=videos),
        speech_cleanup_enabled=cleanup,
    )
    assert check_requirement(_WHOLE_TAKE, facts).status == status


def test_turkish_whole_take_is_recognised() -> None:
    req = _req("timing", description="Videoyu baştan sona olduğu gibi bırak")
    facts = plan_facts_from_strategy(_strategy(), manifest=_manifest())
    assert check_requirement(req, facts).status == "met"


def test_timing_without_a_number_or_whole_take_is_unchanged() -> None:
    req = _req("timing", description="Fast but readable")
    receipt = check_requirement(req, PlanFacts(edit_format="subtitled", video_clip_count=1))
    assert receipt.reason == "I can't verify this timing automatically."


# ----------------------------------------------------------------- reply


def test_food_prompt_reply_has_no_partly_lines_and_keeps_the_summary() -> None:
    brief = CreativeBrief(version=1, requirements=[_POPINS, _WHOLE_TAKE, _CLOSING])
    facts = plan_facts_from_strategy(
        _strategy(_FOOD_BEATS, _CLOSING_MEDIA),
        manifest=_manifest(),
        speech_cleanup_enabled=False,
    )
    receipts = build_receipts(brief.live(), facts)
    assert [r.status for r in receipts] == ["met", "met", "met"]
    reply = reply_from_receipts(brief, receipts, summary="Your food tour is drafted.")
    assert reply.startswith("Your food tour is drafted.\n- Done:")
    assert "Partly" not in reply


def test_unknown_facts_stay_neutral_in_the_reply() -> None:
    # No manifest: nothing verified, so no receipts, no "Partly" lines, just the summary.
    brief = CreativeBrief(version=1, requirements=[_POPINS, _WHOLE_TAKE])
    receipts = build_receipts(brief.live(), plan_facts_from_strategy(_strategy(_FOOD_BEATS)))
    assert receipts == []
    assert reply_from_receipts(brief, receipts, summary="Drafted.") == "Drafted."


def test_real_misses_turn_the_reply_into_a_failure_notice_within_the_cap() -> None:
    triggers = [f"word{i}" for i in range(24)]
    req = _POPINS.model_copy(update={"facts": {"triggers": triggers}})
    brief = CreativeBrief(version=1, requirements=[req, _WHOLE_TAKE])
    facts = plan_facts_from_strategy(_strategy(_FOOD_BEATS), manifest=_manifest())
    receipts = build_receipts(brief.live(), facts)
    assert receipts[0].status == "partial"
    assert "and 18 more" in (receipts[0].reason or "")
    reply = reply_from_receipts(brief, receipts, summary="Drafted.")
    assert reply.startswith("Not everything you asked for made it in")
    assert len(reply) <= MAX_REPLY_CHARS
