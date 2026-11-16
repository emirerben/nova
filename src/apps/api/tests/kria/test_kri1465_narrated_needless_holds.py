"""KRI-1465: a phone Voiceover draft was held twice for things the video then did anyway.

Incident (prod thread 78787267, 2026-11-14, phone render, Narrated = a recorded voiceover edit,
``narrated_planned`` / ``audio_strategy: voiceover``, English chat, German voiceover). The
creator asked: "My voiceover is in German. German captions, word by word. Show each thing when I
say it: the train on "Zug", ... Keep the crowd quietly under my voice. Title: "Ich kann keine
Videos schneiden."" Rows r1, r2, r4, r11 and r12 below are the job's brief binding word for
word; r5, r6, r7 and r10 have its kind, scope and trigger, with the description rebuilt in
r4's shape.

1. Hold 1 (approval readiness): "I can't make this yet:" named r2 "German captions, word by
   word." and r11 "Keep the crowd quietly under my voice." as "I can't confirm this draft does
   it yet." and offered "Make it without these" for exactly those two.
2. The tap waived r2 and r11. The next plan's captions were repaired from "none" to "auto"
   (``dropped_fields: ["caption_style"]``) and that was asked as a second question: "This kind
   of edit always shows captions right now, so I kept them in. Should I make a simpler
   version?".
3. The video had German captions with every word highlighted and the crowd under the voice
   (footage sound at 0.15). The review said "Left out as agreed" for both: false twice.
4. The hold copy read "German captions, word by word.: I can't ...", "... word by word.; Keep
   the crowd quietly under my voice..".

What is replayed, and with what. The first turn runs through the real runtime over scratch
Postgres (``submit_turn`` + ``run_kria_turn``: planner, policy, receipts, readiness, the hold
and its card); with this fix's changes switched back it writes prod's first hold word for
word (the red control at the end of this file). Beside it: the real ``assess_readiness``,
``build_receipts``, ``check_strategy_for_runtime_v2``, ``readiness_reply`` and
``_simpler_version_offer``, the real planner step for the second hold, and the real
``_run_phone_narrated_job`` with the real finished-video review. Only model transport is
replaced: the Main Creator's answer (the strategy it proposed), the clip-intent step, and
speech-to-text. The incident thread's clip analyses and voiceover words are not in the repo,
so the de1 kit's scrubbed clips and German voiceover (``de1_leverkusen_n3.json``) stand in
for the media. Nothing here ran on a phone.

Ways the fix can fail, written down before the code:

r2: a captions ask on a phone Voiceover draft
 F1  readiness still holds r2 as "I can't confirm this draft does it yet" (the bug), for any
     caption style the Main Creator may write for the first draft (none given, "auto", "karaoke").
 F2  the promise reaches a captions ask for another language than the voiceover's (the brief
     states the voiceover's language, or the ask says "translate"): it must stay as today.
 F3  it reaches an ask about the captions' look, place or colour: as today.
 F4  it reaches "No captions": as today (held; "Captions are still on in this draft").
 F5  it promises word-by-word on a draft whose caption style is sentence captions: as today.
 F6  a verdict that is checked today changes ("Captions word by word" on a karaoke draft is
     Done, "Add captions" is Done): a saved ready draft would stop matching its readiness.
 F7  it reaches a cloud voiceover draft, a Talking draft, a montage draft, an editor turn or a
     finished render.
 F8  the promise is not approvable: missing from ``RENDER_PROMISE_REASONS``, no Turkish twin,
     or it turns into the "simpler version?" question on the receipts path (not a format limit).
 F9  the kill switch does not restore today's hold.
 F10 after the render the review says "Left out as agreed", or "Done" for word by word when
     the captions are sentences (the Voiceover record only knows the language), or reads the
     language off the ask instead of the captions the phone drew.

r11: "the crowd ... under my voice"
 F11 r11 is still read as a spoken-excerpt ask with nothing to check (the bug).
 F12 "mute the crowd noise under my voice" (typed mute, KRI-821, and the untyped wording)
     changes; a bare "mute the crowd under my voice" becomes "plays under your voice".
 F13 "crowd" as a picture becomes a sound ask: "Show the crowd when I say "Fans"", "Keep the
     crowd shot short", "Show the crowd under my voiceover" (not an ``audio`` row).
 F14 "bring the crowd up under my voice" is read as keep-it-low.
 F15 a Talking or montage draft answers differently than today.
 F16 a cloud voiceover draft answers something the "crowd noise" wording does not already say
     there (same check, same sentence, or it is over-reach).
 F17 after the render it is not judged from the level the recipe plays (0.15 is under the
     voice, full volume is not).

Both
 F18 a real unsupported ask beside r2 / r11 no longer holds the draft, or the card names more
     than that one ask.

The second hold
 F19 the "none" -> "auto" captions repair still asks when no live ask wants no captions.
 F20 a live "No captions" ask loses its question (today's question must stay).
 F21 the creator is no longer told captions stay (the notice is dropped with the question).
 F22 a caller that does not know the brief changes (readiness's own re-check, the chips).
 F23 the planner never tells the policy what the brief asks (the wiring), so prod still asks.
 F24 the repaired strategy is not a fixed point: readiness re-checks it and refuses, or says
     "This renderer would change the proposed edit".
 F25 a waived (no longer live) "No captions" ask still asks.

Hold copy
 F26 ".:" in the readiness reply, ".;" or ".." in "The simpler version leaves out: ...", ".;"
     in the card's "Leaves out: ...", in English, Turkish or German.
 F27 the tidy-up eats something it must keep: a closing quote or bracket, "!" / "?", or changes
     a name that has no full stop (existing replies must stay byte-identical).
 F28 the card stops recording exactly the named ids and the brief version (consent matching).

Found while implementing, not on the list above:
 F29 the fix adds word-reading regexes (KRI-667 ratchet, `test_kri667_no_new_intent_regexes`):
     the bed reader's existing pattern is widened instead; the brief writer types no fact for
     this ask, and teaching it one is a prompt change.
 F30 the review test for "another language" stamped German words as English: the review reads
     the language off the caption text first (KRI-838), so that case uses English words.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from tests.kria.test_de1_narrated_holds import CREATOR, FIXTURE, _media, _strategy

CLIPS = FIXTURE["clips"]
CLIP = CLIPS[0]["media_id"]
TITLE = "Ich kann keine Videos schneiden."
CANT_CONFIRM = "I can't confirm this draft does it yet."
NOT_BEFORE_RENDER = "I can't check this before the video is made; I'll point it out after."
UNDER_VOICE = "Your footage sound plays under your voice"
HOLD_2 = (
    "This kind of edit always shows captions right now, so I kept them in. "
    "Should I make a simpler version?"
)
CAPTIONS_KEPT = "This kind of edit always shows captions right now, so I kept them in."
FLAG = "narrated_caption_render_promise_enabled"


def _r(rid: str, kind: str, description: str, *, scope: str = "global", facts=None, literal=None):
    return BriefRequirement(
        id=rid, kind=kind, scope=scope, description=description, facts=facts or {}, literal=literal
    )


def _cue(rid: str, thing: str, word: str, *, scope: str = "global") -> BriefRequirement:
    return _r(
        rid,
        "style",
        f'Show the {thing} when I say "{word}".',
        scope=scope,
        facts={"triggers": [word]},
    )


R1 = _r("r1", "audio", "Use my German voiceover.", facts={"voiceover_language": "de"})
R2 = _r(
    "r2",
    "style",
    "German captions, word by word.",
    facts={"captions": "word_by_word", "caption_language": "de"},
)
R11 = _r("r11", "audio", "Keep the crowd quietly under my voice.")
R12 = _r("r12", "text", "Title", scope="title", literal=TITLE)
# The job's brief binding, before the server waived r2 and r11.
INCIDENT = [
    R1,
    R2,
    _cue("r4", "train", "Zug", scope=f"clip:{CLIP}"),
    _cue("r5", "stadium", "Stadion"),
    _cue("r6", "light show", "Lichtshow"),
    _cue("r7", "goal", "Tor"),
    _cue("r10", "app", "Die App"),
    R11,
    R12,
]
FREEZE = _r("u1", "timing", "Freeze the last frame for 2 seconds.")
NO_FREEZE = "I can't freeze a frame in this edit yet, so that moment plays normally"


@pytest.fixture
def phone_voiceover(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod's flag profile with the Voiceover pop-in lane as prod rendered the incident."""
    from app.services.phone_rollout import (  # noqa: PLC0415
        PHONE_SUBTITLED_OVERLAY_FEATURES,
        PHONE_SUBTITLED_SFX_FEATURES,
        PHONE_VOICEOVER_EDITOR_FEATURES,
        phone_render_supported_formats,
    )

    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    for flag in (
        "phone_voiceover_editor_lanes_enabled",
        "phone_subtitled_media_lanes_enabled",
        "phone_subtitled_editor_lanes_enabled",
        "phone_subtitled_reaction_beats_enabled",
        "phone_voiceover_reaction_beats_enabled",
        "phone_subtitled_fullscreen_overlays_enabled",
        "media_overlays_enabled",
        "sound_effects_enabled",
    ):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        sorted(
            set(settings.phone_render_verified_features)
            | set(PHONE_SUBTITLED_OVERLAY_FEATURES)
            | set(PHONE_SUBTITLED_SFX_FEATURES)
            | set(PHONE_VOICEOVER_EDITOR_FEATURES)
            | {"narrationAudio"}
        ),
    )
    assert "narrated_planned" in phone_render_supported_formats()


