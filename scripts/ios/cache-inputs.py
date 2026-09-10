#!/usr/bin/env python3
"""Reuse timestamps only for byte-identical Xcode inputs restored with build data.

Fresh checkouts otherwise make every source newer than the cached object files.
The manifest lives inside the existing Build cache, and never supplies file paths:
we enumerate current inputs ourselves and verify their content before touching them.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

INPUT_ROOTS = (
    "src/apps/ios",
    "src/apps/web/public/fonts",
    "src/apps/web/public/plan/type-posters",
)


def inputs(root):
    raw = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *INPUT_ROOTS,
        ],
        cwd=root,
    )
    paths = {p.decode() for p in raw.split(b"\0") if p}
    project = root / "src/apps/ios/Kria.xcodeproj"
    paths.add("src/apps/ios/Kria.xcodeproj/project.pbxproj")
    paths.update(
        str(p.relative_to(root))
        for p in project.glob("xcshareddata/xcschemes/*.xcscheme")
    )
    for relative in sorted(paths):
        path = root / relative
        if (
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve())
        ):
            yield relative, path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(root):
    content = [(relative, digest(path)) for relative, path in inputs(root)]
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def save(root, manifest):
    state = {
        relative: {"sha256": digest(path), "mtime_ns": path.stat().st_mtime_ns}
        for relative, path in inputs(root)
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(state, sort_keys=True))
    return len(state)


def restore(root, manifest):
    try:
        state = json.loads(manifest.read_text())
        if not isinstance(state, dict):
            return 0
    except (OSError, ValueError):
        return 0
    restored = 0
    for relative, path in inputs(root):
        entry = state.get(relative)
        if not isinstance(entry, dict):
            continue
        timestamp = entry.get("mtime_ns")
        if type(timestamp) is not int or timestamp < 0 or timestamp > 2**63 - 1:
            continue
        if entry.get("sha256") != digest(path):
            continue
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, timestamp))
        restored += 1
    return restored


def main():
    root = Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"])
        .decode()
        .strip()
    )
    manifest = root / "src/apps/ios/.derived-data/Build/ci-input-times.json"
    mode = sys.argv[1]
    if mode == "fingerprint":
        print(fingerprint(root))
        return
    if mode not in ("save", "restore"):
        raise SystemExit("Usage: cache-inputs.py save|restore|fingerprint")
    count = (save if mode == "save" else restore)(root, manifest)
    message = f"Xcode input timestamps: {mode} {count} matching files"
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(message + "\n\n")


if __name__ == "__main__":
    main()
