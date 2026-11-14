"""KRI-1405: a German Talking draft is held over its own render promises.

Prod 2026-11-13 19:16Z (build 1977, thread 8044373b, ylab German Talking test): the first reply
was "Das kann ich noch nicht:" over the pause cut (r4), the repeated-sentence cut (r5) and German
word-by-word captions with the creator's spellings (r6). All three are plans the phone Talking
render carries out, and the video had all three; "Ohne diese erstellen" still listed them as
left out, and the creator's later "Untertitel wie gewünscht behalten" was ignored. The same
reply called "use all five clips" (r2) and "no sound effects" (r10) things a Talking video
can't do, though both were met.

Root causes: approval readiness and the reply matched promise reasons against English and
Turkish text only, so the German line never matched; the captions plan was no promise at all;
a keep answer on a later card did not reach the simpler-version consent.

Ways the fix can fail, each pinned below:

 1. a reply language's promise text is missing from the list (today's bug): every catalog
    language, every promise, through `_loc` exactly as a receipt writes it;
 2. the same gap in the other reason lists (format limits, "can't check", Talking answers);
 3. the prod replay still holds r4/r5/r6, or the hold reply still names them;
 4. a captions plan that really falls short (a language Nova can't caption, sentence lines for
    a word-by-word ask) becomes approvable;
 5. a German shortfall that only shares words with a promise becomes approvable;
 6. "use all clips" on a one-clip Talking edit, or sound pop-ins under "no sound effects", read
    as met;
 7. the hold header says "please choose" for a real "can't", or without the card to choose on;
 8. a keep answer on the tap's own turn (a KRI-1113 restatement) un-drops the ask, or a keep on
    a later card is ignored.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.approval_readiness import (
    APPROVABLE,
    _only_promises,
    _promised_at_render,
    assess_readiness,
    readiness_reply,
)
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief
from app.kria.reply_language import CATALOG_REPLY_LANGUAGES, reply_language_for
from app.services import creator_capabilities as capabilities
from app.tasks.kria_runtime import _kept_after_tap

CREATOR = uuid.UUID("00000000-0000-4000-8000-000000001405")
TURN_1 = "f1ad251d-8d81-4b81-b56d-4e32ba91c630"
# The five clips of thread 8044373b, in capture order (job 1f2de191's media snapshot).
CLIPS = [
    f"analysis-proxy-ios-{name}.mp4"
    for name in (
        "22F8E08F-D5E8-44B8-B019-E48B05ADBFD3",
        "A66C5033-AF7A-422B-B6A0-DDDB845B7D5B",
        "B21B5E7D-6183-429B-8FC2-6A8FDC90B03D",
        "51A1FA37-E2DD-4174-998C-8F513B8B78E4",
        "42BF616E-3977-46DC-AEB4-D7A7899CD3EB",
    )
]
DURATIONS = (34.1, 25.666667, 10.9, 22.433333, 18.5)
CAPTURED = ("18:13:16", "18:18:10", "18:22:39", "18:25:33", "18:27:16")
LANGUAGES = ("en", "tr", *sorted(CATALOG_REPLY_LANGUAGES))
SPELLINGS = [
    "Usersmagic",
    "Nike",
    "Delivery Hero",
    "Mina Foundation",
    "Mexico City",
    "Kria",
    "YLAB",
    "207.000 Nutzer",
    "72 Projekte",
    "eine Million Dollar",
]


def _r(rid: str, kind: str, description: str | None, *, literal=None, facts=None, scope="global"):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=scope,
        description=description,
        literal=literal,
        facts=facts or {},
        source_turn_id=TURN_1,
    )


# Brief v1 of the thread, as job 1f2de191's binding holds it (admin jobs debug, read-only), with
# r4-r6 back to "open" and `superseded_by` dropped: what turn 1 read.
BRIEF = CreativeBrief(
    version=1,
    requirements=[
        _r("r1", "style", "Ein „Talking“-Video mit direkter Ansprache in die Kamera auf Deutsch."),
        _r(
            "r2",
            "select",
            "Alle fünf Clips als Teile derselben Vorstellung nutzen und keine Inhalte weglassen, "
            "außer den ausdrücklich genannten Pausen und Wiederholungen.",
            facts={"all": True},
        ),
        _r(
            "r3",
            "order",
            "Die fünf Clips in der Reihenfolge der Aufnahme verwenden und nichts umstellen.",
            facts={"key": "capture_time"},
        ),
        _r(
            "r4",
            "style",
            "Alle Sprechpausen herausschneiden, damit die Vorstellung straff wirkt.",
            facts={"pauses": "tight", "speech_cleanup": True},
        ),
        _r(
            "r5",
            "style",
            "Bei zweimal gesprochenen Sätzen nur die letzte, saubere Version behalten.",
            facts={"cut_restarts": True, "speech_cleanup": True},
        ),
        _r(
            "r6",
            "style",
            "Deutsche Untertitel Wort für Wort, gut lesbar und nie über meinem Gesicht. Die "
            "angegebenen Namen und Zahlen exakt so schreiben.",
            facts={
                "captions": "word_by_word",
                "spellings": SPELLINGS,
                "keep_text_off": [{"kind": "face"}],
                "caption_language": "de",
            },
        ),
        _r("r7", "text", None, literal="Ihr sucht eine Feuerwehr?", scope="title"),
        _r(
            "r8",
            "style",
            "Kein weiterer Text außer dem Titel und den ausdrücklich gewünschten Untertiteln.",
        ),
        _r("r9", "audio", "Keine Musik.", facts={"audio_intent": "no_music"}),
        # Prod's r10 had no facts (the brief writer had no typed "no sound effects" yet); this
        # fix teaches it `no_sound_effects`, which the replay below uses.
        _r("r10", "audio", "Keine Soundeffekte.", facts={"audio_intent": "no_sound_effects"}),
    ],
)
# The strategy the turn-3 draft applied (thread turn 986ec448), rationale shortened.
STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "rationale": "Deine Vorstellung in Aufnahmefolge mit deutschen Untertiteln.",
    "edit_format": "subtitled",
    "caption_style": "karaoke",
    "opening_title": "Ihr sucht eine Feuerwehr?",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "target_duration_s": 112,
    "selected_media_ids": CLIPS,
    "video_reuse_policy": "once",
    "optional_treatments": [],
}
# The recorded thread (request, brief v1, turn-1 readiness rows, the tap and the keep answer).
THREAD = json.loads((Path(__file__).parent / "kri1405_ylab_thread.json").read_text("utf-8"))
# What prod's readiness said on turn 1 (event seq 11, `brief_coverage.approval_readiness`).
PROD_ROWS = {row["requirement_id"]: row for row in THREAD["turn_1_hold"]["approval_readiness"]}
PROMISE_ROWS = ("r4", "r5", "r6")


@pytest.fixture
def ylab_prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod on 2026-11-13: German replies, several-clip phone Talking and its cleanup, tight
    pauses (the job cut 45 pauses across the five takes)."""
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)
    monkeypatch.setattr(settings, "talking_multi_take_cleanup_enabled", True)
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True, raising=False)
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)