def _manifest(*, phone: bool = True, edit_format: str = "narrated_planned", voiceover: bool = True):
    from app.services import creator_capabilities as capabilities  # noqa: PLC0415

    return capabilities.resolve_creator_manifest(
        item_id="item-kri1465",
        edit_format=edit_format,
        has_voiceover=voiceover,
        narration=(
            {
                "gcs_path": "users/kri1465/voice.m4a",
                "generation": "1",
                "duration_s": FIXTURE["voiceover"]["duration_s"],
            }
            if voiceover
            else None
        ),
        media=[{k: c[k] for k in ("media_id", "kind", "duration_s")} for c in CLIPS]
        + [
            {"media_id": f"asset-kri1465-{name}", "kind": "image", "duration_s": None}
            for name in ("stadium", "app")
        ],
        phone_source_media_ids=[c["media_id"] for c in CLIPS] if phone else None,
        phone_rendering_allowed=phone,
        runs_guided_voiceover=False,
    )


def _draft(
    caption_style: str | None = "karaoke", *, beats: bool = True, title: bool = True
) -> dict:
    """The first draft: the title, and the two Visuals the Main Creator put on their words."""
    strategy = _strategy() | ({"opening_title": TITLE} if title else {})
    if beats:
        strategy["reaction_beats"] = [
            {
                "beat_id": "stadion",
                "trigger": "Stadion",
                "visual_id": "asset-kri1465-stadium",
                "visual_role": "photo",
            },
            {
                "beat_id": "app",
                "trigger": "Die App",
                "visual_id": "asset-kri1465-app",
                "visual_role": "photo",
            },
        ]
    if caption_style is not None:
        strategy["caption_style"] = caption_style
    return strategy


def _report(requirements, strategy: dict | None = None, **kwargs) -> dict:
    from app.kria.approval_readiness import assess_readiness  # noqa: PLC0415

    return assess_readiness(
        CreativeBrief(version=1, requirements=list(requirements)),
        strategy if strategy is not None else _draft(),
        media=kwargs.pop("media", None) or _media(),
        manifest=kwargs.pop("manifest", None) or _manifest(),
        creator_id=CREATOR,
        **kwargs,
    )


