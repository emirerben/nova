"""KRI-1016: a long take that mixes English and Turkish is read per stretch, and the Turkish
whisper never wrote is listened to again.

Plaza de Mayo T2 ("maximo 2", 2026-10-28, 48 s): Maximo, a guide, explains in English and the
creator translates into Turkish in between. whisper-1 reads a whole file in ONE language; on this
take it chose Turkish, so every English line came back as a Turkish translation ("Peki ismin
neydi?"). KRI-843 rescues the English stretches the Gemini reference confirms, but Gemini only
listened to the first 30 s, so everything after it stayed a Turkish translation: "on the other
side of the square" was "Diğer tarafta", the photo cue was never heard, and the end line was never
found. Worse, whisper-1 wrote NO words at all for the creator's real Turkish (6.4-14 s and
26-31 s; its separate language=tr caption pass of the same clip is empty there too): those
stretches stayed wordless, so the captions had nothing to show and the cleanup read them as
pauses.

The fix has two parts, both for mixed takes only. (A) After the merge, a stretch of 1 s or more
with no words, where the reference heard at least two words nobody wrote, is cut out of the audio
and transcribed alone, once per language of the take; a stretch on its own has no other language
around it, so the Turkish comes out as Turkish, and only what the reference confirms is kept
(a translation is not confirmed). (B) When the reference is Gemini's listen to the first 30 s and
names two languages, Gemini listens once more to the rest, so the whole take has a reference.

Replayed on the REAL evidence (`tests/fixtures/kri1016_plaza_t2_passes.json`: the whisper-1 words
recovered from the job, plus free local whisper.cpp stand-ins and transcriptions of the audio
excerpts) through the real `analyze_speech_cleanup`, the snapshot hydration the render worker does,
`apply_take_window` for the creator's quoted lines (and the worker's own `_phone_opening_cut`), the
real reaction-beat grounding, and the engine's cleanup cut plan. Only transcription, silence
detection and the excerpt cutting are replayed (no paid call, no media).

How it can fail (written before the code):

F1  The re-listen runs on a single-language take: paid calls on every take.
F2  A true pause (the reference heard nothing there) triggers a re-listen.
F3  A re-listened stretch that is a translation (English forced onto Turkish speech) is kept.
F4  Re-listened words land outside their stretch, overlap or push the neighbouring words, or drop a
    kept word next to the stretch.
F5  Re-listened words keep excerpt-relative times: the Turkish lands at 0-7 s.
F6  A take with many wordless stretches makes unbounded paid calls.
F7  An excerpt or a re-listen transcription that fails fails the whole analysis; a programming
    error is swallowed.
F8  The 30 s reference stays the only evidence on a long mixed take: English after 30 s stays a
    Turkish translation.
F9  The rest-of-take Gemini listen runs on single-language takes.
F10 The rest-of-take listen fails: the analysis fails or loses the 30 s reference.
F11 Kill switch off is not byte-identical to KRI-843.
F12 Don Julio T1 (KRI-843) changes.
F13 Downstream: the start line is not found, the photo cue is never heard, the cleanup cuts inside
    the re-listened Turkish speech.
F14 A wordless stretch past the reference's coverage is re-listened with no evidence.

F12 is `test_kri843_mixed_language_transcript.py`, which must stay green unchanged. F9 and F10 live
in the task layer (`_run_engine`), the rest at the engine boundary.
"""

from __future__ import annotations

import functools
import json
import re
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.phone_opening_cut import apply_take_window
from app.pipeline.silence_cut import SILENCE_CUT_VERBATIM_PROMPT
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_cleanup_apply import hydrate_speech_cleanup_snapshot
from app.pipeline.transcribe import TranscribeError, Transcript, Word
from app.services import mixed_language_transcript as mlt
from app.services.clip_speech import SilenceDetectionResult
from app.services.speech_cleanup_preflight import ClaimedSpeechCleanupAnalysis
from app.tasks import generative_build as gb
from app.tasks import speech_cleanup_analysis as task_module

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T2 = json.loads((FIXTURES / "kri1016_plaza_t2_passes.json").read_text())
DURATION_S = float(T2["duration_s"])
START_QUOTE = T2["quotes"]["start"]
END_QUOTE = T2["quotes"]["end"]  # "...of the whole country": the clip ends on "...of the whole"
TAIL_QUOTE = "the government of the whole"
PHOTO_TRIGGER = T2["quotes"]["photo_trigger"]
G30 = T2["references"]["G30"]["text"]  # what the 30 s sample heard
REST = T2["references"]["REST"]["text"]  # what a listen to 30 s-48 s hears
GFULL = T2["references"]["GFULL"]["text"]
MIXED = f"{G30} {REST}"
SAMPLE_S = 30.0
FINGERPRINT = "a" * 64
TRUTH = {row["id"]: row for row in T2["speech_truth"]}
# The three stretches whisper-1 wrote nothing for (the wordless gaps of the stored merged words):
# Maximo's Turkish, the English "Yes." the reference heard ("Means... Yes."), "250 sene önce".
HOLES = [(7.54, 14.46), (23.4, 24.88), (26.38, 32.0)]
PAD_S = 0.25


# -- replay ------------------------------------------------------------------------------------


def _transcript(language: str, rows: list[dict]) -> Transcript:
    words = []
    for row in rows:
        text = str(row["text"]).strip()
        if text and re.search(r"\w", text):
            words.append(
                Word(text=text, start_s=row["start_s"], end_s=row["end_s"], confidence=1.0)
            )
    return Transcript(words=words, full_text=" ".join(w.text for w in words), language=language)


