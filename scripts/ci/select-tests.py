#!/usr/bin/env python3
"""Conservative suite selection and fail-closed required checks (stdlib only)."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ios"))
import ui_tests

SUITES = ("api", "ios", "ios_ui", "shared_motion")
SHARED_MOTION_PATHS = (
    "src/apps/api/app/pipeline/motion_scene.py",
    "src/apps/web/src/__tests__/lib/motion-runtime.test.ts",
    "src/apps/web/package.json",
    "src/apps/web/package-lock.json",
    "src/apps/web/jest.config.js",
    "src/apps/web/jest.setup.ts",
    "src/apps/web/tsconfig.json",
)

# This is deliberately independent from the broad suite selector.  KRI-559 is
# a cross-runtime proof: the saved creation transport must survive the API
# compiler, persisted-draft boundary, and the actual iPhone exporter.
JOURNEY_PREFIXES = (
    "src/apps/api/app/pipeline/",
    "src/apps/api/app/agents/edit_copilot.py",
    "src/apps/api/app/services/creation_text_composition.py",
    "src/apps/api/app/services/cloud_render_contract.py",
    "src/apps/api/app/services/creator_render_contract.py",
    "src/apps/api/app/services/kria_editor_ops.py",
    "src/apps/api/app/services/phone_rollout.py",
    "src/apps/api/app/services/phone_sources.py",
    "src/apps/api/app/kria/",
    "src/apps/api/app/schemas/",
    "src/apps/api/prompts/",
    "src/apps/api/tests/fixtures/",
    "src/apps/api/tests/evals/request_following/",
    "src/apps/ios/Kria/",
    "src/apps/ios/Packages/KriaMediaEngine/",
    "src/apps/ios/Tests/KriaTests/DeviceMontageRenderE2ETests.swift",
    "src/apps/ios/Tests/Fixtures/KRI524CreationDraft.json",
    "scripts/ios/kri-524-creation-e2e.py",
)

# CI-visible corpus: a mapped change must execute its incident's fixture-backed
# tests. Broad journey inputs without a mapping block the stable required check.
JOURNEYS = json.loads((Path(__file__).with_name("journey-manifest.json")).read_text())[
    "journeys"
]
JOURNEY_POLICY_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci-changes.yml",
    "scripts/ci/select-tests.py",
    "scripts/ci/journey-gate.py",
    "scripts/ci/journey-manifest.json",
    "scripts/ci/test_journey_gate.py",
    "scripts/ci/test_select_tests.py",
)


def journey_native_for_ids(ids):
    """Whether selected journeys require Xcode/iPhone validation."""
    selected = set(ids)
    known = {journey["id"] for journey in JOURNEYS}
    if selected - known:
        raise ValueError(f"Unknown journey ids: {sorted(selected - known)}")
    for journey in JOURNEYS:
        if not isinstance(journey.get("needs_native"), bool):
            raise ValueError(
                f"Journey {journey.get('id', '<unknown>')} has invalid needs_native"
            )
    return any(
        journey["needs_native"] for journey in JOURNEYS if journey["id"] in selected
    )


def matches_journey_path(path, prefix):
    return path.startswith(prefix) if prefix.endswith("/") else path == prefix


def journey_affected(path):
    """Whether a change needs an incident-corpus journey or an explicit gap."""
    if path.startswith(JOURNEY_PREFIXES) or any(
        matches_journey_path(path, prefix)
        for journey in JOURNEYS
        for prefix in journey["paths"]
    ):
        return True
    # CI policy and this evidence parser are security boundaries.  A change to
    # either must execute the gate it could otherwise weaken.
    return path in JOURNEY_POLICY_PATHS


def journey_details(event, base, head):
    """Return (selected, coverage, fixture ids); unknown inputs fail closed."""
    if event != "pull_request":
        return True, "covered", ",".join(journey["id"] for journey in JOURNEYS)
    try:
        ancestor = git("merge-base", base, head).decode().strip()
        raw = git("diff", "--no-renames", "--name-only", "-z", ancestor, head)
        paths = [p.decode("utf-8") for p in raw.split(b"\0") if p]
        relevant = [path for path in paths if journey_affected(path)]
        if not paths:
            return True, "gap", ""
        if not relevant:
            return False, "not_applicable", ""
        selected = set()
        for path in relevant:
            if path in JOURNEY_POLICY_PATHS:
                selected.update(journey["id"] for journey in JOURNEYS)
                continue
            matches = [
                (len(prefix), journey["id"])
                for journey in JOURNEYS
                for prefix in journey["paths"]
                if matches_journey_path(path, prefix)
            ]
            if not matches:
                return True, "gap", ""
            longest = max(length for length, _ in matches)
            selected.update(
                journey_id for length, journey_id in matches if length == longest
            )
        return (
            True,
            "covered",
            ",".join(
                journey["id"] for journey in JOURNEYS if journey["id"] in selected
            ),
        )
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return True, "gap", ""


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


def changed_ui_tests(base, head, paths):
    """Map each changed UI test source to the test methods its diff touched.

    Line numbers come from head's version of the file, which is also where the
    methods are located. A file maps to None (its whole feature group) when any
    change falls outside a test method or its head content is unavailable.
    """
    sources = [path for path in paths if path.startswith(ui_tests.UI_TEST_ROOT)]
    if not sources:
        return {}
    diff = git("diff", "--no-renames", "--no-color", "-U0", base, head, "--", *sources)
    lines, current = {}, None
    for line in diff.decode("utf-8").splitlines():
        if line.startswith("+++ "):
            current = line.removeprefix("+++ b/") if line.startswith("+++ b/") else None
            if current is not None:
                lines.setdefault(current, set())
        elif line.startswith("@@") and current is not None:
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if not hunk:
                lines[current].add(0)  # Unparseable: line 0 forces the group.
                continue
            start, count = int(hunk.group(1)), int(hunk.group(2) or "1")
            # A pure deletion sits between `start` and `start + 1`; blame both.
            changed = range(start, start + count) if count else (start, start + 1)
            lines[current].update(changed)
    focused = {}
    for path in sources:
        try:
            source = git("show", f"{head}:{path}").decode("utf-8")
        except (subprocess.CalledProcessError, UnicodeError):
            focused[path] = None
            continue
        focused[path] = ui_tests.changed_tests(source, lines.get(path, set()))
    return focused


# Swift files whose server-mirrored constants are parsed by the API drift guards
# test_ios_text_row_look_parity.py and test_slide_post_font_parity.py, so a
# Swift-only change to them runs the API suite too. Not an exhaustive list of
# iOS paths that API tests read.
SWIFT_SERVER_MIRRORS = (
    "src/apps/ios/Kria/Core/NativeEditorDocument.swift",
    "src/apps/ios/Kria/Core/SlidePost.swift",
)


def affected(path):
    if path.startswith("src/packages/motion-runtime/"):
        return set(SUITES)
    if path in SHARED_MOTION_PATHS:
        return (
            {"api", "shared_motion"}
            if path.endswith("motion_scene.py")
            else {"shared_motion"}
        )
    if path in SWIFT_SERVER_MIRRORS:
        return {"api", "ios", "ios_ui"}
    # These web resources are also bundled by the native Xcode project.
    if path.startswith(
        (
            "src/apps/web/public/fonts/",
            "src/apps/web/public/plan/type-posters/",
            "src/apps/web/public/landing/raw-story/",
        )
    ):
        return {"ios", "ios_ui"}
    # Unit-test edits and generated clients need compilation/unit contracts, but
    # do not change the native screens exercised by the fixture-driven UI suite.
    if path.startswith(
        ("src/apps/ios/Tests/KriaTests/", "src/apps/ios/Kria/Generated/")
    ) or path in (
        "src/apps/ios/Tests/Fixtures/editor-commit-picker-contract.json",
        "src/apps/ios/Tests/Fixtures/guided_label_rebase_vectors.json",
    ):
        return {"ios"}
    # Check runtime trees before documentation: prompts and fixtures can be .md.
    if path.startswith(("src/apps/web/", "scripts/ci/web-tests")):
        return set()
    if path.startswith("src/apps/api/app/kria/"):
        matched = {
            journey["id"]
            for journey in JOURNEYS
            for prefix in journey["paths"]
            if matches_journey_path(path, prefix)
        }
        # Known Python-only journey paths stay on Linux; unmapped Kria code
        # keeps the conservative shared-contract fallback until mapped.
        if matched and not journey_native_for_ids(matched):
            return {"api"}
        return {"api", "ios"}
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
            {"api", "ios"}
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


def selection_details(event, base, head):
    if event != "pull_request":
        return set(SUITES), "Full regression run (non-PR event).", "full"
    try:
        # No rename detection: both old and new paths must influence selection.
        ancestor = git("merge-base", base, head).decode().strip()
        raw = git("diff", "--no-renames", "--name-only", "-z", ancestor, head)
        paths = [p.decode("utf-8") for p in raw.split(b"\0") if p]
        if not paths:
            return set(SUITES), "Empty diff: conservatively running all suites.", "full"
        suites = set()
        ui_paths = []
        for path in paths:
            if not release_only(path, ancestor, head):
                suites.update(affected(path))
                if "ios_ui" in affected(path):
                    ui_paths.append(path)
        groups, reason = (
            ui_tests.select_groups(
                ui_paths, focused=changed_ui_tests(ancestor, head, ui_paths)
            )
            if ui_paths
            else ("none", "UI not applicable.")
        )
        return (
            suites,
            f"Classified {len(paths)} changed paths against the PR merge base. {reason}",
            groups,
        )
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return (
            set(SUITES),
            "Diff unavailable: conservatively running all suites.",
            "full",
        )


def journey_selection(event, base, head):
    """Select the expensive offline journey, fail closed when diff data is absent."""
    return journey_details(event, base, head)[0]


def journey_needs_native(event, base, head):
    """Select macOS only when the manifest says a selected journey needs it."""
    selected, _coverage, ids = journey_details(event, base, head)
    if not selected:
        return False
    return journey_native_for_ids(ids.split(",") if ids else ())


def selection(event, base, head):
    return selection_details(event, base, head)[:2]


def ui_shards(event, groups):
    """Keep PR and main to one Mac; manual runs may shard the full suite."""

    if event in ("pull_request", "push"):
        return 1
    if groups == "none":
        return 1
    try:
        return ui_tests.shard_count(groups)
    except (OSError, ValueError, KeyError, TypeError):
        # The UI step fails closed on the same error; one leg reports it.
        return 1


def gate(needs, suite, job):
    """A skipped job passes ONLY when a successful selector explicitly opted out."""
    if suite in ("api", "journey"):
        required_needs = {
            "changes",
            "test-api-suite",
            "journey",
            "creator-contract-replay",
            "shared-motion-runtime",
        }
    elif suite == "ios" and job == "journey":
        required_needs = {"changes", "journey", "ios-contracts"}
    else:
        required_needs = {"changes", job}
    if set(needs) != required_needs or needs["changes"].get("result") != "success":
        raise ValueError("CI selection failed or required job results are missing")
    outputs = needs["changes"].get("outputs", {})
    selected = outputs.get(suite)
    if suite == "ios":
        ui = outputs.get("ios_ui")
        groups = ui_tests.validate_groups(outputs.get("ios_ui_groups"))
        if (groups == "none") != (ui == "false"):
            raise ValueError("Inconsistent iOS UI groups")
        if ui not in ("true", "false") or (ui == "true" and selected != "true"):
            raise ValueError("Missing or inconsistent iOS UI selection")
        shards = outputs.get("ios_ui_shards")
        counts = {str(count) for count in range(1, ui_tests.MAX_SHARDS + 1)}
        if shards not in counts or (ui == "false" and shards != "1"):
            raise ValueError("Missing or inconsistent iOS UI shard count")
    if suite == "lint":
        if outputs.get("api") not in ("true", "false"):
            raise ValueError("Missing or invalid lint selection")
        selected = outputs["api"]
    result = needs[job].get("result")
    if selected not in ("true", "false"):
        raise ValueError("Missing or invalid suite selection")
    # On PRs the journey job owns both selected iOS coverage and incident
    # journeys. A green shared job can represent either selection, while an
    # intentional skip is valid only when neither selection needs it.
    execution_selected = selected == "true"
    if job == "journey" and suite in ("ios", "journey"):
        peer = outputs.get("journey" if suite == "ios" else "ios")
        if peer not in ("true", "false"):
            raise ValueError("Missing or invalid shared native selection")
        execution_selected = execution_selected or peer == "true"
    expected = "success" if execution_selected else "skipped"
    if result != expected:
        raise ValueError(f"{suite}: expected {expected}, got {result}")
    if suite == "ios" and job == "journey":
        contracts_expected = "success" if selected == "true" else "skipped"
        contracts_result = needs["ios-contracts"].get("result")
        if contracts_result != contracts_expected:
            raise ValueError(
                f"ios contracts: expected {contracts_expected}, got {contracts_result}"
            )
    if suite == "api":
        shared_selected = outputs.get("shared_motion")
        shared_result = needs["shared-motion-runtime"].get("result")
        if shared_selected not in ("true", "false"):
            raise ValueError("Missing or invalid shared motion selection")
        shared_expected = "success" if shared_selected == "true" else "skipped"
        if shared_result != shared_expected:
            raise ValueError(
                f"shared_motion: expected {shared_expected}, got {shared_result}"
            )
    return (
        f"{suite}: passed"
        if selected == "true"
        else f"{suite}: not applicable to this PR"
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        print(gate(json.loads(os.environ.get("CI_NEEDS", "{}")), *sys.argv[2:]))
        return
    event = os.environ.get("CI_EVENT")
    suites, reason, groups = selection_details(
        event,
        os.environ.get("CI_BASE", ""),
        os.environ.get("CI_HEAD", ""),
    )
    shards = ui_shards(event, groups)
    output = "".join(f"{suite}={str(suite in suites).lower()}\n" for suite in SUITES)
    journey, coverage, journey_ids = journey_details(
        event, os.environ.get("CI_BASE", ""), os.environ.get("CI_HEAD", "")
    )
    native = journey_native_for_ids(journey_ids.split(",") if journey_ids else ())
    output += f"ios_ui_groups={groups}\nios_ui_shards={shards}\njourney={str(journey).lower()}\njourney_coverage={coverage}\njourney_ids={journey_ids}\njourney_needs_native={str(native).lower()}\n"
    print(reason + "\n" + output)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(output)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(
                f"## CI selection\n\n{reason}\n\niOS UI groups: {groups}"
                f" ({shards} native leg{'s' if shards > 1 else ''})\n\n"
                + "\n".join(
                    f"- {s}: {'run' if s in suites else 'not applicable'}"
                    for s in SUITES
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