def _rows(report: dict) -> dict[str, tuple[str, str | None]]:
    return {r["requirement_id"]: (r["disposition"], r["reason"]) for r in report["requirements"]}


def _held(report: dict) -> list[str]:
    from app.kria.approval_readiness import APPROVABLE  # noqa: PLC0415

    return [
        r["requirement_id"] for r in report["requirements"] if r["disposition"] not in APPROVABLE
    ]


# -- Hold 1: the incident's first draft -----------------------------------------------------------


@pytest.mark.parametrize("caption_style", [None, "auto", "karaoke", "kinetic"])
def test_the_incident_draft_is_ready_and_holds_nothing(phone_voiceover, caption_style) -> None:
    """F1 / F11: prod held r2 and r11 (``omitted_requirement_ids: ["r2", "r11"]``)."""
    report = _report(INCIDENT, _draft(caption_style))

    assert _held(report) == [], _rows(report)
    assert report["ready"] is True
    rows = _rows(report)
    # r11 is the mix every voiceover edit makes, checked.
    assert rows["r11"] == ("executable", UNDER_VOICE)
    # r2 is a promise the finished video is checked against, never "can't confirm".
    assert rows["r2"][0] == "unverified"
    assert rows["r2"][1] != CANT_CONFIRM
    # Everything else is what prod said on that turn.
    assert rows["r1"][0] == "unverified"
    assert rows["r12"] == ("executable", None)
    for rid in ("r4", "r6", "r7"):
        assert rows[rid][1].startswith("Your clips follow your voice"), rows[rid]
    for rid in ("r5", "r10"):
        assert rows[rid] == ("unverified", "I'll show it when I hear those words in your voiceover")


def test_the_captions_promise_says_what_the_voiceover_edit_does(phone_voiceover) -> None:
    """F8: one static promise, approvable in English and Turkish, never the "simpler?" ask."""
    from app.kria import brief_checks as bc  # noqa: PLC0415

    promise = bc._NARRATED_CAPTIONS_PLAN
    assert _rows(_report(INCIDENT))["r2"] == ("unverified", promise)
    assert promise in bc.RENDER_PROMISE_REASONS
    assert promise in bc._REASON_TR
    assert bc.is_format_limit(promise) and bc.is_format_limit(bc._REASON_TR[promise])
    with reply_language_for("tr"):
        assert _rows(_report(INCIDENT))["r2"] == ("unverified", bc._REASON_TR[promise])


def test_the_draft_reply_lists_the_captions_as_planned_and_the_crowd_as_done(
    phone_voiceover,
) -> None:
    """The draft turn's own receipts (the facts `kria_runtime` builds for this draft)."""
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        needs_creator_choice,
        plan_facts_from_strategy,
        reply_from_receipts,
    )

    manifest = _manifest()
    facts = dataclasses.replace(
        plan_facts_from_strategy(
            _draft(), clip_ids=[m.media_id for m in manifest.media], manifest=manifest
        ),
        phone_narrated=True,
        phone_text=True,
        media_snapshot=_media(),
    )
    brief = CreativeBrief(version=1, requirements=INCIDENT)
    receipts = build_receipts(brief.live(), facts, include_unchecked=True, strict_order=True)
    by_id = {r.requirement_id: r for r in receipts}

    assert (by_id["r11"].status, by_id["r11"].reason) == ("met", UNDER_VOICE)
    assert (by_id["r2"].status, by_id["r2"].verification) == ("partial", "checked")
    assert not needs_creator_choice(by_id["r2"])
    reply = reply_from_receipts(brief, receipts, summary="Here's your draft.")
    assert "- Will do at render: German captions, word by word." in reply
    assert "Partly: German captions" not in reply


def test_the_kill_switch_restores_the_captions_hold(phone_voiceover, monkeypatch) -> None:  # noqa: ANN001
    """F9."""
    monkeypatch.setattr(settings, FLAG, False)
    report = _report(INCIDENT)
    assert _rows(report)["r2"] == ("evidence_missing", CANT_CONFIRM)
    assert report["ready"] is False


# -- Hold 1: what must still behave as today ------------------------------------------------------

