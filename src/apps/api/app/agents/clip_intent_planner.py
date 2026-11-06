"""Extract complete, source-backed clip operations from creator chat text."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from hashlib import sha256
from typing import Any, ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.kria.reply_language import current_reply_language, prompt_language_line
from app.kria.speed_ask import says_clip_speed
from app.kria.text_trim import trim_at_word
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import (
    CREATOR_CAPTION_MAX_CHARS,
    CREATOR_LABEL_STORAGE_MAX,
    INTENT_ID_MAX_CHARS,
    MAX_CLIP_INTENTS,
    ClipIntent,
    ClipIntentOp,
)
from app.services.order_mode import key_order_mode

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?i)(^|[\s.;!?])(system|assistant|user|tool|developer)\s*[:>]")
_FENCE = re.compile(r"```+")

# Rendered onto the `order` bullet ONLY when CLIP_FACTS is on ("" otherwise, so the
# flag-off prompt is byte-identical). Ordering by when clips were filmed needs no
# footage reading: the server sorts by each clip's capture-time fact.
_ORDER_BY_NOTE = (
    '\n  A request to arrange ALL the clips by when or where they were filmed ("in the order'
    ' I filmed them", "chronological", "in the order of my route", "from first stop to last")'
    ' is an order intent with `order_by`: "capture_time" for filming order, "route" for a'
    " route walked or driven. You MUST include that `order_by` value on the returned intent;"
    " never omit it. Leave `position` null for it; it is never combined with first/last."
    " Attribute: the creator's own words for the order. Every other order intent (first/last,"
    " a listed sequence, clips shown while the creator talks about them) has `order_by`"
    ' null: JSON null, never the string "null" and never any other value.'
)

_ORDER_BY_FIELD = ',"order_by":"capture_time|route|null"'

# KRI-667: the three prompt slots that teach the `exclude` op. With
# CLIP_INTENT_EXCLUDE_ENABLED off they render today's bytes exactly (no bullet, the
# KRI-511 "no intent for an exclusion" paragraph, no `exclude` in the JSON template).
_EXCLUDE_BULLET = (
    '\n- exclude: membership explicitly left OUT of the edit. "Skip the quad bike clip",\n'
    '  "leave out the laundry", "don\'t use the blurry ones", "without the drone shot"\n'
    '  and "quad klibini kullanma" are one `exclude` intent per named subject,\n'
    '  `attribute` naming the clips as the creator described them ("quad bike clip").\n'
    "  Leave `creator_text`, `caption_attribute` and `position` null. When the generated\n"
    "  Creative Brief lists the ask this exclusion carries out as a `select` requirement\n"
    '  (`[r3] [select/global] ...`), set `requirement_id` to that id ("r3"); otherwise null.\n'
    "  Never invent an id and never use one from another kind of requirement.\n"
    '  When the clips to leave out are told apart only by their LENGTH ("skip anything too\n'
    '  short to use", "drop the tiny clips", "the accidental mis-taps", "nothing under a\n'
    '  second", "çok kısa klipleri kullanma"), set `by_length` to "too_short" and, only when\n'
    '  the creator said a number of seconds, `under_s` to it ("under a second" -> 1, "less\n'
    '  than 2 seconds" -> 2); otherwise both null. The server measures every clip, so a\n'
    '  length exclusion is never a question. Anything about what a clip shows ("the blurry\n'
    '  ones", "the dark hallway clip") keeps `by_length` null.'
)
_EXCLUDE_OP = "|exclude"
# KRI-994: lets an `exclude` name the Creative Brief ask it carries out, so the finished
# video's receipt can settle that ask. "" with exclusions off (byte-identical prompt).
_REQUIREMENT_FIELD = ',"requirement_id":"r1|null"'
# KRI-1031: a length exclusion ("too short to use") is settled from measured clip lengths.
# Rendered in the same slot as `_REQUIREMENT_FIELD` ("" with exclusions off).
_LENGTH_FIELD = ',"by_length":"too_short|null","under_s":null'
_EXCLUSION_RULES_ON = (
    "Leaving clips OUT is `exclude`, never `include`: include FORCES those clips into\n"
    "the edit, the opposite of skipping them. Never ask about an exclusion: when no\n"
    "clip matches it, nothing is left out. An exclude names clips; removing text,\n"
    'labels, captions, a title, music or a sound ("remove the sport labels", "no\n'
    'music") is never an exclude. Asks to use or keep clips are still `include` as\n'
    'before ("using all my clips", "the gym has to be in", "a montage of the\n'
    'remaining clips (excluding the talk video)"): the creator asks to USE that set,\n'
    'even when it is described by what it leaves out. "Skip the laundry, but the gym\n'
    'has to be in" is one `exclude` for the laundry and one `include` for the gym,\n'
    "each quoting only its own words. Picking the best, strongest or favourite N\n"
    "clips names no clips: the edit planner chooses them, so it yields no intent.\n"
    'Using a clip\'s sound ("the voice from the talk to camera video") is an audio\n'
    "choice, never `include` or `exclude`."
)
# KRI-993: the `exclusive` marker on an `include`. Rides the KRI-667 kill switch: with
# CLIP_INTENT_EXCLUDE_ENABLED off the prompt never teaches it and the parser drops it.
_EXCLUSIVE_RULES = (
    'An edit limited to a subject ("only about the steak", "just the sunset shots",\n'
    '"nothing but the dog", "sadece biftek") is ONE `include` intent for that subject with\n'
    "`exclusive` true: the edit is limited to the clips it matches. Use it only when the\n"
    'creator limits the WHOLE edit to a subject, never for "make sure the gym is in", a\n'
    'style or mood ("only fast cuts"), or a plain description of what was filmed.\n'
    "`exclusive` is false on every other intent."
)
_EXCLUSIVE_FIELD = ',"exclusive":false'
_EXCLUSION_RULES_OFF = (
    'Leaving clips OUT is not one of these operations. "Skip the quad bike clip",\n'
    '"leave out the laundry", "don\'t use the blurry ones" and "without the drone\n'
    'shot" choose which clips the edit uses, and the edit planner already handles\n'
    "that from the creator's words. Return no intent for an exclusion and do not ask\n"
    "about it. Never write it as `include`: include FORCES those clips into the edit,\n"
    "the opposite of skipping them. Never invent an op such as `exclude`. Asks to\n"
    'use or keep clips are still `include` as before ("using all my clips", "the gym\n'
    'has to be in", "a montage of the remaining clips (excluding the talk video)"):\n'
    "the creator asks to USE that set, even when it is described by what it leaves\n"
    "out. Picking the best, strongest or favourite N clips names no clips: the edit\n"
    "planner chooses them, so it yields no intent. Using a clip's sound (\"the voice\n"
    'from the talk to camera video") is an audio choice, never `include`.'
)

# Appended after the generic language line (Turkish chats only): the JSON stays machine
# shaped and the provenance fence needs the creator's own words.
_REPLY_LANGUAGE_SCOPE = (
    "In this task only the top-level `question` is shown to the creator, so only it is "
    "written in that language. `source_quote` stays an exact copy of the creator's own "
    "words, and `attribute`, `creator_text` and `caption_attribute` stay in the creator's "
    "own words."
)


def _sanitize_text(value: str) -> str:
    value = _CONTROL_CHARS.sub(" ", value)
    value = _ROLE_MARKERS.sub(r"\1[role-marker-stripped]", value)
    return _FENCE.sub("'''", value)


_QUOTE_TABLE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "–": "-",
        "—": "-",
        "−": "-",
    }
)
# Label copy is a corner tag; caption copy is the creator's own chapter line,
# printed as written (CREATOR_CAPTION_MAX_CHARS). KRI-826: a label the creator wrote is
# bounded by the label bar's two lines (`clean_creator_label_text`), so the planner
# passes up to the stored bound and the fence, not this cap, answers "too long".
_CREATOR_TEXT_MAX = CREATOR_LABEL_STORAGE_MAX
_LABEL_PREVIEW_CHARS = 40
_ASK_PREVIEW_CHARS = 90
_UNEXPRESSED_MAX = 8
_QUESTION_MAX_CHARS = 400
# Leverkusen M2: `hide_part` (part of one clip never shown) has no flag: a privacy ask is
# never dropped as an invented op.
_KNOWN_OPS = {"label", "group", "order", "include", "caption", "hide_part"}
# KRI-667: `exclude` is a real op only with CLIP_INTENT_EXCLUDE_ENABLED; `_KNOWN_OPS`
# stays the flag-off vocabulary (invented-op detection must not drift), and a creator
# question may still name an exclude it could not keep.
_PREVIEW_OPS = _KNOWN_OPS | {"exclude"}
# Leverkusen M2: every op the schema has (another session's new op included). Any other op
# is one no wording can add, so it is never a "please restate" question.
_CLIP_OPS = frozenset(get_args(ClipIntentOp))
# KRI-282: "add a text placeholder so I can replace it with their real names".
# Flash does not reliably set `placeholder: true` (it has mapped this to a
# creator_text caption or a grounded "person's name" label), so a quote that
# literally asks for a placeholder is the deterministic signal.
# KRI-520: "yer tutucu" is the Turkish phrase for the same request.
_PLACEHOLDER_REQUEST = re.compile(r"\bplace[\s-]?holders?\b|\byer\s*tutucu\w*", re.IGNORECASE)
_STOPWORDS = frozenset(
    "a an the and or of to for in on at by with all my our your their clips clip shots shot "
    "videos video content footage".split()
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")
# An elision mark inside a quote ("...", "[...]"; NFKC turns "…" into "...").
_ELLIPSIS = re.compile(r"\s*[\[(]?\.{3,}[\])]?\s*")
# Flash sometimes writes prose into the first/last `position` enum ("at the
# start", "in this chapter order"). Word-bounded so "lasting" never reads as last.
# KRI-520: the same prose in Turkish ("en başta", "sonunda"); "sonra" (after) is not "sona".
_POSITION_FIRST = re.compile(
    r"\b(first|start|beginning|opening|open|[iİıI]lk\w*|ba[sş]ta|ba[sş]ında|basinda|"
    r"ba[sş]lang[ıi]ç\w*|baslangic\w*|a[cç][ıi]l[ıi][sş]\w*|acilis\w*)\b",
    re.IGNORECASE,
)
_POSITION_LAST = re.compile(
    r"\b(last|end|ending|close|closing|finish|son|sonda|sonunda|sona|"
    r"biti[sş]\w*|bitis\w*|kapan[ıi][sş]\w*|kapanis\w*)\b",
    re.IGNORECASE,
)


# KRI-677: a quote copied from a JSON-rendered line keeps its escapes (``\"Hadi
# gidelim\" dediğimiz iskele klibi``). Both sides of the provenance check are
# normalised the same way, so a creator who literally typed ``\"`` still matches.
_JSON_ESCAPES = re.compile(r'\\(["\\])')  # kri667: strips JSON escapes, reads no meaning
# KRI-950: emoji presentation selectors ("☕" vs "☕️"). Invisible, and models add or drop them
# when copying, so provenance ignores them; the printed text keeps the creator's own form.
_VARIATION_SELECTORS = re.compile("[\ufe0e\ufe0f]")  # kri667: invisible marks, reads no meaning


def _norm(value: str) -> str:
    """Compare creator text the way the model saw it, tolerant of typography.

    The prompt shows the model ``_sanitize_text(...)`` output, and models freely
    swap curly/straight quotes, dash styles and line breaks when copying. None of
    that changes what the creator wrote, so provenance is checked on a form with
    sanitization applied, quotes/dashes unified, JSON escapes around quotes
    removed and whitespace collapsed.
    """
    value = unicodedata.normalize("NFKC", _sanitize_text(value)).translate(_QUOTE_TABLE)
    value = _JSON_ESCAPES.sub(r"\1", value)
    value = _VARIATION_SELECTORS.sub("", value)
    return " ".join(value.split())


class ClipIntentSchemaError(SchemaError):
    """Planner output failed validation.

    ``error_class`` is a closed vocabulary (never model or creator text) so a
    sensitive agent can still record WHY a run failed. ``dropped`` carries short
    attribute previews of instructions that could not be verified, used only to
    word a specific question back to the creator. ``drop_classes`` is every
    distinct rejection class (same closed vocabulary), for diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: str,
        dropped: list[str] | None = None,
        drop_classes: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.dropped = dropped or []
        self.drop_classes = drop_classes or [error_class]


