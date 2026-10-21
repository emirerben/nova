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


def _states_two_seconds(text: str) -> bool:
    from app.routes.creator_agent import _excerpt_states_seconds

    return _excerpt_states_seconds(text, 2.0)


def _generic_sfx_ask(text: str) -> bool:
    from app.services.creator_sessions import _GENERIC_SOUND_EFFECT_REQUEST_RE

    return bool(_GENERIC_SOUND_EFFECT_REQUEST_RE.search(text))


def _review_timing_kept(text: str) -> bool:
    """True when a draft-review message keeps the current photo/video timing profile."""
    from app.routes.plan_items import _review_mixed_media_timing
    from app.schemas.edit_proposal import MixedMediaTimingProfile

    current = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )
    return _review_mixed_media_timing(current, text) is current


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


def _unsupported_claim(text: str) -> bool:
    from app.agents.edit_proposal import ai_draft_thought_has_unsupported_claim

    return ai_draft_thought_has_unsupported_claim(text)


def _spoken_score(text: str) -> tuple[int | None, ...]:
    from app.pipeline.narration_labels import _score_value

    return tuple(_score_value(word) for word in text.split())


def _receipt_timing_cue(text: str) -> bool:
    from app.kria.brief import BriefRequirement
    from app.kria.editor_receipts import _named_cues
    from app.kria.style_asks import words

    req = BriefRequirement(id="r1", kind="timing", scope="title", description=text)
    return "timing" in _named_cues(req, words(text))


def _narrated_score_overlay(text: str) -> bool:
    """A creator brief turns the narrated "3-1" into a score overlay (or withdraws it)."""
    from types import SimpleNamespace

    from app.pipeline.transcribe import Transcript, Word
    from app.tasks.generative_build import _narrated_storyboard_text_elements

    spoken = ["Final", "score", "3-1"]
    transcript = Transcript(
        words=[Word(word, i * 0.2, (i + 1) * 0.2, 1.0) for i, word in enumerate(spoken)],
        language="en",
    )
    elements = _narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=2.0)],
        clip_assignments=[],
        clip_id_by_path={},
        creator_request=text,
        explicit_opening_title=None,
        storyboard={"overlays": []},
    )
    return any(
        "score:" in str(item["source_params"].get("narrated_storyboard", "")) for item in elements
    )


def _hook_refusal(text: str) -> bool:
    from app.agents.intro_writer import _is_refusal_text

    return _is_refusal_text(text)


def _caption_language(text: str) -> str | None:
    from app.pipeline.caption_language import parse_caption_language_request

    return parse_caption_language_request(text)


def _chat_and_render_language(text: str) -> tuple[str | None, str]:
    """The chat language ``text`` sets, and the render language a plan item it drives gets."""
    import uuid
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.kria.reply_language import resolve_reply_language
    from app.tasks.content_plan_build import _item_chat_language

    chat = resolve_reply_language(text)
    thread = SimpleNamespace(state={"reply_language": chat} if chat else {})
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = thread
    item = SimpleNamespace(id=uuid.uuid4())
    return chat, _item_chat_language(session, item, uuid.uuid4(), None)


def _hook_in_chat_language(text: str) -> bool:
    from app.agents.intro_writer import _language_instruction

    chat, render = _chat_and_render_language(text)
    return chat is not None and _language_instruction(render) == _language_instruction(chat)


def _caption_fallback_in_chat_language(text: str) -> bool:
    from app.pipeline.caption_language import resolve_spoken_caption_language

    chat, render = _chat_and_render_language(text)
    # Whisper heard no language and the transcript is too short to guess from.
    language, _source = resolve_spoken_caption_language(None, transcript_text="", fallback=render)
    return chat is not None and language == chat


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


def _upper_auto(text: str) -> str:
    # No language known (a stored text element): rules follow the text's own letters.
    from app.agents._schemas.text_element import apply_text_case

    return apply_text_case(text, "upper")


def _title_auto(text: str) -> str:
    from app.agents._schemas.text_element import apply_text_case

    return apply_text_case(text, "title")


def _narrated_norm(text: str) -> str:
    from app.pipeline.narrated_alignment import _norm

    return _norm(text)


def _clip_intent_words(text: str) -> tuple[str, ...]:
    from app.services.clip_selection import _words

    return tuple(_words(text))


