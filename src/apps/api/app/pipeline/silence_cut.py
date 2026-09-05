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
                      2. acoustic fillers   (soundful short gaps; V2 also
                                             scans silence-bounded islands)
                      3. pause tightening   (silence-intersected)
                      4. retake spans       (outward-snapped, never mid-word)
                      5. hygiene: MIN_CUT_S drop → merge →
                         micro-fragment absorb → safety rails
                         (over_budget_policy: bailout | consent clamp)
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

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise, product
from typing import Any, Literal, NamedTuple

# Whisper bias prompt for the CUT path: passed as whisper-1's ``prompt`` /
# faster-whisper's ``initial_prompt`` (transcribe(..., verbatim_prompt=…)) so the
# ASR keeps filler vocalizations as tokens instead of politely dropping them —
# rule 1 needs the tokens to cut them, and caption hygiene needs them to strip
# them from cue input. Keep this deliberately LEXICALLY NEUTRAL: putting Turkish
# restart phrases in an auto-language Whisper prompt caused English production-
# image renders to be classified as Turkish, after which caption correction
# rewrote the English speech as Turkish. Non-lexical EN/TR vocalizations retain
# the fillers without steering language detection; the retake agent sees the
# actual transcript and does not need a scripted restart phrase. Both
# integrations (subtitled + talking_head) import THIS constant — a caller
# inlining its own copy would silently diverge the two paths' transcripts.
SILENCE_CUT_VERBATIM_PROMPT = "Uh, um, erm, hmm... Iıı, eee, aaa."

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
# Explicit-consent removal budget (over_budget_policy="clamp"): a creator who
# turned Speech cleanup ON asked for the dead air to go, so instead of bailing
# out the plan is clamped to this fraction (largest removals first). Chosen so
# a filler-heavy clip still keeps ~45% of its runtime; MIN_OUTPUT_S remains the
# hard floor via the budget formula min(frac·dur, dur − MIN_OUTPUT_S) − slack.
MAX_REMOVAL_FRAC_REQUIRED = 0.55
# Float-safety margin subtracted from the clamp budget so trimmed spans can
# never round past the MIN_OUTPUT_S rail (1 ulp of trim arithmetic did exactly
# that on 5.0–6.67s clips). Millisecond-scale: imperceptible on any timeline.
CLAMP_BUDGET_SLACK_S = 0.001
MIN_OUTPUT_S = 3.0  # cut output shorter than this bails out
MIN_CLIP_S = 5.0  # clips shorter than this are never cut
LEAD_KEEP_S = 0.3  # leading silence is trimmed down to this much, not to zero
TRAIL_KEEP_S = 0.5  # trailing silence is trimmed down to this much
ACOUSTIC_GAP_MIN_S = 0.15  # soundful gap must be at least this long to be a filler
ACOUSTIC_GAP_MAX_S = 1.2  # soundful gaps longer than this are left alone (laughter…)
AVG_LOGPROB_MIN = -1.0  # segment avg_logprob below this blocks lexical cuts
NO_SPEECH_PROB_MAX = 0.5  # segment no_speech_prob above this blocks lexical cuts
MIN_KEEP_SEGMENT_S = 0.25  # word-free keep fragments shorter than this are absorbed
MAX_REMOVALS = 100  # ffmpeg -filter_complex arg-length defense; pathological stutter clips
KEEP_SEGMENTS_PUNCH_IN = 1.08  # alternating punch-in factor — user-validated 2026-07-09;
# integrations pass this to reframe_and_export(keep_segments_punch_in=…) so every
# render path produces the approved jump-cut style from one constant.

_EPS = 1e-9  # float-comparison tolerance for interval arithmetic
_MAX_DIAGNOSTIC_LEXICAL = 32
_MAX_DIAGNOSTIC_ACOUSTIC = 64
_MAX_DIAGNOSTIC_DECISIONS = 32
_MAX_DIAGNOSTIC_DISPOSITIONS = 64
_MAX_DIAGNOSTIC_REMOVALS = MAX_REMOVALS


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

    Normalization: fold Turkish capitals, lowercase, strip everything
    non-alphabetic (punctuation, digits, whitespace), collapse character runs
    to ``_MAX_LEXICON_RUN``, then membership-check against the precomputed
    elongation images. "Uh," / "uhhh" / "ıııı" / "Iıı," match; "ee" / "aa" /
    real words never do. Public so caption hygiene (plans/010 15A) can strip
    filler tokens from cue input even when they were not cut.
    """
    # Turkish casing: str.lower() maps 'I'→'i' and NEVER 'ı', so "Iıı," would
    # normalize to "iıı" and miss the "ııı" lexicon entry. Fold the Turkish
    # capitals explicitly BEFORE lowercasing: 'İ' (dotted) → 'i', 'I'
    # (dotless in Turkish) → 'ı'. Safe for English input because the lexicon
    # has no i-initial entries, so English capital-I words ("I", "It", "Im")
    # can never collapse onto a filler image.
    folded = str(text).replace("İ", "i").replace("I", "ı")
    normalized = "".join(ch for ch in folded.lower() if ch.isalpha())
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


@dataclass(frozen=True)
class AcousticDecision:
    """One V2 soundful-island classification; contains timing only."""

    window_start_s: float
    window_end_s: float
    island_start_s: float
    island_end_s: float
    left_silence_s: float
    right_silence_s: float
    detection: Literal["eligible", "rejected"]
    reason: str


@dataclass(frozen=True)
class AtomicDisposition:
    """Authoritative terminal allocation state for one input atom."""

    atom_start_s: float
    atom_end_s: float
    group_start_s: float
    group_end_s: float
    atom_kind: Literal["filler_lexical", "filler_acoustic", "retake"]
    priority: Literal["protected", "filler", "retake"]
    disposition: Literal[
        "selected_full",
        "promoted_protected",
        "dropped_budget",
        "dropped_max_removals",
        "dropped_min_cut",
        "dropped_micro_gap",
        "dropped_safety_bailout",
    ]


@dataclass(frozen=True)
class CutDiagnostics:
    """Bounded in-memory V2 evidence; never serialized by plan helpers."""

    lexical_candidates: tuple[Removal, ...]
    lexical_candidates_total: int
    lexical_candidates_omitted: int
    acoustic_candidates: tuple[Removal, ...]
    acoustic_decisions: tuple[AcousticDecision, ...]
    acoustic_decisions_total: int
    acoustic_decisions_omitted: int
    acoustic_eligible_total: int
    mixed_gap_decision_dispositions: tuple[tuple[float, float, str], ...]
    atomic_dispositions: tuple[AtomicDisposition, ...]
    atomic_dispositions_total: int
    atomic_dispositions_omitted: int
    proposed_removals: tuple[Removal, ...]
    mixed_gap_full_total: int
    mixed_gap_partial_total: int
    mixed_gap_dropped_total: int


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
    # over_budget_policy="clamp" only: True when the proposed removal set was
    # reduced to fit the explicit-consent budget; the pre-clamp total and the
    # budget are kept for the pipeline trace. Always False/None for the default
    # bailout policy so legacy plans stay byte-identical.
    clamped: bool = False
    proposed_removed_s: float | None = None
    clamp_budget_s: float | None = None
    diagnostics: CutDiagnostics | None = None


@dataclass(frozen=True)
class CutPlanComparison:
    """Byte-compatible baseline plus an isolated V2 candidate attempt."""

    baseline: CutPlan
    candidate: CutPlan | None
    candidate_status: Literal["ready", "build_failed", "validation_failed"]
    candidate_error_class: str | None = None

    @property
    def baseline_plan(self) -> CutPlan:
        """Compatibility alias for task code that names the value explicitly."""
        return self.baseline

    @property
    def candidate_plan(self) -> CutPlan | None:
        """Compatibility alias for task code that names the value explicitly."""
        return self.candidate


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
    """First non-None value among ``names``, via dict key OR attribute access.

    Sibling copy: ``app/pipeline/phrase_sequence.py`` ``_field`` — same helper,
    but the two callers pass OPPOSITE key preferences: ``_normalize_words``
    here prefers ``start_s`` over ``start``; phrase_sequence prefers ``start``
    over ``start_s``. That divergence is deliberate (each matches its own
    persisted-record shape) but fragile — a record carrying BOTH keys with
    different values normalizes differently in the two modules.
    """
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
    Key preference (``start_s`` first) deliberately diverges from the
    phrase_sequence.py sibling — see the ``_field`` docstring above.
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
        try:
            start_f = float(start)
            raw_end_f = float(end)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start_f) or not math.isfinite(raw_end_f):
            continue
        end_f = max(raw_end_f, start_f)
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
        try:
            raw_lo = float(raw[0])
            raw_hi = float(raw[1])
        except (IndexError, TypeError, ValueError):
            continue
        if not math.isfinite(raw_lo) or not math.isfinite(raw_hi):
            continue
        lo = max(0.0, raw_lo)
        hi = min(duration_s, raw_hi)
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


def _subtract_intervals(
    lo: float,
    hi: float,
    spans: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Maximal pieces of ``[lo, hi]`` not covered by ``spans``."""
    if hi - lo <= _EPS:
        return []
    out: list[tuple[float, float]] = []
    cursor = lo
    for span_lo, span_hi in spans:
        if span_hi <= cursor + _EPS:
            continue
        if span_lo >= hi - _EPS:
            break
        clipped_lo = max(lo, span_lo)
        clipped_hi = min(hi, span_hi)
        if clipped_lo - cursor > _EPS:
            out.append((cursor, clipped_lo))
        cursor = max(cursor, clipped_hi)
    if hi - cursor > _EPS:
        out.append((cursor, hi))
    return out