class _IntentRejected(Exception):  # noqa: N818 - internal control flow
    def __init__(self, error_class: str, detail: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.detail = detail


class _IntentUnexpressible(Exception):  # noqa: N818 - internal control flow
    """An op outside ``ClipIntentOp``: named on the draft, never asked about."""


# KRI-520: operation names as the creator reads them in a Turkish question.
_OP_NAMES_TR = {
    "label": "etiket",
    "group": "grup",
    "order": "sıra",
    "include": "dahil etme",
    "caption": "yazı",
    "exclude": "hariç tutma",
    "hide_part": "bölüm gizleme",
}


def _copy(language: str | None, *, en: str, tr: str) -> str:
    """Server copy for ``language`` (the input's), else the turn's bound language."""
    return tr if (language or current_reply_language()) == "tr" else en


def _preview(raw_intent: object, language: str | None = None) -> str:
    """Short, sanitized description of a returned intent, for a creator question."""
    fallback = _copy(language, en="an instruction", tr="bir talimat")
    if not isinstance(raw_intent, dict):
        return fallback
    attribute = raw_intent.get("attribute")
    op = raw_intent.get("op")
    text = " ".join(_sanitize_text(attribute).split()) if isinstance(attribute, str) else ""
    # KRI-552: whole words only; "...restart the kilometer t" read like a broken message.
    text = trim_at_word(text, _LABEL_PREVIEW_CHARS).strip() or fallback
    if isinstance(op, str) and op in _PREVIEW_OPS:
        return f"{_copy(language, en=op, tr=_OP_NAMES_TR[op])}: {text}"
    return text


def _ask_preview(raw_intent: object, sources: tuple[str, ...], language: str | None) -> str:
    """The creator's own words for an intent when its quote is theirs, else ``_preview``.

    "the goal clip with the kids at the start" names the clip, not the ask; "Keep that clip,
    just start it after the kids" is what the creator will recognise.
    """
    quote = raw_intent.get("source_quote") if isinstance(raw_intent, dict) else None
    if isinstance(quote, str) and _norm(quote) and any(_norm(quote) in s for s in sources):
        text = trim_at_word(" ".join(_sanitize_text(quote).split()), _ASK_PREVIEW_CHARS)
        if text := text.strip().rstrip(".!;:,").strip():
            return text
    return _preview(raw_intent, language)


def salvage_question(
    kept: int, labels: list[str], overflow: int = 0, *, language: str | None = None
) -> str:
    """One focused question naming what could not be verified (labels are previews)."""
    named = "; ".join(f'"{label}"' for label in labels[:3])
    extra = len(labels) - 3
    if extra > 0:
        named += _copy(language, en=f" (and {extra} more)", tr=f" (ve {extra} tane daha)")
    pieces: list[str] = []
    if named:
        pieces.append(
            _copy(
                language,
                en=f"I couldn't safely verify {named}",
                tr=f"Şunları güvenle doğrulayamadım: {named}",
            )
        )
    if overflow:
        pieces.append(
            _copy(
                language,
                en=(
                    f"I can apply at most {MAX_CLIP_INTENTS} clip instructions at a time, "
                    f"so {overflow} more weren't included"
                ),
                tr=(
                    f"Bir seferde en fazla {MAX_CLIP_INTENTS} klip talimatı uygulayabiliyorum, "
                    f"bu yüzden {overflow} talimat daha eklenmedi"
                ),
            )
        )
    lead = _copy(
        language,
        en=f"I understood {kept} of your clip instructions",
        tr=f"Klip talimatlarından {kept} tanesini anladım",
    )
    body = "; ".join(pieces)
    text = f"{lead}. {body}." if body else f"{lead}."
    tail = _copy(
        language,
        en=" Please restate just those so I can add them.",
        tr=" Sadece bunları yeniden yazar mısın, ekleyeyim.",
    )
    # The ask to restate is the point of the question: shorten the list in front of it, never it.
    return trim_at_word(text, _QUESTION_MAX_CHARS - len(tail)) + tail


def unexpressed_notice(
    labels: list[str] | tuple[str, ...], *, language: str | None = None
) -> str | None:
    """One line for the draft naming asks no clip operation can carry (``unexpressed``).

    Leverkusen M2: "Put Taha's scream in slow motion" and "start that clip after the kids"
    came back with ops the schema doesn't have, and the creator was asked to restate them.
    No wording adds an op, so the restate looped. This is said once, on the draft, with
    what the clip steps can do, and never holds the turn.
    """
    unique = list(dict.fromkeys(label for label in labels if label))
    if not unique:
        return None
    named = "; ".join(f'"{label}"' for label in unique[:3])
    extra = len(unique) - 3
    if extra > 0:
        named += _copy(language, en=f" (and {extra} more)", tr=f" (ve {extra} tane daha)")
    one = len(unique) == 1
    return _copy(
        language,
        en=(
            f"I can't do {named} with your clips yet, so this draft leaves "
            f"{'it' if one else 'them'} out. I can use, skip, reorder, group, label or caption "
            "clips: tell me which of those you meant and I'll add it."
        ),
        tr=(
            f"{named} {'isteğini' if one else 'isteklerini'} kliplerinle henüz yapamıyorum, "
            "bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, "
            "gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini "
            "kastettiğini söylersen eklerim."
        ),
    )


def _unique_intent_id(intent_id: str, taken: set[str]) -> str:
    """A fresh id for an intent whose model-minted id collides with a kept one."""
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = intent_id[: INTENT_ID_MAX_CHARS - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _repair_placeholder(data: dict[str, Any], sources: tuple[str, ...]) -> None:
    """Normalise a placeholder request into ``label`` + ``placeholder=true`` in place.

    The text is the system's fixed stand-in, never creator copy, so any
    ``creator_text`` the model attached ("NAME", "[Name]") is discarded rather
    than rejected by the source fence -- the creator is never asked to restate
    something the system can resolve itself.
    """
    quote = data.get("source_quote")
    flagged = data.get("placeholder") is True
    asked = (
        isinstance(quote, str)
        and bool(_PLACEHOLDER_REQUEST.search(quote))
        and data.get("op") in {"label", "caption"}
        and any(_norm(quote) in source for source in sources)
    )
    if not (flagged or asked):
        data.pop("placeholder", None)
        return
    data.update(
        op="label",
        placeholder=True,
        label_source="clip",
        creator_text=None,
        caption_attribute=None,
        position=None,
        order_by=None,
    )
    data.pop("transcript_kind", None)
    if not str(data.get("attribute") or "").strip():
        data["attribute"] = "individual shots of people"


def _sentence_naming(attribute: str, sources: tuple[str, ...]) -> str | None:
    """The creator sentence that contains every content word of ``attribute``."""
    words = {w for w in re.findall(r"\w+", attribute.casefold()) if w not in _STOPWORDS}
    if not words:
        return None
    for source in sources:
        for sentence in _SENTENCE_SPLIT.split(source):
            sentence = sentence.strip()
            if sentence and words <= set(re.findall(r"\w+", sentence.casefold())):
                return sentence[:600]
    return None


def _quote_with_text(quote: str, text: str, sources: tuple[str, ...]) -> str | None:
    """The creator span joining a quote to the quoted text it stopped short of (KRI-950).

    Flash quotes "show the ribeye photo full screen" and leaves out ``with the text "Ojo de
    bife 🥩"`` that ends the same sentence. Only for text the creator put in quotation marks,
    in the same sentence, with no other quoted text between the two: "Label the butcher
    "Asado" and the cook "Parrilla"" never hands the butcher the cook's words. ``quote`` and
    ``text`` are ``_norm``-ed.
    """
    marked = f'"{text}"'
    for source in sources:
        for sentence in _SENTENCE_SPLIT.split(source):
            at_quote, at_text = sentence.find(quote), sentence.find(marked)
            if at_quote < 0 or at_text < 0:
                continue
            start, end = min(at_quote, at_text), max(at_quote + len(quote), at_text + len(marked))
            first, second = sorted([(at_quote, len(quote)), (at_text, len(marked))])
            if '"' in sentence[first[0] + first[1] : second[0]] or end - start > 600:
                continue
            return sentence[start:end]
    return None


def _creator_form(text: str, input: ClipIntentPlannerInput) -> str:  # noqa: A002
    """``text`` as the creator typed it, when it differs only by emoji presentation selectors."""
    sources = (input.creator_request, input.latest_user_message or "")
    if not any(_VARIATION_SELECTORS.search(value) for value in (text, *sources)):
        return text
    bare = _VARIATION_SELECTORS.sub("", text)
    # The creator's exact spelling of a text the fence already verified.
    marks = "[\ufe0e\ufe0f]?"
    pattern = re.compile("".join(re.escape(c) + marks for c in bare))  # kri667: spelling only
    for source in sources:
        if match := pattern.search(source):
            return match.group(0)
    return text


def _unstitched_quote(quote: str, creator_text: str | None, sources: tuple[str, ...]) -> str | None:
    """The creator span inside a quote the model stitched from exact creator spans.

    Flash sometimes prefixes each chapter row with the request's governing sentence
    ("show each chapter line ... word for word. Chapter 2 · ... · It's a castell."),
    or elides a list it quotes ("Bölüm başlıkları koy: ..., Üniversite", KRI-531).
    Every word is still the creator's; only the join is not contiguous. Returns the
    piece that holds ``creator_text`` (or the longest piece when there is none), only
    when every piece is an exact creator span, so nothing invented ever passes.
    ``quote`` and ``creator_text`` are ``_norm``-ed.
    """
    if _ELLIPSIS.search(quote):
        pieces = [p.strip(" ,;") for p in _ELLIPSIS.split(quote)]
        pieces = [p for p in pieces if p]
        if pieces and all(any(p in s for s in sources) for p in pieces):
            held = [p for p in pieces if creator_text is None or creator_text in p]
            if held:
                return max(held, key=len)
    best: str | None = None
    for i, char in enumerate(quote):
        if char != " ":
            continue
        left, right = quote[:i], quote[i + 1 :]
        if not (any(left in s for s in sources) and any(right in s for s in sources)):
            continue
        halves = [h for h in (left, right) if creator_text is None or creator_text in h]
        for half in halves:
            if best is None or len(half) > len(best):
                best = half
    return best


def _repair_position(data: dict[str, Any]) -> None:
    """Map prose an order intent wrote into ``position`` onto the enum, in place.

    "at the start" is ``first`` and "at the very end" is ``last``. A request to
    keep the listed order ("in this chapter order", "sequential", a list of the
    chapters) places nothing first or last: ``None``, the shape the model already
    returns for it. The order itself is the sequence of the order intents (one per
    named subject, KRI-941), never ``position``: the sequence rule
    (``clip_order_sequence``) reads only those intents, not ``story_structure``. Two
    placements packed into one intent ("day first and night last") stay invalid:
    guessing one side would silently drop the other, so the creator is asked.
    """
    position = data.get("position")
    if data.get("op") != "order" or position in (None, "first", "last"):
        return
    text = position if isinstance(position, str) else ""
    first = bool(_POSITION_FIRST.search(text))
    last = bool(_POSITION_LAST.search(text))
    if first and last:
        return
    data["position"] = "first" if first else "last" if last else None


_ORDER_BY_VALUES = frozenset({"capture_time", "route"})
_ORDER_BY_NULLS = frozenset({"", "null", "none", "n/a"})
_CAPTURE_ORDER_TOKENS = frozenset(
    "capture captured filmed filming film chronological chronologically".split()
)
_ROUTE_ORDER_TOKENS = frozenset({"route", "itinerary"})
# A fact-based order arranges EVERY clip, so the creator's quote names the order
# itself ("in the order I filmed them", "chronological", "along my route").
_ORDER_BY_EVIDENCE = re.compile(
    r"order|chrono|film|shot|record|captur|route|sequen|happen|time|stop|earliest|latest"
    r"|oldest|newest|sıra|kronoloj|çek",
    re.IGNORECASE,
)


def _repair_order_by(data: dict[str, Any]) -> None:
    """Map an invented ``order_by`` onto the enum or null, in place (KRI-511).

    The template shows ``"capture_time|route|null"``, so Flash writes the string
    ``"null"`` or invents a value ("voiceover_match" for "show the balloons while
    I talk about the balloons"), and the whole instruction was rejected. A
    filming-order synonym maps onto ``capture_time``/``route``; anything else, and
    any value whose quote never asks for an order of the whole edit, is null. A
    valid value on a first/last or non-order intent is left for the validator,
    which asks the creator: guessing which half was meant would drop the other.
    """
    value = data.get("order_by")
    if value is None:
        return
    text = value.strip().casefold() if isinstance(value, str) else ""
    if text in _ORDER_BY_NULLS:
        data["order_by"] = None
        return
    if text in _ORDER_BY_VALUES:
        quote = data.get("source_quote")
        if (
            data.get("op") == "order"
            and data.get("position") is None
            and isinstance(quote, str)
            and not _ORDER_BY_EVIDENCE.search(_norm(quote))
        ):
            data["order_by"] = None
        return
    tokens = set(re.findall(r"[a-z]+", text))
    if data.get("op") == "order" and data.get("position") is None:
        if tokens & _CAPTURE_ORDER_TOKENS:
            data["order_by"] = "capture_time"
            return
        if tokens & _ROUTE_ORDER_TOKENS:
            data["order_by"] = "route"
            return
    data["order_by"] = None


def _repair_length(data: dict[str, Any]) -> None:
    """Settle an exclusion's ``by_length`` / ``under_s`` onto the enum and seconds, in place.

    KRI-1031. The template shows ``"too_short|null"``, so Flash may write the string
    ``"null"``; any value but ``too_short`` is null, and the ask goes to the resolver as
    before. ``under_s`` is kept only as a positive number of seconds; a number the creator
    gave is a length ask even when Flash left ``by_length`` null.
    """
    value = data.get("by_length")
    by_length = "too_short" if isinstance(value, str) and value.strip() == "too_short" else None
    seconds = data.get("under_s")
    under_s: float | None = None
    if isinstance(seconds, (int, float, str)) and not isinstance(seconds, bool):
        try:
            under_s = float(seconds)
        except ValueError:
            under_s = None
        if under_s is not None and not (0 < under_s <= 600 and math.isfinite(under_s)):
            under_s = None
    data["by_length"] = "too_short" if under_s is not None else by_length
    data["under_s"] = under_s


# Leverkusen M2: what may surround a number the creator typed ("(2.5", "3,", "seconds.").
_NUMBER_EDGES = "()[]{}\"'“”‘’«»,;:!?."


def _creator_numbers(sources: tuple[str, ...]) -> set[float]:
    """Every plain number in the creator's own text ("2.5", "2,5", "3"), as seconds might be.

    A provenance check, like the quote fence: it reads no meaning, it only proves the number
    is the creator's. Spelled-out numbers ("three") are not read: the server asks instead.
    """
    found: set[float] = set()
    for source in sources:
        for token in source.split():
            try:
                value = float(token.strip(_NUMBER_EDGES).replace(",", "."))
            except ValueError:
                continue
            if math.isfinite(value):
                found.add(round(value, 3))
    return found


def _repair_hidden_part(data: dict[str, Any], sources: tuple[str, ...]) -> None:
    """Settle a ``hide_part``'s edge, seconds and content onto their fields, in place.

    Leverkusen M2. ``part_edge`` is kept only as exactly "start" or "end"; anything else is
    null and the server asks where the part is. ``part_s`` is kept only as a positive number
    of seconds the creator actually typed (``_creator_numbers``): a number Flash invented
    never decides which frames are hidden, so the server asks for it instead. Nothing is
    printed for a hidden part, so on-screen copy and placement are cleared.
    """
    edge = data.get("part_edge")
    edge = edge.strip().lower() if isinstance(edge, str) else None
    data["part_edge"] = edge if edge in {"start", "end"} else None
    seconds = data.get("part_s")
    value: float | None = None
    if isinstance(seconds, (int, float, str)) and not isinstance(seconds, bool):
        try:
            value = float(str(seconds).replace(",", ".").strip())
        except ValueError:
            value = None
    if value is not None and not (0 < value <= 600 and math.isfinite(value)):
        value = None
    if value is not None and round(value, 3) not in _creator_numbers(sources):
        value = None
    data["part_s"] = value
    content = data.get("part_attribute")
    data["part_attribute"] = content if isinstance(content, str) and content.strip() else None
    # Only a video has seconds to hide; a photo is never a candidate for the part.
    data.update(
        creator_text=None,
        caption_attribute=None,
        position=None,
        placeholder=False,
        source_kind="video",
    )


# KRI-511: leaving clips OUT is clip selection, which the Main Creator owns
# (`selected_media_ids` against the brief's `select` requirement). Without KRI-667
# there is no exclude op: Flash either invents one (a loud rejection, so the creator
# was asked to restate "Skip the quad bike clip") or writes `include`, which FORCES
# the clip into the edit, the opposite of the ask. Both are dropped silently.
# KRI-667 (CLIP_INTENT_EXCLUDE_ENABLED): `exclude` is a real op; these synonyms map
# onto it, and an `include` whose quote asks to leave its own clips out is repaired
# into one (`_repair_exclusion`), never dropped or inverted.
_EXCLUSION_OPS = frozenset("exclude excluded exclusion skip remove omit drop deselect hide".split())
_LEADING_GLUE = r"^(?:(?:and|but|also|please|then|so|just|oh)\s+)*"
_EXCLUSION_ASK = re.compile(
    _LEADING_GLUE + r"(?:skip|remove|omit|exclude|excluding|ignore|lose|without|except|drop"
    r"|get\s+rid\s+of|leave\b.*\bout\b|cut\s+(?:out|the|that|this|those|these|my)\b"
    r"|(?:i\s+)?(?:don't|dont|do\s+not)\s+(?:use|include|show|want|need|put)"
    r"|no\s+more)\b",
    re.IGNORECASE,
)
# Turkish puts the verb last: "quad klibini kullanma", "... atla", "... çıkar".
_EXCLUSION_ASK_TR = re.compile(r"\b(?:kullanma|atla|çıkar|koyma|gösterme|ekleme)\b\W*$")
# KRI-511: an instruction about the spoken captions ("subtitles in English",
# "Spell the place names exactly: Göreme, Paşabağ") names no clips. Flash mints
# it as `include: subtitles`, `label: place name` or a caption per listed name.
_SPEECH_CAPTION_WORDS = frozenset(
    "captions caption subtitles subtitle altyazı altyazılar altyazi".split()
)
_SPELLING_ASK = re.compile(_LEADING_GLUE + r"(?:spell|spelled|spelt|spelling)\b", re.IGNORECASE)
_SPELLING_WORDS = frozenset({"spell", "spelled", "spelt", "spelling"})
# KRI-531: Flash mints an op for asks the prompt says yield no intent ("25 saniye
# olsun" -> `timing`, "dikey olsun" -> `set_style`, "if there are two copies use
# one" -> `select` / `select_unique`). Length, look and which clips to use belong
# to the edit planner (`selected_media_ids` against the brief's `select`
# requirement), which reads the creator's words in the Creative Brief.
_NON_CLIP_OP_WORDS = frozenset(
    "timing time duration length style look format orientation vertical aspect ratio crop "
    "resolution select selection unique dedupe deduplicate duplicate duplicates music song "
    "pace pacing speed transition transitions filter color".split()
)
# A clip's own sound ("with her own voice") is an audio choice the edit planner
# owns; placed first or last it is still a placement of that clip.
_AUDIO_OP_WORDS = frozenset({"audio", "voice", "sound"})
# Leverkusen M2: "Put Taha's scream in slow motion" came back as an op the schema doesn't
# have ("slow_motion"-like, no "speed" in it) and the creator was asked to restate it. Speed
# is the Creative Brief's: the montage planner writes ``playback_rate`` from its words
# (`kria.speed_ask`, KRI-689) and its receipt says whether the speed landed.
_SPEED_OP_WORDS = frozenset(
    "speed slow slower faster slowmo slomo retime playback timelapse hyperlapse".split()
)
_OP_WORDS = re.compile(r"[a-z]+")


def _is_speed_op(op: object, quote: str) -> bool:
    """An op outside the schema (or none) whose name or quote asks footage to play faster or
    slower."""
    if isinstance(op, str) and op in _CLIP_OPS:
        return False
    name = op if isinstance(op, str) else ""
    words = set(_OP_WORDS.findall(name.casefold()))
    return bool(words & _SPEED_OP_WORDS) or says_clip_speed(name) or says_clip_speed(quote)


def _invented_op_words(op: object) -> set[str]:
    """Words of an op outside the schema; empty for a real op or one naming a real op."""
    if not isinstance(op, str) or op in _KNOWN_OPS:
        return set()
    words = set(_OP_WORDS.findall(op.casefold()))
    return set() if words & _KNOWN_OPS else words


def _repair_misplaced_placement(data: dict[str, Any]) -> None:
    """An ``include`` or invented audio op placed first/last is an ``order``, in place.

    "En sonda Elif'in kameraya söylediği cümleyi kendi sesiyle kullan" came back as
    ``op: "audio"`` or ``op: "include"`` with ``position: "last"`` (KRI-531): the
    first was rejected and the creator asked to restate it; the second silently lost
    its placement, so the edit no longer ended on her clip. Keeping her voice is the
    edit planner's audio choice; ending on her clip is the order the creator asked
    for. Only when the creator's quote also names that placement, so a stray model
    position never moves a clip. ``_repair_position`` then normalises the placement
    (two placements still ask). An exclusion is left for ``_repair_exclusion``
    (KRI-667) or ``_silent_non_clip_drop``.
    """
    op = data.get("op")
    quote = data.get("source_quote")
    quote_text = _norm(quote) if isinstance(quote, str) else ""
    if op == "include":
        if _is_exclusion_ask(quote_text):
            return
    elif not _invented_op_words(op) & _AUDIO_OP_WORDS:
        return
    position = data.get("position")
    text = position if isinstance(position, str) else ""
    first = bool(_POSITION_FIRST.search(text) and _POSITION_FIRST.search(quote_text))
    last = bool(_POSITION_LAST.search(text) and _POSITION_LAST.search(quote_text))
    if first or last:
        data["op"] = "order"


def _is_exclusion_ask(quote: str) -> bool:
    text = _norm(quote).casefold()
    return bool(_EXCLUSION_ASK.search(text) or _EXCLUSION_ASK_TR.search(text))


# KRI-667: where the clause an exclusion verb governs ends. English puts the verb first
# ("Skip the laundry, but the gym has to be in": the laundry), Turkish last ("Spor
# salonu kalsın ama çamaşır klibini kullanma": the laundry). A comma, a sentence end or
# a joining word ends it, so "skip the laundry and use the gym" never governs the gym.
# Plain word sets, no new patterns: the model states exclusions; this is only the
# fallback's reading of a quote the model already tied to the clip.
_CLAUSE_BREAKS = str.maketrans(dict.fromkeys(".,;:!?()", "\n"))
_EN_JOINERS = frozenset(
    "and but or then plus however though although instead while whereas yet so".split()
)
_TR_JOINERS = frozenset("ve ama fakat ancak sonra veya ya yoksa".split())
# Words that name no particular clip ("the quad bike CLIP", "çamaşır KLİBİ").
_CLIP_NOUN_PREFIXES = ("klip", "klib", "video", "cekim", "goruntu")
# An `exclude` removes FOOTAGE. "Remove the sport labels" / "müziği çıkar" remove text
# or sound, and read as an exclude they would drop every sport clip (folded words).
_NON_CLIP_TARGET_WORDS = frozenset(
    "label labels labeled labelled labeling caption captions subtitle subtitles title titles "
    "text texts sticker stickers watermark watermarks logo logos music song songs sound sounds "
    "audio voice voices voiceover voiceovers narration filter filters effect effects "
    "transition transitions emoji emojis overlay overlays font fonts ses sesi sesini sesler "
    "sesleri".split()
)
_NON_CLIP_TARGET_PREFIXES = (
    "etiket",
    "yazi",
    "altyazi",
    "baslik",
    "muzik",
    "sarki",
    "efekt",
    "filtre",
    "gecis",
    "seslendir",
)
# Compound op names Flash writes for the same ask ("skip_clip", "exclude-videos").
_EXCLUSION_OP_FILLER = frozenset({"clip", "clips", "shot", "shots", "video", "videos", "footage"})


def _fold_words(text: str) -> list[str]:
    """Case-, accent- and dotless-ı-insensitive words (Turkish suffixes kept)."""
    decomposed = unicodedata.normalize("NFKD", _norm(text)).replace("ı", "i").casefold()
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WORDS.findall(plain)


def _clauses(text: str, joiners: frozenset[str]) -> list[list[str]]:
    """The folded words of ``text``, split at punctuation and joining words (non-empty)."""
    clauses: list[list[str]] = []
    for piece in text.translate(_CLAUSE_BREAKS).split("\n"):
        clause: list[str] = []
        for word in _fold_words(piece):
            if word in joiners:
                clauses.append(clause)
                clause = []
            else:
                clause.append(word)
        clauses.append(clause)
    return [clause for clause in clauses if clause]


# KRI-680: Turkish stacks a clip's descriptions before its noun with commas ("Çok
# karanlık, hiçbir şeyin görünmediği iki klibi kullanma"), so a comma ends the governed
# clause only after a finished verb ("Spor salonu kalsın, çamaşır klibini kullanma").
# Folded word endings of finite verbs: optative, past, progressive, reported, future,
# necessity, "let's", and a negative imperative.
_TR_SENTENCE_BREAKS = str.maketrans(dict.fromkeys(".;:!?()", "\n"))
_TR_VERB_ENDINGS = (
    "sin",
    "sun",
    "di",
    "du",
    "ti",
    "tu",
    "yor",
    "mis",
    "mus",
    "acak",
    "ecek",
    "mali",
    "meli",
    "alim",
    "elim",
    "ma",
    "me",
)
# Bare imperatives ("başla", "koy") carry no ending; the exclusion verbs are here too.
_TR_IMPERATIVES = frozenset(
    "kullan koy ekle basla bitir goster yaz kes ac kapat birak yap al ver tut at cikar atla "
    "sil kaldir".split()
)


def _ends_in_tr_verb(words: list[str]) -> bool:
    last = words[-1]
    return last in _TR_IMPERATIVES or last.endswith(_TR_VERB_ENDINGS)


def _tr_governed_clause(text: str) -> list[str]:
    """The last Turkish clause, with the comma-led descriptions of its noun (KRI-680)."""
    sentences = [s for s in text.translate(_TR_SENTENCE_BREAKS).split("\n") if _fold_words(s)]
    if not sentences:
        return []
    clause: list[str] = []
    for piece in reversed(sentences[-1].split(",")):
        words = _fold_words(piece)
        if not words:
            continue
        if clause and _ends_in_tr_verb(words):
            break
        joiners = [i for i, word in enumerate(words) if word in _TR_JOINERS]
        start = joiners[-1] + 1 if joiners else 0
        clause = words[start:] + clause
        if joiners:
            break
    return clause


def _exclusion_clause(quote: str) -> list[str] | None:
    """The folded words an exclusion verb in ``quote`` governs, or None when it asks none.

    English: the first clause (leading glue such as "but" is a joiner, so it is skipped).
    Turkish: the last one, where the verb is, with its comma-led descriptions.
    """
    text = _norm(quote).casefold()
    if _EXCLUSION_ASK.search(text):
        return next(iter(_clauses(text, _EN_JOINERS)), [])
    if _EXCLUSION_ASK_TR.search(text):
        return _tr_governed_clause(text)
    return None


def _content_words(attribute: str) -> list[str]:
    return [
        word
        for word in _fold_words(attribute)
        if word not in _STOPWORDS and not word.startswith(_CLIP_NOUN_PREFIXES)
    ]


# Turkish softens a final k/p/t before a vowel suffix: "müzik" -> "müziği" (folded g),
# "kitap" -> "kitabı", "dolap" -> "dolabı", "kanat" -> "kanadı".
_SOFTENED = {"k": "g", "p": "b", "t": "d"}


def _same_stem(want: str, have: str) -> bool:
    """One word, allowing suffixes either way ("çamaşır" / "çamaşırı", "one" / "ones")."""
    if want == have:
        return True
    shorter = min(len(want), len(have))
    if shorter >= 3 and (have.startswith(want) or want.startswith(have)):
        return True
    soft = _SOFTENED.get(want[-1:])
    return (
        soft is not None
        and len(want) >= 4
        and len(have) > len(want)
        and have.startswith(want[:-1] + soft)
    )


def _exclusion_governs(attribute: str, quote: str) -> bool:
    """True when every content word of ``attribute`` is in the clause the verb governs."""
    clause = _exclusion_clause(quote)
    want = _content_words(attribute)
    if clause is None or not want:
        return False
    return all(any(_same_stem(word, other) for other in clause) for word in want)


def _names_non_clip_target(words: list[str]) -> bool:
    return any(
        word in _NON_CLIP_TARGET_WORDS or word.startswith(_NON_CLIP_TARGET_PREFIXES)
        for word in words
    )


def _is_exclusion_op(op: object) -> bool:
    """``exclude`` or a synonym Flash writes for it ("skip", "remove", "skip_clip")."""
    if not isinstance(op, str):
        return False
    text = op.strip().casefold()
    if text in _EXCLUSION_OPS:
        return True
    words = set(_OP_WORDS.findall(text))
    return bool(words & _EXCLUSION_OPS) and words <= _EXCLUSION_OPS | _EXCLUSION_OP_FILLER


def _repair_exclusion(data: dict[str, Any]) -> str | None:
    """KRI-667: settle an exclusion onto the ``exclude`` op, in place.

    Returns a closed-vocabulary silent reason: ``exclusion_repaired`` (an ``include``
    whose own quote asks to leave exactly those clips out became an ``exclude``; kept),
    ``exclusion_dropped`` (an exclusion this parser cannot attribute to the clips it
    names, as before KRI-667) or ``non_clip_exclusion_dropped`` (it removes text or
    sound, not footage); None leaves the intent as it is.

    The repair never inverts an ask: an ``include`` becomes an ``exclude`` only when
    every content word of its attribute sits in the clause the exclusion verb governs
    ("Skip the quad bike clip" on "quad bike clip"). "Skip the laundry, but the gym has
    to be in" minted as ``include: gym`` is not repaired into excluding the gym; it is
    dropped, exactly as before KRI-667. An explicit ``exclude`` whose quote the same
    reading can check must pass it too; one in words it cannot read (another language,
    another phrasing) is the model's statement and is kept.
    """
    op = data.get("op")
    quote = data.get("source_quote")
    quote_text = quote if isinstance(quote, str) else ""
    attribute = data.get("attribute")
    attribute_text = attribute if isinstance(attribute, str) else ""
    if _is_exclusion_op(op):
        data["op"] = "exclude"
    elif not (op == "include" and _is_exclusion_ask(quote_text)):
        return None
    clause = _exclusion_clause(quote_text)
    target = _fold_words(attribute_text) + (
        clause if clause is not None else _fold_words(quote_text)
    )
    if _names_non_clip_target(target):
        return "non_clip_exclusion_dropped"
    if clause is not None and not _exclusion_governs(attribute_text, quote_text):
        return "exclusion_dropped"
    if not _content_words(attribute_text):
        # "skip the clips" names no particular clip: nothing safe to remove.
        return "exclusion_dropped"
    if op == "include":
        data["op"] = "exclude"
        return "exclusion_repaired"
    return None


def _silent_non_clip_drop(data: dict[str, Any], *, exclusions: bool = False) -> str | None:
    """Why an intent is not a clip operation at all, or None to keep it (KRI-511).

    Closed vocabulary: ``exclusion_dropped`` (an invented exclude op, or an
    ``include`` whose quote asks to leave clips out), ``non_clip_op_dropped`` (an
    invented timing, style, de-duplication or audio op, KRI-531), ``speed_op_dropped`` (an
    invented op asking footage to play faster or slower, Leverkusen M2),
    ``spelling_dropped`` (how names are spelled), ``speech_caption_dropped`` (a
    non-caption op whose target is the spoken captions). The creator's words stay
    in the Creative Brief, which the Main Creator and the caption steps read.
    ``order_mode_dropped`` (29 Ekim): an ``order`` whose ``order_by`` is a random / any order
    is the brief's order requirement, never a clip sequence.
    ``exclusions`` (KRI-667): ``_repair_exclusion`` already settled every exclusion,
    so an ``exclude`` here is a real op.
    """
    op = data.get("op")
    quote = data.get("source_quote")
    quote_text = quote if isinstance(quote, str) else ""
    if op == "order" and data.get("position") is None and key_order_mode(data.get("order_by")):
        # 29 Ekim: "show all content in random order" came back as an order intent with
        # `order_by: "random"`. The enum has no such value, so it was nulled into a
        # positionless group whose inner order "wasn't recorded", and the creator was asked to
        # choose one. A random / any order is the brief's `order` requirement (facts.key), which
        # the planner shuffles by its stored seed; it is never a clip sequence.
        return "order_mode_dropped"
    if not exclusions and isinstance(op, str) and op.strip().casefold() in _EXCLUSION_OPS:
        return "exclusion_dropped"
    if not exclusions and op == "include" and _is_exclusion_ask(quote_text):
        return "exclusion_dropped"
    if _is_speed_op(op, quote_text):
        return "speed_op_dropped"
    if _invented_op_words(op) & (_NON_CLIP_OP_WORDS | _AUDIO_OP_WORDS):
        return "non_clip_op_dropped"
    attribute = data.get("attribute")
    attribute_words = set(_words(attribute)) if isinstance(attribute, str) else set()
    if _SPELLING_ASK.search(_norm(quote_text)) or attribute_words & _SPELLING_WORDS:
        return "spelling_dropped"
    # A caption op may name its clips beside the word ("captions for the beach
    # clips"); `_silent_caption_drop` owns that shape.
    if op != "caption" and attribute_words & _SPEECH_CAPTION_WORDS:
        return "speech_caption_dropped"
    return None


# KRI-456: a `caption` intent is a chapter caption on named clips. Two shapes the
# model keeps minting are NOT clip operations: a style ask ("big readable
# captions") and a title treatment (`Title: "..."`). Resolved against footage they
# match zero or one arbitrary clip, and the zero-match case stops the turn with
# a clip-not-found question about the word "captions". They are dropped silently here.
_GENERIC_CAPTION_WORDS = frozenset(
    "captions caption subtitles subtitle altyazı altyazılar altyazi text texts".split()
)
# Style adjectives, glue words and articles that may surround a generic caption
# word without naming any clips ("big readable on-screen captions", "the subtitles").
_CAPTION_STYLE_FILLER = frozenset(
    "big bigger large larger huge readable legible bold clear clean simple nice good "
    "on screen onscreen burned burnt in auto automatic generated the a an my our your "
    "some and with büyük okunaklı".split()
)
_WHOLE_VIDEO_TARGETS = frozenset(
    {
        "video",
        "title",
        "the title",
        "video title",
        "the video title",
        "opening title",
        "the opening title",
        "intro title",
        "the intro title",
        "the video",
        "whole video",
        "the whole video",
        "entire video",
        "the entire video",
        # KRI-520
        "başlık",
        "baslik",
        "video başlığı",
        "videonun başlığı",
        "açılış başlığı",
        "giriş başlığı",
        "videoya",
        "tüm video",
        "bütün video",
        "videonun tamamı",
    }
)
# Verbs that may wrap a bare style ask in the creator's quote ("add captions").
_CAPTION_ASK_VERBS = frozenset(
    "add put turn give make use show want please ekle ver aç koy yaz olsun lütfen let lets i would "
    "like can you".split()
)
_TITLE_QUOTE_PREFIX = re.compile(r"^(title|başlık)\s*:", re.IGNORECASE)
_WORDS = re.compile(r"[^\W_]+")


def _words(value: str) -> list[str]:
    return _WORDS.findall(_norm(value).casefold())


def _is_generic_caption_phrase(value: object) -> bool:
    """True when ``value`` is only generic caption words plus style filler."""
    if not isinstance(value, str):
        return False
    words = _words(value)
    rest = [w for w in words if w not in _CAPTION_STYLE_FILLER]
    return bool(rest) and all(w in _GENERIC_CAPTION_WORDS for w in rest)


def _silent_caption_drop(data: dict[str, Any]) -> str | None:
    """Why a `caption` intent is not a clip operation, or None to keep it.

    Closed vocabulary: ``style_caption_dropped`` (the target is just the word
    captions/subtitles/text, e.g. "Big readable captions", or a whole-video target
    whose quote is only a style ask) or ``title_caption_dropped`` (a `Title: "..."`
    line or any other whole-video target). Exact copy on a generic target
    ('add the text "Hello"') is kept: dropping it would silently lose creator words.
    """
    if data.get("op") != "caption":
        return None
    quote = data.get("source_quote")
    quote_text = quote if isinstance(quote, str) else ""
    if _TITLE_QUOTE_PREFIX.match(_norm(quote_text)):
        return "title_caption_dropped"
    attribute = data.get("attribute")
    creator_text = data.get("creator_text")
    if isinstance(attribute, str) and " ".join(_words(attribute)) in _WHOLE_VIDEO_TARGETS:
        # "video" / "the video" targets nothing; if the creator's quote is only a style
        # ask ("Big readable captions") it is a style ask, otherwise a title treatment.
        asked = " ".join(w for w in _words(quote_text) if w not in _CAPTION_ASK_VERBS)
        if creator_text is None and _is_generic_caption_phrase(asked):
            return "style_caption_dropped"
        return "title_caption_dropped"
    if _is_generic_caption_phrase(attribute) and (
        creator_text is None or _is_generic_caption_phrase(creator_text)
    ):
        return "style_caption_dropped"
    return None


def _collapse_captioned_groups(
    kept: list[tuple[PlannedClipIntent, object]],
) -> list[tuple[PlannedClipIntent, object]]:
    """Drop each group a same-named caption already holds together.

    Only called when the inventory is over the cap. A caption keeps its clips in
    ONE beat exactly like a group, so the group adds nothing to the edit. Never
    applied alongside a label: label rows read the creator's group names.
    """
    intents = [intent for intent, _ in kept]
    if any(intent.op == "label" for intent in intents):
        return kept
    captioned = {intent.attribute.casefold() for intent in intents if intent.op == "caption"}
    return [
        (intent, raw)
        for intent, raw in kept
        if not (intent.op == "group" and intent.attribute.casefold() in captioned)
    ]


class PlannedClipIntent(ClipIntent):
    """A clip operation with its exact creator-written provenance."""

    source_quote: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def _validate_caption_shape(self) -> PlannedClipIntent:
        if self.op != "caption":
            return self
        if self.creator_text is None and self.caption_attribute is None:
            raise ValueError("authored captions require caption_attribute")
        if self.creator_text is not None and self.caption_attribute is not None:
            raise ValueError("exact creator caption copy requires caption_attribute=null")
        return self


class ConfirmedTitleCopy(BaseModel):
    """Current-media, server-approved title placement; never a candidate hint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["opening_title", "closing_title"]
    text: str = Field(min_length=1, max_length=280)


class ClipIntentPlannerInput(BaseModel):
    creator_request: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
    latest_user_message: str | None = Field(default=None, max_length=CREATOR_REQUEST_MAX_CHARS)
    # A generated Creative Brief can aid recall but is not creator-authored
    # provenance. Quotes from it never satisfy the parser's source fence.
    generated_brief: str | None = Field(default=None, max_length=CREATOR_REQUEST_MAX_CHARS)
    # Strategy-produced candidates can aid recall, but never authorize output.
    candidate_intents: list[ClipIntent] | None = Field(default=None, max_length=MAX_CLIP_INTENTS)
    confirmed_titles: list[ConfirmedTitleCopy] = Field(
        default_factory=list, max_length=2, exclude_if=lambda value: not value
    )
    available_clip_count: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    # KRI-189: CLIP_FACTS is on for this creator, so the prompt also teaches the
    # fact-based `order_by` field. Omitted from dumps when off (byte-identical).
    clip_facts: bool = Field(default=False, exclude_if=lambda value: not value)
    # KRI-520: the chat's language (app.kria.reply_language). None/"en" => the prompt
    # and the server-written question stay English, byte-identical.
    reply_language: str | None = Field(default=None, max_length=8, exclude_if=lambda v: v is None)
    # KRI-667: CLIP_INTENT_EXCLUDE_ENABLED, so the prompt teaches the `exclude` op and
    # the parser keeps it. Omitted from dumps when off (byte-identical).
    clip_exclusions: bool = Field(default=False, exclude_if=lambda value: not value)


class ClipIntentPlannerOutput(BaseModel):
    intents: list[PlannedClipIntent] = Field(default_factory=list, max_length=MAX_CLIP_INTENTS)
    question: str | None = Field(default=None, max_length=300)
    # Parser-authored (never model-authored): set when some instructions were
    # dropped or exceeded the cap. The kept ``intents`` are valid, but the
    # caller must ask this instead of silently acting on a subset.
    salvage_question: str | None = Field(
        default=None, max_length=500, exclude_if=lambda value: value is None
    )
    # Parser-authored closed vocabulary (rejection classes, plus "over_cap"):
    # WHY ``salvage_question`` was needed, never creator or model text.
    salvage_reasons: list[str] = Field(
        default_factory=list, max_length=16, exclude_if=lambda value: not value
    )
    # Parser-authored (KRI-456, KRI-511): intents that were never clip operations (a
    # style ask like "big readable captions", a `Title: "..."` line, "Skip the quad
    # bike clip", "Spell the place names exactly: ...") and were dropped
    # SILENTLY, as {closed-vocabulary reason: count}. Diagnostics only: it never
    # raises a creator question. KRI-667 also counts `exclusion_repaired` here: an
    # `include` repaired into the `exclude` it asked for (kept, not dropped).
    silent_drops: dict[str, int] = Field(default_factory=dict, exclude_if=lambda value: not value)
    # Parser-authored previews (like the salvage question's) of asks whose op the schema
    # doesn't have (``unexpressible_op_dropped``, Leverkusen M2). Never a question: the
    # draft names them once (`unexpressed_notice`).
    unexpressed: list[str] = Field(
        default_factory=list, max_length=_UNEXPRESSED_MAX, exclude_if=lambda value: not value
    )


class ClipIntentPlannerAgent(Agent[ClipIntentPlannerInput, ClipIntentPlannerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.clip_intent_planner",
        prompt_id="clip_intent_planner",
        # KRI-598: a voiceover cue for a clip is `include`, never a described sequence.
        # KRI-612: "explain / summarise the footage" is one `what is happening in the clip`
        # label, never a caption.
        # KRI-651: inventory receives saved title placement and the owned clip count.
        # KRI-667: the `exclude` op (CLIP_INTENT_EXCLUDE_ENABLED; off renders the
        # 2026-10-13.1 prompt byte-for-byte).
        # KRI-552: "cut my long pauses" / "cut the part where I restart" are speech cleanup,
        # never a caption (or any other) intent.
        # KRI-945: an intro, explanation or facts for the whole video is never a caption.
        # KRI-941: "in this order: A, B, C" is one `order` intent per named subject, position
        # null, in the stated order (the prompt said that and, further down, the opposite).
        #
        # KRI-993: "only about X" is one `include` with `exclusive` true (same kill switch).
        # KRI-994: an `exclude` names the brief `select` requirement it carries out.
        # KRI-1031: a length exclusion ("too short to use") carries `by_length` / `under_s`.
        # 29 Ekim (thread 64bb9853): a random / any order is no `order` intent (the brief owns
        # it), and words shown a piece at a time over the video are no `caption` intent.
        # Leverkusen M2 (2026-10-30): "don't show the kids at the start of the second goal
        # clip" / "start it after 2.5 seconds" is one `hide_part` (edge + creator seconds),
        # never an exclude of the whole clip and never an invented op.
        # KRI-1122: the prompt's operation cap ($max_intents) is 40, not 16.
        prompt_version="2026-10-31-cap-40",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=384,
        max_attempts=2,
        backoff_s=(1.0,),
        # KRI-1122: ~190 output tokens per intent; 18 intents took 14 s live, so a
        # 40-intent inventory needs more than the old 20 s.
        timeout_s=35.0,
        sensitive_io=True,
    )
    Input = ClipIntentPlannerInput
    Output = ClipIntentPlannerOutput

    def required_fields(self) -> list[str]:
        # Empty is the valid result for a request that only changes style,
        # duration, music, or other non-clip operations.
        return []

    def project_input_for_observability(
        self, input_dict: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(input_dict, dict):
            return None

        def redact(value: object) -> dict[str, object]:
            text = str(value or "")
            return {
                "redacted": True,
                "chars": len(text),
                "sha256": sha256(text.encode()).hexdigest(),
            }

        candidates = input_dict.get("candidate_intents") or []
        return {
            "creator_request": redact(input_dict.get("creator_request")),
            "latest_user_message": redact(input_dict.get("latest_user_message")),
            "generated_brief": redact(input_dict.get("generated_brief")),
            "candidate_intents_count": len(candidates) if isinstance(candidates, list) else 0,
            "confirmed_titles": [
                {"target": row["target"], "text": redact(row["text"])}
                for row in input_dict.get("confirmed_titles", [])
            ],
            "available_clip_count": input_dict.get("available_clip_count"),
        }

    def render_prompt(self, input: ClipIntentPlannerInput) -> str:  # noqa: A002
        candidates = [
            intent.model_dump(mode="json")
            for intent in (input.candidate_intents or [])
            # KRI-667: with exclusions off the prompt never shows an `exclude` (before
            # KRI-667 no strategy could carry one).
            if input.clip_exclusions or intent.op != "exclude"
        ]
        prompt = load_prompt(
            "clip_intent_planner",
            creator_request=_sanitize_text(input.creator_request),
            latest_user_message=_sanitize_text(input.latest_user_message or ""),
            generated_brief=_sanitize_text(input.generated_brief or ""),
            candidate_intents=json.dumps(candidates, ensure_ascii=False),
            confirmed_titles=json.dumps(
                [title.model_dump(mode="json") for title in input.confirmed_titles],
                ensure_ascii=False,
            ),
            available_clip_count=(
                str(input.available_clip_count)
                if input.available_clip_count is not None
                else "not supplied"
            ),
            max_intents=str(MAX_CLIP_INTENTS),
            order_by_note=_ORDER_BY_NOTE if input.clip_facts else "",
            order_by_field=_ORDER_BY_FIELD if input.clip_facts else "",
            exclude_bullet=_EXCLUDE_BULLET if input.clip_exclusions else "",
            exclusion_rules=(
                _EXCLUSION_RULES_ON if input.clip_exclusions else _EXCLUSION_RULES_OFF
            ),
            exclude_op=_EXCLUDE_OP if input.clip_exclusions else "",
            exclusive_rules=f"\n\n{_EXCLUSIVE_RULES}" if input.clip_exclusions else "",
            exclusive_field=_EXCLUSIVE_FIELD if input.clip_exclusions else "",
            requirement_field=(_REQUIREMENT_FIELD + _LENGTH_FIELD if input.clip_exclusions else ""),
        )
        # KRI-520: the reply-language instruction goes last. "" for English/unknown:
        # byte-identical prompt.
        language_line = prompt_language_line(input.reply_language)
        if not language_line:
            return prompt
        return f"{prompt.rstrip(chr(10))}\n\n{language_line} {_REPLY_LANGUAGE_SCOPE}\n"

    def parse(self, raw_text: str, input: ClipIntentPlannerInput) -> ClipIntentPlannerOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise ClipIntentSchemaError(
                f"clip_intent_planner: invalid JSON — {exc}", error_class="invalid_json"
            ) from exc
        if not isinstance(data, dict):
            raise ClipIntentSchemaError(
                "clip_intent_planner: response is not a JSON object", error_class="not_object"
            )
        raw_intents = data.get("intents")
        if not isinstance(raw_intents, list):
            raise ClipIntentSchemaError(
                "clip_intent_planner: intents must be a list", error_class="intents_not_list"
            )
        question = data.get("question")
        if question is not None and (not isinstance(question, str) or not question.strip()):
            raise ClipIntentSchemaError(
                "clip_intent_planner: question must be a non-empty string or null",
                error_class="bad_question",
            )

        # Validate provenance against the text the model actually saw (sanitized),
        # tolerant of quote/dash/whitespace typography.
        sources = tuple(
            _norm(source) for source in (input.creator_request, input.latest_user_message or "")
        )
        kept: list[tuple[PlannedClipIntent, object]] = []
        dropped: list[str] = []
        drop_classes: list[str] = []
        failures: list[str] = []
        seen: set[tuple[str, str, str | None, str | None, str | None, str, str | None, bool]] = (
            set()
        )
        seen_ids: set[str] = set()
        silent: list[str] = []
        unexpressed: list[str] = []
        for index, raw_intent in enumerate(raw_intents):
            try:
                intent = self._build_intent(raw_intent, sources, input, index=index, silent=silent)
            except _IntentUnexpressible:
                unexpressed.append(_ask_preview(raw_intent, sources, input.reply_language))
                continue
            except _IntentRejected as rejected:
                dropped.append(_preview(raw_intent, input.reply_language))
                drop_classes.append(rejected.error_class)
                failures.append(f"intents[{index}]: {rejected.detail}")
                continue
            if intent is None:
                continue
            key = (
                intent.op,
                intent.attribute.casefold(),
                intent.creator_text,
                intent.position,
                intent.order_by,
                intent.label_source,
                intent.transcript_kind,
                intent.placeholder,
            )
            if key in seen:
                continue  # a verbatim repeat adds nothing; not worth failing the output
            if intent.intent_id in seen_ids:
                # The id is a model-minted handle: two DIFFERENT operations sharing
                # one is a naming slip, never a reason to drop a creator instruction.
                intent = intent.model_copy(
                    update={"intent_id": _unique_intent_id(intent.intent_id, seen_ids)}
                )
            seen.add(key)
            seen_ids.add(intent.intent_id)
            kept.append((intent, raw_intent))

        if question is not None:
            if kept:
                raise ClipIntentSchemaError(
                    "clip_intent_planner: question cannot accompany partial intents",
                    error_class="question_with_intents",
                )
            return ClipIntentPlannerOutput(intents=[], question=question.strip())
        reasons = sorted(set(drop_classes))
        if not kept and dropped:
            raise ClipIntentSchemaError(
                "clip_intent_planner: every intent was rejected — " + "; ".join(failures[:3]),
                error_class=drop_classes[0],
                dropped=dropped,
                drop_classes=reasons,
            )
        if len(kept) > MAX_CLIP_INTENTS:
            kept = _collapse_captioned_groups(kept)
        # Over the cap: keep the first valid ones in the creator's order and ask about
        # the rest instead of failing the whole inventory.
        overflow = [_preview(raw, input.reply_language) for _, raw in kept[MAX_CLIP_INTENTS:]]
        kept = kept[:MAX_CLIP_INTENTS]
        salvage = None
        if dropped or overflow:
            salvage = salvage_question(
                len(kept), dropped + overflow, len(overflow), language=input.reply_language
            )
            if overflow:
                reasons.append("over_cap")
        try:
            return ClipIntentPlannerOutput(
                intents=[intent for intent, _ in kept],
                salvage_question=salvage,
                salvage_reasons=reasons,
                silent_drops={reason: silent.count(reason) for reason in sorted(set(silent))},
                unexpressed=list(dict.fromkeys(unexpressed))[:_UNEXPRESSED_MAX],
            )
        except ValidationError as exc:
            raise ClipIntentSchemaError(
                f"clip_intent_planner: output validation — {exc}",
                error_class="output_validation",
            ) from exc

    @staticmethod
    def _build_intent(
        raw_intent: object,
        sources: tuple[str, ...],
        input: ClipIntentPlannerInput,  # noqa: A002
        *,
        index: int = 0,
        silent: list[str] | None = None,
    ) -> PlannedClipIntent | None:
        """One validated intent; None drops it silently; ``_IntentRejected`` drops it loudly."""
        if not isinstance(raw_intent, dict):
            raise _IntentRejected("intent_not_object", "is not an object")
        # Flash copies the template's nullable neighbours and writes
        # `"label_source": null`; null means the field's default, "clip".
        data = {
            k: v
            for k, v in raw_intent.items()
            if not (k in {"label_source", "placeholder", "exclusive"} and v is None)
        }
        # The id is a handle the model invents; a missing one is minted here and an
        # over-long one is shortened by ClipIntent (KRI-422), never a rejection.
        if not isinstance(data.get("intent_id"), str) or not data["intent_id"].strip():
            data["intent_id"] = f"intent-{index + 1}"
        _repair_placeholder(data, sources)
        _repair_misplaced_placement(data)
        notes: list[str] = silent if silent is not None else []
        repaired = None
        if input.clip_exclusions:
            # KRI-667: settle the exclusion onto `exclude` first; a repair is kept.
            repaired = _repair_exclusion(data)
            if repaired not in (None, "exclusion_repaired"):
                notes.append(repaired)
                return None
        # KRI-456/KRI-511/KRI-531: a style ask, title line, exclusion, spoken-caption
        # instruction or invented timing/style/audio op is not a clip operation. Drop
        # it before shape/provenance checks so a garbled quote can never turn it into
        # a question.
        silent_reason = _silent_caption_drop(data) or _silent_non_clip_drop(
            data, exclusions=input.clip_exclusions
        )
        if silent_reason is not None:
            notes.append(silent_reason)
            return None
        op = data.get("op")
        if not isinstance(op, str) or op not in _CLIP_OPS:
            # Leverkusen M2: an op the schema doesn't have ("trim_start", "highlight", none
            # at all) is one no restatement can add, so asking to restate looped. The turn
            # goes on with the rest; the draft names it (`unexpressed_notice`).
            notes.append("unexpressible_op_dropped")
            raise _IntentUnexpressible
        if op != "exclude":
            return ClipIntentPlannerAgent._validated_intent(data, sources, input)
        # Nothing is printed for a clip left out. An exclusion that fails the shape or
        # provenance fence is dropped silently, as before KRI-667: asking the creator to
        # restate "Skip the quad bike clip" helps nobody, and an unverified exclude must
        # never remove footage.
        data["creator_text"] = None
        try:
            intent = ClipIntentPlannerAgent._validated_intent(data, sources, input)
        except _IntentRejected:
            notes.append("exclusion_dropped")
            return None
        if intent is not None and repaired is not None:
            notes.append(repaired)
        return intent

    @staticmethod
    def _validated_intent(
        data: dict[str, Any],
        sources: tuple[str, ...],
        input: ClipIntentPlannerInput,  # noqa: A002
    ) -> PlannedClipIntent | None:
        """Shape repairs, schema and provenance fence for one intent (see `_build_intent`)."""
        # Benign shape repairs: none of these change what the creator asked for.
        if isinstance(data.get("creator_text"), str):
            data["creator_text"] = " ".join(data["creator_text"].split()) or None
        if data.get("op") != "caption" or data.get("creator_text") is not None:
            data["caption_attribute"] = None  # exact copy wins over an authored topic
        if data.get("op") != "order":
            data["position"] = None
        if data.get("op") != "include" or not input.clip_exclusions:
            data.pop("exclusive", None)  # KRI-993: a scope means nothing on any other op
        if data.get("op") != "label":
            # Narration-label fields mean nothing on any other op: "Elif's sentence
            # with her own voice, at the end" came back as a transcript order (KRI-531).
            data.pop("label_source", None)
            data.pop("transcript_kind", None)
        if data.get("op") != "hide_part":
            # Leverkusen M2: only a hidden part has an edge, seconds and content.
            for field in ("part_edge", "part_s", "part_attribute"):
                data.pop(field, None)
        else:
            _repair_hidden_part(data, sources)
        if data.get("op") != "exclude" or not input.clip_exclusions:
            # KRI-994: only an exclusion links back to a brief requirement.
            data.pop("requirement_id", None)
            # KRI-1031: and only an exclusion is settled from clip lengths.
            data.pop("by_length", None)
            data.pop("under_s", None)
        else:
            _repair_length(data)
        _repair_position(data)
        _repair_order_by(data)
        attribute = data.get("attribute")
        if (
            data.get("op") == "order"
            and data.get("order_by") in _ORDER_BY_VALUES
            and not (isinstance(attribute, str) and attribute.strip())
        ):
            # A filming/route order arranges every clip, so its attribute is only the
            # creator's words for the order: the quote, which the fence below checks.
            data["attribute"] = data.get("source_quote")
        creator_text = data.get("creator_text")
        text_max = CREATOR_CAPTION_MAX_CHARS if data.get("op") == "caption" else _CREATOR_TEXT_MAX
        if isinstance(creator_text, str) and len(creator_text) > text_max:
            raise _IntentRejected(
                "creator_text_too_long",
                f"creator_text is over {text_max} characters; copy only the exact"
                " on-screen words, not the whole sentence",
            )
        try:
            intent = PlannedClipIntent.model_validate(data)
        except ValidationError as exc:
            fields = sorted(
                {
                    re.sub(r"[^a-z_]", "", str(err["loc"][0]).lower())
                    for err in exc.errors()
                    if err.get("loc")
                }
            )
            # pydantic `msg` strings carry no input values (those live in `input`),
            # so they are safe to relay to the retry prompt.
            messages = "; ".join(str(err.get("msg", "")) for err in exc.errors())[:200]
            raise _IntentRejected(
                "intent_invalid:" + (",".join(fields) or "model"),
                f"failed validation on {', '.join(fields) or 'intent'} ({messages})",
            ) from exc
        # KRI-677: keep the creator's words, not the brief's JSON escapes, on the intent
        # itself; source_quote and creator_text are shown back to the creator and burned.
        unescaped = {
            field: _JSON_ESCAPES.sub(r"\1", value)
            for field in ("source_quote", "creator_text")
            if isinstance(value := getattr(intent, field), str)
            and _JSON_ESCAPES.sub(r"\1", value) != value
        }
        if unescaped:
            intent = intent.model_copy(update=unescaped)
        quote = _norm(intent.source_quote)
        if (not quote or not any(quote in source for source in sources)) and intent.op == "group":
            # A group prints nothing, so its quote is only evidence the creator asked
            # for it. When the model garbled the quote but the creator's own sentence
            # names the group ("we played football, then dodgeball ... group content by
            # sport"), pick that sentence ourselves instead of making them restate it.
            repaired = _sentence_naming(intent.attribute, sources)
            if repaired is not None:
                intent = intent.model_copy(update={"source_quote": repaired})
                quote = _norm(repaired)
        if quote and not any(quote in source for source in sources):
            text = _norm(intent.creator_text) if intent.creator_text is not None else None
            repaired = _unstitched_quote(quote, text, sources)
            if repaired is not None:
                intent = intent.model_copy(update={"source_quote": repaired})
                quote = repaired
        if not quote or not any(quote in source for source in sources):
            raise _IntentRejected(
                "source_quote_not_creator_text",
                "source_quote is not an exact contiguous substring of the creator text",
            )
        if intent.creator_text is not None:
            text = _norm(intent.creator_text)
            if text not in quote and (repaired := _quote_with_text(quote, text, sources)):
                intent = intent.model_copy(update={"source_quote": repaired})
                quote = repaired
            if text not in quote or not any(text in source for source in sources):
                raise _IntentRejected(
                    "creator_text_not_source_backed",
                    "creator_text must be exact creator words inside source_quote",
                )
            creator_form = _creator_form(intent.creator_text, input)
            if creator_form != intent.creator_text:
                intent = intent.model_copy(update={"creator_text": creator_form})
        if intent.order_by is not None and not input.clip_facts:
            # The flag-off prompt never teaches `order_by`, so a stray one behaves as if the
            # field did not exist: dropped without retrying the model for nothing.
            return None
        return intent

    def schema_clarification(self) -> str:
        hint = getattr(self, "_last_schema_error", "")
        return (
            "\n\nReturn only the requested JSON. Every intent needs an exact "
            "source_quote copied from the creator text and a non-empty attribute; "
            "do not emit a partial inventory or invent clip facts. For caption, "
            "attribute is always the target clips/chapter. When creator_text has "
            "exact copy, caption_attribute must be null; otherwise caption_attribute "
            "is only the factual topic to author."
            + (f"\nFix this schema error: {hint}" if hint else "")
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "ClipIntentPlannerAgent",
    "ClipIntentPlannerInput",
    "ClipIntentPlannerOutput",
    "PlannedClipIntent",
]