def _media() -> list[dict]:
    return [
        {"media_id": clip, "kind": "video", "duration_s": seconds, "has_audio": True}
        for clip, seconds in zip(CLIPS, DURATIONS, strict=True)
    ]


def _snapshot_rows() -> list[dict]:
    """The binding snapshot's clip rows: the manifest rows plus each clip's capture time."""
    return [
        {**row, "capture": {"capture_time": f"2026-11-13T{at}Z"}}
        for row, at in zip(_media(), CAPTURED, strict=True)
    ]


def _manifest():
    return capabilities.resolve_creator_manifest(
        item_id="item-1405",
        edit_format="subtitled",
        media=_media(),
        phone_source_media_ids=CLIPS,
        phone_rendering_allowed=True,
    )


def _assess(brief: CreativeBrief = BRIEF, strategy: dict | None = None) -> dict:
    return assess_readiness(
        brief,
        dict(strategy or STRATEGY),
        media={"clip_paths": list(CLIPS), "clip_assignments": _snapshot_rows()},
        manifest=_manifest(),
        creator_id=CREATOR,
    )


def _rows(report: dict) -> dict[str, dict]:
    return {row["requirement_id"]: row for row in report["requirements"]}


def _facts(strategy: dict | None = None):
    import dataclasses

    facts = bc.plan_facts_from_strategy(dict(strategy or STRATEGY))
    return dataclasses.replace(
        facts, phone_talking=True, caption_words_at_render=True, video_clip_count=len(CLIPS)
    )


