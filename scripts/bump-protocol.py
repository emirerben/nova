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
  * each `--config-field` default in src/apps/api/app/config.py, plus the field named
    `<feature>_min_client_protocol` / `<feature>_client_protocol` when it exists

Re-running is safe: the feature keeps its constant and only the number moves. A config
field that still holds the feature's old number but is not named stops the run
(`--config-field` moves it, `--leave-config-field` says it belongs to another feature),
so a gate is never left behind; a field whose `<name>_PROTOCOL` constant belongs to another
feature is left alone. A constant or a config field (named or derived) already on origin/main
is refused (`--force` overrides): builds in the field depend on its number. Every config
field moved is printed with its old and new value.
Tests compare against `KriaClientProtocolContract.version` or the constant, never a
literal; `--check` (run by tests/test_client_protocol_single_source.py) fails when one does.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

IOS_FILE = "src/apps/ios/Kria/Core/Services.swift"
PY_FILE = "src/apps/api/app/services/client_protocol.py"
CONFIG_FILE = "src/apps/api/app/config.py"

IOS_VERSION_RE = re.compile(r"^(\s*static let version = )(\d+)\s*$", re.MULTILINE)
PY_CONSTANT_RE = re.compile(r"^([A-Z][A-Z0-9_]*_PROTOCOL) = (\d+)[ \t]*$", re.MULTILINE)
CONFIG_FIELD_RE = r"^([ \t]+{name}:[ \t]*int[ \t]*=[ \t]*)(\d+)([ \t]*)$"
CONFIG_ANY_PROTOCOL_RE = re.compile(
    r"^[ \t]+(\w*client_protocol):[ \t]*int[ \t]*=[ \t]*(\d+)[ \t]*$", re.MULTILINE
)

# `=999` kill-switch values are env overrides, not protocol numbers; defaults never reach it.
KILL_SWITCH_FLOOR = 900
ALLOW_LITERAL = "protocol-literal-ok"


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
        raise BumpError(
            f"expected exactly one `static let version = N` in {IOS_FILE}, found {len(matches)}"
        )
    return int(matches[0][1])


def check_hardcoded_literals(root: Path) -> list[str]:
    """Tests that hard-code the app's current protocol number, plus an app that trails the server."""
    current = ios_version(read(root, IOS_FILE))
    n = rf"{current}\b"
    problems: list[str] = []

    swift_patterns = [
        re.compile(rf"appProtocol:\s*({current}|{current + 1})\b"),
        re.compile(rf'requires_protocol"\s*:\s*({current}|{current + 1})\b'),
    ]
    py_patterns = [
        re.compile(rf"""requires_protocol["']?\s*[:=]\s*{n}"""),
        re.compile(rf"set_client_protocol\(\s*{n}"),
        re.compile(rf"\.default\s*==\s*{n}"),
    ]
    py_protocol_file_patterns = [re.compile(rf"\(\s*{current}\s*,\s*(True|False)\s*\)")]
    # On a line that talks about the protocol, a bare current number in code position
    # (after `=`, `:`, `(`, `,`, `[` or quoted) is a pin; prose like "protocol 50" is not.
    literal_in_code = re.compile(rf"""([=:(,\[]\s*|["']){current}(?![\w.])""")
    mentions_protocol = re.compile(r"protocol", re.IGNORECASE)
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
            text = path.read_text(encoding="utf-8")
            active = list(patterns)
            if glob == "*.py" and re.search(r"client_protocol|Client-Protocol", text):
                active += py_protocol_file_patterns
            hits = {text.count("\n", 0, m.start()) + 1 for p in active for m in p.finditer(text)}
            for number, line in enumerate(text.splitlines(), 1):
                if (
                    mentions_protocol.search(line)
                    and literal_in_code.search(line)
                    and ALLOW_LITERAL not in line
                    and "XCTAssertGreaterThanOrEqual" not in line
                ):
                    hits.add(number)
            for number in sorted(hits):
                problems.append(
                    f"{path.relative_to(root)}:{number}: hard-codes the current protocol {current}; "
                    "compare against KriaClientProtocolContract.version / the client_protocol constant "
                    f"(or add `{ALLOW_LITERAL}` to the line when the number is a fixture, not the app's)"
                )

    server_numbers = [int(n) for _, n in PY_CONSTANT_RE.findall(read(root, PY_FILE))]
    server_numbers += [
        int(n)
        for _, n in CONFIG_ANY_PROTOCOL_RE.findall(read(root, CONFIG_FILE))
        if int(n) < KILL_SWITCH_FLOOR
    ]
    server_max = max(server_numbers, default=0)
    if current < server_max:
        problems.append(
            f"{IOS_FILE}: KriaClientProtocolContract.version {current} is below the server's highest "
            f"protocol {server_max}; no app build could satisfy that gate. Re-run bump-protocol.py."
        )
    return problems


