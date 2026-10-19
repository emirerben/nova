#!/usr/bin/env python3
"""Score voice dictation against what the creator actually said (KRI-742).

Record ~20 real edit requests on the phone (Turkish, English, mixed, noisy, with clip names and
numbers), drop them in a folder with a sidecar JSON each, and run:

    cd src/apps/api && .venv/bin/python scripts/dictation_eval.py .dictation-eval/

Sidecar ``<name>.json`` next to ``<name>.m4a`` (or .wav/.mp3):

    {
      "truth": "make the Bodrum clip first and cut the intro to two seconds",
      "apple": "make the both room clip first and cut the intro to 2 seconds",   // optional
      "terms": ["Bodrum", "Sunset Beach"],        // what the app would send as context_terms
      "key_terms": ["Bodrum", "two", "2"]         // names/numbers that must come out right
    }

``apple`` is the on-device text shown live in the app (copy it off the phone); without it that arm
is skipped. Arms: Whisper with no vocabulary, Whisper with the app's vocabulary prompt (what ships).

Pass bar (KRI-742): the shipped arm's overall word error rate is <= 10% and below Apple-only, and
names + numbers are right in >= 90% of samples. Costs about $0.006 per recording minute of
whisper-1. Writes ``report.json`` next to the recordings and exits 1 if the bar is missed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.transcribe import transcribe_whisper  # noqa: E402
from app.routes.dictation import build_vocabulary_prompt  # noqa: E402

AUDIO_SUFFIXES = (".m4a", ".wav", ".mp3", ".mp4", ".aac")
PASS_WER = 0.10
PASS_KEY_TERMS = 0.90


def normalize(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def wer(truth: str, hypothesis: str) -> float:
    ref, hyp = normalize(truth), normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def key_terms_right(key_terms: list[str], hypothesis: str) -> tuple[int, int]:
    words = " ".join(normalize(hypothesis))
    hits = sum(1 for term in key_terms if " ".join(normalize(term)) in words)
    return hits, len(key_terms)


def main(folder: Path) -> int:
    rows = []
    for audio in sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES):
        sidecar = audio.with_suffix(".json")
        if not sidecar.exists():
            print(f"skip {audio.name}: no {sidecar.name}")
            continue
        meta = json.loads(sidecar.read_text())
        truth = meta["truth"]
        terms = meta.get("terms", [])
        plain = transcribe_whisper(str(audio)).full_text
        shipped = transcribe_whisper(
            str(audio), verbatim_prompt=build_vocabulary_prompt(terms)
        ).full_text
        row = {
            "file": audio.name,
            "truth": truth,
            "whisper_plain": {"text": plain, "wer": wer(truth, plain)},
            "whisper_shipped": {"text": shipped, "wer": wer(truth, shipped)},
        }
        if meta.get("apple"):
            row["apple"] = {"text": meta["apple"], "wer": wer(truth, meta["apple"])}
        hits, total = key_terms_right(meta.get("key_terms", []), shipped)
        row["key_terms"] = {"hits": hits, "total": total}
        rows.append(row)
        print(f"{audio.name}: shipped WER {row['whisper_shipped']['wer']:.2f}")

    if not rows:
        print("no recordings with a sidecar found")
        return 1

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else float("nan")

    shipped_wer = mean([r["whisper_shipped"]["wer"] for r in rows])
    plain_wer = mean([r["whisper_plain"]["wer"] for r in rows])
    apple_rows = [r for r in rows if "apple" in r]
    apple_wer = mean([r["apple"]["wer"] for r in apple_rows]) if apple_rows else None
    hits = sum(r["key_terms"]["hits"] for r in rows)
    total = sum(r["key_terms"]["total"] for r in rows)
    key_rate = hits / total if total else None

    checks = {
        f"shipped WER <= {PASS_WER:.0%}": shipped_wer <= PASS_WER,
        "shipped WER below Apple-only": apple_wer is None or shipped_wer < apple_wer,
        f"names + numbers right >= {PASS_KEY_TERMS:.0%}": key_rate is None
        or key_rate >= PASS_KEY_TERMS,
    }
    report = {
        "samples": len(rows),
        "wer": {
            "apple_only": apple_wer,
            "whisper_plain": plain_wer,
            "whisper_shipped": shipped_wer,
        },
        "key_term_rate": key_rate,
        "checks": checks,
        "passed": all(checks.values()),
        "rows": rows,
    }
    (folder / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {k: report[k] for k in ("samples", "wer", "key_term_rate", "checks", "passed")},
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
