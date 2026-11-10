# ruff: noqa: E501 - EXPECTED holds captured sentences verbatim, one string per sentence.
"""Copy that picked Turkish outside ``say()`` now goes through it (KRI-1362, "B").

German (``app/kria/copy/de.json``) reaches a sentence only through ``say()``/``say_in()``: a
``language == "tr"`` branch, an English-keyed ``*_TR`` table read without ``say``, or an
f-string/concatenation the catalog builder cannot key stays English for every catalog language.
These builders were rewritten to route through ``say``. Ways the rewrite could go wrong,
written down before the tests:

1. English or Turkish output changes by a byte (a template that does not refill to the old
   f-string, a dropped early return that now formats English text, a re-keyed table that
   misses an entry, a prefix rebuilt with a different separator).
2. An explicit-language builder (a stored chat language, a model input's language) starts
   reading the turn's bound language instead, or the other way round.
3. The rewritten site still does not reach the catalog: German output equals English for
   a sentence the catalog has a line for.
4. A table the builder now harvests resolves to a different string than the code holds
   (checked by ``test_copy_catalog.py``'s runtime-truth tests, not here).

(1)-(2): every case below runs bound to "en" and to "tr" (and the explicit-language ones with
that language passed) and is compared with literals captured from the code before the rewrite
(``scripts`` free: the capture ran these same case functions against the pre-change tree).
(3): with German on and a stand-in ``de`` catalog holding a line for every ``tr.json`` key,
each case marked ``reaches`` returns something other than its English output.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from app.kria import copy_catalog
from app.kria.reply_language import reply_language_for

_TR_JSON = Path(__file__).resolve().parents[2] / "app" / "kria" / "copy" / "tr.json"

# name -> (builder taking the language under test, reaches the catalog for German)
CASES: dict[str, tuple[Callable[[str], Any], bool]] = {}


def case(name: str, *, reaches: bool = True) -> Callable[[Callable[[str], Any]], Callable]:
    def register(fn: Callable[[str], Any]) -> Callable[[str], Any]:
        assert name not in CASES, name
        CASES[name] = (fn, reaches)
        return fn

    return register


# ---------------------------------------------------------------- brief_checks outcomes

_OUTCOME_ROWS = (
    {"name": "first: the goal", "op": "order", "position": "first"},
    {"name": "last: the trophy lift", "op": "order", "position": "last"},
    {"name": "then: the walk", "op": "order", "position": "then"},
    {"name": "middle: x", "op": "order", "position": "middle"},
    {"name": "the order you described", "op": "order"},
    {"name": "group by player", "op": "group"},
    {"name": "text for the intro", "op": "caption"},
    {"name": "the city name on its clips", "op": "label"},
    {"name": "keep the beach", "op": "include"},
)


@case("brief_checks._outcome_name")
def _outcome_names(lang: str) -> Any:
    from app.kria.brief_checks import _outcome_name

    return [_outcome_name(row) for row in _OUTCOME_ROWS]


@case("brief_checks._outcome_reason")
def _outcome_reasons(lang: str) -> Any:
    from app.kria.brief_checks import _OUTCOME_REASON_TR, _outcome_reason

    reasons = [
        *_OUTCOME_REASON_TR,
        "you chose strictly chronological order, so the beach is in 3 stretches; "
        "the city is in 2 stretches",
        "clips stay in the order you filmed them, so the pub is in 2 stretches; odd part",
        "a reason nobody wrote down",
    ]
    return [_outcome_reason(reason) for reason in reasons]


@case("brief_checks small reasons")
def _brief_small_reasons(lang: str) -> Any:
    from app.kria import brief_checks as bc

    sb = [bc._sb(text) for text in list(bc._REASON_TR)[:40]]
    sb += [bc._sb(text, "") for text in list(bc._REASON_TR)[:5]]
    clocks = [
        bc._unclear_clock([]),
        bc._unclear_clock([{"start_s": 3.2}]),
        bc._unclear_clock([{"start_s": 3.2}, {"start_s": 65}, {"start_s": 3.2}, {"start_s": "x"}]),
    ]
    unclear = []
    spans = [
        {"action": "omitted", "start_s": 4.0},
        {"action": "omitted", "start_s": 9.5},
        {"action": "written", "start_s": 12.0, "text": "Vela"},
        {"action": "written", "start_s": 20.0},
    ]
    for state in (
        {"state": "agree"},
        {"state": "not_comparable"},
        {"state": "error"},
        {"state": "other"},
        {"state": "applied", "spans": []},
        {"state": "applied", "spans": spans},
        {"state": "applied", "finished": True, "spans": spans},
        {"state": "applied", "finished": True, "spans": spans[2:]},
    ):
        unclear.append(bc._unclear_word_line(state))
    numbers = [bc._clip_numbers(p) for p in ([2], [2, 3], [1, 4, 7])]
    languages = [bc._language_name(code) for code in ("en", "tr", "de", "es", "xx")]
    return [sb, clocks, unclear, numbers, languages, bc._talking_cutaway_sound_not_here()]


@case("brief_checks._placement_reason and _timed_card_problem")
def _brief_placements(lang: str) -> Any:
    from app.kria.brief_checks import (
        BeatFact,
        TimedCardFact,
        _placement_reason,
        _timed_card_problem,
    )

    beats = [
        BeatFact(trigger="goal", at_s=3.25),
        BeatFact(trigger="trophy", at_s=10.0),
        BeatFact(trigger="fans", at_s=1.0),
        BeatFact(trigger="bus", at_s=None),
        BeatFact(trigger="coach", at_s=12.5),
        BeatFact(trigger="city", at_s=14.0),
        BeatFact(trigger="end", at_s=15.0),
    ]
    titled = [BeatFact(trigger="logo", anchor="title"), BeatFact(trigger="map", anchor="title")]
    placements = [
        _placement_reason([]),
        _placement_reason(beats[:1]),
        _placement_reason(beats[:2]),
        _placement_reason(beats),
        _placement_reason(titled[:1]),
        _placement_reason(titled),
        _placement_reason([*titled, *beats[:3]]),
    ]
    cards = []
    for reason in (
        "past_end",
        "face_collision",
        "overlaps_card",
        "video_not_supported",
        "visual_not_found",
        "clip_not_in_edit",
        "something_else",
        None,
    ):
        for label, at_s, end in (("the map", 12.0, 10.5), (None, 12.0, None), ("x", None, None)):
            card = TimedCardFact(
                card_id="c", label=label, at_s=at_s, reason=reason, timeline_end_s=end
            )
            cards.append(_timed_card_problem(card))
    return [placements, cards]


@case("brief_checks._unshown_sentence")
def _brief_unshown(lang: str) -> Any:
    from app.kria.brief_checks import SpeakerBackgroundFact, _unshown_sentence

    out = []
    for unshown in (
        (),
        (("the menu", "not_found"),),
        (("the menu", "not_found"), ("the bar", "not_found"), ("the sea", "x")),
        (("the menu", "phrase_not_heard"),),
        (("a", "phrase_not_heard"), ("b", "phrase_not_heard"), ("c", "not_found"), (None, "x")),
        ((None, "x"), (None, "y")),
    ):
        out.append(_unshown_sentence(SpeakerBackgroundFact(status="applied", unshown=unshown)))
    return out


@case("brief_checks.speech_cleanup_review_note")
def _brief_cleanup_notes(lang: str) -> Any:
    from app.kria.brief_checks import speech_cleanup_review_note

    def row(reason: str, start: float | None, end: float | None) -> dict[str, Any]:
        return {"reason": reason, "start_s": start, "end_s": end}

    records = (
        [row("silence", 1.0, 2.5)],
        [row("silence", 1.0, 2.5), row("filler_lexical", 4.0, 4.3)],
        [row("silence", 1.0, 2.5), row("filler_acoustic", 4.0, 4.3), row("retake", 6.0, 9.0)],
        [row("silence", 1.0, 2.5), row("silence", 5.0, 6.0), row("filler_lexical", None, None)],
        [row("retake", 1.0, 3.0), row("retake", 5.0, 7.5)],
    )
    out = []
    for archetype in ("subtitled", "narrated", "montage"):
        for outcome in ("applied", "no_change", "skipped"):
            out.append(
                speech_cleanup_review_note(
                    {"resolved_archetype": archetype, "silence_cut_outcome": outcome}
                )
            )
    for removed in records:
        for tight in (None, {"added_cuts": 2}):
            variant = {
                "resolved_archetype": "subtitled",
                "silence_cut_outcome": "applied",
                "silence_cut": {"removed": removed},
                **({"tight_pauses": tight} if tight else {}),
            }
            out.append(speech_cleanup_review_note(variant))
            out.append(speech_cleanup_review_note(variant, retake_cut_possible=True))
    return out


@case("clip question texts with N more")
def _clip_question_texts(lang: str) -> Any:
    from app.kria.clip_exclusions import _question_text as exclusions_text
    from app.kria.clip_ties import _question_text as ties_text
    from app.services.clip_intent_resolution import _found_rows_text, _unfound_rows_text
    from app.services.clip_pick_collisions import _question_text as collisions_text
    from app.services.clip_selection import _not_in_the_video_text, clip_question_text

    out = []
    for n in (1, 2, 4, 5, 7):
        labels = [f"label {i}" for i in range(n)]
        out += [
            exclusions_text(labels),
            ties_text([(label, 2) for label in labels]),
            _found_rows_text(labels),
            _unfound_rows_text(labels),
            collisions_text([(label, "other") for label in labels]),
            clip_question_text([{"label": label} for label in labels]),
            _not_in_the_video_text(labels),
        ]
    return out


@case("choice questions sweep")
def _choice_questions(lang: str) -> Any:
    """Every choice kind's fixture from the Turkish guard sweep, everything the creator reads."""
    from app.services import choice_questions as cq
    from tests.services.test_choice_questions_turkish_guard import _cases, _creator_text

    out = {}
    for name, (_kind, found) in sorted(_cases().items()):
        texts = []
        for choice in found:
            texts.append(_creator_text(choice))
            texts.append([list(option.aliases) for option in choice.options])
            if choice.kind in cq._WAITING_KINDS:
                texts.append(_creator_text(cq._waiting_reask(choice)))
        out[name] = texts
    return out


@case("choice_questions.detect_order_vs_group")
def _order_vs_group(lang: str) -> Any:
    from tests.services.test_kri520_choice_turkish import _mixed

    out = []
    for noun in (None, "sport", "group", "takım"):
        found = _mixed(noun=noun) if noun else _mixed()
        out.append(
            [
                found.intro,
                found.reason,
                [(o.label, o.description, list(o.aliases)) for o in found.options],
            ]
        )
    return out


@case("clip_order_sequence.unconfirmed_order_reason")
def _unconfirmed_orders(lang: str) -> Any:
    from app.services.clip_order_sequence import unconfirmed_order_reason

    return [
        unconfirmed_order_reason(),
        unconfirmed_order_reason(["", ""]),
        unconfirmed_order_reason(["beach"]),
        unconfirmed_order_reason(["beach", "city", "beach"]),
    ]


# ---------------------------------------------------------------- strategy_policy


@case("strategy_policy.localize_known_message")
def _known_messages(lang: str) -> Any:
    from app.kria.strategy_policy import _KNOWN_TR, localize_known_message

    texts = [
        *_KNOWN_TR,
        "Couldn't find \"Mila\"'s photo/sticker; left it out.",
        'Full screen isn\'t available for this edit yet, so "the map" shows as a small card '
        "in the corner instead.",
        "Day vlog shape needs music; kept a regular montage.",
        "Single-hero shape needs music; kept a regular montage.",
        "Reading this as a montage in the day-vlog style.",
        "Reading this as a montage in the single-hero style.",
        "The requested licensed sound effect whoosh is unavailable.",
        "Something the table does not know.",
        "Zaten Türkçe yazılmış bir not.",
    ]
    return [localize_known_message(text) for text in texts]


# ---------------------------------------------------------------- plan_review


@case("plan_review.scope_phrase")
def _scope_phrases(lang: str) -> Any:
    from app.kria.plan_review import SECTION_ORDER, scope_phrase

    sections = list(SECTION_ORDER)
    return [
        scope_phrase([]),
        *(scope_phrase([s]) for s in sections),
        scope_phrase(sections[:2]),
        scope_phrase(sections),
        scope_phrase(["unknown_section", "music"]),
    ]


# ---------------------------------------------------------------- creative copy questions


def _strip_ids(question: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in question.items() if k != "question_id"}


@case("creative_copy_decisions.authorship_question", reaches=False)
def _authorship(lang: str) -> Any:
    from app.services.creative_copy_decisions import authorship_question, wording_question

    return [
        _strip_ids(authorship_question(target="opening_title", dependency_digest="d")),
        _strip_ids(
            wording_question(
                target="opening_title", candidate="Summer in Rome", dependency_digest="d"
            )
        ),
    ]


# ---------------------------------------------------------------- clip selection label


@case("clip_selection.category_label", reaches=False)
def _category_labels(lang: str) -> Any:
    from app.schemas.clip_intents import ClipIntent
    from app.services.clip_selection import category_label

    english = ClipIntent(intent_id="a", op="group", attribute="the moment the goal is scored")
    turkish = ClipIntent(intent_id="b", op="group", attribute="gol anı ve kutlama sahnesi")
    words = "golün atıldığı an, kutlamayla birlikte"
    return [
        category_label(english),
        category_label(english, creator_words=words),
        category_label(turkish, creator_words=words),
        category_label(english, creator_words="   "),
    ]


# ---------------------------------------------------------------- kria_runtime copy


@case("kria_runtime._worker_copy")
def _worker_copies(lang: str) -> Any:
    from app.tasks.kria_runtime import _WORKER_TEXT_TR, _worker_copy

    texts = [
        *_WORKER_TEXT_TR,
        *(f"  {text}  " for text in list(_WORKER_TEXT_TR)[:3]),
        None,
        "",
        "   ",
        "An English sentence nobody translated.",
        "Bu cümle zaten Türkçe yazılmış, olduğu gibi kalsın.",
    ]
    return [_worker_copy(text) for text in texts]


@case("kria_runtime._localized_block_recovery")
def _block_recoveries(lang: str) -> Any:
    from app.kria.brief_checks import NARRATED_ALIGNMENT_RECOVERY
    from app.tasks.kria_runtime import _localized_block_recovery

    return [
        _localized_block_recovery(NARRATED_ALIGNMENT_RECOVERY, []),
        _localized_block_recovery(f"  {NARRATED_ALIGNMENT_RECOVERY}\n", []),
        _localized_block_recovery("Some other message.", []),
    ]


def _phone_gate(multi_clip: bool) -> list[str]:
    from app.tasks.content_plan_build import PHONE_GATE_MESSAGES
    from app.tasks.kria_runtime import _phone_gate_refusal_copy

    with mock.patch(
        "app.services.phone_rollout.phone_talking_multi_clip_supported", return_value=multi_clip
    ):
        return [_phone_gate_refusal_copy(r) for r in [*PHONE_GATE_MESSAGES, "no_such_reason"]]


@case("kria_runtime._phone_gate_refusal_copy")
def _phone_gate_single(lang: str) -> Any:
    return _phone_gate(False)


@case("kria_runtime._phone_gate_refusal_copy multi-clip")
def _phone_gate_multi(lang: str) -> Any:
    return _phone_gate(True)


@case("kria_runtime._job_failure_copy")
def _job_failures(lang: str) -> Any:
    from app.tasks.content_plan_build import CREATOR_FACING_DETAIL_CODES, JOB_FAILURE_MESSAGES
    from app.tasks.kria_runtime import _job_failure_copy

    out = [_job_failure_copy(code, None) for code in JOB_FAILURE_MESSAGES]
    out += [_job_failure_copy(None, None), _job_failure_copy("", None)]
    out += [_job_failure_copy("never_heard_of_it", "detail")]
    for code in sorted(CREATOR_FACING_DETAIL_CODES):
        out += [
            _job_failure_copy(code, "  The creator-facing detail.  "),
            _job_failure_copy(code, "   "),
        ]
    return out


# ---------------------------------------------------------------- explicit-language builders


@case("clip_intent_planner._preview")
def _previews(lang: str) -> Any:
    from app.agents.clip_intent_planner import _preview

    raws = [
        None,
        "not a dict",
        {"op": "label", "attribute": "player names"},
        {"op": "group", "attribute": "by city"},
        {"op": "order", "attribute": "goal first"},
        {"op": "include", "attribute": "the beach"},
        {"op": "caption", "attribute": "the menu"},
        {"op": "exclude", "attribute": "blurry ones"},
        {"op": "hide_part", "attribute": "the ad"},
        {"op": "made_up", "attribute": "x"},
        {"op": "label", "attribute": "   "},
    ]
    return [[_preview(raw), _preview(raw, lang), _preview(raw, "en")] for raw in raws]


@case("clip_intent_planner.salvage_question")
def _salvage_questions(lang: str) -> Any:
    from app.agents.clip_intent_planner import salvage_question

    labels = ["label: player names", "the beach", "group: by city", "x", "y"]
    out = []
    for language in (None, lang, "en"):
        out += [
            salvage_question(2, [], language=language),
            salvage_question(3, labels[:1], language=language),
            salvage_question(1, labels, 2, language=language),
            salvage_question(0, [], 4, language=language),
            salvage_question(5, ["w" * 300, *labels], 1, language=language),
        ]
    return out


@case("clip_intent_planner.unexpressed_notice")
def _unexpressed_notices(lang: str) -> Any:
    from app.agents.clip_intent_planner import unexpressed_notice

    out = []
    for language in (None, lang, "en"):
        out += [
            unexpressed_notice([], language=language),
            unexpressed_notice(["", ""], language=language),
            unexpressed_notice(["slow motion on Taha"], language=language),
            unexpressed_notice(["a", "b", "a"], language=language),
            unexpressed_notice(["a", "b", "c", "d", "e"], language=language),
        ]
    return out


@case("finished_style_learning._instruction")
def _instructions(lang: str) -> Any:
    from app.services.finished_style_learning import _instruction

    rows = []
    for role in ("all_text", "title", "label", "pinned", "caption", "lyric", "sticker"):
        for prop, value in (
            ("favorite_fonts", ["Inter"]),
            ("excluded_fonts", ["Comic Sans"]),
            ("position", "top_left"),
            ("position", "middle"),
            ("position", "far_corner"),
            ("color", "#FF0000"),
            ("stroke_width", 0),
            ("stroke_width", 2.5),
            ("shadow", True),
            ("shadow", False),
        ):
            rows.append(_instruction(role, prop, value, language=lang))
            rows.append(_instruction(role, prop, value))
        rows.append(_instruction(role, "excluded_fonts", ["Arial"], automatic_font=True))
        rows.append(
            _instruction(role, "excluded_fonts", ["Arial"], language=lang, automatic_font=True)
        )
    return rows


@case("named_moments.model_echo")
def _model_echoes(lang: str) -> Any:
    from app.services.named_moments import model_echo

    return [model_echo(lang), model_echo(None), model_echo("en"), model_echo("fr")]


@case("song_order.model_text")
def _model_texts(lang: str) -> Any:
    from app.services.song_order import model_text

    payloads = [
        {"song_order": {}},
        {"moment_order": {}},
        {"song_range": {}},
        {"protected_region": {"label": "  red   car "}},
        {"protected_region": {}},
        {"clip_selection": {"skipped": True}},
        {"clip_selection": {}},
        {"other": 1},
        None,
    ]
    out = []
    for payload in payloads:
        out.append(model_text(payload, "typed"))
        out.append(model_text(payload, "typed", language=lang))
        out.append(model_text(payload, None, language="en"))
    return out


@case("creation_editor_actions._applied_summary")
def _applied_summaries(lang: str) -> Any:
    from app.services.creation_editor_actions import _applied_summary

    return [
        _applied_summary([]),
        _applied_summary(["Moved clip 2 to the start"]),
        _applied_summary(["Klip 2'yi başa taşıdım"]),
        _applied_summary(["Klip 2'yi başa taşıdım", "Başlığı büyüttüm"]),
        _applied_summary(["Klip 2'yi başa taşıdım", "Made the title bigger"]),
    ]


# ---------------------------------------------------------------- the tests

