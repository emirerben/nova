"""Pure cut-plan detection for automatic silence / filler / retake removal.

Given whisper word timings and ffmpeg silencedetect ranges for a clip, this
module decides which time ranges to remove and expresses the result as a
``CutPlan``. It is deliberately pure — no LLM, no FFmpeg, no I/O. Callers own
the detection inputs (transcribe + ``clip_speech.detect_silences``) and the
apply step (``reframe_and_export(keep_segments=…)``); see plans/010.

    audio path ─▶ clip_speech.detect_silences ──┐          (caller, ffmpeg)
                                                │ silences: [(start, end), …]
    Transcript.words ───────────────────────────┤ words: text/start_s/end_s
    retake_detector agent (word-index spans) ───┤          (caller, optional)
                                                ▼
                    build_cut_plan(words, silences, duration_s,
                                   retake_spans=…)
                      1. lexical fillers    (universal lexicon + guards)
                      2. acoustic fillers   (soundful short gaps)
                      3. pause tightening   (silence-intersected)
                      4. retake spans       (outward-snapped, never mid-word)
                      5. hygiene: MIN_CUT_S drop → merge →
                         micro-fragment absorb → safety rails
                                                │
                                                ▼
                    CutPlan(keep_segments, removed, time_saved_s)
                                                │
              ┌─────────────────────────────────┴──────────────────┐
              ▼                                                    ▼
    reframe_and_export(keep_segments=…)            remap_words(words, plan)
    (caller: ONE encode, per-segment                 → surviving words in
     trim/atrim + concat inside the graph)             cut-timeline coords
                                                       for caption cues

Timeline-rebase siblings (eng review 4A): ``remap_words`` is deliberately NOT
extracted into a shared utility with the two existing rebases —
``app/pipeline/lyric_injector.py`` (``_select_section_lines`` window clamp)
and ``app/pipeline/narrated_assembler.py`` (``_rebase_words_to_assembled``,
voiceover→assembled timeline). Their semantics differ (window-clamp vs
cross-timeline rebase vs the multi-segment deletion here) and both siblings
carry byte-identical prod guarantees; extract a shared abstraction only when
a fourth consumer proves the shape.

Whisper END-time drift (phrase_sequence.py D16: only start times are
trustworthy) is why pause cuts require silencedetect agreement — a word gap
with no intersecting silence range is NEVER cut by rule 3. silencedetect is
the ground-truth veto; word-gap arithmetic alone never removes a pause.

The ``has_audio`` pre-whisper gate lives in the caller, not here — reframe
injects silent AAC for audio-less clips and whisper on digital silence
hallucinates plausible words, so the caller must skip the whole stage before
transcription (eng review 3A).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise, product
from typing import Any, NamedTuple

# Whisper bias prompt for the CUT path: passed as whisper-1's ``prompt`` /
# faster-whisper's ``initial_prompt`` (transcribe(..., verbatim_prompt=…)) so the
# ASR keeps filler vocalizations as tokens instead of politely dropping them —
# rule 1 needs the tokens to cut them, and caption hygiene needs them to strip
# them from cue input. TR + EN because the product ships both; the trailing
# "dur baştan alayım" also primes restart phrasing for the retake detector.
# Both integrations (subtitled + talking_head) import THIS constant — a caller
# inlining its own copy would silently diverge the two paths' transcripts.
SILENCE_CUT_VERBATIM_PROMPT = "Iıı, eee, şey, yani... uh, um, hmm, dur baştan alayım."

# -- removal reasons (persisted in Job.assembly_plan — treat as API) --------------
REASON_SILENCE = "silence"
REASON_FILLER_LEXICAL = "filler_lexical"
REASON_FILLER_ACOUSTIC = "filler_acoustic"
REASON_RETAKE = "retake"

# -- bailout reasons (safety rails; each yields a no-op plan) ---------------------
BAILOUT_NO_WORDS = "no_words"
BAILOUT_CLIP_TOO_SHORT = "clip_too_short"
BAILOUT_MAX_REMOVAL = "max_removal_exceeded"
BAILOUT_OUTPUT_TOO_SHORT = "output_too_short"

# -- detection thresholds (explicit over configurable — plans/010) ----------------
MAX_PAUSE_S = 0.6  # inter-word gap at/above this is a tightenable pause
KEPT_GAP_S = 0.25  # residual gap kept around a pause cut (the kept residual IS the pad)
PAD_S = 0.12  # breathing room every kept word keeps on both flanks
PAD_ACOUSTIC_S = 0.15  # thicker flank for cuts silencedetect cannot confirm (rule 2)
MIN_CUT_S = 0.18  # removals shorter than this are not worth a jump cut
MAX_REMOVAL_FRAC = 0.4  # total removal above this fraction of the clip bails out
MIN_OUTPUT_S = 3.0  # cut output shorter than this bails out
MIN_CLIP_S = 5.0  # clips shorter than this are never cut
LEAD_KEEP_S = 0.3  # leading silence is trimmed down to this much, not to zero
TRAIL_KEEP_S = 0.5  # trailing silence is trimmed down to this much
ACOUSTIC_GAP_MIN_S = 0.15  # soundful gap must be at least this long to be a filler
ACOUSTIC_GAP_MAX_S = 1.2  # soundful gaps longer than this are left alone (laughter…)
AVG_LOGPROB_MIN = -1.0  # segment avg_logprob below this blocks lexical cuts
NO_SPEECH_PROB_MAX = 0.5  # segment no_speech_prob above this blocks lexical cuts
MIN_KEEP_SEGMENT_S = 0.25  # word-free keep fragments shorter than this are absorbed
KEEP_SEGMENTS_PUNCH_IN = 1.08  # alternating punch-in factor — user-validated 2026-07-09;
# integrations pass this to reframe_and_export(keep_segments_punch_in=…) so every
# render path produces the approved jump-cut style from one constant.

_EPS = 1e-9  # float-comparison tolerance for interval arithmetic


# ---------------------------------------------------------------------------------
# Filler lexicon
# ---------------------------------------------------------------------------------

# One UNIVERSAL non-lexical vocalization set, applied regardless of detected
# language. Real words ("şey", "like", "you know") are NEVER cut in v1.
_FILLER_LEXICON = frozenset(
    {
        "uh",
        "um",
        "er",
        "erm",
        "hmm",
        "mm",
        "mhm",
        "ıı",
        "ııı",
        "eee",
        "aaa",
        "ıh",
        # 2026-07-09 local-test round 2: user-reported escapes ("eh, ıh, o" class).
        # Bare "o" stays OUT ("o" = Turkish pronoun); "oo"+ elongations are safe.
        "eh",
        "ah",
        "oh",
        "oo",
    }
)
# Real Turkish exclamations — deliberately NOT fillers. Subtracted defensively;
# by construction no lexicon elongation image collapses onto them ("eee" only
# images to "eee": its run floor is 3).
_REAL_EXCLAMATIONS = frozenset({"ee", "aa"})
# Longest character run appearing in any lexicon entry ("ııı"/"eee"/"aaa").
# Tokens are collapsed to runs of at most this length before the membership
# check, so any elongation ("uhhhh", "ıııııı") lands on a precomputed image.
_MAX_LEXICON_RUN = 3


def _run_lengths(token: str) -> list[tuple[str, int]]:
    """Run-length encode: "uhh" → [("u", 1), ("h", 2)]."""
    runs: list[tuple[str, int]] = []
    for ch in token:
        if runs and runs[-1][0] == ch:
            runs[-1] = (ch, runs[-1][1] + 1)
        else:
            runs.append((ch, 1))
    return runs


def _collapse_runs(token: str, max_run: int = _MAX_LEXICON_RUN) -> str:
    """Collapse each character run to at most ``max_run`` repeats."""
    return "".join(ch * min(n, max_run) for ch, n in _run_lengths(token))


def _elongation_images(entry: str) -> Iterable[str]:
    """Every collapse-capped image an elongation of ``entry`` can produce.

    A token elongates ``entry`` when it has the same character skeleton and
    each run is at least as long as the entry's. After collapsing runs to
    ``_MAX_LEXICON_RUN`` such a token lands on one of these finite images:
    each run stretched from its lexicon floor up to the cap. "uh" yields
    {"uh", "uhh", …, "uuuhhh"}; "eee" yields only {"eee"} (floor 3 == cap),
    which is exactly why plain "ee" can never match.
    """
    runs = _run_lengths(entry)
    choices = [range(n, _MAX_LEXICON_RUN + 1) for _, n in runs]
    for combo in product(*choices):
        yield "".join(ch * k for (ch, _), k in zip(runs, combo))


_FILLER_MATCH_SET = (
    frozenset(image for entry in _FILLER_LEXICON for image in _elongation_images(entry))
    - _REAL_EXCLAMATIONS
)


def is_filler_token(text: str) -> bool:
    """True when a raw whisper token is a lexicon filler (incl. elongations).

    Normalization: lowercase, strip everything non-alphabetic (punctuation,
    digits, whitespace), collapse character runs to ``_MAX_LEXICON_RUN``,
    then membership-check against the precomputed elongation images.
    "Uh," / "uhhh" / "ıııı" match; "ee" / "aa" / real words never do.
    Public so caption hygiene (plans/010 15A) can strip filler tokens from
    cue input even when they were not cut.
    """
    normalized = "".join(ch for ch in str(text).lower() if ch.isalpha())
    if not normalized:
        return False
    return _collapse_runs(normalized) in _FILLER_MATCH_SET


# ---------------------------------------------------------------------------------
# Public plan types
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Removal:
    """One removed time range, tagged with the rule that produced it."""

    start_s: float
    end_s: float
    reason: str


@dataclass
class CutPlan:
    """Detection output: what to keep, what was removed, and why.

    ``keep_segments`` is sorted, non-overlapping, and together with
    ``removed`` exactly partitions ``[0, duration_s]``. A no-op plan keeps
    the whole clip; when a safety rail triggered it, ``bailout_reason``
    carries the rail name (see the BAILOUT_* constants).
    """

    keep_segments: list[tuple[float, float]]
    removed: list[Removal]
    time_saved_s: float
    version: int = 1
    bailout_reason: str | None = None


def no_op_plan(duration_s: float, bailout_reason: str | None = None) -> CutPlan:
    """Identity plan: keep the entire clip, remove nothing."""
    return CutPlan(
        keep_segments=[(0.0, float(duration_s))],
        removed=[],
        time_saved_s=0.0,
        bailout_reason=bailout_reason,
    )


# ---------------------------------------------------------------------------------
# Word normalization (accepts dicts or objects, like phrase_sequence)
# ---------------------------------------------------------------------------------


class _CutWord(NamedTuple):
    text: str
    start: float
    end: float
    avg_logprob: float | None
    no_speech_prob: float | None
    confidence: float | None


def _field(word: Any, names: tuple[str, ...]) -> Any:
    """First non-None value among ``names``, via dict key OR attribute access."""
    for name in names:
        if isinstance(word, dict):
            value = word.get(name)
        else:
            value = getattr(word, name, None)
        if value is not None:
            return value
    return None


def _normalize_words(words: Sequence[Any] | None) -> list[_CutWord]:
    """Coerce heterogeneous word records into timed tuples, sorted by start.

    Accepts transcribe.Word-style objects (``start_s``/``end_s``) and
    persisted plain dicts (``start_s``/``end_s`` or ``start``/``end``).
    Words with missing timestamps or empty text are skipped (defensive).
    """
    if not words:
        return []
    normalized: list[_CutWord] = []
    for word in words:
        text = _field(word, ("text", "word"))
        start = _field(word, ("start_s", "start"))
        end = _field(word, ("end_s", "end"))
        if text is None or start is None or end is None:
            continue
        if not str(text).strip():
            continue
        start_f = float(start)
        end_f = max(float(end), start_f)
        avg_logprob = _field(word, ("segment_avg_logprob",))
        no_speech = _field(word, ("segment_no_speech_prob",))
        confidence = _field(word, ("confidence",))
        normalized.append(
            _CutWord(
                text=str(text),
                start=start_f,
                end=end_f,
                avg_logprob=None if avg_logprob is None else float(avg_logprob),
                no_speech_prob=None if no_speech is None else float(no_speech),
                confidence=None if confidence is None else float(confidence),
            )
        )
    normalized.sort(key=lambda w: (w.start, w.end))
    return normalized


# ---------------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------------


def _normalize_silences(
    silences: Sequence[tuple[float, float]] | None, duration_s: float
) -> list[tuple[float, float]]:
    """Clamp silence ranges to the clip, drop empties, sort + merge overlaps."""
    spans: list[tuple[float, float]] = []
    for raw in silences or []:
        lo = max(0.0, float(raw[0]))
        hi = min(duration_s, float(raw[1]))
        if hi - lo > _EPS:
            spans.append((lo, hi))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _intersect_span(
    lo: float, hi: float, spans: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Pieces of ``[lo, hi]`` covered by the (merged, sorted) ``spans``."""
    pieces: list[tuple[float, float]] = []
    for s_lo, s_hi in spans:
        a = max(lo, s_lo)
        b = min(hi, s_hi)
        if b - a > _EPS:
            pieces.append((a, b))
    return pieces