def _interior_soundful_islands(
    window_lo: float,
    window_hi: float,
    silence_spans: list[tuple[float, float]],
) -> list[AcousticDecision]:
    """Classify maximal non-silent complements inside one ASR word gap.

    Only a complement strictly inside the window can be eligible: that proves
    it has a contiguous FFmpeg-detected silence flank on both sides and cannot
    touch an ASR word or clip boundary.  Inputs are expected to be normalized,
    but clipping here keeps the helper deterministic for direct unit tests.
    """
    if window_hi - window_lo <= _EPS:
        return []
    normalized_silences = _normalize_silences(silence_spans, window_hi)
    intersections = _intersect_span(window_lo, window_hi, normalized_silences)
    if not intersections:
        return []

    decisions: list[AcousticDecision] = []
    for island_lo, island_hi in _subtract_intervals(window_lo, window_hi, intersections):
        left_flank = 0.0
        right_flank = 0.0
        for silence_lo, silence_hi in intersections:
            if abs(silence_hi - island_lo) <= _EPS:
                left_flank = silence_hi - silence_lo
            if abs(silence_lo - island_hi) <= _EPS:
                right_flank = silence_hi - silence_lo

        duration = island_hi - island_lo
        if island_lo <= window_lo + _EPS or island_hi >= window_hi - _EPS:
            detection: Literal["eligible", "rejected"] = "rejected"
            reason = "touches_window_boundary"
        elif left_flank < 0.1 - _EPS:
            detection = "rejected"
            reason = "left_silence_too_short"
        elif right_flank < 0.1 - _EPS:
            detection = "rejected"
            reason = "right_silence_too_short"
        elif duration < ACOUSTIC_GAP_MIN_S - _EPS:
            detection = "rejected"
            reason = "island_too_short"
        elif duration > ACOUSTIC_GAP_MAX_S + _EPS:
            detection = "rejected"
            reason = "island_too_long"
        else:
            detection = "eligible"
            reason = "bilateral_silence"
        decisions.append(
            AcousticDecision(
                window_start_s=window_lo,
                window_end_s=window_hi,
                island_start_s=island_lo,
                island_end_s=island_hi,
                left_silence_s=left_flank,
                right_silence_s=right_flank,
                detection=detection,
                reason=reason,
            )
        )
    return decisions


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


def _acoustic_removals_v2(
    words: list[_CutWord],
    silence_spans: list[tuple[float, float]],
    duration_s: float,
) -> tuple[list[Removal], list[AcousticDecision]]:
    """V2 acoustic candidates from whole gaps and mixed-gap complements.

    Whole, silence-free inter-word gaps intentionally retain the V1 rule and
    its acoustic padding.  Windows containing any silence are decomposed and
    only exact bilateral-silence islands are proposed; edge windows are scan-
    only when wholly soundful. An edge-window island can still be a candidate
    when bilateral silence makes it strictly internal to that window.
    """
    if not silence_spans:
        return [], []

    removals: list[Removal] = []
    decisions: list[AcousticDecision] = []
    windows: list[tuple[float, float, bool]] = [(0.0, words[0].start, False)]
    windows.extend((prev.end, nxt.start, True) for prev, nxt in pairwise(words))
    windows.append((words[-1].end, duration_s, False))

    for window_lo, window_hi, allow_legacy_gap in windows:
        if window_hi - window_lo <= _EPS:
            continue
        if not _overlaps_any(window_lo, window_hi, silence_spans):
            if not allow_legacy_gap:
                continue
            gap = window_hi - window_lo
            if gap < ACOUSTIC_GAP_MIN_S - _EPS or gap > ACOUSTIC_GAP_MAX_S + _EPS:
                continue
            lo = window_lo + PAD_ACOUSTIC_S
            hi = window_hi - PAD_ACOUSTIC_S
            if hi - lo > _EPS:
                removals.append(Removal(start_s=lo, end_s=hi, reason=REASON_FILLER_ACOUSTIC))
            continue

        window_decisions = _interior_soundful_islands(
            window_lo,
            window_hi,
            silence_spans,
        )
        decisions.extend(window_decisions)
        for decision in window_decisions:
            if decision.detection != "eligible":
                continue
            removals.append(
                Removal(
                    start_s=decision.island_start_s,
                    end_s=decision.island_end_s,
                    reason=REASON_FILLER_ACOUSTIC,
                )
            )
    removals.sort(key=lambda removal: (removal.start_s, removal.end_s))
    decisions.sort(key=lambda decision: (decision.island_start_s, decision.island_end_s))
    return removals, decisions


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


def _snap_boundary_out_of_words(
    t: float, words: list[_CutWord], *, direction: Literal["right", "left"]
) -> float:
    """Move a trimmed-removal boundary clear of words, PAD_S included.

    A trim boundary landing mid-word would resurrect a PARTIAL word — a
    mid-vocalization jump cut — violating remap_words' precondition that
    removals never intrude into kept words; a boundary landing within PAD_S
    of a word it resurrects would keep the word with sub-pad clearance to
    the cut (adversarial finding, 2026-08-31 — a surviving "um" 40ms before
    a jump cut). The guard zone is therefore the word plus its PAD_S flank
    on the cut side. Snapping always SHRINKS the removal (start boundaries
    move right past the word + PAD_S, end boundaries move left before it −
    PAD_S), so the word is kept whole, padded, and the budget can only be
    under-spent. Fixpoint loop: a snapped boundary can land in a neighboring
    word's zone; each snap moves strictly in one direction, so it terminates.
    """
    changed = True
    while changed:
        changed = False
        for word in words:
            in_zone = (
                word.start + _EPS < t < word.end + PAD_S - _EPS
                if direction == "right"
                else word.start - PAD_S + _EPS < t < word.end - _EPS
            )
            if in_zone:
                t = (word.end + PAD_S) if direction == "right" else (word.start - PAD_S)
                changed = True
    return t


