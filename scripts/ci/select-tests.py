#!/usr/bin/env python3
"""Conservative suite selection and fail-closed required checks (stdlib only)."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ios"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_registry
import release_inputs
import render_evidence
import ui_tests

SUITES = ("api", "ios", "ios_ui", "shared_motion")
# These test sources do not alter app code or a fixture-export harness. Selector
# contracts always run before selection; ChatWorkspace cases run in full units.
ISOLATED_TEST_SUITES = {
    "scripts/ci/test_select_tests.py": frozenset(),
    "src/apps/ios/Tests/KriaTests/ChatWorkspaceTests.swift": frozenset({"api", "ios"}),
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

# Everything outside the selector and CI policy that the iPhone build or its
# tests can read: the app tree, the scripts that drive it, resources Xcode
# bundles from the web tree, and the fonts and parity fixtures Swift tests open
# by path. A PR that changes none of these gives the Mac the same bytes `main`
# already built and tested; a stale mobile contract is caught on Linux by
# ios-contracts, and regenerating it changes src/apps/ios/Kria/Generated/.
# test_select_tests.py scans the iPhone tree for repo paths missing from here.
IOS_NATIVE_INPUTS = (
    "src/apps/ios/",
    "scripts/ios/",
    ".github/workflows/ios.yml",
    "src/apps/web/public/fonts/",
    "src/apps/web/public/plan/type-posters/",
    "src/apps/web/public/landing/raw-story/",
    "src/apps/api/assets/fonts/",
    "src/packages/motion-runtime/",
    "tests/fixtures/",
    "brand/social/dist/",
)
# Swift parity tests open files directly in this directory, some by a name built
# at run time. Its subdirectories hold API-only fixtures.
IOS_NATIVE_FIXTURE_DIR = "src/apps/api/tests/fixtures/"

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
JOURNEYS = journey_registry.journeys()
JOURNEY_POLICY_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci-changes.yml",
    "scripts/ci/select-tests.py",
    "scripts/ci/journey-gate.py",
    "scripts/ci/journey-manifest.json",
    "scripts/ci/journey_registry.py",
    "scripts/ci/test_journey_gate.py",
    "scripts/ci/test_journey_registry.py",
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


def native_legs(ids, event=None, ui_shards=1):
    """Matrix legs to start: `unit`, each leg owning a selected journey, and
    one `ui-<i>` leg per PR UI shard.

    Pull requests stop at PR_NATIVE_LEGS plus their UI shards; nightly and
    manual runs start every leg that owns a selected journey. An editor PR's
    UI group ran 70-105 min serially after build and unit tests (#2225,
    #2234), so a selection of more than one shard leaves the unit leg to build
    and unit tests and runs each shard on its own Mac (ui_tests.py partition).
    """
    selected = set(ids)
    known = {journey["id"] for journey in JOURNEYS}
    if selected - known:
        raise ValueError(f"Unknown journey ids: {sorted(selected - known)}")
    shards = (
        [f"ui-{index}" for index in range(1, ui_shards + 1)]
        if event == "pull_request" and ui_shards > 1
        else []
    )
    return (
        ["unit"]
        + [
            leg
            for leg, journeys in LEG_JOURNEYS.items()
            if selected & set(journeys)
            and (event != "pull_request" or leg in PR_NATIVE_LEGS)
        ]
        + shards
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


matches_journey_path = journey_registry.matches_journey_path


JOURNEY_MANIFEST = "scripts/ci/journey-manifest.json"


def selection_rollback():
    """Repository variable CI_MAC_SELECTION=full: undo KRI-1534's PR narrowing.

    One switch restores what pull requests selected before: a Mac whenever
    `ios` or a native journey is selected, everything for a journey-manifest
    edit, and everything for `.env.example`. No code change is needed.
    """
    return os.environ.get("CI_MAC_SELECTION", "") == "full"


def manifest_row_changes(before, after):
    """Journey ids a manifest edit added or changed, and whether any is native.

    The manifest is CI policy, so a row whose definition changed must execute.
    Unchanged rows are identical on both sides and lose nothing by not running.
    Returns None for every edit that is not a plain row change (a removed
    journey, a change outside the rows, duplicate or missing ids, a manifest
    that is not the expected shape): the caller then selects everything.
    """
    try:
        if {k: v for k, v in before.items() if k != "journeys"} != {
            k: v for k, v in after.items() if k != "journeys"
        }:
            return None
        old = {row["id"]: row for row in before["journeys"]}
        new = {row["id"]: row for row in after["journeys"]}
        if len(old) != len(before["journeys"]) or len(new) != len(after["journeys"]):
            return None
    except (AttributeError, KeyError, TypeError):
        return None
    if set(old) - set(new):
        return None
    changed = sorted(i for i, row in new.items() if old.get(i) != row)

    def native(row):
        return bool(
            row
            and (row.get("needs_native") or row.get("xctests") or row.get("xctest"))
        )

    return changed, any(native(new[i]) or native(old.get(i)) for i in changed)


def selectable_manifest_edit(before, after):
    """`manifest_row_changes`, or None when a changed id cannot be selected."""
    edit = manifest_row_changes(before, after)
    known = {journey["id"] for journey in JOURNEYS}
    # A changed id this checkout's registry does not know cannot be run.
    return edit if edit is not None and set(edit[0]) <= known else None


def manifest_edit(ancestor, head):
    """The selectable manifest edit between two commits, or None."""
    if selection_rollback():
        return None
    try:
        before = json.loads(git("show", f"{ancestor}:{JOURNEY_MANIFEST}"))
        after = json.loads(git("show", f"{head}:{JOURNEY_MANIFEST}"))
    except (subprocess.CalledProcessError, ValueError, UnicodeError):
        return None
    return selectable_manifest_edit(before, after)


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
        ancestor = git("merge-base", base, head).decode().strip()
        paths = journey_diff_paths(base, head)
        if not paths:
            return True, "gap", ""
        selected = set()
        # A plain row edit of the manifest selects the rows it changed
        # (possibly none); every other manifest edit selects every journey.
        edit = manifest_edit(ancestor, head) if JOURNEY_MANIFEST in paths else None
        if edit is not None:
            selected.update(edit[0])
            paths = [path for path in paths if path != JOURNEY_MANIFEST]
        relevant = [path for path in paths if journey_affected(path)]
        if not relevant and not selected:
            return False, "not_applicable", ""
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
        return {"api", "ios"}
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
    # API parity and fixture guards read Swift sources and iOS fixtures by path
    # (tests/kria/test_editor_capability_parity.py, tests/incidents/loader.py),
    # so a change under src/apps/ios/ runs the API suite too.
    if path.startswith("src/apps/ios/"):
        return {"api", "ios", "ios_ui"}
    # tests/evals/request_following/test_creation_composition.py executes this
    # script to check the committed iOS fixture, so the API suite must run too.
    if path == "scripts/ios/kri-524-creation-e2e.py":
        return {"api", "ios", "ios_ui"}
    if path.startswith("scripts/ios/") or path == ".github/workflows/ios.yml":
        return {"ios", "ios_ui"}
    if path.startswith("src/apps/api/"):
        # Fonts and parity fixtures Swift unit tests open by path: both sides
        # read the file, so both suites run.
        if declared_native_input(path):
            return {"api", "ios"}
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
    # API tests read the documented flags; nothing on the iPhone side does.
    if path == ".env.example" and not selection_rollback():
        return {"api"}
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
            if path == JOURNEY_MANIFEST:
                edit = manifest_edit(ancestor, head)
                if edit is not None:
                    # API tests read the manifest. A changed native row also
                    # runs the iPhone unit suite that proves its named tests.
                    suites.update({"api", "ios"} if edit[1] else {"api"})
                    continue
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


def declared_native_input(path):
    """Whether a path is a declared iPhone build or test input."""
    if path.startswith(IOS_NATIVE_INPUTS):
        return True
    name = path.removeprefix(IOS_NATIVE_FIXTURE_DIR)
    return name != path and "/" not in name


def native_input(path):
    """Whether a changed path can alter what the Mac builds or tests.

    Declared inputs, plus every path `affected` cannot place (shared packages,
    CI policy, new directories): those select all suites and stay fail closed.
    """
    return declared_native_input(path) or affected(path) == set(SUITES)


def inputs_changed_between(ancestor, head, is_input=native_input):
    """Whether ancestor..head changes a path `is_input` accepts.

    An empty diff counts as changed, a journey-manifest edit by its rows (a
    native row needs the Mac), and release-only version bumps never count.
    Raises on any git or decoding error; callers fail closed.
    """
    raw = git("diff", "--no-renames", "--name-only", "-z", ancestor, head)
    paths = [p.decode("utf-8") for p in raw.split(b"\0") if p]
    if not paths:
        return True
    for path in paths:
        if path == JOURNEY_MANIFEST:
            edit = manifest_edit(ancestor, head)
            if edit is not None:
                if edit[1]:
                    return True
                continue
        if not release_only(path, ancestor, head) and is_input(path):
            return True
    return False


def native_inputs_changed(event, base, head):
    """The PR Mac rule (KRI-1534): did this PR change an iPhone input?

    Everything except a pull request with a readable, non-empty diff is True.
    """
    if event != "pull_request":
        return True
    try:
        ancestor = git("merge-base", base, head).decode().strip()
        return inputs_changed_between(ancestor, head)
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return True


# The main-push Mac rule (KRI-1547). ios.yml's push lane builds, runs the unit
# suite and the UI smoke tests, then dispatches TestFlight. It starts only when
# a path either side reads changed since the last green push run of ios.yml.
IOS_WORKFLOW = "ios.yml"
TESTFLIGHT_WORKFLOW = "testflight.yml"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
# Errors that mean "history or diff unknown": the Mac runs.
HISTORY_ERRORS = (
    subprocess.SubprocessError,
    OSError,
    UnicodeError,
    ValueError,
    KeyError,
    TypeError,
)


def main_push_input(path):
    """An iPhone build/test input (as on a PR) or a TestFlight release input."""
    return native_input(path) or release_inputs.release_input(path)


def workflow_runs(workflow, **query):
    """One page of a workflow's runs, newest first, through `gh api`.

    Raises on a missing repository name, a failed call or unexpected JSON.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("GITHUB_REPOSITORY is not set")
    command = ["gh", "api", "-X", "GET"]
    command.append(f"repos/{repo}/actions/workflows/{workflow}/runs")
    for key, value in query.items():
        command += ["-f", f"{key}={value}"]
    raw = subprocess.check_output(command, stderr=subprocess.PIPE, timeout=120)
    runs = json.loads(raw)["workflow_runs"]
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        raise ValueError("unexpected workflow run listing")
    return runs


def is_ancestor(commit, head):
    """Whether `commit` is in `head`'s history; an unknown commit is not."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, head], capture_output=True
    )
    return result.returncode == 0


def last_green_push(head):
    """The newest completed, green push run of ios.yml on main whose commit is
    an ancestor of `head`, or None.

    Every field is checked here, not trusted to the API's filters: a
    cancelled, failed, running, manual or nightly run never proves the push
    lane (build, unit, smoke, TestFlight dispatch) passed for its commit.
    """
    for run in workflow_runs(
        IOS_WORKFLOW, branch="main", event="push", status="success", per_page=50
    ):
        if (
            run.get("event") == "push"
            and run.get("head_branch") == "main"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("path", "").split("@", 1)[0]
            == f".github/workflows/{IOS_WORKFLOW}"
            and isinstance(run.get("head_sha"), str)
            and FULL_SHA.fullmatch(run["head_sha"])
            and is_ancestor(run["head_sha"], head)
        ):
            return run
    return None


def unfinished_release():
    """The newest TestFlight run on main, when it ended without success.

    Before KRI-1547 every push to main dispatched TestFlight, so a failed
    release was retried by the next push. A run still in progress covers a
    newer tested commit than any run before it and is not a reason to start.
    """
    runs = [
        run
        for run in workflow_runs(TESTFLIGHT_WORKFLOW, branch="main", per_page=10)
        if run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch"
    ]
    if runs and runs[0].get("status") == "completed":
        if runs[0].get("conclusion") != "success":
            return runs[0]
    return None


def main_push_native(head):
    """Does this push to main start ios.yml's Mac lane? Returns (start, note).

    The pushed commit is compared with the commit of the last green ios.yml
    push run, not with its parent, so a change whose own run was cancelled or
    failed still gets its Mac on the next push. Every unknown runs the Mac:
    no green ancestor run, an API or git error, an unclassifiable path, the
    rollback variable, or a TestFlight run that ended without success.
    """
    if selection_rollback():
        return True, "CI_MAC_SELECTION=full keeps the Mac on every push"
    try:
        if not FULL_SHA.fullmatch(head or ""):
            raise ValueError("the pushed commit is not a full SHA")
        release = unfinished_release()
        if release is not None:
            return True, (
                f"the last TestFlight run ({release.get('id')}) ended "
                f"{release.get('conclusion')}, so the Mac lane runs to dispatch "
                "the release again"
            )
        baseline = last_green_push(head)
        if baseline is None:
            return True, (
                "no green iOS push run on main to compare with, so the Mac runs"
            )
        since = f"{baseline['head_sha'][:9]} (iOS push run {baseline.get('id')})"
        if inputs_changed_between(baseline["head_sha"], head, main_push_input):
            return True, f"changed since {since}"
        return False, (
            f"unchanged since {since}: no Mac, no UI smoke and no TestFlight "
            "dispatch on this push; the Linux contract leg still runs and the "
            "nightly runs the full native suite"
        )
    except HISTORY_ERRORS as error:
        return True, (
            f"run history or diff unavailable ({type(error).__name__}), "
            "so the Mac runs"
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
    """UI legs for the executed selection.

    Main runs smoke on one Mac. PRs run `full` as smoke, so it stays on one leg;
    a longer PR selection gets one leg per ui_tests.PR_SHARD_TARGET_SECONDS, at
    most ui_tests.PR_MAX_SHARDS.
    Nightly/manual shard the full suite in ios.yml.
    """
    if event == "push" or groups == "none":
        return 1
    if event == "pull_request" and groups == "full":
        return 1
    if event == "pull_request":
        target, cap = ui_tests.PR_SHARD_TARGET_SECONDS, ui_tests.PR_MAX_SHARDS
    else:
        target, cap = ui_tests.SHARD_TARGET_SECONDS, ui_tests.MAX_SHARDS
    try:
        return ui_tests.shard_count(groups, target=target, max_shards=cap)
    except (OSError, ValueError, KeyError, TypeError):
        # The UI step fails closed on the same error; one leg reports it.
        return 1


def native_validation(event, suites, journey_native, native_inputs=True):
    """PR/manual/nightly native proof; main's release workflow owns its Mac.

    A pull request starts a Mac only when it changed an iPhone input
    (KRI-1534): otherwise the Mac would rebuild and retest what `main` already
    did. Its Linux contract and journey jobs run either way.
    """
    if event == "push" or (event == "pull_request" and not native_inputs):
        return False
    return "ios" in suites or journey_native


def validate_journey_mapping(coverage, ids):
    selected = ids.split(",") if ids else []
    if coverage != "covered" or not selected or len(selected) != len(set(selected)):
        raise ValueError("Journey selection has an unmapped or invalid incident")
    journey_native_for_ids(selected)
    return selected


def journey_mapped(journey, coverage, ids):
    """Whether iPhone validation may start without waiting for journey-mapping.

    The same check the journey-mapping job runs, computed by the selector so the
    Mac job can queue straight after selection (KRI-604 still holds: an unmapped
    selection never allocates a Mac). journey-mapping stays the visible failure.
    """
    if not journey:
        return True
    try:
        validate_journey_mapping(coverage, ids)
    except ValueError:
        return False
    return True


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
    for key in (
        "journey",
        "ios",
        "ios_native",
        "journey_needs_native",
        "native_validation",
    ):
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
    # journey-native queues on this output instead of the journey-mapping job;
    # a mapped (or unselected) journey is the only state that may emit "true".
    mapped = journey_mapped(
        journey, outputs.get("journey_coverage"), outputs.get("journey_ids")
    )
    if outputs.get("journey_mapped") != str(mapped).lower():
        raise ValueError("Missing or inconsistent journey_mapped")
    native = native_validation(
        event,
        {"ios"} if outputs["ios"] == "true" else set(),
        outputs["journey_needs_native"] == "true",
        outputs["ios_native"] == "true",
    )
    if outputs["native_validation"] != str(native).lower():
        raise ValueError("Native execution does not match event and selection")
    # journey-native's result covers only the legs its matrix started, so the
    # matrix itself must be every leg this selection needs: a dropped UI shard
    # would otherwise pass with its tests never run.
    groups = ui_tests.validate_execution(outputs.get("ios_ui_groups"))
    shards = ui_shards(event, groups)
    if outputs.get("ios_ui_shards") != str(shards):
        raise ValueError("Missing or inconsistent iOS UI shard count")
    if native and outputs.get("native_legs") != json.dumps(
        native_legs(selected, event, shards)
    ):
        raise ValueError("Missing or inconsistent iPhone validation legs")
    for job, selected in (
        ("journey-mapping", journey),
        ("journey-offline", journey),
        ("journey-library", "kri-384-library-ingest" in selected),
        ("journey-native", native),
    ):
        expected = "success" if selected else "skipped"
        if needs[job].get("result") != expected:
            raise ValueError(
                unexpected_result(job, expected, needs[job].get("result"))
            )
    return "Selected portable and native journey dependencies passed."


def unexpected_result(job, expected, result):
    """Gate failure text. `abandoned` is GitHub's result for a job whose hosted
    runner was never acquired ("The job was not acquired by Runner of type hosted
    even after multiple attempts", macOS capacity): no step ran, so say so rather
    than leave it looking like a test failure. The gate stays fail-closed."""
    message = f"{job}: expected {expected}, got {result}"
    if result == "abandoned":
        message += (
            "; GitHub never assigned a hosted runner to this job, so none of its "
            "tests ran. Re-run the workflow (not a test failure)."
        )
    return message


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
    elif suite == "lint":
        # ci.yml runs the CI self-tests beside the suites, not before selection,
        # so this required gate is where a red self-test stops the PR.
        required_needs = {"changes", job, "ci-selftests"}
    else:
        required_needs = {"changes", job}
    if set(needs) != required_needs or needs["changes"].get("result") != "success":
        raise ValueError("CI selection failed or required job results are missing")
    if suite == "lint" and needs["ci-selftests"].get("result") != "success":
        raise ValueError(
            unexpected_result(
                "ci self-tests", "success", needs["ci-selftests"].get("result")
            )
        )
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
        raise ValueError(unexpected_result(suite, expected, result))
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
    legs = native_legs(selected_ids, event, shards)
    # Repository variable CI_MAC_SELECTION=full is the rollback: every selected
    # PR gets its Mac again, with no code change.
    mac_rule = not selection_rollback()
    inputs_changed = native_inputs_changed(
        event, os.environ.get("CI_BASE", ""), os.environ.get("CI_HEAD", "")
    )
    native_inputs = inputs_changed or not mac_rule
    # ios.yml asks for the main-push Mac rule (KRI-1547); ci.yml never does,
    # so Linux push CI keeps exactly what it selected before.
    push_note = None
    if event == "push" and os.environ.get("CI_PUSH_NATIVE_SELECTION") == "true":
        native_inputs, push_note = main_push_native(os.environ.get("CI_SHA", ""))
    mac_runs = native_validation(event, suites, native, native_inputs)
    output += f"native_validation={str(mac_runs).lower()}\n"
    output += f"ios_ui_groups={groups}\nios_ui_shards={shards}\njourney={str(journey).lower()}\njourney_coverage={coverage}\njourney_ids={journey_ids}\njourney_needs_native={str(native).lower()}\n"
    output += f"native_legs={json.dumps(legs)}\n"
    mapped = journey_mapped(journey, coverage, journey_ids)
    output += f"journey_mapped={str(mapped).lower()}\n"
    render = render_evidence_selection(
        event, os.environ.get("CI_BASE", ""), os.environ.get("CI_HEAD", "")
    )
    output += f"ios_render_evidence={str(render).lower()}\n"
    output += f"ios_native={str(native_inputs).lower()}\n"
    inputs_note = (
        push_note
        if push_note is not None
        else "changed"
        if inputs_changed
        else "unchanged, but CI_MAC_SELECTION=full keeps the Mac"
        if not mac_rule
        else "unchanged (no Mac: this PR changes nothing the iPhone build or its"
        " tests read; Linux contracts and journeys still run)"
    )
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
                f"iPhone inputs: {inputs_note}\n\n"
                + "\n".join(
                    f"- {s}: {'run' if s in suites else 'not applicable'}"
                    for s in SUITES
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