def _overlaps_any(lo: float, hi: float, spans: list[tuple[float, float]]) -> bool:
    return any(min(hi, s_hi) - max(lo, s_lo) > _EPS for s_lo, s_hi in spans)


# ---------------------------------------------------------------------------------
# Rule 1 — lexical fillers
# ---------------------------------------------------------------------------------


def _segment_signals_allow(word: _CutWord) -> bool:
    """Quality guard: block cuts only on whisper hallucination signals.

    whisper-1 returns NO per-word confidence (transcribe.py hardcodes 1.0),
    so the guard rides SEGMENT-level signals mapped onto each word. A ``None``
    signal never blocks (the caller may not have segment data).

    Deliberately NO per-word confidence floor: fillers naturally score low
    ASR confidence (they are the sounds whisper is least sure about), so a
    floor blocks exactly the tokens this rule exists to cut — local test
    2026-07-09 saw it protect 2/4 real "um"s (conf 0.03/0.46) that prod
    (confidence hardcoded 1.0) would have cut. For NON-LEXICAL vocalization
    tokens the mis-cut downside is one padded vocalization-length span;
    hallucination protection stays with the segment signals above.
    """
    if word.avg_logprob is not None and word.avg_logprob < AVG_LOGPROB_MIN:
        return False
    if word.no_speech_prob is not None and word.no_speech_prob > NO_SPEECH_PROB_MAX:
        return False
    return True


