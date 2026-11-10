"""Caption timing proven on the audio itself (no transcriber noise).

For every caption word of a case, the word's own SOURCE audio (its take, its preflight
time) is located inside the exported video by cross-correlation near the caption's time.
The lag is how far the caption sits from where that sound really plays. Pass: |lag| <= 0.1 s
for every word that correlates clearly. Negative control: the same captions searched in the
UNCUT export must fail.

Run with the API venv's python (numpy):  python kri720_audio_align.py OUT
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 8000
TAKES = {"takeA": "IMG_7244.MOV", "takeB": "IMG_7245.MOV", "takeC": "IMG_7252.MOV"}
ORDER = {
    "s3_clean": ["takeA", "takeB", "takeC"],
    "s3_clean_a_last": ["takeB", "takeC", "takeA"],
    "single_clean": ["takeA"],
    "s3_uncut": ["takeA", "takeB", "takeC"],
}
FILLERS = {"um", "uh", "erm", "hmm", "mm"}


def pcm(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SR),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def norm(t: str) -> str:
    return "".join(ch for ch in t.lower() if ch.isalnum() or ch == "'")


def lag_of(
    export: np.ndarray, source: np.ndarray, t_cap: float, t_src: float
) -> tuple[float, float]:
    """Where the 0.4 s source snippet starting at the word sits in the export, relative to t_cap."""
    n = int(0.4 * SR)
    s0 = int(t_src * SR)
    snippet = source[s0 : s0 + n]
    if len(snippet) < n or np.std(snippet) < 1e-3:
        return float("nan"), 0.0
    search = int(0.6 * SR)
    e0 = max(0, int(t_cap * SR) - search)
    window = export[e0 : int(t_cap * SR) + search + n]
    if len(window) < n:
        return float("nan"), 0.0
    snippet = (snippet - snippet.mean()) / (snippet.std() + 1e-9)
    best, best_k = -1.0, 0
    for k in range(0, len(window) - n + 1, 4):
        seg = window[k : k + n]
        sd = seg.std()
        if sd < 1e-4:
            continue
        r = float(np.dot(snippet, (seg - seg.mean()) / sd) / n)
        if r > best:
            best, best_k = r, k
    return (e0 + best_k) / SR - t_cap, best


def main(out: Path, kit: Path) -> None:
    analysis = {
        t["media_id"]: t for t in json.load(open(out / "analysis.json"))["takes"]
    }
    sources = {mid: pcm(kit / "upload" / "S3" / name) for mid, name in TAKES.items()}
    report = {}
    exports = {case: pcm(out / "cases" / case / "phone.mp4") for case in ORDER}

    def judge(case: str, export_case: str) -> dict:
        variant = json.load(open(out / "cases" / case / "variant.json"))
        cap_words = [
            w for cue in variant["caption_cues"] for w in cue.get("words") or []
        ]
        cut = case != "s3_uncut"
        src_words = []
        for mid in ORDER[case]:
            a = analysis[mid]["analysis"]
            removed = (
                [(r["start_s"], r["end_s"]) for r in a["cut_plan"]["removed"]]
                if cut
                else []
            )
            for w in a["timed_words"]:
                if norm(w["text"]) in FILLERS:
                    continue
                m = (w["start_s"] + w["end_s"]) / 2
                if any(s <= m < e for s, e in removed):
                    continue
                src_words.append((mid, norm(w["text"]), w["start_s"]))
        import difflib

        sm = difflib.SequenceMatcher(
            a=[t for _, t, _ in src_words],
            b=[norm(c["text"]) for c in cap_words],
            autojunk=False,
        )
        pairs = [
            (src_words[b.a + k], cap_words[b.b + k])
            for b in sm.get_matching_blocks()
            for k in range(b.size)
        ]
        unpaired = len(cap_words) - len(pairs)
        lags, weak = [], 0
        for (mid, text, t_src), cap in pairs:
            lag, r = lag_of(
                exports[export_case], sources[mid], float(cap["start_s"]), t_src
            )
            if r < 0.6 or lag != lag:
                weak += 1
                continue
            lags.append(lag)
        lags = np.abs(np.array(lags))
        return {
            "words": len(cap_words),
            "caption_words_not_paired_to_a_source_word": unpaired,
            "clear_matches": int(len(lags)),
            "weak_or_silent": weak,
            "max_abs_lag_s": round(float(lags.max()), 3) if len(lags) else None,
            "p95_abs_lag_s": round(float(np.percentile(lags, 95)), 3)
            if len(lags)
            else None,
            "over_0.1s": int((lags > 0.1).sum()),
        }

    for case in ORDER:
        report[case] = judge(case, case)
    report["control_a_last_captions_in_uncut_export"] = judge(
        "s3_clean_a_last", "s3_uncut"
    )
    (out / "audio_align.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]).expanduser())
