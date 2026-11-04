"""``hook``: an agent-written opening hook, shown whole on its card. The tap IS the approval.

Two things share this kind:

* the KRI-756 chip (``SPEC``): carries no wording, its tap asks the planner for a hook and the
  creative-copy flow asks for approval. Untouched, and still the only thing a first prompt gets:
  with no draft there is nothing for a card to apply, so ``first_prompt`` stays prompt-only.
* the v2 card (``build_action`` / ``witness``): ``ready`` only. The agent's ``copy`` becomes a
  ``CopyDecisionAction`` the creator reads on the card; a tap applies exactly that text through
  ``set_title`` (``set_title_op``) and records a creative-copy decision with
  ``source="suggestion"`` (``creative_copy_decisions.suggestion_resolution``).

Hard rules, each pinned by ``tests/evals/test_suggestion_kind_hook.py``:

* The copy is never in the card's chat ``prompt``. A tap sends that prompt as the creator's own
  message, and wording the creator quoted back to the server skips the approval question
  (``creator_quoted``, KRI-682). The prompt stays a closed server sentence.
* **Gemini metadata never becomes on-screen text.** The text on a card is the agent's, checked
  against every string the footage analysis wrote: no run of three words from a transcript or any
  Gemini sentence, no whole line of either, no two-word run or single short sign from on-screen
  text or a brand. Nothing in this module derives words from the footage; ``copy=None`` builds
  nothing.
* The creator's request wins (KRI-129) and consent is the creator's (KRI-506): a title in the
  edit, a live title / text requirement in the brief, or any creative-copy decision (approved,
  declined, in discussion) hides the card. The module cannot see those from the editor snapshot
  alone, so the loader hands them over in ``ctx.kind_inputs["hook"]`` (``hook_inputs``) and a
  context without them offers nothing (fail closed).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.services.suggestion_chip_copy import language_of, shares_run, validate_copy
from app.services.suggestions.builders import PHONE_AND_CLOUD, legacy_kind
from app.services.suggestions.context import (
    SuggestionContext,
    Target,
    can_draft,
    families,
    ground_none,
    title_bar,
)
from app.services.suggestions.models import (
    Action,
    ChipKind,
    ChipKindSpec,
    CopyDecisionAction,
    WitnessResult,
)
from app.text_fold import Marker, has_marker, loose_words

INPUTS_KEY = "hook"
# The creative-copy target a hook decision writes (both ``CopyDecisionAction`` roles, "hook" and
# "title", mean the opening title). A suggestion only ever opens the video.
OPENING_TARGET = "opening_title"
MAX_WORDS = 8
_MAX_TEXTS = 200
_MAX_TEXT_CHARS = 2000

# Quotation marks and guillemets never appear in on-screen hook copy, in any language.
_QUOTE_CHARS = frozenset('"“”„‟«»‹›`')
# An apostrophe is a quote unless it sits inside a word ("don't", "İstanbul'da").
_INNER_APOSTROPHE = re.compile(r"(?<=\w)['’](?=\w)")
_APOSTROPHES = frozenset("'‘’‚‛")

# Anti-cringe, engagement bait and prompt-injection residue (the edit-improver skill's rules, made
# hard). Matched on the loose (Turkish-aware, accent-free) fold from a word start, so the Turkish
# entries are written without diacritics. Phrases, not bare words, where the bare word is also an
# ordinary filmable moment ("piano lesson", "train journey", "grinding the beans").
_CRINGE: tuple[str | Marker, ...] = (
    "nobody tells you",
    "no one tells you",
    "nobody talks about",
    "no one talks about",
    "you won't believe",
    "you wont believe",
    "you will not believe",
    "wait for it",
    "wait till the end",
    "link in bio",
    "follow me",
    "follow for",
    "follow along",
    "subscribe",
    "like and share",
    "comment below",
    "click here",
    "dm me",
    "swipe up",
    "buy now",
    "limited time",
    "life lesson",
    "lessons i",
    "what i learned",
    "the secret to",
    "the secret of",
    "secrets of",
    "my journey",
    "my transformation",
    "changed my life",
    "life changing",
    "life-changing",
    "rise and grind",
    "hustle culture",
    "hustle harder",
    "side hustle",
    "mindset",
    "storytime",
    "story time",
    "pov",
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "previous instructions",
    "system prompt",
    "as an ai",
    "language model",
    "ai generated",
    "chatgpt",
    "openai",
    # Turkish
    "abone ol",
    "takip et",
    "linke tikla",
    "biyografide",
    "yorum yap",
    "hemen satin al",
    "inanamayacaksin",
    "kimse soylemiyor",
    "kimse sana soylemiyor",
    "kimse bahsetmiyor",
    "sirri",
    "hayat dersi",
    "hayatimi degistirdi",
    "onceki talimat",
    "talimatlari yok say",
    "yapay zeka",
    "sonunu bekle",
)

# Unambiguous function words of the OTHER language, used only to reject copy in the wrong language
# (the skill already asks the agent for the creator's language). Folded like the text they test, so
# a word that is also a word of the thread's own language is left out: "iş" folds to "is", Turkish
# "on" / "at" / "not" / "has" are a numeral, an animal, a note and "genuine", and English "once"
# is the fold of "önce".
_TR_ONLY = frozenset(
    "bir ve bu icin ile gibi cok daha ama degil kadar sonra nasil neden yok".split()
)
_EN_ONLY = frozenset(
    "the and you your with this that are was but what when how why from have just about "
    "into before after of to in my we our for".split()
)


# ── What the loader hands over ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class HookInputs:
    """``ctx.kind_inputs["hook"]``: what only the thread and the clip records can say.

    ``opening_copy`` is the creative-copy status of the opening title ("" = no decision at all);
    every non-empty value hides the card, because each one is the creator's word (approved,
    skipped, in discussion, stale for other footage). The three text buckets are everything the
    footage analysis wrote in words: the transcripts, the Gemini sentences (subject, summary,
    setting, activity, props, actions, moments, notes) and the on-screen text and brands.
    """

    title_requirement: bool
    opening_copy: str
    transcripts: tuple[str, ...] = ()
    general: tuple[str, ...] = ()
    on_screen: tuple[str, ...] = ()


def _bucket(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        return None
    return tuple(v[:_MAX_TEXT_CHARS] for v in value if v.strip())[:_MAX_TEXTS]


def read_inputs(ctx: SuggestionContext) -> HookInputs | None:
    """The loader's hook inputs, or None when absent or malformed (the card is then hidden)."""
    raw = ctx.kind_inputs.get(INPUTS_KEY) if isinstance(ctx.kind_inputs, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    requirement, opening = raw.get("title_requirement"), raw.get("opening_copy")
    if not isinstance(requirement, bool) or not isinstance(opening, str):
        return None
    transcripts = _bucket(raw.get("transcripts", ()))
    general = _bucket(raw.get("general", ()))
    on_screen = _bucket(raw.get("on_screen", ()))
    if transcripts is None or general is None or on_screen is None:
        return None
    return HookInputs(requirement, opening, transcripts, general, on_screen)


def _wants_text(req: Any) -> bool:
    """A live requirement that already says something about the video's text (KRI-129)."""
    kind, scope = getattr(req, "kind", None), getattr(req, "scope", None)
    return (kind == "text" and scope in {"title", "global"}) or kind == "text_sequence"


def _record_texts(record: Any) -> tuple[list[str], list[str], list[str]]:
    """``(transcripts, general, on_screen)`` strings of one ``ClipUnderstanding``-shaped record."""

    def one(value: object) -> list[str]:
        return [value] if isinstance(value, str) and value.strip() else []

    def many(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str) and v.strip()]

    speech = getattr(record, "speech", None)
    transcripts = one(getattr(speech, "transcript", None))
    for segment in getattr(speech, "segments", None) or []:
        transcripts += one(getattr(segment, "text", None))
    general: list[str] = []
    for key in ("subject", "summary", "setting", "activity", "visibility", "visual_quality"):
        general += one(getattr(record, key, None))
    for key in ("props", "actions"):
        general += many(getattr(record, key, None))
    general += one(getattr(getattr(record, "people", None), "note", None))
    for moment in getattr(record, "notable_moments", None) or []:
        general += one(getattr(moment, "description", None))
    on_screen = one(getattr(record, "on_screen_text", None)) + many(getattr(record, "brands", None))
    return transcripts, general, on_screen