def _lrclib_title_tokens(text: str) -> frozenset[str]:
    from app.services.lrclib_client import _tokenize_title

    return frozenset(_tokenize_title(text))


def _stateful(text: str) -> bool:
    from app.services.creator_memory_learning import requires_stateful_extraction

    return requires_stateful_extraction(text)


def _artist_gate(text: str) -> bool:
    from app.services.lrclib_client import _artists_match

    return _artists_match(text, "Barış Manço")


def _karaoke_upper_composes(text: str) -> bool:
    # generative_overlays cases the element text whole and each word_timings entry alone;
    # rebuild_word_timings_for_text keeps the beat-snapped timings only when they agree.
    from app.agents._schemas.text_element import apply_text_case

    whole = apply_text_case(text, "upper").split()
    return whole == [apply_text_case(word, "upper") for word in text.split()]


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
    # DC1-6: an English contraction reaches the shared status vocabulary ("how's").
    Case(
        "DC1-6",
        "chat_routing",
        "kria.language.is_status_question",
        "status question (contraction)",
        _status_question,
        "how's it going?",
        True,
        {"tr": "Nasıl gidiyor?", "tr_mixed": "status ne?"},
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
    # DC1-1: a title hold stated as a number word ("two seconds") grounds the model's value.
    Case(
        "DC1-1",
        "brief_parsing",
        "routes.creator_agent._excerpt_states_seconds",
        "seconds as a number word",
        _states_two_seconds,
        "hold the title for two seconds",
        True,
        {
            "tr": "Başlık iki saniye kalsın",
            "tr_ascii": "baslik iki saniye kalsin",
            "tr_upper": "BAŞLIK İKİ SANİYE KALSIN",
        },
    ),
    # DC1-2: a generic sound-effect ask gets the "name a moment" notice on iPhone.
    Case(
        "DC1-2",
        "brief_parsing",
        "services.creator_sessions._GENERIC_SOUND_EFFECT_REQUEST_RE",
        "generic sound-effect ask",
        _generic_sfx_ask,
        "add some sound effects",
        True,
        {
            "tr": "Biraz ses efekti ekle",
            "tr_ascii": "biraz ses efekti ekle",
            "tr_upper": "BİRAZ SES EFEKTİ EKLE",
        },
    ),
    # DC1-3: a draft review that resets the photo/video timing drops the profile.
    Case(
        "DC1-3",
        "brief_parsing",
        "routes.plan_items._review_mixed_media_timing",
        "reset mixed-media timing",
        _review_timing_kept,
        "reset the timing",
        False,
        {
            "tr": "Zamanlamayı sıfırla",
            "tr_ascii": "zamanlamayi sifirla",
            "tr_upper": "ZAMANLAMAYI SIFIRLA",
            "tr_mixed": "timing'i sıfırla",
        },
    ),
    # DC1-4: "don't change the timing" keeps the profile.
    Case(
        "DC1-4",
        "brief_parsing",
        "routes.plan_items._review_mixed_media_timing",
        "keep mixed-media timing",
        _review_timing_kept,
        "don't change the timing",
        True,
        {
            "tr": "Zamanlamayı değiştirme",
            "tr_ascii": "zamanlamayi degistirme",
            "tr_upper": "ZAMANLAMAYI DEĞİŞTİRME",
        },
    ),
    # DC1-5: "don't make the photos very fast" retracts the fast-photo profile.
    Case(
        "DC1-5",
        "brief_parsing",
        "routes.plan_items._review_mixed_media_timing",
        "retract fast photos",
        _review_timing_kept,
        "don't make the photos very fast",
        False,
        {
            "tr": "Fotoğrafları hızlı geçirme",
            "tr_ascii": "fotograflari hizli gecirme",
            "tr_upper": "FOTOĞRAFLARI HIZLI GEÇİRME",
        },
    ),
    # DC1-7: a generic Turkish SFX ask names no effect ("videoya" is "to the video").
    Case(
        "DC1-7",
        "brief_parsing",
        "routes.creator_agent._explicit_sfx_name",
        "generic sound-effect ask is unnamed",
        _sfx_name,
        "add a sound effect to the video",
        None,
        {
            "tr": "Videoya ses efekti ekle",
            "tr_ascii": "videoya ses efekti ekle",
            "tr_upper": "VİDEOYA SES EFEKTİ EKLE",
            "tr_mixed": "komik yerlere sfx ekle",
        },
    ),
    # DC1-8: a passing mention of an effect is not a request for it.
    Case(
        "DC1-8",
        "brief_parsing",
        "routes.creator_agent._explicit_sfx_name",
        "declined or described effect is unnamed",
        _sfx_name,
        "use a different sound effect, not the whoosh sfx second time",
        None,
        {
            "tr": "Farklı bir ses efekti kullan, whoosh olmasın",
            "tr_ascii": "farkli bir ses efekti kullan, whoosh olmasin",
            "tr_mixed": "geçiş sfx sakin olsun",
        },
    ),
    # DC1-9: "don't forget to add" asks for the effect.
    Case(
        "DC1-9",
        "brief_parsing",
        "routes.creator_agent._explicit_sfx_name",
        "named sound effect (don't forget)",
        _sfx_name,
        "don't forget to add the whoosh sound effect",
        "whoosh",
        {
            "tr": "Whoosh ses efektini eklemeyi unutma",
            "tr_ascii": "whoosh ses efektini eklemeyi unutma",
            "tr_upper": "WHOOSH SES EFEKTİNİ EKLEMEYİ UNUTMA",
        },
    ),
    # DC1-10: "don't reset the timing" keeps the profile.
    Case(
        "DC1-10",
        "brief_parsing",
        "routes.plan_items._review_mixed_media_timing",
        "keep timing (negated reset)",
        _review_timing_kept,
        "don't reset the timing",
        True,
        {
            "tr": "Zamanlamayı sıfırlama",
            "tr_ascii": "zamanlamayi sifirlama",
            "tr_upper": "ZAMANLAMAYI SIFIRLAMA",
            "tr_mixed": "timing'i iptal etme",
        },
    ),
    # DC1-11: a refused title is not rendered.
    Case(
        "DC1-11",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "refused opening title",
        _opening_title,
        "don't use the title 'Emir'",
        None,
        {
            "tr": "'Emir' başlığını koyma",
            "tr_ascii": "'Emir' basligini koyma",
            "tr_upper": "'Emir' BAŞLIĞINI KOYMA",
            "tr_mixed": "title 'Emir' olmasın",
        },
    ),
    # DC1-12: copy placed at the end is the closing title.
    Case(
        "DC1-12",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "closing title from an end cue",
        _closing_title,
        "end title 'Görüşürüz'",
        "Görüşürüz",
        {
            "tr": "Sona 'Görüşürüz' yazısı ekle",
            "tr_ascii": "videonun sonunda 'Görüşürüz' yazisi olsun",
            "tr_upper": "SONA 'Görüşürüz' YAZISI EKLE",
            "tr_mixed": "'Görüşürüz' kapanış title olsun",
        },
    ),
    # DC1-13: quoted words in an edit to existing text are not a new title.
    Case(
        "DC1-13",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "edit of existing text is no title",
        _opening_title,
        "in the text replace 'Sen' with 'Siz'",
        None,
        {
            "tr": "Yazıda 'Sen' yerine 'Siz' kullan",
            "tr_ascii": "yazida 'Sen' yerine 'Siz' kullan",
            "tr_upper": "YAZIDA 'Sen' YERİNE 'Siz' KULLAN",
        },
    ),
    # DC1-14: digits with the Turkish seconds unit ground the model's value.
    Case(
        "DC1-14",
        "brief_parsing",
        "routes.creator_agent._excerpt_states_seconds",
        "seconds as digits",
        _states_two_seconds,
        "hold the title for 2 seconds",
        True,
        {
            "tr": "Başlık 2 sn kalsın",
            "tr_ascii": "baslik 2 sn kalsin",
            "tr_upper": "BAŞLIK 2 SANİYE KALSIN",
            "tr_mixed": "title 2sn kalsın",
        },
    ),
    # DC1-15: a status question with a subject ("is the video ready?").
    Case(
        "DC1-15",
        "chat_routing",
        "routes.creation_threads._is_status_only_message",
        "status question with a subject",
        _status_only,
        "is the video ready?",
        True,
        {
            "tr": "Video hazır mı?",
            "tr_ascii": "video hazirmi?",
            "tr_upper": "VİDEO HAZIR MI?",
            "tr_mixed": "render bitti mi?",
        },
    ),
    # DC1-16: "a fast pace" with the indefinite article.
    Case(
        "DC1-16",
        "brief_parsing",
        "routes.creator_agent._apply_explicit_render_intent",
        "fast pacing (indefinite)",
        _pacing,
        "a fast pace",
        "fast",
        {
            "tr": "Hızlı bir tempo olsun",
            "tr_ascii": "hizli bir tempo olsun",
            "tr_upper": "HIZLI BİR TEMPO OLSUN",
        },
    ),
    # DC1-17: "use every clip" widens media scope.
    Case(
        "DC1-17",
        "brief_parsing",
        "routes.creator_agent._explicit_media_scope",
        "use every clip",
        _media_scope,
        "use every clip",
        "all",
        {
            "tr": "Her klibi kullan",
            "tr_ascii": "her klibi kullan",
            "tr_upper": "HER KLİBİ KULLAN",
            "tr_mixed": "hiçbirini atlama",
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
    # O18 (KRI-780): a chat-driven plan item's hook is written in the chat's language.
    Case(
        "O18",
        "captions_transcript",
        "tasks.content_plan_build._item_chat_language",
        "hook written in the chat language",
        _hook_in_chat_language,
        "Make my trip video upbeat and add captions",
        True,
        {
            "tr": "Gezi videomu enerjik yap ve altyazı ekle",
            "tr_ascii": "gezi videomu enerjik yap ve altyazi ekle",
            "tr_upper": "GEZİ VİDEOMU ENERJİK YAP VE ALTYAZI EKLE",
            "tr_mixed": "Gezi videomu upbeat yap, captions ekle",
        },
    ),
    # O27 (KRI-780): spoken captions with no detectable language fall back to the chat's.
    Case(
        "O27",
        "captions_transcript",
        "pipeline.caption_language.resolve_spoken_caption_language",
        "caption fallback follows the chat language",
        _caption_fallback_in_chat_language,
        "Make my trip video upbeat and add captions",
        True,
        {
            "tr": "Gezi videomu enerjik yap ve altyazı ekle",
            "tr_ascii": "gezi videomu enerjik yap ve altyazi ekle",
            "tr_upper": "GEZİ VİDEOMU ENERJİK YAP VE ALTYAZI EKLE",
            "tr_mixed": "Gezi videomu upbeat yap, captions ekle",
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
    ),
    # ---------------------------------------------------------------- KRI-783 additions
    # C2-01: a reply that ASKS (not restates) is not a paraphrase.
    Case(
        "C2-01",
        "chat_routing",
        "kria.language.is_paraphrase_only",
        "question is not a paraphrase",
        _paraphrase_only,
        "Do you want the title gold or yellow?",
        False,
        {
            "tr": "Başlık altın mı sarı mı olsun istiyorsun?",
            "tr_ascii": "Baslik altin mi sari mi olsun istiyor musun?",
        },
    ),
    # C2-02: a model caption claiming taste or a first-person experience is ungrounded.
    Case(
        "C2-02",
        "brief_parsing",
        "agents.edit_proposal.ai_draft_thought_has_unsupported_claim",
        "ungrounded caption claim",
        _unsupported_claim,
        "A delicious coffee",
        True,
        {"tr": "Nefis bir kahve", "tr_ascii": "nefis bir kahve", "tr_upper": "NEFİS BİR KAHVE"},
    ),
    # C2-03: spoken numbers become a score label ("three one" -> 3-1).
    Case(
        "C2-03",
        "captions_transcript",
        "pipeline.narration_labels._score_value",
        "spoken score words",
        _spoken_score,
        "three one",
        (3, 1),
        {"tr": "üç bir", "tr_ascii": "uc bir", "tr_upper": "ÜÇ BİR"},
    ),
    # C2-05: a model refusal is never burned as the hook (or spoken as the script).
    Case(
        "C2-05",
        "captions_transcript",
        "agents.intro_writer._is_refusal_text",
        "model refusal rejected",
        _hook_refusal,
        "I need more information to write this hook",
        True,
        {
            "tr": "Bu kancayı yazmak için daha fazla bilgiye ihtiyacım var",
            "tr_ascii": "bu kancayi yazmak icin daha fazla bilgiye ihtiyacim var",
            "tr_upper": "DAHA FAZLA BİLGİYE İHTİYACIM VAR",
        },
    ),
    # C2-04: the editor receipt names a timing sub-ask ("the text should appear later").
    Case(
        "C2-04",
        "style_memory",
        "kria.editor_receipts._named_cues",
        "timing cue",
        _receipt_timing_cue,
        "the text should come in later",
        True,
        {
            "tr": "Yazı daha geç çıksın",
            "tr_ascii": "yazi daha gec ciksin",
            "tr_upper": "YAZI DAHA GEÇ ÇIKSIN",
        },
    ),
    # C2-06: a durable rule that FORBIDS a font is never saved as "always use it".
    Case(
        "C2-06",
        "style_memory",
        "services.creator_direction.infer_typed_direction",
        "negated durable font rule",
        _durable_font,
        "Always avoid the Inter font",
        (None, None),
        {
            "tr": "Artık Inter fontunu kullanma",
            "tr_ascii": "artik inter fontunu kullanma",
            "tr_upper": "ARTIK INTER FONTUNU KULLANMA",
            "tr_alt": "Bundan sonra Inter font istemiyorum",
        },
    ),
    # C2-07: a first-person experience on the verb ("denedim") is an ungrounded claim.
    Case(
        "C2-07",
        "brief_parsing",
        "agents.edit_proposal.ai_draft_thought_has_unsupported_claim",
        "first-person experience claim",
        _unsupported_claim,
        "I tried this coffee and loved it",
        True,
        {
            "tr": "Bu kahveyi denedim ve bayıldım",
            "tr_ascii": "bu kahveyi denedim ve bayildim",
            "tr_upper": "BU KAHVEYİ DENEDİM VE BAYILDIM",
        },
    ),
    # C2-08: a third-person action line (or a name like "Ben") is not a claim.
    Case(
        "C2-08",
        "brief_parsing",
        "agents.edit_proposal.ai_draft_thought_has_unsupported_claim",
        "third-person line is not a claim",
        _unsupported_claim,
        "Ben lands the kickflip",
        False,
        {
            "tr": "Kaan kickflipi yapıyor",
            "tr_ascii": "kaan kickflipi yapiyor",
            "tr_upper": "KAAN KİCKFLİPİ YAPIYOR",
        },
    ),
    # C2-09: an everyday "for more info" call to action is not a model refusal.
    Case(
        "C2-09",
        "captions_transcript",
        "agents.intro_writer._is_refusal_text",
        "call to action is not a refusal",
        _hook_refusal,
        "For more information, follow along",
        False,
        {
            "tr": "Daha fazla bilgi için takip et",
            "tr_ascii": "daha fazla bilgi icin takip et",
            "tr_upper": "DAHA FAZLA BİLGİ İÇİN TAKİP ET",
        },
    ),
    # C2-10: a negated score ask ("no scores") never burns a score overlay.
    Case(
        "C2-10",
        "captions_transcript",
        "tasks.generative_build._narrated_storyboard_text_elements",
        "negated score treatment",
        _narrated_score_overlay,
        "No scores please",
        False,
        {
            "tr": "Skor olmasın",
            "tr_ascii": "skor olmasin",
            "tr_upper": "SKORLARI GÖSTERME",
            "tr_alt": "Sonuçta güzel bir gün oldu",
        },
    ),
    # C2-11: a positive score ask does burn it.
    Case(
        "C2-11",
        "captions_transcript",
        "tasks.generative_build._narrated_storyboard_text_elements",
        "score treatment requested",
        _narrated_score_overlay,
        "show the scores",
        True,
        {
            "tr": "Skorları göster",
            "tr_ascii": "skorlari goster",
            "tr_upper": "SKORLARI GÖSTER",
            "tr_mixed": "Başlıksız olsun, score göster",
        },
    ),
    # C2-12: a reply that reports work and gives the reason last is not a paraphrase.
    Case(
        "C2-12",
        "chat_routing",
        "kria.language.is_paraphrase_only",
        "work report with a reason is not a paraphrase",
        _paraphrase_only,
        "I moved the coffee clips first because you wanted a calm opening.",
        False,
        {
            "tr": "Kahve kliplerini başa aldım, sakin bir açılış istedin.",
            "tr_ascii": "kahve kliplerini basa aldim, sakin bir acilis istedin.",
            "tr_upper": "KAHVE KLİPLERİNİ BAŞA ALDIM, SAKİN BİR AÇILIŞ İSTEDİN.",
        },
    ),
    # C2-13: a voiceover line the creator asks to put on screen stays on-screen copy.
    Case(
        "C2-13",
        "brief_parsing",
        "agents.spoken_script.is_spoken_script_quote",
        "voiceover quote with a display ask is copy",
        _voiceover_script,
        'VO: "today I show you my favorite cafe", put it on screen',
        False,
        {
            "tr": (
                'Seslendirmedeki "bugün sana en sevdiğim kafeyi gösteriyorum" cümlesini ekrana yaz'
            ),
            "tr_ascii": (
                'seslendirmedeki "bugun sana en sevdigim kafeyi gosteriyorum" cumlesini ekrana yaz'
            ),
            "tr_alt": '"bugün sana en sevdiğim kafeyi gösteriyorum" diyorum, bunu ekrana da yaz',
        },
    ),
    # C2-14: a surprise exclamation opening a hook is not a filler.
    Case(
        "C2-14",
        "captions_transcript",
        "pipeline.score._adjust_hook_score",
        "exclamation opener not penalized",
        _hook_filler_penalty,
        "Oh no, look at this",
        False,
        {"tr": "Aaa, şuna bak", "tr_ascii": "aaa, suna bak", "tr_upper": "AAA, ŞUNA BAK"},
    ),
    # C2-15: "only" this quote, in Turkish's other ways to say it.
    Case(
        "C2-15",
        "brief_parsing",
        "agents.edit_proposal._creator_quoted_captions_are_exhaustive",
        "exclusive quote (yalnız / bir tek / no other text)",
        _exclusive_quotes,
        'Only "Paris 2024" on screen',
        True,
        {
            "tr": 'Yalnız "Paris 2024" yazsın',
            "tr_alt": 'Bir tek "Paris 2024" yazsın',
            "tr_mixed": '"Paris 2024" yazısı olsun, başka yazı olmasın',
        },
    ),
    # C2-16: the spoken Turkish score form puts a dative on the first number.
    Case(
        "C2-16",
        "captions_transcript",
        "pipeline.narration_labels._score_value",
        "dative score words",
        _spoken_score,
        "three one",
        (3, 1),
        {"tr": "üçe bir", "tr_ascii": "uce bir", "tr_upper": "ÜÇE BİR"},
    ),
    # D50: a Turkish word that merely starts like a marker is not durable memory.
    Case(
        "D50",
        "style_memory",
        "services.creator_memory_learning.classify_memory_candidate",
        "no false durable marker (prefix words)",
        _memory_candidate,
        "add a lion roar to the video as usual",
        "noop",
        {
            "tr": "Her zamanki gibi videoya aslan sesi ekle",
            "tr_ascii": "her zamanki gibi videoya aslan sesi ekle",
            "tr_upper": "HER ZAMANKİ GİBİ VİDEOYA ASLAN SESİ EKLE",
        },
    ),
    # D51: natural Turkish wordings of a durable rule are explicit memory.
    Case(
        "D51",
        "style_memory",
        "services.creator_memory_learning.classify_memory_candidate",
        "durable memory (natural wordings)",
        _memory_candidate,
        "never use yellow",
        "explicit",
        {
            "tr": "Hiçbir zaman sarı kullanma",
            "tr_every": "her videoda Inter kullan",
            "tr_daima": "daima Inter fontu kullan",
            "tr_ascii": "bundan boyle Inter kullan",
            "tr_upper": "TÜM VİDEOLARDA INTER OLSUN",
        },
    ),
    # D52: "stop using / instead / forget" needs current-memory judgment.
    Case(
        "D52",
        "style_memory",
        "services.creator_memory_learning.requires_stateful_extraction",
        "stateful memory change",
        _stateful,
        "stop using this font",
        True,
        {
            "tr": "Artık bu fontu kullanma",
            "tr_instead": "Playfair yerine Inter kullan",
            "tr_ascii": "sariyi kullanmayi birak",
            "tr_upper": "FONTUMU DEĞİŞTİR",
        },
    ),
    # D53: a one-off style change in Turkish is not learned as a preference.
    Case(
        "D53",
        "style_memory",
        "services.finished_style_learning.explicitly_temporary",
        "temporary style change (one-off wordings)",
        _temporary,
        "make it yellow this time only",
        True,
        {
            "tr": "Bu seferlik sarı yap",
            "tr_only": "yalnızca bu videoda sarı yap",
            "tr_ascii": "bir seferlik sari yap",
            "tr_upper": "BU SEFERLİK SARI YAP",
        },
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
    ),
    # N08: on-screen UPPER case of Turkish text keeps the dotted capital İ.
    Case(
        "N08",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "text_case upper",
        _upper_auto,
        "İSTANBUL",
        "İSTANBUL",
        {"tr": "istanbul", "tr_title": "İstanbul"},
        gaps=_gaps(
            "renderers carry no job language (needs a schema field); a word with no "
            "ı/İ/ğ/ş is cased the English way",
            "tr",
        ),
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
    ),
    # N10: English on-screen casing never picks up Turkish i rules (KRI-781 guard).
    Case(
        "N10",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "English upper unchanged",
        _upper_auto,
        "TITLE",
        "TITLE",
        {"en_lower": "title", "en_title": "Title"},
    ),
    # N11: title case of Turkish text keeps İ/ı and never capitalizes after apostrophes.
    Case(
        "N11",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "text_case title",
        _title_auto,
        "İstanbul'da Işıklar",
        "İstanbul'da Işıklar",
        {"tr": "istanbul'da ışıklar", "tr_upper": "İSTANBUL'DA IŞIKLAR"},
        gaps=_gaps("renderers carry no job language: 'istanbul'da' has no ı/İ/ğ/ş", "tr"),
    ),
    # N12: narrated voiceover-to-script alignment keeps Turkish letters (folded).
    Case(
        "N12",
        "normalization",
        "pipeline.narrated_alignment._norm",
        "narrated alignment token fold",
        _narrated_norm,
        "guzel",
        "guzel",
        {"tr": "güzel", "tr_upper": "GÜZEL", "tr_title": "Güzel,"},
    ),
    # N13: clip-question intent keys fold every spelling alike.
    Case(
        "N13",
        "normalization",
        "services.clip_selection._words",
        "clip intent key words",
        _clip_intent_words,
        "isik",
        ("isik",),
        {"tr": "ışık", "tr_upper": "IŞIK", "tr_title": "Işık"},
    ),
    # N14: song-title matching tokens keep dotless ı (folded, never deleted).
    Case(
        "N14",
        "normalization",
        "services.lrclib_client._tokenize_title",
        "song title tokens",
        _lrclib_title_tokens,
        "kirmizi isiklar",
        frozenset({"kirmizi", "isiklar"}),
        {"tr": "Kırmızı Işıklar", "tr_upper": "KIRMIZI IŞIKLAR"},
    ),
    # N15: an English caption naming a Turkish place keeps English i's (KRI-781 guard).
    Case(
        "N15",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "English upper with a Turkish name",
        _upper_auto,
        "VISITING BEŞİKTAŞ IN THE CITY",
        "VISITING BEŞİKTAŞ IN THE CITY",
        {"en_lower": "visiting beşiktaş in the city", "en_title": "Visiting Beşiktaş In The City"},
    ),
    # N16: karaoke word-by-word casing equals whole-line casing (word timings survive).
    Case(
        "N16",
        "normalization",
        "agents._schemas.text_element.apply_text_case",
        "per-word casing composes",
        _karaoke_upper_composes,
        "light up the city",
        True,
        {"tr": "istanbul ışıkları", "tr_mixed": "bir şey istiyorum"},
    ),
    # N17: the lyric-lookup artist gate matches an artist typed without Turkish letters.
    Case(
        "N17",
        "normalization",
        "services.lrclib_client._artists_match",
        "song artist gate",
        _artist_gate,
        "Barış Manço",
        True,
        {"tr_ascii": "Baris Manco", "tr_upper": "BARIŞ MANÇO"},
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