def _lexical_removals(words: list[_CutWord], duration_s: float) -> list[Removal]:
    """Rule 1: cut lexicon fillers, padded but never eating adjacent words."""
    removals: list[Removal] = []
    for idx, word in enumerate(words):
        if not is_filler_token(word.text):
            continue
        if not _segment_signals_allow(word):
            continue
        lo = max(word.start - PAD_S, words[idx - 1].end if idx > 0 else 0.0)
        hi = min(word.end + PAD_S, words[idx + 1].start if idx + 1 < len(words) else duration_s)
        if hi - lo > _EPS:
            removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_FILLER_LEXICAL))
    return removals


# ---------------------------------------------------------------------------------
# Rule 2 — acoustic fillers (soundful gaps whisper left tokenless)
# ---------------------------------------------------------------------------------


def _acoustic_removals(
    words: list[_CutWord],
    silence_spans: list[tuple[float, float]],
    duration_s: float,
) -> list[Removal]:
    """Rule 2: bounded soundful inter-word gaps become filler cuts.

    CALIBRATION GATE: if the clip yielded ZERO silencedetect ranges the
    detector is blind there (noisy footage) and this rule produces nothing —
    aggressiveness must never scale WITH background noise. Any silence
    overlap inside the gap attributes it to rule 3 instead. Because these
    cuts cannot be silence-confirmed they wear the thicker PAD_ACOUSTIC_S
    flanks off the neighboring word boundaries.
    """
    if not silence_spans:
        return []
    removals: list[Removal] = []
    for prev, nxt in pairwise(words):
        gap = nxt.start - prev.end
        if gap < ACOUSTIC_GAP_MIN_S - _EPS or gap > ACOUSTIC_GAP_MAX_S + _EPS:
            continue
        if _overlaps_any(prev.end, nxt.start, silence_spans):
            continue
        lo = prev.end + PAD_ACOUSTIC_S
        hi = nxt.start - PAD_ACOUSTIC_S
        if hi - lo > _EPS:
            removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_FILLER_ACOUSTIC))
    return removals