def hook_inputs(
    *, brief: Any, copy_states: Mapping[str, Any] | None, records: Iterable[Any]
) -> dict[str, Any]:
    """``ctx.kind_inputs["hook"]`` from the thread's brief, creative-copy states and clip records.

    ``copy_states`` must come from ``fold_creative_copy(..., include_suggestions=True)``: a
    suggestion the creator already tapped is a decision too. ``brief`` is a ``CreativeBrief`` or
    None (no brief, no requirement); ``records`` are ``ClipUnderstanding`` objects. Pure; the
    loader owns the reads.
    """
    state = (copy_states or {}).get(OPENING_TARGET)
    transcripts: list[str] = []
    general: list[str] = []
    on_screen: list[str] = []
    for record in records:
        t, g, o = _record_texts(record)
        transcripts += t
        general += g
        on_screen += o
    live = getattr(brief, "live", None)
    requirements = list(live()) if callable(live) else []
    return {
        "title_requirement": any(_wants_text(req) for req in requirements),
        "opening_copy": str(getattr(state, "status", "") or "") if state is not None else "",
        "transcripts": transcripts[:_MAX_TEXTS],
        "general": general[:_MAX_TEXTS],
        "on_screen": on_screen[:_MAX_TEXTS],
    }


# ── The copy gate ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Sources:
    transcripts: tuple[str, ...]
    general: tuple[str, ...]
    on_screen: tuple[str, ...]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _sources(ctx: SuggestionContext, inputs: HookInputs | None) -> _Sources:
    """Every string the footage analysis wrote that this context can see.

    The clip rows and the loader's inputs overlap on purpose: a row that already carries
    ``on_screen_text`` (the replay fixtures do) is checked even when the loader forgot to hand
    it over, and the loader's inputs reach fields no row exposes (summary, activity, brands).
    """
    served = ctx.kind_inputs.get("transcripts") if isinstance(ctx.kind_inputs, Mapping) else None
    transcripts = (
        [t for t in served.values() if isinstance(t, str)] if isinstance(served, Mapping) else []
    )
    general: list[str] = []
    on_screen: list[str] = []
    for clip in ctx.clips:
        if not isinstance(clip, Mapping):
            continue
        for key in ("subject", "setting", "summary", "activity", "props", "actions"):
            general += _strings(clip.get(key))
        for moment in clip.get("notable_moments") or []:
            if isinstance(moment, Mapping):
                general += _strings(moment.get("label") or moment.get("description"))
        on_screen += _strings(clip.get("on_screen_text")) + _strings(clip.get("brands"))
        transcripts += _strings(clip.get("transcript"))
    if inputs is not None:
        transcripts += inputs.transcripts
        general += inputs.general
        on_screen += inputs.on_screen
    return _Sources(tuple(transcripts), tuple(general), tuple(on_screen))


