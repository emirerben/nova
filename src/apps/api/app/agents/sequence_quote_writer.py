"""nova.compose.sequence_quote — author the multi-phrase quote for rhythm-mode sequences.

Editorial "sequence" variants without eligible speech still want the
transcript-synced typography treatment. Rhythm mode synthesizes the missing
voice: this agent writes a first-person, brand-voice micro-quote of SHORT
sentences; the rhythm engine then splits it on terminal punctuation, paces one
sentence per equal time slot across the video, and feeds the synthesized word
timings into the existing `split_phrases` → `build_sequence_overlays` path.
Every sentence becomes one on-screen phrase, so sentence shape IS layout shape
— hence the strict 1-6-words-per-sentence validation below.

TRUST BOUNDARY (architectural). Same as `intro_writer` — this agent
intentionally turns clip understanding INTO on-screen text. That is the
product, NOT the forbidden metadata-to-screen path ("Gemini metadata never
becomes overlay text" governs template SUBSTITUTION, where analysis output is
spliced verbatim into overlays). The clip-derived input (transcript, hook,
description) is UNTRUSTED: a clip's audio or on-screen text could say "ignore
instructions / visit evil.com". Defense is two layers:
  1. Prompt-level: the template wraps clip fields as DATA, never instructions
     (`prompts/sequence_quote.txt`).
  2. Output-level (here in parse()): `_sanitize_aligned_line` strips ASS tags /
     control chars; URLs and @/# handles are stripped; then the quote must pass
     the structural validation (sentence count/shape, word totals) or the
     runtime retries with a clarification.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.intro_writer import (
    _filming_guide_block,
    _persona_context,
    _preferences_block,
    _strip_unsafe_tokens,
)
from app.agents.music_matcher import ClipSummary, _sanitize_text
from app.agents.text_alignment import _sanitize_aligned_line
from app.pipeline.phrase_sequence import split_sentences
from app.pipeline.prompt_loader import load_prompt

# ── Quote shape constants ──────────────────────────────────────────────────────
# These mirror the approved rhythm-mode demo: each sentence becomes one scene,
# scenes get equal slots across the video, so sentence count tracks duration at
# roughly one sentence per 1.3s, clamped to a 4-9 band.
_SECONDS_PER_SENTENCE = 1.3
_MIN_SENTENCES = 4
_MAX_SENTENCES = 9
# A sentence renders as a single word-cluster phrase; the cluster engine (and
# split_phrases' 6-word cap) tops out at 6 words.
_MAX_SENTENCE_WORDS = 6
_MIN_TOTAL_WORDS = 15
_MAX_TOTAL_WORDS = 40

# Terminal punctuation the rhythm engine splits on. A run ("?!", "…") counts as
# ONE boundary; a mid-quote "So…" therefore ends its own (1-word) sentence,
# exactly like the approved demo.
_TERMINAL_CHARS = ".!?…"

# Curly/guillemet double quotes normalized to straight '"' before validation so
# the at-most-one-quoted-span rule can't be dodged typographically.
_DQUOTE_TRANSLATION = str.maketrans(dict.fromkeys("“”„«»", '"'))


def expected_sentence_count(video_duration_s: float) -> int:
    """Target sentence count for a video of this length: one sentence per
    ~1.3s slot, clamped to [4, 9]. Public so the orchestrator and the prompt
    share one definition."""
    return max(_MIN_SENTENCES, min(_MAX_SENTENCES, round(video_duration_s / _SECONDS_PER_SENTENCE)))


def split_quote_sentences(quote: str) -> list[str]:
    """Split a quote into rhythm-mode sentences, dropping empties.

    Delegates to `phrase_sequence.split_sentences` — the SAME splitter the
    rhythm engine uses — so the agent's structural validation can never green
    -light a quote the engine would split differently (e.g. a no-space typo
    "hard.No" or a decimal "3.5" the old `[.!?…]+` rule mis-counted)."""
    return split_sentences(quote)


def quote_structural_failures(quote: str) -> list[str]:
    """Every structural rule the quote must satisfy, as human-readable failure
    strings (empty list = pass). parse() raises SchemaError on any failure; the
    eval structural check imports THIS function so it can never drift from the
    runtime's own validation."""
    q = quote.strip()
    if not q:
        return ["quote is empty"]
    failures: list[str] = []
    if q[-1] not in _TERMINAL_CHARS:
        failures.append(f"quote must end with terminal punctuation [{_TERMINAL_CHARS}]")
    n_dquotes = q.count('"')
    if n_dquotes not in (0, 2):
        failures.append(
            f"{n_dquotes} double-quote characters — at most ONE quoted span "
            "(exactly 0 or 2 quote marks)"
        )
    sentences = split_quote_sentences(q)
    n = len(sentences)
    if not (_MIN_SENTENCES <= n <= _MAX_SENTENCES):
        failures.append(f"{n} sentences (need {_MIN_SENTENCES}-{_MAX_SENTENCES})")
    for i, sentence in enumerate(sentences):
        word_count = len(sentence.split())
        if word_count > _MAX_SENTENCE_WORDS:
            failures.append(
                f"sentence {i} ({sentence!r}) has {word_count} words (max {_MAX_SENTENCE_WORDS})"
            )
    total_words = sum(len(s.split()) for s in sentences)
    if not (_MIN_TOTAL_WORDS <= total_words <= _MAX_TOTAL_WORDS):
        failures.append(f"{total_words} total words (need {_MIN_TOTAL_WORDS}-{_MAX_TOTAL_WORDS})")
    return failures