def _clamp_removals_to_budget(
    removals: list[Removal],
    budget_s: float,
    duration_s: float,
    *,
    words: list[_CutWord],
    protected_spans: Sequence[tuple[float, float]] = (),
) -> list[Removal]:
    """Reduce a removal set to fit ``budget_s``, edge cuts first.

    Deterministic greedy: EDGE cuts (leading/trailing dead air) are charged
    before interior cuts — an edge trim is perceptually free (no jump cut)
    and a dropped LEAD cut ships a dead-air opening that violates the 2-3s
    hook window (adversarial finding, 2026-08-31) — then interior cuts by
    descending duration with a start_s tiebreak (the MAX_REMOVALS cap's
    ordering). Whole removals are kept while they fit; removals that do not
    fit are TRIMMED into the remaining budget instead of dropped — otherwise
    a clip whose dead air is one contiguous block (a long trailing silence)
    would clamp to a no-op. Trims anchor to the clip edge for edge cuts (a
    shrunk lead cut must still start at 0, a shrunk trail cut must still
    reach the clip end) and shrink symmetrically for interior removals so
    the residual gap stays balanced; every trimmed boundary is then snapped
    clear of words (see `_snap_boundary_out_of_words`). A snap can shrink
    (or drop) the trim, so the loop keeps charging the TRUE kept span and
    lets later removals use the leftover budget. Remainders below MIN_CUT_S
    are not worth a jump cut and are skipped. Dropping/shrinking removals
    only ever ENLARGES the adjacent keep fragments, so
    `_absorb_micro_fragments` invariants survive unchanged.

    Edge classification is by KEPT WORDS, not raw clip coordinates: a
    removal with no kept word before it trims as a lead cut and one with no
    kept word after it as a trail cut, even when silencedetect closed the
    range a little inside the container (audio stream shorter than video).
    A removal that is both (no words at all) trims symmetrically.

    ``protected_spans`` (the caller's forced/manual-review intervals):
    `_validate_speech_cut_publication` requires every forced interval to stay
    covered by the rendered plan, so the clamp never drops or trims through
    one. When the overlapping removals fit the budget WHOLE they are kept
    whole (charged first). When they do not — `_merge_removals` can fuse a
    tiny forced cut into a huge detected silence block, and protecting the
    whole merged carrier would blow the budget or trip the MIN_OUTPUT_S rail
    (red-team finding, 2026-08-31) — each protected removal is shrunk to its
    per-forced-span INTERSECTIONS, never their hull: two far-apart forced
    cuts in one carrier must not drag the dead gap between them along
    (adversarial finding, 2026-08-31). A forced set that alone exceeds the
    budget is still kept in full: explicit manual cuts outrank the consent
    budget, and the caller's MIN_OUTPUT_S rail backstops a pathological set.
    NOTE: no live dispatch reaches the protected branch today — required_v1
    analyses run with include_retakes=False, so review candidates (the only
    forced_removals writer) never exist on clamp-policy jobs; the protection
    is armed for when candidates ship on required jobs.
    """

    def _protected_intersections(removal: Removal) -> list[Removal]:
        return [
            Removal(
                start_s=max(removal.start_s, lo),
                end_s=min(removal.end_s, hi),
                reason=removal.reason,
            )
            for lo, hi in sorted(protected_spans)
            if min(removal.end_s, hi) - max(removal.start_s, lo) > _EPS
        ]

    protected: list[Removal] = []
    unprotected: list[Removal] = []
    intersections: list[Removal] = []
    for removal in removals:
        overlaps = _protected_intersections(removal)
        if not overlaps:
            unprotected.append(removal)
        else:
            protected.append(removal)
            intersections.extend(overlaps)
    if sum(r.end_s - r.start_s for r in protected) <= budget_s + _EPS:
        kept: list[Removal] = list(protected)
    else:
        kept = intersections

    def _edge_kind(removal: Removal) -> str:
        touches_start = removal.start_s <= _EPS
        touches_end = removal.end_s >= duration_s - _EPS
        if touches_start and touches_end:
            return "interior"  # whole-clip removal: balanced trim
        if touches_start:
            return "lead"
        if touches_end:
            return "trail"
        # Perceptual fallback (adversarial finding, 2026-08-31): silencedetect
        # can close a trailing range a little inside the container (audio
        # stream shorter than video) — classify by kept words instead of raw
        # coordinates so such a block still trims edge-anchored.
        no_word_before = not any(word.end <= removal.start_s + _EPS for word in words)
        no_word_after = not any(word.start >= removal.end_s - _EPS for word in words)
        if no_word_before and no_word_after:
            return "interior"  # ambiguous (no words at all): balanced trim
        if no_word_before:
            return "lead"
        if no_word_after:
            return "trail"
        return "interior"

    kinds = {id(r): _edge_kind(r) for r in unprotected}
    ordered = sorted(
        unprotected,
        key=lambda r: (
            0 if kinds[id(r)] in ("lead", "trail") else 1,
            -(r.end_s - r.start_s),
            r.start_s,
        ),
    )
    remaining = budget_s - sum(r.end_s - r.start_s for r in kept)
    for removal in ordered:
        if remaining <= _EPS:
            break
        span = removal.end_s - removal.start_s
        if span <= remaining + _EPS:
            kept.append(removal)
            remaining -= span
            continue
        if remaining < MIN_CUT_S - _EPS:
            continue
        kind = kinds[id(removal)]
        if kind == "lead":  # stay anchored at the clip start
            lo = removal.start_s
            hi = _snap_boundary_out_of_words(removal.start_s + remaining, words, direction="left")
        elif kind == "trail":  # stay anchored at the clip end
            lo = _snap_boundary_out_of_words(removal.end_s - remaining, words, direction="right")
            hi = removal.end_s
        else:  # interior: shrink symmetrically around the center
            center = (removal.start_s + removal.end_s) / 2.0
            lo = _snap_boundary_out_of_words(center - remaining / 2.0, words, direction="right")
            hi = _snap_boundary_out_of_words(center + remaining / 2.0, words, direction="left")
        if hi - lo >= MIN_CUT_S - _EPS:
            kept.append(Removal(start_s=lo, end_s=hi, reason=removal.reason))
            remaining -= hi - lo
    kept.sort(key=lambda r: (r.start_s, r.end_s))
    return kept


# ---------------------------------------------------------------------------------
# V2 provenance-aware allocation
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _AtomicInput:
    atom_id: int
    removal: Removal
    kind: Literal["filler_lexical", "filler_acoustic", "retake"]


@dataclass
class _AtomicGroup:
    group_id: int
    start_s: float
    end_s: float
    atoms: list[_AtomicInput]
    protected: bool
    priority: Literal["protected", "filler", "retake"]
    reason: str


@dataclass
class _AllocationItem:
    start_s: float
    end_s: float
    reason: str
    kind: Literal["protected", "filler", "retake", "flexible", "bridge"]
    group_id: int | None = None
    protected: bool = False


_Disposition = Literal[
    "selected_full",
    "promoted_protected",
    "dropped_budget",
    "dropped_max_removals",
    "dropped_min_cut",
    "dropped_micro_gap",
    "dropped_safety_bailout",
]


class _V2CandidateValidationError(Exception):
    """Boundary marker that keeps validation distinct from candidate build."""

    def __init__(self, original_error_class: str):
        super().__init__("V2 candidate validation failed")
        self.original_error_class = original_error_class


def _normalize_forced_removals(
    forced_removals: Sequence[Removal | dict[str, Any]] | None,
    duration_s: float,
) -> list[Removal]:
    normalized: list[Removal] = []
    for forced in forced_removals or []:
        try:
            if isinstance(forced, Removal):
                removal = forced
            else:
                removal = Removal(
                    start_s=float(forced["start_s"]),
                    end_s=float(forced["end_s"]),
                    reason=str(forced.get("reason") or "manual_review"),
                )
            raw_lo = float(removal.start_s)
            raw_hi = float(removal.end_s)
            if not math.isfinite(raw_lo) or not math.isfinite(raw_hi):
                continue
            lo = max(0.0, raw_lo)
            hi = min(duration_s, raw_hi)
            if math.isfinite(lo) and math.isfinite(hi) and hi - lo > _EPS:
                normalized.append(Removal(start_s=lo, end_s=hi, reason=removal.reason))
        except (KeyError, TypeError, ValueError):
            # Corrupt optional JSON can never make a render fail.
            continue
    normalized.sort(key=lambda removal: (removal.start_s, removal.end_s, removal.reason))
    return normalized


def _positive_overlap(
    first_lo: float,
    first_hi: float,
    second_lo: float,
    second_hi: float,
) -> bool:
    return min(first_hi, second_hi) - max(first_lo, second_lo) > _EPS