def _has_quote(text: str) -> bool:
    if any(ch in _QUOTE_CHARS for ch in text):
        return True
    return any(ch in _APOSTROPHES for ch in _INNER_APOSTROPHE.sub("", text))


def _wrong_language(text: str, language: str) -> bool:
    words = set(loose_words(text))
    if language == "tr":
        # Turkish is Latin script: another script inside it is a lookalike, not a Turkish word.
        if any(ch.isalpha() and "LATIN" not in unicodedata.name(ch, "") for ch in text):
            return True
        return bool(words & _EN_ONLY)
    return bool(words & _TR_ONLY)


def _lifts_on_screen_text(text: str, on_screen: Iterable[str]) -> bool:
    """Words read off the screen come back out: a two-word run, or the one word of a short sign."""
    mine = set(loose_words(text))
    for source in on_screen:
        words = loose_words(source)
        if len(words) == 1 and len(words[0]) >= 4 and words[0] in mine:
            return True
    return shares_run(text, on_screen, n=2)


def check_copy(copy: object, ctx: SuggestionContext) -> tuple[str | None, str | None]:
    """``(text, None)`` when ``copy`` may be shown as the hook, else ``(None, reason)``.

    Idempotent: a checked text checks again unchanged, so the build step, the witness and a
    rebuild of a stored card all run the same gate.
    """
    if not isinstance(copy, str):
        return None, "empty"
    text = " ".join(copy.split())
    if any(ch.isdigit() for ch in text):
        return None, "digit"
    if _has_quote(text):
        return None, "quote"
    if len(text.split()) > MAX_WORDS:
        return None, "words"
    sources = _sources(ctx, read_inputs(ctx))
    language = language_of(ctx.language)
    checked, reason = validate_copy(
        text,
        language=language,
        transcripts=sources.transcripts,
        footage=[*sources.general, *sources.on_screen],
    )
    if checked is None:
        return None, reason
    if _wrong_language(checked, language):
        return None, "language"
    if has_marker(checked, _CRINGE):
        return None, "cringe"
    if _lifts_on_screen_text(checked, sources.on_screen):
        return None, "reused_on_screen_text"
    return checked, None