ENGLISH = _r("c1", "style", "English captions.", facts={"captions": "on", "caption_language": "en"})
WORDS = _r("c2", "style", "Captions word by word.", facts={"captions": "word_by_word"})
NO_CAPTIONS = _r("c3", "style", "No captions.", facts={"captions": "off"})
AS_TODAY = [
    # F2: another language than the voiceover's (r1 states it), or a translation.
    ([R1, ENGLISH], "karaoke", "c1", ("evidence_missing", CANT_CONFIRM)),
    (
        [_r("c1", "style", "Translate the captions to English.")],
        "karaoke",
        "c1",
        ("evidence_missing", CANT_CONFIRM),
    ),
    # F3: look, place, colour.
    (
        [
            _r(
                "c1",
                "style",
                "German captions, yellow and at the top.",
                facts={"captions": "on", "caption_language": "de"},
            )
        ],
        "karaoke",
        "c1",
        ("evidence_missing", CANT_CONFIRM),
    ),
    # F4: no captions.
    ([NO_CAPTIONS], "auto", "c3", ("evidence_missing", CANT_CONFIRM)),
    ([NO_CAPTIONS], "karaoke", "c3", ("needs_choice", "Captions are still on in this draft.")),
    # F5: sentence captions on a word-by-word ask.
    ([R2], "clean", "r2", ("evidence_missing", CANT_CONFIRM)),
    (
        [WORDS],
        "clean",
        "c2",
        ("needs_choice", "Captions are on as full sentences, not word by word."),
    ),
    # F6: what is checked today stays checked.
    ([WORDS], "karaoke", "c2", ("executable", "words light up as they're spoken")),
    (
        [_r("c4", "style", "Add captions.", facts={"captions": "on"})],
        "auto",
        "c4",
        ("executable", None),
    ),
    # F12: a mute is a mute (KRI-821), typed or not; a bare "mute the crowd" is not a keep.
    (
        [
            _r(
                "c5",
                "audio",
                "Mute the crowd noise under my voice.",
                facts={"audio_intent": "source_muted"},
            )
        ],
        "karaoke",
        "c5",
        ("executable", "Your footage sound is muted under your voice"),
    ),
    (
        [_r("c5", "audio", "Mute the crowd noise under my voice.")],
        "karaoke",
        "c5",
        ("executable", UNDER_VOICE),
    ),
    (
        [_r("c5", "audio", "Mute the crowd under my voice.")],
        "karaoke",
        "c5",
        ("evidence_missing", CANT_CONFIRM),
    ),
    (
        [
            _r(
                "c5",
                "audio",
                "Mute the crowd under my voice.",
                facts={"audio_intent": "source_muted"},
            )
        ],
        "karaoke",
        "c5",
        ("unverified", NOT_BEFORE_RENDER),
    ),
    # F13: the crowd as a picture.
    (
        [_cue("c6", "crowd", "Fans")],
        "karaoke",
        "c6",
        (
            "unverified",
            "Your clips follow your voice, so once the video is made I'll check what's on "
            "screen when you say those words",
        ),
    ),
    (
        [_r("c6", "style", "Keep the crowd shot short.")],
        "karaoke",
        "c6",
        ("unverified", NOT_BEFORE_RENDER),
    ),
    (
        [_r("c6", "style", "Show the crowd under my voiceover.")],
        "karaoke",
        "c6",
        ("unverified", NOT_BEFORE_RENDER),
    ),
    # F14: louder is not lower.
    (
        [_r("c7", "audio", "Bring the crowd up under my voice.")],
        "karaoke",
        "c7",
        ("evidence_missing", CANT_CONFIRM),
    ),
]


@pytest.mark.parametrize(("requirements", "caption_style", "rid", "expected"), AS_TODAY)
def test_neighbouring_asks_answer_as_today(  # noqa: ANN001
    phone_voiceover, requirements, caption_style, rid, expected
) -> None:
    assert _rows(_report(requirements, _draft(caption_style, beats=False)))[rid] == expected


def test_a_captions_ask_with_no_language_stated_anywhere_is_promised(phone_voiceover) -> None:
    """The voiceover's language is unknown before the render: the promise claims no language."""
    from app.kria import brief_checks as bc  # noqa: PLC0415

    rows = _rows(_report([ENGLISH], _draft("auto", beats=False)))
    assert rows["c1"] == ("unverified", bc._NARRATED_CAPTIONS_PLAN)
    for word in ("English", "German", "language you"):
        assert word not in bc._NARRATED_CAPTIONS_PLAN


def test_a_real_unsupported_ask_still_holds_and_the_card_names_only_it(phone_voiceover) -> None:
    """F18."""
    from app.kria.approval_readiness import readiness_reply  # noqa: PLC0415
    from app.tasks.kria_runtime import _simpler_version_offer  # noqa: PLC0415

    brief = CreativeBrief(version=1, requirements=[*INCIDENT, FREEZE])
    report = _report(brief.requirements)

    assert report["ready"] is False
    assert _held(report) == ["u1"]
    assert _rows(report)["u1"] == ("unsupported", NO_FREEZE)
    assert readiness_reply(brief, report) == (
        f"I can't make this yet:\n- Freeze the last frame for 2 seconds: {NO_FREEZE}"
    )
    sentence, question = _simpler_version_offer(brief, _held(report))
    assert sentence == "The simpler version leaves out: Freeze the last frame for 2 seconds."
    assert question["omitted_requirement_ids"] == ["u1"]


# -- Other routes (F7, F15, F16) ------------------------------------------------------------------

NOISE = _r("n1", "audio", "Keep the crowd noise quietly under my voice.")


def _route_receipts(strategy: dict, **facts):
    from app.kria.brief_checks import build_receipts, plan_facts_from_strategy  # noqa: PLC0415

    manifest = _manifest()
    built = dataclasses.replace(
        plan_facts_from_strategy(
            strategy, clip_ids=[m.media_id for m in manifest.media], manifest=manifest
        ),
        **facts,
    )
    receipts = build_receipts([R2, R11, NOISE], built, include_unchecked=True, strict_order=True)
    return {r.requirement_id: (r.status, r.verification, r.reason) for r in receipts}


def test_a_talking_draft_answers_as_today(phone_voiceover) -> None:
    rows = _route_receipts(
        {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "caption_style": "karaoke",
        },
        phone_talking=True,
        caption_words_at_render=True,
        phone_text=True,
    )
    assert rows["r2"] == (
        "partial",
        "checked",
        "Every voice in your take gets captions, off-camera ones too; they'll be in German, "
        "and I'll check that in the finished video",
    )
    assert rows["r11"] == (
        "not_possible",
        "checked",
        "A Talking video can't do this, so it stays as filmed",
    )


def test_a_montage_draft_answers_as_today(phone_voiceover) -> None:
    rows = _route_receipts(
        {"edit_format": "montage", "audio_strategy": "original_audio"},
        phone_montage=True,
        phone_text=True,
    )
    # Nothing is judged there, before or after (the neutral wording is never shown).
    for rid in ("r2", "r11", "n1"):
        assert rows[rid][:2] == ("partial", "unchecked"), rid


def test_a_cloud_voiceover_draft_keeps_the_captions_hold_and_says_the_usual_mix(
    phone_voiceover,
) -> None:
    """F7 / F16: no captions promise off the phone; the crowd ask reads as the "crowd noise"
    wording already does on a cloud voiceover (the bed is that format's own mix, KRI-537)."""
    media = _media() | {
        "clip_paths": [
            f"users/de1/originals/{c['media_id'].replace('analysis-proxy-', '')}" for c in CLIPS
        ]
    }
    rows = _rows(
        _report(
            [R2, R11, NOISE],
            _draft(beats=False),
            media=media,
            manifest=_manifest(phone=False),
        )
    )
    assert rows["r2"] == ("evidence_missing", CANT_CONFIRM)
    assert rows["n1"] == ("executable", UNDER_VOICE)
    assert rows["r11"] == rows["n1"]


