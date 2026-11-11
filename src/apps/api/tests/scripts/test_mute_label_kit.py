"""KRI-1252 mute label kit: the prod read must run unattended and pick distinct footage."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[5] / "scripts" / "prod" / "mute_label_kit.py"

FAKE_FLY = r"""#!/usr/bin/env python3
import base64, json, sys
args = sys.argv[1:]
if "-s" in args or "--select" in args:
    sys.stderr.write("Error: prompt requires an interactive terminal\n")
    sys.exit(1)
if args[:2] != ["ssh", "console"] or "-g" not in args:
    sys.stderr.write("Error: unexpected fly invocation\n")
    sys.exit(2)
cmd = args[args.index("-C") + 1]
blob = cmd.split("echo ", 1)[1].split(" |", 1)[0]
compile(base64.b64decode(blob).decode(), "remote", "exec")
payload = json.load(open(sys.argv[0] + ".payload"))
print("noise before")
print("<<<KIT_JSON")
print(json.dumps(payload))
print("KIT_JSON>>>")
"""


def _load():
    spec = importlib.util.spec_from_file_location("mute_label_kit", KIT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mute_label_kit"] = mod
    spec.loader.exec_module(mod)
    return mod


def _entry(i, media, path, user, group="x"):
    return {"item_id": f"i{i}", "media_id": media, "gcs_path": path, "user_id": user}


def test_choose_never_repeats_footage_and_caps_each_creator():
    kit = _load()
    hits = [_entry(i, f"m{i % 3}", f"p{i % 3}", "u1") for i in range(12)]
    hits += [_entry(20 + i, f"h{i}", f"hp{i}", "u2") for i in range(10)]
    pool = [_entry(40, "m0", "pX", "u3")]
    pool += [_entry(41, "mY", "p1", "u3")]
    pool += [_entry(50 + i, f"s{i}", f"sp{i}", "u4") for i in range(10)]
    chosen = kit.choose(hits, pool, 8, 6, 2, 7)
    media = [c["media_id"] for c in chosen]
    paths = [c["gcs_path"] for c in chosen]
    assert len(media) == len(set(media))
    assert len(paths) == len(set(paths))
    for user in {"u1", "u2", "u3", "u4"}:
        assert sum(1 for c in chosen if c["user_id"] == user) <= 2
    assert {c["group"] for c in chosen} == {"detector_hit", "random_sample"}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_pull_reads_prod_without_an_interactive_machine_prompt(tmp_path):
    src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(src)], check=True
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fly = bin_dir / "fly"
    fly.write_text(FAKE_FLY)
    fly.chmod(0o755)
    clips = [
        dict(_entry(i, f"m{i}", f"p{i}", "u1"), group="detector_hit", url=src.as_uri(), signal={})
        for i in range(2)
    ]
    Path(str(fly) + ".payload").write_text(
        json.dumps({"scanned_hits": 2, "scanned_pool": 0, "clips": clips})
    )
    out = tmp_path / "kit"
    proc = subprocess.run(
        [sys.executable, str(KIT), "pull", "--out", str(out)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(json.loads((out / "labels.json").read_text())) == ["clip_001.m4a", "clip_002.m4a"]
    assert (out / "clip_001.m4a").stat().st_size > 0
