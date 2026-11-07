#!/usr/bin/env python3
"""Build the real-clip labeling kit for the mute-clip card (KRI-1252).

    pull   pick ~40 prod clips (detector hits + a random sample), download them, write blind audio
           files, a labels.json template and a README into the kit folder
    check  validate the labels you filled in and print the calibration command
    remote print the read-only snippet `pull` runs on a prod machine (to review it first)

    python3 scripts/prod/mute_label_kit.py pull                     # fly ssh -> select -> download
    python3 scripts/prod/mute_label_kit.py pull --selection s.json  # skip the prod read
    python3 scripts/prod/mute_label_kit.py check

The prod read is a SELECT over `plan_items.clip_assignments` plus one short-lived signed GET URL per
chosen clip; it writes nothing. URLs and the machine's output are never printed: they live in
memory for the length of the download. The kit folder (default ~/nova-mute-labels) is outside git.
Files are named clip_001.m4a... in shuffled order and the detector's verdicts go to a hidden
`.detector.json`, so labeling is blind.
"""

from __future__ import annotations

import argparse
import base64
import inspect
import json
import random
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KIT = Path.home() / "nova-mute-labels"
FLY_APP = "nova-video"
MARK_START, MARK_END = "<<<KIT_JSON", "KIT_JSON>>>"
LABELS = ("clean", "wind", "crowd", "music", "clipped", "silence")
TEMPLATE = ""
MAX_BYTES = 150 * 1024 * 1024


def choose(hits, pool, n_hits, n_sample, per_user, seed):
    """Pick distinct footage: no media_id or gcs_path twice, at most per_user clips per creator."""
    import random

    rng = random.Random(seed)
    rng.shuffle(hits)
    rng.shuffle(pool)
    seen_media, seen_path, per = set(), set(), {}
    chosen = []
    for group, entries, limit in (
        ("detector_hit", hits, n_hits),
        ("random_sample", pool, n_sample),
    ):
        taken = 0
        for e in entries:
            if taken >= limit:
                break
            owner = e.get("user_id")
            if e["media_id"] in seen_media or e["gcs_path"] in seen_path:
                continue
            if per.get(owner, 0) >= per_user:
                continue
            seen_media.add(e["media_id"])
            seen_path.add(e["gcs_path"])
            per[owner] = per.get(owner, 0) + 1
            chosen.append(dict(e, group=group))
            taken += 1
    return chosen


REMOTE = r"""
import json, sys
from sqlalchemy import select
from app import storage
from app.database import sync_session
from app.models import ContentPlan, PlanItem
from app.services.clip_signals import registry

{choose_src}

HITS, SAMPLE, SCAN, SEED, PER_USER = {hits}, {sample}, {scan}, {seed}, {per_user}
hits, pool = [], []
with sync_session() as db:
    rows = db.execute(
        select(PlanItem.id, ContentPlan.user_id, PlanItem.clip_assignments)
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .order_by(PlanItem.id.desc())
        .limit(SCAN)
    )
    for item_id, user_id, assignments in rows:
        for row in assignments or []:
            if not isinstance(row, dict) or str(row.get("kind") or "video") != "video":
                continue
            path, media_id = row.get("gcs_path"), row.get("media_id")
            generation = str(row.get("storage_generation") or row.get("generation") or "")
            if not path or not media_id or not generation:
                continue
            data = registry.read_signals(row.get("analysis")).get("audio_quality") or {{}}
            entry = {{
                "item_id": str(item_id), "user_id": str(user_id),
                "media_id": str(media_id), "gcs_path": path,
                "generation": generation, "signal": data,
            }}
            measured = data.get("measured") is not False
            if measured and data.get("noisy") is True and not data.get("silent"):
                hits.append(entry)
            elif measured and data.get("noisy") is False:
                pool.append(entry)
chosen = choose(hits, pool, HITS, SAMPLE, PER_USER, SEED)
out = []
for e in chosen:
    try:
        meta = storage.object_metadata(e["gcs_path"])
        if meta.size and meta.size > {max_bytes} or str(meta.generation) != e["generation"]:
            continue
        e["url"] = storage.signed_get_url_for_generation(
            e["gcs_path"], generation=e["generation"], expiration_minutes=30
        )
        e["bytes"] = int(meta.size or 0)
        out.append(e)
    except Exception:
        continue
print("{start}")
print(json.dumps({{"scanned_hits": len(hits), "scanned_pool": len(pool), "clips": out}}))
print("{end}")
"""


def remote_snippet(hits: int, sample: int, scan: int, seed: int, per_user: int) -> str:
    return REMOTE.format(
        hits=hits,
        sample=sample,
        scan=scan,
        seed=seed,
        per_user=per_user,
        choose_src=inspect.getsource(choose),
        max_bytes=MAX_BYTES,
        start=MARK_START,
        end=MARK_END,
    )