# ------------------------------------------- 1-2. every language, every reason list


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_render_promise_is_one_in_every_reply_language(ylab_prod, language):
    """Each promise as `_loc` writes it in that language, alone and joined, is a promise."""
    with reply_language_for(language):
        written = [bc._loc(promise) for promise in bc.RENDER_PROMISE_REASONS]
    for text in written:
        assert text in bc.RENDER_PROMISE_TEXTS, (language, text)
        assert _only_promises(text), (language, text)
    assert _only_promises("; ".join(written[:3])), language


@pytest.mark.parametrize("language", sorted(CATALOG_REPLY_LANGUAGES))
def test_every_catalog_language_translates_every_matched_reason(language):
    """A reason with no line falls back to English and still matches, but then the creator
    reads English: each matched list must be translated in every catalog language."""
    from app.kria.copy_catalog import translate

    lists = {
        "promise": bc.RENDER_PROMISE_REASONS,
        "format_limit": bc._FORMAT_LIMIT_EN,
        "neutral": tuple(sorted(bc._NEUTRAL_EN)),
        "talking_answer": bc._TALKING_HONEST_EN,
    }
    missing = {
        name: [r for r in reasons if translate(language, r) is None]
        for name, reasons in lists.items()
    }
    assert not any(missing.values()), missing


@pytest.mark.parametrize("language", LANGUAGES)
def test_format_limits_neutral_and_talking_answers_match_in_every_language(ylab_prod, language):
    with reply_language_for(language):
        limit = bc._loc(bc._TALKING_NO_SUCH_EDIT)
        neutral = bc._loc(bc._NO_CHECKER)
    assert bc.is_format_limit(limit), (language, limit)
    assert not bc.needs_creator_choice(
        {"verification": "checked", "status": "not_possible", "reason": limit}
    ), language
    assert neutral in bc._NEUTRAL_REASONS, (language, neutral)
    receipt = bc._receipt(BRIEF.requirements[0], "not_possible", limit)
    assert bc.is_judged(BRIEF.requirements[0], receipt), language


# ------------------------------------------------------------------ 3. the prod replay


def test_the_replay_brief_is_the_recorded_one():
    recorded = {r["id"]: r for r in THREAD["brief_v1"]["requirements"]}
    for req in BRIEF.requirements:
        facts = dict(recorded[req.id]["facts"])
        if req.id == "r10":
            facts["audio_intent"] = "no_sound_effects"  # taught by this fix
        assert (req.kind, req.scope, req.description, req.literal, req.facts) == (
            recorded[req.id]["kind"],
            recorded[req.id]["scope"],
            recorded[req.id]["description"],
            recorded[req.id]["literal"],
            facts,
        ), req.id
    assert THREAD["turn_1_hold"]["omitted_requirement_ids"] == list(PROMISE_ROWS)