# --- BEGIN EXPECTED (captured from the code before the KRI-1362 routing rewrite) ---
EXPECTED: dict[str, dict[str, Any]] = {
    "brief_checks small reasons": {
        "en": [
            [
                "I can't verify per-clip text on an editor edit..",
                "I can't verify text for that target clip in this draft..",
                "I can't confirm the order this draft uses..",
                "I couldn't confirm the clips are in a random order..",
                "You moved the clips yourself, so your own order is kept..",
                "I can't verify this ordering automatically..",
                "I couldn't match your description to the clips, so they stay in the order you attached them..",
                "I couldn't place everything you listed in the order you gave..",
                "I couldn't find every moment you named in your clip, so I can't confirm the ones playing are those moments.",
                "Some moments are your call: ones you said aren't in your clips, or let me pick.",
                "I couldn't confirm the part of the clip that plays shows the moment you named.",
                "I couldn't confirm the order your edit used..",
                "I can't verify this ordering rule, and the clips are not in an order I can show matches it..",
                "I can't check the pop-ins on this draft yet..",
                "I can't check the spoken parts on this draft yet..",
                "I can't confirm this draft keeps your whole take..",
                "I can't confirm where this draft's title came from..",
                "I didn't add a title because no creator text or grounded brief facts were available..",
                "I can't confirm this draft's length yet..",
                "A Talking edit keeps your whole take, so its length follows your clip.",
                "A Talking edit keeps only the part of your take you asked for, so its length follows that part.",
                "A Talking edit keeps the whole talking clip, so its length follows it.",
                "If that clip plays as a cutaway over your voice, it plays without its own sound; I can't tell yet which clip you talk in, and that clip keeps its sound.",
                "A voiceover edit runs as long as your voiceover.",
                "I can't verify this timing automatically..",
                "I can't check the speech cleanup on this draft yet..",
                "I can't check the captions on this draft yet..",
                "I can't verify this one automatically yet..",
                "I can't check this text style automatically..",
                "I can't check the sound mix on this draft yet..",
                "I can't change clip speed on your iPhone yet, so the clips play at normal speed.",
                "I can't change clip speed on this edit because the clips follow your song, so they play at normal speed.",
                "Add your song first and I'll use it..",
                "This draft doesn't use your song..",
                "This draft plays your song in the background; your clips aren't matched to its words..",
                "I can't change the speed of a clip that holds a freeze frame, so it plays at normal speed.",
                "I can't ramp speed inside a clip yet, so it plays at normal speed.",
                "A photo has no speed to change, so it stays as it is.",
                "The finished video plays this clip at normal speed.",
                "I couldn't tell which clip you meant for the speed change, so the clips play at normal speed. Tell me which clip.",
                "I can't verify per-clip text on an editor edit.",
                "I can't verify text for that target clip in this draft.",
                "I can't confirm the order this draft uses.",
                "I couldn't confirm the clips are in a random order.",
                "You moved the clips yourself, so your own order is kept.",
            ],
            ["", "0:03", "0:03 and 1:05"],
            [
                "Both listens heard the same words, so I didn't find an unclear word",
                "I couldn't double-check the speech on this clip, so I can't spot the unclear word: tell me the word, or say leave it out",
                "I couldn't check the unclear word in this video, so have a look at the captions",
                None,
                None,
                "I'll leave out the word I couldn't make out around 0:04 and 0:10 in your clip; I'll write the word you gave me, “Vela”, around 0:12 in your clip; I'll write the word you gave me, “”, around 0:20 in your clip",
                "I left out the word I couldn't make out at 0:04 and 0:10 in the video; I wrote the word you gave me, “Vela”, at 0:12 in the video; I wrote the word you gave me, “”, at 0:20 in the video",
                "I wrote the word you gave me, “Vela”, at 0:12 in the video; I wrote the word you gave me, “”, at 0:20 in the video",
            ],
            ["clip 2", "clips 2 and 3", "clips 1, 4 and 7"],
            ["English", "Turkish", "German", "Spanish", "XX"],
            "In a Talking edit the clips shown over your voice play without their own sound. Turning clip sound on or off by clip or by seconds works on a montage made on your iPhone, not in this edit",
        ],
        "tr": [
            [
                "Editör düzenlemesinde klip başına yazıyı doğrulayamıyorum..",
                "Bu taslakta o klibin yazısını doğrulayamıyorum..",
                "Bu taslağın kullandığı sırayı doğrulayamıyorum..",
                "Kliplerin rastgele sırada olduğunu doğrulayamadım..",
                "Klipleri kendin taşıdığın için senin sıran korundu..",
                "Bu sıralamayı otomatik olarak doğrulayamıyorum..",
                "Anlattığını kliplerle eşleştiremedim, o yüzden klipler eklediğin sırada kaldı..",
                "Saydığın her şeyi verdiğin sıraya yerleştiremedim..",
                "Saydığın her anı klipte bulamadım, o yüzden oynayanların o anlar olduğunu doğrulayamıyorum.",
                "Bazı anlar senin kararın: kliplerinde olmadığını söylediklerin ya da bana bıraktıkların.",
                "Oynayan klip parçasının söylediğin anı gösterdiğini doğrulayamadım.",
                "Düzenlemenin kullandığı sırayı doğrulayamadım..",
                "Bu sıralama kuralını doğrulayamıyorum ve klipler kurala uyduğunu gösterebileceğim bir sırada değil..",
                "Bu taslakta sözlerine göre çıkan görselleri henüz kontrol edemiyorum..",
                "Bu taslaktaki konuşma bölümlerini henüz kontrol edemiyorum..",
                "Bu taslağın çekimini baştan sona koruduğunu doğrulayamıyorum..",
                "Bu taslaktaki başlığın nereden geldiğini doğrulayamıyorum..",
                "Başlık eklemedim, çünkü kullanabileceğim bir yazın ya da isteğinde dayanabileceğim bir bilgi yoktu..",
                "Bu taslağın uzunluğunu henüz doğrulayamıyorum..",
                "Konuşmalı düzenleme çekimini baştan sona korur, o yüzden uzunluğu klibini izler.",
                "Konuşmalı düzenleme çekiminin yalnızca istediğin kısmını korur, o yüzden uzunluğu o kısmı izler.",
                "Konuşmalı düzenleme konuşma klibini baştan sona korur, o yüzden uzunluğu onu izler.",
                "O klip sesinin üzerinde gösterilirse kendi sesi olmadan oynar; hangi klipte konuştuğunu henüz bilmiyorum, konuştuğun klibin sesi kalır.",
                "Seslendirmeli düzenleme seslendirmen kadar sürer.",
                "Bu süreyi otomatik olarak doğrulayamıyorum..",
                "Bu taslakta konuşma temizliğini henüz kontrol edemiyorum..",
                "Bu taslaktaki altyazıları henüz kontrol edemiyorum..",
                "Bunu henüz otomatik olarak doğrulayamıyorum..",
                "Bu yazı stilini otomatik olarak kontrol edemiyorum..",
                "Bu taslakta ses karışımını henüz kontrol edemiyorum..",
                "iPhone'unda klip hızını henüz değiştiremiyorum, o yüzden klipler normal hızda oynuyor.",
                "Bu düzenlemede klip hızını değiştiremiyorum çünkü klipler şarkını takip ediyor, o yüzden normal hızda oynuyorlar.",
                "Önce şarkını ekle, ben de onu kullanayım..",
                "Bu taslak şarkını kullanmıyor..",
                "Bu taslak şarkını arka planda çalıyor; klipler sözlerine göre yerleştirilmedi..",
                "Donmuş kare tutan bir klibin hızını değiştiremiyorum, o yüzden normal hızda oynuyor.",
                "Bir klibin içinde hızı henüz kademeli değiştiremiyorum, o yüzden normal hızda oynuyor.",
                "Bir fotoğrafın değişecek hızı yok, o yüzden olduğu gibi kalıyor.",
                "Bitmiş video bu klibi normal hızda oynatıyor.",
                "Hız değişikliği için hangi klibi kastettiğini anlayamadım, o yüzden klipler normal hızda oynuyor. Hangi klip olduğunu yaz.",
                "Editör düzenlemesinde klip başına yazıyı doğrulayamıyorum.",
                "Bu taslakta o klibin yazısını doğrulayamıyorum.",
                "Bu taslağın kullandığı sırayı doğrulayamıyorum.",
                "Kliplerin rastgele sırada olduğunu doğrulayamadım.",
                "Klipleri kendin taşıdığın için senin sıran korundu.",
            ],
            ["", "0:03", "0:03 ve 1:05"],
            [
                "İki dinleme de aynı kelimeleri duydu, o yüzden belirsiz bir kelime bulmadım",
                "Bu klipteki konuşmayı iki kez karşılaştıramadım, o yüzden belirsiz kelimeyi bulamıyorum: kelimeyi söyle ya da çıkar de",
                "Videodaki belirsiz kelimeyi kontrol edemedim, o yüzden altyazılara bir göz at",
                None,
                None,
                "Anlayamadığım kelimeyi çıkarırım (klibinde yaklaşık 0:04 ve 0:10); Verdiğin kelimeyi yazarım: “Vela” (klibinde yaklaşık 0:12); Verdiğin kelimeyi yazarım: “” (klibinde yaklaşık 0:20)",
                "Anlayamadığım kelimeyi çıkardım (videoda 0:04 ve 0:10); Verdiğin kelimeyi yazdım: “Vela” (videoda 0:12); Verdiğin kelimeyi yazdım: “” (videoda 0:20)",
                "Verdiğin kelimeyi yazdım: “Vela” (videoda 0:12); Verdiğin kelimeyi yazdım: “” (videoda 0:20)",
            ],
            ["2. klip", "2. ve 3. klipler", "1., 4. ve 7. klipler"],
            ["İngilizce", "Türkçe", "Almanca", "İspanyolca", "XX"],
            "Konuşmalı düzenlemede sesinin üzerinde gösterilen klipler kendi sesi olmadan oynar. Klip sesini klip ya da saniye bazında açıp kapatmak yalnızca iPhone'unda yapılan montajda çalışıyor, bu düzenlemede değil",
        ],
    },
    "brief_checks._outcome_name": {
        "en": [
            "first: the goal",
            "last: the trophy lift",
            "then: the walk",
            "middle: x",
            "the order you described",
            "group by player",
            "text for the intro",
            "the city name on its clips",
            "keep the beach",
        ],
        "tr": [
            "ilk: the goal",
            "son: the trophy lift",
            "sonra: the walk",
            "middle: x",
            "tarif ettiğin sıra",
            "gruplama: player",
            "the intro için yazı",
            "kliplerindeki city adı",
            "keep the beach",
        ],
    },
    "brief_checks._outcome_reason": {
        "en": [
            "I found no clips of it",
            "I couldn't put your own words on it",
            "no clip got one",
            "I couldn't tell which clips",
            "its clips are already in an earlier group",
            "its clips are already placed at another spot",
            "the clips did not end up there",
            "you didn't say what order its clips play in",
            "I couldn't find this moment in your clip, so I can't confirm it's the one playing",
            "you let me pick this moment, so I chose it myself",
            "you told me this moment isn't in your clips",
            "you chose grouping first, each group in the order you filmed it",
            "you chose strictly chronological order, so the beach is in 3 stretches; the city is in 2 stretches",
            "clips stay in the order you filmed them, so the pub is in 2 stretches; odd part",
            "a reason nobody wrote down",
        ],
        "tr": [
            "Bununla ilgili klip bulamadım",
            "Senin sözlerini üstüne koyamadım",
            "hiçbir klibe gelmedi",
            "Hangi kliplerden söz ettiğini anlayamadım",
            "klipleri zaten önceki bir grupta",
            "klipleri zaten başka bir yerde",
            "klipler oraya gelmedi",
            "kliplerinin hangi sırayla oynayacağını söylemedin",
            "Bu anı klipte bulamadım, o yüzden oynayanın o an olduğunu doğrulayamıyorum",
            "bu anı seçmeyi bana bıraktın, ben seçtim",
            "bu anın kliplerinde olmadığını söyledin",
            "önce gruplamayı seçtin, her grup çektiğin sırada",
            "kesin kronolojik sırayı seçtin, o yüzden the beach 3 parçaya bölünüyor; the city 2 parçaya bölünüyor",
            "klipler çektiğin sırada kalıyor, o yüzden the pub 2 parçaya bölünüyor; odd part",
            "a reason nobody wrote down",
        ],
    },
    "brief_checks._placement_reason and _timed_card_problem": {
        "en": [
            [
                None,
                'Pop-in on "goal" at 3.2 s',
                'Pop-ins on "goal" at 3.2 s and "trophy" at 10.0 s',
                'Pop-ins on "fans" at 1.0 s, "goal" at 3.2 s, "trophy" at 10.0 s, "coach" at 12.5 s, "city" at 14.0 s and "end" at 15.0 s',
                '"logo" shown above your title',
                '"logo" and "map" shown above your title',
                '"logo" and "map" shown above your title; Pop-ins on "fans" at 1.0 s, "goal" at 3.2 s and "trophy" at 10.0 s',
            ],
            [
                "12.0 s is past the end of the video (10.5 s), so the small card of the map isn't shown",
                "12.0 s is past the end of the video, so the small card of the photo isn't shown",
                "None is past the end of the video, so the small card of x isn't shown",
                "I couldn't place the small card of the map without covering a face",
                "I couldn't place the small card of the photo without covering a face",
                "I couldn't place the small card of x without covering a face",
                "The small card of the map would overlap another card, so it isn't shown",
                "The small card of the photo would overlap another card, so it isn't shown",
                "The small card of x would overlap another card, so it isn't shown",
                "Videos can't show as small cards in this edit yet, so the map is left out",
                "Videos can't show as small cards in this edit yet, so the photo is left out",
                "Videos can't show as small cards in this edit yet, so x is left out",
                "I couldn't find the map in your Visuals for the small card",
                "I couldn't find the photo in your Visuals for the small card",
                "I couldn't find x in your Visuals for the small card",
                "The clip the small card of the map should follow isn't in this edit",
                "The clip the small card of the photo should follow isn't in this edit",
                "The clip the small card of x should follow isn't in this edit",
                "The small card of the map couldn't be drawn on this render",
                "The small card of the photo couldn't be drawn on this render",
                "The small card of x couldn't be drawn on this render",
                "The small card of the map couldn't be drawn on this render",
                "The small card of the photo couldn't be drawn on this render",
                "The small card of x couldn't be drawn on this render",
            ],
        ],
        "tr": [
            [
                None,
                '"goal" 3.2 sn\'de çıkıyor',
                '"goal" 3.2 sn\'de, "trophy" 10.0 sn\'de çıkıyor',
                '"fans" 1.0 sn\'de, "goal" 3.2 sn\'de, "trophy" 10.0 sn\'de, "coach" 12.5 sn\'de, "city" 14.0 sn\'de, "end" 15.0 sn\'de çıkıyor',
                '"logo" başlığının üstünde çıkıyor',
                '"logo" ve "map" başlığının üstünde çıkıyor',
                '"logo" ve "map" başlığının üstünde çıkıyor; "fans" 1.0 sn\'de, "goal" 3.2 sn\'de, "trophy" 10.0 sn\'de çıkıyor',
            ],
            [
                "12.0 s videonun sonundan sonra (10.5 sn), o yüzden the map küçük kartı gösterilmiyor",
                "12.0 s videonun sonundan sonra, o yüzden fotoğraf küçük kartı gösterilmiyor",
                "None videonun sonundan sonra, o yüzden x küçük kartı gösterilmiyor",
                "the map küçük kartını bir yüzü kapatmadan yerleştiremedim",
                "fotoğraf küçük kartını bir yüzü kapatmadan yerleştiremedim",
                "x küçük kartını bir yüzü kapatmadan yerleştiremedim",
                "the map küçük kartı başka bir kartla çakışıyor, o yüzden gösterilmiyor",
                "fotoğraf küçük kartı başka bir kartla çakışıyor, o yüzden gösterilmiyor",
                "x küçük kartı başka bir kartla çakışıyor, o yüzden gösterilmiyor",
                "Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor, the map koymadım",
                "Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor, fotoğraf koymadım",
                "Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor, x koymadım",
                "Küçük kart için the map görsellerinde bulamadım",
                "Küçük kart için fotoğraf görsellerinde bulamadım",
                "Küçük kart için x görsellerinde bulamadım",
                "the map küçük kartının takip edeceği klip bu düzenlemede yok",
                "fotoğraf küçük kartının takip edeceği klip bu düzenlemede yok",
                "x küçük kartının takip edeceği klip bu düzenlemede yok",
                "the map küçük kartı bu videoya çizilemedi",
                "fotoğraf küçük kartı bu videoya çizilemedi",
                "x küçük kartı bu videoya çizilemedi",
                "the map küçük kartı bu videoya çizilemedi",
                "fotoğraf küçük kartı bu videoya çizilemedi",
                "x küçük kartı bu videoya çizilemedi",
            ],
        ],
    },
    "brief_checks._unshown_sentence": {
        "en": [
            None,
            "The recording for “the menu” never shows.",
            "The recordings for “the menu”, “the bar” and “the sea” never show.",
            "I never heard “the menu”, so that recording never shows.",
            "I never heard “a” or “b”, so those recordings never show. The recording for “c” never shows. 1 of your recordings never shows.",
            "2 of your recordings never show.",
        ],
        "tr": [
            None,
            "“the menu” kaydı hiç görünmüyor.",
            "“the menu”, “the bar” ve “the sea” kayıtları hiç görünmüyor.",
            "“the menu” sözünü hiç duymadım, o yüzden o kayıt hiç görünmüyor.",
            "“a” ya da “b” sözlerini hiç duymadım, o yüzden o kayıtlar hiç görünmüyor. “c” kaydı hiç görünmüyor. Kayıtlarından 1 tanesi hiç görünmüyor.",
            "Kayıtlarından 2 tanesi hiç görünmüyor.",
        ],
    },
    "brief_checks.speech_cleanup_review_note": {
        "en": [
            "Speech cleanup cut the long pauses. It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup found no long pauses to cut. It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            None,
            "Speech cleanup cut the long pauses. It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup found no long pauses to cut. It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            None,
            None,
            None,
            None,
            "Speech cleanup cut what it heard as 1 long pause (1.5 s). It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup cut what it heard as 1 long pause (1.5 s).",
            "Speech cleanup cut what it heard as 1 pause (1.5 s). It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup cut what it heard as 1 pause (1.5 s).",
            "Speech cleanup cut what it heard as 1 long pause and 1 filler sound (1.8 s). It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup cut what it heard as 1 long pause and 1 filler sound (1.8 s).",
            "Speech cleanup cut what it heard as 1 pause and 1 filler sound (1.8 s). It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup cut what it heard as 1 pause and 1 filler sound (1.8 s).",
            "Speech cleanup cut what it heard as 1 long pause, 1 filler sound and 1 restarted sentence (4.8 s).",
            "Speech cleanup cut what it heard as 1 long pause, 1 filler sound and 1 restarted sentence (4.8 s).",
            "Speech cleanup cut what it heard as 1 pause, 1 filler sound and 1 restarted sentence (4.8 s).",
            "Speech cleanup cut what it heard as 1 pause, 1 filler sound and 1 restarted sentence (4.8 s).",
            "Speech cleanup cut what it heard as 2 long pauses and 1 filler sound. It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup cut what it heard as 2 long pauses and 1 filler sound.",
            "Speech cleanup cut what it heard as 2 pauses and 1 filler sound. It looks only for pauses and filler sounds, so a sentence you restarted stays in.",
            "Speech cleanup cut what it heard as 2 pauses and 1 filler sound.",
            "Speech cleanup cut what it heard as 2 restarted sentences (4.5 s).",
            "Speech cleanup cut what it heard as 2 restarted sentences (4.5 s).",
            "Speech cleanup cut what it heard as 2 restarted sentences (4.5 s).",
            "Speech cleanup cut what it heard as 2 restarted sentences (4.5 s).",
        ],
        "tr": [
            "Konuşma temizliği uzun duraklamaları kesti. Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği kesilecek uzun bir duraklama bulmadı. Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            None,
            "Konuşma temizliği uzun duraklamaları kesti. Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği kesilecek uzun bir duraklama bulmadı. Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            None,
            None,
            None,
            None,
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 uzun duraklama (1,5 sn). Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 uzun duraklama (1,5 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 duraklama (1,5 sn). Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 duraklama (1,5 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 uzun duraklama, 1 dolgu sesi (1,8 sn). Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 uzun duraklama, 1 dolgu sesi (1,8 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 duraklama, 1 dolgu sesi (1,8 sn). Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 duraklama, 1 dolgu sesi (1,8 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 uzun duraklama, 1 dolgu sesi, 1 baştan başlanan cümle (4,8 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 uzun duraklama, 1 dolgu sesi, 1 baştan başlanan cümle (4,8 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 duraklama, 1 dolgu sesi, 1 baştan başlanan cümle (4,8 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 1 duraklama, 1 dolgu sesi, 1 baştan başlanan cümle (4,8 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 uzun duraklama, 1 dolgu sesi. Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 uzun duraklama, 1 dolgu sesi.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 duraklama, 1 dolgu sesi. Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 duraklama, 1 dolgu sesi.",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 baştan başlanan cümle (4,5 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 baştan başlanan cümle (4,5 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 baştan başlanan cümle (4,5 sn).",
            "Konuşma temizliği duyduğu şu sesleri kesti: 2 baştan başlanan cümle (4,5 sn).",
        ],
    },
    "choice questions sweep": {
        "en": {
            "coverage_cap_below_coverage": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "There are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately there are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                    "Choose fewer clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately there are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "There are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately there are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                    "Choose fewer clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately there are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                ],
            ],
            "coverage_insufficient_capacity": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "The selected voiceover runs 40 seconds, but the footage can provide only 2 seconds without reuse. Add longer footage.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately the selected voiceover runs 40 seconds, but the footage can provide only 2 seconds without reuse. Add longer footage. The most I can do is: Add longer footage. Reply "Add longer footage" to go with that, or change your request.',
                    "Add longer footage",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately the selected voiceover runs 40 seconds, but the footage can provide only 2 seconds without reuse. Add longer footage. The most I can do is: Add longer footage. Reply "Add longer footage" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "The selected voiceover runs 40 seconds, but the footage can provide only 2 seconds without reuse. Add longer footage.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately the selected voiceover runs 40 seconds, but the footage can provide only 2 seconds without reuse. Add longer footage. The most I can do is: Add longer footage. Reply "Add longer footage" to go with that, or change your request.',
                    "Add longer footage",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately the selected voiceover runs 40 seconds, but the footage can provide only 2 seconds without reuse. Add longer footage. The most I can do is: Add longer footage. Reply "Add longer footage" to go with that, or change your request.',
                ],
            ],
            "coverage_insufficient_media": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "This edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately this edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately this edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "This edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately this edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately this edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
            ],
            "coverage_leave_out": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "Some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back.",
                    "The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back. Which do you prefer?\n1. Update the selected clips\n2. Leave those clips out (recommended)\nTap an option, or tell me in your own words.",
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    "Leave those clips out",
                    "I will make this edit from the clips that are still attached.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can\'t read their footage. Select your clips again, or add them back. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
                [
                    ["update footage", "add footage", "choose fewer clips"],
                    [
                        "leave them out",
                        "without them",
                        "skip those clips",
                        "continue without them",
                        "onları dışarıda bırak",
                        "onlar olmadan devam et",
                        "o klipleri atla",
                        "onlarsız devam et",
                    ],
                ],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "Some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back.",
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back. Which do you prefer?\n1. Update the selected clips\n2. Leave those clips out (recommended)\nTap an option, or tell me in your own words.",
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    "Leave those clips out",
                    "I will make this edit from the clips that are still attached.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can\'t read their footage. Select your clips again, or add them back. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
            ],
            "coverage_no_usable_sources": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "Some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can\'t read their footage. Select your clips again, or add them back. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can\'t read their footage. Select your clips again, or add them back. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "Some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can\'t read their footage. Select your clips again, or add them back. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately some of the selected clips are no longer attached to this edit, so I can\'t read their footage. Select your clips again, or add them back. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
            ],
            "coverage_no_video_sources": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "You picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately you picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately you picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "You picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately you picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately you picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
            ],
            "coverage_single_hero_order_conflict": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "The selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately the selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately the selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "The selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately the selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately the selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
            ],
            "coverage_source_count_limit": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "This edit can include at most 1 clips. Choose fewer clips so each one can be used.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately this edit can include at most 1 clips. Choose fewer clips so each one can be used. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                    "Choose fewer clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately this edit can include at most 1 clips. Choose fewer clips so each one can be used. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "This edit can include at most 1 clips. Choose fewer clips so each one can be used.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately this edit can include at most 1 clips. Choose fewer clips so each one can be used. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                    "Choose fewer clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately this edit can include at most 1 clips. Choose fewer clips so each one can be used. The most I can do is: Choose fewer clips. Reply "Choose fewer clips" to go with that, or change your request.',
                ],
            ],
            "coverage_target_below_coverage": [
                [
                    "The selected footage cannot meet the included-clip constraints.",
                    "The requested 4 seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately the requested 4 seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'The selected footage cannot meet the included-clip constraints. Unfortunately the requested 4 seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
                [["update footage", "add footage", "choose fewer clips"]],
                [
                    "Your length choice is still waiting. The selected footage cannot meet the included-clip constraints.",
                    "The requested 4 seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately the requested 4 seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                    "Update the selected clips",
                    "I will wait for your updated footage before making this edit.",
                    'Your length choice is still waiting. The selected footage cannot meet the included-clip constraints. Unfortunately the requested 4 seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering. The most I can do is: Update the selected clips. Reply "Update the selected clips" to go with that, or change your request.',
                ],
            ],
            "duration_vs_count": [
                [
                    'Your brief says "Keep 15 seconds" and this edit uses 166 clips.',
                    "166 clips can't each stay on screen long enough to be seen in 15 seconds (each needs about 0.1 seconds).",
                    'Your brief says "Keep 15 seconds" and this edit uses 166 clips. Unfortunately 166 clips can\'t each stay on screen long enough to be seen in 15 seconds (each needs about 0.1 seconds). Which do you prefer?\n1. Extend it to 16.6 seconds (recommended)\n2. Keep 15 seconds with 150 clips\nTap an option, or tell me in your own words.',
                    "Extend it to 16.6 seconds",
                    "Every clip gets at least 0.1 seconds, so all 166 stay in.",
                    "Keep 15 seconds with 150 clips",
                    "Uses 150 of your 166 clips, spread evenly through your footage; the others are left out.",
                    'Your brief says "Keep 15 seconds" and this edit uses 166 clips. Unfortunately 166 clips can\'t each stay on screen long enough to be seen in 15 seconds (each needs about 0.1 seconds). The most I can do is: Extend it to 16.6 seconds. Reply "Extend it to 16.6 seconds" to go with that, or change your request.',
                ],
                [
                    [
                        "extend",
                        "extend it",
                        "longer",
                        "make it longer",
                        "extend the length",
                        "uzat",
                        "uzatalım",
                        "süreyi uzat",
                        "daha uzun yap",
                        "daha uzun",
                        "uzun olsun",
                        "videoyu uzat",
                        "süreyi artır",
                    ],
                    [
                        "fewer",
                        "fewer clips",
                        "use fewer",
                        "use fewer clips",
                        "keep it short",
                        "daha az klip",
                        "az klip",
                        "daha az",
                        "kısa kalsın",
                        "kısa tut",
                        "süreyi koru",
                        "süreyi değiştirme",
                        "klip sayısını azalt",
                        "klipleri azalt",
                    ],
                ],
                [
                    'Your length choice is still waiting. Your brief says "Keep 15 seconds" and this edit uses 166 clips.',
                    "166 clips can't each stay on screen long enough to be seen in 15 seconds (each needs about 0.1 seconds).",
                    'Your length choice is still waiting. Your brief says "Keep 15 seconds" and this edit uses 166 clips. Unfortunately 166 clips can\'t each stay on screen long enough to be seen in 15 seconds (each needs about 0.1 seconds). Which do you prefer?\n1. Extend it to 16.6 seconds (recommended)\n2. Keep 15 seconds with 150 clips\nTap an option, or tell me in your own words.',
                    "Extend it to 16.6 seconds",
                    "Every clip gets at least 0.1 seconds, so all 166 stay in.",
                    "Keep 15 seconds with 150 clips",
                    "Uses 150 of your 166 clips, spread evenly through your footage; the others are left out.",
                    'Your length choice is still waiting. Your brief says "Keep 15 seconds" and this edit uses 166 clips. Unfortunately 166 clips can\'t each stay on screen long enough to be seen in 15 seconds (each needs about 0.1 seconds). The most I can do is: Extend it to 16.6 seconds. Reply "Extend it to 16.6 seconds" to go with that, or change your request.',
                ],
            ],
            "freeze_one_moment": [
                [
                    "You asked me to freeze the frame on that moment. I found it in your clip (5.0–8.0s), but I can't tell the exact instant, and I won't guess it.",
                    "Which frame should hold?",
                    "You asked me to freeze the frame on that moment. I found it in your clip (5.0–8.0s), but I can't tell the exact instant, and I won't guess it. Which frame should hold?\n1. Early in it (5.7s)\n2. Middle (6.5s) (recommended)\n3. Late in it (7.3s)\n4. No freeze\nTap an option, or tell me in your own words.",
                    "Early in it (5.7s)",
                    "",
                    "Middle (6.5s)",
                    "",
                    "Late in it (7.3s)",
                    "",
                    "No freeze",
                    "That moment plays normally, without a freeze.",
                    "You asked me to freeze the frame on that moment. I found it in your clip (5.0–8.0s), but I can't tell the exact instant, and I won't guess it. Unfortunately which frame should hold? The most I can do is: Early in it (5.7s). Reply \"Early in it (5.7s)\" to go with that, or change your request.",
                ],
                [
                    ["early", "earlier", "erken", "başına yakın"],
                    ["middle", "the middle", "mid", "orta", "ortası"],
                    ["late", "later", "sonuna yakın"],
                    [
                        "no freeze",
                        "no freeze please",
                        "skip the freeze",
                        "skip freeze",
                        "without a freeze",
                        "without the freeze",
                        "dont freeze",
                        "don t freeze",
                        "do not freeze",
                        "donma olmasın",
                        "dondurma yapma",
                        "dondurmadan",
                        "donmasın",
                    ],
                ],
            ],
            "freeze_tied_moments": [
                [
                    "You asked me to freeze the frame on a moment, and more than one moment in your footage fits what you described. I won't guess which one.",
                    "Which moment should freeze? I'll hold the middle of it.",
                    "You asked me to freeze the frame on a moment, and more than one moment in your footage fits what you described. I won't guess which one. Unfortunately which moment should freeze? I'll hold the middle of it. Which do you prefer?\n1. “his bite” (5.0–8.0s, clip 1)\n2. “another bite” (5.0–8.0s, clip 2)\n3. No freeze\nTap an option, or tell me in your own words.",
                    "“his bite” (5.0–8.0s, clip 1)",
                    "",
                    "“another bite” (5.0–8.0s, clip 2)",
                    "",
                    "No freeze",
                    "That moment plays normally, without a freeze.",
                    "You asked me to freeze the frame on a moment, and more than one moment in your footage fits what you described. I won't guess which one. Unfortunately which moment should freeze? I'll hold the middle of it. The most I can do is: “his bite” (5.0–8.0s, clip 1). Reply \"“his bite” (5.0–8.0s, clip 1)\" to go with that, or change your request.",
                ],
                [
                    [],
                    [],
                    [
                        "no freeze",
                        "no freeze please",
                        "skip the freeze",
                        "skip freeze",
                        "without a freeze",
                        "without the freeze",
                        "dont freeze",
                        "don t freeze",
                        "do not freeze",
                        "donma olmasın",
                        "dondurma yapma",
                        "dondurmadan",
                        "donmasın",
                    ],
                ],
            ],
            "holds_extend": [
                [
                    'Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds.',
                    "Those photos and your footage need about 12 seconds, more than 5.",
                    'Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 12 seconds, more than 5. Which do you prefer?\n1. Make it 12 seconds (recommended)\n2. Keep 5 seconds, photos at the usual pace\nTap an option, or tell me in your own words.',
                    "Make it 12 seconds",
                    "Every photo plays the length you gave it, with the footage around it.",
                    "Keep 5 seconds, photos at the usual pace",
                    "The photos you timed play at the edit's own pace so everything fits.",
                    'Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 12 seconds, more than 5. The most I can do is: Make it 12 seconds. Reply "Make it 12 seconds" to go with that, or change your request.',
                ],
                [
                    [
                        "extend",
                        "make it longer",
                        "longer",
                        "12 seconds",
                        "uzat",
                        "uzatalım",
                        "süreyi uzat",
                        "daha uzun yap",
                        "daha uzun",
                        "uzun olsun",
                        "videoyu uzat",
                        "süreyi artır",
                    ],
                    [
                        "keep the length",
                        "keep it",
                        "keep 5 seconds",
                        "süreyi koru",
                        "süre kalsın",
                        "süreyi değiştirme",
                    ],
                ],
                [
                    'Your length choice is still waiting. Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds.',
                    "Those photos and your footage need about 12 seconds, more than 5.",
                    'Your length choice is still waiting. Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 12 seconds, more than 5. Which do you prefer?\n1. Make it 12 seconds (recommended)\n2. Keep 5 seconds, photos at the usual pace\nTap an option, or tell me in your own words.',
                    "Make it 12 seconds",
                    "Every photo plays the length you gave it, with the footage around it.",
                    "Keep 5 seconds, photos at the usual pace",
                    "The photos you timed play at the edit's own pace so everything fits.",
                    'Your length choice is still waiting. Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 12 seconds, more than 5. The most I can do is: Make it 12 seconds. Reply "Make it 12 seconds" to go with that, or change your request.',
                ],
            ],
            "holds_keep_only": [
                [
                    'Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds.',
                    "Those photos and your footage need about 400 seconds, more than 5.",
                    'Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 400 seconds, more than 5. The most I can do is: Keep 5 seconds, photos at the usual pace. Reply "Keep 5 seconds, photos at the usual pace" to go with that, or change your request.',
                    "Keep 5 seconds, photos at the usual pace",
                    "The photos you timed play at the edit's own pace so everything fits.",
                    'Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 400 seconds, more than 5. The most I can do is: Keep 5 seconds, photos at the usual pace. Reply "Keep 5 seconds, photos at the usual pace" to go with that, or change your request.',
                ],
                [
                    [
                        "keep the length",
                        "keep it",
                        "keep 5 seconds",
                        "süreyi koru",
                        "süre kalsın",
                        "süreyi değiştirme",
                    ]
                ],
                [
                    'Your length choice is still waiting. Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds.',
                    "Those photos and your footage need about 400 seconds, more than 5.",
                    'Your length choice is still waiting. Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 400 seconds, more than 5. The most I can do is: Keep 5 seconds, photos at the usual pace. Reply "Keep 5 seconds, photos at the usual pace" to go with that, or change your request.',
                    "Keep 5 seconds, photos at the usual pace",
                    "The photos you timed play at the edit's own pace so everything fits.",
                    'Your length choice is still waiting. Your brief says "Keep 5 seconds" and the photos you timed take 6 seconds. Unfortunately those photos and your footage need about 400 seconds, more than 5. The most I can do is: Keep 5 seconds, photos at the usual pace. Reply "Keep 5 seconds, photos at the usual pace" to go with that, or change your request.',
                ],
            ],
            "length_song": [
                [
                    'Your brief says "Keep 20 seconds", but the available song is 14 seconds.',
                    "That song cannot make a 20-second edit without changing the source timing.",
                    'Your brief says "Keep 20 seconds", but the available song is 14 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. The most I can do is: Use the available 14 seconds. Reply "Use the available 14 seconds" to go with that, or change your request.',
                    "Use the available 14 seconds",
                    "The video follows the song without reuse or a silent extension.",
                    'Your brief says "Keep 20 seconds", but the available song is 14 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. The most I can do is: Use the available 14 seconds. Reply "Use the available 14 seconds" to go with that, or change your request.',
                ],
                [["use available length", "use the available length", "match the source"]],
                [
                    'Your length choice is still waiting. Your brief says "Keep 20 seconds", but the available song is 14 seconds.',
                    "That song cannot make a 20-second edit without changing the source timing.",
                    'Your length choice is still waiting. Your brief says "Keep 20 seconds", but the available song is 14 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. The most I can do is: Use the available 14 seconds. Reply "Use the available 14 seconds" to go with that, or change your request.',
                    "Use the available 14 seconds",
                    "The video follows the song without reuse or a silent extension.",
                    'Your length choice is still waiting. Your brief says "Keep 20 seconds", but the available song is 14 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. The most I can do is: Use the available 14 seconds. Reply "Use the available 14 seconds" to go with that, or change your request.',
                ],
            ],
            "length_song_window": [
                [
                    'Your brief says "Keep 20 seconds", but the available song is 30 seconds.',
                    "That song cannot make a 20-second edit without changing the source timing.",
                    'Your brief says "Keep 20 seconds", but the available song is 30 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. Which do you prefer?\n1. Trim it to 20 seconds (recommended)\n2. Use the available 30 seconds\nTap an option, or tell me in your own words.',
                    "Trim it to 20 seconds",
                    "Use the supported trim for this song.",
                    "Use the available 30 seconds",
                    "The video follows the song without reuse or a silent extension.",
                    'Your brief says "Keep 20 seconds", but the available song is 30 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. The most I can do is: Trim it to 20 seconds. Reply "Trim it to 20 seconds" to go with that, or change your request.',
                ],
                [
                    ["trim it", "trim", "keep requested length"],
                    ["use available length", "use the available length", "match the source"],
                ],
                [
                    'Your length choice is still waiting. Your brief says "Keep 20 seconds", but the available song is 30 seconds.',
                    "That song cannot make a 20-second edit without changing the source timing.",
                    'Your length choice is still waiting. Your brief says "Keep 20 seconds", but the available song is 30 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. Which do you prefer?\n1. Trim it to 20 seconds (recommended)\n2. Use the available 30 seconds\nTap an option, or tell me in your own words.',
                    "Trim it to 20 seconds",
                    "Use the supported trim for this song.",
                    "Use the available 30 seconds",
                    "The video follows the song without reuse or a silent extension.",
                    'Your length choice is still waiting. Your brief says "Keep 20 seconds", but the available song is 30 seconds. Unfortunately that song cannot make a 20-second edit without changing the source timing. The most I can do is: Trim it to 20 seconds. Reply "Trim it to 20 seconds" to go with that, or change your request.',
                ],
            ],
            "length_take": [
                [
                    'Your brief says "Keep 30 seconds", but the available spoken take is 12 seconds.',
                    "That spoken take cannot make a 30-second edit without changing the source timing.",
                    'Your brief says "Keep 30 seconds", but the available spoken take is 12 seconds. Unfortunately that spoken take cannot make a 30-second edit without changing the source timing. The most I can do is: Use the available 12 seconds. Reply "Use the available 12 seconds" to go with that, or change your request.',
                    "Use the available 12 seconds",
                    "The video follows the spoken take without reuse or a silent extension.",
                    'Your brief says "Keep 30 seconds", but the available spoken take is 12 seconds. Unfortunately that spoken take cannot make a 30-second edit without changing the source timing. The most I can do is: Use the available 12 seconds. Reply "Use the available 12 seconds" to go with that, or change your request.',
                ],
                [["use available length", "use the available length", "match the source"]],
                [
                    'Your length choice is still waiting. Your brief says "Keep 30 seconds", but the available spoken take is 12 seconds.',
                    "That spoken take cannot make a 30-second edit without changing the source timing.",
                    'Your length choice is still waiting. Your brief says "Keep 30 seconds", but the available spoken take is 12 seconds. Unfortunately that spoken take cannot make a 30-second edit without changing the source timing. The most I can do is: Use the available 12 seconds. Reply "Use the available 12 seconds" to go with that, or change your request.',
                    "Use the available 12 seconds",
                    "The video follows the spoken take without reuse or a silent extension.",
                    'Your length choice is still waiting. Your brief says "Keep 30 seconds", but the available spoken take is 12 seconds. Unfortunately that spoken take cannot make a 30-second edit without changing the source timing. The most I can do is: Use the available 12 seconds. Reply "Use the available 12 seconds" to go with that, or change your request.',
                ],
            ],
            "length_take_window": [
                [
                    "Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”.",
                    "That part can't make a 7-second edit without changing those lines.",
                    'Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 7-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                    "Keep those 5 seconds",
                    "The video plays just the part your lines keep, without reuse or a silent extension.",
                    'Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 7-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                ],
                [["use available length", "use the available length", "match the source"]],
                [
                    "Your length choice is still waiting. Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”.",
                    "That part can't make a 7-second edit without changing those lines.",
                    'Your length choice is still waiting. Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 7-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                    "Keep those 5 seconds",
                    "The video plays just the part your lines keep, without reuse or a silent extension.",
                    'Your length choice is still waiting. Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 7-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                ],
            ],
            "length_take_window_longer": [
                [
                    "Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”.",
                    "That part can't make a 3-second edit without changing those lines.",
                    'Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 3-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                    "Keep those 5 seconds",
                    "The video plays just the part your lines keep, without reuse or a silent extension.",
                    'Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 3-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                ],
                [["use available length", "use the available length", "match the source"]],
                [
                    "Your length choice is still waiting. Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”.",
                    "That part can't make a 3-second edit without changing those lines.",
                    'Your length choice is still waiting. Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 3-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                    "Keep those 5 seconds",
                    "The video plays just the part your lines keep, without reuse or a silent extension.",
                    'Your length choice is still waiting. Your start and end lines keep about 5 seconds of your take: from “what do you think?” to right after “Nice!”. Unfortunately that part can\'t make a 3-second edit without changing those lines. The most I can do is: Keep those 5 seconds. Reply "Keep those 5 seconds" to go with that, or change your request.',
                ],
            ],
            "length_take_window_start_only": [
                [
                    "Your start line keeps about 11.7 seconds of your take: from “what do you think?” to the end.",
                    "That part can't make a 7-second edit without changing that line.",
                    'Your start line keeps about 11.7 seconds of your take: from “what do you think?” to the end. Unfortunately that part can\'t make a 7-second edit without changing that line. The most I can do is: Keep those 11.7 seconds. Reply "Keep those 11.7 seconds" to go with that, or change your request.',
                    "Keep those 11.7 seconds",
                    "The video plays just the part your lines keep, without reuse or a silent extension.",
                    'Your start line keeps about 11.7 seconds of your take: from “what do you think?” to the end. Unfortunately that part can\'t make a 7-second edit without changing that line. The most I can do is: Keep those 11.7 seconds. Reply "Keep those 11.7 seconds" to go with that, or change your request.',
                ],
                [["use available length", "use the available length", "match the source"]],
                [
                    "Your length choice is still waiting. Your start line keeps about 11.7 seconds of your take: from “what do you think?” to the end.",
                    "That part can't make a 7-second edit without changing that line.",
                    'Your length choice is still waiting. Your start line keeps about 11.7 seconds of your take: from “what do you think?” to the end. Unfortunately that part can\'t make a 7-second edit without changing that line. The most I can do is: Keep those 11.7 seconds. Reply "Keep those 11.7 seconds" to go with that, or change your request.',
                    "Keep those 11.7 seconds",
                    "The video plays just the part your lines keep, without reuse or a silent extension.",
                    'Your length choice is still waiting. Your start line keeps about 11.7 seconds of your take: from “what do you think?” to the end. Unfortunately that part can\'t make a 7-second edit without changing that line. The most I can do is: Keep those 11.7 seconds. Reply "Keep those 11.7 seconds" to go with that, or change your request.',
                ],
            ],
            "order_basis": [
                [
                    "You asked for the clips in the order you filmed them.",
                    "none of your clips have a filming time, so I can't put them in that order.",
                    "You asked for the clips in the order you filmed them. Unfortunately none of your clips have a filming time, so I can't put them in that order. Which do you prefer?\n1. Use the order you added the clips (recommended)\n2. Continue without a fixed order\nTap an option, or tell me in your own words.",
                    "Use the order you added the clips",
                    "The clips play in the order they were added to this project.",
                    "Continue without a fixed order",
                    "I won't promise or check a particular order; the clips still play in a sensible sequence.",
                    'You asked for the clips in the order you filmed them. Unfortunately none of your clips have a filming time, so I can\'t put them in that order. The most I can do is: Use the order you added the clips. Reply "Use the order you added the clips" to go with that, or change your request.',
                ],
                [
                    [
                        "upload order",
                        "order i added them",
                        "the order i added them",
                        "order i uploaded them",
                        "attachment order",
                        "as uploaded",
                        "as added",
                        "eklediğim sıra",
                        "eklediğim sırayla",
                        "yüklediğim sıra",
                        "yüklediğim sırayla",
                        "ekleme sırası",
                        "yükleme sırası",
                        "eklenme sırası",
                        "ekleme sırasına göre",
                        "eklediğim gibi",
                        "yüklediğim gibi",
                    ],
                    [
                        "without order",
                        "no order",
                        "skip the order",
                        "continue without order",
                        "no fixed order",
                        "sırasız",
                        "sırasız devam et",
                        "sıra olmasın",
                        "sıra yok",
                        "sıra olmadan",
                        "sıra olmadan devam et",
                        "sabit sıra olmasın",
                        "sabit sıra yok",
                        "sırayı atla",
                        "sıra önemli değil",
                        "fark etmez",
                    ],
                ],
            ],
            "speech_occurrence_cut": [
                [
                    'You asked me to cut "thank you Karo". It is said 2 times in the part of your take I\'m keeping.',
                    "Which one should I cut?",
                    "You asked me to cut \"thank you Karo\". It is said 2 times in the part of your take I'm keeping. Which one should I cut?\n1. 0:03 “…are at Merope, thank you Karo. Merope is great, thank…”\n2. 0:10 “…Merope is great, thank you Karo.”\n3. Every time\n4. Don't cut it\nTap an option, or tell me in your own words.",
                    "0:03 “…are at Merope, thank you Karo. Merope is great, thank…”",
                    "",
                    "0:10 “…Merope is great, thank you Karo.”",
                    "",
                    "Every time",
                    "",
                    "Don't cut it",
                    "",
                    'You asked me to cut "thank you Karo". It is said 2 times in the part of your take I\'m keeping. Unfortunately which one should I cut? The most I can do is: 0:03 “…are at Merope, thank you Karo. Merope is great, thank…”. Reply "0:03 “…are at Merope, thank you Karo. Merope is great, thank…”" to go with that, or change your request.',
                ],
                [
                    [
                        "1 seçenek",
                        "seçenek 1",
                        "1 numara",
                        "numara 1",
                        "1 numaralı",
                        "1 numaralı seçenek",
                        "1 nolu",
                        "1 şık",
                        "şık 1",
                        "birinci",
                        "birinci seçenek",
                        "birinci şık",
                        "birincisi",
                        "birincisi seçenek",
                        "birincisi şık",
                        "birincisini",
                        "birincisini seçenek",
                        "birincisini şık",
                        "ilk",
                        "ilk seçenek",
                        "ilk şık",
                        "ilki",
                        "ilki seçenek",
                        "ilki şık",
                        "ilkini",
                        "ilkini seçenek",
                        "ilkini şık",
                        "bir numara",
                        "numara bir",
                        "seçenek bir",
                        "bir numaralı",
                    ],
                    [
                        "2 seçenek",
                        "seçenek 2",
                        "2 numara",
                        "numara 2",
                        "2 numaralı",
                        "2 numaralı seçenek",
                        "2 nolu",
                        "2 şık",
                        "şık 2",
                        "ikinci",
                        "ikinci seçenek",
                        "ikinci şık",
                        "ikincisi",
                        "ikincisi seçenek",
                        "ikincisi şık",
                        "ikincisini",
                        "ikincisini seçenek",
                        "ikincisini şık",
                        "iki numara",
                        "numara iki",
                        "seçenek iki",
                        "iki numaralı",
                    ],
                    ["all of them", "every time", "both", "hepsi", "hepsini", "ikisi de"],
                    [],
                ],
            ],
            "speech_occurrence_text": [
                [
                    'You asked for "MEROPE" when she says "Merope". "Merope" is said 2 times in the part of your take I\'m keeping, and I can\'t tell who is speaking.',
                    "Which one should it appear on?",
                    'You asked for "MEROPE" when she says "Merope". "Merope" is said 2 times in the part of your take I\'m keeping, and I can\'t tell who is speaking. Which one should it appear on?\n1. 0:02 “We are at Merope, thank you Karo. Merope…”\n2. 0:08 “…thank you Karo. Merope is great, thank you…”\n3. Leave the text off\nTap an option, or tell me in your own words.',
                    "0:02 “We are at Merope, thank you Karo. Merope…”",
                    "",
                    "0:08 “…thank you Karo. Merope is great, thank you…”",
                    "",
                    "Leave the text off",
                    "",
                    'You asked for "MEROPE" when she says "Merope". "Merope" is said 2 times in the part of your take I\'m keeping, and I can\'t tell who is speaking. Unfortunately which one should it appear on? The most I can do is: 0:02 “We are at Merope, thank you Karo. Merope…”. Reply "0:02 “We are at Merope, thank you Karo. Merope…”" to go with that, or change your request.',
                ],
                [
                    [
                        "1 seçenek",
                        "seçenek 1",
                        "1 numara",
                        "numara 1",
                        "1 numaralı",
                        "1 numaralı seçenek",
                        "1 nolu",
                        "1 şık",
                        "şık 1",
                        "birinci",
                        "birinci seçenek",
                        "birinci şık",
                        "birincisi",
                        "birincisi seçenek",
                        "birincisi şık",
                        "birincisini",
                        "birincisini seçenek",
                        "birincisini şık",
                        "ilk",
                        "ilk seçenek",
                        "ilk şık",
                        "ilki",
                        "ilki seçenek",
                        "ilki şık",
                        "ilkini",
                        "ilkini seçenek",
                        "ilkini şık",
                        "bir numara",
                        "numara bir",
                        "seçenek bir",
                        "bir numaralı",
                    ],
                    [
                        "2 seçenek",
                        "seçenek 2",
                        "2 numara",
                        "numara 2",
                        "2 numaralı",
                        "2 numaralı seçenek",
                        "2 nolu",
                        "2 şık",
                        "şık 2",
                        "ikinci",
                        "ikinci seçenek",
                        "ikinci şık",
                        "ikincisi",
                        "ikincisi seçenek",
                        "ikincisi şık",
                        "ikincisini",
                        "ikincisini seçenek",
                        "ikincisini şık",
                        "iki numara",
                        "numara iki",
                        "seçenek iki",
                        "iki numaralı",
                    ],
                    [],
                ],
            ],
            "take_line_end": [
                [
                    "I couldn't find “Thank you” in what's said in your video.",
                    "Which line should it end on?",
                    "I couldn't find “Thank you” in what's said in your video. Which line should it end on?\n1. “Lucas”\n2. “Acá tenemos salsas tradicionales argentinas,”\n3. “salsas criollas,”\n4. “chivichurros y sardines”\n5. “Gracias”\n6. None of these — keep it to the end\nTap an option, or tell me in your own words.",
                    "“Lucas”",
                    "",
                    "“Acá tenemos salsas tradicionales argentinas,”",
                    "",
                    "“salsas criollas,”",
                    "",
                    "“chivichurros y sardines”",
                    "",
                    "“Gracias”",
                    "",
                    "None of these — keep it to the end",
                    "The video still runs to the end of your take.",
                    "I couldn't find “Thank you” in what's said in your video. Unfortunately which line should it end on? The most I can do is: “Lucas”. Reply \"“Lucas”\" to go with that, or change your request.",
                ],
                [
                    ["Lucas"],
                    ["Acá tenemos salsas tradicionales argentinas,"],
                    ["salsas criollas,"],
                    ["chivichurros y sardines"],
                    ["Gracias"],
                    [
                        "none",
                        "none of these",
                        "none of them",
                        "neither",
                        "skip",
                        "skip it",
                        "no",
                        "keep it to the end",
                        "to the end",
                        "keep the end",
                        "hiçbiri",
                        "hiçbirini",
                        "hiçbiri değil",
                        "hayır",
                        "yok",
                        "atla",
                        "sona kadar",
                        "sonuna kadar",
                        "sona kadar sürsün",
                    ],
                ],
            ],
            "take_line_start": [
                [
                    "I couldn't find “Here we have traditional Argentinian sauces” in what's said in your video.",
                    "Which line should it open on?",
                    "I couldn't find “Here we have traditional Argentinian sauces” in what's said in your video. Which line should it open on?\n1. “¿Qué te llaman?”\n2. “Lucas”\n3. “Acá tenemos salsas tradicionales argentinas,” (recommended)\n4. “salsas criollas,”\n5. “chivichurros y sardines”\n6. None of these — keep it from the start\nTap an option, or tell me in your own words.",
                    "“¿Qué te llaman?”",
                    "",
                    "“Lucas”",
                    "",
                    "“Acá tenemos salsas tradicionales argentinas,”",
                    "",
                    "“salsas criollas,”",
                    "",
                    "“chivichurros y sardines”",
                    "",
                    "None of these — keep it from the start",
                    "The video still opens at the start of your take.",
                    "I couldn't find “Here we have traditional Argentinian sauces” in what's said in your video. Unfortunately which line should it open on? The most I can do is: “¿Qué te llaman?”. Reply \"“¿Qué te llaman?”\" to go with that, or change your request.",
                ],
                [
                    ["¿Qué te llaman?"],
                    ["Lucas"],
                    ["Acá tenemos salsas tradicionales argentinas,"],
                    ["salsas criollas,"],
                    ["chivichurros y sardines"],
                    [
                        "none",
                        "none of these",
                        "none of them",
                        "neither",
                        "skip",
                        "skip it",
                        "no",
                        "keep it from the start",
                        "from the start",
                        "keep the start",
                        "hiçbiri",
                        "hiçbirini",
                        "hiçbiri değil",
                        "hayır",
                        "yok",
                        "atla",
                        "baştan",
                        "baştan başlasın",
                        "başından",
                    ],
                ],
            ],
            "unclear_word": [
                [
                    "I couldn't make out one word around 0:09 in your clip: the two listens heard different words, and I won't guess.",
                    "What should the captions say?",
                    "I couldn't make out one word around 0:09 in your clip: the two listens heard different words, and I won't guess. What should the captions say?\n1. Leave it out\n2. salma y limón\n3. sal marina\nTap an option, or tell me in your own words.",
                    "Leave it out",
                    "The captions skip that word.",
                    "salma y limón",
                    "Write this in the captions.",
                    "sal marina",
                    "Write this in the captions.",
                    "I couldn't make out one word around 0:09 in your clip: the two listens heard different words, and I won't guess. Unfortunately what should the captions say? The most I can do is: Leave it out. Reply \"Leave it out\" to go with that, or change your request.",
                ],
                [
                    [
                        "leave it out",
                        "leave that out",
                        "leave the word out",
                        "leave that word out",
                        "skip it",
                        "skip that word",
                        "i don't know it either, leave that word out of the captions",
                        "çıkar",
                        "kelimeyi çıkar",
                        "o kelimeyi çıkar",
                        "atla",
                        "boş bırak",
                    ],
                    [],
                    [],
                ],
            ],
            "voice_implicit_length": [
                [
                    "Your voice runs 75 seconds.",
                    "How long should the video be?",
                    "Your voice runs 75 seconds. How long should the video be?\n1. 5 seconds (recommended)\n2. 30 seconds\n3. 45 seconds\nTap an option, or tell me in your own words.",
                    "5 seconds",
                    "The shortest length where every clip stays on screen long enough to be seen.",
                    "30 seconds",
                    "Longer: each clip stays on screen a little longer.",
                    "45 seconds",
                    "Longer: each clip stays on screen a little longer.",
                    'Your voice runs 75 seconds. How long should the video be? I can make it 5 seconds. Reply "5 seconds" to go with that, or change your request.',
                ],
                [["5", "5 seconds"], ["30", "30 seconds"], ["45", "45 seconds"]],
                [
                    "Your length choice is still waiting. Your voice runs 75 seconds.",
                    "How long should the video be?",
                    "Your length choice is still waiting. Your voice runs 75 seconds. How long should the video be?\n1. 5 seconds (recommended)\n2. 30 seconds\n3. 45 seconds\nTap an option, or tell me in your own words.",
                    "5 seconds",
                    "The shortest length where every clip stays on screen long enough to be seen.",
                    "30 seconds",
                    "Longer: each clip stays on screen a little longer.",
                    "45 seconds",
                    "Longer: each clip stays on screen a little longer.",
                    'Your length choice is still waiting. Your voice runs 75 seconds. How long should the video be? I can make it 5 seconds. Reply "5 seconds" to go with that, or change your request.',
                ],
            ],
            "voice_short": [
                [
                    'Your brief says "Keep 30 seconds" and your voice runs 14 seconds.',
                    "your voice can't fill the whole video.",
                    'Your brief says "Keep 30 seconds" and your voice runs 14 seconds. Unfortunately your voice can\'t fill the whole video. Which do you prefer?\n1. End the edit when your voice ends (14 seconds) (recommended)\n2. Keep 30 seconds, the last 16 seconds play without voice\nTap an option, or tell me in your own words.',
                    "End the edit when your voice ends (14 seconds)",
                    "The video is exactly as long as your voice.",
                    "Keep 30 seconds, the last 16 seconds play without voice",
                    "Your voice plays first, then the footage carries on in silence.",
                    'Your brief says "Keep 30 seconds" and your voice runs 14 seconds. your voice can\'t fill the whole video. I can make it End the edit when your voice ends (14 seconds). Reply "End the edit when your voice ends (14 seconds)" to go with that, or change your request.',
                ],
                [
                    ["end when my voice ends", "match my voice", "match the voice"],
                    ["keep the length", "keep the length without voice"],
                ],
                [
                    'Your length choice is still waiting. Your brief says "Keep 30 seconds" and your voice runs 14 seconds.',
                    "your voice can't fill the whole video.",
                    'Your length choice is still waiting. Your brief says "Keep 30 seconds" and your voice runs 14 seconds. Unfortunately your voice can\'t fill the whole video. Which do you prefer?\n1. End the edit when your voice ends (14 seconds) (recommended)\n2. Keep 30 seconds, the last 16 seconds play without voice\nTap an option, or tell me in your own words.',
                    "End the edit when your voice ends (14 seconds)",
                    "The video is exactly as long as your voice.",
                    "Keep 30 seconds, the last 16 seconds play without voice",
                    "Your voice plays first, then the footage carries on in silence.",
                    'Your length choice is still waiting. Your brief says "Keep 30 seconds" and your voice runs 14 seconds. your voice can\'t fill the whole video. I can make it End the edit when your voice ends (14 seconds). Reply "End the edit when your voice ends (14 seconds)" to go with that, or change your request.',
                ],
            ],
            "which_voice": [
                [
                    "More than one of your clips has someone talking.",
                    "only one recording can be the voice that plays under the rest.",
                    "More than one of your clips has someone talking. Unfortunately only one recording can be the voice that plays under the rest. Which do you prefer?\n1. Clip 1 (20 s): “hello there”\n2. Clip 2 (30 s): “second speaker here”\nTap an option, or tell me in your own words.",
                    "Clip 1 (20 s): “hello there”",
                    "Use this recording's voice under the other clips.",
                    "Clip 2 (30 s): “second speaker here”",
                    "Use this recording's voice under the other clips.",
                    'More than one of your clips has someone talking. Unfortunately only one recording can be the voice that plays under the rest. The most I can do is: Clip 1 (20 s): “hello there”. Reply "Clip 1 (20 s): “hello there”" to go with that, or change your request.',
                ],
                [["clip 1", "1. klip"], ["clip 2", "2. klip"]],
            ],
        },
        "tr": {
            "coverage_cap_below_coverage": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                    "Daha az klip seç",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                ],
                [["update footage", "add footage", "choose fewer clips", "Choose fewer clips"]],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                    "Daha az klip seç",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_insufficient_capacity": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili kaynak 40 saniye sürüyor ama çekimler tekrar olmadan yalnızca 2 saniye sağlayabiliyor. Daha uzun çekim ekle.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kaynak 40 saniye sürüyor ama çekimler tekrar olmadan yalnızca 2 saniye sağlayabiliyor. Daha uzun çekim ekle. Yapabileceğim en fazla şu: Daha uzun çekim ekle. Bunu istiyorsan "Daha uzun çekim ekle" yaz ya da isteğini değiştir.',
                    "Daha uzun çekim ekle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kaynak 40 saniye sürüyor ama çekimler tekrar olmadan yalnızca 2 saniye sağlayabiliyor. Daha uzun çekim ekle. Yapabileceğim en fazla şu: Daha uzun çekim ekle. Bunu istiyorsan "Daha uzun çekim ekle" yaz ya da isteğini değiştir.',
                ],
                [["update footage", "add footage", "choose fewer clips", "Add longer footage"]],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili kaynak 40 saniye sürüyor ama çekimler tekrar olmadan yalnızca 2 saniye sağlayabiliyor. Daha uzun çekim ekle.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kaynak 40 saniye sürüyor ama çekimler tekrar olmadan yalnızca 2 saniye sağlayabiliyor. Daha uzun çekim ekle. Yapabileceğim en fazla şu: Daha uzun çekim ekle. Bunu istiyorsan "Daha uzun çekim ekle" yaz ya da isteğini değiştir.',
                    "Daha uzun çekim ekle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kaynak 40 saniye sürüyor ama çekimler tekrar olmadan yalnızca 2 saniye sağlayabiliyor. Daha uzun çekim ekle. Yapabileceğim en fazla şu: Daha uzun çekim ekle. Bunu istiyorsan "Daha uzun çekim ekle" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_insufficient_media": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "En az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60'ını alabilir.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef en az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60\'ını alabilir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef en az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60\'ını alabilir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "update footage",
                        "add footage",
                        "choose fewer clips",
                        "Update the selected clips",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "En az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60'ını alabilir.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef en az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60\'ını alabilir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef en az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60\'ını alabilir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_leave_out": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle.",
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Hangisini tercih edersin?\n1. Seçili klipleri güncelle\n2. O klipleri dışarıda bırak (önerilen)\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    "O klipleri dışarıda bırak",
                    "Bu düzenlemeyi hâlâ bağlı olan kliplerle yapacağım.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "update footage",
                        "add footage",
                        "choose fewer clips",
                        "Update the selected clips",
                    ],
                    [
                        "leave them out",
                        "without them",
                        "skip those clips",
                        "continue without them",
                        "onları dışarıda bırak",
                        "onlar olmadan devam et",
                        "o klipleri atla",
                        "onlarsız devam et",
                        "Leave those clips out",
                    ],
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle.",
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Hangisini tercih edersin?\n1. Seçili klipleri güncelle\n2. O klipleri dışarıda bırak (önerilen)\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    "O klipleri dışarıda bırak",
                    "Bu düzenlemeyi hâlâ bağlı olan kliplerle yapacağım.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_no_usable_sources": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "update footage",
                        "add footage",
                        "choose fewer clips",
                        "Update the selected clips",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_no_video_sources": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "update footage",
                        "add footage",
                        "choose fewer clips",
                        "Update the selected clips",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_single_hero_order_conflict": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "update footage",
                        "add footage",
                        "choose fewer clips",
                        "Update the selected clips",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_source_count_limit": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Her biri kurguya girebilsin diye bu video için en fazla 1 klip seç.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri kurguya girebilsin diye bu video için en fazla 1 klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                    "Daha az klip seç",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri kurguya girebilsin diye bu video için en fazla 1 klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                ],
                [["update footage", "add footage", "choose fewer clips", "Choose fewer clips"]],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "Her biri kurguya girebilsin diye bu video için en fazla 1 klip seç.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri kurguya girebilsin diye bu video için en fazla 1 klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                    "Daha az klip seç",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef her biri kurguya girebilsin diye bu video için en fazla 1 klip seç. Yapabileceğim en fazla şu: Daha az klip seç. Bunu istiyorsan "Daha az klip seç" yaz ya da isteğini değiştir.',
                ],
            ],
            "coverage_target_below_coverage": [
                [
                    "Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "İstenen 4 saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef istenen 4 saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef istenen 4 saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "update footage",
                        "add footage",
                        "choose fewer clips",
                        "Update the selected clips",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
                    "İstenen 4 saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef istenen 4 saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                    "Seçili klipleri güncelle",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                    'Uzunluk seçimin hâlâ bekliyor. Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. Maalesef istenen 4 saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir. Yapabileceğim en fazla şu: Seçili klipleri güncelle. Bunu istiyorsan "Seçili klipleri güncelle" yaz ya da isteğini değiştir.',
                ],
            ],
            "duration_vs_count": [
                [
                    'İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 166 klip var.',
                    "15 saniyede 166 klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık 0.1 saniye lazım).",
                    'İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 166 klip var. Maalesef 15 saniyede 166 klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık 0.1 saniye lazım). Hangisini tercih edersin?\n1. Süreyi 16.6 saniyeye uzat (önerilen)\n2. 15 saniyeyi koru, 150 klip kullan\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "Süreyi 16.6 saniyeye uzat",
                    "Her klip en az 0.1 saniye görünür, böylece 166 klibin hepsi kalır.",
                    "15 saniyeyi koru, 150 klip kullan",
                    "166 klibinden 150 tanesini kullanır, çekimlerine eşit aralıklarla dağıtır; diğerleri dışarıda kalır.",
                    'İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 166 klip var. Maalesef 15 saniyede 166 klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık 0.1 saniye lazım). Yapabileceğim en fazla şu: Süreyi 16.6 saniyeye uzat. Bunu istiyorsan "Süreyi 16.6 saniyeye uzat" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "extend",
                        "extend it",
                        "longer",
                        "make it longer",
                        "extend the length",
                        "uzat",
                        "uzatalım",
                        "süreyi uzat",
                        "daha uzun yap",
                        "daha uzun",
                        "uzun olsun",
                        "videoyu uzat",
                        "süreyi artır",
                        "Extend it to 16.6 seconds",
                    ],
                    [
                        "fewer",
                        "fewer clips",
                        "use fewer",
                        "use fewer clips",
                        "keep it short",
                        "daha az klip",
                        "az klip",
                        "daha az",
                        "kısa kalsın",
                        "kısa tut",
                        "süreyi koru",
                        "süreyi değiştirme",
                        "klip sayısını azalt",
                        "klipleri azalt",
                        "Keep 15 seconds with 150 clips",
                    ],
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 166 klip var.',
                    "15 saniyede 166 klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık 0.1 saniye lazım).",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 166 klip var. Maalesef 15 saniyede 166 klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık 0.1 saniye lazım). Hangisini tercih edersin?\n1. Süreyi 16.6 saniyeye uzat (önerilen)\n2. 15 saniyeyi koru, 150 klip kullan\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "Süreyi 16.6 saniyeye uzat",
                    "Her klip en az 0.1 saniye görünür, böylece 166 klibin hepsi kalır.",
                    "15 saniyeyi koru, 150 klip kullan",
                    "166 klibinden 150 tanesini kullanır, çekimlerine eşit aralıklarla dağıtır; diğerleri dışarıda kalır.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 166 klip var. Maalesef 15 saniyede 166 klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık 0.1 saniye lazım). Yapabileceğim en fazla şu: Süreyi 16.6 saniyeye uzat. Bunu istiyorsan "Süreyi 16.6 saniyeye uzat" yaz ya da isteğini değiştir.',
                ],
            ],
            "freeze_one_moment": [
                [
                    "O anda kareyi dondurmamı istedin. O anı klibinde buldum (5.0–8.0. saniyeler arası) ama tam anını bilemiyorum ve tahmin etmeyeceğim.",
                    "Hangi kare dursun?",
                    "O anda kareyi dondurmamı istedin. O anı klibinde buldum (5.0–8.0. saniyeler arası) ama tam anını bilemiyorum ve tahmin etmeyeceğim. Hangi kare dursun?\n1. Başına yakın (5.7 sn)\n2. Ortası (6.5 sn) (önerilen)\n3. Sonuna yakın (7.3 sn)\n4. Donma olmasın\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "Başına yakın (5.7 sn)",
                    "",
                    "Ortası (6.5 sn)",
                    "",
                    "Sonuna yakın (7.3 sn)",
                    "",
                    "Donma olmasın",
                    "O an normal oynar, donmadan.",
                    'O anda kareyi dondurmamı istedin. O anı klibinde buldum (5.0–8.0. saniyeler arası) ama tam anını bilemiyorum ve tahmin etmeyeceğim. Maalesef hangi kare dursun? Yapabileceğim en fazla şu: Başına yakın (5.7 sn). Bunu istiyorsan "Başına yakın (5.7 sn)" yaz ya da isteğini değiştir.',
                ],
                [
                    ["early", "earlier", "erken", "başına yakın", "Early in it (5.7s)"],
                    ["middle", "the middle", "mid", "orta", "ortası", "Middle (6.5s)"],
                    ["late", "later", "sonuna yakın", "Late in it (7.3s)"],
                    [
                        "no freeze",
                        "no freeze please",
                        "skip the freeze",
                        "skip freeze",
                        "without a freeze",
                        "without the freeze",
                        "dont freeze",
                        "don t freeze",
                        "do not freeze",
                        "donma olmasın",
                        "dondurma yapma",
                        "dondurmadan",
                        "donmasın",
                        "No freeze",
                    ],
                ],
            ],
            "freeze_tied_moments": [
                [
                    "Bir anda kareyi dondurmamı istedin; çekimlerinde tarif ettiğine uyan birden fazla an var. Hangisi olduğunu tahmin etmeyeceğim.",
                    "Hangi an donsun? Ortasındaki kareyi tutarım.",
                    "Bir anda kareyi dondurmamı istedin; çekimlerinde tarif ettiğine uyan birden fazla an var. Hangisi olduğunu tahmin etmeyeceğim. Maalesef hangi an donsun? Ortasındaki kareyi tutarım. Hangisini tercih edersin?\n1. “his bite” (5.0–8.0 sn, 1. klip)\n2. “another bite” (5.0–8.0 sn, 2. klip)\n3. Donma olmasın\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "“his bite” (5.0–8.0 sn, 1. klip)",
                    "",
                    "“another bite” (5.0–8.0 sn, 2. klip)",
                    "",
                    "Donma olmasın",
                    "O an normal oynar, donmadan.",
                    'Bir anda kareyi dondurmamı istedin; çekimlerinde tarif ettiğine uyan birden fazla an var. Hangisi olduğunu tahmin etmeyeceğim. Maalesef hangi an donsun? Ortasındaki kareyi tutarım. Yapabileceğim en fazla şu: “his bite” (5.0–8.0 sn, 1. klip). Bunu istiyorsan "“his bite” (5.0–8.0 sn, 1. klip)" yaz ya da isteğini değiştir.',
                ],
                [
                    ["“his bite” (5.0–8.0s, clip 1)"],
                    ["“another bite” (5.0–8.0s, clip 2)"],
                    [
                        "no freeze",
                        "no freeze please",
                        "skip the freeze",
                        "skip freeze",
                        "without a freeze",
                        "without the freeze",
                        "dont freeze",
                        "don t freeze",
                        "do not freeze",
                        "donma olmasın",
                        "dondurma yapma",
                        "dondurmadan",
                        "donmasın",
                        "No freeze",
                    ],
                ],
            ],
            "holds_extend": [
                [
                    'İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor.',
                    "O fotoğraflar ve çekimlerin yaklaşık 12 saniye istiyor, 5 saniyeden fazla.",
                    'İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 12 saniye istiyor, 5 saniyeden fazla. Hangisini tercih edersin?\n1. Videoyu 12 saniye yap (önerilen)\n2. 5 saniyeyi koru, fotoğraflar normal hızda\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "Videoyu 12 saniye yap",
                    "Her fotoğraf verdiğin süre kadar kalır, çekimler de etrafında oynar.",
                    "5 saniyeyi koru, fotoğraflar normal hızda",
                    "Süre verdiğin fotoğraflar her şey sığsın diye düzenlemenin kendi hızında oynar.",
                    'İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 12 saniye istiyor, 5 saniyeden fazla. Yapabileceğim en fazla şu: Videoyu 12 saniye yap. Bunu istiyorsan "Videoyu 12 saniye yap" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "extend",
                        "make it longer",
                        "longer",
                        "12 seconds",
                        "uzat",
                        "uzatalım",
                        "süreyi uzat",
                        "daha uzun yap",
                        "daha uzun",
                        "uzun olsun",
                        "videoyu uzat",
                        "süreyi artır",
                        "Make it 12 seconds",
                    ],
                    [
                        "keep the length",
                        "keep it",
                        "keep 5 seconds",
                        "süreyi koru",
                        "süre kalsın",
                        "süreyi değiştirme",
                        "Keep 5 seconds, photos at the usual pace",
                    ],
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor.',
                    "O fotoğraflar ve çekimlerin yaklaşık 12 saniye istiyor, 5 saniyeden fazla.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 12 saniye istiyor, 5 saniyeden fazla. Hangisini tercih edersin?\n1. Videoyu 12 saniye yap (önerilen)\n2. 5 saniyeyi koru, fotoğraflar normal hızda\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "Videoyu 12 saniye yap",
                    "Her fotoğraf verdiğin süre kadar kalır, çekimler de etrafında oynar.",
                    "5 saniyeyi koru, fotoğraflar normal hızda",
                    "Süre verdiğin fotoğraflar her şey sığsın diye düzenlemenin kendi hızında oynar.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 12 saniye istiyor, 5 saniyeden fazla. Yapabileceğim en fazla şu: Videoyu 12 saniye yap. Bunu istiyorsan "Videoyu 12 saniye yap" yaz ya da isteğini değiştir.',
                ],
            ],
            "holds_keep_only": [
                [
                    'İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor.',
                    "O fotoğraflar ve çekimlerin yaklaşık 400 saniye istiyor, 5 saniyeden fazla.",
                    'İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 400 saniye istiyor, 5 saniyeden fazla. Yapabileceğim en fazla şu: 5 saniyeyi koru, fotoğraflar normal hızda. Bunu istiyorsan "5 saniyeyi koru, fotoğraflar normal hızda" yaz ya da isteğini değiştir.',
                    "5 saniyeyi koru, fotoğraflar normal hızda",
                    "Süre verdiğin fotoğraflar her şey sığsın diye düzenlemenin kendi hızında oynar.",
                    'İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 400 saniye istiyor, 5 saniyeden fazla. Yapabileceğim en fazla şu: 5 saniyeyi koru, fotoğraflar normal hızda. Bunu istiyorsan "5 saniyeyi koru, fotoğraflar normal hızda" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "keep the length",
                        "keep it",
                        "keep 5 seconds",
                        "süreyi koru",
                        "süre kalsın",
                        "süreyi değiştirme",
                        "Keep 5 seconds, photos at the usual pace",
                    ]
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor.',
                    "O fotoğraflar ve çekimlerin yaklaşık 400 saniye istiyor, 5 saniyeden fazla.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 400 saniye istiyor, 5 saniyeden fazla. Yapabileceğim en fazla şu: 5 saniyeyi koru, fotoğraflar normal hızda. Bunu istiyorsan "5 saniyeyi koru, fotoğraflar normal hızda" yaz ya da isteğini değiştir.',
                    "5 saniyeyi koru, fotoğraflar normal hızda",
                    "Süre verdiğin fotoğraflar her şey sığsın diye düzenlemenin kendi hızında oynar.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 5 seconds" yazıyor ve süre verdiğin fotoğraflar 6 saniye tutuyor. Maalesef O fotoğraflar ve çekimlerin yaklaşık 400 saniye istiyor, 5 saniyeden fazla. Yapabileceğim en fazla şu: 5 saniyeyi koru, fotoğraflar normal hızda. Bunu istiyorsan "5 saniyeyi koru, fotoğraflar normal hızda" yaz ya da isteğini değiştir.',
                ],
            ],
            "length_song": [
                [
                    'İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 14 saniye.',
                    "kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam.",
                    'İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 14 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 14 saniyeyi kullan. Bunu istiyorsan "Mevcut 14 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "Mevcut 14 saniyeyi kullan",
                    "Video tekrar veya sessiz uzatma olmadan şarkı süresini takip eder.",
                    'İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 14 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 14 saniyeyi kullan. Bunu istiyorsan "Mevcut 14 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "use available length",
                        "use the available length",
                        "match the source",
                        "Use the available 14 seconds",
                    ]
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 14 saniye.',
                    "kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 14 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 14 saniyeyi kullan. Bunu istiyorsan "Mevcut 14 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "Mevcut 14 saniyeyi kullan",
                    "Video tekrar veya sessiz uzatma olmadan şarkı süresini takip eder.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 14 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 14 saniyeyi kullan. Bunu istiyorsan "Mevcut 14 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
            ],
            "length_song_window": [
                [
                    'İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 30 saniye.',
                    "kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam.",
                    'İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 30 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Hangisini tercih edersin?\n1. 20 saniyeye kısalt (önerilen)\n2. Mevcut 30 saniyeyi kullan\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "20 saniyeye kısalt",
                    "Bu şarkı için desteklenen kırpmayı kullanır.",
                    "Mevcut 30 saniyeyi kullan",
                    "Video tekrar veya sessiz uzatma olmadan şarkı süresini takip eder.",
                    'İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 30 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Yapabileceğim en fazla şu: 20 saniyeye kısalt. Bunu istiyorsan "20 saniyeye kısalt" yaz ya da isteğini değiştir.',
                ],
                [
                    ["trim it", "trim", "keep requested length", "Trim it to 20 seconds"],
                    [
                        "use available length",
                        "use the available length",
                        "match the source",
                        "Use the available 30 seconds",
                    ],
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 30 saniye.',
                    "kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 30 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Hangisini tercih edersin?\n1. 20 saniyeye kısalt (önerilen)\n2. Mevcut 30 saniyeyi kullan\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "20 saniyeye kısalt",
                    "Bu şarkı için desteklenen kırpmayı kullanır.",
                    "Mevcut 30 saniyeyi kullan",
                    "Video tekrar veya sessiz uzatma olmadan şarkı süresini takip eder.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 20 seconds" yazıyor ama mevcut şarkı 30 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 20 saniyelik bir video yapamam. Yapabileceğim en fazla şu: 20 saniyeye kısalt. Bunu istiyorsan "20 saniyeye kısalt" yaz ya da isteğini değiştir.',
                ],
            ],
            "length_take": [
                [
                    'İsteğinde "Keep 30 seconds" yazıyor ama mevcut konuşma kaydı 12 saniye.',
                    "kaynağın zamanlamasını değiştirmeden 30 saniyelik bir video yapamam.",
                    'İsteğinde "Keep 30 seconds" yazıyor ama mevcut konuşma kaydı 12 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 30 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 12 saniyeyi kullan. Bunu istiyorsan "Mevcut 12 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "Mevcut 12 saniyeyi kullan",
                    "Video tekrar veya sessiz uzatma olmadan konuşma kaydı süresini takip eder.",
                    'İsteğinde "Keep 30 seconds" yazıyor ama mevcut konuşma kaydı 12 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 30 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 12 saniyeyi kullan. Bunu istiyorsan "Mevcut 12 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "use available length",
                        "use the available length",
                        "match the source",
                        "Use the available 12 seconds",
                    ]
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 30 seconds" yazıyor ama mevcut konuşma kaydı 12 saniye.',
                    "kaynağın zamanlamasını değiştirmeden 30 saniyelik bir video yapamam.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 30 seconds" yazıyor ama mevcut konuşma kaydı 12 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 30 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 12 saniyeyi kullan. Bunu istiyorsan "Mevcut 12 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "Mevcut 12 saniyeyi kullan",
                    "Video tekrar veya sessiz uzatma olmadan konuşma kaydı süresini takip eder.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 30 seconds" yazıyor ama mevcut konuşma kaydı 12 saniye. Maalesef kaynağın zamanlamasını değiştirmeden 30 saniyelik bir video yapamam. Yapabileceğim en fazla şu: Mevcut 12 saniyeyi kullan. Bunu istiyorsan "Mevcut 12 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
            ],
            "length_take_window": [
                [
                    "Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar.",
                    "Bu cümleleri değiştirmeden 7 saniyelik bir video yapamam.",
                    'Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "O 5 saniyeyi kullan",
                    "Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok.",
                    'Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "use available length",
                        "use the available length",
                        "match the source",
                        "Keep those 5 seconds",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar.",
                    "Bu cümleleri değiştirmeden 7 saniyelik bir video yapamam.",
                    'Uzunluk seçimin hâlâ bekliyor. Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "O 5 saniyeyi kullan",
                    "Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok.",
                    'Uzunluk seçimin hâlâ bekliyor. Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
            ],
            "length_take_window_longer": [
                [
                    "Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar.",
                    "Bu cümleleri değiştirmeden 3 saniyelik bir video yapamam.",
                    'Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 3 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "O 5 saniyeyi kullan",
                    "Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok.",
                    'Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 3 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "use available length",
                        "use the available length",
                        "match the source",
                        "Keep those 5 seconds",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar.",
                    "Bu cümleleri değiştirmeden 3 saniyelik bir video yapamam.",
                    'Uzunluk seçimin hâlâ bekliyor. Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 3 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "O 5 saniyeyi kullan",
                    "Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok.",
                    'Uzunluk seçimin hâlâ bekliyor. Başlangıç ve bitiş cümlelerin çekiminin yaklaşık 5 saniyesini tutuyor: “what do you think?” cümlesinden “Nice!” cümlesinin hemen sonrasına kadar. Maalesef bu cümleleri değiştirmeden 3 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 5 saniyeyi kullan. Bunu istiyorsan "O 5 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
            ],
            "length_take_window_start_only": [
                [
                    "Başlangıç cümlen çekiminin yaklaşık 11.7 saniyesini tutuyor: “what do you think?” cümlesinden sona kadar.",
                    "Bu cümleyi değiştirmeden 7 saniyelik bir video yapamam.",
                    'Başlangıç cümlen çekiminin yaklaşık 11.7 saniyesini tutuyor: “what do you think?” cümlesinden sona kadar. Maalesef bu cümleyi değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 11.7 saniyeyi kullan. Bunu istiyorsan "O 11.7 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "O 11.7 saniyeyi kullan",
                    "Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok.",
                    'Başlangıç cümlen çekiminin yaklaşık 11.7 saniyesini tutuyor: “what do you think?” cümlesinden sona kadar. Maalesef bu cümleyi değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 11.7 saniyeyi kullan. Bunu istiyorsan "O 11.7 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "use available length",
                        "use the available length",
                        "match the source",
                        "Keep those 11.7 seconds",
                    ]
                ],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Başlangıç cümlen çekiminin yaklaşık 11.7 saniyesini tutuyor: “what do you think?” cümlesinden sona kadar.",
                    "Bu cümleyi değiştirmeden 7 saniyelik bir video yapamam.",
                    'Uzunluk seçimin hâlâ bekliyor. Başlangıç cümlen çekiminin yaklaşık 11.7 saniyesini tutuyor: “what do you think?” cümlesinden sona kadar. Maalesef bu cümleyi değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 11.7 saniyeyi kullan. Bunu istiyorsan "O 11.7 saniyeyi kullan" yaz ya da isteğini değiştir.',
                    "O 11.7 saniyeyi kullan",
                    "Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok.",
                    'Uzunluk seçimin hâlâ bekliyor. Başlangıç cümlen çekiminin yaklaşık 11.7 saniyesini tutuyor: “what do you think?” cümlesinden sona kadar. Maalesef bu cümleyi değiştirmeden 7 saniyelik bir video yapamam. Yapabileceğim en fazla şu: O 11.7 saniyeyi kullan. Bunu istiyorsan "O 11.7 saniyeyi kullan" yaz ya da isteğini değiştir.',
                ],
            ],
            "order_basis": [
                [
                    "Klipleri çektiğin sırayla istedin.",
                    "hiçbir klibinde çekim saati yok, bu yüzden onları o sıraya koyamıyorum.",
                    "Klipleri çektiğin sırayla istedin. Maalesef hiçbir klibinde çekim saati yok, bu yüzden onları o sıraya koyamıyorum. Hangisini tercih edersin?\n1. Klipleri eklediğin sırayı kullan (önerilen)\n2. Sabit bir sıra olmadan devam et\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "Klipleri eklediğin sırayı kullan",
                    "Klipler projeye eklendikleri sırayla oynar.",
                    "Sabit bir sıra olmadan devam et",
                    "Belirli bir sıra vaat etmem ya da kontrol etmem; klipler yine de mantıklı bir sırayla oynar.",
                    'Klipleri çektiğin sırayla istedin. Maalesef hiçbir klibinde çekim saati yok, bu yüzden onları o sıraya koyamıyorum. Yapabileceğim en fazla şu: Klipleri eklediğin sırayı kullan. Bunu istiyorsan "Klipleri eklediğin sırayı kullan" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "upload order",
                        "order i added them",
                        "the order i added them",
                        "order i uploaded them",
                        "attachment order",
                        "as uploaded",
                        "as added",
                        "eklediğim sıra",
                        "eklediğim sırayla",
                        "yüklediğim sıra",
                        "yüklediğim sırayla",
                        "ekleme sırası",
                        "yükleme sırası",
                        "eklenme sırası",
                        "ekleme sırasına göre",
                        "eklediğim gibi",
                        "yüklediğim gibi",
                        "Use the order you added the clips",
                    ],
                    [
                        "without order",
                        "no order",
                        "skip the order",
                        "continue without order",
                        "no fixed order",
                        "sırasız",
                        "sırasız devam et",
                        "sıra olmasın",
                        "sıra yok",
                        "sıra olmadan",
                        "sıra olmadan devam et",
                        "sabit sıra olmasın",
                        "sabit sıra yok",
                        "sırayı atla",
                        "sıra önemli değil",
                        "fark etmez",
                        "Continue without a fixed order",
                    ],
                ],
            ],
            "speech_occurrence_cut": [
                [
                    '"thank you Karo" kısmını kesmemi istedin. Koruduğum kısımda 2 kez geçiyor.',
                    "Hangisini keseyim?",
                    '"thank you Karo" kısmını kesmemi istedin. Koruduğum kısımda 2 kez geçiyor. Hangisini keseyim?\n1. 0:03 “…are at Merope, thank you Karo. Merope is great, thank…”\n2. 0:10 “…Merope is great, thank you Karo.”\n3. Her seferinde\n4. Kesme\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "0:03 “…are at Merope, thank you Karo. Merope is great, thank…”",
                    "",
                    "0:10 “…Merope is great, thank you Karo.”",
                    "",
                    "Her seferinde",
                    "",
                    "Kesme",
                    "",
                    '"thank you Karo" kısmını kesmemi istedin. Koruduğum kısımda 2 kez geçiyor. Maalesef hangisini keseyim? Yapabileceğim en fazla şu: 0:03 “…are at Merope, thank you Karo. Merope is great, thank…”. Bunu istiyorsan "0:03 “…are at Merope, thank you Karo. Merope is great, thank…”" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "1 seçenek",
                        "seçenek 1",
                        "1 numara",
                        "numara 1",
                        "1 numaralı",
                        "1 numaralı seçenek",
                        "1 nolu",
                        "1 şık",
                        "şık 1",
                        "birinci",
                        "birinci seçenek",
                        "birinci şık",
                        "birincisi",
                        "birincisi seçenek",
                        "birincisi şık",
                        "birincisini",
                        "birincisini seçenek",
                        "birincisini şık",
                        "ilk",
                        "ilk seçenek",
                        "ilk şık",
                        "ilki",
                        "ilki seçenek",
                        "ilki şık",
                        "ilkini",
                        "ilkini seçenek",
                        "ilkini şık",
                        "bir numara",
                        "numara bir",
                        "seçenek bir",
                        "bir numaralı",
                    ],
                    [
                        "2 seçenek",
                        "seçenek 2",
                        "2 numara",
                        "numara 2",
                        "2 numaralı",
                        "2 numaralı seçenek",
                        "2 nolu",
                        "2 şık",
                        "şık 2",
                        "ikinci",
                        "ikinci seçenek",
                        "ikinci şık",
                        "ikincisi",
                        "ikincisi seçenek",
                        "ikincisi şık",
                        "ikincisini",
                        "ikincisini seçenek",
                        "ikincisini şık",
                        "iki numara",
                        "numara iki",
                        "seçenek iki",
                        "iki numaralı",
                    ],
                    [
                        "all of them",
                        "every time",
                        "both",
                        "hepsi",
                        "hepsini",
                        "ikisi de",
                        "Every time",
                    ],
                    ["Don't cut it"],
                ],
            ],
            "speech_occurrence_text": [
                [
                    '"Merope" denildiğinde "MEROPE" yazısını istedin. Koruduğum kısımda "Merope" 2 kez geçiyor ve kimin konuştuğunu ayırt edemiyorum.',
                    "Yazı hangisinde çıksın?",
                    '"Merope" denildiğinde "MEROPE" yazısını istedin. Koruduğum kısımda "Merope" 2 kez geçiyor ve kimin konuştuğunu ayırt edemiyorum. Yazı hangisinde çıksın?\n1. 0:02 “We are at Merope, thank you Karo. Merope…”\n2. 0:08 “…thank you Karo. Merope is great, thank you…”\n3. Yazıyı koyma\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "0:02 “We are at Merope, thank you Karo. Merope…”",
                    "",
                    "0:08 “…thank you Karo. Merope is great, thank you…”",
                    "",
                    "Yazıyı koyma",
                    "",
                    '"Merope" denildiğinde "MEROPE" yazısını istedin. Koruduğum kısımda "Merope" 2 kez geçiyor ve kimin konuştuğunu ayırt edemiyorum. Maalesef yazı hangisinde çıksın? Yapabileceğim en fazla şu: 0:02 “We are at Merope, thank you Karo. Merope…”. Bunu istiyorsan "0:02 “We are at Merope, thank you Karo. Merope…”" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "1 seçenek",
                        "seçenek 1",
                        "1 numara",
                        "numara 1",
                        "1 numaralı",
                        "1 numaralı seçenek",
                        "1 nolu",
                        "1 şık",
                        "şık 1",
                        "birinci",
                        "birinci seçenek",
                        "birinci şık",
                        "birincisi",
                        "birincisi seçenek",
                        "birincisi şık",
                        "birincisini",
                        "birincisini seçenek",
                        "birincisini şık",
                        "ilk",
                        "ilk seçenek",
                        "ilk şık",
                        "ilki",
                        "ilki seçenek",
                        "ilki şık",
                        "ilkini",
                        "ilkini seçenek",
                        "ilkini şık",
                        "bir numara",
                        "numara bir",
                        "seçenek bir",
                        "bir numaralı",
                    ],
                    [
                        "2 seçenek",
                        "seçenek 2",
                        "2 numara",
                        "numara 2",
                        "2 numaralı",
                        "2 numaralı seçenek",
                        "2 nolu",
                        "2 şık",
                        "şık 2",
                        "ikinci",
                        "ikinci seçenek",
                        "ikinci şık",
                        "ikincisi",
                        "ikincisi seçenek",
                        "ikincisi şık",
                        "ikincisini",
                        "ikincisini seçenek",
                        "ikincisini şık",
                        "iki numara",
                        "numara iki",
                        "seçenek iki",
                        "iki numaralı",
                    ],
                    ["Leave the text off"],
                ],
            ],
            "take_line_end": [
                [
                    "“Thank you” cümlesini videonda söylenenler arasında bulamadım.",
                    "Hangi cümleyle bitsin?",
                    "“Thank you” cümlesini videonda söylenenler arasında bulamadım. Hangi cümleyle bitsin?\n1. “Lucas”\n2. “Acá tenemos salsas tradicionales argentinas,”\n3. “salsas criollas,”\n4. “chivichurros y sardines”\n5. “Gracias”\n6. Hiçbiri — sona kadar sürsün\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "“Lucas”",
                    "",
                    "“Acá tenemos salsas tradicionales argentinas,”",
                    "",
                    "“salsas criollas,”",
                    "",
                    "“chivichurros y sardines”",
                    "",
                    "“Gracias”",
                    "",
                    "Hiçbiri — sona kadar sürsün",
                    "Video yine çekimin sonuna kadar sürer.",
                    '“Thank you” cümlesini videonda söylenenler arasında bulamadım. Maalesef hangi cümleyle bitsin? Yapabileceğim en fazla şu: “Lucas”. Bunu istiyorsan "“Lucas”" yaz ya da isteğini değiştir.',
                ],
                [
                    ["Lucas"],
                    ["Acá tenemos salsas tradicionales argentinas,"],
                    ["salsas criollas,"],
                    ["chivichurros y sardines"],
                    ["Gracias"],
                    [
                        "none",
                        "none of these",
                        "none of them",
                        "neither",
                        "skip",
                        "skip it",
                        "no",
                        "keep it to the end",
                        "to the end",
                        "keep the end",
                        "hiçbiri",
                        "hiçbirini",
                        "hiçbiri değil",
                        "hayır",
                        "yok",
                        "atla",
                        "sona kadar",
                        "sonuna kadar",
                        "sona kadar sürsün",
                        "None of these — keep it to the end",
                    ],
                ],
            ],
            "take_line_start": [
                [
                    "“Here we have traditional Argentinian sauces” cümlesini videonda söylenenler arasında bulamadım.",
                    "Hangi cümleyle açılsın?",
                    "“Here we have traditional Argentinian sauces” cümlesini videonda söylenenler arasında bulamadım. Hangi cümleyle açılsın?\n1. “¿Qué te llaman?”\n2. “Lucas”\n3. “Acá tenemos salsas tradicionales argentinas,” (önerilen)\n4. “salsas criollas,”\n5. “chivichurros y sardines”\n6. Hiçbiri — baştan başlasın\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "“¿Qué te llaman?”",
                    "",
                    "“Lucas”",
                    "",
                    "“Acá tenemos salsas tradicionales argentinas,”",
                    "",
                    "“salsas criollas,”",
                    "",
                    "“chivichurros y sardines”",
                    "",
                    "Hiçbiri — baştan başlasın",
                    "Video yine çekimin başından açılır.",
                    '“Here we have traditional Argentinian sauces” cümlesini videonda söylenenler arasında bulamadım. Maalesef hangi cümleyle açılsın? Yapabileceğim en fazla şu: “¿Qué te llaman?”. Bunu istiyorsan "“¿Qué te llaman?”" yaz ya da isteğini değiştir.',
                ],
                [
                    ["¿Qué te llaman?"],
                    ["Lucas"],
                    ["Acá tenemos salsas tradicionales argentinas,"],
                    ["salsas criollas,"],
                    ["chivichurros y sardines"],
                    [
                        "none",
                        "none of these",
                        "none of them",
                        "neither",
                        "skip",
                        "skip it",
                        "no",
                        "keep it from the start",
                        "from the start",
                        "keep the start",
                        "hiçbiri",
                        "hiçbirini",
                        "hiçbiri değil",
                        "hayır",
                        "yok",
                        "atla",
                        "baştan",
                        "baştan başlasın",
                        "başından",
                        "None of these — keep it from the start",
                    ],
                ],
            ],
            "unclear_word": [
                [
                    "Klibinde 0:09 civarında bir kelimeyi anlayamadım: klibini iki kez dinledim, farklı kelimeler duydum ve tahmin yürütmeyeceğim.",
                    "Altyazıda ne yazsın?",
                    "Klibinde 0:09 civarında bir kelimeyi anlayamadım: klibini iki kez dinledim, farklı kelimeler duydum ve tahmin yürütmeyeceğim. Altyazıda ne yazsın?\n1. Çıkar\n2. salma y limón\n3. sal marina\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "Çıkar",
                    "Altyazılar o kelimeyi atlar.",
                    "salma y limón",
                    "Bunu altyazıya yaz.",
                    "sal marina",
                    "Bunu altyazıya yaz.",
                    'Klibinde 0:09 civarında bir kelimeyi anlayamadım: klibini iki kez dinledim, farklı kelimeler duydum ve tahmin yürütmeyeceğim. Maalesef altyazıda ne yazsın? Yapabileceğim en fazla şu: Çıkar. Bunu istiyorsan "Çıkar" yaz ya da isteğini değiştir.',
                ],
                [
                    [
                        "leave it out",
                        "leave that out",
                        "leave the word out",
                        "leave that word out",
                        "skip it",
                        "skip that word",
                        "i don't know it either, leave that word out of the captions",
                        "çıkar",
                        "kelimeyi çıkar",
                        "o kelimeyi çıkar",
                        "atla",
                        "boş bırak",
                        "Leave it out",
                    ],
                    [],
                    [],
                ],
            ],
            "voice_implicit_length": [
                [
                    "Sesin 75 saniye sürüyor.",
                    "Video ne kadar uzun olsun?",
                    "Sesin 75 saniye sürüyor. Video ne kadar uzun olsun?\n1. 5 saniye (önerilen)\n2. 30 saniye\n3. 45 saniye\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "5 saniye",
                    "Her klibin görülecek kadar ekranda kaldığı en kısa süre.",
                    "30 saniye",
                    "Daha uzun: her klip ekranda biraz daha uzun kalır.",
                    "45 saniye",
                    "Daha uzun: her klip ekranda biraz daha uzun kalır.",
                    'Sesin 75 saniye sürüyor. Video ne kadar uzun olsun? Şunu yapabilirim: 5 saniye. Bunu istiyorsan "5 saniye" yaz ya da isteğini değiştir.',
                ],
                [["5", "5 seconds"], ["30", "30 seconds"], ["45", "45 seconds"]],
                [
                    "Uzunluk seçimin hâlâ bekliyor. Sesin 75 saniye sürüyor.",
                    "Video ne kadar uzun olsun?",
                    "Uzunluk seçimin hâlâ bekliyor. Sesin 75 saniye sürüyor. Video ne kadar uzun olsun?\n1. 5 saniye (önerilen)\n2. 30 saniye\n3. 45 saniye\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "5 saniye",
                    "Her klibin görülecek kadar ekranda kaldığı en kısa süre.",
                    "30 saniye",
                    "Daha uzun: her klip ekranda biraz daha uzun kalır.",
                    "45 saniye",
                    "Daha uzun: her klip ekranda biraz daha uzun kalır.",
                    'Uzunluk seçimin hâlâ bekliyor. Sesin 75 saniye sürüyor. Video ne kadar uzun olsun? Şunu yapabilirim: 5 saniye. Bunu istiyorsan "5 saniye" yaz ya da isteğini değiştir.',
                ],
            ],
            "voice_short": [
                [
                    'İsteğinde "Keep 30 seconds" yazıyor ve sesin 14 saniye sürüyor.',
                    "sesin videonun tamamını dolduramıyor.",
                    'İsteğinde "Keep 30 seconds" yazıyor ve sesin 14 saniye sürüyor. Maalesef sesin videonun tamamını dolduramıyor. Hangisini tercih edersin?\n1. Sesin bittiğinde bitir (14 saniye) (önerilen)\n2. 30 saniye kalsın, son 16 saniye sessiz oynasın\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "Sesin bittiğinde bitir (14 saniye)",
                    "Video tam olarak sesin kadar uzun olur.",
                    "30 saniye kalsın, son 16 saniye sessiz oynasın",
                    "Önce sesin çalar, sonra çekim sessizce devam eder.",
                    'İsteğinde "Keep 30 seconds" yazıyor ve sesin 14 saniye sürüyor. sesin videonun tamamını dolduramıyor. Şunu yapabilirim: Sesin bittiğinde bitir (14 saniye). Bunu istiyorsan "Sesin bittiğinde bitir (14 saniye)" yaz ya da isteğini değiştir.',
                ],
                [
                    ["end when my voice ends", "match my voice", "match the voice"],
                    ["keep the length", "keep the length without voice"],
                ],
                [
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 30 seconds" yazıyor ve sesin 14 saniye sürüyor.',
                    "sesin videonun tamamını dolduramıyor.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 30 seconds" yazıyor ve sesin 14 saniye sürüyor. Maalesef sesin videonun tamamını dolduramıyor. Hangisini tercih edersin?\n1. Sesin bittiğinde bitir (14 saniye) (önerilen)\n2. 30 saniye kalsın, son 16 saniye sessiz oynasın\nBir seçeneğe dokun ya da kendi sözlerinle anlat.',
                    "Sesin bittiğinde bitir (14 saniye)",
                    "Video tam olarak sesin kadar uzun olur.",
                    "30 saniye kalsın, son 16 saniye sessiz oynasın",
                    "Önce sesin çalar, sonra çekim sessizce devam eder.",
                    'Uzunluk seçimin hâlâ bekliyor. İsteğinde "Keep 30 seconds" yazıyor ve sesin 14 saniye sürüyor. sesin videonun tamamını dolduramıyor. Şunu yapabilirim: Sesin bittiğinde bitir (14 saniye). Bunu istiyorsan "Sesin bittiğinde bitir (14 saniye)" yaz ya da isteğini değiştir.',
                ],
            ],
            "which_voice": [
                [
                    "Birden fazla klibinde biri konuşuyor.",
                    "diğerlerinin altında yalnızca bir kayıt ses olarak çalabilir.",
                    "Birden fazla klibinde biri konuşuyor. Maalesef diğerlerinin altında yalnızca bir kayıt ses olarak çalabilir. Hangisini tercih edersin?\n1. 1. klip (20 s): “hello there”\n2. 2. klip (30 s): “second speaker here”\nBir seçeneğe dokun ya da kendi sözlerinle anlat.",
                    "1. klip (20 s): “hello there”",
                    "Bu kaydın sesini diğer kliplerin altında kullan.",
                    "2. klip (30 s): “second speaker here”",
                    "Bu kaydın sesini diğer kliplerin altında kullan.",
                    'Birden fazla klibinde biri konuşuyor. Maalesef diğerlerinin altında yalnızca bir kayıt ses olarak çalabilir. Yapabileceğim en fazla şu: 1. klip (20 s): “hello there”. Bunu istiyorsan "1. klip (20 s): “hello there”" yaz ya da isteğini değiştir.',
                ],
                [["clip 1", "1. klip"], ["clip 2", "2. klip"]],
            ],
        },
    },
    "choice_questions.detect_order_vs_group": {
        "en": [
            [
                "You asked for a chronological video and for the clips grouped by group.",
                "your football and dodgeball clips were filmed mixed together, so I can't do both.",
                [
                    [
                        "Group by group, chronological inside each group",
                        "Each group plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them.",
                        [],
                    ],
                    [
                        "Keep it strictly chronological; groups may interleave",
                        "Clips play in the exact order you filmed them.",
                        [],
                    ],
                ],
            ],
            [
                "You asked for a chronological video and for the clips grouped by sport.",
                "your football and dodgeball clips were filmed mixed together, so I can't do both.",
                [
                    [
                        "Group by sport, chronological inside each sport",
                        "Each sport plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them.",
                        [],
                    ],
                    [
                        "Keep it strictly chronological; sports may interleave",
                        "Clips play in the exact order you filmed them.",
                        [],
                    ],
                ],
            ],
            [
                "You asked for a chronological video and for the clips grouped by group.",
                "your football and dodgeball clips were filmed mixed together, so I can't do both.",
                [
                    [
                        "Group by group, chronological inside each group",
                        "Each group plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them.",
                        [],
                    ],
                    [
                        "Keep it strictly chronological; groups may interleave",
                        "Clips play in the exact order you filmed them.",
                        [],
                    ],
                ],
            ],
            [
                "You asked for a chronological video and for the clips grouped by takım.",
                "your football and dodgeball clips were filmed mixed together, so I can't do both.",
                [
                    [
                        "Group by takım, chronological inside each takım",
                        "Each takım plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them.",
                        [],
                    ],
                    [
                        "Keep it strictly chronological; takıms may interleave",
                        "Clips play in the exact order you filmed them.",
                        [],
                    ],
                ],
            ],
        ],
        "tr": [
            [
                "Hem kronolojik bir video hem de kliplerin grup bazında gruplanmasını istedin.",
                "football ve dodgeball klipleri birbirine karışık çekilmiş, bu yüzden ikisini birden yapamıyorum.",
                [
                    [
                        "Her grup bir blok olsun; blok içinde kronolojik sırala",
                        "Her grup tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur.",
                        [
                            "grupla",
                            "gruplandır",
                            "önce grupla",
                            "Group by group, chronological inside each group",
                        ],
                    ],
                    [
                        "Tam kronolojik kalsın; grup klipleri iç içe geçebilir",
                        "Klipler tam olarak çektiğin sırayla oynar.",
                        [
                            "kronolojik",
                            "kronolojik kalsın",
                            "tam kronolojik",
                            "Keep it strictly chronological; groups may interleave",
                        ],
                    ],
                ],
            ],
            [
                "Hem kronolojik bir video hem de kliplerin spor bazında gruplanmasını istedin.",
                "football ve dodgeball klipleri birbirine karışık çekilmiş, bu yüzden ikisini birden yapamıyorum.",
                [
                    [
                        "Her spor bir blok olsun; blok içinde kronolojik sırala",
                        "Her spor tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur.",
                        [
                            "grupla",
                            "gruplandır",
                            "önce grupla",
                            "Group by sport, chronological inside each sport",
                        ],
                    ],
                    [
                        "Tam kronolojik kalsın; spor klipleri iç içe geçebilir",
                        "Klipler tam olarak çektiğin sırayla oynar.",
                        [
                            "kronolojik",
                            "kronolojik kalsın",
                            "tam kronolojik",
                            "Keep it strictly chronological; sports may interleave",
                        ],
                    ],
                ],
            ],
            [
                "Hem kronolojik bir video hem de kliplerin grup bazında gruplanmasını istedin.",
                "football ve dodgeball klipleri birbirine karışık çekilmiş, bu yüzden ikisini birden yapamıyorum.",
                [
                    [
                        "Her grup bir blok olsun; blok içinde kronolojik sırala",
                        "Her grup tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur.",
                        [
                            "grupla",
                            "gruplandır",
                            "önce grupla",
                            "Group by group, chronological inside each group",
                        ],
                    ],
                    [
                        "Tam kronolojik kalsın; grup klipleri iç içe geçebilir",
                        "Klipler tam olarak çektiğin sırayla oynar.",
                        [
                            "kronolojik",
                            "kronolojik kalsın",
                            "tam kronolojik",
                            "Keep it strictly chronological; groups may interleave",
                        ],
                    ],
                ],
            ],
            [
                "Hem kronolojik bir video hem de kliplerin takım bazında gruplanmasını istedin.",
                "football ve dodgeball klipleri birbirine karışık çekilmiş, bu yüzden ikisini birden yapamıyorum.",
                [
                    [
                        "Her takım bir blok olsun; blok içinde kronolojik sırala",
                        "Her takım tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur.",
                        [
                            "grupla",
                            "gruplandır",
                            "önce grupla",
                            "Group by takım, chronological inside each takım",
                        ],
                    ],
                    [
                        "Tam kronolojik kalsın; takım klipleri iç içe geçebilir",
                        "Klipler tam olarak çektiğin sırayla oynar.",
                        [
                            "kronolojik",
                            "kronolojik kalsın",
                            "tam kronolojik",
                            "Keep it strictly chronological; takıms may interleave",
                        ],
                    ],
                ],
            ],
        ],
    },
    "clip question texts with N more": {
        "en": [
            'I couldn\'t tell which of your clips to leave out for "label 0". Tap the ones to leave out, or tell me none.',
            'Two of your clips show "label 0". Which one should I use for it? Keep that one ticked and untick the other.',
            'I ticked the clips I found for "label 0". Tap any others that show it too, or untick one that doesn\'t.',
            "I couldn't find \"label 0\" in your clips. Tap the clip that shows it, or tell me there isn't one.",
            'The same photo is down for both "label 0" and "other". Which clip is "label 0"? Tap it, or tell me there isn\'t one.',
            "I couldn't tell which of your clips show \"label 0\". Tap the clips that do, or tell me there aren't any.",
            'About when does "label 0" appear in your video? Give a rough time, such as near the start or 30 seconds, or tell me it isn\'t there.',
            'I couldn\'t tell which of your clips to leave out for "label 0", "label 1". Tap the clips to leave out for each one, or mark the ones with none.',
            'More than one of your clips shows "label 0", "label 1". For each one, keep the clip to use ticked and untick the others.',
            'I ticked the clips I found for "label 0", "label 1". Tap any others that show it too, or untick one that doesn\'t.',
            'I couldn\'t find "label 0", "label 1" in your clips. Tap the clip for each one, or mark the ones that aren\'t there.',
            'The same photo is down for more than one of your moments. Tap the clip for each of "label 0", "label 1", or mark the ones that have none.',
            'I couldn\'t tell which of your clips match "label 0", "label 1". Tap the clips for each one, or mark the ones that have none.',
            'About when do "label 0", "label 1" appear in your video? Give a rough time for each, or tell me which aren\'t there.',
            'I couldn\'t tell which of your clips to leave out for "label 0", "label 1", "label 2", "label 3". Tap the clips to leave out for each one, or mark the ones with none.',
            'More than one of your clips shows "label 0", "label 1", "label 2", "label 3". For each one, keep the clip to use ticked and untick the others.',
            'I ticked the clips I found for "label 0", "label 1", "label 2", "label 3". Tap any others that show it too, or untick one that doesn\'t.',
            'I couldn\'t find "label 0", "label 1", "label 2", "label 3" in your clips. Tap the clip for each one, or mark the ones that aren\'t there.',
            'The same photo is down for more than one of your moments. Tap the clip for each of "label 0", "label 1", "label 2", "label 3", or mark the ones that have none.',
            'I couldn\'t tell which of your clips match "label 0", "label 1", "label 2", "label 3". Tap the clips for each one, or mark the ones that have none.',
            'About when do "label 0", "label 1", "label 2", "label 3" appear in your video? Give a rough time for each, or tell me which aren\'t there.',
            'I couldn\'t tell which of your clips to leave out for "label 0", "label 1", "label 2", "label 3" and 1 more. Tap the clips to leave out for each one, or mark the ones with none.',
            'More than one of your clips shows "label 0", "label 1", "label 2", "label 3" and 1 more. For each one, keep the clip to use ticked and untick the others.',
            'I ticked the clips I found for "label 0", "label 1", "label 2", "label 3" and 1 more. Tap any others that show it too, or untick one that doesn\'t.',
            'I couldn\'t find "label 0", "label 1", "label 2", "label 3" and 1 more in your clips. Tap the clip for each one, or mark the ones that aren\'t there.',
            'The same photo is down for more than one of your moments. Tap the clip for each of "label 0", "label 1", "label 2", "label 3" and 1 more, or mark the ones that have none.',
            'I couldn\'t tell which of your clips match "label 0", "label 1", "label 2", "label 3" and 1 more. Tap the clips for each one, or mark the ones that have none.',
            'About when do "label 0", "label 1", "label 2", "label 3" and 1 more appear in your video? Give a rough time for each, or tell me which aren\'t there.',
            'I couldn\'t tell which of your clips to leave out for "label 0", "label 1", "label 2", "label 3" and 3 more. Tap the clips to leave out for each one, or mark the ones with none.',
            'More than one of your clips shows "label 0", "label 1", "label 2", "label 3" and 3 more. For each one, keep the clip to use ticked and untick the others.',
            'I ticked the clips I found for "label 0", "label 1", "label 2", "label 3" and 3 more. Tap any others that show it too, or untick one that doesn\'t.',
            'I couldn\'t find "label 0", "label 1", "label 2", "label 3" and 3 more in your clips. Tap the clip for each one, or mark the ones that aren\'t there.',
            'The same photo is down for more than one of your moments. Tap the clip for each of "label 0", "label 1", "label 2", "label 3" and 3 more, or mark the ones that have none.',
            'I couldn\'t tell which of your clips match "label 0", "label 1", "label 2", "label 3" and 3 more. Tap the clips for each one, or mark the ones that have none.',
            'About when do "label 0", "label 1", "label 2", "label 3" and 3 more appear in your video? Give a rough time for each, or tell me which aren\'t there.',
        ],
        "tr": [
            '"label 0" için hangi klipleri çıkaracağımı anlayamadım. Çıkarılacak olanlara dokun ya da hiçbirini çıkarmamamı söyle.',
            '"label 0" iki klibinde de görünüyor. Hangisini kullanayım? Onu işaretli bırak, diğerinin işaretini kaldır.',
            '"label 0" için bulduğum klipleri işaretledim. Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır.',
            '"label 0" kliplerinde bulamadım. Onu gösteren klibe dokun ya da olmadığını söyle.',
            '"label 0" ve "other" için aynı fotoğraf seçilmiş. "label 0" hangi klip? Ona dokun ya da olmadığını söyle.',
            'Kliplerinden hangilerinde "label 0" olduğunu anlayamadım. Olanlara dokun ya da hiç olmadığını söyle.',
            'Videonda "label 0" yaklaşık ne zaman görünüyor? Başlara doğru veya 30. saniye gibi yaklaşık bir zaman ver ya da olmadığını söyle.',
            'Şunlar için hangi klipleri çıkaracağımı anlayamadım: "label 0", "label 1". Her biri için çıkarılacak klipleri seç ya da hiçbirini çıkarmayacaklarını işaretle.',
            'Şunlar birden fazla klibinde görünüyor: "label 0", "label 1". Her biri için kullanılacak klibi işaretli bırak, diğerlerinin işaretini kaldır.',
            '"label 0", "label 1" için bulduğum klipleri işaretledim. Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır.',
            'Şunları kliplerinde bulamadım: "label 0", "label 1". Her biri için klibe dokun ya da olmayanları işaretle.',
            'Birden fazla anın için aynı fotoğraf seçilmiş. Şunlar için klibi seç: "label 0", "label 1". Klibi olmayanları işaretle.',
            'Şunlar için hangi klipleri kastettiğini anlayamadım: "label 0", "label 1". Her biri için klipleri seç ya da hiç klibi olmayanları işaretle.',
            'Videonda şunlar yaklaşık ne zaman görünüyor: "label 0", "label 1"? Her biri için yaklaşık bir zaman ver ya da hangilerinin olmadığını söyle.',
            'Şunlar için hangi klipleri çıkaracağımı anlayamadım: "label 0", "label 1", "label 2", "label 3". Her biri için çıkarılacak klipleri seç ya da hiçbirini çıkarmayacaklarını işaretle.',
            'Şunlar birden fazla klibinde görünüyor: "label 0", "label 1", "label 2", "label 3". Her biri için kullanılacak klibi işaretli bırak, diğerlerinin işaretini kaldır.',
            '"label 0", "label 1", "label 2", "label 3" için bulduğum klipleri işaretledim. Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır.',
            'Şunları kliplerinde bulamadım: "label 0", "label 1", "label 2", "label 3". Her biri için klibe dokun ya da olmayanları işaretle.',
            'Birden fazla anın için aynı fotoğraf seçilmiş. Şunlar için klibi seç: "label 0", "label 1", "label 2", "label 3". Klibi olmayanları işaretle.',
            'Şunlar için hangi klipleri kastettiğini anlayamadım: "label 0", "label 1", "label 2", "label 3". Her biri için klipleri seç ya da hiç klibi olmayanları işaretle.',
            'Videonda şunlar yaklaşık ne zaman görünüyor: "label 0", "label 1", "label 2", "label 3"? Her biri için yaklaşık bir zaman ver ya da hangilerinin olmadığını söyle.',
            'Şunlar için hangi klipleri çıkaracağımı anlayamadım: "label 0", "label 1", "label 2", "label 3" ve 1 tane daha. Her biri için çıkarılacak klipleri seç ya da hiçbirini çıkarmayacaklarını işaretle.',
            'Şunlar birden fazla klibinde görünüyor: "label 0", "label 1", "label 2", "label 3" ve 1 tane daha. Her biri için kullanılacak klibi işaretli bırak, diğerlerinin işaretini kaldır.',
            '"label 0", "label 1", "label 2", "label 3" ve 1 tane daha için bulduğum klipleri işaretledim. Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır.',
            'Şunları kliplerinde bulamadım: "label 0", "label 1", "label 2", "label 3" ve 1 tane daha. Her biri için klibe dokun ya da olmayanları işaretle.',
            'Birden fazla anın için aynı fotoğraf seçilmiş. Şunlar için klibi seç: "label 0", "label 1", "label 2", "label 3" ve 1 tane daha. Klibi olmayanları işaretle.',
            'Şunlar için hangi klipleri kastettiğini anlayamadım: "label 0", "label 1", "label 2", "label 3" ve 1 tane daha. Her biri için klipleri seç ya da hiç klibi olmayanları işaretle.',
            'Videonda şunlar yaklaşık ne zaman görünüyor: "label 0", "label 1", "label 2", "label 3" ve 1 tane daha? Her biri için yaklaşık bir zaman ver ya da hangilerinin olmadığını söyle.',
            'Şunlar için hangi klipleri çıkaracağımı anlayamadım: "label 0", "label 1", "label 2", "label 3" ve 3 tane daha. Her biri için çıkarılacak klipleri seç ya da hiçbirini çıkarmayacaklarını işaretle.',
            'Şunlar birden fazla klibinde görünüyor: "label 0", "label 1", "label 2", "label 3" ve 3 tane daha. Her biri için kullanılacak klibi işaretli bırak, diğerlerinin işaretini kaldır.',
            '"label 0", "label 1", "label 2", "label 3" ve 3 tane daha için bulduğum klipleri işaretledim. Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır.',
            'Şunları kliplerinde bulamadım: "label 0", "label 1", "label 2", "label 3" ve 3 tane daha. Her biri için klibe dokun ya da olmayanları işaretle.',
            'Birden fazla anın için aynı fotoğraf seçilmiş. Şunlar için klibi seç: "label 0", "label 1", "label 2", "label 3" ve 3 tane daha. Klibi olmayanları işaretle.',
            'Şunlar için hangi klipleri kastettiğini anlayamadım: "label 0", "label 1", "label 2", "label 3" ve 3 tane daha. Her biri için klipleri seç ya da hiç klibi olmayanları işaretle.',
            'Videonda şunlar yaklaşık ne zaman görünüyor: "label 0", "label 1", "label 2", "label 3" ve 3 tane daha? Her biri için yaklaşık bir zaman ver ya da hangilerinin olmadığını söyle.',
        ],
    },
    "clip_intent_planner._preview": {
        "en": [
            ["an instruction", "an instruction", "an instruction"],
            ["an instruction", "an instruction", "an instruction"],
            ["label: player names", "label: player names", "label: player names"],
            ["group: by city", "group: by city", "group: by city"],
            ["order: goal first", "order: goal first", "order: goal first"],
            ["include: the beach", "include: the beach", "include: the beach"],
            ["caption: the menu", "caption: the menu", "caption: the menu"],
            ["exclude: blurry ones", "exclude: blurry ones", "exclude: blurry ones"],
            ["hide_part: the ad", "hide_part: the ad", "hide_part: the ad"],
            ["x", "x", "x"],
            ["label: an instruction", "label: an instruction", "label: an instruction"],
        ],
        "tr": [
            ["bir talimat", "bir talimat", "an instruction"],
            ["bir talimat", "bir talimat", "an instruction"],
            ["etiket: player names", "etiket: player names", "label: player names"],
            ["grup: by city", "grup: by city", "group: by city"],
            ["sıra: goal first", "sıra: goal first", "order: goal first"],
            ["dahil etme: the beach", "dahil etme: the beach", "include: the beach"],
            ["yazı: the menu", "yazı: the menu", "caption: the menu"],
            ["hariç tutma: blurry ones", "hariç tutma: blurry ones", "exclude: blurry ones"],
            ["bölüm gizleme: the ad", "bölüm gizleme: the ad", "hide_part: the ad"],
            ["x", "x", "x"],
            ["etiket: bir talimat", "etiket: bir talimat", "label: an instruction"],
        ],
    },
    "clip_intent_planner.salvage_question": {
        "en": [
            "I understood 2 of your clip instructions. Please restate just those so I can add them.",
            'I understood 3 of your clip instructions. I couldn\'t safely verify "label: player names". Please restate just those so I can add them.',
            'I understood 1 of your clip instructions. I couldn\'t safely verify "label: player names"; "the beach"; "group: by city" (and 2 more); I can apply at most 40 clip instructions at a time, so 2 more weren\'t included. Please restate just those so I can add them.',
            "I understood 0 of your clip instructions. I can apply at most 40 clip instructions at a time, so 4 more weren't included. Please restate just those so I can add them.",
            "I understood 5 of your clip instructions. I couldn't safely verify… Please restate just those so I can add them.",
            "I understood 2 of your clip instructions. Please restate just those so I can add them.",
            'I understood 3 of your clip instructions. I couldn\'t safely verify "label: player names". Please restate just those so I can add them.',
            'I understood 1 of your clip instructions. I couldn\'t safely verify "label: player names"; "the beach"; "group: by city" (and 2 more); I can apply at most 40 clip instructions at a time, so 2 more weren\'t included. Please restate just those so I can add them.',
            "I understood 0 of your clip instructions. I can apply at most 40 clip instructions at a time, so 4 more weren't included. Please restate just those so I can add them.",
            "I understood 5 of your clip instructions. I couldn't safely verify… Please restate just those so I can add them.",
            "I understood 2 of your clip instructions. Please restate just those so I can add them.",
            'I understood 3 of your clip instructions. I couldn\'t safely verify "label: player names". Please restate just those so I can add them.',
            'I understood 1 of your clip instructions. I couldn\'t safely verify "label: player names"; "the beach"; "group: by city" (and 2 more); I can apply at most 40 clip instructions at a time, so 2 more weren\'t included. Please restate just those so I can add them.',
            "I understood 0 of your clip instructions. I can apply at most 40 clip instructions at a time, so 4 more weren't included. Please restate just those so I can add them.",
            "I understood 5 of your clip instructions. I couldn't safely verify… Please restate just those so I can add them.",
        ],
        "tr": [
            "Klip talimatlarından 2 tanesini anladım. Sadece bunları yeniden yazar mısın, ekleyeyim.",
            'Klip talimatlarından 3 tanesini anladım. Şunları güvenle doğrulayamadım: "label: player names". Sadece bunları yeniden yazar mısın, ekleyeyim.',
            'Klip talimatlarından 1 tanesini anladım. Şunları güvenle doğrulayamadım: "label: player names"; "the beach"; "group: by city" (ve 2 tane daha); Bir seferde en fazla 40 klip talimatı uygulayabiliyorum, bu yüzden 2 talimat daha eklenmedi. Sadece bunları yeniden yazar mısın, ekleyeyim.',
            "Klip talimatlarından 0 tanesini anladım. Bir seferde en fazla 40 klip talimatı uygulayabiliyorum, bu yüzden 4 talimat daha eklenmedi. Sadece bunları yeniden yazar mısın, ekleyeyim.",
            "Klip talimatlarından 5 tanesini anladım. Şunları güvenle doğrulayamadım… Sadece bunları yeniden yazar mısın, ekleyeyim.",
            "Klip talimatlarından 2 tanesini anladım. Sadece bunları yeniden yazar mısın, ekleyeyim.",
            'Klip talimatlarından 3 tanesini anladım. Şunları güvenle doğrulayamadım: "label: player names". Sadece bunları yeniden yazar mısın, ekleyeyim.',
            'Klip talimatlarından 1 tanesini anladım. Şunları güvenle doğrulayamadım: "label: player names"; "the beach"; "group: by city" (ve 2 tane daha); Bir seferde en fazla 40 klip talimatı uygulayabiliyorum, bu yüzden 2 talimat daha eklenmedi. Sadece bunları yeniden yazar mısın, ekleyeyim.',
            "Klip talimatlarından 0 tanesini anladım. Bir seferde en fazla 40 klip talimatı uygulayabiliyorum, bu yüzden 4 talimat daha eklenmedi. Sadece bunları yeniden yazar mısın, ekleyeyim.",
            "Klip talimatlarından 5 tanesini anladım. Şunları güvenle doğrulayamadım… Sadece bunları yeniden yazar mısın, ekleyeyim.",
            "I understood 2 of your clip instructions. Please restate just those so I can add them.",
            'I understood 3 of your clip instructions. I couldn\'t safely verify "label: player names". Please restate just those so I can add them.',
            'I understood 1 of your clip instructions. I couldn\'t safely verify "label: player names"; "the beach"; "group: by city" (and 2 more); I can apply at most 40 clip instructions at a time, so 2 more weren\'t included. Please restate just those so I can add them.',
            "I understood 0 of your clip instructions. I can apply at most 40 clip instructions at a time, so 4 more weren't included. Please restate just those so I can add them.",
            "I understood 5 of your clip instructions. I couldn't safely verify… Please restate just those so I can add them.",
        ],
    },
    "clip_intent_planner.unexpressed_notice": {
        "en": [
            None,
            None,
            "I can't do \"slow motion on Taha\" with your clips yet, so this draft leaves it out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I'll add it.",
            'I can\'t do "a"; "b" with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
            'I can\'t do "a"; "b"; "c" (and 2 more) with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
            None,
            None,
            "I can't do \"slow motion on Taha\" with your clips yet, so this draft leaves it out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I'll add it.",
            'I can\'t do "a"; "b" with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
            'I can\'t do "a"; "b"; "c" (and 2 more) with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
            None,
            None,
            "I can't do \"slow motion on Taha\" with your clips yet, so this draft leaves it out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I'll add it.",
            'I can\'t do "a"; "b" with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
            'I can\'t do "a"; "b"; "c" (and 2 more) with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
        ],
        "tr": [
            None,
            None,
            '"slow motion on Taha" isteğini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim.',
            '"a"; "b" isteklerini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim.',
            '"a"; "b"; "c" (ve 2 tane daha) isteklerini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim.',
            None,
            None,
            '"slow motion on Taha" isteğini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim.',
            '"a"; "b" isteklerini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim.',
            '"a"; "b"; "c" (ve 2 tane daha) isteklerini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim.',
            None,
            None,
            "I can't do \"slow motion on Taha\" with your clips yet, so this draft leaves it out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I'll add it.",
            'I can\'t do "a"; "b" with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
            'I can\'t do "a"; "b"; "c" (and 2 more) with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I\'ll add it.',
        ],
    },
    "clip_order_sequence.unconfirmed_order_reason": {
        "en": [
            "You didn't say what order these clips should play in. Tell me the order, or say any order is fine.",
            "You didn't say what order these clips should play in. Tell me the order, or say any order is fine.",
            'You didn\'t say what order the clips in "beach" should play in. Tell me the order, or say any order is fine.',
            'You didn\'t say what order the clips in "beach", "city" should play in. Tell me the order, or say any order is fine.',
        ],
        "tr": [
            "Bu kliplerin hangi sırayla oynayacağını söylemedin. Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle.",
            "Bu kliplerin hangi sırayla oynayacağını söylemedin. Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle.",
            '"beach" içindeki kliplerin hangi sırayla oynayacağını söylemedin. Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle.',
            '"beach", "city" içindeki kliplerin hangi sırayla oynayacağını söylemedin. Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle.',
        ],
    },
    "clip_selection.category_label": {
        "en": [
            "the moment the goal is scored",
            "the moment the goal is scored",
            "gol anı ve kutlama sahnesi",
            "the moment the goal is scored",
        ],
        "tr": [
            "the moment the goal is scored",
            "golün atıldığı an, kutlamayla birlikte",
            "gol anı ve kutlama sahnesi",
            "the moment the goal is scored",
        ],
    },
    "creation_editor_actions._applied_summary": {
        "en": [
            "Applied your edit",
            "Moved clip 2 to the start",
            "Klip 2'yi başa taşıdım",
            "Klip 2'yi başa taşıdım and Başlığı büyüttüm",
            "Klip 2'yi başa taşıdım and Made the title bigger",
        ],
        "tr": [
            "Düzenlemeni uyguladım",
            "Düzenlemeni uyguladım",
            "Klip 2'yi başa taşıdım",
            "Klip 2'yi başa taşıdım ve Başlığı büyüttüm",
            "Düzenlemeni uyguladım",
        ],
    },
    "creative_copy_decisions.authorship_question": {
        "en": [
            {
                "version": 1,
                "conflict": "creative_copy",
                "kind": "creative_copy_authorship",
                "creative_target": "opening_title",
                "dependency_digest": "d",
                "allow_free_text": True,
                "options": [
                    {"key": "write_my_own", "label": "I have an idea", "recommended": False},
                    {"key": "generate", "label": "Generate one", "recommended": False},
                    {
                        "key": "cancel",
                        "label": "Skip it",
                        "recommended": False,
                        "aliases": [
                            "no",
                            "none",
                            "nope",
                            "skip",
                            "skip title",
                            "no title",
                            "no title please",
                            "don't add a title",
                            "without title",
                            "without a title",
                            "leave it off",
                            "başlık olmasın",
                            "başlıksız",
                            "continue without a title",
                            "hayır",
                            "hayır teşekkürler",
                            "atla",
                            "geç",
                            "başlık yok",
                            "başlık istemiyorum",
                            "başlık istemem",
                            "başlığa gerek yok",
                            "başlık koyma",
                            "başlık ekleme",
                            "başlıksız devam et",
                            "gerek yok",
                        ],
                    },
                ],
            },
            {
                "version": 1,
                "conflict": "creative_copy",
                "kind": "creative_copy_wording",
                "creative_target": "opening_title",
                "candidate": "Summer in Rome",
                "candidate_digest": "56cd6fa7c9dcbdd432b0e4c4",
                "dependency_digest": "d",
                "allow_free_text": True,
                "options": [
                    {"key": "approve", "label": "Use this wording", "recommended": False},
                    {"key": "revise", "label": "Revise it", "recommended": False},
                    {
                        "key": "cancel",
                        "label": "Skip it",
                        "recommended": False,
                        "aliases": [
                            "no",
                            "none",
                            "nope",
                            "skip",
                            "skip title",
                            "no title",
                            "no title please",
                            "don't add a title",
                            "without title",
                            "without a title",
                            "leave it off",
                            "başlık olmasın",
                            "başlıksız",
                            "continue without a title",
                            "hayır",
                            "hayır teşekkürler",
                            "atla",
                            "geç",
                            "başlık yok",
                            "başlık istemiyorum",
                            "başlık istemem",
                            "başlığa gerek yok",
                            "başlık koyma",
                            "başlık ekleme",
                            "başlıksız devam et",
                            "gerek yok",
                        ],
                    },
                ],
            },
        ],
        "tr": [
            {
                "version": 1,
                "conflict": "creative_copy",
                "kind": "creative_copy_authorship",
                "creative_target": "opening_title",
                "dependency_digest": "d",
                "allow_free_text": True,
                "options": [
                    {
                        "key": "write_my_own",
                        "label": "Bir fikrim var",
                        "recommended": False,
                        "aliases": [
                            "I have an idea",
                            "fikrim var",
                            "kendim yazacağım",
                            "kendim yazarım",
                            "kendim yazayım",
                        ],
                    },
                    {
                        "key": "generate",
                        "label": "Bir tane yaz",
                        "recommended": False,
                        "aliases": [
                            "Generate one",
                            "sen yaz",
                            "sen bir tane yaz",
                            "bir tane öner",
                            "öner",
                            "sen öner",
                        ],
                    },
                    {
                        "key": "cancel",
                        "label": "Atla",
                        "recommended": False,
                        "aliases": [
                            "no",
                            "none",
                            "nope",
                            "skip",
                            "skip title",
                            "no title",
                            "no title please",
                            "don't add a title",
                            "without title",
                            "without a title",
                            "leave it off",
                            "başlık olmasın",
                            "başlıksız",
                            "continue without a title",
                            "hayır",
                            "hayır teşekkürler",
                            "atla",
                            "geç",
                            "başlık yok",
                            "başlık istemiyorum",
                            "başlık istemem",
                            "başlığa gerek yok",
                            "başlık koyma",
                            "başlık ekleme",
                            "başlıksız devam et",
                            "gerek yok",
                            "Skip it",
                        ],
                    },
                ],
            },
            {
                "version": 1,
                "conflict": "creative_copy",
                "kind": "creative_copy_wording",
                "creative_target": "opening_title",
                "candidate": "Summer in Rome",
                "candidate_digest": "56cd6fa7c9dcbdd432b0e4c4",
                "dependency_digest": "d",
                "allow_free_text": True,
                "options": [
                    {
                        "key": "approve",
                        "label": "Bu ifadeyi kullan",
                        "recommended": False,
                        "aliases": [
                            "Use this wording",
                            "bunu kullan",
                            "kullan",
                            "bu ifadeyi kullan",
                        ],
                    },
                    {
                        "key": "revise",
                        "label": "Düzenle",
                        "recommended": False,
                        "aliases": ["Revise it", "değiştir", "düzenle", "başka bir şey"],
                    },
                    {
                        "key": "cancel",
                        "label": "Atla",
                        "recommended": False,
                        "aliases": [
                            "no",
                            "none",
                            "nope",
                            "skip",
                            "skip title",
                            "no title",
                            "no title please",
                            "don't add a title",
                            "without title",
                            "without a title",
                            "leave it off",
                            "başlık olmasın",
                            "başlıksız",
                            "continue without a title",
                            "hayır",
                            "hayır teşekkürler",
                            "atla",
                            "geç",
                            "başlık yok",
                            "başlık istemiyorum",
                            "başlık istemem",
                            "başlığa gerek yok",
                            "başlık koyma",
                            "başlık ekleme",
                            "başlıksız devam et",
                            "gerek yok",
                            "Skip it",
                        ],
                    },
                ],
            },
        ],
    },
    "finished_style_learning._instruction": {
        "en": [
            "Favor Inter when automatically choosing all_text fonts.",
            "Favor Inter when automatically choosing all_text fonts.",
            "Exclude Comic Sans from automatic all_text text selection.",
            "Exclude Comic Sans from automatic all_text text selection.",
            "Place all_text text at top left in future videos.",
            "Place all_text text at top left in future videos.",
            "Place all_text text at middle in future videos.",
            "Place all_text text at middle in future videos.",
            "Place all_text text at far corner in future videos.",
            "Place all_text text at far corner in future videos.",
            "Use #FF0000 for all_text text in future videos.",
            "Use #FF0000 for all_text text in future videos.",
            "All_text outlines off in future videos.",
            "All_text outlines off in future videos.",
            "All_text outlines 2.5px in future videos.",
            "All_text outlines 2.5px in future videos.",
            "All_text shadows on in future videos.",
            "All_text shadows on in future videos.",
            "All_text shadows off in future videos.",
            "All_text shadows off in future videos.",
            "Use automatic all_text fonts and exclude Arial from selection.",
            "Use automatic all_text fonts and exclude Arial from selection.",
            "Favor Inter when automatically choosing title fonts.",
            "Favor Inter when automatically choosing title fonts.",
            "Exclude Comic Sans from automatic title text selection.",
            "Exclude Comic Sans from automatic title text selection.",
            "Place title text at top left in future videos.",
            "Place title text at top left in future videos.",
            "Place title text at middle in future videos.",
            "Place title text at middle in future videos.",
            "Place title text at far corner in future videos.",
            "Place title text at far corner in future videos.",
            "Use #FF0000 for title text in future videos.",
            "Use #FF0000 for title text in future videos.",
            "Title outlines off in future videos.",
            "Title outlines off in future videos.",
            "Title outlines 2.5px in future videos.",
            "Title outlines 2.5px in future videos.",
            "Title shadows on in future videos.",
            "Title shadows on in future videos.",
            "Title shadows off in future videos.",
            "Title shadows off in future videos.",
            "Use automatic title fonts and exclude Arial from selection.",
            "Use automatic title fonts and exclude Arial from selection.",
            "Favor Inter when automatically choosing label fonts.",
            "Favor Inter when automatically choosing label fonts.",
            "Exclude Comic Sans from automatic label text selection.",
            "Exclude Comic Sans from automatic label text selection.",
            "Place label text at top left in future videos.",
            "Place label text at top left in future videos.",
            "Place label text at middle in future videos.",
            "Place label text at middle in future videos.",
            "Place label text at far corner in future videos.",
            "Place label text at far corner in future videos.",
            "Use #FF0000 for label text in future videos.",
            "Use #FF0000 for label text in future videos.",
            "Label outlines off in future videos.",
            "Label outlines off in future videos.",
            "Label outlines 2.5px in future videos.",
            "Label outlines 2.5px in future videos.",
            "Label shadows on in future videos.",
            "Label shadows on in future videos.",
            "Label shadows off in future videos.",
            "Label shadows off in future videos.",
            "Use automatic label fonts and exclude Arial from selection.",
            "Use automatic label fonts and exclude Arial from selection.",
            "Favor Inter when automatically choosing pinned fonts.",
            "Favor Inter when automatically choosing pinned fonts.",
            "Exclude Comic Sans from automatic pinned text selection.",
            "Exclude Comic Sans from automatic pinned text selection.",
            "Place pinned text at top left in future videos.",
            "Place pinned text at top left in future videos.",
            "Place pinned text at middle in future videos.",
            "Place pinned text at middle in future videos.",
            "Place pinned text at far corner in future videos.",
            "Place pinned text at far corner in future videos.",
            "Use #FF0000 for pinned text in future videos.",
            "Use #FF0000 for pinned text in future videos.",
            "Pinned outlines off in future videos.",
            "Pinned outlines off in future videos.",
            "Pinned outlines 2.5px in future videos.",
            "Pinned outlines 2.5px in future videos.",
            "Pinned shadows on in future videos.",
            "Pinned shadows on in future videos.",
            "Pinned shadows off in future videos.",
            "Pinned shadows off in future videos.",
            "Use automatic pinned fonts and exclude Arial from selection.",
            "Use automatic pinned fonts and exclude Arial from selection.",
            "Favor Inter when automatically choosing caption fonts.",
            "Favor Inter when automatically choosing caption fonts.",
            "Exclude Comic Sans from automatic caption text selection.",
            "Exclude Comic Sans from automatic caption text selection.",
            "Place caption text at top left in future videos.",
            "Place caption text at top left in future videos.",
            "Place caption text at middle in future videos.",
            "Place caption text at middle in future videos.",
            "Place caption text at far corner in future videos.",
            "Place caption text at far corner in future videos.",
            "Use #FF0000 for caption text in future videos.",
            "Use #FF0000 for caption text in future videos.",
            "Caption outlines off in future videos.",
            "Caption outlines off in future videos.",
            "Caption outlines 2.5px in future videos.",
            "Caption outlines 2.5px in future videos.",
            "Caption shadows on in future videos.",
            "Caption shadows on in future videos.",
            "Caption shadows off in future videos.",
            "Caption shadows off in future videos.",
            "Use automatic caption fonts and exclude Arial from selection.",
            "Use automatic caption fonts and exclude Arial from selection.",
            "Favor Inter when automatically choosing lyric fonts.",
            "Favor Inter when automatically choosing lyric fonts.",
            "Exclude Comic Sans from automatic lyric text selection.",
            "Exclude Comic Sans from automatic lyric text selection.",
            "Place lyric text at top left in future videos.",
            "Place lyric text at top left in future videos.",
            "Place lyric text at middle in future videos.",
            "Place lyric text at middle in future videos.",
            "Place lyric text at far corner in future videos.",
            "Place lyric text at far corner in future videos.",
            "Use #FF0000 for lyric text in future videos.",
            "Use #FF0000 for lyric text in future videos.",
            "Lyric outlines off in future videos.",
            "Lyric outlines off in future videos.",
            "Lyric outlines 2.5px in future videos.",
            "Lyric outlines 2.5px in future videos.",
            "Lyric shadows on in future videos.",
            "Lyric shadows on in future videos.",
            "Lyric shadows off in future videos.",
            "Lyric shadows off in future videos.",
            "Use automatic lyric fonts and exclude Arial from selection.",
            "Use automatic lyric fonts and exclude Arial from selection.",
            "Favor Inter when automatically choosing sticker fonts.",
            "Favor Inter when automatically choosing sticker fonts.",
            "Exclude Comic Sans from automatic sticker text selection.",
            "Exclude Comic Sans from automatic sticker text selection.",
            "Place sticker text at top left in future videos.",
            "Place sticker text at top left in future videos.",
            "Place sticker text at middle in future videos.",
            "Place sticker text at middle in future videos.",
            "Place sticker text at far corner in future videos.",
            "Place sticker text at far corner in future videos.",
            "Use #FF0000 for sticker text in future videos.",
            "Use #FF0000 for sticker text in future videos.",
            "Sticker outlines off in future videos.",
            "Sticker outlines off in future videos.",
            "Sticker outlines 2.5px in future videos.",
            "Sticker outlines 2.5px in future videos.",
            "Sticker shadows on in future videos.",
            "Sticker shadows on in future videos.",
            "Sticker shadows off in future videos.",
            "Sticker shadows off in future videos.",
            "Use automatic sticker fonts and exclude Arial from selection.",
            "Use automatic sticker fonts and exclude Arial from selection.",
        ],
        "tr": [
            "Tüm yazılar için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing all_text fonts.",
            "Tüm yazılar için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic all_text text selection.",
            "Gelecek videolarda tüm yazılar sol üst kısımda dursun.",
            "Place all_text text at top left in future videos.",
            "Gelecek videolarda tüm yazılar orta kısımda dursun.",
            "Place all_text text at middle in future videos.",
            "Gelecek videolarda tüm yazılar far corner kısımda dursun.",
            "Place all_text text at far corner in future videos.",
            "Gelecek videolarda tüm yazılar için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for all_text text in future videos.",
            "Gelecek videolarda tüm yazılar için kenar çizgisi kapalı olsun.",
            "All_text outlines off in future videos.",
            "Gelecek videolarda tüm yazılar için kenar çizgisi 2.5px olsun.",
            "All_text outlines 2.5px in future videos.",
            "Gelecek videolarda tüm yazılar için gölge açık olsun.",
            "All_text shadows on in future videos.",
            "Gelecek videolarda tüm yazılar için gölge kapalı olsun.",
            "All_text shadows off in future videos.",
            "Use automatic all_text fonts and exclude Arial from selection.",
            "Tüm yazılar için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
            "Başlık için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing title fonts.",
            "Başlık için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic title text selection.",
            "Gelecek videolarda başlık sol üst kısımda dursun.",
            "Place title text at top left in future videos.",
            "Gelecek videolarda başlık orta kısımda dursun.",
            "Place title text at middle in future videos.",
            "Gelecek videolarda başlık far corner kısımda dursun.",
            "Place title text at far corner in future videos.",
            "Gelecek videolarda başlık için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for title text in future videos.",
            "Gelecek videolarda başlık için kenar çizgisi kapalı olsun.",
            "Title outlines off in future videos.",
            "Gelecek videolarda başlık için kenar çizgisi 2.5px olsun.",
            "Title outlines 2.5px in future videos.",
            "Gelecek videolarda başlık için gölge açık olsun.",
            "Title shadows on in future videos.",
            "Gelecek videolarda başlık için gölge kapalı olsun.",
            "Title shadows off in future videos.",
            "Use automatic title fonts and exclude Arial from selection.",
            "Başlık için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
            "Etiket için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing label fonts.",
            "Etiket için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic label text selection.",
            "Gelecek videolarda etiket sol üst kısımda dursun.",
            "Place label text at top left in future videos.",
            "Gelecek videolarda etiket orta kısımda dursun.",
            "Place label text at middle in future videos.",
            "Gelecek videolarda etiket far corner kısımda dursun.",
            "Place label text at far corner in future videos.",
            "Gelecek videolarda etiket için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for label text in future videos.",
            "Gelecek videolarda etiket için kenar çizgisi kapalı olsun.",
            "Label outlines off in future videos.",
            "Gelecek videolarda etiket için kenar çizgisi 2.5px olsun.",
            "Label outlines 2.5px in future videos.",
            "Gelecek videolarda etiket için gölge açık olsun.",
            "Label shadows on in future videos.",
            "Gelecek videolarda etiket için gölge kapalı olsun.",
            "Label shadows off in future videos.",
            "Use automatic label fonts and exclude Arial from selection.",
            "Etiket için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
            "Sabit yazı için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing pinned fonts.",
            "Sabit yazı için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic pinned text selection.",
            "Gelecek videolarda sabit yazı sol üst kısımda dursun.",
            "Place pinned text at top left in future videos.",
            "Gelecek videolarda sabit yazı orta kısımda dursun.",
            "Place pinned text at middle in future videos.",
            "Gelecek videolarda sabit yazı far corner kısımda dursun.",
            "Place pinned text at far corner in future videos.",
            "Gelecek videolarda sabit yazı için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for pinned text in future videos.",
            "Gelecek videolarda sabit yazı için kenar çizgisi kapalı olsun.",
            "Pinned outlines off in future videos.",
            "Gelecek videolarda sabit yazı için kenar çizgisi 2.5px olsun.",
            "Pinned outlines 2.5px in future videos.",
            "Gelecek videolarda sabit yazı için gölge açık olsun.",
            "Pinned shadows on in future videos.",
            "Gelecek videolarda sabit yazı için gölge kapalı olsun.",
            "Pinned shadows off in future videos.",
            "Use automatic pinned fonts and exclude Arial from selection.",
            "Sabit yazı için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
            "Altyazı için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing caption fonts.",
            "Altyazı için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic caption text selection.",
            "Gelecek videolarda altyazı sol üst kısımda dursun.",
            "Place caption text at top left in future videos.",
            "Gelecek videolarda altyazı orta kısımda dursun.",
            "Place caption text at middle in future videos.",
            "Gelecek videolarda altyazı far corner kısımda dursun.",
            "Place caption text at far corner in future videos.",
            "Gelecek videolarda altyazı için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for caption text in future videos.",
            "Gelecek videolarda altyazı için kenar çizgisi kapalı olsun.",
            "Caption outlines off in future videos.",
            "Gelecek videolarda altyazı için kenar çizgisi 2.5px olsun.",
            "Caption outlines 2.5px in future videos.",
            "Gelecek videolarda altyazı için gölge açık olsun.",
            "Caption shadows on in future videos.",
            "Gelecek videolarda altyazı için gölge kapalı olsun.",
            "Caption shadows off in future videos.",
            "Use automatic caption fonts and exclude Arial from selection.",
            "Altyazı için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
            "Şarkı sözü için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing lyric fonts.",
            "Şarkı sözü için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic lyric text selection.",
            "Gelecek videolarda şarkı sözü sol üst kısımda dursun.",
            "Place lyric text at top left in future videos.",
            "Gelecek videolarda şarkı sözü orta kısımda dursun.",
            "Place lyric text at middle in future videos.",
            "Gelecek videolarda şarkı sözü far corner kısımda dursun.",
            "Place lyric text at far corner in future videos.",
            "Gelecek videolarda şarkı sözü için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for lyric text in future videos.",
            "Gelecek videolarda şarkı sözü için kenar çizgisi kapalı olsun.",
            "Lyric outlines off in future videos.",
            "Gelecek videolarda şarkı sözü için kenar çizgisi 2.5px olsun.",
            "Lyric outlines 2.5px in future videos.",
            "Gelecek videolarda şarkı sözü için gölge açık olsun.",
            "Lyric shadows on in future videos.",
            "Gelecek videolarda şarkı sözü için gölge kapalı olsun.",
            "Lyric shadows off in future videos.",
            "Use automatic lyric fonts and exclude Arial from selection.",
            "Şarkı sözü için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
            "Sticker için yazı tipi otomatik seçilirken Inter tercih edilsin.",
            "Favor Inter when automatically choosing sticker fonts.",
            "Sticker için otomatik yazı tipi seçiminde Comic Sans kullanılmasın.",
            "Exclude Comic Sans from automatic sticker text selection.",
            "Gelecek videolarda sticker sol üst kısımda dursun.",
            "Place sticker text at top left in future videos.",
            "Gelecek videolarda sticker orta kısımda dursun.",
            "Place sticker text at middle in future videos.",
            "Gelecek videolarda sticker far corner kısımda dursun.",
            "Place sticker text at far corner in future videos.",
            "Gelecek videolarda sticker için #FF0000 rengi kullanılsın.",
            "Use #FF0000 for sticker text in future videos.",
            "Gelecek videolarda sticker için kenar çizgisi kapalı olsun.",
            "Sticker outlines off in future videos.",
            "Gelecek videolarda sticker için kenar çizgisi 2.5px olsun.",
            "Sticker outlines 2.5px in future videos.",
            "Gelecek videolarda sticker için gölge açık olsun.",
            "Sticker shadows on in future videos.",
            "Gelecek videolarda sticker için gölge kapalı olsun.",
            "Sticker shadows off in future videos.",
            "Use automatic sticker fonts and exclude Arial from selection.",
            "Sticker için yazı tipi otomatik seçilsin, Arial seçimden çıkarılsın.",
        ],
    },
    "kria_runtime._job_failure_copy": {
        "en": [
            "I couldn't prepare the text while keeping your approved wording. Your draft and footage are saved. Try generating this edit again.",
            "I couldn't line your clips up with your song. Film with the song playing out loud, or sing along clearly, then try again.",
            "Your video has no sound, so there's nothing to caption or cut. Record it again and check that nothing is covering the microphone.",
            "This edit uses something your iPhone can't render yet. Start a new edit with a different clip or format.",
            "This edit needs an iPhone rendering feature that's switched off right now. Your edit and footage are saved. Try again later, or ask for a different style.",
            "Kria's AI service has reached its usage limit for now, so your video wasn't made. Your edit and footage are saved. Trying again right away won't help; try again later.",
            "Writing the on-screen text for this many clips took longer than our AI allows, so your video wasn't made. Your edit and footage are saved. Try again; if it happens again, use fewer clips or ask for less text.",
            "Our AI took too long to plan this edit, so your video wasn't made. Your edit and footage are saved. Try again in a moment.",
            "Something broke while planning this edit, so your video wasn't made. Your edit and footage are saved. Try again, or ask for a change.",
            "Something you approved changed before this edit could render, so I didn't make it. Ask Kria to prepare the edit again.",
            "This edit was prepared by a newer version of Kria than the one rendering it. Ask Kria to prepare it again.",
            "Only preview copies of your clips are uploaded (the originals stay on your iPhone), so this edit has to be made on your iPhone. Your edit is saved. Open the project on the iPhone that has the clips and ask Kria to make it again.",
            "Processing timed out — your clips are heavy (likely 4K/HDR). Try fewer or shorter clips.",
            "Cleaning up the audio didn't finish. Try again, or turn off speech cleanup.",
            "No matching music was found for this edit. Try again, or pick a track yourself.",
            "Matching your footage to music didn't finish. Try again.",
            "Automatic music matching is turned off right now. Try again later.",
            "This project can only be rendered on our servers, and server rendering is switched off, so this version wasn't made. Start a new edit on your iPhone with these clips to make it there.",
            "The render couldn't be handed to the queue. Give it another go.",
            "Importing from Drive didn't finish. Try again.",
            "Importing from Drive didn't finish. Try again.",
            "Saving your upload didn't finish. Try again.",
            "This edit needs a renderer that's temporarily turned off. Try again later.",
            "This render was cancelled.",
            "Rendering ran out of memory — your clips are heavy. Try fewer or shorter clips.",
            "Something went wrong while assembling your video. Try again.",
            "Analyzing your footage didn't finish. Try again.",
            "Analyzing your footage didn't finish. Try again.",
            "Writing your video's captions didn't finish. Try again.",
            "Your finished video couldn't be saved. Try again.",
            "One of your clips couldn't be downloaded. Reattach it and try again.",
            "One of your clips couldn't be used (it may be corrupted or unsupported). Try a different clip.",
            "This template has a configuration problem. Try a different one.",
            "This template is missing assets it needs. Try a different one.",
            "This export is no longer available.",
            "Something changed while exporting. Try again.",
            None,
            None,
            "Something went wrong and this render didn't finish. Your direction and footage are still saved — try again.",
            "The creator-facing detail.",
            "Your video has no sound, so there's nothing to caption or cut. Record it again and check that nothing is covering the microphone.",
            "The creator-facing detail.",
            "I couldn't line your clips up with your song. Film with the song playing out loud, or sing along clearly, then try again.",
        ],
        "tr": [
            "Onayladığın yazıyı koruyarak metni hazırlayamadım. Taslağın ve çekimlerin kayıtlı. Bu videoyu yeniden oluşturmayı dene.",
            "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya da sözleri net söyleyerek eşlik et, sonra tekrar dene.",
            "Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey bulamadım. Tekrar çek ve mikrofonun kapalı olmadığından emin ol.",
            "Bu düzenleme iPhone'unun henüz oluşturamadığı bir şey içeriyor. Farklı bir klip ya da formatla yeni bir düzenleme başlat.",
            "Bu düzenleme için gereken bir iPhone oluşturma özelliği şu anda kapalı. Düzenlemen ve çekimlerin kayıtlı. Daha sonra tekrar dene ya da farklı bir stil iste.",
            "Kria'nın yapay zekâ hizmeti şimdilik kullanım sınırına ulaştı, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Hemen tekrar denemek işe yaramaz; daha sonra tekrar dene.",
            "Bu kadar klip için ekrandaki yazıları hazırlamak yapay zekânın izin verdiği süreden uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Tekrar dene; yine olursa daha az klip kullan ya da daha az yazı iste.",
            "Yapay zekâ bu düzenlemeyi planlarken çok uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Birazdan tekrar dene.",
            "Bu düzenleme planlanırken bir şey bozuldu, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Tekrar dene ya da bir değişiklik iste.",
            "Onayladığın bir şey bu düzenleme oluşturulmadan önce değişti, bu yüzden videoyu yapmadım. Kria'dan düzenlemeyi yeniden hazırlamasını iste.",
            "Bu düzenleme, onu oluşturan sürümden daha yeni bir Kria sürümüyle hazırlandı. Kria'dan yeniden hazırlamasını iste.",
            "Kliplerinin yalnızca önizleme kopyaları yüklü (orijinaller iPhone'unda duruyor), bu yüzden bu düzenleme iPhone'unda yapılmalı. Düzenlemen kayıtlı. Projeyi klipler olan iPhone'da aç ve Kria'dan videoyu yeniden yapmasını iste.",
            "İşlem zaman aşımına uğradı; kliplerin çok ağır (muhtemelen 4K/HDR). Daha az ya da daha kısa klip dene.",
            "Sesi temizleme tamamlanmadı. Tekrar dene ya da konuşma temizlemeyi kapat.",
            "Bu düzenleme için uygun müzik bulunamadı. Tekrar dene ya da müziği kendin seç.",
            "Çekimlerini müzikle eşleştirme tamamlanmadı. Tekrar dene.",
            "Otomatik müzik eşleştirme şu anda kapalı. Daha sonra tekrar dene.",
            "Bu proje yalnızca sunucularımızda oluşturulabiliyor ve sunucuda oluşturma şu anda kapalı, bu yüzden bu sürüm yapılmadı. Bu kliplerle iPhone'unda yeni bir düzenleme başlatarak videoyu orada yap.",
            "Video sıraya iletilemedi. Bir kez daha dene.",
            "Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
            "Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
            "Yüklemen kaydedilemedi. Tekrar dene.",
            "Bu düzenleme şu anda geçici olarak kapalı bir oluşturucuya ihtiyaç duyuyor. Daha sonra tekrar dene.",
            "Bu video iptal edildi.",
            "Video hazırlanırken bellek yetmedi; kliplerin çok ağır. Daha az ya da daha kısa klip dene.",
            "Videon birleştirilirken bir şeyler ters gitti. Tekrar dene.",
            "Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
            "Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
            "Videonun paylaşım metnini yazma tamamlanmadı. Tekrar dene.",
            "Hazır videon kaydedilemedi. Tekrar dene.",
            "Kliplerinden biri indirilemedi. Yeniden ekle ve tekrar dene.",
            "Kliplerinden biri kullanılamadı (bozuk ya da desteklenmiyor olabilir). Farklı bir klip dene.",
            "Bu şablonda bir yapılandırma sorunu var. Farklı bir tane dene.",
            "Bu şablonun ihtiyaç duyduğu dosyalar eksik. Farklı bir tane dene.",
            "Bu dışa aktarma artık kullanılamıyor.",
            "Dışa aktarma sırasında bir şey değişti. Tekrar dene.",
            None,
            None,
            "Bir şeyler ters gitti ve bu video tamamlanmadı. Yönün ve çekimlerin hâlâ kayıtlı; tekrar dene.",
            "Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey bulamadım. Tekrar çek ve mikrofonun kapalı olmadığından emin ol.",
            "Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey bulamadım. Tekrar çek ve mikrofonun kapalı olmadığından emin ol.",
            "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya da sözleri net söyleyerek eşlik et, sonra tekrar dene.",
            "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya da sözleri net söyleyerek eşlik et, sonra tekrar dene.",
        ],
    },
    "kria_runtime._localized_block_recovery": {
        "en": [
            "I couldn't match the narration to the actions reliably. Your draft is saved. Should I try again or use a simpler clip sequence?",
            "  I couldn't match the narration to the actions reliably. Your draft is saved. Should I try again or use a simpler clip sequence?\n",
            "Some other message.",
        ],
        "tr": [
            "Seslendirmeyi kliplerle güvenilir bir şekilde eşleştiremedim. Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa daha basit bir klip sırası mı kullanayım?",
            "Seslendirmeyi kliplerle güvenilir bir şekilde eşleştiremedim. Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa daha basit bir klip sırası mı kullanayım?",
            "Some other message.",
        ],
    },
    "kria_runtime._phone_gate_refusal_copy": {
        "en": [
            "This project cannot render entirely on this iPhone. Its existing video is unchanged. Tell me what you'd like to change and I'll try a different approach.",
            "This project's footage lives on your iPhone and can't be rendered from this account right now. Tell me what you'd like to change and I'll try a different approach.",
            "This edit plan needs approving again before it can render on your iPhone. Tell me what you'd like to change and I'll try a different approach.",
            "This kind of video can't render on your iPhone right now. Choose a format that renders on this iPhone (Montage, or Talking to camera / Narrated where available). No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "Talking-to-camera videos render on your iPhone with exactly one clip. Remove the extra clips (or add one) and try again. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "This clip is too long for talking-to-camera on your iPhone. Use a shorter clip (under 5 minutes) and try again. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "Narrating across several clips isn't on iPhone yet. Record a voiceover, or keep one clip for talking-to-camera. Tell me what you'd like to change and I'll try a different approach.",
            "Your own song can only be used in videos made on your iPhone right now. Ask for this edit without your song. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "That kind of edit isn't available for iPhone renders yet. Tell me what you'd like to change and I'll try a different approach.",
        ],
        "tr": [
            "Bu proje tamamen bu iPhone'da oluşturulamıyor. Mevcut videon değişmedi. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu projenin çekimleri iPhone'unda duruyor ve şu anda bu hesaptan videoya dönüştürülemiyor. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu düzenleme planının iPhone'unda videoya dönüşebilmesi için yeniden onaylanması gerekiyor. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu tür bir video şu anda iPhone'unda oluşturulamıyor. iPhone'da oluşturulabilen bir format seç (Montaj, ya da uygun olduğunda Kameraya konuşma / Seslendirmeli). Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Kameraya konuşma videoları iPhone'unda tam olarak tek klipten oluşturulur. Fazla klipleri kaldır (ya da bir klip ekle) ve tekrar dene. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu klip, iPhone'unda kameraya konuşma videosu için çok uzun. 5 dakikadan kısa bir klip kullan ve tekrar dene. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Birden fazla klip üzerinde anlatım iPhone'da henüz yok. Bir seslendirme kaydet ya da kameraya konuşma için tek klip kullan. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Kendi şarkın şu anda yalnızca iPhone'unda hazırlanan videolarda kullanılabiliyor. Bu düzenlemeyi şarkın olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu tür bir düzenleme iPhone'da henüz kullanılamıyor. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
        ],
    },
    "kria_runtime._phone_gate_refusal_copy multi-clip": {
        "en": [
            "This project cannot render entirely on this iPhone. Its existing video is unchanged. Tell me what you'd like to change and I'll try a different approach.",
            "This project's footage lives on your iPhone and can't be rendered from this account right now. Tell me what you'd like to change and I'll try a different approach.",
            "This edit plan needs approving again before it can render on your iPhone. Tell me what you'd like to change and I'll try a different approach.",
            "This kind of video can't render on your iPhone right now. Choose a format that renders on this iPhone (Montage, or Talking to camera / Narrated where available). No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "Talking-to-camera videos render on your iPhone with up to 10 clips. Remove the extra clips and try again. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "This clip is too long for talking-to-camera on your iPhone. Use a shorter clip (under 5 minutes) and try again. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "Narrating across several clips isn't on iPhone yet. Record a voiceover, or keep one clip for talking-to-camera. Tell me what you'd like to change and I'll try a different approach.",
            "Your own song can only be used in videos made on your iPhone right now. Ask for this edit without your song. No fallback edit was rendered. Tell me what you'd like to change and I'll try a different approach.",
            "That kind of edit isn't available for iPhone renders yet. Tell me what you'd like to change and I'll try a different approach.",
        ],
        "tr": [
            "Bu proje tamamen bu iPhone'da oluşturulamıyor. Mevcut videon değişmedi. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu projenin çekimleri iPhone'unda duruyor ve şu anda bu hesaptan videoya dönüştürülemiyor. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu düzenleme planının iPhone'unda videoya dönüşebilmesi için yeniden onaylanması gerekiyor. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu tür bir video şu anda iPhone'unda oluşturulamıyor. iPhone'da oluşturulabilen bir format seç (Montaj, ya da uygun olduğunda Kameraya konuşma / Seslendirmeli). Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Kameraya konuşma videoları iPhone'unda en fazla 10 klipten oluşturulur. Fazla klipleri kaldır ve tekrar dene. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu klip, iPhone'unda kameraya konuşma videosu için çok uzun. 5 dakikadan kısa bir klip kullan ve tekrar dene. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Birden fazla klip üzerinde anlatım iPhone'da henüz yok. Bir seslendirme kaydet ya da kameraya konuşma için tek klip kullan. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Kendi şarkın şu anda yalnızca iPhone'unda hazırlanan videolarda kullanılabiliyor. Bu düzenlemeyi şarkın olmadan iste. Yedek bir düzenleme oluşturulmadı. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
            "Bu tür bir düzenleme iPhone'da henüz kullanılamıyor. Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
        ],
    },
    "kria_runtime._worker_copy": {
        "en": [
            "Tell me which option you want and I'll continue.",
            "I can rebuild the edit so it shows this, or you can relax the requirement.",
            "Ask for it to be rendered on your iPhone, where I can check it, or tell me to drop that requirement.",
            "I can rebuild the edit with that soundtrack playing once, or you can pick a different one.",
            "Choose either your recorded voice or the camera audio for this edit.",
            "I can rebuild the edit without that soundtrack, or you can ask for music.",
            "Your confirmed edit lengths conflict, so I can't render it safely.",
            "Tell me which length you want.",
            "This edit belongs to a different approved revision.",
            "Ask me to make the edit again from your latest request.",
            "This edit needs your confirmed recorded voice.",
            "Record or upload your voice, or tell me to use music instead.",
            "This renderer can't combine the confirmed soundtracks.",
            "Choose one soundtrack: the camera audio, your voice, or your song.",
            "I couldn't build the confirmed camera-audio edit.",
            "Pick a different clip to carry the speech, or ask for a plain montage.",
            "I can't verify this ordering rule from the approved media.",
            "I need to know which clips are in the edit to verify their order.",
            "I need capture times for every selected clip to verify chronological order.",
            "I couldn't tell which clips you want first or last, so I can't confirm their order.",
            "This edit is missing confirmed on-screen text.",
            "This edit couldn't keep confirmed text on screen long enough.",
            "This edit put confirmed text in the wrong place.",
            "This edit keeps confirmed text on screen after the picture has ended.",
            "I can render it again, or you can relax the requirement.",
            "Ask for a spoken-excerpt montage on your iPhone, which can keep only that camera audio, or tell me to drop that requirement.",
            "Ask for it to be rendered on your iPhone, which renders clips in the order you confirmed, or tell me to drop that requirement.",
            "Ask for a spoken-excerpt montage on your iPhone, which keeps or mutes camera audio as you confirmed, or tell me to drop that requirement.",
            "Ask for a different format.",
            "Ask for a montage with your voice, or a different format.",
            "Record your voice for this edit, or ask for a different format.",
            "Ask for a montage, a subtitled clip, or a narrated edit.",
            "This plan has a guided story but the edit format is audio-led. Pick one: the guided story, or the spoken/narrated edit.",
            "Pick a clip where you talk to use as the voice, or ask for a plain montage.",
            "I can rebuild the edit from your confirmed plan, or you can ask for a different edit.",
            "I couldn't find clear speech in your clips to build this edit.",
            "Pick a clip where you talk, record a voiceover, or ask for a montage.",
            "The clip with your speech is too short to cut other footage into.",
            "Use a longer clip of you talking, or ask for a montage.",
            "I couldn't read the speech in your clip.",
            "Try a different clip of you talking, or ask for a montage.",
            "Your video has no sound, so there's nothing to caption or cut.",
            "Record it again and check that nothing is covering the microphone.",
            "Ask me to make the change as a new edit instead.",
            "Choose another song or render with footage audio.",
            "  Tell me which option you want and I'll continue.  ",
            "  I can rebuild the edit so it shows this, or you can relax the requirement.  ",
            "  Ask for it to be rendered on your iPhone, where I can check it, or tell me to drop that requirement.  ",
            None,
            None,
            None,
            "An English sentence nobody translated.",
            "Bu cümle zaten Türkçe yazılmış, olduğu gibi kalsın.",
        ],
        "tr": [
            "Hangi seçeneği istediğini söyle, devam edeyim.",
            "Düzenlemeyi bunu gösterecek şekilde yeniden kurabilirim ya da bu isteği esnetebilirsin.",
            "Kontrol edebilmem için iPhone'unda oluşturulmasını iste ya da bu isteği bırakmamı söyle.",
            "Düzenlemeyi o ses bir kez çalacak şekilde yeniden kurabilirim ya da başka bir ses seçebilirsin.",
            "Bu düzenleme için ya kaydettiğin sesi ya da kamera sesini seç.",
            "Düzenlemeyi o ses olmadan yeniden kurabilirim ya da müzik isteyebilirsin.",
            "Onayladığın uzunluklar birbiriyle çelişiyor, o yüzden videoyu güvenle oluşturamıyorum.",
            "Hangi uzunluğu istediğini söyle.",
            "Bu düzenleme onaylanmış başka bir sürüme ait.",
            "Düzenlemeyi son isteğine göre yeniden yapmamı iste.",
            "Bu düzenleme için onayladığın ses kaydın gerekiyor.",
            "Sesini kaydet ya da yükle; istersen bunun yerine müzik kullanmamı söyle.",
            "Bu video hazırlama yolu onayladığın sesleri birleştiremiyor.",
            "Tek bir ses seç: kamera sesi, kendi sesin ya da şarkın.",
            "Onayladığın kamera sesli düzenlemeyi kuramadım.",
            "Konuşmayı taşıyacak başka bir klip seç ya da düz bir montaj iste.",
            "Bu sıralama kuralını onaylanan çekimlerden doğrulayamıyorum.",
            "Sıralarını doğrulamak için düzenlemede hangi kliplerin olduğunu bilmem gerekiyor.",
            "Çekim sırasını doğrulamak için seçilen her klibin çekim zamanını bilmem gerekiyor.",
            "Hangi klipleri başa ya da sona istediğini anlayamadım, o yüzden sıralarını doğrulayamıyorum.",
            "Bu düzenlemede onayladığın ekran yazısı eksik.",
            "Bu düzenleme onayladığın yazıyı ekranda yeterince uzun tutamadı.",
            "Bu düzenleme onayladığın yazıyı yanlış yere koydu.",
            "Bu düzenleme onayladığın yazıyı görüntü bittikten sonra da ekranda tutuyor.",
            "Videoyu yeniden oluşturabilirim ya da bu isteği esnetebilirsin.",
            "iPhone'unda yalnızca o kamera sesini koruyabilen, konuşma kesitli bir montaj iste ya da bu isteği bırakmamı söyle.",
            "Klipleri onayladığın sırayla oluşturan iPhone'unda oluşturulmasını iste ya da bu isteği bırakmamı söyle.",
            "iPhone'unda kamera sesini onayladığın gibi koruyan ya da kısan, konuşma kesitli bir montaj iste ya da bu isteği bırakmamı söyle.",
            "Farklı bir format iste.",
            "Sesinle bir montaj ya da farklı bir format iste.",
            "Bu düzenleme için sesini kaydet ya da farklı bir format iste.",
            "Bir montaj, altyazılı bir klip ya da seslendirmeli bir düzenleme iste.",
            "Bu planda rehberli bir hikâye var ama düzenleme formatı sese dayalı. Birini seç: rehberli hikâye ya da konuşmalı/seslendirmeli düzenleme.",
            "Ses olarak kullanmak için konuştuğun bir klip seç ya da düz bir montaj iste.",
            "Düzenlemeyi onayladığın plana göre yeniden kurabilirim ya da farklı bir düzenleme isteyebilirsin.",
            "Bu düzenlemeyi kurmak için kliplerinde net bir konuşma bulamadım.",
            "Konuştuğun bir klip seç, bir seslendirme kaydet ya da bir montaj iste.",
            "Konuştuğun klip, araya başka çekimler koymak için çok kısa.",
            "Konuştuğun daha uzun bir klip kullan ya da bir montaj iste.",
            "Klibindeki konuşmayı okuyamadım.",
            "Konuştuğun başka bir klibi dene ya da bir montaj iste.",
            "Videonda hiç ses yok, o yüzden altyazı eklenecek ya da kesilecek bir şey yok.",
            "Yeniden kaydet ve mikrofonun üstünü hiçbir şeyin kapatmadığından emin ol.",
            "Bunun yerine değişikliği yeni bir düzenleme olarak yapmamı iste.",
            "Başka bir şarkı seç ya da videoyu çekimlerin kendi sesiyle oluştur.",
            "Hangi seçeneği istediğini söyle, devam edeyim.",
            "Düzenlemeyi bunu gösterecek şekilde yeniden kurabilirim ya da bu isteği esnetebilirsin.",
            "Kontrol edebilmem için iPhone'unda oluşturulmasını iste ya da bu isteği bırakmamı söyle.",
            None,
            None,
            None,
            None,
            "Bu cümle zaten Türkçe yazılmış, olduğu gibi kalsın.",
        ],
    },
    "named_moments.model_echo": {
        "en": [
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
        ],
        "tr": [
            "Anlarımın her birinin nerede olduğunu gösterdim.",
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
        ],
    },
    "plan_review.scope_phrase": {
        "en": [
            "",
            "title",
            "clips",
            "captions",
            "music",
            "sound effects",
            "overlays",
            "look",
            "post caption",
            "title and clips",
            "title, clips, captions, music, sound effects, overlays, look and post caption",
            "unknown_section and music",
        ],
        "tr": [
            "",
            "başlık",
            "klipler",
            "altyazılar",
            "müzik",
            "ses efektleri",
            "katmanlar",
            "görünüm",
            "paylaşım metni",
            "başlık ve klipler",
            "başlık, klipler, altyazılar, müzik, ses efektleri, katmanlar, görünüm ve paylaşım metni",
            "unknown_section ve müzik",
        ],
    },
    "song_order.model_text": {
        "en": [
            "I confirmed where my clips go on the song timeline.",
            "I confirmed where my clips go on the song timeline.",
            "I confirmed where my clips go on the song timeline.",
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
            "I showed you where each of my moments is.",
            "I told you which part of the song I filmed.",
            "I told you which part of the song I filmed.",
            "I told you which part of the song I filmed.",
            "I marked the red car on a frame.",
            "I marked the red car on a frame.",
            "I marked the red car on a frame.",
            "I marked the it on a frame.",
            "I marked the it on a frame.",
            "I marked the it on a frame.",
            "I skipped your clip question, so pick those clips for me.",
            "I skipped your clip question, so pick those clips for me.",
            "I skipped your clip question, so pick those clips for me.",
            "I tapped the clips you asked about.",
            "I tapped the clips you asked about.",
            "I tapped the clips you asked about.",
            "typed",
            "typed",
            "",
            "typed",
            "typed",
            "",
        ],
        "tr": [
            "Kliplerimin şarkıda nereye geleceğini onayladım.",
            "Kliplerimin şarkıda nereye geleceğini onayladım.",
            "I confirmed where my clips go on the song timeline.",
            "Anlarımın her birinin nerede olduğunu gösterdim.",
            "Anlarımın her birinin nerede olduğunu gösterdim.",
            "I showed you where each of my moments is.",
            "Şarkının hangi kısmını çektiğimi söyledim.",
            "Şarkının hangi kısmını çektiğimi söyledim.",
            "I told you which part of the song I filmed.",
            "red car bir karede işaretledim.",
            "red car bir karede işaretledim.",
            "I marked the red car on a frame.",
            "it bir karede işaretledim.",
            "it bir karede işaretledim.",
            "I marked the it on a frame.",
            "Klip sorusunu boş bıraktım, o klipleri sen belirle.",
            "Klip sorusunu boş bıraktım, o klipleri sen belirle.",
            "I skipped your clip question, so pick those clips for me.",
            "Sorduğun klipleri seçtim.",
            "Sorduğun klipleri seçtim.",
            "I tapped the clips you asked about.",
            "typed",
            "typed",
            "",
            "typed",
            "typed",
            "",
        ],
    },
    "strategy_policy.localize_known_message": {
        "en": [
            "A lip-sync edit follows your song, so I'm not timing it to a recorded voiceover.",
            "I'll use your song as the background music for this edit.",
            "You picked Lip-sync, so I'll sync your takes to your song, placing each one where it fits the music.",
            "A lip-sync edit follows your song, so I'm not using a day-vlog or single-hero shape.",
            "Your own song only works on edits that render on your iPhone. Should I make this with licensed music instead?",
            "Upload the song first, then I'll build the edit around it. Or should I use licensed music instead?",
            "Talking edits caption what you say, so I didn't add separate labels or captions to individual clips.",
            "Sound effects on iPhone are placed at the moments you named; the general sound-effect treatment was left out.",
            "Sound and photo pop-ins timed to your words aren't available for this edit yet; left them out.",
            "Small cards at an exact time aren't available for this edit yet; left them out.",
            "Couldn't find the clip the small card should follow; left that card out.",
            "Couldn't find the photo or video you named for a small card; left it out.",
            "Videos can't show as small cards in this edit yet; left that card out.",
            "Couldn't find the closing photo you named; kept the normal ending.",
            "Couldn't find the closing badge you named; left it off.",
            "Couldn't add that video at the end with its own sound: only a talking-to-camera edit on your iPhone can end on another video.",
            "Couldn't find the video you named for the end; kept the normal ending.",
            "Couldn't add that at the end with its own sound: it's a photo, not a video. A photo can be the closing photo instead.",
            "Couldn't add that video at the end: this iPhone can't play videos from Visuals yet.",
            "Couldn't add that video at the end: this iPhone render can't place pictures and videos around your talk yet.",
            "Couldn't add that at the end with its own sound: this iPhone can't mix its sound into the video yet.",
            "Couldn't add that clip at the end silent: one of your own clips can only end the video with its own sound.",
            "Couldn't add that clip at the end: you asked to leave it out of this edit.",
            "Couldn't add that clip at the end: it is the only clip in this edit.",
            "Couldn't add that clip at the end: ending on another of your clips needs a Talking edit with several clips, which isn't available yet.",
            "The video you wanted at the end plays only there, not also on a spoken word.",
            "Couldn't put that behind you: a photo or video background only works on a talking-to-camera edit rendered on your iPhone, and it isn't available here right now.",
            "Couldn't find the photo or video you named for the background; kept your own background.",
            "Couldn't use the background you picked in the camera: it is no longer in this edit's photos and videos; kept your own background.",
            "Couldn't use that as the background: this iPhone can't draw it behind you yet.",
            "Couldn't change the backgrounds or put you small in a corner: your iPhone app can't switch backgrounds yet. One background stays behind you for the whole video.",
            "Couldn't find some of the photos or videos you named for the changing background; left them out.",
            "Couldn't start you full screen before the background: your iPhone app can't do that yet. Update the app and ask again.",
            "Couldn't start you full screen before the background: it isn't available here right now.",
            "Aiming the recordings needs the latest Kria app, so they're centred.",
            "Aiming the recordings isn't available yet, so they're centred.",
            "Couldn't put a plain colour behind you: your iPhone app can't show one yet; kept your own background.",
            "Couldn't use that colour for your background; kept your own background. Try white, black, green, blue, red, yellow or grey.",
            "The photo or video behind you is only the background, not also a pop-in on a spoken word.",
            "Sound effects can't render on your iPhone yet. Ask for this edit without the sound effect.",
            "The requested licensed sound effect is unavailable. Choose another effect.",
            "Couldn't find \"Mila\"'s photo/sticker; left it out.",
            'Full screen isn\'t available for this edit yet, so "the map" shows as a small card in the corner instead.',
            "Day vlog shape needs music; kept a regular montage.",
            "Single-hero shape needs music; kept a regular montage.",
            "Reading this as a montage in the day-vlog style.",
            "Reading this as a montage in the single-hero style.",
            "The requested licensed sound effect whoosh is unavailable.",
            "Something the table does not know.",
            "Zaten Türkçe yazılmış bir not.",
        ],
        "tr": [
            "Dudak senkronlu düzenleme şarkını izler, o yüzden kaydedilmiş seslendirmeye göre zamanlamıyorum.",
            "Şarkını bu düzenlemede arka plan müziği olarak kullanacağım.",
            "Dudak senkronu seçtin, o yüzden çekimlerini şarkına senkronluyorum; her birini müziğe uyduğu yere koyuyorum.",
            "Dudak senkronlu düzenleme şarkını izler, o yüzden günlük vlog ya da tek kahraman kurgusu kullanmıyorum.",
            "Kendi şarkın sadece iPhone'unda hazırlanan düzenlemelerde çalışıyor. Bunu lisanslı müzikle yapayım mı?",
            "Önce şarkıyı yükle, düzenlemeyi ona göre kurarım. Ya da lisanslı müzik kullanayım mı?",
            "Konuşmalı düzenlemeler söylediklerini altyazıya çevirir, o yüzden tek tek kliplere ayrı etiket ya da altyazı eklemedim.",
            "iPhone'da ses efektleri senin söylediğin anlara konuyor; genel ses efekti uygulamasını koymadım.",
            "Sözlerine göre çıkan ses ve fotoğraflar bu düzenleme için henüz yok; koymadım.",
            "Tam zamanlı küçük kartlar bu düzenleme için henüz yok; koymadım.",
            "Küçük kartın takip edeceği klibi bulamadım; o kartı koymadım.",
            "Küçük kart için söylediğin fotoğrafı ya da videoyu bulamadım; koymadım.",
            "Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor; o kartı koymadım.",
            "Söylediğin kapanış fotoğrafını bulamadım; normal bitişi bıraktım.",
            "Söylediğin kapanış rozetini bulamadım; koymadım.",
            "Bu videoyu kendi sesiyle sona ekleyemedim: sadece iPhone'daki konuşmalı düzenlemeler başka bir videoyla bitebilir.",
            "Son için söylediğin videoyu bulamadım; normal bitişi bıraktım.",
            "Bunu kendi sesiyle sona ekleyemedim: bu bir fotoğraf, video değil. Fotoğraf kapanış fotoğrafı olarak girebilir.",
            "Bu videoyu sona ekleyemedim: bu iPhone Visuals'taki videoları henüz oynatamıyor.",
            "Bu videoyu sona ekleyemedim: bu iPhone oluşturması konuşmanın çevresine resim ve video koyamıyor henüz.",
            "Bunu kendi sesiyle sona ekleyemedim: bu iPhone sesini videoya karıştıramıyor henüz.",
            "Bu klibi sessiz olarak sona ekleyemedim: kendi kliplerinden biri videoyu ancak kendi sesiyle bitirebilir.",
            "Bu klibi sona ekleyemedim: bu düzenlemeden çıkarmamı istemiştin.",
            "Bu klibi sona ekleyemedim: bu düzenlemedeki tek klip bu.",
            "Bu klibi sona ekleyemedim: başka bir klibinle bitirmek için birden çok klipli konuşmalı düzenleme gerekiyor ve o henüz yok.",
            "Sonda istediğin video sadece orada oynuyor, söylediğin bir kelimede ayrıca çıkmıyor.",
            "Bunu arkana koyamadım: fotoğraf ya da video arka planı sadece iPhone'da hazırlanan konuşmalı düzenlemelerde çalışıyor ve şu an burada kullanılamıyor.",
            "Arka plan için söylediğin fotoğraf ya da videoyu bulamadım; kendi arka planını bıraktım.",
            "Kamerada seçtiğin arka planı kullanamadım: artık bu düzenlemenin fotoğraf ve videoları arasında değil; kendi arka planını bıraktım.",
            "Bunu arka plan yapamadım: bu iPhone onu henüz arkanda çizemiyor.",
            "Arka planları değiştirip seni köşede küçük gösteremedim: iPhone uygulaman henüz arka plan değiştiremiyor. Bütün video boyunca arkanda tek bir arka plan duruyor.",
            "Değişen arka plan için söylediğin bazı fotoğraf ya da videoları bulamadım; onları çıkardım.",
            "Arka plandan önce seni tam ekran başlatamadım: iPhone uygulaman bunu henüz yapamıyor. Uygulamayı güncelleyip tekrar iste.",
            "Arka plandan önce seni tam ekran başlatamadım: bu şu an burada kullanılamıyor.",
            "Kayıtları istediğin yöne çevirmek için Kria uygulamasının son sürümü gerekiyor; şimdilik ortalı duruyorlar.",
            "Kayıtları istediğin yöne çevirmek henüz kullanılamıyor; şimdilik ortalı duruyorlar.",
            "Arkana düz bir renk koyamadım: iPhone uygulaman henüz bunu gösteremiyor; kendi arka planını bıraktım.",
            "Bu rengi arka planın için kullanamadım; kendi arka planını bıraktım. Beyaz, siyah, yeşil, mavi, kırmızı, sarı ya da gri dene.",
            "Arkandaki fotoğraf ya da video sadece arka plan, söylediğin bir kelimede ayrıca çıkmıyor.",
            "Ses efektleri iPhone'unda henüz hazırlanamıyor. Bu düzenlemeyi ses efektsiz iste.",
            "İstediğin lisanslı ses efekti yok. Başka bir efekt seç.",
            '"Mila" için fotoğraf ya da çıkartma bulamadım; koymadım.',
            'Bu düzenlemede tam ekran henüz yok; "the map" köşede küçük bir kart olarak çıkacak.',
            "Günlük vlog kurgusu için müzik gerekiyor; normal bir montaj yaptım.",
            "Tek kahraman kurgusu için müzik gerekiyor; normal bir montaj yaptım.",
            "Bunu günlük vlog tarzında bir montaj olarak okudum.",
            "Bunu tek kahraman tarzında bir montaj olarak okudum.",
            "İstediğin lisanslı ses efekti (whoosh) yok.",
            "Something the table does not know.",
            "Zaten Türkçe yazılmış bir not.",
        ],
    },
}
# --- END EXPECTED ---