# ---------------------------------------------------------------------------------
# Rule 3 — pause tightening (dual-signal intersection)
# ---------------------------------------------------------------------------------


def _pause_removals(
    words: list[_CutWord],
    silence_spans: list[tuple[float, float]],
    duration_s: float,
) -> list[Removal]:
    """Rule 3: tighten long pauses ONLY where silencedetect agrees.

    ``removed = (prev.end + KEPT_GAP_S/2, next.start − KEPT_GAP_S/2) ∩
    silence`` — the kept residual IS the padding (one mechanism, one
    constant). No intersection ⇒ no cut: whisper end times drift (D16), so
    word-gap arithmetic alone is never trusted. Leading silence is trimmed
    down to LEAD_KEEP_S, trailing to TRAIL_KEEP_S, both silence-confirmed.
    """
    removals: list[Removal] = []
    first, last = words[0], words[-1]
    if first.start > LEAD_KEEP_S:
        for lo, hi in _intersect_span(0.0, first.start - LEAD_KEEP_S, silence_spans):
            removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_SILENCE))
    for prev, nxt in pairwise(words):
        if nxt.start - prev.end < MAX_PAUSE_S - _EPS:
            continue
        window_lo = prev.end + KEPT_GAP_S / 2
        window_hi = nxt.start - KEPT_GAP_S / 2
        for lo, hi in _intersect_span(window_lo, window_hi, silence_spans):
            removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_SILENCE))
    if duration_s - last.end > TRAIL_KEEP_S:
        for lo, hi in _intersect_span(last.end + TRAIL_KEEP_S, duration_s, silence_spans):
            removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_SILENCE))
    return removals