def test_an_editor_turn_and_a_finished_render_never_get_the_draft_promise(phone_voiceover) -> None:
    from app.kria import brief_checks as bc  # noqa: PLC0415

    for extra in ({"editor": True}, {"rendered_variant": True}):
        rows = _route_receipts(_draft(), phone_narrated=True, **extra)
        assert rows["r2"][2] != getattr(bc, "_NARRATED_CAPTIONS_PLAN", object()), extra


# -- After the render (F10, F17) ------------------------------------------------------------------

DE_WORDS = [(w["text"], w["start_s"], w["end_s"]) for w in FIXTURE["voiceover"]["words"]]
# The same sixteen seconds said in English: the review reads the captions' language off their
# text first (KRI-838), so "another language" has to be another language's words.
EN_WORDS = [
    (word, round(0.8 + i * 0.75, 2), round(1.4 + i * 0.75, 2))
    for i, word in enumerate(
        "On Wednesday night we went to the stadium and the announcer read out the "
        "starting lineup before the game.".split()
    )
]


def _rendered(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spoken: str = "de",
    caption_style: str | None = "word",
    bed_level: float = 0.15,
):
    """The real `_run_phone_narrated_job` and finished-video review; only speech-to-text is
    faked (the kit's German words, or English ones for ``spoken="en"``)."""
    import app.pipeline.transcribe as transcribe_mod  # noqa: PLC0415
    import app.services.phone_reaction_grounding as grounding  # noqa: PLC0415
    import app.services.phone_visuals as phone_visuals  # noqa: PLC0415
    import app.tasks.generative_build as gb  # noqa: PLC0415
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415
    from app.tasks import kria_runtime  # noqa: PLC0415
    from tests.tasks.test_phone_subtitled_narrated_dispatch import (  # noqa: PLC0415
        Transcript,
        _make_fake_bind,
        _setup_narrated,
        _words,
    )

    words = _words(*(DE_WORDS if spoken == "de" else EN_WORDS))
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        edit_format="narrated_planned",
        n_clips=2,
        spoken_words=words,
        voiceover_s=FIXTURE["voiceover"]["duration_s"],
        clip_duration_s=30.0,
        extra_candidates={
            "language": spoken,
            "voiceover_bed_level": bed_level,
            **({"voiceover_caption_style": caption_style} if caption_style else {}),
        },
    )
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper",
        lambda *a, **k: Transcript(words=words, language=spoken),
        raising=False,
    )
    monkeypatch.setattr(grounding, "_load_ready_pool_assets", lambda *a, **k: [])
    monkeypatch.setattr(grounding, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", _make_fake_bind([]))
    job.all_candidates["creator_strategy"] = {
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
    }
    binding = BriefBinding.create(uuid.uuid4(), CreativeBrief(version=2, requirements=[R2, R11]))
    snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return text, {row["requirement_id"]: row for row in payload}, variant


def test_the_review_judges_the_incident_video_from_what_it_has(monkeypatch) -> None:  # noqa: ANN001
    """German captions, a word at a time, the footage at 0.15 under the voice: both Done."""
    text, rows, variant = _rendered(monkeypatch)

    assert variant["caption_language"] == "de" and variant["voiceover_caption_style"] == "word"
    assert "Left out" not in text
    assert (rows["r2"]["status"], rows["r2"]["verification"]) == ("met", "checked"), rows["r2"]
    assert rows["r2"]["reason"] == "The captions are in German."
    assert (rows["r11"]["status"], rows["r11"]["verification"]) == ("met", "checked"), rows["r11"]
    assert rows["r11"]["reason"] == "Your footage sound sits under your voice at about 15% volume"


def test_the_review_says_so_when_the_captions_are_sentences(monkeypatch) -> None:  # noqa: ANN001
    """F10: the Voiceover record knows only the language; the word-by-word part is read off the
    captions the phone drew."""
    _text, rows, variant = _rendered(monkeypatch, caption_style=None)

    assert variant["voiceover_caption_style"] == "sentence"
    assert rows["r2"]["status"] == "partial", rows["r2"]
    assert "Captions are on as full sentences, not word by word." in rows["r2"]["reason"]


def test_the_review_says_so_when_the_captions_are_in_another_language(monkeypatch) -> None:  # noqa: ANN001
    _text, rows, _variant = _rendered(monkeypatch, spoken="en")

    assert rows["r2"]["status"] == "partial", rows["r2"]
    assert "not German" in rows["r2"]["reason"]


def test_the_review_says_so_when_the_crowd_is_not_under_the_voice(monkeypatch) -> None:  # noqa: ANN001
    """F17."""
    _text, rows, _variant = _rendered(monkeypatch, bed_level=1.0)

    assert rows["r11"]["status"] == "partial", rows["r11"]
    assert rows["r11"]["reason"] == "The footage sound plays at full volume alongside your voice"


# -- Hold 2: the captions repair ------------------------------------------------------------------


def _policy_inputs(edit_format: str):
    """A phone Voiceover draft (the incident's) or a one-take phone Talking draft."""
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from tests.kria import test_strategy_policy as policy_kit  # noqa: PLC0415

    if edit_format == "subtitled":
        return policy_kit._talking_manifest(), policy_kit._talking(caption_style="none")
    return _manifest(), CreativeStrategy.model_validate(_draft("none", beats=False))


def _policy(edit_format: str = "narrated_planned", **kwargs):
    from app.kria.strategy_policy import check_strategy_for_runtime_v2  # noqa: PLC0415

    manifest, strategy = _policy_inputs(edit_format)
    return check_strategy_for_runtime_v2(
        manifest,
        strategy,
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
        **kwargs,
    )


@pytest.mark.parametrize("edit_format", ["narrated_planned", "subtitled"])
def test_the_captions_repair_is_a_notice_when_nobody_asked_for_no_captions(
    phone_voiceover, edit_format
) -> None:
    """F19 / F21 / F24: the repaired strategy is drafted with the notice, and is a fixed point."""
    from app.kria.approval_readiness import simplification_refusal  # noqa: PLC0415
    from app.kria.strategy_policy import CheckedStrategy  # noqa: PLC0415

    checked = _policy(edit_format, creator_asked_no_captions=False)

    assert isinstance(checked, CheckedStrategy), checked
    assert checked.strategy.caption_style == "auto"
    assert CAPTIONS_KEPT in checked.notices
    manifest, _strategy_asked = _policy_inputs(edit_format)
    assert simplification_refusal(manifest, checked.strategy.model_dump(mode="json")) is None


@pytest.mark.parametrize("edit_format", ["narrated_planned", "subtitled"])
@pytest.mark.parametrize(
    "kwargs", [{"creator_asked_no_captions": True}, {}], ids=["asked", "unknown"]
)
def test_a_no_captions_ask_keeps_todays_question(phone_voiceover, edit_format, kwargs) -> None:
    """F20 / F22: a live ask, or a caller that does not know the brief."""
    from app.kria.strategy_policy import RefusedStrategy  # noqa: PLC0415

    refused = _policy(edit_format, **kwargs)

    assert isinstance(refused, RefusedStrategy), refused
    assert refused.fields == ("caption_style",)
    assert refused.question == HOLD_2


def test_only_a_live_no_captions_ask_counts(phone_voiceover) -> None:
    """F25."""
    from app.kria.brief_checks import brief_asks_no_captions  # noqa: PLC0415

    assert brief_asks_no_captions(None) is False
    assert brief_asks_no_captions(CreativeBrief(version=1, requirements=INCIDENT)) is False
    assert brief_asks_no_captions(CreativeBrief(version=1, requirements=[NO_CAPTIONS])) is True
    assert (
        brief_asks_no_captions(
            CreativeBrief(
                version=1,
                requirements=[_r("c9", "style", "Drop the subtitles please.")],
            )
        )
        is True
    )
    waived = NO_CAPTIONS.model_copy(update={"status": "superseded"})
    assert brief_asks_no_captions(CreativeBrief(version=2, requirements=[waived])) is False


async def _planned_turn(monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief):
    """The REAL planner step over a real thread, for a strategy carrying ``caption_style: none``
    (what the Main Creator wrote on the incident's second turn)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy  # noqa: PLC0415
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.kria import planner  # noqa: PLC0415
    from tests.kria import test_kri759_simpler_version_refusal as k759  # noqa: PLC0415

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(return_value=k759.PlannedIntentResolution([], k759.IntentResolution(intents=[]))),
    )
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    _user_id, thread_id, item_id = k759._seed_runtime_project()
    action = ProposeStrategy(
        kind="propose_strategy",
        # No title: this harness has no creator message to ground one in.
        strategy=CreativeStrategy.model_validate(_draft("none", beats=False, title=False)),
        summary="Your voiceover over the match clips.",
    )
    async with AsyncSessionLocal() as db:
        return await planner._plan_from_creator_output(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=uuid.uuid4(),
            user_message="Make it without these",
            manifest=_manifest(),
            inputs=planner._CreatorInputs(
                agent_input=SimpleNamespace(), intent_clips=[], creator_request="Make it"
            ),
            output=SimpleNamespace(action=action),
            effective_brief=brief,
        )


@pytest.mark.asyncio
async def test_the_planner_drafts_instead_of_asking_a_second_time(  # noqa: ANN001
    phone_voiceover, monkeypatch
) -> None:
    """F19 / F23, the incident's second hold: r2 and r11 were waived, nobody asked for no
    captions, and the plan carried ``caption_style: "none"``."""
    from app.database import engine as async_engine  # noqa: PLC0415

    waived = [
        req.model_copy(update={"status": "superseded"}) if req.id in ("r2", "r11") else req
        for req in INCIDENT
        if req.id != "r12"
    ]
    try:
        planned = await _planned_turn(monkeypatch, CreativeBrief(version=2, requirements=waived))
    finally:
        await async_engine.dispose()

    assert planned.plan.mode == "act", planned.plan.response
    assert planned.plan.choice_question is None
    arguments = planned.plan.intents[0].arguments
    assert arguments["strategy"]["caption_style"] == "auto"
    assert CAPTIONS_KEPT in arguments["summary"]


@pytest.mark.asyncio
async def test_the_planner_still_asks_when_the_creator_wants_no_captions(  # noqa: ANN001
    phone_voiceover, monkeypatch
) -> None:
    """F20, through the planner."""
    from app.database import engine as async_engine  # noqa: PLC0415

    try:
        planned = await _planned_turn(
            monkeypatch, CreativeBrief(version=1, requirements=[R1, NO_CAPTIONS])
        )
    finally:
        await async_engine.dispose()

    assert planned.plan.mode == "respond"
    assert planned.plan.response == HOLD_2
    assert planned.plan.choice_question["dropped_fields"] == ["caption_style"]


# -- Hold copy (F26-F28) --------------------------------------------------------------------------

PUNCTUATION = [
    _r("p1", "timing", "Freeze the last frame for 2 seconds."),
    _r("p2", "timing", "Freeze the first frame for 1 second"),
    _r("p3", "timing", "Freeze the goal for 2 seconds!"),
    _r("p4", "text", "End card.", literal="Folge mir."),
    _r("p5", "text", "", literal="Bis bald."),
]


@pytest.mark.parametrize("language", ["en", "tr", "de"])
def test_hold_copy_has_no_doubled_punctuation(phone_voiceover, monkeypatch, language) -> None:  # noqa: ANN001
    from app.kria.approval_readiness import readiness_reply  # noqa: PLC0415
    from app.tasks.kria_runtime import _simpler_version_offer  # noqa: PLC0415

    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    brief = CreativeBrief(version=3, requirements=PUNCTUATION)
    with reply_language_for(language):
        report = _report(brief.requirements, _draft(beats=False))
        held = _held(report)
        reply = readiness_reply(brief, report)
        sentence, question = _simpler_version_offer(brief, held)
    [option] = [o for o in question["options"] if o["key"] == "simpler_version"]

    assert held == ["p1", "p2", "p3", "p4", "p5"]
    for copy in (reply, sentence, option["description"]):
        for bad in (".:", ".;", ".."):
            assert bad not in copy, (bad, copy)
    # F27: nothing but a closing full stop is touched.
    names = (
        "Freeze the last frame for 2 seconds",
        "Freeze the first frame for 1 second",
        "Freeze the goal for 2 seconds!",
        'End card. ("Folge mir.")',
        '"Bis bald."',
    )
    listed = "; ".join(names)
    assert sentence.endswith(f": {listed}."), sentence
    assert option["description"].endswith(f": {listed}"), option["description"]
    lines = reply.split("\n")[1:]
    assert len(lines) == len(names)
    for line, name in zip(lines, names, strict=True):
        assert line.startswith(f"- {name}: "), line
    # F28: the card still records exactly what it named, at this brief version.
    assert question["omitted_requirement_ids"] == held
    assert question["brief_version"] == 3


def test_the_incident_hold_copy_reads_cleanly_with_the_switch_off(  # noqa: ANN001
    phone_voiceover, monkeypatch
) -> None:
    """The incident's own two lines (still reachable with the captions switch off)."""
    from app.kria.approval_readiness import readiness_reply  # noqa: PLC0415
    from app.tasks.kria_runtime import _simpler_version_offer  # noqa: PLC0415

    monkeypatch.setattr(settings, FLAG, False)
    brief = CreativeBrief(version=1, requirements=[R1, R2, R12])
    report = _report(brief.requirements, _draft(beats=False))

    assert readiness_reply(brief, report) == (
        f"I can't make this yet:\n- German captions, word by word: {CANT_CONFIRM}"
    )
    sentence, question = _simpler_version_offer(brief, _held(report))
    assert sentence == "The simpler version leaves out: German captions, word by word."
    [option] = [o for o in question["options"] if o["key"] == "simpler_version"]
    assert option["description"] == "Leaves out: German captions, word by word"


# -- The incident's first turn through the real runtime (scratch Postgres) ------------------------

REQUEST = (
    "My voiceover is in German. German captions, word by word. Show each thing when I say it: "
    'the train on "Zug", the stadium on "Stadion", the light show on "Lichtshow", the goal on '
    '"Tor", the app on "Die App". Keep the crowd quietly under my voice. '
    f'Title: "{TITLE}"'
)


def _seed_incident_project(requirements) -> tuple[uuid.UUID, uuid.UUID]:
    """A phone Voiceover project at the incident's state before the first draft: the kit's
    two clips as phone proxies, a recorded voiceover, and the brief as the job pinned it."""
    from app.database import sync_session  # noqa: PLC0415
    from app.models import CreationThread, CreativeBriefVersion, PlanItem  # noqa: PLC0415
    from tests.kria.test_runtime_postgres_integration import (  # noqa: PLC0415
        _seed_runtime_project,
    )

    user_id, thread_id, _session_id = _seed_runtime_project()
    media = _media()
    rows = [
        {
            **clip,
            "gcs_path": path,
            "manifest_identity": clip["media_id"],
            "storage_generation": "1",
            "source_filename": clip["media_id"],
            # What the phone registers with each proxy (the original stays on the iPhone).
            "upload_contract": {
                "purpose": "analysis_proxy",
                "proxy": {
                    "width": 320,
                    "height": 568,
                    "duration_s": clip["duration_s"],
                    "frame_rate": 30.0,
                    "timing_version": 1,
                    "orientation_degrees": 0,
                    "original": {
                        "kind": "video",
                        "width": 1080,
                        "height": 1920,
                        "sha256": f"{index + 1:064x}",
                        "has_audio": True,
                        "byte_count": 12_000_000,
                        "duration_s": clip["duration_s"],
                        "orientation_degrees": 0,
                    },
                },
            },
        }
        for index, (clip, path) in enumerate(
            zip(media["clip_assignments"], media["clip_paths"], strict=True)
        )
    ]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "narrated_planned"
        item.audio_mode = "voiceover"
        item.clip_gcs_paths = list(media["clip_paths"])
        item.clip_assignments = rows
        item.voiceover_gcs_path = "users/kri1465/voice.m4a"
        item.voiceover_generation = "1"
        item.voiceover_duration_s = FIXTURE["voiceover"]["duration_s"]
        thread.state = {
            "media": [{"media_id": row["media_id"], "filename": row["media_id"]} for row in rows],
            "media_count": len(rows),
            "edit_format": "narrated_planned",
            "reply_language": "en",
        }
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[req.model_dump(mode="json") for req in requirements],
            )
        )
        db.commit()
    return user_id, thread_id


