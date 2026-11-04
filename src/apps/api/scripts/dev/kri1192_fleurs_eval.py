"""KRI-1192 free offline evidence: do spoken captions stay in the spoken language?

Real native read speech from Google FLEURS (CC BY 4.0, test split) for the ten new caption
languages plus English and Turkish controls. Nothing here calls a paid model:

1. stream N clips per language from huggingface.co/datasets/google/fleurs (only the first
   members of each test tarball are read; audio stays in --out, never in git);
2. transcribe each clip with whisper.cpp (``whisper-cli``, language auto-detect), the free
   local stand-in for prod's whisper-1;
3. run Nova's REAL language decisions on it: ``transcribe._normalize_lang`` on whisper's
   reported language (as whisper-1 reports it: a full name), ``crosscheck_detected_language``
   against the FLEURS human transcript (standing in for Gemini's), and
   ``detect_text_language``; when the cross-check asks for a re-run, whisper.cpp is run
   again with that hint and THAT text is what ships, exactly as the worker adopts it;
4. build caption text the way the pipeline does (Serbian one-alphabet step, plain cues,
   sentence re-split) and score word/character error rate against the FLEURS transcript
   with jiwer (Whisper-style basic normalisation on both sides; Serbian compared in Latin);
5. optionally compare ``detect_text_language`` with the independent lingua detector.

Usage (from src/apps/api; needs whisper-cli and a ggml model; jiwer, optionally lingua):

    PYTHONPATH=$PWD python scripts/dev/kri1192_fleurs_eval.py \\
        --model ~/models/ggml-large-v3-turbo-q5_0.bin --out ../../../.caption-verify/fleurs

Writes ``report.json`` (every clip) and ``report.md`` (per-language table) in --out.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tarfile
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path

for _key, _value in {
    "STORAGE_BUCKET": "nova-eval",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://eval@localhost/eval",
    "OPENAI_API_KEY": "sk-not-used",
    "TOKEN_ENCRYPTION_KEY": "not-used",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}.items():
    os.environ.setdefault(_key, _value)

from app.pipeline import caption_language as cl  # noqa: E402
from app.pipeline.captions import build_plain_cues, resplit_cues_into_sentences  # noqa: E402
from app.pipeline.transcribe import Word, _normalize_lang  # noqa: E402

FLEURS = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"
CONFIGS = {
    "pt_br": "pt",
    "es_419": "es",
    "el_gr": "el",
    "de_de": "de",
    "ko_kr": "ko",
    "it_it": "it",
    "fr_fr": "fr",
    "ar_eg": "ar",
    "da_dk": "da",
    "sr_rs": "sr",
    "en_us": "en",
    "tr_tr": "tr",
}
CER_LANGUAGES = {"ko", "ar"}
EXPECTED_SCRIPT = {"el": "GREEK", "ko": "HANGUL", "ar": "ARABIC"}  # everything else: LATIN
_ARABIC_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")


def _fetch(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "nova-kri1192-eval"})
    return urllib.request.urlopen(request, timeout=60)  # noqa: S310 - fixed https host


def download(out: Path, config: str, per_language: int) -> list[dict]:
    folder = out / "audio" / config
    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    folder.mkdir(parents=True, exist_ok=True)
    rows = {}
    tsv = _fetch(f"{FLEURS}/{config}/test.tsv").read().decode("utf-8")
    for r in csv.reader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        rows[r[1]] = {"id": r[0], "file": r[1], "raw": r[2], "samples": int(r[5])}
    kept: list[dict] = []
    seen: set[str] = set()
    with _fetch(f"{FLEURS}/{config}/audio/test.tar.gz") as response:
        with tarfile.open(fileobj=response, mode="r|gz") as archive:
            for member in archive:
                row = rows.get(member.name.rsplit("/", 1)[-1])
                if not member.isfile() or not row or row["id"] in seen:
                    continue
                if not 5.0 <= row["samples"] / 16000 <= 20.0:
                    continue
                (folder / row["file"]).write_bytes(archive.extractfile(member).read())
                seen.add(row["id"])
                kept.append(row)
                if len(kept) >= per_language:
                    break
    manifest_path.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    return kept


def whisper(model: str, wav: Path, out_stem: Path, language: str = "auto") -> dict:
    json_path = out_stem.with_suffix(".json")
    if not json_path.exists():
        out_stem.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["whisper-cli", "-m", model, "-f", str(wav), "-l", language, "-np", "-oj"]
            + ["-of", str(out_stem)],
            check=True,
            capture_output=True,
        )
    data = json.loads(json_path.read_bytes().decode("utf-8", "replace"))
    segments = [
        (s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip())
        for s in data["transcription"]
    ]
    return {"language": data["result"]["language"], "segments": segments}


# whisper-1 reports a full lowercase name ("greek"), not the code whisper.cpp prints.
_WHISPER1_NAMES = {lang.code: lang.english.lower() for lang in cl.CAPTION_LANGUAGES} | {
    "hr": "croatian",
    "bs": "bosnian",
}


def caption_text(segments: list[tuple[float, float, str]], language: str) -> str:
    """The text the captions show: words through the one-alphabet step, plain cues and
    the sentence re-split (spread evenly over each whisper segment)."""
    words: list[Word] = []
    for start, end, text in segments:
        tokens = text.split()
        step = (end - start) / max(1, len(tokens))
        words.extend(
            Word(text=token, start_s=start + i * step, end_s=start + (i + 1) * step, confidence=1.0)
            for i, token in enumerate(tokens)
        )
    if language == "sr":
        for word in words:
            word.text = cl.serbian_latin(word.text)
    cues = resplit_cues_into_sentences(build_plain_cues(words))
    return " ".join(str(cue.get("text") or "") for cue in cues)


def normalise(text: str, language: str) -> str:
    text = unicodedata.normalize("NFKC", cl.serbian_latin(text) if language == "sr" else text)
    if language == "ar":
        text = _ARABIC_MARKS.sub("", text)
    text = "".join(" " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in text.casefold())
    return " ".join(text.split())


def error_rate(reference: str, hypothesis: str, language: str) -> float:
    import jiwer  # noqa: PLC0415

    ref, hyp = normalise(reference, language), normalise(hypothesis, language) or "-"
    return jiwer.cer(ref, hyp) if language in CER_LANGUAGES else jiwer.wer(ref, hyp)


def script_of(text: str) -> str:
    counts = Counter(unicodedata.name(ch, "?").split(" ", 1)[0] for ch in text if ch.isalpha())
    return counts.most_common(1)[0][0] if counts else ""


def lingua_detector():
    try:
        from lingua import Language, LanguageDetectorBuilder  # noqa: PLC0415
    except ImportError:
        return None
    names = {
        "en": "ENGLISH", "tr": "TURKISH", "de": "GERMAN", "es": "SPANISH", "fr": "FRENCH",
        "it": "ITALIAN", "ar": "ARABIC", "nl": "DUTCH", "pt": "PORTUGUESE", "el": "GREEK",
        "ko": "KOREAN", "da": "DANISH", "sr": "SERBIAN",
    }  # fmt: skip
    # lingua's SERBIAN model is Cyrillic-only; Latin Serbian is its Croatian/Bosnian, which
    # Nova treats as one caption language with Serbian (whisper cannot tell them apart).
    languages = [getattr(Language, name) for name in (*names.values(), "CROATIAN", "BOSNIAN")]
    detector = LanguageDetectorBuilder.from_languages(*languages).build()
    back = {getattr(Language, name): code for code, name in names.items()}
    back |= {Language.CROATIAN: "sr", Language.BOSNIAN: "sr"}

    def detect(text: str) -> str | None:
        found = detector.detect_language_of(text)
        return back.get(found) if found else None

    return detect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--model", required=True, help="whisper.cpp ggml model path")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--per-language", type=int, default=12)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    lingua = lingua_detector()
    rows: list[dict] = []
    for config, language in CONFIGS.items():
        for clip in download(args.out, config, args.per_language):
            stem = clip["file"].rsplit(".", 1)[0]
            wav = args.out / "audio" / config / clip["file"]
            heard = whisper(args.model, wav, args.out / "asr" / config / stem)
            reported = _WHISPER1_NAMES.get(heard["language"], heard["language"])
            detected = _normalize_lang(reported)
            redo = cl.crosscheck_detected_language(detected, reference_text=clip["raw"])
            shipped = heard
            if redo:
                shipped = whisper(
                    args.model, wav, args.out / "asr_hint" / config / f"{stem}.{redo}", redo
                )
            raw_text = " ".join(s[2] for s in heard["segments"])
            final = caption_text(shipped["segments"], language)
            rows.append(
                {
                    "config": config,
                    "language": language,
                    "fleurs_id": clip["id"],
                    "whisper_reported": reported,
                    "caption_language": detected,
                    "rerun_hint": redo,
                    "text_language": cl.detect_text_language(final),
                    "lingua": lingua(final) if lingua else None,
                    "script": script_of(final),
                    "raw_error": error_rate(clip["raw"], raw_text, language),
                    "final_error": error_rate(clip["raw"], final, language),
                    "reference": clip["raw"],
                    "caption_text": final,
                }
            )
        print(config, "done", file=sys.stderr)
    (args.out / "report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    lines = [
        "| Language | Clips | Caption language = spoken | Re-run in another language | "
        "Script ok | Text-ID wrong | Raw whisper error | Final caption error | lingua agrees |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for config, language in CONFIGS.items():
        group = [r for r in rows if r["config"] == config]
        expected = EXPECTED_SCRIPT.get(language, "LATIN")
        decided = [r for r in group if r["text_language"]]
        lines.append(
            f"| {language} | {len(group)} "
            f"| {sum(r['caption_language'] == language for r in group)} "
            f"| {sum(bool(r['rerun_hint']) for r in group)} "
            f"| {sum(r['script'] == expected for r in group)} "
            f"| {sum(r['text_language'] != language for r in decided)} "
            f"| {statistics.mean(r['raw_error'] for r in group):.3f} "
            f"| {statistics.mean(r['final_error'] for r in group):.3f} "
            f"| {sum(r['lingua'] == r['text_language'] for r in decided) if lingua else '-'}"
            f"/{len(decided)} |"
        )
    (args.out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