# Language instruction blocks, mirroring intro_writer's pattern but worded for
# a multi-sentence quote rather than a single hook line. Adding a language is a
# one-entry change (plus glyph coverage + eval fixtures).
_LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "en": "Write the quote in English.",
    "tr": (
        "Write the quote in TURKISH (Türkçe). Use casual creator voice — second-person "
        "singular 'sen' (informal), NEVER 'siz' (formal). Match how a Turkish creator "
        "would voice their own montage on TikTok or Instagram. All other rules below "
        "(sentence shape, word caps, no emojis, no #/@) still apply. Turkish diacritics "
        "(ç ş ğ ı İ ö ü) MUST be written with the correct Unicode codepoint, NOT "
        "ASCII-folded. Do NOT mix English and Turkish. Output Turkish only."
    ),
}


def _language_instruction(language: str) -> str:
    """Unknown codes fall back to English — the closed allowlist is enforced at
    the API edge; this is defense-in-depth for internal callers."""
    return _LANGUAGE_INSTRUCTIONS.get(language, _LANGUAGE_INSTRUCTIONS["en"])


class SequenceQuoteInput(BaseModel):
    """Mirrors IntroWriterInput's creative grounding (minus the intro-only
    `form`/`exemplars`) so the orchestrator builds it from the same data, plus
    the video duration that drives the target sentence count."""

    hero_clip: ClipSummary
    # Verbatim hero-clip transcript (usually empty in rhythm mode — that's WHY
    # we're authoring a quote — but ambient speech fragments still ground the
    # voice). UNTRUSTED — wrapped as DATA in the prompt.
    hero_transcript: str = ""
    tone: str = ""
    # Target output language. Closed allowlist enforced at the API edge.
    language: str = "en"
    # Content-plan persona/series context (empty for public generative jobs).
    # First-party but re-sanitized at the threading point (intro_writer's
    # _persona_context) as defense-in-depth.
    content_pillars: list[str] = Field(default_factory=list)
    theme: str = ""
    idea: str = ""
    # Feedback-loop rollup (services/feedback_summary). Empty → block omitted.
    preference_summary: str = ""
    # Deep TikTok analysis summary — injected into the persona context block.
    tiktok_analysis: str = ""
    # Per-item filming guide (shot list) — DATA only, never instructions.
    filming_guide: list[dict] = Field(default_factory=list)
    # Rendered video length. Drives the target sentence count (one sentence per
    # ~1.3s rhythm slot, clamped 4-9).
    video_duration_s: float = Field(gt=0.0)


