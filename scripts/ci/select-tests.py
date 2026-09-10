#!/usr/bin/env python3
"""Conservative suite selection and fail-closed required checks (stdlib only)."""

import json
import os
import subprocess
import sys

SUITES = ("web", "api", "ios", "ios_ui")


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.PIPE)


def release_only(path, base, head):
    """Ignore only version values, never dependency/script/lockfile changes."""
    if path not in ("package.json", "package-lock.json"):
        return False
    try:
        versions = []
        for ref in (base, head):
            value = json.loads(git("show", f"{ref}:{path}"))
            value.pop("version", None)
            if path == "package-lock.json":
                value.get("packages", {}).get("", {}).pop("version", None)
            versions.append(value)
        return versions[0] == versions[1]
    except (subprocess.CalledProcessError, ValueError, AttributeError):
        return False


def affected(path):
    # These web resources are also bundled by the native Xcode project.
    if path.startswith(
        ("src/apps/web/public/fonts/", "src/apps/web/public/plan/type-posters/")
    ):
        return {"web", "ios", "ios_ui"}
    # Unit-test edits and generated clients need compilation/unit contracts, but
    # do not change the native screens exercised by the fixture-driven UI suite.
    if (
        path.startswith(
            ("src/apps/ios/Tests/KriaTests/", "src/apps/ios/Kria/Generated/")
        )
        or path == "src/apps/ios/Tests/Fixtures/editor-commit-picker-contract.json"
    ):
        return {"ios"}
    # Check runtime trees before documentation: prompts and fixtures can be .md.
    if path.startswith(("src/apps/web/", "scripts/ci/web-tests")):
        return {"web"}
    if (
        path.startswith(("src/apps/ios/", "scripts/ios/"))
        or path == ".github/workflows/ios.yml"
    ):
        return {"ios", "ios_ui"}
    if path.startswith("src/apps/api/"):
        # Public contracts affect both clients; internal pipeline/tests only API.
        contracts = (
            "app/routes/",
            "app/schemas/",
            "app/kria/",
            "app/cli/kria_contracts.py",
            "app/models.py",
            "app/config.py",
            "app/main.py",
            "app/worker.py",
            "app/services/mobile_auth.py",
            "app/tasks/mobile_upload_cleanup.py",
            "app/migrations/versions/0100_mobile_identity_sessions.py",
            "tests/fixtures/kria_mobile.json",
            "tests/fixtures/kria_edit_recipe_v1.json",
            "pyproject.toml",
            "uv.lock",
            "requirements",
        )
        return (
            {"web", "api", "ios"}
            if path.removeprefix("src/apps/api/").startswith(contracts)
            else {"api"}
        )
    if path.startswith(("docs/", "plans/", "agents/")) or path in (
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "DESIGN.md",
        "TODOS.md",
        "CHANGELOG.md",
        "VERSION",
    ):
        return set()
    # Shared packages, dependencies, assets, CI policy, and unknown paths fail open
    # to full testing. New directories cannot silently bypass regression coverage.
    return set(SUITES)


def selection(event, base, head):
    if event != "pull_request":
        return set(SUITES), "Full regression run (non-PR event)."
    try:
        # No rename detection: both old and new paths must influence selection.
        ancestor = git("merge-base", base, head).decode().strip()
        raw = git("diff", "--no-renames", "--name-only", "-z", ancestor, head)
        paths = [p.decode("utf-8") for p in raw.split(b"\0") if p]
        if not paths:
            return set(SUITES), "Empty diff: conservatively running all suites."
        suites = set()
        for path in paths:
            if not release_only(path, ancestor, head):
                suites.update(affected(path))
        return (
            suites,
            f"Classified {len(paths)} changed paths against the PR merge base.",
        )
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return set(SUITES), "Diff unavailable: conservatively running all suites."


def gate(needs, suite, job):
    """A skipped job passes ONLY when a successful selector explicitly opted out."""
    if set(needs) != {"changes", job} or needs["changes"].get("result") != "success":
        raise ValueError("CI selection failed or required job results are missing")
    outputs = needs["changes"].get("outputs", {})
    selected = outputs.get(suite)
    if suite == "ios":
        ui = outputs.get("ios_ui")
        if ui not in ("true", "false") or (ui == "true" and selected != "true"):
            raise ValueError("Missing or inconsistent iOS UI selection")
    if suite == "lint":
        if any(outputs.get(key) not in ("true", "false") for key in ("web", "api")):
            raise ValueError("Missing or invalid lint selection")
        selected = str(any(outputs[key] == "true" for key in ("web", "api"))).lower()
    result = needs[job].get("result")
    if selected not in ("true", "false"):
        raise ValueError("Missing or invalid suite selection")
    expected = "success" if selected == "true" else "skipped"
    if result != expected:
        raise ValueError(f"{suite}: expected {expected}, got {result}")
    return (
        f"{suite}: passed"
        if selected == "true"
        else f"{suite}: not applicable to this PR"
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        print(gate(json.loads(os.environ.get("CI_NEEDS", "{}")), *sys.argv[2:]))
        return
    suites, reason = selection(
        os.environ.get("CI_EVENT"),
        os.environ.get("CI_BASE", ""),
        os.environ.get("CI_HEAD", ""),
    )
    output = "".join(f"{suite}={str(suite in suites).lower()}\n" for suite in SUITES)
    print(reason + "\n" + output)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(output)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(
                f"## CI selection\n\n{reason}\n\n"
                + "\n".join(
                    f"- {s}: {'run' if s in suites else 'not applicable'}"
                    for s in SUITES
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