def test_replay_reaches_prods_german_reasons(ylab_prod):
    """The replay reaches prod's own words, so the verdicts below are about prod's input."""
    with reply_language_for("de"):
        rows = _rows(_assess())
    for rid in ("r4", "r5"):
        assert PROD_ROWS[rid]["disposition"] == "needs_choice"
        assert rows[rid]["reason"] == PROD_ROWS[rid]["reason"], rows[rid]
    # r6's prod line also says "auf German" (KRI-1408's English leak): only its start is pinned.
    prefix = "Jede Stimme in deinem Take bekommt Untertitel, auch die außerhalb des Bildes"
    assert PROD_ROWS["r6"]["reason"].startswith(prefix)
    assert rows["r6"]["reason"].startswith(prefix), rows["r6"]


def test_the_german_draft_holds_nothing_kria_does_at_render(ylab_prod):
    with reply_language_for("de"):
        report = _assess()
    rows = _rows(report)
    for rid in PROMISE_ROWS:
        assert rows[rid]["disposition"] == "unverified", rows[rid]
    held = {rid for rid, row in rows.items() if row["disposition"] not in APPROVABLE}
    assert held == set(), held
    assert report["ready"] is True


@pytest.mark.parametrize("language", ["en", "tr"])
def test_english_and_turkish_turns_stay_unheld(ylab_prod, language):
    with reply_language_for(language):
        rows = _rows(_assess())
    for rid in PROMISE_ROWS:
        assert rows[rid]["disposition"] == "unverified", (language, rows[rid])


# --------------------------------------------- 4-5. real shortfalls still hold


def test_captions_in_a_language_nova_cannot_write_still_hold(ylab_prod, monkeypatch):
    """Without the translation step Nova can only caption the spoken en/tr, not German."""
    monkeypatch.setattr(settings, "caption_translation_enabled", False)
    captions = BRIEF.requirements[5]
    assert bc.talking_caption_promise(captions, _facts()) is None
    with reply_language_for("de"):
        report = _assess(CreativeBrief(version=1, requirements=[captions]))
    (row,) = report["requirements"]
    assert row["disposition"] == "needs_choice", row


def test_sentence_captions_for_a_word_by_word_ask_still_hold(ylab_prod):
    strategy = {**STRATEGY, "caption_style": "sentence"}
    facts = _facts(strategy)
    assert bc.talking_caption_promise(BRIEF.requirements[5], facts) is None


def test_a_german_shortfall_next_to_a_promise_is_not_a_promise(ylab_prod):
    """Wrong-content control: a real German refusal joined to a German promise still holds."""
    with reply_language_for("de"):
        promise = bc._loc(bc._TIGHT_IF_OFFERED)
        shortfall = bc._loc(bc._TIGHT_KIND_LONG_ONLY)
    req = BRIEF.requirements[3]
    for reason in (shortfall, f"{promise}; {shortfall}", f"{shortfall}; {promise}"):
        receipt = bc._receipt(req, "partial", reason, verification="checked")
        assert not _promised_at_render(req, receipt, _facts()), reason
    assert _promised_at_render(
        req, bc._receipt(req, "partial", promise, verification="checked"), _facts()
    )


# ---------------------------------- 6. "all five clips" and "no sound effects" are met


def test_all_clips_and_no_sound_effects_are_met_on_the_talking_draft(ylab_prod):
    facts = _facts()
    with reply_language_for("de"):
        receipts = {
            r.requirement_id: r
            for r in bc.build_receipts(
                [BRIEF.requirements[1], BRIEF.requirements[9]], facts, include_unchecked=True
            )
        }
    assert receipts["r2"].status == "met", receipts["r2"]
    assert receipts["r10"].status == "met", receipts["r10"]
    assert receipts["r10"].reason == "Keine Soundeffekte, wie gewünscht.", receipts["r10"]


def test_all_clips_on_a_one_clip_talking_draft_is_not_met(ylab_prod, monkeypatch):
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", False)
    import dataclasses

    facts = dataclasses.replace(_facts(), video_clip_count=5)
    (receipt,) = bc.build_receipts([BRIEF.requirements[1]], facts, include_unchecked=True)
    assert receipt.status != "met", receipt


