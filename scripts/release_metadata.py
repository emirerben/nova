#!/usr/bin/env python3
"""Own Nova's post-merge release metadata without parallel PR collisions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


ROOT_FILES = ("VERSION", "CHANGELOG.md", "package.json", "package-lock.json")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")
CHANGELOG_HEADING_RE = re.compile(
    r"^## \[(\d+\.\d+\.\d+\.\d+)\] - (.+)$", re.MULTILINE
)
RELEASE_MARKER_RE = re.compile(r"<!-- release-pr: (\d+) -->")

VERSION_LABELS = {"release:major": "major", "release:minor": "minor"}
CATEGORY_LABELS = {
    "release:added": "Added",
    "release:fixed": "Fixed",
    "release:changed": "Changed",
    "release:deprecated": "Deprecated",
    "release:removed": "Removed",
    "release:security": "Security",
}


class ReleaseMetadataError(ValueError):
    """A release owner input is malformed or unsafe to publish."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    revision: int

    @classmethod
    def parse(cls, raw: str) -> "Version":
        match = VERSION_RE.fullmatch(raw.strip())
        if not match:
            raise ReleaseMetadataError(
                f"VERSION must be four non-negative integers, got {raw!r}"
            )
        return cls(*(int(part) for part in match.groups()))

    def bump(self, kind: str) -> "Version":
        if kind == "major":
            return Version(self.major + 1, 0, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0, 0)
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1, 0)
        raise ReleaseMetadataError(f"unsupported release bump {kind!r}")

    def package(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}.{self.revision}"


@dataclass(frozen=True)
class ChangelogSection:
    version: Version
    date: str
    body: str


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseMetadataError(f"could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseMetadataError(f"{path} must contain a JSON object")
    return value


def _parse_changelog(content: str) -> tuple[str, list[ChangelogSection]]:
    matches = list(CHANGELOG_HEADING_RE.finditer(content))
    if not matches:
        raise ReleaseMetadataError("CHANGELOG.md has no four-part release headings")
    prefix = content[: matches[0].start()].rstrip() + "\n\n"
    sections: list[ChangelogSection] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end() : end].strip()
        sections.append(
            ChangelogSection(Version.parse(match.group(1)), match.group(2).strip(), body)
        )
    return prefix, sections


def normalize_changelog(content: str) -> str:
    """Collapse duplicate headings and put releases in descending version order."""
    prefix, parsed = _parse_changelog(content)
    grouped: dict[Version, ChangelogSection] = {}
    for section in parsed:
        existing = grouped.get(section.version)
        if existing is None:
            grouped[section.version] = section
        else:
            body = "\n\n".join(part for part in (existing.body, section.body) if part)
            grouped[section.version] = ChangelogSection(
                section.version, existing.date, body
            )
    rendered = []
    for section in sorted(grouped.values(), key=lambda value: value.version, reverse=True):
        rendered.append(f"## [{section.version}] - {section.date}\n\n{section.body}".rstrip())
    return prefix + "\n\n".join(rendered) + "\n"


def release_labels(labels: Iterable[str]) -> tuple[str, str]:
    labels = set(labels)
    bump_matches = labels & VERSION_LABELS.keys()
    category_matches = labels & CATEGORY_LABELS.keys()
    # Ambiguous labels deliberately fail closed to the harmless default selected
    # for KRI-31: a patch-level Changed entry.
    bump = VERSION_LABELS[next(iter(bump_matches))] if len(bump_matches) == 1 else "patch"
    category = (
        CATEGORY_LABELS[next(iter(category_matches))]
        if len(category_matches) == 1
        else "Changed"
    )
    return bump, category


def _release_marker(pr_number: int) -> str:
    return f"<!-- release-pr: {pr_number} -->"


def prepare_release(repo: Path, pr_number: int, title: str, labels: Iterable[str], date: str) -> Version | None:
    """Write one atomic release metadata update, or return None if already done."""
    changelog_path = repo / "CHANGELOG.md"
    current_changelog = changelog_path.read_text()
    if _release_marker(pr_number) in current_changelog:
        return None

    current_version = Version.parse((repo / "VERSION").read_text())
    bump, category = release_labels(labels)
    next_version = current_version.bump(bump)
    normalized = normalize_changelog(current_changelog)
    safe_title = " ".join(title.split())
    if not safe_title:
        raise ReleaseMetadataError("PR title must not be empty")
    entry = (
        f"## [{next_version}] - {date}\n\n"
        f"### {category}\n"
        f"- {safe_title} (#{pr_number}) {_release_marker(pr_number)}"
    )
    prefix, sections = _parse_changelog(normalized)
    updated_changelog = prefix + entry + "\n\n" + "\n\n".join(
        f"## [{section.version}] - {section.date}\n\n{section.body}".rstrip()
        for section in sections
    ) + "\n"

    package_path = repo / "package.json"
    lock_path = repo / "package-lock.json"
    package = _read_json(package_path)
    lock = _read_json(lock_path)
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise ReleaseMetadataError("package-lock.json must contain packages['']")
    package["version"] = next_version.package()
    lock["version"] = next_version.package()
    packages[""]["version"] = next_version.package()

    # Input parsing and output formatting complete before the owned files change.
    Version.parse(str(next_version))
    (repo / "VERSION").write_text(f"{next_version}\n")
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    changelog_path.write_text(updated_changelog)
    validate_release_metadata(repo)
    return next_version


