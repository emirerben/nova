#!/usr/bin/env python3
"""Assign the next client protocol number and write it everywhere it lives (KRI-1381).

Every feature that needs a new app build takes the next protocol number. Picking it
by hand inside the PR makes whichever PR merges second conflict, so run this when
you rebase onto main, after main's numbers are in your tree:

    python3 scripts/bump-protocol.py split-collapse
    python3 scripts/bump-protocol.py split-collapse --config-field split_collapse_min_client_protocol

It sets the number to max(every number in the tree other than this feature's own) + 1
and rewrites, in one step:

  * `KriaClientProtocolContract.version` in src/apps/ios/Kria/Core/Services.swift
  * `<FEATURE>_PROTOCOL` in src/apps/api/app/services/client_protocol.py (added when new)
  * each `--config-field` default in src/apps/api/app/config.py

Re-running is safe: the feature keeps its constant and only the number moves.
Tests compare against `KriaClientProtocolContract.version` or the constant, never a
literal; `--check` (run by tests/test_client_protocol_single_source.py) fails when one does.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

IOS_FILE = "src/apps/ios/Kria/Core/Services.swift"
PY_FILE = "src/apps/api/app/services/client_protocol.py"
CONFIG_FILE = "src/apps/api/app/config.py"

IOS_VERSION_RE = re.compile(r"^(\s*static let version = )(\d+)\s*$", re.MULTILINE)
PY_CONSTANT_RE = re.compile(r"^([A-Z][A-Z0-9_]*_PROTOCOL) = (\d+)[ \t]*$", re.MULTILINE)
CONFIG_FIELD_RE = r"^([ \t]+{name}:[ \t]*int[ \t]*=[ \t]*)(\d+)([ \t]*)$"
CONFIG_ANY_PROTOCOL_RE = re.compile(r"^[ \t]+(\w*client_protocol):[ \t]*int[ \t]*=[ \t]*(\d+)[ \t]*$", re.MULTILINE)

# `=999` kill-switch values are env overrides, not protocol numbers; defaults never reach it.
KILL_SWITCH_FLOOR = 900


class BumpError(Exception):
    pass


def constant_name(feature: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", feature.strip()).strip("_").upper()
    if name.endswith("_PROTOCOL"):
        name = name[: -len("_PROTOCOL")]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise BumpError(f"feature name {feature!r} does not make a constant name")
    return f"{name}_PROTOCOL"


def read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        raise BumpError(f"{rel} not found under {root}")
    return path.read_text(encoding="utf-8")


def ios_version(text: str) -> int:
    matches = IOS_VERSION_RE.findall(text)
    if len(matches) != 1:
        raise BumpError(f"expected exactly one `static let version = N` in {IOS_FILE}, found {len(matches)}")
    return int(matches[0][1])


def check_hardcoded_literals(root: Path) -> list[str]:
    """Tests that hard-code the app's current protocol number, plus an app that trails the server."""
    current = ios_version(read(root, IOS_FILE))
    problems: list[str] = []

    swift_patterns = [
        re.compile(rf'Client-Protocol[^\n]*"{current}"'),
        re.compile(rf'protocolHeader[^\n]*"{current}"'),
        re.compile(rf"KriaClientProtocolContract\.version\s*,\s*{current}\b"),
        re.compile(rf"appProtocol:\s*{current}\b"),
        re.compile(rf'requires_protocol"\s*:\s*({current}|{current + 1})\b'),
    ]
    py_patterns = [
        re.compile(rf"""Client-Protocol["']\s*:\s*["']?{current}\b"""),
        re.compile(rf"""Client-Protocol["'][^\n]*["']{current}["']"""),
    ]
    scans = [
        (root / "src/apps/ios", "*.swift", swift_patterns, lambda p: "Tests" in p.parts),
        (root / "src/apps/api/tests", "*.py", py_patterns, lambda p: True),
    ]
    for base, glob, patterns, wanted in scans:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(glob)):
            if not wanted(path.relative_to(root)):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(p.search(line) for p in patterns):
                    problems.append(
                        f"{path.relative_to(root)}:{number}: hard-codes the current protocol {current}; "
                        "compare against KriaClientProtocolContract.version / the client_protocol constant"
                    )

    server_max = max((int(n) for _, n in PY_CONSTANT_RE.findall(read(root, PY_FILE))), default=0)
    if current < server_max:
        problems.append(
            f"{IOS_FILE}: KriaClientProtocolContract.version {current} is below the server's highest "
            f"protocol {server_max}; no app build could satisfy that gate. Re-run bump-protocol.py."
        )
    return problems