async def _first_turn(monkeypatch: pytest.MonkeyPatch, requirements, strategy: dict):
    """`submit_turn` + `run_kria_turn` for the creator's request; only the Main Creator's model
    call (the strategy it proposed) and the clip-intent model step are replaced."""
    import asyncio  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from app.agents._schemas.creator_agent import (  # noqa: PLC0415
        CreativeStrategy,
        CreatorRenderIntentEvidence,
        ProposeStrategy,
    )
    from app.agents.main_creator import MainCreatorOutput  # noqa: PLC0415
    from app.database import AsyncSessionLocal, sync_session  # noqa: PLC0415
    from app.kria import planner  # noqa: PLC0415
    from app.kria.api_schemas import SubmitTurnBody  # noqa: PLC0415
    from app.kria.brief import load_latest_brief_sync  # noqa: PLC0415
    from app.kria.runtime import submit_turn  # noqa: PLC0415
    from app.models import CreationThread, CreationThreadEvent  # noqa: PLC0415
    from app.tasks.kria_runtime import run_kria_turn  # noqa: PLC0415
    from tests.kria import test_kri759_simpler_version_refusal as k759  # noqa: PLC0415

    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_choice_questions_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    for name in ("kria_creative_brief_user_ids", "kria_brief_binding_user_ids"):
        monkeypatch.setattr(settings, name, [])
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(return_value=k759.PlannedIntentResolution([], k759.IntentResolution(intents=[]))),
    )
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())

    async def _model(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return MainCreatorOutput(
            action=ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy.model_validate(strategy),
                summary="Your German voiceover over the match clips.",
                render_intent_evidence=CreatorRenderIntentEvidence(
                    opening_title=f'Title: "{TITLE}"'
                ),
            ),
        )

    monkeypatch.setattr(planner, "_call_main_creator", _model)
    user_id, thread_id = _seed_incident_project(requirements)
    with sync_session() as db:
        revision = int(db.get(CreationThread, thread_id).revision)
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=REQUEST,
                client_event_id=f"kri1465-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    with sync_session() as db:
        reply = (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        brief = load_latest_brief_sync(db, thread_id)
        return result["status"], reply.event_type, reply.content, dict(reply.payload or {}), brief


@pytest.mark.asyncio
async def test_the_incident_turn_drafts_with_nothing_held_or_waived(  # noqa: ANN001
    phone_voiceover, monkeypatch
) -> None:
    """F1 / F11 end to end. Prod answered this turn with "I can't make this yet:" and the
    "Make it without these" card for r2 and r11 (the pop-in rows are left out here: their
    Visuals are not in this project, and they held nothing in prod)."""
    from app.database import engine as async_engine  # noqa: PLC0415

    try:
        status, event_type, content, payload, brief = await _first_turn(
            monkeypatch, [R1, R2, R11, R12], _draft("karaoke", beats=False)
        )
    finally:
        await async_engine.dispose()

    assert (status, event_type) == ("awaiting_approval", "draft_applied"), content
    assert payload.get("choice_question") is None
    assert "I can't make this yet" not in content
    assert "leaves out" not in content
    assert {req.id for req in brief.live()} == {"r1", "r2", "r11", "r12"}
    assert "Will do at render: German captions, word by word." in content
    assert "Done: Keep the crowd quietly under my voice." in content


@pytest.mark.asyncio
async def test_a_real_unsupported_ask_still_gets_the_card_for_it_alone(  # noqa: ANN001
    phone_voiceover, monkeypatch
) -> None:
    """F18 / F26 end to end: the hold, the card and its copy for the one ask that is held."""
    from app.database import engine as async_engine  # noqa: PLC0415

    try:
        status, event_type, content, payload, brief = await _first_turn(
            monkeypatch, [R1, R2, R11, R12, FREEZE], _draft("karaoke", beats=False)
        )
    finally:
        await async_engine.dispose()

    assert (status, event_type) == ("completed", "assistant_response"), content
    question = payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["u1"]
    [option] = [o for o in question["options"] if o["key"] == "simpler_version"]
    assert option["description"] == "Leaves out: Freeze the last frame for 2 seconds"
    assert f"- Freeze the last frame for 2 seconds: {NO_FREEZE}" in content
    assert "The simpler version leaves out: Freeze the last frame for 2 seconds." in content
    assert "German captions" not in content.split("I held this first draft back.")[1]
    for bad in (".:", ".;", ".."):
        assert bad not in content, content
    assert {req.id for req in brief.live()} == {"r1", "r2", "r11", "r12", "u1"}


PROD_HOLD_1 = (
    "I can't make this yet:\n"
    "- German captions, word by word.: I can't confirm this draft does it yet.\n"
    "- Keep the crowd quietly under my voice.: I can't confirm this draft does it yet.\n"
    "I held this first draft back. The simpler version leaves out: German captions, word by "
    "word.; Keep the crowd quietly under my voice.. Should I make it without these, or do you "
    "want to change your request?"
)


@pytest.mark.asyncio
async def test_red_control_the_old_behaviour_writes_prods_hold_word_for_word(  # noqa: ANN001
    phone_voiceover, monkeypatch
) -> None:
    """The replay is the incident: with this fix's three changes switched back (the captions
    switch off, the bed reader without the bare source, the joins on ``text()``), the same
    turn writes prod's first hold exactly, card and all."""
    from app.database import engine as async_engine  # noqa: PLC0415
    from app.kria import brief_checks as bc  # noqa: PLC0415

    monkeypatch.setattr(settings, FLAG, False)
    monkeypatch.setattr(
        bc,
        "_names_footage_sound",
        lambda _req, text: any(m.group("bare") is None for m in bc._BED_NOISE_RE.finditer(text)),
    )
    monkeypatch.setattr(BriefRequirement, "listed", BriefRequirement.text)
    try:
        status, event_type, content, payload, _brief = await _first_turn(
            monkeypatch, [R1, R2, R11, R12], _draft("karaoke", beats=False)
        )
    finally:
        await async_engine.dispose()

    assert (status, event_type) == ("completed", "assistant_response")
    assert content == PROD_HOLD_1
    question = payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["r2", "r11"]
    [option] = [o for o in question["options"] if o["key"] == "simpler_version"]
    assert option["description"] == (
        "Leaves out: German captions, word by word.; Keep the crowd quietly under my voice."
    )
    assert payload["brief_coverage"]["reason"] == "approval_requirements_not_ready"
