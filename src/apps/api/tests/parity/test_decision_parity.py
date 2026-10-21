"""English <-> Turkish parity for Kria's rule-based decision points (KRI-773, phase 2, layer 1).

Free, offline, deterministic. Every case calls the REAL decision function that reads
creator text (a regex, a phrase set, a marker list, a normalizer) once with an English
phrase and once per Turkish variant, and checks that the Turkish result equals the
English one:

- ``tr``        natural Turkish with diacritics, as a native creator would type it
- ``tr_ascii``  the same message typed without Turkish letters (phone, no TR keyboard)
- ``tr_upper``  the same message in capitals ("TÜM VİDEOLARIMDA")
- ``tr_mixed``  Turkish with English product words mixed in ("music olmasın")

A sanity check per case asserts the English phrase really produces the expected value,
so a broken English case never counts as parity.

This is a measurement plus a ratchet, not a fix. Known gaps are marked
``xfail(strict=True)``: the suite is green today, and a change that closes a gap makes
that variant XPASS, which fails strictly, so the developer removes the marker and the
baseline moves up. ``tests/parity/conftest.py`` writes
``test-results/parity-decisions.json`` (relative to ``src/apps/api``) with the evaluated
result of every case and variant (computed by ``evaluate()``, never read from the
markers) and parity totals overall and per stage.

Normalization cases (stage ``normalization``) have no English counterpart: the "English"
input is the canonical spelling and ``expect`` is the correct output.

Decision points NOT unit-reachable (not covered here):

- ``kria/brief_checks._wants_speech`` is covered, but the rest of the speech-excerpt
  receipt (``_check_speech_excerpts``) needs full ``PlanFacts``; only the word gate is
  measured.
- ``routes/creator_agent`` "photo sub-second timing" fallback (the inline regex in
  ``_apply_explicit_render_intent``) runs only when ``recognize_mixed_media_timing``
  returns None; it is measured through ``_apply_explicit_render_intent`` as a whole.
- ``kria/planner`` re-plan decision inside ``plan_kria_turn`` (async, DB + model);
  measured through its pure helpers ``_is_text_edit_ask`` / ``_fast_path_eligible`` /
  ``_is_single_ask``.
- ``pipeline/silence_cut`` retake detection is an LLM agent, not a word rule; only the
  lexical filler lexicon (``is_filler_token``) is measured.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

# --------------------------------------------------------------------------- adapters
# Each adapter calls one real decision function and returns a comparable value.


def _status_only(text: str) -> bool:
    from app.routes.creation_threads import _is_status_only_message

    return _is_status_only_message(text)


def _status_question(text: str) -> bool:
    from app.kria.language import is_status_question

    return is_status_question(text)


def _help_question(text: str) -> bool:
    from app.kria.language import is_help_question

    return is_help_question(text)


def _paraphrase_only(text: str) -> bool:
    from app.kria.language import is_paraphrase_only

    return is_paraphrase_only(
        user_message="make the title bigger and gold please", assistant_message=text
    )


def _full_replan(text: str) -> bool:
    from app.kria.brief import wants_full_replan

    return wants_full_replan(text)


def _text_edit_ask(text: str) -> bool:
    from app.kria.planner import _is_text_edit_ask

    return _is_text_edit_ask(text)


def _needs_replan(text: str) -> bool:
    from app.kria.planner import _fast_path_eligible

    return not _fast_path_eligible(text)


def _compound_ask(text: str) -> bool:
    from app.kria.planner import _is_single_ask

    return not _is_single_ask(text)


_CHOICE_QUESTION = {
    "kind": "duration_vs_count",
    "options": [{"key": "keep_all", "recommended": True}, {"key": "trim"}],
}


def _delegated(text: str) -> str | None:
    from app.services.choice_questions import delegated_choice

    return delegated_choice(_CHOICE_QUESTION, text)


def _media_scope(text: str) -> str:
    from app.routes.creator_agent import _explicit_media_scope

    return _explicit_media_scope(text)


def _keep_order(text: str) -> bool:
    from app.routes.creator_agent import _requests_preserved_clip_order

    return _requests_preserved_clip_order(text)


def _sfx_name(text: str) -> str | None:
    from app.routes.creator_agent import _explicit_sfx_name

    name = _explicit_sfx_name(text)
    return name.casefold() if name else None


def _render_intent(text: str) -> Any:
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.routes.creator_agent import _apply_explicit_render_intent

    return _apply_explicit_render_intent(CreativeStrategy(), text, latest_user_message=text)


def _opening_title(text: str) -> str | None:
    return _render_intent(text).opening_title


def _closing_title(text: str) -> str | None:
    return _render_intent(text).closing_title


def _pacing(text: str) -> str | None:
    return _render_intent(text).pacing


def _photo_hold_via_intent(text: str) -> float | None:
    timing = _render_intent(text).mixed_media_timing
    return timing.image_hold_s if timing is not None else None


def _photo_hold_s(text: str) -> float | None:
    from app.schemas.edit_proposal import recognize_mixed_media_timing

    timing = recognize_mixed_media_timing(text)
    return timing.image_hold_s if timing is not None else None


def _photo_fast_video_long(text: str) -> tuple[str, str] | None:
    from app.schemas.edit_proposal import recognize_mixed_media_timing

    timing = recognize_mixed_media_timing(text)
    return (timing.image_hold, timing.video_hold) if timing is not None else None


def _total_duration(text: str) -> float | None:
    from app.schemas.edit_proposal import recognize_total_duration_s

    value = recognize_total_duration_s(text)
    return float(value) if value is not None else None


def _round_robin(text: str) -> float | None:
    from app.schemas.edit_proposal import recognize_round_robin_cadence

    return recognize_round_robin_cadence(text)


def _reuse_policy(text: str) -> str | None:
    from app.schemas.edit_proposal import recognize_explicit_cadence_reuse_policy

    return recognize_explicit_cadence_reuse_policy(text)


def _image_layout(text: str) -> str | None:
    from app.schemas.edit_proposal import recognize_image_layout

    return recognize_image_layout(text)


def _exclusive_quotes(text: str) -> bool:
    from app.agents.edit_proposal import _creator_quoted_captions_are_exhaustive

    return _creator_quoted_captions_are_exhaustive(text)


def _first_quote(text: str) -> re.Match[str]:
    from app.agents.spoken_script import QUOTED_TEXT_RE

    match = QUOTED_TEXT_RE.search(text)
    assert match is not None, f"case text has no quote: {text!r}"
    return match


def _reported_speech(text: str) -> bool:
    from app.agents.spoken_script import is_reported_speech_quote

    return is_reported_speech_quote(text, _first_quote(text))


def _voiceover_script(text: str) -> bool:
    from app.agents.spoken_script import is_spoken_script_quote

    return is_spoken_script_quote(text, _first_quote(text), spoken=[])


def _wants_speech(text: str) -> bool:
    from app.kria.brief import BriefRequirement
    from app.kria.brief_checks import _wants_speech

    return _wants_speech(BriefRequirement(id="r1", kind="audio", scope="video", description=text))


def _durable_font(text: str) -> tuple[str | None, Any]:
    from app.services.creator_direction import infer_typed_direction

    key, value, _ = infer_typed_direction(text, None, None, "default")
    return key, value


def _memory_candidate(text: str) -> str:
    from app.services.creator_memory_learning import classify_memory_candidate

    return classify_memory_candidate(text)


def _temporary(text: str) -> bool:
    from app.services.finished_style_learning import explicitly_temporary

    return explicitly_temporary(text)


def _receipt_font_cue(text: str) -> bool:
    from app.kria.brief import BriefRequirement
    from app.kria.editor_receipts import _named_cues
    from app.kria.style_asks import words

    req = BriefRequirement(id="r1", kind="style", scope="video", description=text)
    return "font" in _named_cues(req, words(text))


def _caption_language(text: str) -> str | None:
    from app.pipeline.caption_language import parse_caption_language_request

    return parse_caption_language_request(text)


def _filler_token(text: str) -> bool:
    from app.pipeline.silence_cut import is_filler_token

    return is_filler_token(text)


def _hook_filler_penalty(text: str) -> bool:
    from app.pipeline.score import _adjust_hook_score

    return _adjust_hook_score(5.0, text, start_s=10.0, cut_timestamps=set()) < 5.0


def _fold_text(text: str) -> str:
    from app.kria.brief_route import fold_text

    return fold_text(text)


def _loose_text(text: str) -> str:
    from app.kria.brief_route import loose_text

    return loose_text(text)


def _legacy_fold(text: str) -> str:
    from app.kria.brief import _legacy_fold as fold

    return fold(text)


def _lyrics_normalize(text: str) -> str:
    from app.pipeline.lyrics_alignment import _normalize

    return _normalize(text)


def _dedup_tokens(text: str) -> frozenset[str]:
    from app.services.content_plan_dedup import _idea_tokens

    return frozenset(_idea_tokens(text))


def _seed_tokens(text: str) -> frozenset[str]:
    from app.services.seed_provenance import _tokens

    return frozenset(_tokens(text))


def _upper(text: str) -> str:
    from app.agents._schemas.text_element import apply_text_case

    return apply_text_case(text, "upper")


def _lower(text: str) -> str:
    from app.agents._schemas.text_element import apply_text_case

    return apply_text_case(text, "lower")


# --------------------------------------------------------------------------- table


@dataclass(frozen=True)
class Case:
    id: str
    stage: str
    decision: str  # "module.function" actually called
    capability: str
    fn: Callable[[str], Any]
    en: str
    expect: Any  # what the English phrase must produce (sanity) and every variant must match
    variants: dict[str, str]
    gaps: dict[str, str] = field(default_factory=dict)  # variant -> baseline-gap reason


def _gaps(reason: str, *variants: str) -> dict[str, str]:
    """The same baseline-gap reason for several variants of one case."""
    return dict.fromkeys(variants, reason)


CASES: tuple[Case, ...] = (
    # ---------------------------------------------------------------- chat routing
    # D01: a status-only chat message while a render runs only reports progress.
    Case(
        "D01",
        "chat_routing",
        "routes.creation_threads._is_status_only_message",
        "status-only message during render",
        _status_only,
        "how's it going?",
        True,
        {
            "tr": "Son durum ne?",
            "tr_ascii": "son durum ne",
            "tr_upper": "SON DURUM NE?",
            "tr_mixed": "status ne?",
        },
        gaps=_gaps("English-only _STATUS_ONLY_RE", "tr", "tr_ascii", "tr_upper", "tr_mixed"),
    ),
    # D02: the runtime's inert status question (no new edit).
    Case(
        "D02",
        "chat_routing",
        "kria.language.is_status_question",
        "status question",
        _status_question,
        "how is it going?",
        True,
        {"tr": "Nasıl gidiyor?", "tr_ascii": "nasil gidiyor", "tr_upper": "NASIL GİDİYOR?"},
    ),
    # D03: capability help question, never an edit.
    Case(
        "D03",
        "chat_routing",
        "kria.language.is_help_question",
        "help question",
        _help_question,
        "what can you do?",
        True,
        {
            "tr": "Neler yapabilirsin?",
            "tr_ascii": "neler yapabilirsin",
            "tr_upper": "NELER YAPABİLİRSİN?",
        },
    ),
    # D04: a reply that only restates the creator's ask is rejected as no-value.
    Case(
        "D04",
        "chat_routing",
        "kria.language.is_paraphrase_only",
        "reject paraphrase-only reply",
        _paraphrase_only,
        "You want the title bigger and gold.",
        True,
        {
            "tr": "Başlığın daha büyük ve altın rengi olmasını istiyorsun.",
            "tr_ascii": "Basligin daha buyuk ve altin rengi olmasini istiyorsun.",
            "tr_upper": "BAŞLIĞIN DAHA BÜYÜK VE ALTIN RENGİ OLMASINI İSTİYORSUN.",
        },
        gaps=_gaps(
            "only acknowledgement prefixes are matched; Turkish restates with -istiyorsun",
            "tr",
            "tr_ascii",
            "tr_upper",
        ),
    ),
    # D05: "from scratch" is a full re-plan, not an editor op.
    Case(
        "D05",
        "chat_routing",
        "kria.brief.wants_full_replan",
        "redo from scratch",
        _full_replan,
        "do it again from scratch",
        True,
        {"tr": "Baştan yap", "tr_ascii": "bastan yap", "tr_upper": "BAŞTAN YAP"},
    ),
    # D06: "do it again based on my prompt" is a re-plan.
    Case(
        "D06",
        "chat_routing",
        "kria.brief.wants_full_replan",
        "redo from my prompt",
        _full_replan,
        "try again based on my prompt",
        True,
        {
            "tr": "İsteğime göre yeniden oluştur",
            "tr_ascii": "istegime gore yeniden olustur",
            "tr_upper": "İSTEĞİME GÖRE YENİDEN OLUŞTUR",
            "tr_mixed": "prompta göre tekrar dene",
        },
    ),
    # D07: a text/title ask keeps the copilot's answer instead of re-planning.
    Case(
        "D07",
        "chat_routing",
        "kria.planner._is_text_edit_ask",
        "text edit ask",
        _text_edit_ask,
        "change the title",
        True,
        {
            "tr": "Başlığı değiştir",
            "tr_ascii": "basligi degistir",
            "tr_upper": "BAŞLIĞI DEĞİŞTİR",
            "tr_mixed": "title'ı değiştir",
        },
    ),
    # D08: re-plan wording ("a different version") skips the editor fast path.
    Case(
        "D08",
        "chat_routing",
        "kria.planner._fast_path_eligible",
        "re-plan cue blocks fast path",
        _needs_replan,
        "make a different version",
        True,
        {
            "tr": "Farklı bir versiyon yap",
            "tr_ascii": "farkli bir versiyon yap",
            "tr_upper": "FARKLI BİR VERSİYON YAP",
        },
    ),
    # D09: two asks joined by "and" are not a single fast-path ask.
    Case(
        "D09",
        "chat_routing",
        "kria.planner._is_single_ask",
        "compound ask detection",
        _compound_ask,
        "make the title bigger and change the font",
        True,
        {
            "tr": "Başlığı büyüt ve fontu değiştir",
            "tr_ascii": "basligi buyut ve fontu degistir",
            "tr_upper": "BAŞLIĞI BÜYÜT VE FONTU DEĞİŞTİR",
            "tr_mixed": "Başlığı büyüt ayrıca fontu değiştir",
        },
    ),
    # D10: "you choose" hands a pending choice to Kria's recommended option.
    Case(
        "D10",
        "chat_routing",
        "services.choice_questions.delegated_choice",
        "delegate choice (you choose)",
        _delegated,
        "you choose",
        "keep_all",
        {"tr": "Sen seç", "tr_ascii": "sen sec", "tr_upper": "SEN SEÇ"},
    ),
    # D11: "up to you" is a delegation too.
    Case(
        "D11",
        "chat_routing",
        "services.choice_questions.delegated_choice",
        "delegate choice (up to you)",
        _delegated,
        "up to you",
        "keep_all",
        {"tr": "Sana bırakıyorum", "tr_ascii": "sana birakiyorum", "tr_upper": "SANA BIRAKIYORUM"},
    ),
    # ---------------------------------------------------------------- brief parsing
    # D12: an explicit "use all the videos" widens media scope to every upload.
    Case(
        "D12",
        "brief_parsing",
        "routes.creator_agent._explicit_media_scope",
        "use all media",
        _media_scope,
        "use all the videos",
        "all",
        {
            "tr": "Tüm videoları kullan",
            "tr_ascii": "tum videolari kullan",
            "tr_upper": "TÜM VİDEOLARI KULLAN",
            "tr_mixed": "bütün clipleri kullan",
        },
        gaps=_gaps(
            "English-only all-media regex; tüm/bütün never read",
            "tr",
            "tr_ascii",
            "tr_upper",
            "tr_mixed",
        ),
    ),
    # D13: "keep the order" preserves the creator's source sequence.
    Case(
        "D13",
        "brief_parsing",
        "routes.creator_agent._requests_preserved_clip_order",
        "keep clip order",
        _keep_order,
        "keep the order of the clips",
        True,
        {
            "tr": "Kliplerin sırasını koru",
            "tr_ascii": "kliplerin sirasini koru",
            "tr_upper": "KLİPLERİN SIRASINI KORU",
            "tr_mixed": "clip sırasını koru",
        },
        gaps=_gaps(
            "English-only keep/preserve ... order regex", "tr", "tr_ascii", "tr_upper", "tr_mixed"
        ),
    ),
    # D14: a named sound effect becomes a required SFX request.
    Case(
        "D14",
        "brief_parsing",
        "routes.creator_agent._explicit_sfx_name",
        "named sound effect",
        _sfx_name,
        "add the whoosh sound effect",
        "whoosh",
        {
            "tr": "Whoosh ses efektini ekle",
            "tr_ascii": "whoosh ses efektini ekle",
            "tr_upper": "WHOOSH SES EFEKTİNİ EKLE",
            "tr_mixed": "whoosh sfx ekle",
        },
        gaps=_gaps(
            "needs English verb (add/use) before the name", "tr", "tr_ascii", "tr_upper", "tr_mixed"
        ),
    ),
    # D15: quoted opening title copy is extracted literally.
    Case(
        "D15",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "opening title extraction",
        _opening_title,
        'title "Yaz Tatili"',
        "Yaz Tatili",
        {
            "tr": 'Başlık "Yaz Tatili" olsun',
            "tr_ascii": 'baslik "Yaz Tatili" olsun',
            "tr_upper": 'BAŞLIK "Yaz Tatili" OLSUN',
            "tr_mixed": 'title "Yaz Tatili" olsun',
        },
        gaps=_gaps(
            "legacy title grammar needs the English noun title/intro/hook",
            "tr",
            "tr_ascii",
            "tr_upper",
        ),
    ),
    # D16: quoted closing title copy is extracted literally.
    Case(
        "D16",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "closing title extraction",
        _closing_title,
        'closing title "Görüşürüz"',
        "Görüşürüz",
        {
            "tr": 'Kapanış başlığı "Görüşürüz" olsun',
            "tr_ascii": 'kapanis basligi "Görüşürüz" olsun',
            "tr_upper": 'KAPANIŞ BAŞLIĞI "Görüşürüz" OLSUN',
            "tr_mixed": 'outro yazısı "Görüşürüz" olsun',
        },
        gaps=_gaps(
            "closing cue and title noun are English-only", "tr", "tr_ascii", "tr_upper", "tr_mixed"
        ),
    ),
    # D17: stated pacing sets strategy.pacing.
    Case(
        "D17",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "fast pacing",
        _pacing,
        "fast pacing",
        "fast",
        {
            "tr": "Hızlı tempo olsun",
            "tr_ascii": "hizli tempo olsun",
            "tr_upper": "HIZLI TEMPO OLSUN",
            "tr_mixed": "pacing hızlı olsun",
        },
        gaps=_gaps("pacing regex is English-only", "tr", "tr_ascii", "tr_upper", "tr_mixed"),
    ),
    # D18: an exact sub-second photo hold among videos (route-level, incl. fallback).
    Case(
        "D18",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "photo sub-second hold (route)",
        _photo_hold_via_intent,
        "photos 0.3 seconds each between the videos",
        0.3,
        {
            "tr": "Videoların arasında fotoğraflar 0,3 saniye kalsın",
            "tr_ascii": "videolarin arasinda fotograflar 0,3 saniye kalsin",
            "tr_upper": "VİDEOLARIN ARASINDA FOTOĞRAFLAR 0,3 SANİYE KALSIN",
        },
    ),
    # D19: same request through the schema recognizer.
    Case(
        "D19",
        "brief_parsing",
        "schemas.edit_proposal.recognize_mixed_media_timing",
        "photo sub-second hold (schema)",
        _photo_hold_s,
        "photos 0.3 seconds each between the videos",
        0.3,
        {
            "tr": "Videoların arasında fotoğraflar 0,3 saniye kalsın",
            "tr_ascii": "videolarin arasinda fotograflar 0,3 saniye kalsin",
            "tr_upper": "VİDEOLARIN ARASINDA FOTOĞRAFLAR 0,3 SANİYE KALSIN",
            "tr_mixed": "fotoğraflar 0.3 sn, videolar daha uzun",
        },
    ),
    # D20: quick photos + longer videos profile.
    Case(
        "D20",
        "brief_parsing",
        "schemas.edit_proposal.recognize_mixed_media_timing",
        "quick photos, longer videos",
        _photo_fast_video_long,
        "photos very fast, videos longer",
        ("very_fast", "longer"),
        {
            "tr": "Fotoğraflar çok hızlı geçsin, videolar daha uzun kalsın",
            "tr_ascii": "fotograflar cok hizli gecsin, videolar daha uzun kalsin",
            "tr_upper": "FOTOĞRAFLAR ÇOK HIZLI GEÇSİN, VİDEOLAR DAHA UZUN KALSIN",
        },
    ),
    # D21: an explicit whole-video length.
    Case(
        "D21",
        "brief_parsing",
        "schemas.edit_proposal.recognize_total_duration_s",
        "total duration",
        _total_duration,
        "make it 30 seconds",
        30.0,
        {
            "tr": "Toplam süre 30 saniye olsun",
            "tr_ascii": "toplam sure 30 saniye olsun",
            "tr_upper": "TOPLAM SÜRE 30 SANİYE OLSUN",
            "tr_mixed": "30 sn'lik bir video yap",
        },
    ),
    # D22: alternate-between-clips cadence with a numeric cut.
    Case(
        "D22",
        "brief_parsing",
        "schemas.edit_proposal.recognize_round_robin_cadence",
        "round-robin cadence",
        _round_robin,
        "alternate between the clips every 2 seconds",
        2.0,
        {
            "tr": "Klipler arasında 2 saniyede bir sırayla geçiş yap",
            "tr_ascii": "klipler arasinda 2 saniyede bir sirayla gecis yap",
            "tr_upper": "KLİPLER ARASINDA 2 SANİYEDE BİR SIRAYLA GEÇİŞ YAP",
        },
    ),
    # D23: explicit permission to reuse footage.
    Case(
        "D23",
        "brief_parsing",
        "schemas.edit_proposal.recognize_explicit_cadence_reuse_policy",
        "allow clip reuse",
        _reuse_policy,
        "you can repeat clips",
        "allow_repeat",
        {
            "tr": "Klipleri tekrar kullanabilirsin",
            "tr_ascii": "klipleri tekrar kullanabilirsin",
            "tr_upper": "KLİPLERİ TEKRAR KULLANABİLİRSİN",
        },
    ),
    # D24: "don't crop the photos" keeps the whole image (supporting card).
    Case(
        "D24",
        "brief_parsing",
        "schemas.edit_proposal.recognize_image_layout",
        "photos uncropped",
        _image_layout,
        "don't crop the photos",
        "supporting_card",
        {
            "tr": "Fotoğrafları kırpma",
            "tr_ascii": "fotograflari kirpma",
            "tr_upper": "FOTOĞRAFLARI KIRPMA",
        },
        gaps=_gaps("recognize_image_layout is English-only", "tr", "tr_ascii", "tr_upper"),
    ),
    # D25: "fullscreen photos" fills the frame.
    Case(
        "D25",
        "brief_parsing",
        "schemas.edit_proposal.recognize_image_layout",
        "photos fullscreen",
        _image_layout,
        "fullscreen photos",
        "fullscreen",
        {
            "tr": "Fotoğraflar tam ekran olsun",
            "tr_ascii": "fotograflar tam ekran olsun",
            "tr_upper": "FOTOĞRAFLAR TAM EKRAN OLSUN",
        },
        gaps=_gaps("recognize_image_layout is English-only", "tr", "tr_ascii", "tr_upper"),
    ),
    # D26: "only write 'X'" makes the quoted copy the complete caption list.
    Case(
        "D26",
        "brief_parsing",
        "agents.edit_proposal._creator_quoted_captions_are_exhaustive",
        "exclusive quoted caption",
        _exclusive_quotes,
        'only write "Summer 26"',
        True,
        {
            "tr": 'Sadece "Summer 26" yaz',
            "tr_ascii": 'sadece "Summer 26" yaz',
            "tr_upper": 'SADECE "Summer 26" YAZ',
            "tr_mixed": 'yalnızca "Summer 26" yazsın',
        },
        gaps=_gaps("_EXCLUSIVE_QUOTE_RE is only/just", "tr", "tr_ascii", "tr_upper", "tr_mixed"),
    ),
    # D27: a quote attributed to someone ("he said") is story dialogue, not an overlay.
    Case(
        "D27",
        "brief_parsing",
        "agents.spoken_script.is_reported_speech_quote",
        "reported speech quote",
        _reported_speech,
        'my friend said "this is the best day ever"',
        True,
        {
            "tr": 'Arkadaşım "bu hayatımın en güzel günü" dedi',
            "tr_ascii": 'arkadasim "bu hayatimin en guzel gunu" dedi',
            "tr_upper": 'ARKADAŞIM "bu hayatımın en güzel günü" DEDİ',
        },
        gaps=_gaps(
            "attribution verb must precede the quote; Turkish 'dedi' follows it",
            "tr",
            "tr_ascii",
            "tr_upper",
        ),
    ),
    # D28: a quote tagged as voiceover script is spoken, never on-screen copy.
    Case(
        "D28",
        "brief_parsing",
        "agents.spoken_script.is_spoken_script_quote",
        "voiceover script cue",
        _voiceover_script,
        'voiceover: "we finally made it to the top"',
        True,
        {
            "tr": 'Seslendirme: "sonunda zirveye ulaştık"',
            "tr_ascii": 'seslendirme: "sonunda zirveye ulastik"',
            "tr_upper": 'SESLENDİRME: "sonunda zirveye ulaştık"',
            "tr_mixed": 'VO: "sonunda zirveye ulaştık"',
        },
        gaps=_gaps(
            "_SPOKEN_SCRIPT_CUE_RE has no Turkish cue (seslendirme)", "tr", "tr_ascii", "tr_upper"
        ),
    ),
    # D29: an audio ask to play the creator's own spoken words.
    Case(
        "D29",
        "brief_parsing",
        "kria.brief_checks._wants_speech",
        "use my speech",
        _wants_speech,
        "use my voice from the clips",
        True,
        {
            "tr": "Kliplerdeki konuşmamı kullan",
            "tr_ascii": "kliplerdeki konusmami kullan",
            "tr_upper": "KLİPLERDEKİ KONUŞMAMI KULLAN",
            "tr_mixed": "klipteki speech'imi kullan",
        },
        gaps=_gaps("_SPEECH_EXCERPT_RE is English-only", "tr", "tr_ascii", "tr_upper"),
    ),
    # ---------------------------------------------------------------- style + memory
    # D30: a durable "always use <font>" instruction becomes a typed font constraint.
    Case(
        "D30",
        "style_memory",
        "services.creator_direction.infer_typed_direction",
        "durable font rule",
        _durable_font,
        "always use Inter font",
        ("font_family", {"font_family": "Inter"}),
        {
            "tr": "Her zaman Inter fontunu kullan",
            "tr_ascii": "her zaman Inter fontunu kullan",
            "tr_upper": "HER ZAMAN INTER FONTUNU KULLAN",
            "tr_mixed": "always Inter font kullan",
        },
        gaps=_gaps("_FONT_PATTERNS are English-only", "tr", "tr_ascii", "tr_upper", "tr_mixed"),
    ),
    # D31: "always ..." is explicit durable memory.
    Case(
        "D31",
        "style_memory",
        "services.creator_memory_learning.classify_memory_candidate",
        "durable memory (always)",
        _memory_candidate,
        "always put the title at the top",
        "explicit",
        {
            "tr": "Başlığı her zaman üste koy",
            "tr_ascii": "basligi her zaman uste koy",
            "tr_upper": "BAŞLIĞI HER ZAMAN ÜSTE KOY",
        },
    ),
    # D32: "in all my videos" is explicit durable memory.
    Case(
        "D32",
        "style_memory",
        "services.creator_memory_learning.classify_memory_candidate",
        "durable memory (all my videos)",
        _memory_candidate,
        "use this font in every video",
        "explicit",
        {
            "tr": "Tüm videolarımda bu fontu kullan",
            "tr_ascii": "tum videolarimda bu fontu kullan",
            "tr_upper": "TÜM VİDEOLARIMDA BU FONTU KULLAN",
        },
        gaps=_gaps(
            "literal 'tüm videolarımda' substring; ASCII and casefolded İ miss",
            "tr_ascii",
            "tr_upper",
        ),
    ),
    # D33: an ordinary edit is NOT durable memory ("başlasın" must not hit "asla").
    Case(
        "D33",
        "style_memory",
        "services.creator_memory_learning.classify_memory_candidate",
        "no false durable marker",
        _memory_candidate,
        "start with the beach clip",
        "noop",
        {
            "tr": "Plaj klibiyle başlasın",
            "tr_ascii": "plaj klibiyle baslasin",
            "tr_upper": "PLAJ KLİBİYLE BAŞLASIN",
        },
        gaps={"tr_ascii": "'asla' substring matches inside 'baslasin'"},
    ),
    # D34: a temporary style change is not learned as a preference.
    Case(
        "D34",
        "style_memory",
        "services.finished_style_learning.explicitly_temporary",
        "temporary style change",
        _temporary,
        "temporarily use yellow text",
        True,
        {
            "tr": "Geçici olarak sarı yazı kullan",
            "tr_ascii": "gecici olarak sari yazi kullan",
            "tr_upper": "GEÇİCİ OLARAK SARI YAZI KULLAN",
        },
        gaps=_gaps(
            "literal 'geçici' substring; ASCII and casefolded İ miss", "tr_ascii", "tr_upper"
        ),
    ),
    # D35: the editor receipt names a font change as a sub-ask to verify.
    Case(
        "D35",
        "style_memory",
        "kria.editor_receipts._named_cues",
        "font change cue",
        _receipt_font_cue,
        "change the font",
        True,
        {
            "tr": "Yazı tipini değiştir",
            "tr_ascii": "yazi tipini degistir",
            "tr_upper": "YAZI TİPİNİ DEĞİŞTİR",
            "tr_mixed": "fontu değiştir",
        },
        gaps=_gaps(
            "font cue needs English verb + font noun or a registry font name",
            "tr",
            "tr_ascii",
            "tr_upper",
            "tr_mixed",
        ),
    ),
    # ---------------------------------------------------------------- captions + transcript
    # D36: "English captions" requests English caption language.
    Case(
        "D36",
        "captions_transcript",
        "pipeline.caption_language.parse_caption_language_request",
        "English captions",
        _caption_language,
        "English captions",
        "en",
        {
            "tr": "İngilizce altyazı",
            "tr_ascii": "ingilizce altyazi",
            "tr_upper": "İNGİLİZCE ALTYAZI",
        },
    ),
    # D37: "Turkish captions" requests Turkish caption language.
    Case(
        "D37",
        "captions_transcript",
        "pipeline.caption_language.parse_caption_language_request",
        "Turkish captions",
        _caption_language,
        "Turkish captions",
        "tr",
        {
            "tr": "Türkçe altyazı",
            "tr_ascii": "turkce altyazi",
            "tr_upper": "TÜRKÇE ALTYAZI",
            "tr_mixed": "Türkçe captions olsun",
        },
    ),
    # D38: a hesitation vocalization in the transcript is a lexical filler (cut).
    Case(
        "D38",
        "captions_transcript",
        "pipeline.silence_cut.is_filler_token",
        "filler vocalization cut",
        _filler_token,
        "um",
        True,
        {"tr": "ııı", "tr_alt": "eee", "tr_upper": "IIı,"},
    ),
    # D39: a real word used as filler is never cut (documented: "like"/"şey" stay).
    Case(
        "D39",
        "captions_transcript",
        "pipeline.silence_cut.is_filler_token",
        "real-word filler kept",
        _filler_token,
        "like",
        False,
        {"tr": "şey", "tr_ascii": "sey", "tr_upper": "ŞEY"},
    ),
    # D40: a hook that opens with a filler scores lower.
    Case(
        "D40",
        "captions_transcript",
        "pipeline.score._adjust_hook_score",
        "hook filler penalty",
        _hook_filler_penalty,
        "um so here's the thing",
        True,
        {"tr": "ııı yani olay şu", "tr_ascii": "yani olay su", "tr_upper": "YANİ OLAY ŞU"},
        gaps=_gaps(
            "FILLER_WORDS is English-only; ııı/yani not penalized", "tr", "tr_ascii", "tr_upper"
        ),
    ),
    # ---------------------------------------------------------------- normalization
    # N01: shared Turkish-aware fold: every spelling of "kırmızı" folds alike.
    Case(
        "N01",
        "normalization",
        "kria.brief_route.fold_text",
        "fold dotless/dotted i",
        _fold_text,
        "kirmizi",
        "kirmizi",
        {"tr": "kırmızı", "tr_upper": "KIRMIZI", "tr_title": "Kırmızı"},
    ),
    # N02: loose_text strips diacritics too.
    Case(
        "N02",
        "normalization",
        "kria.brief_route.loose_text",
        "loose fold place name",
        _loose_text,
        "eminonu",
        "eminonu",
        {"tr": "Eminönü", "tr_upper": "EMİNÖNÜ"},
    ),
    # N03: the legacy English redo fold must keep English capitals readable.
    Case(
        "N03",
        "normalization",
        "kria.brief._legacy_fold",
        "legacy fold of English capitals",
        _legacy_fold,
        "do it again",
        "do it again",
        {"en_upper": "DO IT AGAIN"},
        gaps={"en_upper": "maps I -> dotless ı, so 'DO IT AGAIN' -> 'do ıt agaın'"},
    ),
    # N04: ...which makes an uppercase English redo miss the re-plan route.
    Case(
        "N04",
        "normalization",
        "kria.brief.wants_full_replan",
        "uppercase English redo",
        _full_replan,
        "do it again",
        True,
        {"en_upper": "DO IT AGAIN"},
        gaps={"en_upper": "_legacy_fold maps I -> ı before the English patterns"},
    ),
    # N05: lyric alignment matches Whisper and canonical lyric words letter-insensitively.
    Case(
        "N05",
        "normalization",
        "pipeline.lyrics_alignment._normalize",
        "lyric token fold",
        _lyrics_normalize,
        "kiz",
        "kiz",
        {"tr": "kız", "tr_upper": "KIZ", "tr_title": "Kız"},
        gaps=_gaps("dotless ı has no NFKD base letter, stripped to 'kz'", "tr", "tr_title"),
    ),
    # N06: idea dedup tokens fold every spelling alike.
    Case(
        "N06",
        "normalization",
        "services.content_plan_dedup._idea_tokens",
        "idea dedup tokens",
        _dedup_tokens,
        "kirmizi elbise",
        frozenset({"kirmizi", "elbise"}),
        {"tr": "kırmızı elbise", "tr_upper": "KIRMIZI ELBİSE"},
        gaps={"tr": "NFKD + ascii-ignore drops ı, 'krmz'"},
    ),
    # N07: seed provenance tokens fold every spelling alike.
    Case(
        "N07",
        "normalization",
        "services.seed_provenance._tokens",
        "seed provenance tokens",
        _seed_tokens,
        "kirmizi elbise",
        frozenset({"kirmizi", "elbise"}),
        {"tr": "kırmızı elbise", "tr_upper": "KIRMIZI ELBİSE"},
        gaps={"tr": "NFKD + ascii-ignore drops ı, 'krmz'"},
    ),
    # N08: on-screen UPPER case of Turkish text keeps the dotted capital İ.
    Case(
        "N08",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "text_case upper",
        _upper,
        "İSTANBUL",
        "İSTANBUL",
        {"tr": "istanbul", "tr_title": "İstanbul"},
        gaps={"tr": "str.upper maps i to I, not İ"},
    ),
    # N09: on-screen lower case of Turkish text keeps dotless ı.
    Case(
        "N09",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "text_case lower",
        _lower,
        "ışık",
        "ışık",
        {"tr_upper": "IŞIK"},
        gaps={"tr_upper": "str.lower maps I to i, not ı"},
    ),
)


# --------------------------------------------------------------------------- evaluation


def _safe_call(fn: Callable[[str], Any], text: str) -> tuple[Any, str | None]:
    try:
        return fn(text), None
    except Exception as exc:  # noqa: BLE001 - a crash on Turkish input is a parity failure
        return None, f"{type(exc).__name__}: {exc}"


def evaluate(case: Case, variant: str) -> dict[str, Any]:
    """Run one case/variant through the real function. ``variant == "en"`` is the sanity check."""
    text = case.en if variant == "en" else case.variants[variant]
    actual, error = _safe_call(case.fn, text)
    return {
        "id": case.id,
        "stage": case.stage,
        "decision": case.decision,
        "capability": case.capability,
        "variant": variant,
        "text": text,
        "expected": case.expect,
        "actual": actual,
        "error": error,
        "passed": error is None and actual == case.expect,
        "known_gap": case.gaps.get(variant),
    }


def _params() -> list[Any]:
    params = []
    for case in CASES:
        params.append(pytest.param(case, "en", id=f"{case.id}-en-sanity"))
        for variant in case.variants:
            marks = []
            if variant in case.gaps:
                marks.append(
                    pytest.mark.xfail(strict=True, reason=f"baseline gap: {case.gaps[variant]}")
                )
            params.append(pytest.param(case, variant, id=f"{case.id}-{variant}", marks=marks))
    return params


@pytest.mark.parametrize(("case", "variant"), _params())
def test_decision_parity(case: Case, variant: str) -> None:
    result = evaluate(case, variant)
    assert result["error"] is None, result["error"]
    assert result["actual"] == case.expect, (
        f"{case.id} {case.capability} [{variant}] {result['text']!r}: "
        f"expected {case.expect!r}, got {result['actual']!r}"
    )


def test_case_table_is_well_formed() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert case.variants, case.id
        assert set(case.gaps) <= set(case.variants), case.id