def plan(root: Path, feature: str, config_fields: list[str], note: str | None) -> dict[str, str]:
    """Return {relative path: new text} for every file that changes."""
    const = constant_name(feature)
    ios_text = read(root, IOS_FILE)
    py_text = read(root, PY_FILE)
    cfg_text = read(root, CONFIG_FILE)

    ios_current = ios_version(ios_text)
    py_constants = {name: int(n) for name, n in PY_CONSTANT_RE.findall(py_text)}
    own = py_constants.get(const)

    pool = [n for name, n in py_constants.items() if name != const]
    if ios_current != own:
        pool.append(ios_current)

    own_config: dict[str, re.Pattern[str]] = {}
    for field in config_fields:
        pattern = re.compile(CONFIG_FIELD_RE.format(name=re.escape(field)), re.MULTILINE)
        if len(pattern.findall(cfg_text)) != 1:
            raise BumpError(
                f"expected exactly one `{field}: int = N` in {CONFIG_FILE}; add the field first "
                "with any placeholder number"
            )
        own_config[field] = pattern
    pool += [
        int(m.group(2))
        for m in CONFIG_ANY_PROTOCOL_RE.finditer(cfg_text)
        if m.group(1) not in own_config and int(m.group(2)) < KILL_SWITCH_FLOOR
    ]
    new = max(pool, default=0) + 1

    changes: dict[str, str] = {}

    new_ios = IOS_VERSION_RE.sub(lambda m: f"{m.group(1)}{new}", ios_text, count=1)
    if new_ios != ios_text:
        changes[IOS_FILE] = new_ios

    if own is not None:
        new_py = re.sub(rf"^{const} = \d+[ \t]*$", f"{const} = {new}", py_text, count=1, flags=re.MULTILINE)
    else:
        last = list(PY_CONSTANT_RE.finditer(py_text))
        if not last:
            raise BumpError(f"no `*_PROTOCOL = N` constant in {PY_FILE} to insert after")
        comment = f"# {note}\n" if note else ""
        insert_at = last[-1].end()
        new_py = f"{py_text[:insert_at]}\n\n{comment}{const} = {new}{py_text[insert_at:]}"
    if new_py != py_text:
        changes[PY_FILE] = new_py

    new_cfg = cfg_text
    for pattern in own_config.values():
        new_cfg = pattern.sub(lambda m: f"{m.group(1)}{new}{m.group(3)}", new_cfg, count=1)
    if new_cfg != cfg_text:
        changes[CONFIG_FILE] = new_cfg

    changes["__number__"] = str(new)
    changes["__constant__"] = const
    return changes


def write_atomically(root: Path, changes: dict[str, str]) -> None:
    staged: list[tuple[Path, Path, str]] = []
    try:
        for rel, text in changes.items():
            target = root / rel
            handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                tmp.write(text)
            os.chmod(tmp_name, target.stat().st_mode & 0o7777)
            staged.append((target, Path(tmp_name), target.read_text(encoding="utf-8")))
    except BaseException:
        for _, tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)
        raise

    replaced: list[tuple[Path, str]] = []
    try:
        for target, tmp_path, original in staged:
            os.replace(tmp_path, target)
            replaced.append((target, original))
    except BaseException:
        for target, original in replaced:
            target.write_text(original, encoding="utf-8")
        for _, tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("feature", nargs="?", help="feature name, e.g. split-collapse -> SPLIT_COLLAPSE_PROTOCOL")
    parser.add_argument(
        "--config-field",
        action="append",
        default=[],
        metavar="FIELD",
        help="config.py `*_min_client_protocol` field to set to the new number (repeatable)",
    )
    parser.add_argument("--note", help="one-line comment written above a newly added constant")
    parser.add_argument("--dry-run", action="store_true", help="print the number and files, write nothing")
    parser.add_argument("--check", action="store_true", help="fail if a test hard-codes the current protocol number")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.check:
            problems = check_hardcoded_literals(args.root)
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1 if problems else 0
        if not args.feature:
            parser.error("a feature name is required (or --check)")
        changes = plan(args.root, args.feature, args.config_field, args.note)
    except BumpError as error:
        print(f"bump-protocol: {error}", file=sys.stderr)
        return 1

    number = changes.pop("__number__")
    const = changes.pop("__constant__")
    if not changes:
        print(f"{const} = {number}: already assigned, nothing to change")
        return 0
    if not args.dry_run:
        write_atomically(args.root, changes)
    verb = "would set" if args.dry_run else "set"
    print(f"{verb} {const} = {number} in: {', '.join(sorted(changes))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
