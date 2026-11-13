"""The one table of model facts: provider, thinking levels, the degrade ladder and token price.

Every module that needs a fact about a model reads it here, and no other module spells a
non-Gemini model name (``tests/agents/test_model_registry_sentinel.py``). A name resolves to
the registered entry it starts with, longest first, so a provider-reported version such as
``gemini-3.1-pro-preview-0915`` or a dated snapshot keeps its family's price. An unregistered
``gemini`` name gets the most expensive Google rate, so a new alias cannot under-reserve.
Requested non-Google models must match their entry exactly (``exact_spec``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, NamedTuple

Provider = Literal["google", "openai", "anthropic"]

SOL = "gpt-6.1-sol"
SONNET = "claude-sonnet-5-5"

# Named here so no other module spells them. Whisper and the generated-media models settle a
# fixed or per-minute cost instead of token rates; ``audio_minute_usd`` prices Whisper. The
# caption correction and translation default is a registered token-priced text model.
WHISPER = "whisper-1"
CAPTION_CORRECTION = "gpt-4o"
IMAGE_MODEL = "gpt-image-2.5-sunburst"
NARRATION_MODEL = "gpt-realtime-2.1-mini"

# Every thinking-level word any provider uses, lightest first.
_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


class Rates(NamedTuple):
    """USD per one million tokens."""

    input: Decimal
    output: Decimal
    cached: Decimal


@dataclass(frozen=True, slots=True)
class TokenPrice:
    standard: Rates
    # (prompt tokens above which, the rates then).
    long_context: tuple[int, Rates] | None = None
    # (uncached, cached) rates that replace the text input rates for audio prompt tokens.
    audio_input: tuple[Decimal, Decimal] | None = None
    # Replaces the output rate for video output tokens.
    video_output: Decimal | None = None
    # (5-minute, 1-hour) TTL cache-write rates added on top of the input rate.
    cache_write_surcharge: tuple[Decimal, Decimal] | None = None
    # USD per grounded search request (charged at the paid tier even when free quota exists).
    grounded_search_usd: Decimal | None = None

    def rates(self, prompt_tokens: int) -> Rates:
        if self.long_context is not None and prompt_tokens > self.long_context[0]:
            return self.long_context[1]
        return self.standard


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    provider: Provider
    # Thinking levels the provider accepts; empty = the model takes no named level.
    efforts: tuple[str, ...]
    # Heaviest to lightest. A provider deadline steps one rung down on the same model; an
    # output truncation, and a fallback model after a deadline, run on the last rung.
    effort_ladder: tuple[str, ...]
    price: TokenPrice
    # Accepts the runtime's uploaded-media reference (a Gemini File API URI).
    supports_media: bool
    # Read by typed tool calling (KRI-1393).
    supports_tools: bool


def _r(i: str, o: str, c: str) -> Rates:
    return Rates(Decimal(i), Decimal(o), Decimal(c))


# USD per minute of audio, for models billed by duration. Not in ``_SPECS``: an entry there
# would route Whisper to the text providers (``provider_of``), and it takes audio, not a prompt.
# OpenAI's Whisper page, verified 2026-10-11.
_AUDIO_MINUTE_USD = {WHISPER: Decimal("0.006")}

_GOOGLE_CEILING = TokenPrice(_r("4.00", "18.00", "0.40"))
_GEMINI3_EFFORTS = ("minimal", "low", "medium", "high")
_GEMINI3_LADDER = ("high", "medium", "low")
_GROUNDED = Decimal("0.035")


def _gemini3(name: str, price: TokenPrice, efforts: tuple[str, ...] = _GEMINI3_EFFORTS):
    return ModelSpec(name, "google", efforts, _GEMINI3_LADDER, price, True, True)


def _gemini(name: str, price: TokenPrice, *, tools: bool = True) -> ModelSpec:
    return ModelSpec(name, "google", (), (), price, True, tools)


# Gemini rates: the official Developer API paid tier, 2026-09-04 (3.6-3.8 Flash are
# promotional through 2026-12-31). OpenAI and Anthropic rates: the providers' price pages,
# 2026-11-02. gpt-4o ($2.50 / $10.00 / $1.25 cached per million tokens) is the published list
# rate, added 2026-11-13 and still to be confirmed against the price page; reservations for
# it carry ``openai_metering.PRICE_VERSION``, so the intent-review preregistration that pins
# ``ai_cost_control.PRICE_VERSION`` stays valid. A change to a rate already here needs a new
# ``ai_cost_control.PRICE_VERSION``.
_SPECS = (
    ModelSpec(
        SOL,
        "openai",
        ("low", "medium", "high", "xhigh"),
        ("xhigh", "high", "medium", "low"),
        TokenPrice(_r("2", "10", ".10"), long_context=(272_000, _r("4", "15", ".20"))),
        supports_media=False,
        supports_tools=True,
    ),
    ModelSpec(
        SONNET,
        "anthropic",
        ("low", "medium", "high", "xhigh", "max"),
        ("max", "xhigh", "high", "medium", "low"),
        TokenPrice(_r("2", "10", ".10"), cache_write_surcharge=(Decimal(".5"), Decimal("2"))),
        supports_media=False,
        supports_tools=True,
    ),
    ModelSpec(
        CAPTION_CORRECTION,
        "openai",
        (),
        (),
        TokenPrice(_r("2.50", "10.00", "1.25")),
        supports_media=False,
        supports_tools=False,
    ),
    _gemini3(
        "gemini-3.1-pro",
        TokenPrice(
            _r("2.00", "12.00", "0.20"), long_context=(200_000, _r("4.00", "18.00", "0.40"))
        ),
        efforts=("low", "medium", "high"),
    ),
    _gemini3("gemini-3.8-flash", TokenPrice(_r("0.75", "3.75", "0.075"))),
    _gemini3("gemini-3.7-flash", TokenPrice(_r("0.75", "3.75", "0.075"))),
    _gemini3("gemini-3.6-flash", TokenPrice(_r("0.75", "3.75", "0.075"))),
    _gemini3("gemini-3.5-flash", TokenPrice(_r("1.50", "9.00", "0.15"))),
    _gemini3("gemini-3-flash", TokenPrice(_r("0.50", "3.00", "0.05"))),
    _gemini3("gemini-3", _GOOGLE_CEILING),
    _gemini(
        "gemini-2.5-pro",
        TokenPrice(
            _r("1.25", "10.00", "0.125"),
            long_context=(200_000, _r("2.50", "15.00", "0.25")),
            grounded_search_usd=_GROUNDED,
        ),
    ),
    _gemini(
        "gemini-2.5-flash-lite",
        TokenPrice(
            _r("0.10", "0.40", "0.01"),
            audio_input=(Decimal("0.30"), Decimal("0.03")),
            grounded_search_usd=_GROUNDED,
        ),
    ),
    _gemini(
        "gemini-2.5-flash",
        TokenPrice(
            _r("0.30", "2.50", "0.03"),
            audio_input=(Decimal("1.00"), Decimal("0.10")),
            grounded_search_usd=_GROUNDED,
        ),
    ),
    _gemini(
        "gemini-2.5",
        TokenPrice(_GOOGLE_CEILING.standard, grounded_search_usd=_GROUNDED),
        tools=False,
    ),
    _gemini(
        "gemini-omni-flash",
        TokenPrice(_r("1.50", "9.00", "1.50"), video_output=Decimal("17.50")),
        tools=False,
    ),
    _gemini("gemini", _GOOGLE_CEILING, tools=False),
)
_BY_LENGTH = sorted(_SPECS, key=lambda spec: len(spec.name), reverse=True)


def model_spec(name: str | None) -> ModelSpec | None:
    """The entry a requested or provider-reported model name belongs to, or None."""

    lowered = (name or "").strip().lower()
    return next((spec for spec in _BY_LENGTH if lowered.startswith(spec.name)), None)


def exact_spec(name: str) -> ModelSpec | None:
    """The entry registered under exactly this name (a configured non-Google model)."""

    return next((spec for spec in _SPECS if spec.name == name), None)


def audio_minute_usd(name: str) -> Decimal | None:
    """USD per audio minute for a duration-priced model (Whisper), or None."""

    return _AUDIO_MINUTE_USD.get((name or "").strip().lower())


def provider_of(name: str | None) -> Provider | None:
    spec = model_spec(name)
    return spec.provider if spec is not None else None


def supported_effort(name: str, level: str | None) -> str | None:
    """``level`` in this model's vocabulary: unchanged when accepted, else the closest lighter one.

    A fallback model inherits the primary's configured level, which can be a word it does not
    take (Sol's "xhigh" on Gemini). A model without named levels gets ``level`` unchanged.
    """

    spec = model_spec(name)
    if level is None or spec is None or not spec.efforts:
        return level
    wanted = level.strip().lower()
    if wanted in spec.efforts:
        return wanted
    rank = _EFFORT_ORDER.index(wanted) if wanted in _EFFORT_ORDER else len(_EFFORT_ORDER)
    lighter = [e for e in spec.efforts if _EFFORT_ORDER.index(e) < rank]
    return lighter[-1] if lighter else spec.efforts[0]