def _fixture_pass(name: str) -> Transcript:
    return _transcript(T2["passes"][name]["language"], T2["passes"][name]["words"])


def _t2(*, extra: str = "en") -> dict[str, Transcript]:
    """The prod-shaped passes: whisper-1's first pass is Turkish, the other is the English one."""
    return {"tr": _fixture_pass("tr"), "en": _fixture_pass(extra)}


def _excerpt_at(path: str, *, start_s: float, end_s: float) -> str:
    """The stub for the engine's excerpt cutter: the 'file' names its own window."""
    return f"{path}@{start_s:.3f}-{end_s:.3f}"


_EXCERPT = re.compile(r"@(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$")


class _Replay:
    """`transcribe_fn`. A full-take call (``language=None`` is the first pass) answers from
    ``passes``; a call on an excerpt (the path ends ``@a-b``) answers with the recorded stretch
    transcription whose window overlaps the request most, shifted to the request's start."""

    def __init__(
        self,
        passes: dict[str, Transcript],
        *,
        first: str,
        stretches: list[dict] | None = None,
        errors: dict | None = None,
        excerpt_errors: dict | None = None,
    ):
        self.passes = passes
        self.first = first
        self.stretches = T2["stretches"] if stretches is None else stretches
        self.errors = errors or {}
        self.excerpt_errors = excerpt_errors or {}
        self.calls: list[str | None] = []  # full-take transcriptions
        self.excerpts: list[tuple[float, float, str | None]] = []  # (start, end, language)
        self.prompts: list[str | None] = []

    def __call__(self, path: str, *, language: str | None, verbatim_prompt: str | None):
        self.prompts.append(verbatim_prompt)
        found = _EXCERPT.search(path)
        if found:
            start_s, end_s = float(found[1]), float(found[2])
            self.excerpts.append((start_s, end_s, language))
            if language in self.excerpt_errors:
                raise self.excerpt_errors[language]
            return self._stretch(start_s, end_s, str(language))
        self.calls.append(language)
        if language in self.errors:
            raise self.errors[language]
        key = self.first if language is None else language
        return self.passes.get(key) or Transcript(words=[], full_text="", language=str(language))

    def _stretch(self, start_s: float, end_s: float, language: str) -> Transcript:
        best, best_overlap = None, 0.0
        for stretch in self.stretches:
            low, high = stretch["window"]
            overlap = min(high, end_s) - max(low, start_s)
            if stretch["language"] == language and overlap > best_overlap:
                best, best_overlap = stretch, overlap
        if best is None:
            return Transcript(words=[], full_text="", language=language)
        shift, length = best["window"][0] - start_s, end_s - start_s
        rows = []
        for row in best["words"]:
            low, high = row["start_s"] + shift, row["end_s"] + shift
            if high < 0 or low > length:
                continue  # the audio of that request does not hold the word
            rows.append({"text": row["text"], "start_s": max(0.0, low), "end_s": min(length, high)})
        return _transcript(language, rows)

    @property
    def excerpt_windows(self) -> list[tuple[float, float]]:
        return sorted({(a, b) for a, b, _ in self.excerpts})


def _silences(spans: list[list[float]]):
    def detect(_path: str, *, min_silence_s: float):
        return SilenceDetectionResult(spans=tuple((s, e) for s, e in spans), status="ok")

    return detect


def _analyze(
    passes: dict[str, Transcript],
    *,
    first: str = "tr",
    reference: str | None = MIXED,
    reference_source: str = "gemini_audio",
    covers_s: float | None = None,
    duration_s: float = DURATION_S,
    silences: list[list[float]] | None = None,
    stretches: list[dict] | None = None,
    errors: dict | None = None,
    excerpt_errors: dict | None = None,
    excerpt_fn=_excerpt_at,
) -> tuple[SpeechCleanupAnalysisResult, _Replay]:
    replay = _Replay(
        passes, first=first, stretches=stretches, errors=errors, excerpt_errors=excerpt_errors
    )
    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=FINGERPRINT,
            local_media_path="take.wav",
            duration_s=duration_s,
            reference_transcript=reference,
            reference_source=reference_source if reference else None,
            reference_covers_s=covers_s,
        ),
        transcribe_fn=replay,
        silence_detect_fn=_silences(T2["silence_spans"] if silences is None else silences),
        **({} if excerpt_fn is None else {"excerpt_fn": excerpt_fn}),
    )
    return result, replay


def _crosscheck(result: SpeechCleanupAnalysisResult) -> dict:
    return result.diagnostics["language_crosscheck"]


@dataclass
class Run:
    result: SpeechCleanupAnalysisResult
    replay: _Replay

    @property
    def words(self) -> list[Word]:
        """What the render worker reads back from the stored analysis."""
        return _snapshot(self.result).source_words()


@functools.cache
def _main_run() -> Run:
    """The incident clip with a reference for the whole take (the rest-of-take listen landed)."""
    result, replay = _analyze(_t2(), covers_s=None)
    return Run(result, replay)


def _rows(words) -> list[tuple[str, float, float]]:
    return [(w.text, w.start_s, w.end_s) for w in words]


# -- the render boundaries ---------------------------------------------------------------------


