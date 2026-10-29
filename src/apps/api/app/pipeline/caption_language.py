"""Caption-language policy for talking-to-camera and voiceover captions (KRI-177, KRI-838).

Default rule: captions are in the language the person SPEAKS. The only thing that
may change that is the creator explicitly asking for captions in another language
("captions in English", "altyazılar Türkçe olsun"). Nothing else — the job's
UI/content language, a hard-coded "en", a previous item — may silently win.

KRI-838: a request used to be honoured by *labelling* — whisper was hinted with the
requested language and the variant was stamped with it, whatever text came back.
a whisper language hint does not reliably translate (local whisper.cpp medium, an
approximation of prod's whisper-1, wrote English again for a Turkish hint on English
speech), so "Turkish captions" shipped English words under a "tr" label and the review
believed the label. The helpers below let every side judge the TEXT:

- ``CAPTION_LANGUAGES`` — the one table: code, English and Turkish name, and every
  spelling a creator types. The brief side, the dispatcher and the worker all read it.
- ``detect_text_language`` / ``caption_language_verdict`` — a deterministic,
  dependency-free language-ID over caption text (closed-class function words plus
  script/character evidence). Three-valued on purpose: "unknown" for short or
  ambiguous text is not a failure, only "contradicted" is.
- ``can_deliver_caption_language`` — the ONE rule for whether Nova can honour a
  request, used by the pre-approval promise and the worker alike.

Pure helpers:

- ``parse_caption_language_request`` — grounded, deterministic extraction of an
  explicit caption-language request from the creator's own words. Requires a
  caption/subtitle keyword AND a language name in the same clause, so a prompt
  merely *written* in Turkish never changes the caption language.
- ``caption_language_decision`` — KRI-1015: the same parse as a decision, one message per
  line (a later message corrects an earlier one). A creator who captions two languages each
  as itself ("English captions for the English, Turkish captions for my Turkish") keeps the
  take as spoken: no target, ``keep_spoken``.
- ``coerce_caption_language_request`` — render-time re-validation of the persisted
  request (supported code or None).
- ``infer_language_from_text`` — a conservative EN/TR guess from transcript text,
  used when whisper reports no language, and to cross-check the one it reports.
- ``crosscheck_detected_language`` — whisper's detection vs an independent
  transcript of the same clip (Gemini's); returns the language to re-transcribe
  in when the two clearly disagree.
- ``resolve_spoken_caption_language`` — detected → transcript text → deliberate,
  recorded fallback. Returns the source so callers can trace the decision.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.text_fold import loose_case
from app.text_fold import lower as text_lower


@dataclass(frozen=True)
class CaptionLanguage:
    """One language a caption request can name.

    ``words`` are the exact creator spellings, ``stems`` the Turkish exonyms that take
    case endings ("ingilizce" -> "ingilizceye"). Both are stored the way ``loose_case``
    folds them (lowercase, diacritics stripped, Turkish i-letters folded).
    """

    code: str
    english: str
    turkish: str
    words: tuple[str, ...]
    stems: tuple[str, ...] = ()


CAPTION_LANGUAGES: tuple[CaptionLanguage, ...] = (
    CaptionLanguage("en", "English", "İngilizce", ("english",), ("ingilizce",)),
    CaptionLanguage("tr", "Turkish", "Türkçe", ("turkish",), ("turkce",)),
    CaptionLanguage("de", "German", "Almanca", ("german", "deutsch"), ("almanca",)),
    CaptionLanguage("es", "Spanish", "İspanyolca", ("spanish", "espanol"), ("ispanyolca",)),
    CaptionLanguage("fr", "French", "Fransızca", ("french", "francais"), ("fransizca",)),
    CaptionLanguage("it", "Italian", "İtalyanca", ("italian", "italiano"), ("italyanca",)),
    CaptionLanguage("ar", "Arabic", "Arapça", ("arabic",), ("arapca",)),
    CaptionLanguage(
        "nl",
        "Dutch",
        "Felemenkçe",
        ("dutch", "nederlands"),
        ("hollandaca", "felemenkce", "flemenkce"),
    ),
    CaptionLanguage("pt", "Portuguese", "Portekizce", ("portuguese", "portugues"), ("portekizce",)),
)

# The languages a caption request may name; ``can_deliver_caption_language`` decides
# which of them the worker can actually honour.
SUPPORTED_CAPTION_LANGUAGES: frozenset[str] = frozenset(lang.code for lang in CAPTION_LANGUAGES)

# code -> (English name, Turkish name)
LANGUAGE_NAMES: dict[str, tuple[str, str]] = {
    lang.code: (lang.english, lang.turkish) for lang in CAPTION_LANGUAGES
}
# Every spelling a creator types (diacritics stripped) -> language code.
LANGUAGE_WORDS: dict[str, str] = {
    spelling: lang.code for lang in CAPTION_LANGUAGES for spelling in (*lang.words, *lang.stems)
}

# What the whisper language hint alone has always delivered. With translation off,
# only these two keep being promised (the hint plus the text check decide the rest).
_HINT_ONLY_LANGUAGES: frozenset[str] = frozenset({"en", "tr"})
# In the table (names, language-ID, the parser) but never promised or rendered: the cloud
# renderer's caption fonts (Playfair Display and the rest) and right-to-left shaping were
# never verified for Arabic, so promising Arabic captions risks a promise followed by empty
# boxes. The draft says "can't" before approval and the worker never hears the request.
_NOT_DELIVERABLE: frozenset[str] = frozenset({"ar"})

# Sources reported by ``resolve_spoken_caption_language``.
SOURCE_DETECTED = "detected"
SOURCE_TRANSCRIPT_TEXT = "transcript_text"
SOURCE_FALLBACK = "fallback"


def language_name(code: str | None, *, turkish: bool = False) -> str:
    """The display name of ``code`` ("es" -> "Spanish"/"İspanyolca"); the upper-cased code
    for a language outside the table."""
    key = (code or "").strip().casefold().split("-")[0]
    english, tr = LANGUAGE_NAMES.get(key, (key.upper(), key.upper()))
    return tr if turkish else english


def can_deliver_caption_language(code: str | None) -> bool:
    """Whether Nova can honour a request for captions in ``code``.

    The ONE rule behind the pre-approval promise (``brief_checks._caption_words_plan``)
    and the worker (``coerce_caption_language_request``, so the flag off means exactly the
    legacy en/tr world at render time): a language in the table that the renderer can
    show, and either the translation step is on or the language is one the whisper hint
    alone has always delivered (en/tr). Anything else is answered honestly before approval
    instead of being promised and then rendered in the spoken language.
    """
    from app.config import settings  # noqa: PLC0415 - keeps this module import-light

    key = (code or "").strip().casefold()
    if key not in SUPPORTED_CAPTION_LANGUAGES or key in _NOT_DELIVERABLE:
        return False
    return bool(getattr(settings, "caption_translation_enabled", False)) or (
        key in _HINT_ONLY_LANGUAGES
    )


def _fold(text: str) -> str:
    """Lowercase + ASCII-fold Turkish letters so one pattern matches both spellings.

    The shared Turkish-aware fold (KRI-781), line breaks kept for the clause split.
    """
    return loose_case(text or "")


# Words that name captions, as a regex source (folded text: "altyazı" is "altyazi").
_CW = r"(?:captions?|captioned|captioning|subtitles?|subtitled|subs|alt\s?yazi\w*)"
_CAPTION_KEYWORD = re.compile(rf"\b{_CW}\b")

# What may follow a bare "captions <language>" for the language to be the ask ("make the
# subtitles Spanish.", "...Spanish please", "altyazılar Türkçe olsun"): the end of the
# clause, punctuation, or a word that closes a request. "subtitles Spanish food intro",
# "captions Dutch oven" and "subtitles Italian restaurant style" name a topic, not a
# language for the captions. Also used by the brief side's identical bare pattern.
ASK_END = (
    r"(?=\s*(?:$|[.,;:!?)\]\"']|\b(?:please|pls|plz|too|also|for|and|or|so|then|now|instead|"
    r"but|olsun|olacak|olarak|olsa|lutfen|yap\w*|ver|ekle\w*|koy\w*|ve|ile|veya)\b))"
)


def _language_source(lang: CaptionLanguage) -> str:
    """The regex source of one language's spellings (Turkish stems take case endings,
    with or without the apostrophe: "ingilizceye", "İngilizce'ye")."""
    exact = "|".join(re.escape(word) for word in lang.words)
    stems = "|".join(re.escape(stem) for stem in lang.stems)
    return rf"(?:(?:{exact}|(?:{stems})\w*)(?:'\w+)?)"


_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (lang.code, re.compile(rf"\b{_language_source(lang)}\b")) for lang in CAPTION_LANGUAGES
)


def _binding_patterns(lang: CaptionLanguage) -> tuple[re.Pattern[str], ...]:
    """The ways a language is BOUND to the captions in a clause; group 1 is the language.

    A caption keyword and a language name in one clause is not an ask ("Spanish food with
    subtitles", "Lucas speaks Spanish so add subtitles", "German shepherd video with
    captions"): the language must stand next to the caption word.
    """
    lng = _language_source(lang)
    return (
        # "Spanish subtitles", "İspanyolca bir altyazı", "Spanish-language captions"
        re.compile(rf"\b({lng})(?:[-\s]language)?\s+(?:bir\s+)?{_CW}\b"),
        # "captions in/into/to/as Spanish"
        re.compile(rf"\b{_CW}\s+(?:in|into|to|as)\s+({lng})\b"),
        # "subtitles for the Turkish audience in English"
        re.compile(rf"\b{_CW}\s+(?:\w+\s+){{1,4}}?(?:in|into|to|as)\s+({lng})\b{ASK_END}"),
        # "make the subtitles Spanish", "subtitles should be Spanish", "altyazılar da
        # Türkçe olsun", "captions: Spanish"
        re.compile(
            rf"\b{_CW}(?:\s*[:=]\s*|\s+)(?:(?:da|de)\s+)?"
            rf"(?:(?:\w+\s+){{0,2}}?(?:be|are|is)\s+)?({lng})\b{ASK_END}"
        ),
        # "altyazıyı İngilizceye çevir", "İngilizce olarak"
        re.compile(rf"\b({lng})\s+(?:olarak|cevir\w*)\b"),
        # "translate the subtitles into Spanish"
        re.compile(rf"\btranslat\w*\s+(?:\w+\s+){{0,3}}?(?:in|into|to)\s+({lng})\b"),
    )


_BINDINGS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (lang.code, _binding_patterns(lang)) for lang in CAPTION_LANGUAGES
)
# A language that names the voice, never the captions: "he speaks Spanish", "his English
# voice", "İngilizce konuşuyor" (checked on the words just before the language).
_SPOKEN_BEFORE = re.compile(
    r"\b(?:speak\w*|spoken|voice\w*|konus\w*|anlat\w*|dub\w*)\s+(?:(?:in|the|with)\s+)?$"
)
# Two languages joined into one mention ("Spanish and English subtitles").
_JOINED = re.compile(r"^\s*(?:and|or|&|\+|/|ve|ile|veya)?\s*$")
# A clause ends at sentence punctuation or a contrastive conjunction — "Türkçe
# konuşuyorum AMA altyazılar İngilizce olsun" binds İngilizce to the captions.
_CLAUSE_SPLIT = re.compile(r"[.!?;,\n]+|\b(?:but|though|however|ama|fakat|ancak|lakin)\b")
_NEGATION = re.compile(
    r"\b(?:don'?t|do\s+not|dont|never|without|no|not|"
    r"istemiyorum|istemem|olmasin\w*|degil|yapma|koyma)\b"
)
# In a clause naming two languages, the target is the one after a direction word
# ("turkish captions IN english", "İngilizceye çevir").
_TARGET_MARKER = re.compile(r"\b(?:in|into|to)\s+$")
# Turkish dative of an exonym ending in a vowel: "İngilizceye", "İspanyolcaya",
# "Almancaya", with or without the apostrophe ("İngilizce'ye").
_DATIVE = re.compile(r"\w*y[ae]\b|\w+['’]y[ae]\b")


def _bound_mentions(clause: str) -> list[tuple[int, int, str]]:
    """(start, end, code) of every language in ``clause`` that is bound to its captions."""
    bound: list[tuple[int, int, str]] = []
    for code, patterns in _BINDINGS:
        for pattern in patterns:
            for found in pattern.finditer(clause):
                pos = found.start(1)
                if _SPOKEN_BEFORE.search(clause[:pos]):
                    continue
                bound.append((pos, found.end(1), code))
    if not bound:
        return []
    # "Spanish and English subtitles": a language joined to a bound one is bound too, so
    # the two conflict (and nothing is requested) instead of the nearer one winning.
    mentions = sorted(
        (m.start(), m.end(), code)
        for code, pattern in _LANGUAGE_PATTERNS
        for m in pattern.finditer(clause)
    )
    changed = True
    while changed:
        changed = False
        for start, end, code in mentions:
            if (start, end, code) in bound:
                continue
            if any(
                (_JOINED.match(clause[b_end:start]) and b_end <= start)
                or (_JOINED.match(clause[end:b_start]) and end <= b_start)
                for b_start, b_end, _ in bound
            ):
                bound.append((start, end, code))
                changed = True
    return bound


@dataclass(frozen=True)
class CaptionLanguageDecision:
    """What the creator's words say about the caption language (KRI-1015).

    ``target`` is the one language the captions are asked in. ``keep_spoken`` is the
    creator captioning two languages each as itself (a mixed take keeps every stretch in
    the language it is spoken in): no target, and nothing may be translated.
    """

    target: str | None = None
    keep_spoken: bool = False


def _message_decision(message: str) -> CaptionLanguageDecision | None:
    """The caption-language decision of ONE creator message; None when it makes none."""
    asked: set[str] = set()
    keep_spoken = False
    for clause in _CLAUSE_SPLIT.split(message):
        if not clause or not _CAPTION_KEYWORD.search(clause):
            continue
        if _NEGATION.search(clause):
            continue
        hits = _bound_mentions(clause)
        codes = {code for _, _, code in hits}
        if not codes:
            continue
        # "captions in English where he speaks English": captioned in the language spoken
        # there, so the take stays as spoken.
        if any(
            code in codes
            and (m.start(), m.end(), code) not in hits
            and _SPOKEN_BEFORE.search(clause[: m.start()])
            for code, pattern in _LANGUAGE_PATTERNS
            for m in pattern.finditer(clause)
        ):
            keep_spoken = True
            continue
        if len(codes) == 1:
            asked.add(next(iter(codes)))
            continue
        targeted = {code for pos, _, code in hits if _TARGET_MARKER.search(clause[:pos])}
        # Turkish dative marks the target too: "İngilizceye çevir".
        targeted |= {code for pos, _, code in hits if _DATIVE.match(clause, pos)}
        # "subtitles in English and Turkish": a language joined to a targeted one is
        # targeted with it.
        grown = True
        while grown:
            grown = False
            for start, end, code in hits:
                if code not in targeted and any(
                    (_JOINED.match(clause[t_end:start]) and t_end <= start)
                    or (_JOINED.match(clause[end:t_start]) and end <= t_start)
                    for t_start, t_end, t_code in hits
                    if t_code in targeted
                ):
                    targeted.add(code)
                    grown = True
        if len(targeted) == 1:
            asked.update(targeted)
        else:
            keep_spoken = True
    if keep_spoken or len(asked) > 1:
        return CaptionLanguageDecision(keep_spoken=True)
    return CaptionLanguageDecision(target=asked.pop()) if asked else None


def caption_language_decision(text: str | None) -> CaptionLanguageDecision:
    """The caption-language decision in ``text``: each line is one creator message, and the
    last message that decides wins (a later message corrects an earlier one)."""
    folded = _fold(text or "").replace("’", "'")
    decision = CaptionLanguageDecision()
    for line in folded.splitlines():
        decision = _message_decision(line) or decision
    return decision


def parse_caption_language_request(text: str | None) -> str | None:
    """Return the language code when the creator explicitly asks for captions in it.

    Grounded on the creator's own words: a clause must contain a caption/subtitle
    keyword, a language name BOUND to it ("Spanish subtitles", "captions in Spanish",
    "make the subtitles Spanish", "İspanyolca altyazı"), and must not be negated. Returns
    None otherwise — including when the prompt is merely written in a language, names a
    language as a topic, the voice or the audience ("Spanish food with subtitles", "his
    English voice", "Spanish speakers"), or requests conflict. Two languages each
    captioned as itself in one message ("English captions for the English, Turkish
    captions for my Turkish") request nothing; across messages (one per line) the later
    message decides.
    """
    return caption_language_decision(text).target


def coerce_caption_language_request(value: object) -> str | None:
    """Re-validate a persisted ``caption_language_request`` at render time.

    The dispatcher parses any table language, but the worker honours only what
    ``can_deliver_caption_language`` says it can: with translation off that is exactly the
    legacy en/tr world (so the draft's "I can't make Spanish captions yet" is also what the
    render does), and Arabic never. A stray or legacy value must never silently become a
    caption-language override.
    """
    if not isinstance(value, str):
        return None
    code = value.strip().lower()
    return code if can_deliver_caption_language(code) else None


_TR_CHARS = re.compile(r"[ğşıİçöüĞŞÇÖÜ]")
_TR_WORDS = frozenset(
    "ve bir bu çok ama için ne da de ben sen biz mi mı değil gibi daha şey var yok "
    "olarak ile diye ki şu o bence yani evet hayır nasıl neden".split()
)
_EN_WORDS = frozenset(
    "the and is you to of i it that this in for with my we are was what so just "
    "have be not but they on your do".split()
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def infer_language_from_text(text: str | None) -> str:
    """Conservative EN/TR guess from transcript text. "" when not clearly one of them."""
    tokens = [t.lower() for t in _WORD.findall(text or "")]
    if len(tokens) < 3:
        return ""
    tr_score = sum(1 for t in tokens if t in _TR_WORDS or _TR_CHARS.search(t))
    en_score = sum(1 for t in tokens if t in _EN_WORDS)
    if tr_score >= 2 and tr_score >= 2 * max(en_score, 1):
        return "tr"
    if en_score >= 2 and en_score >= 2 * max(tr_score, 1):
        return "en"
    return ""


def crosscheck_detected_language(detected: str | None, *, reference_text: str | None) -> str | None:
    """The language an independent transcript of the SAME clip clearly speaks,
    when it contradicts whisper's detection; None when they agree or either is unknown.

    whisper-1 detects the language from the audio alone and misreads accented
    speech (Turkish-accented English → "tr"), then TRANSLATES into that language
    instead of transcribing. ``reference_text`` is Gemini's clip transcript, which
    every ingested clip already has, so a clear EN/TR disagreement costs no extra call.
    """
    lang = (detected or "").strip().lower()
    reference = infer_language_from_text(reference_text)
    if lang and reference and reference != lang:
        return reference
    return None


def resolve_spoken_caption_language(
    detected: str | None, *, transcript_text: str | None, fallback: str | None
) -> tuple[str, str]:
    """Pick the caption language for a talking-to-camera render.

    Order: whisper's detected language → a guess from the transcript text →
    ``fallback`` (the job language, else "en"). The fallback is deliberate and the
    returned source lets the caller record it — it never wins silently.
    """
    lang = (detected or "").strip().lower()
    if lang:
        return lang, SOURCE_DETECTED
    inferred = infer_language_from_text(transcript_text)
    if inferred:
        return inferred, SOURCE_TRANSCRIPT_TEXT
    return ((fallback or "").strip().lower() or "en"), SOURCE_FALLBACK


# ---------------------------------------------------------------- text language-ID
#
# No language-ID library is installed (and the prod image would have to ship one), so
# this is a small deterministic scorer: closed-class function words per language — the
# words every sentence is made of and proper nouns never are — plus script/character
# evidence. A word several languages share gives each of them an equal fraction of one
# point, so the Spanish/Portuguese/Italian overlap cancels instead of tipping the call.
# It is conservative by design: short or ambiguous text is "unknown", never a guess.

_FUNCTION_WORDS_SRC: dict[str, str] = {
    "en": (
        "the and is are was were be been to of in that it you he she we they this these those "
        "with for on at by from as but or if so not have has had do does did will would can "
        "could should my your his her our their what which who how when where why there here "
        "just very really all some an its i me him them us than then into about because also "
        "too only out up now am don't i'm it's that's i'll i've i'd you're we're they're "
        "let's isn't can't didn't what's there's he's she's here's won't doesn't wasn't "
        "aren't couldn't wouldn't shouldn't haven't hasn't thank thanks hello yes okay "
        "going gonna want need make get got see know think like love good great nice really "
        "much many more most other another every each any no one two three"
    ),
    "tr": (
        "ve bir bu şu o çok ama için ne da de ben sen biz siz onlar mi mı mu mü değil gibi "
        "daha şey var yok olarak ile diye ki bence yani evet hayır nasıl neden niye nerede "
        "kim hangi her bütün tüm sadece sonra önce şimdi bugün yarın dün burada orada "
        "çünkü eğer belki hem veya ya hiç bile kadar artık zaten tamam merhaba selam "
        "teşekkürler lütfen güzel iyi olur olsun oldu bunu şunu onu bana sana ona bizim "
        "benim senin onun bize size onlara bunun hadi haydi kendi başka diğer aynı hepsi "
        "hep bazı biraz göre sanki tabii tabi elbette ayrıca üstelik hatta oysa lazım "
        "gerek istiyorum biliyorum sevdim bence bizce sizce"
    ),
    "es": (
        "el los las un una unos unas y que qué del al es son está están estoy estás soy eres "
        "somos fue ser estar hay tiene tengo tenemos tienen hacer hace puede pueden quiero "
        "quieres con para por pero como cómo muy más esto esta este eso esa ese estos estas "
        "aquí acá allí ahí ahora siempre nunca también porque cuando cuándo donde dónde "
        "quién nosotros ustedes usted yo tú vos él ella ellos ellas mi mis tu tus su sus "
        "nuestro nuestra lo les se me te nos si sí no ya todo todos todas nada algo mucho "
        "mucha muchas gracias hola bueno buenos buenas días vamos vale entonces después "
        "antes sobre entre sin hasta desde otro otra tan así cada de la le en hoy mañana "
        "ayer bien mal mejor peor gente según durante contra mientras aunque pues sino "
        "tampoco además alguien nadie mismo misma aquel aquella cuál cuánto cuántos sé "
        "sabes creo dice dijo veo voy vas va van vengo venimos estaba había hemos han "
        "ni pero tal fueron será sería tenía"
    ),
    "pt": (
        "o os as um uma uns umas não sim é são está estou você vocês nós eu ele ela eles "
        "elas meu minha seu sua isso isto esse essa aquele mais muito também com para pra "
        "por mas como quando onde porque obrigado obrigada olá bom boa dia tem têm foi ser "
        "ter fazer faz pode podem quero aqui ali agora sempre nunca já ainda só então "
        "depois antes do da dos das no na nos nas ao aos à às num numa pelo pela de em e "
        "que se me te lhe vai vão vou temos tenho nosso nossa todos todas vamos hoje "
        "amanhã ontem até ótimo legal gente né coisa aí lá meus minhas seus suas pelos "
        "pelas dele dela deles delas nem ou mesmo outro outra cada tudo nada algo quem "
        "qual quais bem sobre entre sem desde durante contra através muitos muitas era "
        "estava tinha foram será seria"
    ),
    "it": (
        "il lo la i gli le un uno una di da in con su per tra fra e ed o ma che chi cui non "
        "sì sono sei è siamo siete ho hai ha abbiamo avete hanno questo questa questi queste "
        "quello quella mio mia tuo tua suo sua nostro dove quando perché molto più anche "
        "ancora sempre mai già solo tutto tutti ciao grazie prego buongiorno buonasera bene "
        "io tu lui lei noi voi loro mi ti ci vi si ne del dello della dei degli delle "
        "al allo alla ai agli alle dal dalla nel nella nei nelle sul sulla fare fatto può "
        "posso voglio andiamo qui qua là ora adesso oggi domani ieri cosa così dopo prima "
        "senza sopra sotto dentro fuori ogni alcuni ecco allora però quindi poi stato "
        "essere avere l' un' dell' nell' all' sull' dall' c'è faccio fai fanno"
    ),
    "fr": (
        "le la les un une des du de au aux et ou mais donc car ni que qui quoi dont où ne pas "
        "plus jamais rien je tu il elle on nous vous ils elles me te se lui leur y en ce cet "
        "cette ces ça c'est mon ma mes ton ta tes son sa ses notre nos votre vos leurs est "
        "sont suis es êtes sommes été être avoir ai avons avez ont fait faire peut peux "
        "veux vais va allons dans sur sous avec sans pour par chez vers très trop bien tout "
        "tous toute toutes comme comment quand pourquoi parce aussi encore déjà toujours "
        "maintenant ici là oui non bonjour bonsoir merci salut voilà alors puis ensuite "
        "après avant si j' l' d' qu' n' s' m' t' c' jusqu' lorsqu' puisqu' s'il j'ai "
        "aujourd'hui beaucoup vraiment très ensemble depuis pendant entre sont étaient "
        "était sera serait cela celui celle ceux"
    ),
    "de": (
        "der die das den dem des ein eine einen einem einer und oder aber nicht kein keine "
        "ich du er sie es wir ihr mich mir dich dir ihn ihm uns euch mein meine dein sein "
        "seine unser ist sind bin bist seid war waren hat haben habe hast hatte wird werden "
        "wurde kann können muss müssen will wollen soll auf aus bei mit nach von zu zum zur "
        "für über unter vor durch gegen ohne um im am vom beim auch noch schon sehr nur "
        "dann wenn dass weil als wie was wer wo warum hier dort heute jetzt immer nie ja "
        "nein bitte danke hallo guten tag gut alles mehr viel ganz vielen fürs bis mal "
        "nächsten nächste morgen abend wieder gerade etwas nichts alle jede jeder diese "
        "dieser dieses welche damit dafür deshalb sondern obwohl während bevor nachdem "
        "zwischen seit ins ans aufs in an"
    ),
    "nl": (
        "de het een en van in is dat die dit deze niet geen ik jij je u hij zij ze wij we "
        "jullie mij me hem haar ons mijn jouw zijn onze hun was waren ben bent heb hebt "
        "heeft hebben wordt worden kan kunnen moet moeten wil willen zal zullen op aan met "
        "voor naar uit bij om tot te ook nog wel al heel veel erg maar of als "
        "dan dus omdat want toen wat wie waar hoe waarom hier daar nu altijd nooit ja nee "
        "alsjeblieft dank bedankt hallo goed mooi allemaal vandaag volgende keer"
    ),
    "ar": "",
}
_SPLIT_WORDS = {code: frozenset(src.split()) for code, src in _FUNCTION_WORDS_SRC.items()}
# word -> languages that list it (a shared word scores 1/len(languages) for each).
_WORD_LANGUAGES: dict[str, tuple[str, ...]] = {}
for _code, _words in _SPLIT_WORDS.items():
    for _word in _words:
        _WORD_LANGUAGES[_word] = (*_WORD_LANGUAGES.get(_word, ()), _code)
del _code, _words, _word

# Characters only (or mostly) one language uses; per distinct character, capped per token.
_CHAR_EVIDENCE: dict[str, dict[str, float]] = {
    **dict.fromkeys("ğşıİ", {"tr": 2.0}),
    **dict.fromkeys("ñ¿¡", {"es": 2.0}),
    "ß": {"de": 2.0},
    **dict.fromkeys("ãõ", {"pt": 2.0}),
    "œ": {"fr": 2.0},
    # Weak: Finnish, Swedish and Hungarian use these too, so a few of them never decide.
    "ä": {"de": 0.25},
    **dict.fromkeys("öü", {"de": 0.25, "tr": 0.25}),
    **dict.fromkeys("ëï", {"fr": 0.5, "nl": 0.5}),
    **dict.fromkeys("îû", {"fr": 1.0}),
    **dict.fromkeys("êô", {"fr": 0.5, "pt": 0.5}),
    **dict.fromkeys("èù", {"fr": 0.5, "it": 0.5}),
    **dict.fromkeys("òì", {"it": 1.0}),
    **dict.fromkeys("áíóú", {"es": 0.25, "pt": 0.25}),
}
_EN_CONTRACTION = re.compile(r"^[a-z]+(?:n't|'(?:s|re|ve|ll|d|m))$")
_TR_VERB_ENDING = re.compile(r"^\w{2,}[iıuü]yor(?:um|uz|sun|sunuz|lar)?$")
_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*['’]?|[¿¡]")
# Arabic, Arabic Supplement, and the two Arabic presentation-form blocks.
_ARABIC = re.compile(
    "["
    + "-".join(chr(c) for c in (0x0600, 0x06FF))
    + "-".join(chr(c) for c in (0x0750, 0x077F))
    + "-".join(chr(c) for c in (0xFB50, 0xFDFF))
    + "-".join(chr(c) for c in (0xFE70, 0xFEFC))
    + "]"
)
_SENTENCE_END = frozenset('.!?…:¿¡\n"“”()—-–')
_MIN_EVIDENCE = 2.0  # points the winner needs (about two clear function words)
_MIN_RATIO = 1.8  # winner vs runner-up
_MIN_GAP = 1.0


def _lower(token: str) -> str:
    # The shared Turkish-aware casing ("İlk" -> "ilk", "IŞIK" -> "ışık"); never casefold,
    # which would turn "ß" into "ss" and erase German's one decisive letter.
    return text_lower(token.replace("’", "'"))


def _ignored_positions(tokens: list[str], terms: Sequence[str]) -> set[int]:
    """Indexes of the tokens that sit inside one of the creator's own terms."""
    if not terms:
        return set()
    folded = [loose_case(t) for t in tokens]
    masked: set[int] = set()
    for term in terms:
        wanted = [loose_case(t) for t in _TOKEN.findall(term or "")]
        if not wanted:
            continue
        for start in range(len(folded) - len(wanted) + 1):
            if folded[start : start + len(wanted)] == wanted:
                masked.update(range(start, start + len(wanted)))
    return masked


def _score_languages(text: str, ignore_terms: Sequence[str] = ()) -> dict[str, float]:
    """Evidence points per language for ``text`` (empty when nothing counts)."""
    if not text or not text.strip():
        return {}
    if not any(ch.islower() for ch in text):
        text = text_lower(text)  # an all-caps caption has no capitalization to read
    spans = [(m.group(0), m.start()) for m in _TOKEN.finditer(text)]
    tokens = [token for token, _ in spans]
    masked = _ignored_positions(tokens, ignore_terms)
    scores: dict[str, float] = {}

    def add(lang: str, points: float) -> None:
        scores[lang] = scores.get(lang, 0.0) + points

    for index, (token, start) in enumerate(spans):
        if index in masked:
            continue
        if token in {"¿", "¡"}:
            add("es", 2.0)
            continue
        arabic = len(_ARABIC.findall(token))
        if arabic:
            if arabic * 2 >= len(token):
                add("ar", 3.0)
            continue
        # A capitalized word mid-sentence is a name ("Carolina", "Oblea", "Ecuador'da"),
        # an all-caps one an acronym — neither is evidence. "I" and "I'm" are English.
        before = text[:start].rstrip(" \t")
        sentence_start = not before or before[-1] in _SENTENCE_END
        head = token.replace("’", "'")
        english_i = head == "I" or head.startswith("I'")
        if not english_i and not sentence_start and token[:1].isupper():
            continue
        if len(token) > 1 and token.isupper() and not english_i:
            continue
        low = _lower(token)
        langs = _WORD_LANGUAGES.get(low)
        if langs is None and _EN_CONTRACTION.match(low):
            add("en", 1.0)
        elif langs is None and _TR_VERB_ENDING.match(low):
            add("tr", 1.0)
        elif langs is None and "'" in low:
            # Elisions and clitics: "l'amour" -> "l'" + "amour", "qu'il" -> "qu'" + "il".
            head_part, _, tail = low.partition("'")
            for piece in (head_part + "'", tail):
                piece_langs = _WORD_LANGUAGES.get(piece)
                if piece_langs:
                    for lang in piece_langs:
                        add(lang, 1.0 / len(piece_langs))
        elif langs:
            for lang in langs:
                add(lang, 1.0 / len(langs))
        # A capitalized word that is not itself a function word is a name even at the start
        # of a sentence ("Ayşe says hi to everyone."): it carries no character evidence.
        name_at_start = sentence_start and token[:1].isupper() and not english_i and langs is None
        chars = (
            {}
            if name_at_start
            else {ch: _CHAR_EVIDENCE[ch] for ch in set(low) if ch in _CHAR_EVIDENCE}
        )
        if chars:
            token_points: dict[str, float] = {}
            for weights in chars.values():
                for lang, points in weights.items():
                    token_points[lang] = token_points.get(lang, 0.0) + points
            for lang, points in token_points.items():
                add(lang, min(points, 2.0))
    return scores


def detect_text_language(text: str | None, *, ignore_terms: Sequence[str] = ()) -> str | None:
    """The table language ``text`` is clearly written in, else None.

    ``ignore_terms`` are the creator's own words (dish and place names they asked to
    keep spelled their way): they carry no language evidence. Capitalized words in the
    middle of a sentence are skipped the same way. None for short, mixed or ambiguous
    text, and for a language outside the table.
    """
    scores = _score_languages(text or "", ignore_terms)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < _MIN_EVIDENCE:
        return None
    if runner_up and (best_score < _MIN_RATIO * runner_up or best_score - runner_up < _MIN_GAP):
        return None
    return best


Verdict = Literal["confirmed", "contradicted", "unknown"]


def caption_language_verdict(
    texts: Iterable[str] | None, requested: str | None, *, ignore_terms: Sequence[str] = ()
) -> tuple[Verdict, str | None]:
    """Whether caption text is in the ``requested`` language, and what it was detected as.

    "confirmed": the text is clearly in ``requested``. "contradicted": it is clearly in
    another language (returned as the second value). "unknown": too short or ambiguous
    to say, or ``requested`` is not a language the table knows — a verdict a caller
    must treat as "no evidence", never as a failure. All the cues are read as one text,
    so a few lines of another language inside a mostly-right caption set do not flip it.
    """
    wanted = (requested or "").strip().casefold().split("-")[0]
    detected = detect_text_language(
        " ".join(t for t in (texts or ()) if t), ignore_terms=ignore_terms
    )
    if wanted not in SUPPORTED_CAPTION_LANGUAGES or detected is None:
        return "unknown", detected
    return ("confirmed" if detected == wanted else "contradicted"), detected