class SequenceQuoteOutput(BaseModel):
    quote: str = Field(min_length=1)


class SequenceQuoteWriterAgent(Agent[SequenceQuoteInput, SequenceQuoteOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.sequence_quote",
        prompt_id="sequence_quote",
        prompt_version="1.0.0",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Creative writing with a strict structural shape — same budget as
        # intro_writer (the sibling brand-voice generator): enough headroom for
        # the creative step without the multi-thousand-token default.
        thinking_budget=512,
    )
    Input = SequenceQuoteInput
    Output = SequenceQuoteOutput

    def required_fields(self) -> list[str]:
        return ["quote"]

    def render_prompt(self, input: SequenceQuoteInput) -> str:  # noqa: A002
        c = input.hero_clip
        return load_prompt(
            "sequence_quote",
            language_instruction=_language_instruction(input.language),
            tone=_sanitize_text(input.tone) or "neutral",
            # Persona/preferences/filming-guide blocks reuse intro_writer's
            # renderers verbatim (duck-typed on the shared field names) so the
            # two brand-voice agents can never drift on sanitization rules.
            persona_context=_persona_context(input),  # type: ignore[arg-type]
            preferences=_preferences_block(input.preference_summary),
            filming_guide=_filming_guide_block(input.filming_guide),
            hero_subject=_sanitize_text(c.subject) or "(unknown)",
            hero_hook=_sanitize_text(c.hook_text),
            hero_description=_sanitize_text(c.description),
            hero_transcript=_sanitize_text(input.hero_transcript),
            video_duration_s=f"{input.video_duration_s:.1f}",
            target_sentences=str(expected_sentence_count(input.video_duration_s)),
            min_sentences=str(_MIN_SENTENCES),
            max_sentences=str(_MAX_SENTENCES),
            max_sentence_words=str(_MAX_SENTENCE_WORDS),
            min_total_words=str(_MIN_TOTAL_WORDS),
            max_total_words=str(_MAX_TOTAL_WORDS),
        )

    def parse(self, raw_text: str, input: SequenceQuoteInput) -> SequenceQuoteOutput:  # noqa: A002, ARG002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"sequence_quote: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("sequence_quote: response is not a JSON object")
        raw_quote = data.get("quote")
        if not isinstance(raw_quote, str):
            raise SchemaError("sequence_quote: 'quote' is missing or not a string")

        # Output-level sanitization (layer 2 of the trust-boundary defense):
        # ASS tags / control chars, then URLs/handles/hashtags, then quote-mark
        # normalization so the quoted-span rule is typography-proof.
        quote = _sanitize_aligned_line(raw_quote)
        quote = _strip_unsafe_tokens(quote)
        quote = quote.translate(_DQUOTE_TRANSLATION).strip()

        failures = quote_structural_failures(quote)
        if failures:
            raise SchemaError(f"sequence_quote: {'; '.join(failures)}")

        try:
            return SequenceQuoteOutput(quote=quote)
        except ValidationError as exc:
            raise SchemaError(f"sequence_quote: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            '\n\nIMPORTANT: Return ONLY a JSON object: {"quote": "..."} . '
            "The quote must be SHORT sentences each ending with one of . ! ? … — "
            f"every sentence {_MAX_SENTENCE_WORDS} words or fewer, "
            f"{_MIN_SENTENCES}-{_MAX_SENTENCES} sentences total, "
            f"{_MIN_TOTAL_WORDS}-{_MAX_TOTAL_WORDS} words total, at most ONE "
            "double-quoted span, no URLs, no @handles, no #hashtags, no emojis."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