def _snapshot(result: SpeechCleanupAnalysisResult, *, duration_s: float = DURATION_S):
    payload = json.loads(json.dumps(result.to_payload()))
    return hydrate_speech_cleanup_snapshot(
        {
            "schema_version": 1,
            "analysis_id": str(uuid.uuid4()),
            "engine_version": "preflight-v1-2026-09-05",
            "detector_version": result.detector_version,
            "source": {
                "kind": "embedded_spine",
                "media_identity": "c0",
                "storage_path": "users/u/clip.mp4",
                "generation": "1",
                "window_start_s": 0.0,
                "window_end_s": duration_s,
                "source_policy_fingerprint": FINGERPRINT,
            },
            "analysis": payload,
        }
    ).require_source(
        kind="embedded_spine",
        storage_path="users/u/clip.mp4",
        window_start_s=0.0,
        window_duration_s=duration_s,
    )


def _brief_job(end_line: str = END_QUOTE) -> dict:
    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="start and end on the lines",
                facts={"start_line": START_QUOTE, "end_line": end_line},
            )
        ],
    )
    binding = BriefBinding.create("32a5b608-6397-4608-889f-dff5ad9bec40", brief)
    return {"creator_brief_binding": binding.model_dump(mode="json")}


# -- what was said, per stretch ----------------------------------------------------------------


def _token(text: str) -> str:
    return text.strip(" .,!?¡¿…\"'-").lower()


def _said(words, stretch: dict) -> list[str]:
    """Tokens of the words whose middle lies in the stretch of the ground truth."""
    last = stretch["end_s"] >= DURATION_S  # the take's last word ends exactly on its end
    return [
        _token(w.text)
        for w in words
        if stretch["start_s"] <= (w.start_s + w.end_s) / 2 < stretch["end_s"]
        or (last and (w.start_s + w.end_s) / 2 == DURATION_S)
    ]


_TURKISH_LETTERS = re.compile("[ğşıİçöü]")
_TURKISH_WORDS = frozenset(
    "neden peki ve ne zaman ismin neydi tamam evet bu yer şehir hükümeti diğer tarafta gerçek "
    "vardı bütün çünkü binalarda binaların".split()
)


def _turkish(tokens: list[str]) -> list[str]:
    return [t for t in tokens if _TURKISH_LETTERS.search(t) or t in _TURKISH_WORDS]


def _letters_only(tokens) -> set[str]:
    return {t for t in tokens if re.search("[^\\W\\d_]", t)}


# What a forced-English pass wrote over the speaker's Turkish (recorded) and what the synthetic
# control says: none of it may appear where he spoke Turkish.
EN_ON_TURKISH = _letters_only(
    _token(row["text"]) for s in T2["stretches"] if s["language"] == "en" for row in s["words"]
)
SYNTHETIC_TRANSLATION = _letters_only(
    _token(row["text"])
    for row in T2["passes"]["en_translating"]["words"]
    if 7.7 <= row["start_s"] < 13.2
)

PHRASES = {
    "en1": ("okay i'll translate it to turkish", "what was your name again sorry"),
    "tr1": ("bağımsızlığını", "kazanıldığını", "ilan ediyor", "neden peki"),
    "en2": ("you gained independence", "215 years ago"),
    "tr2": ("sene önce",),
    "en3": ("on the other side of the square", "the government of the whole"),
}


def _relistened(base: Run, now: Run) -> list[tuple[str, float, float]]:
    """The words the re-listen added: in the result, not in the same run without it."""
    before = set(_rows(base.result.timed_words))
    return [row for row in _rows(now.result.timed_words) if row not in before]


def _without_relisten(**kwargs) -> Run:
    """The same analysis with the KRI-1016 switch off: the KRI-843 words, the oracle for what the
    re-listen may and may not change."""
    passes = kwargs.pop("passes", None) or _t2()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "speech_cleanup_mixed_relisten_enabled", False)
        result, replay = _analyze(passes, **kwargs)
    return Run(result, replay)


# -- 1. the incident take ----------------------------------------------------------------------


def _assert_the_incident(result: SpeechCleanupAnalysisResult) -> None:
    """What the engine returned for this take before KRI-1016 (the recorded job): the English
    until 27 s, then the Turkish stretches without a single word, then Turkish translations."""
    words = result.timed_words

    def inside(low: float, high: float) -> list:
        return [w for w in words if low < (w.start_s + w.end_s) / 2 < high]

    assert inside(7.6, 14.4) == [], "Maximo's Turkish has no words"
    assert inside(26.5, 31.6) == [], "'250 sene önce' has no words"
    tail = [_token(w.text) for w in words if w.start_s >= 31.6]
    assert "neden" in tail and "specifically" not in tail  # the Turkish translation stands
    assert _turkish(tail) and "square" not in tail
    # ...so the quoted end line was never found ("couldn't find 'the government of the whole
    # country'"), and the video ended wherever cleanup left it.
    snapshot = _snapshot(result)
    _plan, _start, end = apply_take_window(
        snapshot.cut_plan,
        snapshot.source_words(),
        start_quote=START_QUOTE,
        end_quote=END_QUOTE,
        duration_s=DURATION_S,
    )
    assert end is not None and (end["status"], end["reason"]) == ("not_applied", "not_found")


def test_the_reference_names_both_languages_of_the_take() -> None:
    assert set(mlt.reference_languages(MIXED)) == {"en", "tr"}
    assert set(mlt.reference_languages(G30)) == {"en", "tr"}  # the 30 s sample alone is mixed
    assert mlt.reference_languages(REST) == ["en"]  # the rest alone is not: one call only


@pytest.mark.parametrize("stretch", ["en1", "en2", "en3"])
def test_f8_english_is_written_in_english_across_the_whole_take(stretch) -> None:
    """The English stretches, including everything after 30 s: no Turkish translation."""
    words = _main_run().words
    tokens = _said(words, TRUTH[stretch])
    text = " ".join(tokens)

    for phrase in PHRASES[stretch]:
        assert phrase in text, (stretch, phrase, text)
    assert _turkish(tokens) == [], (stretch, text)