def _run(name: str, lang: str) -> Any:
    fn, _reaches = CASES[name]
    with reply_language_for(lang):
        return json.loads(json.dumps(fn(lang), ensure_ascii=False))


@pytest.mark.parametrize("lang", ["en", "tr"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_english_and_turkish_are_unchanged(name: str, lang: str) -> None:
    assert name in EXPECTED, f"capture {name} from the pre-change code"
    assert _run(name, lang) == EXPECTED[name][lang]


@pytest.fixture
def stand_in_german(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """German on, with a ``de`` catalog line for every key ``tr.json`` has."""
    from app.config import settings

    messages = json.loads(_TR_JSON.read_text(encoding="utf-8"))["messages"]
    german = {key: f"DE<{key}>" for key in messages}
    real = copy_catalog.catalog
    monkeypatch.setattr(
        copy_catalog, "catalog", lambda language: german if language == "de" else real(language)
    )
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    return german


@pytest.mark.parametrize("name", sorted(n for n, (_fn, reaches) in CASES.items() if reaches))
def test_german_reaches_the_catalog(name: str, stand_in_german: dict[str, str]) -> None:
    german = _run(name, "de")
    assert "DE<" in json.dumps(german, ensure_ascii=False), german


def test_german_chat_gets_german_buttons(stand_in_german: dict[str, str]) -> None:
    """The wording buttons are a hand-written table per language (``_COPY_LABELS``), not catalog
    keys: a German chat gets the German row, Turkish and English keep theirs (cases above)."""
    from app.services.creative_copy_decisions import _COPY_LABELS, authorship_question

    with reply_language_for("de"):
        question = authorship_question(target="opening_title", dependency_digest="d")
    labels = [option["label"] for option in question["options"]]
    assert labels == [_COPY_LABELS["de"][0], _COPY_LABELS["de"][1], _COPY_LABELS["de"][4]]
    # the English label stays a typed alias, as in a Turkish chat
    assert "I have an idea" in question["options"][0]["aliases"]


def test_german_chat_shows_the_creator_words_for_an_english_label(
    stand_in_german: dict[str, str],
) -> None:
    from app.schemas.clip_intents import ClipIntent
    from app.services.clip_selection import category_label

    intent = ClipIntent(intent_id="a", op="group", attribute="the moment the goal is scored")
    with reply_language_for("de"):
        assert category_label(intent, creator_words="der Moment, als das Tor fällt") == (
            "der Moment, als das Tor fällt"
        )