def _origin_main(root: Path, rel: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"origin/main:{rel}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def shipped_constants(root: Path) -> set[str]:
    """`*_PROTOCOL` names already on origin/main; empty when git or the ref is unavailable."""
    text = _origin_main(root, PY_FILE)
    return {name for name, _ in PY_CONSTANT_RE.findall(text)} if text else set()


def shipped_config_fields(root: Path) -> dict[str, int]:
    """`*client_protocol` config defaults already on origin/main, with their values."""
    text = _origin_main(root, CONFIG_FILE)
    if not text:
        return {}
    return {name: int(n) for name, n in CONFIG_ANY_PROTOCOL_RE.findall(text)}


def plan(
    root: Path,
    feature: str,
    config_fields: list[str],
    note: str | None,
    leave_fields: list[str] | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Return {relative path: new text} for every file that changes."""
    const = constant_name(feature)
    if not force and const in shipped_constants(root):
        raise BumpError(
            f"{const} is already on origin/main, so builds in the field depend on its number. "
            "Add a new constant for the new gate; pass --force only to renumber one that has not shipped."
        )
    ios_text = read(root, IOS_FILE)
    py_text = read(root, PY_FILE)
    cfg_text = read(root, CONFIG_FILE)

    ios_current = ios_version(ios_text)
    py_constants = {name: int(n) for name, n in PY_CONSTANT_RE.findall(py_text)}
    own = py_constants.get(const)

    pool = [n for name, n in py_constants.items() if name != const]
    if ios_current != own:
        pool.append(ios_current)

    stem = const[: -len("_PROTOCOL")].lower()
    derived = [f"{stem}_min_client_protocol", f"{stem}_client_protocol"]
    wanted_fields = list(
        dict.fromkeys(
            config_fields
            + [
                f
                for f in derived
                if re.search(CONFIG_FIELD_RE.format(name=f), cfg_text, re.MULTILINE)
            ]
        )
    )
    left = set(leave_fields or [])

    own_config: dict[str, re.Pattern[str]] = {}
    for field in wanted_fields:
        pattern = re.compile(CONFIG_FIELD_RE.format(name=re.escape(field)), re.MULTILINE)
        if len(pattern.findall(cfg_text)) != 1:
            raise BumpError(
                f"expected exactly one `{field}: int = N` in {CONFIG_FILE}; add the field first "
                "with any placeholder number"
            )
        own_config[field] = pattern

    if not force:
        shipped_fields = shipped_config_fields(root)
        moved_shipped = [f for f in own_config if f in shipped_fields]
        if moved_shipped:
            detail = ", ".join(f"{f} (shipped as {shipped_fields[f]})" for f in moved_shipped)
            raise BumpError(
                f"config field(s) {detail} are already on origin/main, so builds in the field depend on "
                "their numbers and moving them would lock those builds out of a shipped feature. Add a new "
                "field for the new gate; pass --force only to renumber one that has not shipped."
            )

    if own is not None:
        py_owner = {
            c[: -len("_PROTOCOL")].lower(): v for c, v in py_constants.items() if c != const
        }
        stranded = [
            m.group(1)
            for m in CONFIG_ANY_PROTOCOL_RE.finditer(cfg_text)
            if int(m.group(2)) == own
            and m.group(1) not in own_config
            and m.group(1) not in left
            and py_owner.get(re.sub(r"_(min_)?client_protocol$", "", m.group(1))) != own
        ]
        if stranded:
            raise BumpError(
                f"config field(s) {', '.join(stranded)} still hold {const}'s current number {own} but were not "
                "named. Moving the number without them leaves the gate admitting builds that cannot decode the "
                "feature. Pass --config-field <name> to move one with it, or --leave-config-field <name> "
                "when it belongs to a different feature."
            )

    pool += [
        int(m.group(2))
        for m in CONFIG_ANY_PROTOCOL_RE.finditer(cfg_text)
        if m.group(1) not in own_config and int(m.group(2)) < KILL_SWITCH_FLOOR
    ]
    new = max(pool, default=0) + 1

    changes: dict[str, str] = {}
    report: list[str] = []

    new_ios = IOS_VERSION_RE.sub(lambda m: f"{m.group(1)}{new}", ios_text, count=1)
    if new_ios != ios_text:
        changes[IOS_FILE] = new_ios

    if own is not None:
        new_py = re.sub(
            rf"^{const} = \d+[ \t]*$",
            f"{const} = {new}",
            py_text,
            count=1,
            flags=re.MULTILINE,
        )
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
    for field, pattern in own_config.items():
        moved = pattern.search(new_cfg)
        old_value = moved.group(2) if moved else "?"
        new_cfg = pattern.sub(lambda m: f"{m.group(1)}{new}{m.group(3)}", new_cfg, count=1)
        report.append(f"{field}: {old_value} -> {new}")
    if new_cfg != cfg_text:
        changes[CONFIG_FILE] = new_cfg

    changes["__report__"] = "\n".join(report)
    changes["__number__"] = str(new)
    changes["__constant__"] = const
    return changes


def write_atomically(root: Path, changes: dict[str, str]) -> None:
    staged: list[tuple[Path, Path, str]] = []
    try:
        for rel, text in changes.items():
            target = root / rel
            handle, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "feature",
        nargs="?",
        help="feature name, e.g. split-collapse -> SPLIT_COLLAPSE_PROTOCOL",
    )
    parser.add_argument(
        "--config-field",
        action="append",
        default=[],
        metavar="FIELD",
        help="config.py `*_min_client_protocol` field to set to the new number (repeatable)",
    )
    parser.add_argument(
        "--leave-config-field",
        action="append",
        default=[],
        metavar="FIELD",
        help="config.py field that shares this feature's current number but belongs to another feature",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="renumber a constant that is already on origin/main",
    )
    parser.add_argument("--note", help="one-line comment written above a newly added constant")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the number and files, write nothing",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if a test hard-codes the current protocol number",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            problems = check_hardcoded_literals(args.root)
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1 if problems else 0
        if not args.feature:
            parser.error("a feature name is required (or --check)")
        changes = plan(
            args.root,
            args.feature,
            args.config_field,
            args.note,
            args.leave_config_field,
            args.force,
        )
    except BumpError as error:
        print(f"bump-protocol: {error}", file=sys.stderr)
        return 1

    report = changes.pop("__report__")
    number = changes.pop("__number__")
    const = changes.pop("__constant__")
    if not changes:
        print(f"{const} = {number}: already assigned, nothing to change")
        return 0
    if not args.dry_run:
        write_atomically(args.root, changes)
    verb = "would set" if args.dry_run else "set"
    print(f"{verb} {const} = {number} in: {', '.join(sorted(changes))}")
    for line in report.splitlines():
        print(f"  config {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