def validate_release_metadata(repo: Path) -> None:
    version = Version.parse((repo / "VERSION").read_text())
    expected_package = version.package()
    package = _read_json(repo / "package.json")
    lock = _read_json(repo / "package-lock.json")
    package_version = package.get("version")
    lock_version = lock.get("version")
    lock_root_version = lock.get("packages", {}).get("", {}).get("version")
    if {package_version, lock_version, lock_root_version} != {expected_package}:
        raise ReleaseMetadataError(
            "package.json and package-lock.json root versions must equal "
            f"the first three VERSION components ({expected_package})"
        )
    _, sections = _parse_changelog((repo / "CHANGELOG.md").read_text())
    versions = [section.version for section in sections]
    if len(versions) != len(set(versions)):
        raise ReleaseMetadataError("CHANGELOG.md contains duplicate version headings")
    if versions != sorted(versions, reverse=True):
        raise ReleaseMetadataError("CHANGELOG.md headings must be descending by version")
    if not versions or versions[0] != version:
        raise ReleaseMetadataError(
            "the newest CHANGELOG.md heading must match canonical VERSION "
            f"({version})"
        )


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _json_at_ref(repo: Path, ref: str, path: str) -> dict:
    try:
        raw = _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError as exc:
        raise ReleaseMetadataError(f"could not read {path} at {ref}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseMetadataError(f"{path} at {ref} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseMetadataError(f"{path} at {ref} must be an object")
    return value


def guard_pr_metadata(
    repo: Path, base: str, head: str | None = None, *, working: bool = False
) -> list[str]:
    diff_args = ("diff", "--name-only", base) if working else ("diff", "--name-only", f"{base}...{head}")
    changed = set(filter(None, _git(repo, *diff_args).splitlines()))
    errors = []
    protected = changed & {"VERSION", "CHANGELOG.md"}
    if protected:
        errors.append(
            "Release metadata is post-merge automation-owned; remove "
            + ", ".join(sorted(protected))
            + " from this PR."
        )
    for path in ("package.json", "package-lock.json"):
        if path not in changed:
            continue
        base_json = _json_at_ref(repo, base, path)
        head_json = _read_json(repo / path) if working else _json_at_ref(repo, str(head), path)
        if path == "package.json":
            changed_version = base_json.get("version") != head_json.get("version")
        else:
            changed_version = (
                base_json.get("version") != head_json.get("version")
                or base_json.get("packages", {}).get("", {}).get("version")
                != head_json.get("packages", {}).get("", {}).get("version")
            )
        if changed_version:
            errors.append(
                f"{path} version fields are post-merge automation-owned; "
                "dependency changes are allowed, manual version edits are not."
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--pr-number", type=int, required=True)
    prepare.add_argument("--title", required=True)
    prepare.add_argument("--labels-json", required=True)
    prepare.add_argument("--date", required=True)
    subparsers.add_parser("validate")
    guard = subparsers.add_parser("guard")
    guard.add_argument("--base", required=True)
    guard_target = guard.add_mutually_exclusive_group(required=True)
    guard_target.add_argument("--head")
    guard_target.add_argument("--working", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "prepare":
            labels = json.loads(args.labels_json)
            if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
                raise ReleaseMetadataError("--labels-json must be a JSON array of strings")
            released = prepare_release(args.repo, args.pr_number, args.title, labels, args.date)
            print("already released" if released is None else f"prepared v{released}")
        elif args.command == "validate":
            validate_release_metadata(args.repo)
            print("release metadata valid")
        else:
            errors = guard_pr_metadata(
                args.repo, args.base, args.head, working=args.working
            )
            if errors:
                raise ReleaseMetadataError("\n".join(errors))
            print("release metadata ownership guard passed")
    except ReleaseMetadataError as exc:
        print(f"release metadata error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