# ---------------------------------------------------------------------------------
# Rule 4 — retake spans (caller-provided, LLM-detected)
# ---------------------------------------------------------------------------------


def _retake_removals(
    words: list[_CutWord],
    retake_spans: Sequence[tuple[int, int]] | None,
    duration_s: float,
) -> list[Removal]:
    """Map inclusive word-index spans to removals, snapping outward only.

    Boundaries snap to PADDED WORD BOUNDARIES of the surviving neighbors
    (``neighbor.end + PAD_S`` / ``neighbor.start − PAD_S``) — one of the two
    snap targets plans/010 allows; the other (silencedetect-confirmed
    boundaries) converges to the same merged interval whenever the inter-take
    gap is silent, because rule 3 cuts it and the hygiene pass merges the two.
    A boundary NEVER lands mid-word: when the gap is thinner than PAD_S the
    boundary clamps to the removed word's own edge. Spans at the clip edges
    extend to 0.0 / ``duration_s``. Malformed spans (out of range, inverted)
    are skipped defensively — a bad agent output must not fail the plan.
    """
    removals: list[Removal] = []
    count = len(words)
    for span in retake_spans or []:
        try:
            i, j = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        if i < 0 or j >= count or i > j:
            continue
        if i == 0:
            lo = 0.0
        else:
            lo = min(words[i].start, words[i - 1].end + PAD_S)
        if j == count - 1:
            hi = duration_s
        else:
            hi = max(words[j].end, words[j + 1].start - PAD_S)
        if hi - lo > _EPS:
            removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_RETAKE))
    return removals


