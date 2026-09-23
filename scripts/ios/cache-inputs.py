#!/usr/bin/env python3
"""Reuse timestamps only for byte-identical Xcode inputs restored with build data.

Fresh checkouts otherwise make every source newer than the cached object files.
The manifest lives inside the existing Build cache, and never supplies file paths:
we enumerate current inputs ourselves and verify their content before touching them.

Directories matter too: Xcode fingerprints folder inputs such as asset catalogs by
their directory tree, so a checkout-fresh `.xcassets` directory mtime reran actool,
regenerated asset symbols and recompiled/relinked the app on every warm CI build.
A directory's mtime only reflects its entry names, so it is restored only when the
current listing is identical to the recorded one.
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


def directories(root):
    """Every directory inside an input root that contains an input, plus the roots."""
    found = set()
    for relative, _ in inputs(root):
        parent = Path(relative).parent
        while any(parent == Path(r) or Path(r) in parent.parents for r in INPUT_ROOTS):
            found.add(parent)
            parent = parent.parent
    for relative in sorted(found):
        path = root / relative
        if (
            path.is_dir()
            and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve())
        ):
            # Trailing slash keeps directory entries distinct from file entries.
            yield f"{relative}/", path


def listing(path):
    return hashlib.sha256(json.dumps(sorted(os.listdir(path))).encode()).hexdigest()


def fingerprint(root):
    content = [(relative, digest(path)) for relative, path in inputs(root)]
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def save(root, manifest):
    """Record input files and directories; returns (files, directories)."""
    # Create the manifest directory first so its entry is part of the listing.
    manifest.parent.mkdir(parents=True, exist_ok=True)
    files = {
        relative: {"sha256": digest(path), "mtime_ns": path.stat().st_mtime_ns}
        for relative, path in inputs(root)
    }
    folders = {
        relative: {"entries": listing(path), "mtime_ns": path.stat().st_mtime_ns}
        for relative, path in directories(root)
    }
    manifest.write_text(json.dumps({**files, **folders}, sort_keys=True))
    return len(files), len(folders)


def _restorable(entry, key, value):
    if not isinstance(entry, dict):
        return None
    timestamp = entry.get("mtime_ns")
    if type(timestamp) is not int or timestamp < 0 or timestamp > 2**63 - 1:
        return None
    return timestamp if entry.get(key) == value() else None


def restore(root, manifest):
    """Restore recorded times for unchanged inputs; returns (files, directories)."""
    try:
        state = json.loads(manifest.read_text())
        if not isinstance(state, dict):
            return 0, 0
    except (OSError, ValueError):
        return 0, 0
    counts = []
    # Files first: touching a file never changes its parent directory's mtime.
    for candidates, key, value in (
        (inputs(root), "sha256", digest),
        (directories(root), "entries", listing),
    ):
        restored = 0
        for relative, path in candidates:
            timestamp = _restorable(state.get(relative), key, lambda: value(path))
            if timestamp is None:
                continue
            os.utime(path, ns=(path.stat().st_atime_ns, timestamp))
            restored += 1
        counts.append(restored)
    return tuple(counts)


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
    files, folders = (save if mode == "save" else restore)(root, manifest)
    message = (
        f"Xcode input timestamps: {mode} {files} matching files, "
        f"{folders} unchanged directories"
    )
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(message + "\n\n")


if __name__ == "__main__":
    main()