# ── Whether the card may exist at all ────────────────────────────────────────────────


def hidden_reason(ctx: SuggestionContext) -> str | None:
    """Why no hook card may be offered for ``ctx``, or None when one may."""
    if ctx.state != "ready":
        return "not_ready"  # first prompt: no draft to apply a title to; the chip stays prompt-only
    if ctx.snapshot is None:
        return "no_snapshot"
    if "title" not in families(ctx):
        return "no_title_family"
    if title_bar(ctx) is not None:
        return "title_present"
    inputs = read_inputs(ctx)
    if inputs is None:
        return "no_inputs"
    if inputs.title_requirement:
        return "title_requested"
    if inputs.opening_copy:
        return "copy_decided"
    return None


# ── The v2 card ──────────────────────────────────────────────────────────────────────


def set_title_op(action: CopyDecisionAction) -> dict[str, Any]:
    """The one editor op a tap applies: the card's exact text as the opening title."""
    return {"op": "set_title", "title": action.text}


def build_action(
    ctx: SuggestionContext, targets: Sequence[Target], copy: str | None
) -> Action | None:
    """The agent's ``copy`` as a decision, or None. Nothing here reads words from the footage."""
    if copy is None or hidden_reason(ctx) is not None:
        return None
    text, _reason = check_copy(copy, ctx)
    if text is None:
        return None
    try:
        return CopyDecisionAction(role="hook", text=text)
    except ValidationError:
        return None


def _applied_text(parsed: Mapping[str, Any]) -> object:
    return parsed.get("title") if "title" in parsed else parsed.get("text")


def witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    """The card's text passes the gate again and ``set_title`` with it compiles to a real change.

    The copilot parser sees the text first: if it would clean, cut or rewrite it, the applied
    title would differ from the one on the card, and the creator approved the card's.
    """
    if not isinstance(action, CopyDecisionAction):
        return WitnessResult(ok=False, tier="copy", reason="not_copy_decision")
    why = hidden_reason(ctx)
    if why is not None:
        return WitnessResult(ok=False, tier="copy", reason=why)
    text, reason = check_copy(action.text, ctx)
    if text is None or text != action.text:
        return WitnessResult(ok=False, tier="copy", reason=f"copy_{reason or 'changed'}")
    if ctx.editor_witness is None:
        return WitnessResult(ok=False, tier="copy", reason="no_witness")
    op = set_title_op(action)
    try:
        from app.agents.edit_copilot import (  # noqa: PLC0415
            EditorOperationParseState,
            parse_editor_operation,
        )

        parsed = parse_editor_operation(
            dict(op), dict(ctx.snapshot or {}), EditorOperationParseState(1.0)
        )
    except Exception:  # noqa: BLE001 - a parser crash proves nothing
        return WitnessResult(ok=False, tier="copy", reason="op_refused")
    if not parsed or _applied_text(parsed) != action.text:
        return WitnessResult(ok=False, tier="copy", reason="title_changed")
    proven = ctx.editor_witness(op)
    if proven is None:
        return WitnessResult(ok=False, tier="copy", reason="op_refused")
    return WitnessResult(ok=True, tier="copy", touched=proven)


# ── The legacy chip (unchanged) ──────────────────────────────────────────────────────


def _available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return can_draft(ctx)
    return "title" in families(ctx)


def _present(ctx: SuggestionContext) -> bool:
    # first_prompt: nothing is in the edit yet. ready: any title bar already says something.
    return ctx.state == "ready" and title_bar(ctx) is not None


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    # A PROBE: proves a title CAN be set on this edit. The chip itself routes into the
    # creative-copy flow and carries no wording; set_title is in CHIP_OP_WITNESS_PROBES.
    return {"op": "set_title", "title": "Hook"}


SPEC = ChipKindSpec(
    kind=ChipKind.HOOK,
    states=frozenset({"first_prompt", "ready"}),
    capability={"first_prompt": "dispatch_render", "ready": "family:title"},
    available=_available,
    already_present=_present,
    ground=ground_none,
    dry_run={"first_prompt": "strategy", "ready": "editor"},
    expects="an opening hook, through the creative-copy approval flow",
    op=_op,
    witness_op="set_title",
    edit_tag="title",
)

KIND = legacy_kind(
    SPEC,
    surfaces=PHONE_AND_CLOUD,
    build_action=build_action,
    witness=witness,
    flag="kria_suggestion_hook_copy_enabled",
)
