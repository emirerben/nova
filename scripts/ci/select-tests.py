#!/usr/bin/env python3
"""Conservative suite selection and fail-closed required checks (stdlib only)."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ios"))
import render_evidence
import ui_tests

SUITES = ("api", "ios", "ios_ui", "shared_motion")
# These test sources do not alter app code or a fixture-export harness. Selector
# contracts always run before selection; ChatWorkspace cases run in full units.
ISOLATED_TEST_SUITES = {
    "scripts/ci/test_select_tests.py": frozenset(),
    "src/apps/ios/Tests/KriaTests/ChatWorkspaceTests.swift": frozenset({"ios"}),
}
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
    "scripts/ci/wait-for-jobs.py",
    "scripts/ios/render_evidence.py",
)


# Mac legs of ci.yml's journey-native matrix. `unit` always runs: it builds,
# executes the unit suite, the UI smoke subset and the named-unit gate. The
# fixture-driven journeys are split across two more legs that compile from the
# same cache, balanced on 2026-10-23 measurements (unit ~17 min, creation
# ~17 min, screens ~20 min; one serial Mac took 40-55 min). A native journey
# missing here has no workflow step of its own and is proven by its named
# unit tests on the unit leg. test_ci_throughput.py keeps this map and the
# workflow's step conditions in agreement.
#
# Pull requests start only `unit`. Three Macs per iOS PR (~70 Mac minutes)
# saturated the account's five concurrent macOS runners: from 2026-10-27 the
# median PR waited 10-19 min for a Mac (p90 ~85) and took 67-87 min overall.
# The creation and screens legs run in the nightly CI schedule and on manual
# CI dispatch (run it on a branch for pre-merge native render evidence).
PR_NATIVE_LEGS = ("unit",)
LEG_JOURNEYS = {
    "creation": (
        "kri-524-creation",
        "kri-492-length-ownership",
        "kri-669-gameday",
        "kri-689-clip-speed",
        "kri-729-camera-roll-recommendations",
    ),
    "screens": (
        "kri-648-phone-text",
        "kri-756-suggestion-chips",
        "kri-802-question-batch",
        "kri-384-ios-library",
    ),
}


def native_legs(ids, event=None):
    """Matrix legs to start: `unit`, plus each leg owning a selected journey.

    Pull requests stop at PR_NATIVE_LEGS; nightly and manual runs start every
    leg that owns a selected journey."""
    selected = set(ids)
    known = {journey["id"] for journey in JOURNEYS}
    if selected - known:
        raise ValueError(f"Unknown journey ids: {sorted(selected - known)}")
    return ["unit"] + [
        leg
        for leg, journeys in LEG_JOURNEYS.items()
        if selected & set(journeys)
        and (event != "pull_request" or leg in PR_NATIVE_LEGS)
    ]


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
    if path in ISOLATED_TEST_SUITES:
        return False
    if path.startswith(JOURNEY_PREFIXES) or any(
        matches_journey_path(path, prefix)
        for journey in JOURNEYS
        for prefix in journey["paths"]
    ):
        return True
    # CI policy and this evidence parser are security boundaries.  A change to
    # either must execute the gate it could otherwise weaken.
    return path in JOURNEY_POLICY_PATHS


def journey_ids_for_path(path):
    """Journey ids a relevant path selects; empty means the path is unmapped.

    Single source of truth shared by the CI selector and `unmapped-paths`, so
    the local preship report can never disagree with what CI will decide.
    """
    if path in ISOLATED_TEST_SUITES:
        return set()
    if path in JOURNEY_POLICY_PATHS:
        return {journey["id"] for journey in JOURNEYS}
    matches = [
        (len(prefix), journey["id"])
        for journey in JOURNEYS
        for prefix in journey["paths"]
        if matches_journey_path(path, prefix)
    ]
    if not matches:
        return set()
    longest = max(length for length, _ in matches)
    return {journey_id for length, journey_id in matches if length == longest}


def journey_diff_paths(base, head):
    """Changed paths on the merge-base diff; raises on any git/decoding error."""
    ancestor = git("merge-base", base, head).decode().strip()
    raw = git("diff", "--no-renames", "--name-only", "-z", ancestor, head)
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def journey_details(event, base, head):
    """Return (selected, coverage, fixture ids); unknown inputs fail closed."""
    if event != "pull_request":
        return True, "covered", ",".join(journey["id"] for journey in JOURNEYS)
    try:
        paths = journey_diff_paths(base, head)
        relevant = [path for path in paths if journey_affected(path)]
        if not paths:
            return True, "gap", ""
        if not relevant:
            return False, "not_applicable", ""
        selected = set()
        for path in relevant:
            ids = journey_ids_for_path(path)
            if not ids:
                return True, "gap", ""
            selected.update(ids)
        return (
            True,
            "covered",
            ",".join(
                journey["id"] for journey in JOURNEYS if journey["id"] in selected
            ),
        )
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return True, "gap", ""


def unmapped_journey_paths(base, head, paths=None):
    """Journey-relevant changed paths with no manifest mapping (raises on git error).

    `paths` replaces the git diff (used for uncommitted files in preship)."""
    if paths is None:
        paths = journey_diff_paths(base, head)
    return [
        path
        for path in paths
        if journey_affected(path) and not journey_ids_for_path(path)
    ]


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
    if path in ISOLATED_TEST_SUITES:
        return set(ISOLATED_TEST_SUITES[path])
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
        groups = "smoke" if event == "push" else "full"
        return (
            set(SUITES),
            "Main release tripwire." if event == "push" else "Full regression run.",
            groups,
        )
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


def render_evidence_selection(event, base, head):
    """PRs run the slow export/OCR unit proofs only when their code changed.

    Everything except a pull request with a readable diff runs them, so main
    pushes, nightly and manual runs keep the whole unit suite (fail closed).
    """
    if event != "pull_request":
        return True
    try:
        ancestor = git("merge-base", base, head).decode().strip()
        raw = git("diff", "--no-renames", "--name-only", "-z", ancestor, head)
        paths = [p.decode("utf-8") for p in raw.split(b"\0") if p]
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return True
    return not paths or render_evidence.triggered(paths)


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
    """PR/main use one Mac; nightly/manual shard the full suite to fit the cap."""

    if event in ("pull_request", "push"):
        return 1
    if groups == "none":
        return 1
    try:
        return ui_tests.shard_count(groups)
    except (OSError, ValueError, KeyError, TypeError):
        # The UI step fails closed on the same error; one leg reports it.
        return 1


def native_validation(event, suites, journey_native):
    """PR/manual/nightly native proof; main's release workflow owns its Mac."""
    return event != "push" and ("ios" in suites or journey_native)