def select_remote(args: argparse.Namespace) -> dict:
    code = remote_snippet(args.hits, args.sample, args.scan, args.seed, args.per_user)
    blob = base64.b64encode(code.encode()).decode()
    cmd = [
        "fly",
        "ssh",
        "console",
        "-a",
        FLY_APP,
        "-g",
        "worker",
        "-C",
        f"sh -c 'echo {blob} | base64 -d | python -'",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    text = proc.stdout
    if MARK_START not in text or MARK_END not in text:
        tail = "\n".join((proc.stderr or text).strip().splitlines()[-6:])
        sys.exit(f"prod read failed (exit {proc.returncode}); last lines:\n{tail}")
    body = text.split(MARK_START, 1)[1].split(MARK_END, 1)[0]
    return json.loads(body)


def extract_audio(src: Path, dst: Path) -> bool:
    """Audio-only AAC like the analysis proxy (mono, 44.1 kHz): what the detector listens to."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(dst),
        ],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def fetch(url: str, dst: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp, dst.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)


README = """# Mute-card labels (KRI-1252)

{n} clips from real prod footage, audio only. Your job: decide for each one what its sound is, so
we know how often the "this clip is noisy, mute it?" card is right. About 25 minutes.

## How to label

1. Open `labels.json` in any editor. Each line is `"clip_001.m4a": ""`.
2. Play a clip (double-click, it opens in QuickTime) and type one word between the quotes:

   | word | when |
   | --- | --- |
   | `wind` | wind rumble or buffeting is the main sound; you would want it muted |
   | `crowd` | crowd, chatter, traffic or a busy room is the main sound; you would want it muted |
   | `clean` | fine to keep: speech, a quiet room, natural ambience, a little noise behind a voice |
   | `music` | loud music or bass is the main sound (not wind or crowd) |
   | `clipped` | harsh distortion from being too loud |
   | `silence` | nothing audible |

3. If unsure between `clean` and `wind`/`crowd`, ask: "would this clip sound better to a viewer with
   its own audio removed?" Yes means `wind` or `crowd`. No means `clean`.
4. Do not look at `.detector.json` until you are done (it says what the detector thought; it would
   bias you).

## When you are done

    python3 scripts/prod/mute_label_kit.py check

It tells you if a clip is unlabeled or a word is misspelled, and prints the next command
(`clip_signal_calibration.py report --real-dir ~/nova-mute-labels`). If precision on `wind`
+ `crowd` is 0.9 or better, the mute card can be turned on. If not, we refit the thresholds on
your labels first.

These clips are real users' footage. They stay in this folder, outside git. Delete the folder when
the calibration is finished.
"""


def pull(args: argparse.Namespace) -> int:
    kit = Path(args.out).expanduser()
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is required")
    if (kit / "labels.json").exists() and not args.force:
        sys.exit(f"{kit}/labels.json exists (labels may be filled in); use --force to rebuild")
    if args.selection:
        selected = json.loads(Path(args.selection).read_text())
    else:
        selected = select_remote(args)
    clips = list(selected["clips"])
    random.Random(args.seed + 1).shuffle(clips)
    kit.mkdir(parents=True, exist_ok=True)
    labels: dict[str, str] = {}
    detector: dict[str, dict] = {}
    skipped = 0
    with tempfile.TemporaryDirectory(prefix="mute-kit-") as tmp:
        for entry in clips:
            n = len(labels) + 1
            name = f"clip_{n:03d}.m4a"
            raw = Path(tmp) / f"{n}.bin"
            try:
                fetch(entry["url"], raw)
            except Exception as exc:  # noqa: BLE001 - one dead URL never stops the kit
                skipped += 1
                print(f"skip a clip: download failed ({type(exc).__name__})", file=sys.stderr)
                continue
            if not extract_audio(raw, kit / name):
                skipped += 1
                print("skip a clip: no audio stream", file=sys.stderr)
                continue
            labels[name] = TEMPLATE
            detector[name] = {k: entry.get(k) for k in ("group", "signal", "item_id", "media_id")}
    (kit / "labels.json").write_text(json.dumps(labels, indent=2) + "\n")
    (kit / ".detector.json").write_text(json.dumps(detector, indent=2) + "\n")
    (kit / "README.md").write_text(README.format(n=len(labels)))
    hits = sum(1 for d in detector.values() if d["group"] == "detector_hit")
    print(f"kit: {kit}  clips={len(labels)} detector_hits={hits} skipped={skipped}")
    print(
        f"prod had {selected.get('scanned_hits')} measured-noisy and "
        f"{selected.get('scanned_pool')} measured-clean clips in the scan"
    )
    return 0


def check(args: argparse.Namespace) -> int:
    kit = Path(args.out).expanduser()
    labels = json.loads((kit / "labels.json").read_text())
    todo = [k for k, v in labels.items() if v == TEMPLATE]
    bad = {k: v for k, v in labels.items() if v != TEMPLATE and v not in LABELS}
    missing = [k for k in labels if not (kit / k).exists()]
    counts = {name: sum(1 for v in labels.values() if v == name) for name in LABELS}
    print("counts:", counts, f"unlabeled={len(todo)}")
    if todo or bad or missing:
        if todo:
            print("unlabeled:", ", ".join(todo[:10]), "..." if len(todo) > 10 else "")
        if bad:
            print("not one of", "/".join(LABELS), "->", bad)
        if missing:
            print("missing files:", missing)
        return 1
    if counts["wind"] + counts["crowd"] == 0:
        print("no wind or crowd clips: precision is undefined, label a few or pull more")
        return 1
    print(
        f"ready. run:\n  cd {ROOT} && src/apps/api/.venv/bin/python "
        f"scripts/clip_signal_calibration.py report --real-dir {kit}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("pull", "check", "remote"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=str(DEFAULT_KIT))
        p.add_argument("--seed", type=int, default=1252)
        if name in ("pull", "remote"):
            p.add_argument("--hits", type=int, default=20)
            p.add_argument("--sample", type=int, default=20)
            p.add_argument(
                "--scan", type=int, default=4000, help="newest plan items to look through"
            )
            p.add_argument("--per-user", type=int, default=4, help="max clips from one creator")
        if name == "pull":
            p.add_argument("--selection", help="a saved selection JSON: skip the prod read")
            p.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.cmd == "remote":
        print(remote_snippet(args.hits, args.sample, args.scan, args.seed, args.per_user))
        return 0
    return pull(args) if args.cmd == "pull" else check(args)


if __name__ == "__main__":
    raise SystemExit(main())