def test_a_sound_pop_in_is_not_no_sound_effects(ylab_prod):
    import dataclasses

    beat = SimpleNamespace(trigger="Nike", sound="ding")
    facts = dataclasses.replace(_facts(), reaction_beats=(beat,))
    (receipt,) = bc.build_receipts([BRIEF.requirements[9]], facts, include_unchecked=True)
    assert receipt.status == "partial", receipt


# ------------------------------------------------------------ 7. the hold header


def _held_report(disposition: str) -> dict:
    return {
        "policy_problem": None,
        "requirements": [
            {"requirement_id": "r4", "disposition": disposition, "reason": "x"},
            {"requirement_id": "r1", "disposition": "executable", "reason": None},
        ],
    }


def test_header_says_choose_only_for_choices_with_a_card():
    choose = readiness_reply(BRIEF, _held_report("needs_choice"), offers_choice=True)
    assert choose.startswith("Before I make this, please choose:")
    for reply in (
        readiness_reply(BRIEF, _held_report("needs_choice")),
        readiness_reply(BRIEF, _held_report("unsupported"), offers_choice=True),
        readiness_reply(BRIEF, _held_report("evidence_missing"), offers_choice=True),
    ):
        assert reply.startswith("I can't make this yet:"), reply


def test_header_is_german_on_a_german_turn(ylab_prod):
    with reply_language_for("de"):
        reply = readiness_reply(BRIEF, _held_report("needs_choice"), offers_choice=True)
    assert reply.startswith("Bevor ich das erstelle, entscheide bitte:"), reply


# ------------------------------------------------- 8. a keep answer after the tap


def _event(selection: dict | None):
    return SimpleNamespace(payload={"choice_selection": selection} if selection else {})


def _keep(rid: str) -> BriefUpdate:
    req = next(r for r in BRIEF.requirements if r.id == rid)
    return BriefUpdate(
        operation="change",
        target_requirement_id=rid,
        expected_version=1,
        kind=req.kind,
        scope=req.scope,
        literal=req.literal,
        description=req.description,
        facts=req.facts,
    )


def test_a_keep_on_a_later_card_comes_off_the_drop_list():
    """Prod seq 14: "Untertitel wie gewünscht behalten" on the turn-2 question."""
    answer = _event({"option_key": "option_1", "question_id": "b49026ad"})
    assert _kept_after_tap([_keep("r6")], ["r4", "r5", "r6"], answer) == {"r6"}


def test_a_restatement_on_the_tap_itself_keeps_nothing():
    """KRI-1113: the model restating what the tap leaves out is not a keep."""
    tap = _event({"option_key": "simpler_version", "question_id": "7577d79d"})
    assert _kept_after_tap([_keep("r6")], ["r4", "r5", "r6"], tap) == frozenset()
    assert _kept_after_tap([_keep("r6")], ["r4", "r5", "r6"], _event(None)) == frozenset()


def test_a_keep_of_an_ask_the_card_never_offered_changes_nothing():
    answer = _event({"option_key": "option_1", "question_id": "b49026ad"})
    assert _kept_after_tap([_keep("r7")], ["r4", "r5", "r6"], answer) == frozenset()
    remove = BriefUpdate(operation="remove", target_requirement_id="r6", expected_version=1)
    assert _kept_after_tap([remove], ["r4", "r5", "r6"], answer) == frozenset()


# ------------------------------------------------- the brief writer is taught both


def test_the_brief_writers_are_taught_no_sound_effects_and_keeping_an_ask():
    """Static only: whether a live model writes the facts is a paid check (not run)."""
    from app.agents.brief_extractor import _NARROW_BRIEF_SECTION
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION

    for section in (_BRIEF_PROMPT_SECTION, _NARROW_BRIEF_SECTION):
        assert '{"audio_intent": "no_sound_effects"}' in section
    assert "restate that requirement as a `change` of the same" in _BRIEF_PROMPT_SECTION