def validate_journey_mapping(coverage, ids):
    selected = ids.split(",") if ids else []
    if coverage != "covered" or not selected or len(selected) != len(set(selected)):
        raise ValueError("Journey selection has an unmapped or invalid incident")
    journey_native_for_ids(selected)
    return selected


def gate_journey(needs, event):
    required = {
        "changes",
        "journey-mapping",
        "journey-offline",
        "journey-library",
        "journey-native",
    }
    if set(needs) != required or needs["changes"].get("result") != "success":
        raise ValueError("Journey selection failed or dependencies are missing")
    outputs = needs["changes"].get("outputs", {})
    for key in ("journey", "ios", "journey_needs_native", "native_validation"):
        if outputs.get(key) not in ("true", "false"):
            raise ValueError(f"Missing or invalid {key}")
    journey = outputs["journey"] == "true"
    selected = []
    if journey:
        selected = validate_journey_mapping(
            outputs.get("journey_coverage"), outputs.get("journey_ids")
        )
        if journey_native_for_ids(selected) != (
            outputs["journey_needs_native"] == "true"
        ):
            raise ValueError("Inconsistent manifest native selection")
    elif (
        outputs.get("journey_coverage") != "not_applicable"
        or outputs.get("journey_ids") != ""
        or outputs["journey_needs_native"] != "false"
    ):
        raise ValueError("Inconsistent unselected journey")
    native = native_validation(
        event,
        {"ios"} if outputs["ios"] == "true" else set(),
        outputs["journey_needs_native"] == "true",
    )
    if outputs["native_validation"] != str(native).lower():
        raise ValueError("Native execution does not match event and selection")
    for job, selected in (
        ("journey-mapping", journey),
        ("journey-offline", journey),
        ("journey-library", "kri-384-library-ingest" in selected),
        ("journey-native", native),
    ):
        expected = "success" if selected else "skipped"
        if needs[job].get("result") != expected:
            raise ValueError(
                f"{job}: expected {expected}, got {needs[job].get('result')}"
            )
    return "Selected portable and native journey dependencies passed."


