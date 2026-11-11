"""Measure the KRI-720 proof exports (free: ffmpeg + whisper.cpp).

For each case: export length vs recipe, silences >= 0.4 s inside speech (silencedetect),
words the creator said that are missing from the export (whisper.cpp of the export vs the
kept preflight words), and caption timing (each caption word vs the same word heard in the
export). Negative control: the a_last captions judged against the UNCUT export must fail.

    python3 kri720_measure.py OUT
"""

from __future__ import annotations

import difflib
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

MODEL = str(Path("~/Downloads/kria-cigdem-test/_src/models/ggml-large-v3-turbo-q5_0.bin").expanduser())
FILLERS = {"um", "uh", "erm", "hmm", "mm"}
ORDER = {
    "s3_clean": ["takeA", "takeB", "takeC"],
    "s3_uncut": ["takeA", "takeB", "takeC"],
    "s3_clean_a_last": ["takeB", "takeC", "takeA"],
    "single_clean": ["takeA"],
    "s3_named_line": ["takeA", "takeB", "takeC"],
    "s3_named_line_tail": ["takeA", "takeB", "takeC"],
}
# Lines the creator named in the brief (cut from the one take that says them): their words are
# not expected in the export; everything else is.
NAMED = {
    "s3_named_line": ["but", "that's", "for", "the", "next", "episode"],
    "s3_named_line_tail": ["don't", "forget", "to", "follow"],
}
# Words that must be GONE (slate / restart) and words that must stay (neighbours / controls).
GONE_EVERYWHERE = {"wait the light okay": ["wait", "the", "light", "okay"], "oh sorry again": ["oh", "sorry", "again"]}
STAY = {
    "s3_named_line": ["cafes", "don't forget to follow"],
    "s3_named_line_tail": ["but that's for the next episode", "cafes"],
}


def norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower().replace("’", "'"))