@pytest.mark.parametrize("stretch", ["tr1", "tr2"])
def test_the_speakers_real_turkish_is_written_in_turkish(stretch) -> None:
    """RC3: whisper-1 wrote nothing for these. The re-listen writes it, as Turkish."""
    tokens = _said(_main_run().words, TRUTH[stretch])
    text = " ".join(tokens)

    for phrase in PHRASES[stretch]:
        assert phrase in text, (stretch, phrase, text)
    assert not set(tokens) & EN_ON_TURKISH, (stretch, text)  # F3: no English over Turkish


def test_f4_relistened_words_sit_inside_their_stretch_and_move_nothing(monkeypatch) -> None:
    now = _main_run()
    base = _without_relisten(covers_s=None)
    added = _relistened(base, now)
    stretches = [(e["start_s"], e["end_s"]) for e in _crosscheck(now.result)["relistened"]]

    assert added and stretches
    # Nothing the first merge kept is dropped or moved (the neighbours of the stretches too).
    assert set(_rows(base.result.timed_words)) <= set(_rows(now.result.timed_words))
    for _text, start_s, end_s in added:
        assert any(low - 1e-6 <= start_s <= end_s <= high + 1e-6 for low, high in stretches), (
            start_s,
            end_s,
            stretches,
        )
    words = now.result.timed_words
    assert [(w.start_s, w.end_s) for w in words] == sorted((w.start_s, w.end_s) for w in words)
    assert all(0.0 <= w.start_s <= w.end_s <= DURATION_S + 1e-6 for w in words)
    assert all(a.end_s <= b.start_s + 1e-6 for a, b in zip(words, words[1:], strict=False))
    _snapshot(now.result)  # the render fences words past the window


def test_f5_relistened_words_carry_take_times_not_excerpt_times() -> None:
    """The excerpt starts at 7.29 s and its words are timed from 0: they are stored on the take's
    clock."""
    now = _main_run()
    words = now.result.timed_words
    by_start = {(_token(w.text), round(w.start_s, 2)) for w in words}
    stretches = _crosscheck(now.result)["relistened"]
    checked = 0
    for stretch in T2["stretches"]:
        if stretch["language"] != "tr":
            continue
        shift = stretch["window"][0]
        for row in stretch["words"]:
            start = round(shift + row["start_s"], 2)
            inside = any(s["start_s"] <= start <= s["end_s"] for s in stretches)
            if inside and not any(abs(start - s["start_s"]) < 1e-6 for s in stretches):
                assert (_token(row["text"]), start) in by_start, (row["text"], start)
                checked += 1
    assert checked >= 5
    # The one thing that goes wrong with excerpt-relative times: the Turkish at 0-7 s.
    early = {_token(w.text) for w in words if w.start_s < 7.0}
    assert not early & {"bağımsızlığını", "kazanıldığını", "ilan", "ediyor"}


def test_the_receipt_says_what_was_re_listened() -> None:
    run = _main_run()
    crosscheck = _crosscheck(run.result)

    assert run.replay.calls == [None, "en"]  # the full-take passes are KRI-843's, unchanged
    assert "reference_covers_s" in crosscheck and crosscheck["reference_covers_s"] is None
    assert len(crosscheck["relistened"]) == 3  # Maximo's Turkish, "Yes.", "250 sene önce"
    for entry, (low, high) in zip(crosscheck["relistened"], HOLES, strict=True):
        assert entry["start_s"] == pytest.approx(low, abs=0.02)
        assert entry["end_s"] == pytest.approx(high, abs=0.02)
        assert set(entry["tried"]) == {"en", "tr"} and entry["heard"]
        assert entry["failed"] == []
    assert run.replay.excerpt_windows == [
        pytest.approx(window, abs=0.02)
        for window in [(7.29, 14.71), (23.15, 25.13), (26.13, 32.25)]
    ]
    assert len(run.replay.excerpts) == 6 == mlt.RELISTEN_MAX_CALLS  # three stretches, two languages
    assert set(run.replay.prompts) == {SILENCE_CUT_VERBATIM_PROMPT}  # the same verbatim prompt


def test_f3_a_translation_never_fills_a_turkish_stretch() -> None:
    """The control: the English pass itself has a (made-up) English translation of the Turkish
    stretch. The reference does not confirm it, so the stretch is listened to alone and comes
    out Turkish."""
    result, _replay = _analyze(_t2(extra="en_translating"))
    words = result.timed_words
    tokens = _said(words, TRUTH["tr1"])

    assert "bağımsızlığını" in tokens and "kazanıldığını" in tokens, tokens
    assert not set(tokens) & SYNTHETIC_TRANSLATION, tokens
    assert not set(tokens) & EN_ON_TURKISH, tokens
    assert "önce" in _said(words, TRUTH["tr2"])


# -- 2. downstream: the worker's own use of the words ---------------------------------------------


def test_f13_the_quoted_lines_are_found_where_the_take_says_them() -> None:
    """The recorded run could not find "the government of the whole country" (the take's last
    words came back as "Bütün..."). The clip ends on "...of the whole", and the engine's line
    matching accepts a quote whose last word was not said: with the English after 30 s in
    English the end line is found, and the take already ends on it (nothing to cut)."""
    snapshot = _snapshot(_main_run().result)
    words = snapshot.source_words()

    for quote in (END_QUOTE, TAIL_QUOTE):
        _plan, start, end = apply_take_window(
            snapshot.cut_plan,
            words,
            start_quote=START_QUOTE,
            end_quote=quote,
            duration_s=DURATION_S,
        )
        assert start is not None and start["status"] == "applied" and start["start_s"] == 0.0
        assert end is not None and end["status"] == "applied", (quote, end)
        assert end["end_s"] == pytest.approx(DURATION_S, abs=0.05)