# ---------------------------------------------------------------------------------
# Hygiene + assembly
# ---------------------------------------------------------------------------------


def _merge_removals(raw: list[Removal], duration_s: float) -> list[Removal]:
    """Clamp to the clip, drop sub-MIN_CUT_S slivers, merge overlaps/adjacency.

    Merged removals keep the reason of the FIRST component by time (simple +
    documented — the admin viewer colors by dominant onset, not by mix).
    """
    clamped: list[Removal] = []
    for removal in raw:
        lo = max(0.0, removal.start_s)
        hi = min(duration_s, removal.end_s)
        if hi - lo >= MIN_CUT_S - _EPS:
            clamped.append(Removal(start_s=lo, end_s=hi, reason=removal.reason))
    clamped.sort(key=lambda r: (r.start_s, r.end_s, r.reason))
    merged: list[Removal] = []
    for removal in clamped:
        if merged and removal.start_s <= merged[-1].end_s + _EPS:
            previous = merged[-1]
            merged[-1] = Removal(
                start_s=previous.start_s,
                end_s=max(previous.end_s, removal.end_s),
                reason=previous.reason,
            )
        else:
            merged.append(removal)
    return merged


def _absorb_micro_fragments(
    removals: list[Removal],
    words: list[_CutWord],
    duration_s: float,
) -> list[Removal]:
    """Glitch hygiene: absorb word-free keep fragments < MIN_KEEP_SEGMENT_S.

    A keep fragment shorter than MIN_KEEP_SEGMENT_S sandwiched between two
    removals (or between a removal and a clip edge) that carries no kept word
    is a few-frame flash of video between two jump cuts — found in local
    testing 2026-07-09 as a 110ms three-frame stutter between an "um" cut and
    a pause cut. Fragments carrying ANY kept word are never absorbed.
    """
    out = list(removals)
    changed = True
    while changed and out:
        changed = False
        for i in range(len(out) + 1):
            lo = out[i - 1].end_s if i > 0 else 0.0
            hi = out[i].start_s if i < len(out) else duration_s
            frag = hi - lo
            if frag <= _EPS or frag >= MIN_KEEP_SEGMENT_S - _EPS:
                continue
            if any(w.end > lo + _EPS and w.start < hi - _EPS for w in words):
                continue  # carries a kept word — never absorb
            if 0 < i < len(out):  # between two removals: merge through
                left, right = out[i - 1], out[i]
                out[i - 1 : i + 1] = [
                    Removal(start_s=left.start_s, end_s=right.end_s, reason=left.reason)
                ]
            elif i == 0:  # leading sliver before the first removal
                out[0] = Removal(start_s=0.0, end_s=out[0].end_s, reason=out[0].reason)
            else:  # trailing sliver after the last removal
                out[-1] = Removal(start_s=out[-1].start_s, end_s=duration_s, reason=out[-1].reason)
            changed = True
            break
    return out


def _complement(removals: list[Removal], duration_s: float) -> list[tuple[float, float]]:
    """Keep segments: everything in ``[0, duration_s]`` not removed."""
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for removal in removals:
        if removal.start_s - cursor > _EPS:
            segments.append((cursor, removal.start_s))
        cursor = max(cursor, removal.end_s)
    if duration_s - cursor > _EPS:
        segments.append((cursor, duration_s))
    return segments