def heard(mp4: Path) -> list[tuple[str, float, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(mp4), "-vn", "-ac", "1",
                        "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)
        base = str(Path(tmp) / "w")
        subprocess.run(["whisper-cli", "-m", MODEL, "-l", "en", "-f", str(wav), "-ml", "1", "-sow",
                        "-oj", "-of", base, "-t", "8", "-np"], check=True, capture_output=True)
        data = json.load(open(base + ".json"))
    return [(s["text"].strip(), s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000)
            for s in data["transcription"] if s["text"].strip()]


def silences(mp4: Path) -> list[tuple[float, float]]:
    err = subprocess.run(["ffmpeg", "-nostdin", "-i", str(mp4), "-af", "silencedetect=n=-35dB:d=0.4",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return list(zip(starts, ends))


def duration(mp4: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                 "csv=p=0", str(mp4)], capture_output=True, text=True).stdout)


def drop_seq(words: list[str], seq: list[str]) -> list[str]:
    for i in range(len(words) - len(seq) + 1):
        if words[i:i + len(seq)] == seq:
            return words[:i] + words[i + len(seq):]
    raise AssertionError(f"named line {seq} not in the kept words")


def has_seq(words: list[str], seq: list[str]) -> bool:
    return any(words[i:i + len(seq)] == seq for i in range(len(words) - len(seq) + 1))


def expected_words(analysis: dict, order: list[str], cut: bool) -> list[str]:
    takes = {t["media_id"]: t for t in analysis["takes"]}
    words = []
    for mid in order:
        a = takes[mid]["analysis"]
        removed = [(r["start_s"], r["end_s"]) for r in a["cut_plan"]["removed"]] if cut else []
        for w in a["timed_words"]:
            mid_s = (w["start_s"] + w["end_s"]) / 2
            if any(s <= mid_s < e for s, e in removed):
                continue
            if norm(w["text"]) in FILLERS:
                continue
            words.append(norm(w["text"]))
    return [w for w in words if w]


def caption_offsets(cues: list[dict], export_words) -> list[float]:
    cap = [(norm(w["text"]), float(w["start_s"])) for cue in cues for w in cue.get("words") or []]
    exp = [(norm(t), s) for t, s, _ in export_words]
    sm = difflib.SequenceMatcher(a=[c for c, _ in cap], b=[e for e, _ in exp], autojunk=False)
    offsets = []
    for block in sm.get_matching_blocks():
        for k in range(block.size):
            offsets.append(abs(cap[block.a + k][1] - exp[block.b + k][1]))
    return offsets


def main(out: Path) -> None:
    analysis = json.load(open(out / "analysis.json"))
    report = {}
    exports = {}
    for case, order in ORDER.items():
        directory = out / "cases" / case
        mp4 = directory / "phone.mp4"
        recipe = json.load(open(directory / "recipe.json"))
        variant = json.load(open(directory / "variant.json"))
        words = heard(mp4)
        exports[case] = words
        spoken = [(s, e) for _, s, e in words]
        first, last = spoken[0][0], spoken[-1][1]
        inside = [(round(s, 2), round(e, 2)) for s, e in silences(mp4) if s > first and e < last]
        want = expected_words(analysis, order, cut=case != "s3_uncut")
        if case in NAMED:
            want = drop_seq(want, NAMED[case])
        got = [norm(t) for t, _, _ in words if norm(t)]
        sm = difflib.SequenceMatcher(a=want, b=got, autojunk=False)
        missing = [want[i] for tag, i1, i2, _, _ in sm.get_opcodes() if tag in ("delete", "replace")
                   for i in range(i1, i2)]
        offsets = caption_offsets(variant["caption_cues"], words)
        recipe_clips = next(t for t in recipe["tracks"] if t["id"] == "subtitled")["clips"]
        # take joins: the first clip of every take after the first
        firsts, seen = [], set()
        for c in recipe_clips:
            if c["source_asset_id"] not in seen:
                seen.add(c["source_asset_id"])
                firsts.append(round(c["timeline_start"], 3))
        joins = firsts[1:]
        all_sil = [(s_, e_) for s_, e_ in silences(mp4)]
        join_gaps = [max([round(e_ - s_, 2) for s_, e_ in all_sil if s_ < j + 0.5 and e_ > j - 0.5] or [0.0])
                     for j in joins]
        joined = [norm(t) for t, _, _ in words if norm(t)]
        checks = {
            "slate_gone": not has_seq(joined, GONE_EVERYWHERE["wait the light okay"][:3])
                          and "okay" not in joined,
            "restart_gone": "sorry" not in joined and "again" not in joined,
        }
        if case in NAMED:
            checks["named_line_gone"] = not has_seq(joined, NAMED[case][:3]) and not has_seq(joined, NAMED[case][-3:])
            checks["control_lines_stay"] = {c: has_seq(joined, [norm(w) for w in c.split()]) for c in STAY[case]}
        report[case] = {
            "checks": checks if case != "s3_uncut" else None,
            "join_times_s": joins,
            "longest_silence_within_0.5s_of_join_s": join_gaps,
            "export_s": round(duration(mp4), 3),
            "recipe_s": json.load(open(out / "summary.json"))[case]["duration_s"],
            "silences_over_0.4s_inside_speech": inside,
            "expected_words": len(want),
            "heard_words": len(got),
            "missing_or_changed_words": missing,
            "caption_offset_median_s": round(statistics.median(offsets), 3),
            "caption_offset_p90_s": round(sorted(offsets)[int(0.9 * (len(offsets) - 1))], 3),
            "caption_words_matched": len(offsets),
        }
    # Negative control: a_last's captions (take A cut, played last) against the UNCUT export.
    a_last = json.load(open(out / "cases" / "s3_clean_a_last" / "variant.json"))["caption_cues"]
    wrong = caption_offsets(a_last, exports["s3_uncut"])
    report["control_a_last_captions_on_uncut_export"] = {
        "caption_offset_median_s": round(statistics.median(wrong), 3),
        "caption_offset_p90_s": round(sorted(wrong)[int(0.9 * (len(wrong) - 1))], 3),
    }
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