@pytest.mark.parametrize("cleanup", [True, False], ids=["cleanup-on", "cleanup-off"])
def test_f13_the_worker_reads_the_same_lines(cleanup) -> None:
    snapshot = _snapshot(_main_run().result)
    words = snapshot.source_words()

    _plan, receipts = gb._phone_opening_cut(
        _brief_job(),
        snapshot.cut_plan if cleanup else None,
        words if cleanup else (),
        duration_s=DURATION_S,
        off_words=None if cleanup else words,
    )

    assert receipts is not None
    assert receipts["opening_cut"]["status"] == "applied"
    assert receipts["end_cut"]["status"] == "applied", receipts["end_cut"]
    _plan, receipts = gb._phone_opening_cut(
        _brief_job(TAIL_QUOTE),
        snapshot.cut_plan if cleanup else None,
        words if cleanup else (),
        duration_s=DURATION_S,
        off_words=None if cleanup else words,
    )
    assert receipts is not None and receipts["end_cut"]["status"] == "applied"


def _ground_photo(monkeypatch, words) -> dict:
    import app.services.phone_reaction_grounding as rg  # noqa: PLC0415

    monkeypatch.setattr(
        rg,
        "_load_ready_pool_assets",
        lambda *a, **k: [
            {
                "id": "p1",
                "gcs_path": "users/u1/plan/item1/pool/p1.jpg",
                "gcs_generation": "7",
                "kind": "image",
                "source_filename": "casa-rosada.jpg",
                "duration_s": None,
                "aspect": 1.0,
                "user_context": "",
                "analysis": {},
            }
        ],
    )
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: [])
    grounded = rg.ground_phone_reaction_beats(
        lambda: pytest.fail("the DB seams are patched"),
        job_id="j1",
        beats=[
            {
                "visual_id": "p1",
                "visual_role": "photo",
                "beat_id": "casa-rosada",
                "trigger": PHOTO_TRIGGER,
            }
        ],
        closing=None,
        words=[{"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in words],
        duration_s=DURATION_S,
        clip_path=None,
    )
    return grounded.receipt


def test_f13_the_photo_cue_is_heard_where_the_voice_says_it(monkeypatch) -> None:
    receipt = _ground_photo(monkeypatch, _main_run().words)

    assert receipt["unplaced"] == []
    [placed] = receipt["placed"]
    assert placed["at_s"] == pytest.approx(41.0, abs=0.3)  # "On the other side of the square"


def test_f13_cleanup_never_cuts_most_of_a_relistened_word(monkeypatch) -> None:
    """KRI-1017's symptom on this take: the cleanup cut "kazanıldığını" as a pause because the
    stretch had no words. With the words in, no removal eats more than half of any of them."""
    now = _main_run()
    base = _without_relisten(covers_s=None)
    added = _relistened(base, now)
    assert added

    for text, start_s, end_s in added:
        eaten = sum(
            max(0.0, min(end_s, r.end_s) - max(start_s, r.start_s))
            for r in now.result.cut_plan.removed
        )
        assert eaten <= 0.5 * (end_s - start_s) + 1e-6, (text, start_s, end_s, eaten)


# -- 3. the kill switch, the incident as it was --------------------------------------------------


def test_f11_kill_switch_off_is_the_incident_exactly_as_kri843_made_it(monkeypatch) -> None:
    """Switch off, the 30 s reference only (what Fly ran): the Turkish stretches are wordless and
    everything after 30 s is a Turkish translation. Byte-identical to `merge_passes` alone."""
    passes = _t2()
    run = _without_relisten(passes=passes, reference=G30, covers_s=SAMPLE_S)

    assert run.replay.excerpts == []  # not a single excerpt cut, not a single extra call
    assert run.replay.calls == [None, "en"]
    _assert_the_incident(run.result)
    merged = mlt.merge_passes(
        mlt.MixedPass("tr", T2["passes"]["tr"]["words"]),
        [mlt.MixedPass("en", T2["passes"]["en"]["words"])],
        G30,
        duration_s=DURATION_S,
        covers_s=SAMPLE_S,
    )
    assert merged is not None
    assert [(w["text"], w["start_s"], w["end_s"]) for w in merged.words] == _rows(
        run.result.timed_words
    )
    # No new receipt key: the kill switch off leaves the receipt as KRI-843 wrote it.
    assert set(_crosscheck(run.result)) == {
        "whisper_language",
        "reference_language",
        "applied",
        "reference_source",
        "mixed",
        "reference_languages",
        "tried",
        "heard",
        "failed",
        "segments",
    }


@pytest.mark.parametrize(
    "switch", ["speech_cleanup_mixed_relisten_enabled", "speech_cleanup_mixed_language_enabled"]
)
def test_f11_either_switch_off_makes_no_excerpt_call(monkeypatch, switch) -> None:
    monkeypatch.setattr(settings, switch, False)

    result, replay = _analyze(_t2(), covers_s=None)

    assert replay.excerpts == []
    assert "relistened" not in _crosscheck(result)


# -- 4. controls: when the re-listen must stay quiet ---------------------------------------------

ENGLISH_ONLY = (
    "Okay, I'll translate it to Turkish. So what was your name again? Maximo. Why? Why Maximo? "
    "Why and when? You gained independence. Yes. 215 years ago. " + REST
)
HOLE_IN_ENGLISH = (34.0, 38.0)  # "Because this place was" is not in the first pass


def _english_take_with_a_gap() -> dict[str, Transcript]:
    rows = [
        w
        for w in T2["passes"]["local_en"]["words"]
        if not HOLE_IN_ENGLISH[0] <= w["start_s"] < HOLE_IN_ENGLISH[1]
    ]
    return {"first": _transcript("en", rows)}


def test_f1_a_single_language_take_never_re_listens() -> None:
    """The same wordless stretch, the reference heard words in it: but the take is English only."""
    assert mlt.reference_languages(ENGLISH_ONLY) == ["en"]

    result, replay = _analyze(
        _english_take_with_a_gap(), first="first", reference=ENGLISH_ONLY, covers_s=None
    )

    assert replay.calls == [None] and replay.excerpts == []
    assert "relistened" not in _crosscheck(result)


def test_f1_the_same_take_with_a_mixed_reference_does_re_listen() -> None:
    """The control of the control: it is the reference's second language that switches it on."""
    mixed = ENGLISH_ONLY + " Neden peki ve ne zaman bu yer çok güzel."
    assert set(mlt.reference_languages(mixed)) == {"en", "tr"}

    result, replay = _analyze(_english_take_with_a_gap(), first="first", reference=mixed)

    assert [w for w in replay.excerpt_windows if 33.0 <= w[0] and w[1] <= 39.0]
    assert _crosscheck(result)["relistened"]


def test_f2_a_true_pause_is_not_listened_to_again() -> None:
    """The reference heard nothing in 26-32 s ("250 sene önce, 1810, 1810" is not in it): that
    stretch is a pause, so it is not cut and transcribed (the other two stretches still are)."""
    assert "250 sene önce, 1810, 1810." in MIXED
    reference = MIXED.replace("250 sene önce, 1810, 1810.", "")

    result, replay = _analyze(_t2(), reference=reference)

    assert [w for w in replay.excerpt_windows if w[0] < 15.0]  # the rest are still listened to
    assert not [w for w in replay.excerpt_windows if w[1] > 26.0]  # a pause is not
    assert len(_crosscheck(result)["relistened"]) == 2
    assert _said(_snapshot(result).source_words(), TRUTH["tr2"]) == []  # still a pause


def test_f2_the_pure_function_finds_the_three_holes_and_only_those() -> None:
    merged = mlt.merge_passes(
        mlt.MixedPass("tr", T2["passes"]["tr"]["words"]),
        [mlt.MixedPass("en", T2["passes"]["en"]["words"])],
        MIXED,
        duration_s=DURATION_S,
        covers_s=None,
    )
    assert merged is not None
    words = list(merged.words)

    found = mlt.unheard_stretches(words, MIXED, duration_s=DURATION_S, covers_s=None)
    assert [(s.start_s, s.end_s) for s in found] == [pytest.approx(h, abs=0.02) for h in HOLES]
    assert found[0].reference_tokens >= 8 and found[1].reference_tokens >= 2
    assert found[2].reference_tokens >= 2
    paused = MIXED.replace("250 sene önce, 1810, 1810.", "")
    assert len(mlt.unheard_stretches(words, paused, duration_s=DURATION_S)) == 2


@pytest.mark.parametrize("reference", [G30, MIXED], ids=["30s-reference", "whole-reference"])
def test_f14_nothing_past_the_references_coverage_is_re_listened(reference) -> None:
    """The reference only covers 30 s. A wordless stretch at 34-39 s (the Turkish translation's
    words dropped there) has no evidence behind it: no excerpt is cut for it, and the one before
    30 s ends at 30 s (plus the pad), not at the next word."""
    tr = _fixture_pass("tr")
    tr.words = [w for w in tr.words if not 34.3 <= w.start_s < 39.0]

    result, replay = _analyze(
        {"tr": tr, "en": _fixture_pass("en")}, reference=reference, covers_s=SAMPLE_S
    )

    assert replay.excerpts, "the stretch inside the coverage is still listened to"
    assert all(b <= SAMPLE_S + PAD_S + 1e-6 for _a, b in replay.excerpt_windows)
    assert all(e["end_s"] <= SAMPLE_S + 1e-6 for e in _crosscheck(result)["relistened"])
    assert _crosscheck(result)["reference_covers_s"] == SAMPLE_S


def test_f14_the_pure_function_clips_a_stretch_to_the_coverage() -> None:
    merged = mlt.merge_passes(
        mlt.MixedPass("tr", T2["passes"]["tr"]["words"]),
        [mlt.MixedPass("en", T2["passes"]["en"]["words"])],
        G30,
        duration_s=DURATION_S,
        covers_s=SAMPLE_S,
    )
    assert merged is not None

    found = mlt.unheard_stretches(list(merged.words), G30, duration_s=DURATION_S, covers_s=SAMPLE_S)

    assert [s.start_s for s in found] == [pytest.approx(h[0], abs=0.02) for h in HOLES]
    assert found[-1].end_s == pytest.approx(SAMPLE_S, abs=1e-6)  # 26.38 s to 30 s, not to 32 s


# -- 5. many wordless stretches: bounded calls --------------------------------------------------

_ANCHORS = [
    "Hello there",
    "Okay then",
    "Thank you",
    "Right now",
    "Look here",
    "Come on",
    "Good one",
    "Nice work",
    "Well done",
    "Next up",
]
_TURKISH = (
    "bugün çok güzel bir yer göstereceğim burası istanbul'un en eski kahvecisi fiyatlar biraz "
    "pahalı ama değer hadi gidelim tamam evet"
).split()


def _many_gaps() -> tuple[dict[str, Transcript], str]:
    """Ten English anchors 10 s apart; between anchor k and k+1 the reference has 2+k Turkish
    words nobody wrote (the longest gaps are the last)."""
    words, reference, used = [], [], 0
    for k, anchor in enumerate(_ANCHORS):
        for j, text in enumerate(anchor.split()):
            words.append({"text": text, "start_s": 10.0 * k + j, "end_s": 10.0 * k + j + 0.9})
        reference.append(anchor + ".")
        if k < len(_ANCHORS) - 1:
            count = 2 + k
            gap = [_TURKISH[(used + i) % len(_TURKISH)] for i in range(count)]
            reference.append(" ".join(gap) + ".")
            used += count
    return {"first": _transcript("en", words)}, " ".join(reference)


def test_f6_many_wordless_stretches_make_a_bounded_number_of_calls() -> None:
    passes, reference = _many_gaps()
    assert set(mlt.reference_languages(reference)) == {"en", "tr"}

    result, replay = _analyze(
        passes, first="first", reference=reference, duration_s=95.0, silences=[]
    )

    # Nine gaps could be listened to; at most three stretches and six calls are paid for...
    assert 0 < len(replay.excerpts) <= mlt.RELISTEN_MAX_CALLS
    assert len(replay.excerpt_windows) <= mlt.RELISTEN_MAX_STRETCHES
    assert len(_crosscheck(result)["relistened"]) <= mlt.RELISTEN_MAX_STRETCHES
    # ...and they are the stretches the reference has the most words for (gaps 6, 7 and 8).
    for low, high in replay.excerpt_windows:
        assert any(
            10.0 * k + 1.9 - PAD_S - 1e-6 <= low and high <= 10.0 * (k + 1) + PAD_S + 1e-6
            for k in (6, 7, 8)
        ), (low, high)
    assert len(replay.excerpt_windows) == mlt.RELISTEN_MAX_STRETCHES


# -- 6. failures --------------------------------------------------------------------------------


def _raises(error: Exception):
    def excerpt(_path: str, *, start_s: float, end_s: float) -> str:
        raise error

    return excerpt


@pytest.mark.parametrize(
    "error",
    [OSError("disk full"), wave.Error("bad header"), TimeoutError("cut timed out")],
    ids=["os-error", "wave-error", "timeout"],
)
def test_f7_an_excerpt_that_cannot_be_cut_is_skipped_and_reported(monkeypatch, error) -> None:
    base = _without_relisten(covers_s=None)

    result, replay = _analyze(_t2(), covers_s=None, excerpt_fn=_raises(error))

    assert replay.excerpts == []  # nothing was cut, so nothing was transcribed
    assert _rows(result.timed_words) == _rows(base.result.timed_words)  # the merge stands
    entries = _crosscheck(result)["relistened"]
    assert len(entries) == 3 and all(e["failed"] and not e["heard"] for e in entries)


def test_f7_a_failed_transcription_of_one_language_keeps_the_other(monkeypatch) -> None:
    result, replay = _analyze(
        _t2(), covers_s=None, excerpt_errors={"en": TranscribeError("provider down")}
    )

    tokens = _said(_snapshot(result).source_words(), TRUTH["tr1"])
    assert "bağımsızlığını" in tokens  # the Turkish transcription still landed
    entries = _crosscheck(result)["relistened"]
    assert all("en" in e["failed"] and "tr" in e["heard"] for e in entries)
    assert {lang for _a, _b, lang in replay.excerpts} == {"en", "tr"}  # both were asked for


@pytest.mark.parametrize("error", [TypeError("a bug"), KeyError("a bug")], ids=["type", "key"])
def test_f7_a_programming_error_in_the_excerpt_cutter_escapes(error) -> None:
    with pytest.raises(type(error), match="a bug"):
        _analyze(_t2(), covers_s=None, excerpt_fn=_raises(error))


@pytest.mark.parametrize("error", [TypeError("a bug"), KeyError("a bug")], ids=["type", "key"])
def test_f7_a_programming_error_in_an_excerpt_transcription_escapes(error) -> None:
    with pytest.raises(type(error), match="a bug"):
        _analyze(_t2(), covers_s=None, excerpt_errors={"tr": error})


@pytest.mark.parametrize("where", ["cutter", "transcription"])
def test_f7_a_worker_time_limit_escapes(where) -> None:
    if where == "cutter":
        with pytest.raises(SoftTimeLimitExceeded):
            _analyze(_t2(), covers_s=None, excerpt_fn=_raises(SoftTimeLimitExceeded()))
    else:
        with pytest.raises(SoftTimeLimitExceeded):
            _analyze(_t2(), covers_s=None, excerpt_errors={"en": SoftTimeLimitExceeded()})


# -- 7. the task layer: a mixed take's reference covers the whole take ----------------------------


def _work(duration_s: float = DURATION_S, **overrides) -> task_module._ClaimedWork:
    values = {
        "claim": ClaimedSpeechCleanupAnalysis(
            analysis_id=uuid.uuid4(),
            attempt_token="attempt-1",
            source_policy_fingerprint="source-fingerprint",
        ),
        "source_storage_path": "users/u/clip.mp4",
        "source_generation": "1700000000000000",
        "window_start_s": 0.0,
        "window_end_s": duration_s,
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": "detector",
        "source_kind": "embedded_spine",
    }
    values.update(overrides)
    return task_module._ClaimedWork(**values)


class _Gemini:
    """`_transcribe_reference_sample(work, local_audio_path, *, start_s, end_s)`, recorded."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.windows: list[tuple[float, float]] = []

    def __call__(self, _work, _path, *, start_s: float = 0.0, end_s: float = SAMPLE_S):
        self.windows.append((start_s, end_s))
        answer = self.answers[min(len(self.windows), len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _reference_for(monkeypatch, gemini: _Gemini, *, duration_s: float = DURATION_S, **work):
    """The reference and coverage the worker hands the engine."""
    captured: list[SpeechCleanupAnalysisInput] = []
    monkeypatch.setattr(settings, "speech_cleanup_gemini_reference_enabled", True)
    monkeypatch.setattr(task_module, "_transcribe_reference_sample", gemini)
    monkeypatch.setattr(
        task_module,
        "run_speech_cleanup_engine",
        lambda analysis_input, **_kw: (
            captured.append(analysis_input) or SimpleNamespace(language="en")
        ),
    )
    monkeypatch.setattr(task_module, "_late_reference_transcript", lambda _work: None)
    task_module._run_engine(
        _work(duration_s, source_kind=work.pop("source_kind", "embedded_spine"), **work),
        Path("narration.wav"),
    )
    [analysis_input] = captured
    return analysis_input.reference_transcript, analysis_input.reference_covers_s


def test_part_b_a_mixed_take_gets_a_second_listen_to_the_rest(monkeypatch) -> None:
    gemini = _Gemini(G30, REST)

    reference, covers_s = _reference_for(monkeypatch, gemini)

    assert gemini.windows == [
        pytest.approx((0.0, SAMPLE_S)),
        pytest.approx((SAMPLE_S, DURATION_S)),
    ]
    assert reference == f"{G30} {REST}"  # the sample first, one space
    assert covers_s is None  # the reference now heard the whole take


def test_f9_a_single_language_take_is_listened_to_once(monkeypatch) -> None:
    gemini = _Gemini(REST)  # English only (said as the 30 s sample)

    reference, covers_s = _reference_for(monkeypatch, gemini)

    assert gemini.windows == [pytest.approx((0.0, SAMPLE_S))]
    assert (reference, covers_s) == (REST, SAMPLE_S)


@pytest.mark.parametrize("duration_s", [20.0, 30.5])
def test_f9_a_take_the_sample_already_covers_is_listened_to_once(monkeypatch, duration_s) -> None:
    gemini = _Gemini(G30, REST)

    reference, _covers_s = _reference_for(monkeypatch, gemini, duration_s=duration_s)

    assert len(gemini.windows) == 1 and reference == G30


@pytest.mark.parametrize(
    "switch", ["speech_cleanup_mixed_relisten_enabled", "speech_cleanup_mixed_language_enabled"]
)
def test_f9_f11_either_switch_off_keeps_todays_single_listen(monkeypatch, switch) -> None:
    monkeypatch.setattr(settings, switch, False)
    gemini = _Gemini(G30, REST)

    reference, covers_s = _reference_for(monkeypatch, gemini)

    assert gemini.windows == [pytest.approx((0.0, SAMPLE_S))]
    assert (reference, covers_s) == (G30, SAMPLE_S)


@pytest.mark.parametrize(
    "rest",
    [RuntimeError("gemini 503"), TimeoutError("never active"), None],
    ids=["provider-error", "timeout", "nothing"],
)
def test_f10_a_failed_rest_listen_keeps_the_thirty_second_reference(monkeypatch, rest) -> None:
    gemini = _Gemini(G30, rest)

    reference, covers_s = _reference_for(monkeypatch, gemini)

    assert len(gemini.windows) == 2  # it was asked for...
    assert (reference, covers_s) == (G30, SAMPLE_S)  # ...and the analysis went on without it


def test_f10_a_worker_time_limit_in_the_rest_listen_escapes(monkeypatch) -> None:
    gemini = _Gemini(G30, SoftTimeLimitExceeded())

    with pytest.raises(SoftTimeLimitExceeded):
        _reference_for(monkeypatch, gemini)


def test_f10_a_failed_sample_makes_no_rest_call(monkeypatch) -> None:
    gemini = _Gemini(RuntimeError("gemini 503"), REST)

    reference, covers_s = _reference_for(monkeypatch, gemini, source_kind="voiceover")

    assert len(gemini.windows) == 1
    assert (reference, covers_s) == (None, None)


def test_part_b_a_clip_analysis_reference_needs_no_listen_at_all(monkeypatch) -> None:
    gemini = _Gemini(G30, REST)

    reference, covers_s = _reference_for(monkeypatch, gemini, reference_transcript=GFULL)

    assert gemini.windows == []
    assert (reference, covers_s) == (GFULL, None)


# -- 8. the whole recorded take, as local whisper heard it ---------------------------------------


def test_a_mixed_take_whose_passes_heard_everything_makes_no_excerpt_call() -> None:
    """Locally both forced passes wrote the Turkish (prod whisper-1 did not), so no stretch is
    wordless: the re-listen is only for what is missing, and costs nothing here."""
    passes = {"tr": _fixture_pass("local_tr"), "en": _fixture_pass("local_en")}

    result, replay = _analyze(passes, reference=GFULL, covers_s=None)

    assert replay.excerpts == []
    assert "relistened" not in _crosscheck(result)
