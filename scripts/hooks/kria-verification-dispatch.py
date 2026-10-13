#!/usr/bin/env python3
"""Installed Git hook dispatcher; preserve prior hooks and enqueue local proof."""

import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    event, *args = sys.argv[1:]
    if os.environ.get("KRIA_VERIFY_AUTO_SNAPSHOT") == "1":
        return 0
    config = json.loads(Path(__file__).with_name("previous.json").read_text())
    payload = sys.stdin.buffer.read() if event in {"pre-push", "post-rewrite"} else None
    previous = Path(config["path"]) / event
    if previous.is_file() and os.access(previous, os.X_OK):
        result = subprocess.run([str(previous), *args], input=payload)
        if result.returncode:
            return result.returncode
    if event not in {"post-merge", "post-rewrite", "pre-push"}:
        return 0
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        script = Path(root) / "scripts/ios/auto-verify.py"
        if script.exists():
            selections = []
            if event == "pre-push" and payload:
                for line in payload.decode().splitlines():
                    ref, head, _, base = line.split()
                    if ref.startswith("refs/heads/") and set(head) != {"0"}:
                        selections.append(
                            ["--head", head]
                            + (["--base", base] if set(base) != {"0"} else [])
                        )
            else:
                selections.append([])
            for selection in selections:
                subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "enqueue",
                        "--reason",
                        event,
                        *selection,
                    ],
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"[Kria verification] hook could not enqueue: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