def gate(needs, suite, job, event="pull_request"):
    """A skipped job passes ONLY when a successful selector explicitly opted out."""
    if suite in ("api", "journey"):
        required_needs = {
            "changes",
            "test-api-suite",
            "journey",
            "journey-library",
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
        groups = outputs.get("ios_ui_groups")
        if event == "push" and groups == "smoke":
            ui_tests.validate_execution(groups)
        else:
            ui_tests.validate_groups(groups)
        if (groups == "none") != (ui == "false"):
            raise ValueError("Inconsistent iOS UI groups")
        if ui not in ("true", "false") or (ui == "true" and selected != "true"):
            raise ValueError("Missing or inconsistent iOS UI selection")
        shards = outputs.get("ios_ui_shards")
        counts = {str(count) for count in range(1, ui_tests.MAX_SHARDS + 1)}
        if shards not in counts or (
            (ui == "false" or groups == "smoke") and shards != "1"
        ):
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
    if suite == "journey":
        ids = (
            outputs.get("journey_ids", "").split(",")
            if outputs.get("journey_ids")
            else []
        )
        library_expected = "success" if "kri-384-library-ingest" in ids else "skipped"
        library_result = needs["journey-library"].get("result")
        if library_result != library_expected:
            raise ValueError(
                f"library journey: expected {library_expected}, got {library_result}"
            )
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
    if len(sys.argv) > 1 and sys.argv[1] == "gate-journey":
        print(
            gate_journey(
                json.loads(os.environ["CI_NEEDS"]), os.environ["GITHUB_EVENT_NAME"]
            )
        )
        return
    if len(sys.argv) > 1 and sys.argv[1] == "validate-mapping":
        validate_journey_mapping(os.environ["COVERAGE"], os.environ["JOURNEY_IDS"])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "unmapped-paths":
        # Local preship helper (KRI-749): name the paths that make CI's
        # journey-mapping check fail closed. Exit 1 on any unmapped path or
        # git error; usage: unmapped-paths --base <ref> [--head <ref>], or
        # unmapped-paths --stdin to classify newline-separated paths instead.
        args = sys.argv[2:]
        use_stdin = "--stdin" in args
        opts = dict(zip(*[iter([a for a in args if a != "--stdin"])] * 2))
        base, head = opts.get("--base"), opts.get("--head", "HEAD")
        if not base and not use_stdin:
            print(
                "usage: unmapped-paths --base <ref> [--head <ref>] | --stdin",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            unmapped = unmapped_journey_paths(
                base,
                head,
                [p for p in sys.stdin.read().splitlines() if p] if use_stdin else None,
            )
        except (subprocess.CalledProcessError, UnicodeError, OSError) as error:
            print(f"unmapped-paths: git diff failed: {error}", file=sys.stderr)
            sys.exit(1)
        print("\n".join(unmapped))
        sys.exit(1 if unmapped else 0)
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        print(
            gate(
                json.loads(os.environ.get("CI_NEEDS", "{}")),
                *sys.argv[2:],
                event=os.environ.get("GITHUB_EVENT_NAME", ""),
            )
        )
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
    selected_ids = journey_ids.split(",") if journey_ids else ()
    native = journey_native_for_ids(selected_ids)
    legs = native_legs(selected_ids, event)
    output += (
        f"native_validation={str(native_validation(event, suites, native)).lower()}\n"
    )
    output += f"ios_ui_groups={groups}\nios_ui_shards={shards}\njourney={str(journey).lower()}\njourney_coverage={coverage}\njourney_ids={journey_ids}\njourney_needs_native={str(native).lower()}\n"
    output += f"native_legs={json.dumps(legs)}\n"
    render = render_evidence_selection(
        event, os.environ.get("CI_BASE", ""), os.environ.get("CI_HEAD", "")
    )
    output += f"ios_render_evidence={str(render).lower()}\n"
    print(reason + "\n" + output)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(output)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(
                f"## CI selection\n\n{reason}\n\niOS UI groups: {groups}"
                f" ({shards} native leg{'s' if shards > 1 else ''})\n\n"
                f"iPhone validation legs: {', '.join(legs)}\n\n"
                f"Export/OCR unit proofs: {'run' if render else 'skipped (no render code changed; main runs them)'}\n\n"
                + "\n".join(
                    f"- {s}: {'run' if s in suites else 'not applicable'}"
                    for s in SUITES
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