def _build_atomic_groups(
    atoms: list[_AtomicInput],
    forced: list[Removal],
) -> list[_AtomicGroup]:
    """Connected components over positive-overlap atomic/protected spans."""
    nodes: list[tuple[float, float, _AtomicInput | None, Removal | None]] = [
        (atom.removal.start_s, atom.removal.end_s, atom, None) for atom in atoms
    ]
    nodes.extend((item.start_s, item.end_s, None, item) for item in forced)
    if not nodes:
        return []

    parent = list(range(len(nodes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    order = sorted(range(len(nodes)), key=lambda index: (nodes[index][0], nodes[index][1]))
    component_root = order[0]
    component_max_end = nodes[component_root][1]
    for index in order[1:]:
        lo, hi, _atom, _forced = nodes[index]
        if lo < component_max_end - _EPS:
            # Interval-graph components on a line need only join the current
            # sweep component; the member contributing max_end proves an edge
            # (possibly through an already-connected overlap chain).
            union(component_root, index)
            component_max_end = max(component_max_end, hi)
        else:
            component_root = index
            component_max_end = hi

    components: dict[int, list[int]] = {}
    for index in range(len(nodes)):
        components.setdefault(find(index), []).append(index)

    groups: list[_AtomicGroup] = []
    for member_indices in components.values():
        member_nodes = [nodes[index] for index in member_indices]
        group_atoms = [node[2] for node in member_nodes if node[2] is not None]
        group_forced = [node[3] for node in member_nodes if node[3] is not None]
        has_filler = any(atom.kind != "retake" for atom in group_atoms)
        protected = bool(group_forced)
        priority: Literal["protected", "filler", "retake"]
        if protected:
            priority = "protected"
        elif has_filler:
            priority = "filler"
        else:
            priority = "retake"
        first_node = min(member_nodes, key=lambda node: (node[0], node[1]))
        if first_node[2] is not None:
            reason = first_node[2].removal.reason
        else:
            assert first_node[3] is not None
            reason = first_node[3].reason
        groups.append(
            _AtomicGroup(
                group_id=-1,
                start_s=min(node[0] for node in member_nodes),
                end_s=max(node[1] for node in member_nodes),
                atoms=list(group_atoms),
                protected=protected,
                priority=priority,
                reason=reason,
            )
        )
    groups.sort(key=lambda group: (group.start_s, group.end_s, group.priority))
    for group_id, group in enumerate(groups):
        group.group_id = group_id
    return groups


def _carve_group_geometry(
    flexible: Sequence[Removal],
    groups: Sequence[_AtomicGroup],
) -> list[Removal]:
    """Remove all group geometry from flexible carriers, even dropped groups."""
    group_spans = [(group.start_s, group.end_s) for group in groups]
    carved: list[Removal] = []
    for removal in flexible:
        for lo, hi in _subtract_intervals(removal.start_s, removal.end_s, group_spans):
            if hi - lo > _EPS:
                carved.append(Removal(start_s=lo, end_s=hi, reason=removal.reason))
    carved.sort(key=lambda removal: (removal.start_s, removal.end_s))
    return carved


def _merged_item_clusters(
    items: Sequence[_AllocationItem],
) -> list[tuple[float, float, list[_AllocationItem]]]:
    clusters: list[tuple[float, float, list[_AllocationItem]]] = []
    for item in sorted(items, key=lambda value: (value.start_s, value.end_s)):
        if not clusters or item.start_s > clusters[-1][1] + _EPS:
            clusters.append((item.start_s, item.end_s, [item]))
            continue
        lo, hi, members = clusters[-1]
        clusters[-1] = (lo, max(hi, item.end_s), [*members, item])
    return clusters


def _item_component_count(items: Sequence[_AllocationItem]) -> int:
    return len(_merged_item_clusters(items))


def _edge_kind_v2(
    removal: Removal,
    words: list[_CutWord],
    duration_s: float,
) -> Literal["lead", "trail", "interior"]:
    touches_start = removal.start_s <= _EPS
    touches_end = removal.end_s >= duration_s - _EPS
    if touches_start and not touches_end:
        return "lead"
    if touches_end and not touches_start:
        return "trail"
    no_word_before = not any(word.end <= removal.start_s + _EPS for word in words)
    no_word_after = not any(word.start >= removal.end_s - _EPS for word in words)
    if no_word_before and not no_word_after:
        return "lead"
    if no_word_after and not no_word_before:
        return "trail"
    return "interior"


def _fit_flexible_piece(
    removal: Removal,
    remaining_s: float,
    *,
    edge_kind: Literal["lead", "trail", "interior"],
    words: list[_CutWord],
) -> Removal | None:
    span = removal.end_s - removal.start_s
    if span <= remaining_s + _EPS:
        return removal
    if remaining_s <= _EPS:
        return None
    if edge_kind == "lead":
        lo = removal.start_s
        hi = _snap_boundary_out_of_words(lo + remaining_s, words, direction="left")
    elif edge_kind == "trail":
        hi = removal.end_s
        lo = _snap_boundary_out_of_words(hi - remaining_s, words, direction="right")
    else:
        center = (removal.start_s + removal.end_s) / 2.0
        lo = _snap_boundary_out_of_words(center - remaining_s / 2.0, words, direction="right")
        hi = _snap_boundary_out_of_words(center + remaining_s / 2.0, words, direction="left")
    if hi - lo <= _EPS:
        return None
    return Removal(start_s=lo, end_s=hi, reason=removal.reason)


def _items_duration(items: Sequence[_AllocationItem], *, budgeted_only: bool = False) -> float:
    selected = [item for item in items if not budgeted_only or not item.protected]
    return sum(hi - lo for lo, hi, _members in _merged_item_clusters(selected))


def _has_word_overlap(lo: float, hi: float, words: Sequence[_CutWord]) -> bool:
    return any(word.end > lo + _EPS and word.start < hi - _EPS for word in words)


def _eviction_key(item: _AllocationItem) -> tuple[int, float]:
    # Flexible carrier first, then retake, then filler; later loses a tie.
    rank = {"flexible": 0, "bridge": 0, "retake": 1, "filler": 2}.get(item.kind, 3)
    return rank, -item.start_s


def _apply_v2_micro_gap_hygiene(
    items: list[_AllocationItem],
    words: list[_CutWord],
    duration_s: float,
    budget_s: float,
    groups: Sequence[_AtomicGroup],
    dispositions: dict[int, _Disposition],
) -> tuple[list[_AllocationItem], bool]:
    """Absorb affordable word-free flashes or evict a whole budgeted item."""
    active = list(items)
    for _iteration in range((len(items) + 1) * 4 + 20):
        clusters = _merged_item_clusters(active)
        changed = False
        gaps: list[
            tuple[
                float,
                float,
                list[_AllocationItem],
                list[_AllocationItem],
            ]
        ] = []
        if clusters:
            gaps.append((0.0, clusters[0][0], [], clusters[0][2]))
            for left, right in pairwise(clusters):
                gaps.append((left[1], right[0], left[2], right[2]))
            gaps.append((clusters[-1][1], duration_s, clusters[-1][2], []))
        for gap_lo, gap_hi, left_members, right_members in gaps:
            gap = gap_hi - gap_lo
            if gap <= _EPS or gap >= MIN_KEEP_SEGMENT_S - _EPS:
                continue
            if _has_word_overlap(gap_lo, gap_hi, words):
                if (
                    left_members
                    and right_members
                    and all(item.protected for item in left_members)
                    and all(item.protected for item in right_members)
                ):
                    return active, False
                continue

            adjacent = [
                *(left_members[-1:] if left_members else []),
                *(right_members[:1] if right_members else []),
            ]
            protected_bridge = bool(adjacent) and all(item.protected for item in adjacent)
            bridge_cost = 0.0 if protected_bridge else gap
            dropped_group_overlap = any(
                dispositions.get(group.group_id, "selected_full").startswith("dropped_")
                and _positive_overlap(gap_lo, gap_hi, group.start_s, group.end_s)
                for group in groups
                if group.atoms
            )
            if not dropped_group_overlap and (
                protected_bridge
                or _items_duration(active, budgeted_only=True) + bridge_cost <= budget_s + _EPS
            ):
                active.append(
                    _AllocationItem(
                        start_s=gap_lo,
                        end_s=gap_hi,
                        reason=adjacent[0].reason if adjacent else REASON_SILENCE,
                        kind="bridge",
                        protected=protected_bridge,
                    )
                )
                changed = True
                break

            evictable = [item for item in adjacent if not item.protected]
            if not evictable:
                return active, False
            evicted = min(evictable, key=_eviction_key)
            active.remove(evicted)
            if evicted.group_id is not None:
                dispositions[evicted.group_id] = "dropped_micro_gap"
            changed = True
            break
        if not changed:
            return active, True
    return active, False


def _enforce_v2_component_cap(
    items: list[_AllocationItem],
    dispositions: dict[int, _Disposition],
) -> tuple[list[_AllocationItem], bool]:
    """Bound final FFmpeg components without ever slicing an atomic group.

    The cap is deliberately applied after micro-gap hygiene: provisional
    removals that coalesce through a word-free bridge are one FFmpeg component,
    not several.  When the final graph is still too large, discard whole
    unprotected components in allocator priority order.  A protected component
    is never partially retained or evicted.
    """

    active = list(items)
    while True:
        clusters = _merged_item_clusters(active)
        if len(clusters) <= MAX_REMOVALS:
            return active, True

        evictable = [
            cluster for cluster in clusters if not any(item.protected for item in cluster[2])
        ]
        if not evictable:
            return active, False

        def component_eviction_key(
            cluster: tuple[float, float, list[_AllocationItem]],
        ) -> tuple[int, float]:
            lo, _hi, members = cluster
            # A component inherits its most valuable member's priority. This
            # prevents a flexible carrier from making a filler component look
            # disposable while still preferring later components on ties.
            rank = max(_eviction_key(item)[0] for item in members)
            return rank, -lo

        _lo, _hi, members = min(evictable, key=component_eviction_key)
        for item in members:
            active.remove(item)
            if item.group_id is not None:
                dispositions[item.group_id] = "dropped_max_removals"


def _merge_allocated_items(items: Sequence[_AllocationItem]) -> list[Removal]:
    removals: list[Removal] = []
    for lo, hi, members in _merged_item_clusters(items):
        first = min(members, key=lambda item: (item.start_s, item.end_s))
        removals.append(Removal(start_s=lo, end_s=hi, reason=first.reason))
    return removals


def _bounded_diagnostics(
    *,
    lexical: list[Removal],
    acoustic: list[Removal],
    decisions: list[AcousticDecision],
    groups: list[_AtomicGroup],
    dispositions: dict[int, _Disposition],
    proposed: list[Removal],
) -> CutDiagnostics:
    atomic_records: list[AtomicDisposition] = []
    for group in groups:
        for atom in group.atoms:
            atomic_records.append(
                AtomicDisposition(
                    atom_start_s=atom.removal.start_s,
                    atom_end_s=atom.removal.end_s,
                    group_start_s=group.start_s,
                    group_end_s=group.end_s,
                    atom_kind=atom.kind,
                    priority=group.priority,
                    disposition=dispositions[group.group_id],
                )
            )
    atomic_records.sort(
        key=lambda record: (
            record.atom_start_s,
            record.atom_end_s,
            record.atom_kind,
        )
    )
    decision_total = len(decisions)
    disposition_total = len(atomic_records)
    eligible_mixed_atoms = {
        (decision.island_start_s, decision.island_end_s)
        for decision in decisions
        if decision.detection == "eligible"
    }
    mixed_records = [
        record
        for record in atomic_records
        if record.atom_kind == "filler_acoustic"
        and (record.atom_start_s, record.atom_end_s) in eligible_mixed_atoms
    ]
    decision_dispositions: list[tuple[float, float, str]] = []
    for decision in decisions[:_MAX_DIAGNOSTIC_DECISIONS]:
        disposition = next(
            (
                record.disposition
                for record in atomic_records
                if record.atom_kind == "filler_acoustic"
                and abs(record.atom_start_s - decision.island_start_s) <= _EPS
                and abs(record.atom_end_s - decision.island_end_s) <= _EPS
            ),
            "not_candidate",
        )
        decision_dispositions.append((decision.island_start_s, decision.island_end_s, disposition))
    return CutDiagnostics(
        lexical_candidates=tuple(lexical[:_MAX_DIAGNOSTIC_LEXICAL]),
        lexical_candidates_total=len(lexical),
        lexical_candidates_omitted=max(0, len(lexical) - _MAX_DIAGNOSTIC_LEXICAL),
        acoustic_candidates=tuple(acoustic[:_MAX_DIAGNOSTIC_ACOUSTIC]),
        acoustic_decisions=tuple(decisions[:_MAX_DIAGNOSTIC_DECISIONS]),
        acoustic_decisions_total=decision_total,
        acoustic_decisions_omitted=max(0, decision_total - _MAX_DIAGNOSTIC_DECISIONS),
        acoustic_eligible_total=sum(decision.detection == "eligible" for decision in decisions),
        mixed_gap_decision_dispositions=tuple(decision_dispositions),
        atomic_dispositions=tuple(atomic_records[:_MAX_DIAGNOSTIC_DISPOSITIONS]),
        atomic_dispositions_total=disposition_total,
        atomic_dispositions_omitted=max(0, disposition_total - _MAX_DIAGNOSTIC_DISPOSITIONS),
        proposed_removals=tuple(proposed[:_MAX_DIAGNOSTIC_REMOVALS]),
        mixed_gap_full_total=sum(
            record.disposition in {"selected_full", "promoted_protected"}
            for record in mixed_records
        ),
        mixed_gap_partial_total=0,
        mixed_gap_dropped_total=sum(
            record.disposition.startswith("dropped_") for record in mixed_records
        ),
    )


def _v2_no_op_plan(
    duration_s: float,
    bailout_reason: str,
    *,
    lexical: list[Removal],
    acoustic: list[Removal],
    decisions: list[AcousticDecision],
    groups: list[_AtomicGroup],
    dispositions: dict[int, _Disposition],
    proposed: list[Removal],
    clamped: bool = False,
    proposed_removed_s: float | None = None,
    clamp_budget_s: float | None = None,
) -> CutPlan:
    for group in groups:
        if group.atoms:
            dispositions[group.group_id] = "dropped_safety_bailout"
    diagnostics = _bounded_diagnostics(
        lexical=lexical,
        acoustic=acoustic,
        decisions=decisions,
        groups=groups,
        dispositions=dispositions,
        proposed=proposed,
    )
    return CutPlan(
        keep_segments=[(0.0, duration_s)],
        removed=[],
        time_saved_s=0.0,
        version=2,
        bailout_reason=bailout_reason,
        clamped=clamped,
        proposed_removed_s=proposed_removed_s,
        clamp_budget_s=clamp_budget_s,
        diagnostics=diagnostics,
    )


def _validate_v2_candidate(
    plan: CutPlan,
    *,
    duration_s: float,
    words: list[_CutWord],
    forced: list[Removal],
    budget_s: float | None,
    groups: Sequence[_AtomicGroup] | None = None,
    dispositions: dict[int, _Disposition] | None = None,
) -> None:
    """Reject a candidate that violates geometry, budget, or atom invariants."""
    if plan.version != 2 or plan.diagnostics is None:
        raise ValueError("not_v2")
    values = [duration_s, plan.time_saved_s]
    values.extend(value for segment in plan.keep_segments for value in segment)
    values.extend(value for removal in plan.removed for value in (removal.start_s, removal.end_s))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("nonfinite")
    if plan.bailout_reason is not None:
        if plan.removed or plan.keep_segments != [(0.0, duration_s)]:
            raise ValueError("invalid_bailout")
        return
    if len(plan.removed) > MAX_REMOVALS:
        raise ValueError("max_removals")
    if any(
        removal.start_s < -_EPS
        or removal.end_s > duration_s + _EPS
        or removal.end_s - removal.start_s < MIN_CUT_S - _EPS
        for removal in plan.removed
    ):
        raise ValueError("removal_bounds")
    if any(nxt.start_s <= previous.end_s + _EPS for previous, nxt in pairwise(plan.removed)):
        raise ValueError("removal_order")
    expected_keep = _complement(plan.removed, duration_s)
    if len(expected_keep) != len(plan.keep_segments) or any(
        abs(actual[0] - expected[0]) > _EPS or abs(actual[1] - expected[1]) > _EPS
        for actual, expected in zip(plan.keep_segments, expected_keep)
    ):
        raise ValueError("partition")
    total = sum(removal.end_s - removal.start_s for removal in plan.removed)
    if abs(total - plan.time_saved_s) > 1e-7:
        raise ValueError("saved_duration")
    if duration_s - total < MIN_OUTPUT_S - _EPS:
        raise ValueError("output_floor")

    diagnostics = plan.diagnostics
    group_states: list[tuple[float, float, _Disposition, tuple[str, ...]]] = []
    if groups is not None and dispositions is not None:
        group_states = [
            (
                group.start_s,
                group.end_s,
                dispositions[group.group_id],
                tuple(atom.kind for atom in group.atoms),
            )
            for group in groups
            if group.atoms
        ]
    else:
        bounded_records: dict[tuple[float, float], list[AtomicDisposition]] = {}
        for record in diagnostics.atomic_dispositions:
            bounded_records.setdefault((record.group_start_s, record.group_end_s), []).append(
                record
            )
        group_states = [
            (
                group_lo,
                group_hi,
                records[0].disposition,
                tuple(record.atom_kind for record in records),
            )
            for (group_lo, group_hi), records in bounded_records.items()
        ]
        if any(
            record.disposition != records[0].disposition
            for records in bounded_records.values()
            for record in records
        ):
            raise ValueError("group_disposition_mismatch")

    for group_lo, group_hi, disposition, _atom_kinds in group_states:
        overlap = sum(
            max(0.0, min(group_hi, removal.end_s) - max(group_lo, removal.start_s))
            for removal in plan.removed
        )
        selected = disposition in {"selected_full", "promoted_protected"}
        if selected and abs(overlap - (group_hi - group_lo)) > 1e-7:
            raise ValueError("partial_selected_atom")
        if not selected and overlap > _EPS:
            raise ValueError("overlapped_dropped_atom")

    protected_spans = [(item.start_s, item.end_s) for item in forced]
    if budget_s is not None:
        protected_covered = sum(
            hi - lo
            for lo, hi in _merge_plain_spans(
                [
                    (max(removal.start_s, protected_lo), min(removal.end_s, protected_hi))
                    for removal in plan.removed
                    for protected_lo, protected_hi in protected_spans
                    if _positive_overlap(
                        removal.start_s,
                        removal.end_s,
                        protected_lo,
                        protected_hi,
                    )
                ]
            )
        )
        if total - protected_covered > budget_s + 1e-7:
            raise ValueError("budget")

    allowed_word_spans = [
        (group_lo, group_hi)
        for group_lo, group_hi, disposition, atom_kinds in group_states
        if disposition in {"selected_full", "promoted_protected"}
        and any(kind in {"filler_lexical", "retake"} for kind in atom_kinds)
    ]
    allowed_word_spans.extend(protected_spans)
    for removal in plan.removed:
        for word in words:
            if not _positive_overlap(removal.start_s, removal.end_s, word.start, word.end):
                continue
            if not any(
                word.start >= allowed_lo - _EPS and word.end <= allowed_hi + _EPS
                for allowed_lo, allowed_hi in allowed_word_spans
            ):
                raise ValueError("word_intrusion")


def _merge_plain_spans(spans: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(spans):
        if hi - lo <= _EPS:
            continue
        if merged and lo <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _raw_v2_proposal(raw: Sequence[Removal], duration_s: float) -> list[Removal]:
    items = [
        _AllocationItem(
            start_s=max(0.0, removal.start_s),
            end_s=min(duration_s, removal.end_s),
            reason=removal.reason,
            kind="flexible",
        )
        for removal in raw
        if min(duration_s, removal.end_s) - max(0.0, removal.start_s) > _EPS
    ]
    return _merge_allocated_items(items)


def _build_v2_cut_plan_normalized(
    cut_words: list[_CutWord],
    silence_spans: list[tuple[float, float]],
    duration: float,
    *,
    retake_spans: Sequence[tuple[int, int]] | None,
    forced: list[Removal],
    include_silence_and_fillers: bool,
    over_budget_policy: Literal["bailout", "clamp"],
) -> CutPlan:
    """Build the V2 candidate without invoking normalization or external I/O."""
    if not cut_words:
        return _v2_no_op_plan(
            duration,
            BAILOUT_NO_WORDS,
            lexical=[],
            acoustic=[],
            decisions=[],
            groups=[],
            dispositions={},
            proposed=[],
        )
    if duration < MIN_CLIP_S:
        return _v2_no_op_plan(
            duration,
            BAILOUT_CLIP_TOO_SHORT,
            lexical=[],
            acoustic=[],
            decisions=[],
            groups=[],
            dispositions={},
            proposed=[],
        )

    lexical: list[Removal] = []
    acoustic: list[Removal] = []
    decisions: list[AcousticDecision] = []
    pauses: list[Removal] = []
    if include_silence_and_fillers:
        lexical = _lexical_removals(cut_words, duration)
        acoustic, decisions = _acoustic_removals_v2(cut_words, silence_spans, duration)
        pauses = _pause_removals(cut_words, silence_spans, duration)
    retakes = _retake_removals(cut_words, retake_spans, duration)

    atom_inputs: list[_AtomicInput] = []
    for removal in [*lexical, *acoustic, *retakes]:
        lo = max(0.0, removal.start_s)
        hi = min(duration, removal.end_s)
        if hi - lo <= _EPS:
            continue
        if removal.reason == REASON_FILLER_LEXICAL:
            kind: Literal["filler_lexical", "filler_acoustic", "retake"] = "filler_lexical"
        elif removal.reason == REASON_FILLER_ACOUSTIC:
            kind = "filler_acoustic"
        else:
            kind = "retake"
        atom_inputs.append(
            _AtomicInput(
                atom_id=len(atom_inputs),
                removal=Removal(start_s=lo, end_s=hi, reason=removal.reason),
                kind=kind,
            )
        )

    groups = _build_atomic_groups(atom_inputs, forced)
    flexible = _carve_group_geometry(pauses, groups)
    proposed = _raw_v2_proposal([*lexical, *acoustic, *pauses, *retakes, *forced], duration)
    proposed_total = sum(removal.end_s - removal.start_s for removal in proposed)
    dispositions: dict[int, _Disposition] = {
        group.group_id: "dropped_budget" for group in groups if group.atoms
    }

    clamp_budget_s: float | None = None
    if over_budget_policy == "clamp":
        clamp_budget_s = max(
            0.0,
            min(MAX_REMOVAL_FRAC_REQUIRED * duration, duration - MIN_OUTPUT_S)
            - CLAMP_BUDGET_SLACK_S,
        )
        budget_s = clamp_budget_s
        clamped = proposed_total > budget_s + _EPS
    else:
        budget_s = math.inf
        clamped = False

    active: list[_AllocationItem] = []
    protected_groups = [group for group in groups if group.protected]
    for group in protected_groups:
        active.append(
            _AllocationItem(
                start_s=group.start_s,
                end_s=group.end_s,
                reason=group.reason,
                kind="protected",
                group_id=group.group_id,
                protected=True,
            )
        )
        if group.atoms:
            dispositions[group.group_id] = "promoted_protected"

    def add_flexible(removal: Removal, edge_kind: Literal["lead", "trail", "interior"]) -> None:
        remaining = math.inf
        if math.isfinite(budget_s):
            remaining = max(0.0, budget_s - _items_duration(active, budgeted_only=True))
        fitted = _fit_flexible_piece(
            removal,
            remaining,
            edge_kind=edge_kind,
            words=cut_words,
        )
        if fitted is None:
            return
        item = _AllocationItem(
            start_s=fitted.start_s,
            end_s=fitted.end_s,
            reason=fitted.reason,
            kind="flexible",
        )
        active.append(item)

    edge_flexible: list[tuple[Removal, Literal["lead", "trail", "interior"]]] = []
    interior_flexible: list[Removal] = []
    for removal in flexible:
        edge_kind = _edge_kind_v2(removal, cut_words, duration)
        if edge_kind in {"lead", "trail"}:
            edge_flexible.append((removal, edge_kind))
        else:
            interior_flexible.append(removal)
    edge_flexible.sort(
        key=lambda pair: (
            -(pair[0].end_s - pair[0].start_s),
            pair[0].start_s,
        )
    )
    for removal, edge_kind in edge_flexible:
        add_flexible(removal, edge_kind)

    for priority in ("filler", "retake"):
        for group in sorted(
            (item for item in groups if not item.protected and item.priority == priority),
            key=lambda item: (item.start_s, item.end_s),
        ):
            group_duration = group.end_s - group.start_s
            remaining = math.inf
            if math.isfinite(budget_s):
                remaining = max(0.0, budget_s - _items_duration(active, budgeted_only=True))
            if group_duration > remaining + _EPS:
                dispositions[group.group_id] = "dropped_budget"
                continue
            item = _AllocationItem(
                start_s=group.start_s,
                end_s=group.end_s,
                reason=group.reason,
                kind=priority,
                group_id=group.group_id,
            )
            active.append(item)
            dispositions[group.group_id] = "selected_full"

    for removal in sorted(
        interior_flexible,
        key=lambda item: (-(item.end_s - item.start_s), item.start_s),
    ):
        add_flexible(removal, "interior")

    active, micro_ok = _apply_v2_micro_gap_hygiene(
        active,
        cut_words,
        duration,
        budget_s,
        groups,
        dispositions,
    )
    if not micro_ok:
        return _v2_no_op_plan(
            duration,
            BAILOUT_OUTPUT_TOO_SHORT,
            lexical=lexical,
            acoustic=acoustic,
            decisions=decisions,
            groups=groups,
            dispositions=dispositions,
            proposed=proposed,
            clamped=clamped,
            proposed_removed_s=proposed_total if clamped else None,
            clamp_budget_s=clamp_budget_s,
        )

    # MIN_CUT is evaluated only after groups and adjacent carriers are tagged.
    for lo, hi, members in list(_merged_item_clusters(active)):
        if hi - lo >= MIN_CUT_S - _EPS:
            continue
        if any(item.protected for item in members):
            return _v2_no_op_plan(
                duration,
                BAILOUT_OUTPUT_TOO_SHORT,
                lexical=lexical,
                acoustic=acoustic,
                decisions=decisions,
                groups=groups,
                dispositions=dispositions,
                proposed=proposed,
                clamped=clamped,
                proposed_removed_s=proposed_total if clamped else None,
                clamp_budget_s=clamp_budget_s,
            )
        for item in members:
            active.remove(item)
            if item.group_id is not None:
                dispositions[item.group_id] = "dropped_min_cut"

    active, component_cap_ok = _enforce_v2_component_cap(active, dispositions)
    if not component_cap_ok:
        return _v2_no_op_plan(
            duration,
            BAILOUT_MAX_REMOVAL,
            lexical=lexical,
            acoustic=acoustic,
            decisions=decisions,
            groups=groups,
            dispositions=dispositions,
            proposed=proposed,
            clamped=clamped,
            proposed_removed_s=proposed_total if clamped else None,
            clamp_budget_s=clamp_budget_s,
        )

    removals = _merge_allocated_items(active)
    total_removed = sum(removal.end_s - removal.start_s for removal in removals)
    if over_budget_policy == "bailout" and total_removed > MAX_REMOVAL_FRAC * duration + _EPS:
        return _v2_no_op_plan(
            duration,
            BAILOUT_MAX_REMOVAL,
            lexical=lexical,
            acoustic=acoustic,
            decisions=decisions,
            groups=groups,
            dispositions=dispositions,
            proposed=proposed,
        )
    if duration - total_removed < MIN_OUTPUT_S - _EPS:
        return _v2_no_op_plan(
            duration,
            BAILOUT_OUTPUT_TOO_SHORT,
            lexical=lexical,
            acoustic=acoustic,
            decisions=decisions,
            groups=groups,
            dispositions=dispositions,
            proposed=proposed,
            clamped=clamped,
            proposed_removed_s=proposed_total if clamped else None,
            clamp_budget_s=clamp_budget_s,
        )

    diagnostics = _bounded_diagnostics(
        lexical=lexical,
        acoustic=acoustic,
        decisions=decisions,
        groups=groups,
        dispositions=dispositions,
        proposed=proposed,
    )
    plan = CutPlan(
        keep_segments=_complement(removals, duration),
        removed=removals,
        time_saved_s=total_removed,
        version=2,
        clamped=clamped,
        proposed_removed_s=proposed_total if clamped else None,
        clamp_budget_s=clamp_budget_s,
        diagnostics=diagnostics,
    )
    try:
        _validate_v2_candidate(
            plan,
            duration_s=duration,
            words=cut_words,
            forced=[
                Removal(
                    start_s=item.start_s,
                    end_s=item.end_s,
                    reason=item.reason,
                )
                for item in active
                if item.protected
            ],
            budget_s=clamp_budget_s,
            groups=groups,
            dispositions=dispositions,
        )
    except Exception as exc:
        raise _V2CandidateValidationError(type(exc).__name__) from None
    return plan


def _build_v1_cut_plan_normalized(
    cut_words: list[_CutWord],
    silence_spans: list[tuple[float, float]],
    duration: float,
    *,
    retake_spans: Sequence[tuple[int, int]] | None = None,
    forced: list[Removal],
    include_silence_and_fillers: bool = True,
    over_budget_policy: Literal["bailout", "clamp"] = "bailout",
) -> CutPlan:
    """The pre-V2 algorithm over already-normalized immutable inputs."""
    if not cut_words:
        return no_op_plan(duration, bailout_reason=BAILOUT_NO_WORDS)
    if duration < MIN_CLIP_S:
        return no_op_plan(duration, bailout_reason=BAILOUT_CLIP_TOO_SHORT)

    raw: list[Removal] = []
    if include_silence_and_fillers:
        raw.extend(_lexical_removals(cut_words, duration))
        raw.extend(_acoustic_removals(cut_words, silence_spans, duration))
        raw.extend(_pause_removals(cut_words, silence_spans, duration))
    raw.extend(_retake_removals(cut_words, retake_spans, duration))
    forced_spans: list[tuple[float, float]] = []
    for forced_removal in forced:
        raw.append(forced_removal)
        forced_spans.append((forced_removal.start_s, forced_removal.end_s))

    removals = _merge_removals(raw, duration)
    removals = _absorb_micro_fragments(removals, cut_words, duration)
    if len(removals) > MAX_REMOVALS:
        # Cap the segment count (each removal splits the keep list, and every
        # keep segment becomes a trim/atrim pair in the caller's single
        # -filter_complex graph): keep the MAX_REMOVALS largest-duration
        # removals, deterministic tiebreak by start_s, then restore time
        # order. Dropping a removal only ever ENLARGES adjacent keep
        # fragments, so no new micro-fragments can appear and the safety
        # rails below still see the final removal set.
        removals = sorted(removals, key=lambda r: (-(r.end_s - r.start_s), r.start_s))
        removals = sorted(removals[:MAX_REMOVALS], key=lambda r: (r.start_s, r.end_s))
    total_removed = sum(r.end_s - r.start_s for r in removals)

    clamped = False
    proposed_removed_s: float | None = None
    clamp_budget_s: float | None = None
    if over_budget_policy == "clamp":
        # CLAMP_BUDGET_SLACK_S guarantees `duration − total ≥ MIN_OUTPUT_S`
        # survives float arithmetic: trimmed spans recompute with ~1 ulp of
        # rounding, which on the binding `duration − MIN_OUTPUT_S` leg
        # (5.0–6.67s clips) tripped the epsilon-free output rail below and
        # resurrected the strict unsafe_plan failure. 1 ms is imperceptible
        # and dwarfs any accumulated error (≤ ~1e-13 over MAX_REMOVALS spans).
        clamp_budget_s = max(
            0.0,
            min(MAX_REMOVAL_FRAC_REQUIRED * duration, duration - MIN_OUTPUT_S)
            - CLAMP_BUDGET_SLACK_S,
        )
        if total_removed > clamp_budget_s + _EPS:
            proposed_removed_s = total_removed
            removals = _clamp_removals_to_budget(
                removals,
                clamp_budget_s,
                duration,
                words=cut_words,
                protected_spans=forced_spans,
            )
            total_removed = sum(r.end_s - r.start_s for r in removals)
            clamped = True
    elif total_removed > MAX_REMOVAL_FRAC * duration:
        return no_op_plan(duration, bailout_reason=BAILOUT_MAX_REMOVAL)
    # Defense in depth: unreachable with the shipped constants for detected
    # cuts (a bailout-policy plan retains ≥ (1−MAX_REMOVAL_FRAC)·MIN_CLIP_S =
    # MIN_OUTPUT_S; a clamped plan retains ≥ MIN_OUTPUT_S + slack by the
    # budget formula). Under the clamp it remains REACHABLE by protected
    # forced/manual removals, which are exempt from the budget — a forced set
    # so large it leaves <3s of clip must still bail rather than ship it.
    if duration - total_removed < MIN_OUTPUT_S:
        return no_op_plan(duration, bailout_reason=BAILOUT_OUTPUT_TOO_SHORT)

    return CutPlan(
        keep_segments=_complement(removals, duration),
        removed=removals,
        time_saved_s=total_removed,
        clamped=clamped,
        proposed_removed_s=proposed_removed_s,
        clamp_budget_s=clamp_budget_s,
    )


def build_cut_plan(
    words: Sequence[Any] | None,
    silences: Sequence[tuple[float, float]] | None,
    duration_s: float,
    *,
    retake_spans: Sequence[tuple[int, int]] | None = None,
    forced_removals: Sequence[Removal | dict[str, Any]] | None = None,
    include_silence_and_fillers: bool = True,
    over_budget_policy: Literal["bailout", "clamp"] = "bailout",
    mixed_gap_enabled: bool = False,
) -> CutPlan:
    """Detect silence/filler/retake cuts and return one executable plan.

    The default remains the byte-compatible V1 algorithm.  Set
    ``mixed_gap_enabled=True`` only for an explicitly selected V2 candidate;
    it enables silence-complement islands and provenance-aware allocation.
    ``build_cut_plan_comparison`` is the preferred shadow/apply entry point
    because it preserves the valid baseline if candidate construction fails.
    """
    duration = float(duration_s)
    cut_words = _normalize_words(words)
    if not mixed_gap_enabled and (not cut_words or duration < MIN_CLIP_S):
        # Preserve V1's early-return ordering: invalid optional silence/forced
        # payloads were never inspected for a no-word or too-short clip.
        return _build_v1_cut_plan_normalized(
            cut_words,
            [],
            duration,
            retake_spans=retake_spans,
            forced=[],
            include_silence_and_fillers=include_silence_and_fillers,
            over_budget_policy=over_budget_policy,
        )
    silence_spans = _normalize_silences(silences, duration)
    forced = _normalize_forced_removals(forced_removals, duration)
    kwargs = {
        "retake_spans": retake_spans,
        "forced": forced,
        "include_silence_and_fillers": include_silence_and_fillers,
        "over_budget_policy": over_budget_policy,
    }
    if mixed_gap_enabled:
        return _build_v2_cut_plan_normalized(
            cut_words,
            silence_spans,
            duration,
            **kwargs,
        )
    return _build_v1_cut_plan_normalized(
        cut_words,
        silence_spans,
        duration,
        **kwargs,
    )


def build_cut_plan_comparison(
    words: Sequence[Any] | None,
    silences: Sequence[tuple[float, float]] | None,
    duration_s: float,
    *,
    retake_spans: Sequence[tuple[int, int]] | None = None,
    forced_removals: Sequence[Removal | dict[str, Any]] | None = None,
    include_silence_and_fillers: bool = True,
    over_budget_policy: Literal["bailout", "clamp"] = "bailout",
) -> CutPlanComparison:
    """Build V1 first, then isolate all V2-only failures from that baseline."""
    duration = float(duration_s)
    cut_words = _normalize_words(words)
    if not cut_words or duration < MIN_CLIP_S:
        silence_spans: list[tuple[float, float]] = []
        forced: list[Removal] = []
    else:
        silence_spans = _normalize_silences(silences, duration)
        forced = _normalize_forced_removals(forced_removals, duration)
    kwargs = {
        "retake_spans": retake_spans,
        "forced": forced,
        "include_silence_and_fillers": include_silence_and_fillers,
        "over_budget_policy": over_budget_policy,
    }
    baseline = _build_v1_cut_plan_normalized(
        cut_words,
        silence_spans,
        duration,
        **kwargs,
    )
    try:
        candidate = _build_v2_cut_plan_normalized(
            cut_words,
            silence_spans,
            duration,
            **kwargs,
        )
    except _V2CandidateValidationError as exc:
        return CutPlanComparison(
            baseline=baseline,
            candidate=None,
            candidate_status="validation_failed",
            candidate_error_class=exc.original_error_class,
        )
    except Exception as exc:  # Candidate failure must never poison the baseline.
        return CutPlanComparison(
            baseline=baseline,
            candidate=None,
            candidate_status="build_failed",
            candidate_error_class=type(exc).__name__,
        )
    return CutPlanComparison(
        baseline=baseline,
        candidate=candidate,
        candidate_status="ready",
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


# ---------------------------------------------------------------------------------
# Task-layer serialization — persisted summary + pipeline-event payload
# ---------------------------------------------------------------------------------


def clamp_metadata(plan: CutPlan) -> dict[str, Any]:
    """Additive clamp keys shared by every serializer and trace event.

    Empty for un-clamped plans so legacy shapes stay byte-identical; one key
    vocabulary (``clamped``/``proposed_removed_s``/``clamp_budget_s``) across
    the persisted summary, the plan event payload, and the task-layer
    ``silence_cut_clamped`` trace event.
    """
    if not plan.clamped:
        return {}
    return {
        "clamped": True,
        "proposed_removed_s": round(plan.proposed_removed_s or 0.0, 3),
        "clamp_budget_s": round(plan.clamp_budget_s or 0.0, 3),
    }


def plan_summary(plan: CutPlan, *, original_duration_s: float | None = None) -> dict[str, Any]:
    """Persisted ``variants[i]['silence_cut']`` shape — single source of truth
    (admin strip contract). Clamp keys are ADDITIVE and appear only on clamped
    plans so every pre-clamp summary stays byte-identical."""
    summary = {
        "removed": [
            {"start_s": round(r.start_s, 3), "end_s": round(r.end_s, 3), "reason": r.reason}
            for r in plan.removed
        ],
        "time_saved_s": round(plan.time_saved_s, 3),
        "version": plan.version,
        "original_duration_s": (
            round(original_duration_s, 3) if original_duration_s is not None else None
        ),
    }
    summary.update(clamp_metadata(plan))
    return summary


def plan_event_payload(
    plan: CutPlan,
    *,
    variant_id: str,
    retake_spans: int,
    applied: bool,
    cut_reused: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``record_pipeline_event`` payload shared by every silence-cut caller.

    Both task-layer integrations (subtitled + talking_head) emit through THIS
    helper so the admin job-debug view sees one payload shape; ``extra`` merges
    caller-specific keys last and may override the base fields.
    """
    reasons: dict[str, int] = {}
    for removal in plan.removed:
        reasons[removal.reason] = reasons.get(removal.reason, 0) + 1
    payload = {
        "variant_id": variant_id,
        "removed_count": len(plan.removed),
        "time_saved_s": round(plan.time_saved_s, 3),
        "reasons": reasons,
        "retake_spans": retake_spans,
        "applied": applied,
        "cut_reused": cut_reused,
        **clamp_metadata(plan),
    }
    payload.update(extra or {})
    return payload
