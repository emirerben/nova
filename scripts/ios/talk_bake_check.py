#!/usr/bin/env python3
"""Reduces one pulled talk-bake robustness run (scripts/ios/talk-bake-robustness.sh) to <run-dir>/bake-check.json.

Reads, from the run directory:
  teleprompter/   the app's Library/Application Support/KriaTeleprompter (take manifests and movies)
  bench/          Documents/talk-bench (summary.json: talk.bake.run intervals)
  tmp-before.txt, tmp-after.txt   `devicectl device info files` listings of the app's tmp folder
  tmp-staging/    any cinematic-*.mov staging file left after the run

A movie is `baked` when it has exactly one video and one audio stream (the baker writes nothing else) and `raw` when the
camera's extra tracks (depth, disparity, metadata) are still there. `decodes` is a full ffmpeg decode to the null muxer.
"""
import json
import re
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(movie: Path) -> dict:
    result = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(movie)])
    if result.returncode != 0:
        return {"file": movie.name, "size_bytes": movie.stat().st_size, "probe_error": result.stderr.strip()[:300], "decodes": False}
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    kinds = [s.get("codec_type") for s in streams]
    decode = run(["ffmpeg", "-v", "error", "-i", str(movie), "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = data.get("format", {})
    only_av = sorted(k for k in kinds if k) == ["audio", "video"]
    return {
        "file": movie.name,
        "size_bytes": movie.stat().st_size,
        "stream_kinds": kinds,
        "kind": "baked" if only_av else "raw",
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "bit_rate_mbps": round(int(fmt["bit_rate"]) / 1e6, 2) if fmt.get("bit_rate") else None,
        "video_codec": video.get("codec_name"),
        "size": f'{video.get("width")}x{video.get("height")}',
        "decodes": decode.returncode == 0 and not decode.stderr.strip(),
        "decode_stderr": decode.stderr.strip()[:300],
    }


def manifests(run_dir: Path) -> list[dict]:
    found = []
    for path in sorted((run_dir / "teleprompter").rglob("takes/*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            found.append({"manifest": path.name, "unreadable": str(error)})
            continue
        movie = path.with_name(data.get("fileName") or f"{path.stem}.mov")
        entry = {
            "manifest": path.name,
            "id": data.get("id"),
            "status": data.get("status"),
            "slot": data.get("slot"),
            "cinematic": data.get("cinematic", "MISSING"),
            "fileName": data.get("fileName"),
            "movie_exists": movie.exists(),
        }
        if movie.exists():
            entry["movie"] = probe(movie)
        found.append(entry)
    for movie in sorted((run_dir / "teleprompter").rglob("takes/*.mov")):
        if not movie.with_suffix(".json").exists():
            found.append({"orphan_movie": movie.name, "movie": probe(movie)})
    return found


def listing(path: Path) -> dict[str, str]:
    """name -> size text for every cinematic-*.mov row of a devicectl file listing."""
    rows = {}
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        match = re.match(r"^(\S*cinematic-[0-9A-Fa-f-]+\.mov)\s+.*?(\d+(?:\.\d+)?\s*(?:bytes|KB|MB|GB))\s", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def bake_intervals(run_dir: Path) -> list[dict]:
    try:
        summary = json.loads((run_dir / "bench" / "summary.json").read_text())
    except (OSError, ValueError):
        return []
    return [{"t_ms": round(t), "duration_ms": round(d), "detail": detail}
            for t, d, detail in summary.get("intervals", {}).get("talk.bake.run", [])]


def main() -> int:
    run_dir = Path(sys.argv[1])
    before, after = listing(run_dir / "tmp-before.txt"), listing(run_dir / "tmp-after.txt")
    staging = [probe(movie) for movie in sorted((run_dir / "tmp-staging").glob("cinematic-*.mov"))]
    report = {
        "takes": manifests(run_dir),
        "bake_intervals": bake_intervals(run_dir),
        "tmp_staging_before": before,
        "tmp_staging_after": after,
        "tmp_staging_new": {name: size for name, size in after.items() if name not in before},
        "tmp_staging_probe": staging,
    }
    (run_dir / "bake-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
