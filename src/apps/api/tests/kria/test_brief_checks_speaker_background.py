"""KRI-1161: a green-screen ask on a phone Talking edit is judged, named, and never dropped.

Prod 2026-11-01 (TestFlight 1848, chats "Green screen" / "Green 2"): "Make the whole video like a
green screen: me small at the bottom right in front, and behind me full screen whichever photo or
video I'm talking about, changing every 3-5 seconds. ... No music." The reply listed only the
cleanup and the captions. The green-screen requirement had no checker (`unchecked`, "I can't verify
this one automatically yet", hidden from the reply), and the "No music" requirement was
`not_possible/checked` in the receipts but `unverified` in `approval_readiness`, so approval was
offered with a refusal the creator was never told about.

Ways this can fail, each pinned below (rows S36 of
`docs/reviews/speaker-background-windows-failure-modes.md`):

 1. the ask is recognised only by one wording (English, or a Turkish twin missing), or "background
    music" / "arka plan müziği" is read as a background photo;
 2. the ask with NO photo or video in the pool gets the generic Talking refusal or silence, instead
    of a named reason ("attach the photos or videos first, in Visuals");
 3. a draft promises what the repair then drops (windows an old app cannot draw, a corner it
    cannot place), or says "Done" for a plan that holds no background;
 4. a finished video that has no background is called met;
 5. readiness lists a checked refusal as "unverified" (approvable, hidden) where the receipt says
    not possible: root cause, the readiness facts were never told the draft is a phone Talking one.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    asks_speaker_background,
    build_receipts,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.services import creator_capabilities as capabilities

CREATOR = uuid.UUID("00000000-0000-4000-8000-0000000011a1")
TAKE = "analysis-proxy-ios-A.mp4"
PHOTO = "asset-5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10"
OTHER = "asset-7d4b1f30-0e5c-4b8d-9a43-9c2f6e5d4c32"
THE_ASK = (
    "Make the whole video like a green screen: me small at the bottom right in front, and behind "
    "me full screen whichever photo or video I'm talking about, changing every 3-5 seconds."
)
STRATEGY = {
    "direction": "fast_montage",
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "target_duration_s": 24,
    "selected_media_ids": [TAKE],
    "rationale": "Keep the take whole.",
}


@pytest.fixture(autouse=True)
def _gate(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackground"],
    )


def _open_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackgroundWindows"],
    )


def _media(*pool: str) -> list[dict]:
    rows: list[dict] = [{"media_id": TAKE, "kind": "video", "duration_s": 24.6}]
    rows += [{"media_id": item, "kind": "image", "label": f"{item[-4:]}.jpg"} for item in pool]
    return rows


def _manifest(*pool: str, edit_format: str = "subtitled"):
    return capabilities.resolve_creator_manifest(
        item_id="item-1161",
        edit_format=edit_format,
        media=_media(*pool),
        phone_source_media_ids=[TAKE],
        phone_rendering_allowed=True,
    )


ASK_FACTS = {"speaker_background": True, "background_changes": True, "speaker_small": True}


def _req(
    text: str = THE_ASK,
    *,
    rid: str = "r2",
    kind: str = "style",
    facts: dict | None = None,
) -> BriefRequirement:
    """The ask as the brief writer records it: the fact owns it, whatever the words (KRI-667)."""
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope="global",
        description=text,
        facts=ASK_FACTS if facts is None else facts,
    )


def _draft_facts(manifest, **strategy):
    facts = plan_facts_from_strategy({**STRATEGY, **strategy}, clip_ids=[TAKE], manifest=manifest)
    return dataclasses.replace(facts, phone_talking=True)


def _receipt(req, facts):
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


# --------------------------------------------------------------- 1. what counts as the ask


@pytest.mark.parametrize(
    "text",
    [
        "Put the beach photo behind me",
        "arkamda bu fotoğraf dursun",
        "yeşil perde gibi olsun, arkamda videolar değişsin",
    ],
)
def test_the_recorded_fact_owns_the_ask_in_any_language(text):
    assert asks_speaker_background(_req(text, facts={"speaker_background": True}))


@pytest.mark.parametrize(
    "text",
    [
        "No music",
        "Add some background music",
        "keep the background noise low",
        "arka plan müziği olmasın",
        "arkada müzik çalsın",
    ],
)
def test_background_music_is_not_a_background_ask(text):
    # Sound is never a picture behind the speaker: it records no `speaker_background` fact, and
    # nothing here reads the words (KRI-667).
    assert not asks_speaker_background(_req(text, kind="audio", facts={}))


def test_the_brief_writer_is_taught_the_fact_and_the_music_exception():
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION
    from app.kria.brief import STICKY_FACTS

    assert '"speaker_background": true' in _BRIEF_PROMPT_SECTION
    assert "background music" in _BRIEF_PROMPT_SECTION
    assert {"speaker_background", "background_changes", "speaker_small"} <= STICKY_FACTS


# --------------------------------------------------- 2. the KRI-1161 refusals name the part


def test_kri_1161_ask_without_pool_names_the_background_part(monkeypatch):
    receipt = _receipt(_req(), _draft_facts(_manifest()))

    assert receipt.status == "not_possible"
    assert receipt.verification == "checked"
    assert "Attach the photos or videos first, in Visuals" in receipt.reason
    assert "A Talking video can't do this" not in receipt.reason
    assert is_judged(_req(), receipt)
    # Not a limit of the format: the creator can attach photos, so it is a choice to put to them.
    assert needs_creator_choice(receipt)
    assert not is_format_limit(receipt.reason)


def test_the_refusal_reads_in_turkish_for_a_turkish_chat(monkeypatch):
    from app.kria.reply_language import reply_language_for

    with reply_language_for("tr"):
        receipt = _receipt(_req("arkamda fotoğraflar değişsin"), _draft_facts(_manifest()))
    assert receipt.status == "not_possible"
    assert "Visuals" in receipt.reason and "Attach" not in receipt.reason


def test_a_montage_cannot_put_a_photo_behind_the_speaker_and_says_so():
    facts = dataclasses.replace(
        plan_facts_from_strategy(
            {**STRATEGY, "edit_format": "montage"},
            clip_ids=[TAKE],
            manifest=_manifest(PHOTO, edit_format="montage"),
        ),
        phone_talking=False,
    )
    receipt = _receipt(_req(), facts)
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("Only a talking-to-camera edit made on your iPhone")
    assert is_format_limit(receipt.reason)


@pytest.mark.parametrize(
    ("gate", "needle"),
    [("flag", "isn't available on your iPhone yet"), ("token", "isn't available on your iPhone")],
)
def test_a_closed_gate_is_named_in_plain_words(monkeypatch, gate, needle):
    manifest = _manifest(PHOTO)
    if gate == "flag":
        monkeypatch.setattr(settings, "speaker_background_enabled", False)
    else:
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [f for f in settings.phone_render_verified_features if f != "speakerBackground"],
        )
    manifest = _manifest(PHOTO)
    receipt = _receipt(_req(), _draft_facts(manifest))
    assert receipt.status == "not_possible"
    assert needle in receipt.reason
    assert is_format_limit(receipt.reason)


# ----------------------------------------------------------- 3. the draft judges what it keeps


def test_a_single_background_is_met_with_the_photo_behind_the_speaker():
    manifest = _manifest(PHOTO)
    facts = _draft_facts(manifest, background_media={"media_id": PHOTO})
    receipt = _receipt(
        _req("put the beach photo behind me", facts={"speaker_background": True}), facts
    )
    assert receipt.status == "met" and receipt.verification == "checked"
    assert "sits behind you for the whole video" in receipt.reason


def test_a_draft_that_forgot_the_background_is_not_called_done():
    receipt = _receipt(
        _req("put the beach photo behind me", facts={"speaker_background": True}),
        _draft_facts(_manifest(PHOTO)),
    )
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("This draft doesn't put a photo or video behind you")


def test_several_photos_and_a_corner_are_met_when_the_phone_can_draw_them(monkeypatch):
    _open_windows(monkeypatch)
    manifest = _manifest(PHOTO, OTHER)
    facts = _draft_facts(
        manifest,
        background_media={
            "media_id": PHOTO,
            "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}],
            "speaker_frame": "small_bottom_right",
        },
    )
    receipt = _receipt(_req(), facts)
    assert receipt.status == "met"
    assert "2 photos or videos take turns behind you" in receipt.reason
    assert "small at the bottom right" in receipt.reason


def test_windows_the_app_cannot_draw_make_the_ask_partial_not_done(monkeypatch):
    manifest = _manifest(PHOTO, OTHER)  # the windows token is not verified
    facts = _draft_facts(
        manifest,
        background_media={
            "media_id": PHOTO,
            "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}],
            "speaker_frame": "small_bottom_right",
        },
    )
    receipt = _receipt(_req(), facts)
    assert receipt.status == "partial" and receipt.verification == "checked"
    assert receipt.reason.startswith("One photo or video stays behind you for the whole video")
    assert is_format_limit(receipt.reason)


def test_fewer_windows_than_asked_is_partial(monkeypatch):
    _open_windows(monkeypatch)
    manifest = _manifest(PHOTO, OTHER)
    foreign = "asset-00000000-0000-4000-8000-0000000000ff"
    facts = _draft_facts(
        manifest,
        background_media={
            "media_id": PHOTO,
            "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}, {"media_id": foreign}],
            "speaker_frame": "small_bottom_right",
        },
    )
    receipt = _receipt(_req(), facts)
    assert receipt.status == "partial"
    assert "Only 2 of your 3" in receipt.reason


# ------------------------------------------------------- 4. the finished video is judged too


def _variant(**extra):
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "edit_format": "subtitled",
        **extra,
    }


def test_a_finished_video_without_a_background_is_not_met():
    facts = plan_facts_from_phone_variant(
        _variant(phone_lane_receipt={"applied": ["overlays"], "dropped": []})
    )
    receipt = _receipt(_req(), facts)
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("The finished video doesn't have a photo or video")


def test_a_finished_video_with_windows_and_a_corner_is_met():
    facts = plan_facts_from_phone_variant(
        _variant(
            phone_lane_receipt={
                "applied": ["background"],
                "dropped": [],
                "background": {"windows": 6, "speaker_frame": "small_bottom_right"},
            }
        )
    )
    receipt = _receipt(_req(), facts)
    assert receipt.status == "met"
    assert "6 photos or videos take turns behind you" in receipt.reason


def test_a_render_that_recorded_nothing_claims_nothing():
    facts = plan_facts_from_phone_variant(_variant())
    receipt = _receipt(_req(), facts)
    assert receipt.status != "met"
    assert not is_judged(_req(), receipt)


# --------------------------------------------------------- 5. readiness agrees with the receipt


def _readiness(requirements, manifest):
    brief = CreativeBrief(version=1, requirements=requirements)
    media = {"clip_paths": [TAKE]}
    report = assess_readiness(
        brief, dict(STRATEGY), media=media, manifest=manifest, creator_id=CREATOR
    )
    return {row["requirement_id"]: row for row in report["requirements"]}, report


def test_readiness_lists_the_background_refusal_as_unsupported():
    rows, report = _readiness([_req()], _manifest())
    assert rows["r2"]["disposition"] == "unsupported", rows["r2"]
    assert "Attach the photos or videos first, in Visuals" in rows["r2"]["reason"]
    assert report["ready"] is False


def test_readiness_lists_a_refused_talking_ask_as_unsupported_not_unverified():
    """The 'No music' row of the prod payload: not_possible/checked in the receipts, `unverified`
    (approvable, never told) in readiness, because readiness built its facts without saying the
    draft is a phone Talking one."""
    no_music = _req("No music", rid="r5", kind="audio")
    facts = _draft_facts(_manifest())
    receipt = _receipt(no_music, facts)
    assert receipt.status == "not_possible" and receipt.verification == "checked"

    rows, report = _readiness([no_music], _manifest())
    assert rows["r5"]["disposition"] not in APPROVABLE, rows["r5"]
    assert rows["r5"]["disposition"] == "unsupported"
    assert rows["r5"]["reason"] == receipt.reason
    assert report["ready"] is False


def test_readiness_approves_a_planned_background():
    manifest = _manifest(PHOTO)
    brief = CreativeBrief(
        version=1,
        requirements=[_req("put the beach photo behind me", facts={"speaker_background": True})],
    )
    report = assess_readiness(
        brief,
        {**STRATEGY, "background_media": {"media_id": PHOTO}},
        media={"clip_paths": [TAKE]},
        manifest=manifest,
        creator_id=CREATOR,
    )
    (row,) = report["requirements"]
    assert row["disposition"] == "executable", row


def test_a_rendered_colour_fill_is_an_applied_background_not_none():
    """KRI-1173: the rendered-fact reader keyed on `media_id` would call a colour 'nothing'."""
    from app.kria.brief_checks import _rendered_speaker_background_fact

    fact = _rendered_speaker_background_fact(
        {"speaker_background": {"kind": "color", "color": "#FFFFFF"}}
    )
    assert fact.status == "applied" and fact.window_count == 0
    assert _rendered_speaker_background_fact({"speaker_background": None}).status == "none"


# -------------------------------------------- KRI-1173: a plain colour behind the speaker (S58)
#
# Ways this can fail, each pinned below:
#  a. a colour ask is answered "Attach the photos or videos first" (it needs no pool at all);
#  b. the colour is called met while the gate is closed / the plan holds no colour, or refused
#     without naming WHY (token / app too old / not an iPhone Talking edit);
#  c. the planner's colour is not remembered across turns (a sticky fact), or the ask is
#     recognised by the creator's words instead of the recorded fact (KRI-667);
#  d. a finished video with a colour fill is read as "nothing behind the speaker".

COLOUR_FACTS = {"speaker_background": True, "background_color": "white"}


@pytest.fixture
def _client_context():
    from app.services.client_protocol import clear_request_context

    clear_request_context()
    yield
    clear_request_context()


def _open_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackgroundColor"],
    )


def _colour_req(said: str = "white", **extra) -> BriefRequirement:
    return _req(
        "a plain white background behind me",
        facts={"speaker_background": True, "background_color": said, **extra},
    )


def test_the_colour_fact_is_taught_remembered_and_owns_the_ask():
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION
    from app.kria.brief import STICKY_FACTS

    assert '"background_color"' in _BRIEF_PROMPT_SECTION
    assert "background_color" in STICKY_FACTS
    # Recognised through the recorded fact, in any language and even without the flag beside it.
    assert asks_speaker_background(_colour_req())
    assert asks_speaker_background(
        _req("arkam düz beyaz olsun", facts={"background_color": "beyaz"})
    )
    assert not asks_speaker_background(_req("a plain white background", kind="style", facts={}))


def test_colour_ask_needs_no_pool(monkeypatch):
    _open_colour(monkeypatch)
    manifest = _manifest()  # NO photo or video attached
    facts = _draft_facts(manifest, background_media={"color": "#FFFFFF"})
    receipt = _receipt(_colour_req(), facts)

    assert receipt.status == "met" and receipt.verification == "checked"
    assert "plain white background sits behind you for the whole video" in receipt.reason
    assert "Attach" not in receipt.reason and "Visuals" not in receipt.reason
    assert is_judged(_colour_req(), receipt)


def test_a_hex_the_creator_gave_is_met_and_a_different_colour_is_partial(monkeypatch):
    _open_colour(monkeypatch)
    manifest = _manifest()
    facts = _draft_facts(manifest, background_media={"color": "#00FF00"})
    met = _receipt(_colour_req("#00ff00"), facts)
    assert met.status == "met" and "#00FF00" in met.reason
    other = _receipt(_colour_req("blue"), facts)
    assert other.status == "partial" and "#00FF00" in other.reason


def test_a_colour_under_windows_and_a_corner_says_so(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    manifest = _manifest(PHOTO)
    facts = _draft_facts(
        manifest,
        background_media={
            "color": "#FFFFFF",
            "sequence": [{"media_id": PHOTO}],
            "speaker_frame": "small_bottom_right",
        },
    )
    receipt = _receipt(_colour_req(), facts)
    assert receipt.status == "met"
    assert "one photo or video sits over it" in receipt.reason
    assert "take turns" not in receipt.reason
    assert "small at the bottom right" in receipt.reason


def test_a_colour_while_its_token_is_closed_names_the_part_and_the_reason(monkeypatch):
    # The photo / video lane is open; the colour token is not verified.
    manifest = _manifest()
    facts = _draft_facts(manifest, background_media={"color": "#FFFFFF"})
    receipt = _receipt(_colour_req(), facts)
    assert receipt.status == "not_possible" and receipt.verification == "checked"
    assert receipt.reason.startswith("Putting a plain colour behind you isn't available")
    assert "Attach" not in receipt.reason
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)


def test_a_colour_asked_of_an_app_too_old_says_to_update(monkeypatch, _client_context):
    from app.services.client_protocol import set_client_protocol

    _open_colour(monkeypatch)
    set_client_protocol(33)
    manifest = _manifest()
    receipt = _receipt(_colour_req(), _draft_facts(manifest, background_media={"color": "white"}))
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("Your iPhone app is too old to put a plain colour behind you")
    assert is_format_limit(receipt.reason)


def test_a_colour_on_a_montage_names_the_format(monkeypatch):
    _open_colour(monkeypatch)
    facts = dataclasses.replace(
        plan_facts_from_strategy(
            {**STRATEGY, "edit_format": "montage"},
            clip_ids=[TAKE],
            manifest=_manifest(edit_format="montage"),
        ),
        phone_talking=False,
    )
    receipt = _receipt(_colour_req(), facts)
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("Only a talking-to-camera edit made on your iPhone")
    assert "plain colour" in receipt.reason and is_format_limit(receipt.reason)


def test_a_draft_that_holds_no_colour_is_not_called_done_and_never_asks_for_photos(monkeypatch):
    _open_colour(monkeypatch)
    receipt = _receipt(_colour_req(), _draft_facts(_manifest()))
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("This draft doesn't put a plain colour behind you")
    assert "Attach" not in receipt.reason
    # A photo fill is not the colour the creator asked for either.
    photo = _receipt(
        _colour_req(), _draft_facts(_manifest(PHOTO), background_media={"media_id": PHOTO})
    )
    assert photo.status == "not_possible"
    assert photo.reason.startswith("This draft doesn't put a plain colour behind you")


def test_an_invalid_colour_the_repair_dropped_is_not_called_done(monkeypatch):
    _open_colour(monkeypatch)
    facts = _draft_facts(_manifest(), background_media={"color": "turquoise"})
    receipt = _receipt(_colour_req("turquoise"), facts)
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("This draft doesn't put a plain colour behind you")


def test_the_colour_refusals_read_in_turkish_for_a_turkish_chat(monkeypatch):
    from app.kria.reply_language import reply_language_for

    with reply_language_for("tr"):
        closed = _receipt(
            _colour_req("beyaz"), _draft_facts(_manifest(), background_media={"color": "white"})
        )
        _open_colour(monkeypatch)
        met = _receipt(
            _colour_req("beyaz"), _draft_facts(_manifest(), background_media={"color": "white"})
        )
    assert closed.status == "not_possible" and "düz renk" in closed.reason
    assert closed.reason.isascii() is False and "Putting" not in closed.reason
    assert met.status == "met" and "düz beyaz" in met.reason


def test_a_finished_colour_video_is_met_and_the_receipt_carries_the_colour():
    from app.kria.brief_checks import _rendered_speaker_background_fact

    saved = plan_facts_from_phone_variant(
        _variant(speaker_background={"kind": "color", "color": "#FFFFFF"})
    )
    assert _receipt(_colour_req(), saved).status == "met"
    assert (
        _rendered_speaker_background_fact(
            {"speaker_background": {"kind": "color", "color": "#FFFFFF"}}
        ).color
        == "#FFFFFF"
    )

    rendered = plan_facts_from_phone_variant(
        _variant(
            phone_lane_receipt={
                "applied": ["background"],
                "dropped": [],
                "background": {"color": "#FFFFFF", "windows": 0, "speaker_frame": None},
            }
        )
    )
    receipt = _receipt(_colour_req(), rendered)
    assert receipt.status == "met" and "plain white background" in receipt.reason


def test_a_finished_video_with_a_photo_or_nothing_is_not_a_colour_met():
    photo = plan_facts_from_phone_variant(
        _variant(speaker_background={"media_id": PHOTO, "kind": "image", "source_start_s": 0.0})
    )
    assert _receipt(_colour_req(), photo).status == "not_possible"
    none = plan_facts_from_phone_variant(
        _variant(phone_lane_receipt={"applied": ["overlays"], "dropped": []})
    )
    receipt = _receipt(_colour_req(), none)
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("The finished video doesn't have a plain colour")


def test_readiness_approves_a_planned_colour_without_any_attached_media(monkeypatch):
    _open_colour(monkeypatch)
    manifest = _manifest()
    brief = CreativeBrief(version=1, requirements=[_colour_req()])
    report = assess_readiness(
        brief,
        {**STRATEGY, "background_media": {"color": "#FFFFFF"}},
        media={"clip_paths": [TAKE]},
        manifest=manifest,
        creator_id=CREATOR,
    )
    (row,) = report["requirements"]
    assert row["disposition"] == "executable", row


# ------------------------- KRI-1173 review (Emir, PR #2088): the colour verdict combines checks
#
# `_check_speaker_background` used to return the colour verdict before it looked at the corner
# (`speaker_small`) and the changing backgrounds (`background_changes`), so "a green background,
# me small at the bottom right" was called met with the speaker still full size. Ways it fails:
#  e. colour + corner asked, the plan keeps the speaker full size -> called met;
#  f. colour + changing backgrounds asked, the plan has no windows over the colour -> called met;
#  g. fewer windows than the creator's photos over the colour -> called met;
#  h. a wrong colour hides a missing corner (only one of the two shortfalls is named);
#  i. the fix breaks the plain colour ask (still met, still never "Attach ... in Visuals").


def test_colour_with_a_corner_the_plan_keeps_full_size_is_partial(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(_manifest(), background_media={"color": "#00FF00"})
    receipt = _receipt(_colour_req("green", speaker_small=True), facts)
    assert receipt.status == "partial"
    assert "full size" in receipt.reason and "corner" in receipt.reason


def test_colour_with_a_corner_the_iphone_cannot_draw_is_a_named_format_limit(monkeypatch):
    _open_colour(monkeypatch)  # the windows token (which carries the corner) is NOT verified
    facts = _draft_facts(
        _manifest(),
        background_media={"color": "#00FF00", "speaker_frame": "small_bottom_right"},
    )
    receipt = _receipt(_colour_req("green", speaker_small=True), facts)
    assert receipt.status == "partial"
    assert "full size" in receipt.reason and "can't do that yet" in receipt.reason
    assert is_format_limit(receipt.reason)


def test_colour_with_changing_backgrounds_but_no_windows_is_partial(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(_manifest(PHOTO, OTHER), background_media={"color": "#00FF00"})
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "partial"
    assert "plain colour stays behind you" in receipt.reason


def test_colour_with_changing_backgrounds_and_nothing_attached_names_what_to_attach(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(_manifest(), background_media={"color": "#00FF00"})
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "partial"
    assert "plain colour stays behind you" in receipt.reason
    assert "no photos or videos" in receipt.reason
    assert not is_format_limit(receipt.reason)


def test_colour_with_changing_backgrounds_the_iphone_cannot_switch_is_a_format_limit(monkeypatch):
    _open_colour(monkeypatch)  # windows token closed: the repair drops the sequence
    facts = _draft_facts(
        _manifest(PHOTO, OTHER),
        background_media={
            "color": "#00FF00",
            "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}],
        },
    )
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "partial"
    assert "can't switch backgrounds yet" in receipt.reason
    assert is_format_limit(receipt.reason)


def test_colour_with_both_asks_met_is_met(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(
        _manifest(PHOTO, OTHER),
        background_media={
            "color": "#00FF00",
            "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}],
            "speaker_frame": "small_bottom_right",
        },
    )
    receipt = _receipt(_colour_req("green", speaker_small=True, background_changes=True), facts)
    assert receipt.status == "met"
    assert "2 photos or videos take turns over it" in receipt.reason
    assert "small at the bottom right" in receipt.reason


def test_a_wrong_colour_and_a_missing_corner_are_both_named(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(_manifest(), background_media={"color": "#FFFFFF"})
    receipt = _receipt(_colour_req("green", speaker_small=True), facts)
    assert receipt.status == "partial"
    assert "#FFFFFF" in receipt.reason and "green" in receipt.reason
    assert "full size" in receipt.reason


def test_a_finished_colour_video_without_the_corner_is_partial():
    facts = plan_facts_from_phone_variant(
        _variant(
            speaker_background={"kind": "color", "color": "#00FF00"},
            phone_lane_receipt={"applied": ["background"], "dropped": []},
        )
    )
    receipt = _receipt(_colour_req("green", speaker_small=True), facts)
    assert receipt.status == "partial"
    assert "full size" in receipt.reason


# ------------------------------------------- KRI-1173 x Green 3: distinct photos, not windows
#
# The windows cycle the creator's photos (Green 3: 8 windows over 5 photos / videos), so what
# the creator hears is the DISTINCT count, as the photo path already says it (#2089). Ways the
# colour receipt can get this wrong:
#  a. "8 photos or videos take turns over it" for 5 photos;
#  b. one photo repeated in every window called "taking turns" (met) instead of no change;
#  c. "Only 6 of your 8" when 3 of the 5 photos asked for are shown;
#  d. a finished colour video with repeating windows counted by windows.

FIVE = [f"asset-00000000-0000-4000-8000-00000000117{n}" for n in range(5)]


def _cycled(ids: list[str], windows: int = 8) -> list[dict]:
    return [{"media_id": ids[n % len(ids)]} for n in range(windows)]


def test_green3_colour_under_8_windows_of_5_photos_says_5(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(
        _manifest(*FIVE),
        background_media={"color": "#00FF00", "sequence": _cycled(FIVE)},
    )
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "met"
    assert "5 photos or videos take turns over it" in receipt.reason
    assert "8 photos" not in receipt.reason


def test_colour_under_8_windows_of_one_photo_is_no_change(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    facts = _draft_facts(
        _manifest(*FIVE),
        background_media={"color": "#00FF00", "sequence": _cycled(FIVE[:1])},
    )
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "partial"
    assert "nothing changes over it" in receipt.reason
    assert "take turns" not in receipt.reason


def test_colour_showing_3_of_5_asked_photos_says_only_3_of_5(monkeypatch):
    _open_colour(monkeypatch)
    _open_windows(monkeypatch)
    foreign = [f"asset-00000000-0000-4000-8000-0000000011f{n}" for n in range(2)]
    facts = _draft_facts(
        _manifest(*FIVE[:3]),
        background_media={"color": "#00FF00", "sequence": _cycled([*FIVE[:3], *foreign])},
    )
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "partial"
    assert "Only 3 of your 5 photos or videos" in receipt.reason


def test_a_finished_colour_video_with_repeating_windows_counts_photos():
    windows = [
        {"media_id": FIVE[n % 5], "kind": "image", "start_s": 3.0 * n, "end_s": 3.0 * n + 3}
        for n in range(8)
    ]
    facts = plan_facts_from_phone_variant(
        _variant(
            speaker_background={"kind": "color", "color": "#00FF00", "windows": windows},
            phone_lane_receipt={"applied": ["background"], "dropped": []},
        )
    )
    receipt = _receipt(_colour_req("green", background_changes=True), facts)
    assert receipt.status == "met"
    assert "5 photos or videos take turns over it" in receipt.reason