def build_cut_plan(
    words: Sequence[Any] | None,
    silences: Sequence[tuple[float, float]] | None,
    duration_s: float,
    *,
    retake_spans: Sequence[tuple[int, int]] | None = None,
) -> CutPlan:
    """Detect silence/filler/retake cuts and return the plan.

    ``words`` accepts dicts or objects carrying ``text``/``start_s``/``end_s``
    plus optional ``segment_avg_logprob``/``segment_no_speech_prob``/
    ``confidence``. ``silences`` are silencedetect ranges from the caller
    (this module never runs ffmpeg). ``retake_spans`` are inclusive
    word-index ranges from the retake_detector agent.

    Safety rails each return a no-op plan with a distinct
    ``bailout_reason``; the caller reads the plan and emits pipeline events.
    """
    duration = float(duration_s)
    cut_words = _normalize_words(words)
    if not cut_words:
        return no_op_plan(duration, bailout_reason=BAILOUT_NO_WORDS)
    if duration < MIN_CLIP_S:
        return no_op_plan(duration, bailout_reason=BAILOUT_CLIP_TOO_SHORT)

    silence_spans = _normalize_silences(silences, duration)

    raw: list[Removal] = []
    raw.extend(_lexical_removals(cut_words, duration))
    raw.extend(_acoustic_removals(cut_words, silence_spans, duration))
    raw.extend(_pause_removals(cut_words, silence_spans, duration))
    raw.extend(_retake_removals(cut_words, retake_spans, duration))

    removals = _merge_removals(raw, duration)
    removals = _absorb_micro_fragments(removals, cut_words, duration)
    total_removed = sum(r.end_s - r.start_s for r in removals)

    if total_removed > MAX_REMOVAL_FRAC * duration:
        return no_op_plan(duration, bailout_reason=BAILOUT_MAX_REMOVAL)
    # Defense in depth: unreachable with the shipped constants (a clip passing
    # both rails above retains ≥ (1−MAX_REMOVAL_FRAC)·MIN_CLIP_S = MIN_OUTPUT_S)
    # but pinned so a future constant change cannot ship a 2-second video.
    if duration - total_removed < MIN_OUTPUT_S:
        return no_op_plan(duration, bailout_reason=BAILOUT_OUTPUT_TOO_SHORT)

    return CutPlan(
        keep_segments=_complement(removals, duration),
        removed=removals,
        time_saved_s=total_removed,
    )


# ---------------------------------------------------------------------------------
# Remap — original-timeline words → cut-timeline words
# ---------------------------------------------------------------------------------


def _removed_before(t: float, removals: list[Removal]) -> float:
    """Total removed time strictly before ``t`` (clamped for robustness)."""
    return sum(max(0.0, min(r.end_s, t) - r.start_s) for r in removals)


def remap_words(words: Sequence[Any] | None, plan: CutPlan) -> list[dict]:
    """Shift surviving words into cut-timeline coordinates.

    Words fully inside a removal are dropped; survivors shift left by the
    cumulative removed time before them. Removals never intrude into kept
    words' interiors by construction, so the remap is exact arithmetic —
    kept spans keep their exact durations. Returns plain dicts
    (``text``/``start_s``/``end_s``) ready for caption-cue building.
    """
    removals = sorted(plan.removed, key=lambda r: (r.start_s, r.end_s))
    remapped: list[dict] = []
    for word in _normalize_words(words):
        if any(word.start >= r.start_s - _EPS and word.end <= r.end_s + _EPS for r in removals):
            continue
        new_start = word.start - _removed_before(word.start, removals)
        new_end = word.end - _removed_before(word.end, removals)
        remapped.append({"text": word.text, "start_s": new_start, "end_s": new_end})
    for entry in remapped:
        assert entry["end_s"] >= entry["start_s"] - _EPS, "remap inverted a word span"
    for prev, nxt in pairwise(remapped):
        assert nxt["start_s"] >= prev["start_s"] - _EPS, "remap broke start monotonicity"
    return remapped
