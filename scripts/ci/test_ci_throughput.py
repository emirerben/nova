"""KRI-605 failure contracts, written before the workflow implementation."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(filename)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = load("throughput_selector", "select-tests.py")
evidence = load("throughput_evidence", "journey-gate.py")


class XcodeCacheTests(unittest.TestCase):
    """A restored SDK module must belong to this image and selected Xcode."""

    def cache(self, workflow, image="20260907.0337.1", xcode="/Applications/Xcode.app"):
        source = (Path(__file__).resolve().parents[2] / ".github/workflows" / workflow).read_text()
        identify = source.split("- name: Identify Xcode cache", 1)[1]
        script = re.search(r"run: \|\n((?:          [^\n]*\n)+)", identify).group(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in {
                "xcodebuild": "printf 'Xcode 26.3\\nBuild version 17C529\\n'",
                "xcode-select": 'printf "%s\\n" "$CACHE_TEST_XCODE"',
            }.items():
                command = root / name
                command.write_text("#!/bin/bash\n" + body + "\n")
                command.chmod(0o755)
            output = root / "output"
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                   "GITHUB_OUTPUT": str(output), "CACHE_TEST_XCODE": xcode}
            env.pop("ImageVersion", None)
            if image is not None:
                env["ImageVersion"] = image
            result = subprocess.run(
                ["bash", "-eo", "pipefail", "-c", textwrap.dedent(script)],
                env=env, capture_output=True, text=True,
            )
            values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
        if result.returncode:
            return result, None, None
        key = re.search(r"key: (ios-build[^\n]+)", source).group(1)
        prefix = re.search(r"restore-keys: \|\n\s+(ios-build[^\n]+)", source).group(1)

        def render(match):
            expression = match.group(1).strip()
            fixed = {"runner.os": "macOS", "runner.arch": "ARM64", "github.sha": "source-sha"}
            if expression == "steps.xcode-inputs.outputs.hash":
                return "project-hash"
            if expression.startswith("steps.xcode-cache.outputs."):
                return values[expression.rsplit(".", 1)[1]]
            return fixed[expression]

        return result, re.sub(r"\$\{\{(.*?)\}\}", render, key), re.sub(r"\$\{\{(.*?)\}\}", render, prefix)

    def test_sdk_cache_cannot_cross_runner_images_even_with_identical_xcode_versions(self):
        for workflow in ("ci.yml", "ios.yml"):
            with self.subTest(workflow=workflow):
                _, september, september_prefix = self.cache(workflow)
                _, october, october_prefix = self.cache(workflow, image="20261028.0489.1")
                self.assertTrue(september.startswith(september_prefix))
                self.assertTrue(october.startswith(october_prefix))
                self.assertFalse(september.startswith(october_prefix))
                self.assertFalse(october.startswith(september_prefix))

    def test_selected_xcode_path_is_part_of_cache_identity(self):
        for workflow in ("ci.yml", "ios.yml"):
            with self.subTest(workflow=workflow):
                _, stable, stable_prefix = self.cache(workflow)
                _, alternate, alternate_prefix = self.cache(workflow, xcode="/Applications/Other Xcode.app")
                self.assertFalse(stable.startswith(alternate_prefix))
                self.assertFalse(alternate.startswith(stable_prefix))

    def test_unknown_image_cannot_silently_restore_an_old_sdk_cache(self):
        for workflow in ("ci.yml", "ios.yml"):
            with self.subTest(workflow=workflow):
                result, _, _ = self.cache(workflow, image=None)
                self.assertNotEqual(result.returncode, 0)


class ThroughputTests(unittest.TestCase):
    def test_new_ruled_out_clips_journey_runs_on_linux(self):
        workflow = (
            Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
        ).read_text()
        portable, native = workflow.split("  journey-native:", 1)
        step = "journey_registry.py step kri-623-ruled-out-clips"
        self.assertIn(step, portable)
        self.assertNotIn(step, native)
        registry = importlib.util.spec_from_file_location(
            "journey_registry", Path(__file__).with_name("journey_registry.py")
        )
        module = importlib.util.module_from_spec(registry)
        registry.loader.exec_module(module)
        self.assertEqual(
            module.step_files("kri-623-ruled-out-clips"),
            ["tests/kria/test_kri623_ruled_out_clips.py"],
        )

    def native_steps(self):
        """[(name, if-expression)] for every step of ci.yml's journey-native job."""
        import re

        workflow = (
            Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
        ).read_text()
        job = workflow.split("\n  journey-native:\n", 1)[1].split("\n  journey:\n", 1)[
            0
        ]
        # The fallback literal keeps the matrix valid when selection is absent;
        # the job's own `if` already requires a successful selector.
        self.assertIn(
            "leg: ${{ fromJSON(needs.changes.outputs.native_legs || '[\"unit\"]') }}",
            job,
        )
        self.assertIn("fail-fast: false", job)
        steps = []
        for block in re.split(r"\n      - (?=name: |uses: )", job)[1:]:
            name = re.match(r"(?:name|uses): (.*)", block).group(1)
            condition = re.search(r"\n        if: (.*)", block)
            steps.append((name, condition.group(1) if condition else ""))
        return steps

    def test_pr_native_legs_split_journeys_without_dropping_any(self):
        # One serial Mac took 40-55 min per PR (2026-10-23: compile+unit 13-22,
        # then ~27 min of journeys one after another). Each leg compiles from
        # the shared cache and runs only its journeys; the selector starts
        # only the legs whose journeys were selected.
        import re

        mapped = {
            journey: leg
            for leg, journeys in ci.LEG_JOURNEYS.items()
            for journey in journeys
        }
        self.assertNotIn("unit", ci.LEG_JOURNEYS)
        seen = set()
        for name, condition in self.native_steps():
            journeys = re.findall(
                r"contains\(needs\.changes\.outputs\.journey_ids, '([^']+)'\)",
                condition,
            )
            legs = re.findall(r"matrix\.leg == '([^']+)'", condition)
            with self.subTest(step=name):
                if journeys:
                    self.assertEqual(len(legs), 1, "journey step must name one leg")
                    for journey in journeys:
                        self.assertEqual(mapped.get(journey), legs[0])
                    seen.update(journeys)
                else:
                    self.assertLessEqual(len(legs), 1)
        self.assertEqual(seen, set(mapped), "leg map and workflow steps disagree")
        unit_only = {
            "Run selected iPhone UI scenarios",
            "Verify selected named native unit journeys",
        }
        every_leg = {
            "Build iPhone app and run selected unit tests",
            "Reuse the same simulator for selected native journeys",
            "Restore Xcode build data",
        }
        conditions = dict(self.native_steps())
        for name in unit_only:
            with self.subTest(step=name):
                self.assertIn("matrix.leg == 'unit'", conditions[name])
        for name in every_leg:
            with self.subTest(step=name):
                self.assertNotIn("matrix.leg", conditions[name])
        # Every artifact name carries the leg so legs never overwrite each other.
        job = (
            (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml")
            .read_text()
            .split("\n  journey-native:\n", 1)[1]
            .split("\n  journey:\n", 1)[0]
        )
        self.assertIn("name: ios-native-test-diagnostics-${{ matrix.leg }}", job)
        self.assertIn("-${{ github.sha }}-${{ matrix.leg }}", job)
        self.assertEqual(job.count("upload-artifact@v4"), 2)
        # PR runs only restore main's Xcode cache (ios.yml seeds it). Each PR
        # save (~0.9 GB per push) evicted main's seed from the full 10 GB repo
        # cache within 30-46 minutes (2026-11-04..06): 70 of 97 PR runs
        # compiled cold, and only 6 of 27 hits came from a PR's own save.
        self.assertIn("actions/cache/restore@v4", job)
        self.assertNotIn("actions/cache/save", job)
        self.assertNotIn("cache-inputs.py save", job)

    def test_mac_queues_after_selection_but_never_for_an_unmapped_journey(self):
        # Failure modes: an unmapped selection allocates a Mac (KRI-604); an
        # iOS-only PR (no journey) loses its Mac; the output never reaches the
        # job, which then silently skips; the Mac waits on the mapping job again.
        self.assertTrue(ci.journey_mapped(False, "not_applicable", ""))
        self.assertTrue(ci.journey_mapped(True, "covered", "kri-524-creation"))
        self.assertFalse(ci.journey_mapped(True, "gap", ""))
        self.assertFalse(ci.journey_mapped(True, "covered", ""))
        self.assertFalse(
            ci.journey_mapped(True, "covered", "kri-524-creation,kri-524-creation")
        )
        self.assertFalse(ci.journey_mapped(True, "covered", "not-a-journey"))
        everything = ",".join(journey["id"] for journey in ci.JOURNEYS)
        self.assertTrue(ci.journey_mapped(True, "covered", everything))
        root = Path(__file__).resolve().parents[2] / ".github/workflows"
        changes = (root / "ci-changes.yml").read_text()
        self.assertIn("value: ${{ jobs.select.outputs.journey_mapped }}", changes)
        self.assertIn(
            "journey_mapped: ${{ steps.select.outputs.journey_mapped }}", changes
        )
        job = (root / "ci.yml").read_text().split("\n  journey-native:\n", 1)[1]
        header = job.split("\n    steps:\n", 1)[0]
        self.assertIn("\n    needs: [changes]\n", header)
        self.assertIn("needs.changes.outputs.journey_mapped == 'true'", header)
        self.assertNotIn("journey-mapping.result", header)

    def run_ui_step(self, leg, shards):
        """Run the workflow's UI step script against a stub verify.sh."""
        job = (
            (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml")
            .read_text()
            .split("\n  journey-native:\n", 1)[1]
            .split("\n  journey:\n", 1)[0]
        )
        step = job.split("- name: Run selected iPhone UI scenarios\n", 1)[1]
        step = re.split(r"\n      - ", step, 1)[0]
        script = re.search(r"run: \|\n((?:          [^\n]*\n?)+)", step).group(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts/ios").mkdir(parents=True)
            (root / "scripts/ios/verify.sh").write_text(
                'printf "%s|%s" "${KRIA_IOS_UI_SHARD-unset}" '
                '"${KRIA_IOS_UI_SHARD_OVERHEAD_SECONDS-unset}"\n'
            )
            env = {**os.environ, "LEG": leg, "UI_SHARDS": shards}
            env.pop("KRIA_IOS_UI_SHARD", None)
            return subprocess.run(
                ["bash", "-eo", "pipefail", "-c", textwrap.dedent(script)],
                cwd=root, env=env, capture_output=True, text=True,
            )

    def test_each_ui_leg_runs_exactly_its_shard(self):
        # Failure modes: two legs running the same shard (one part never
        # runs), the unit leg repeating a shard, or a shard partition that
        # still reserves time for unit tests the UI legs never run.
        for leg, shards, expected in (
            ("ui-1", "3", "1/3|0"),
            ("ui-2", "3", "2/3|0"),
            ("ui-3", "3", "3/3|0"),
            ("ui-2", "2", "2/2|0"),
            ("unit", "1", "unset|unset"),
        ):
            with self.subTest(leg=leg, shards=shards):
                result = self.run_ui_step(leg, shards)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
        conditions = dict(self.native_steps())
        ui = conditions["Run selected iPhone UI scenarios"]
        # The unit leg keeps the UI only when the selection is one shard.
        self.assertIn(
            "(matrix.leg == 'unit' && needs.changes.outputs.ios_ui_shards == '1')", ui
        )
        self.assertIn("startsWith(matrix.leg, 'ui-')", ui)
        # A UI leg never waits for, or needs, the portable journey fixtures.
        for name in (
            "Wait for the portable journeys this leg depends on",
            "Download this attempt's portable fixture inputs",
            "Check fixture provenance and transferred bytes",
        ):
            with self.subTest(step=name):
                self.assertIn("!startsWith(matrix.leg, 'ui-')", conditions[name])
        # A UI leg whose UI step was skipped must not report success.
        guard = conditions["Require this UI leg's shard to have run"]
        self.assertIn("always()", guard)
        self.assertIn("startsWith(matrix.leg, 'ui-')", guard)

    def test_selector_starts_only_the_legs_with_selected_journeys(self):
        self.assertEqual(ci.native_legs([]), ["unit"])
        self.assertEqual(ci.native_legs(["kri-524-creation"]), ["unit", "creation"])
        self.assertEqual(
            ci.native_legs(["kri-648-phone-text", "kri-492-length-ownership"]),
            ["unit", "creation", "screens"],
        )
        # Journeys proven by named unit tests alone stay on the unit leg.
        self.assertEqual(ci.native_legs(["kri-557-thinking"]), ["unit"])
        everything = [journey["id"] for journey in ci.JOURNEYS]
        self.assertEqual(ci.native_legs(everything), ["unit", "creation", "screens"])
        with self.assertRaises(ValueError):
            ci.native_legs(["not-a-journey"])
        # Nightly and manual CI keep every journey leg.
        for event in ("schedule", "workflow_dispatch"):
            self.assertEqual(
                ci.native_legs(everything, event), ["unit", "creation", "screens"]
            )
        # PRs start one Mac: build, unit tests, UI smoke and named-unit gate.
        # Fixture-driven journeys still run portably on Linux for the PR.
        self.assertEqual(ci.native_legs(everything, "pull_request"), ["unit"])
        self.assertEqual(ci.native_legs(["kri-524-creation"], "pull_request"), ["unit"])
        with self.assertRaises(ValueError):
            ci.native_legs(["not-a-journey"], "pull_request")
        changes = (
            Path(__file__).resolve().parents[2] / ".github/workflows/ci-changes.yml"
        ).read_text()
        self.assertIn("value: ${{ jobs.select.outputs.native_legs }}", changes)
        self.assertIn("native_legs: ${{ steps.select.outputs.native_legs }}", changes)

    def test_only_main_push_defers_native_regression(self):
        for event in ("pull_request", "schedule", "workflow_dispatch", "unknown"):
            self.assertTrue(ci.native_validation(event, {"ios"}, False))
            self.assertTrue(ci.native_validation(event, {"api"}, True))
            self.assertFalse(ci.native_validation(event, {"api"}, False))
        self.assertFalse(ci.native_validation("push", {"ios"}, True))
        self.assertEqual(ci.selection_details("push", "", "")[2], "smoke")
        self.assertEqual(ci.ui_shards("push", "full"), 1)
        self.assertEqual(ci.ui_shards("schedule", "full"), 5)

    def test_abandoned_runner_is_reported_as_infrastructure_and_still_fails(self):
        # KRI-1379: macOS capacity left jobs with no runner ("abandoned"). That is
        # not a test failure, but it must still fail the gate: no test ran.
        needs = {
            "changes": {
                "result": "success",
                "outputs": {
                    "ios": "true",
                    "ios_ui": "true",
                    "ios_ui_groups": "smoke",
                    "ios_ui_shards": "1",
                },
            },
            "ios-tests": {"result": "abandoned"},
        }
        with self.assertRaises(ValueError) as caught:
            ci.gate(needs, "ios", "ios-tests", event="push")
        self.assertIn("expected success, got abandoned", str(caught.exception))
        self.assertIn("never assigned a hosted runner", str(caught.exception))
        needs["ios-tests"]["result"] = "cancelled"
        with self.assertRaises(ValueError) as caught:
            ci.gate(needs, "ios", "ios-tests", event="push")
        self.assertNotIn("hosted runner", str(caught.exception))

    def test_main_smoke_is_accepted_only_for_one_push_lane(self):
        needs = {
            "changes": {
                "result": "success",
                "outputs": {
                    "ios": "true",
                    "ios_ui": "true",
                    "ios_ui_groups": "smoke",
                    "ios_ui_shards": "1",
                },
            },
            "ios-tests": {"result": "success"},
        }
        ci.gate(needs, "ios", "ios-tests", event="push")
        needs["changes"]["outputs"]["ios_ui_shards"] = "3"
        with self.assertRaises(ValueError):
            ci.gate(needs, "ios", "ios-tests", event="push")
        needs["changes"]["outputs"]["ios_ui_shards"] = "1"
        for event in ("pull_request", "schedule", "workflow_dispatch"):
            with self.assertRaises(ValueError):
                ci.gate(needs, "ios", "ios-tests", event=event)

    def needs(self, journey="true", native="true", ios="true"):
        return {
            "changes": {
                "result": "success",
                "outputs": {
                    "journey": journey,
                    "ios": ios,
                    "native_validation": native,
                    "journey_needs_native": "true" if journey == "true" else "false",
                    "journey_coverage": "covered"
                    if journey == "true"
                    else "not_applicable",
                    "journey_ids": "kri-524-creation" if journey == "true" else "",
                    "journey_mapped": "true",
                    "ios_ui": "false",
                    "ios_ui_groups": "none",
                    "ios_ui_shards": "1",
                    "native_legs": '["unit"]',
                },
            },
            "journey-mapping": {
                "result": "success" if journey == "true" else "skipped"
            },
            "journey-offline": {
                "result": "success" if journey == "true" else "skipped"
            },
            "journey-library": {"result": "skipped"},
            "journey-native": {"result": "success" if native == "true" else "skipped"},
        }

    def test_selected_library_preparation_is_required_for_each_cadence(self):
        for event in ("pull_request", "push", "schedule", "workflow_dispatch"):
            needs = self.needs(native="false", ios="false")
            needs["changes"]["outputs"].update(
                journey_ids="kri-384-library-ingest",
                journey_needs_native="false",
            )
            needs["journey-library"]["result"] = "success"
            ci.gate_journey(needs, event)
            for result in ("failure", "cancelled", "skipped", None):
                needs["journey-library"]["result"] = result
                with (
                    self.subTest(event=event, result=result),
                    self.assertRaises(ValueError),
                ):
                    ci.gate_journey(needs, event)
        mixed = self.needs()
        mixed["changes"]["outputs"]["journey_ids"] += ",kri-384-library-ingest"
        mixed["journey-library"]["result"] = "success"
        ci.gate_journey(mixed, "pull_request")
        mixed["journey-library"]["result"] = "skipped"
        with self.assertRaises(ValueError):
            ci.gate_journey(mixed, "pull_request")
        unselected = self.needs()
        unselected["journey-library"]["result"] = "success"
        with self.assertRaises(ValueError):
            ci.gate_journey(unselected, "pull_request")

    def test_each_selected_dependency_is_required(self):
        ci.gate_journey(self.needs(), "pull_request")
        for job in ("journey-mapping", "journey-offline", "journey-native"):
            for result in ("failure", "cancelled", "skipped", None):
                needs = self.needs()
                needs[job]["result"] = result
                with (
                    self.subTest(job=job, result=result),
                    self.assertRaises(ValueError),
                ):
                    ci.gate_journey(needs, "pull_request")
        for job in self.needs():
            needs = self.needs()
            del needs[job]
            with self.assertRaises(ValueError):
                ci.gate_journey(needs, "pull_request")

    def test_explicit_skips_and_main_deferral(self):
        ci.gate_journey(self.needs(native="false"), "push")
        ci.gate_journey(self.needs(journey="false"), "pull_request")
        ci.gate_journey(
            self.needs(journey="false", native="false", ios="false"), "pull_request"
        )
        needs = self.needs(native="false")
        with self.assertRaises(ValueError):
            ci.gate_journey(needs, "pull_request")
        needs = self.needs(native="false")
        needs["changes"]["outputs"]["journey_needs_native"] = "false"
        needs["changes"]["outputs"]["ios"] = "false"
        needs["changes"]["outputs"]["journey_ids"] = "kri-586-hook-copy"
        ci.gate_journey(needs, "pull_request")
        needs["journey-native"]["result"] = "success"
        with self.assertRaises(ValueError):
            ci.gate_journey(needs, "pull_request")

    def test_missing_metadata_and_unmapped_ids_never_pass(self):
        for field in (
            "journey",
            "ios",
            "native_validation",
            "journey_needs_native",
            "journey_coverage",
            "journey_ids",
            "journey_mapped",
            "ios_ui_groups",
            "ios_ui_shards",
            "native_legs",
        ):
            needs = self.needs()
            del needs["changes"]["outputs"][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.gate_journey(needs, "pull_request")
        for ids in ("unknown", "kri-524-creation,kri-524-creation", ""):
            needs = self.needs()
            needs["changes"]["outputs"]["journey_ids"] = ids
            with self.assertRaises(ValueError):
                ci.gate_journey(needs, "pull_request")
        # A Mac may queue only for a mapped or unselected journey.
        for journey in ("true", "false"):
            needs = self.needs(journey=journey)
            needs["changes"]["outputs"]["journey_mapped"] = "false"
            with self.subTest(journey=journey), self.assertRaises(ValueError):
                ci.gate_journey(needs, "pull_request")


    def ui_needs(self, journey="false"):
        groups = "smoke,editor"
        shards = ci.ui_shards("pull_request", groups)
        needs = self.needs(journey=journey)
        needs["changes"]["outputs"].update(
            ios_ui="true",
            ios_ui_groups=groups,
            ios_ui_shards=str(shards),
            native_legs=json.dumps(
                ci.native_legs(
                    needs["changes"]["outputs"]["journey_ids"].split(",")
                    if journey == "true"
                    else [],
                    "pull_request",
                    shards,
                )
            ),
        )
        return needs, shards

    def test_every_pr_ui_shard_leg_is_required(self):
        # Failure modes: a matrix result only covers the legs that exist, so a
        # shard missing from native_legs would pass silently; a shard count that
        # disagrees with the selection runs a different partition; a failed,
        # cancelled or skipped shard leg turns the matrix result non-success.
        for journey in ("false", "true"):
            needs, shards = self.ui_needs(journey)
            legs = json.loads(needs["changes"]["outputs"]["native_legs"])
            self.assertEqual(
                legs[-shards:], [f"ui-{i}" for i in range(1, shards + 1)]
            )
            ci.gate_journey(needs, "pull_request")
            for bad in (
                legs[:-1],
                [leg for leg in legs if not leg.startswith("ui-")],
                legs + [f"ui-{shards + 1}"],
                legs[1:],
                [legs[0], *reversed(legs[1:])],
            ):
                broken, _ = self.ui_needs(journey)
                broken["changes"]["outputs"]["native_legs"] = json.dumps(bad)
                with self.subTest(legs=bad), self.assertRaises(ValueError):
                    ci.gate_journey(broken, "pull_request")
            for count in ("1", str(shards - 1), str(shards + 1), ""):
                broken, _ = self.ui_needs(journey)
                broken["changes"]["outputs"]["ios_ui_shards"] = count
                with self.subTest(shards=count), self.assertRaises(ValueError):
                    ci.gate_journey(broken, "pull_request")
            for result in ("failure", "cancelled", "skipped"):
                broken, _ = self.ui_needs(journey)
                broken["journey-native"]["result"] = result
                with self.subTest(result=result), self.assertRaises(ValueError):
                    ci.gate_journey(broken, "pull_request")

    def test_pr_ui_shards_run_on_their_own_legs(self):
        # The unit leg keeps build + unit tests; each UI shard gets a Mac.
        self.assertEqual(
            ci.native_legs([], "pull_request", 3), ["unit", "ui-1", "ui-2", "ui-3"]
        )
        self.assertEqual(
            ci.native_legs(["kri-524-creation"], "pull_request", 2),
            ["unit", "ui-1", "ui-2"],
        )
        self.assertEqual(ci.native_legs([], "pull_request", 1), ["unit"])
        self.assertEqual(ci.native_legs([], "pull_request"), ["unit"])
        # ci.yml runs UI only on PRs; nightly sharding belongs to ios.yml.
        for event in ("schedule", "workflow_dispatch", "push"):
            self.assertEqual(ci.native_legs([], event, 3), ["unit"])


class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "fixture").mkdir()
        (self.root / "fixture/e2e.json").write_text("{}")
        (self.root / "fixture/source.mp4").write_bytes(b"synthetic media")

    def seal(self):
        evidence.seal_bundle(self.root, "a" * 40, "123", "1", "kri-524-creation")

    def verify(self, head="a" * 40, run="123", attempt="1"):
        evidence.verify_bundle(self.root, head, run, attempt, "kri-524-creation")

    def test_transfer_preserves_paths_bytes_and_attempt_identity(self):
        self.seal()
        self.verify()
        for args in (
            ("b" * 40, "123", "1"),
            ("a" * 40, "456", "1"),
            ("a" * 40, "123", "2"),
        ):
            with self.assertRaises(ValueError):
                self.verify(*args)

    def test_missing_changed_or_added_inputs_fail(self):
        self.seal()
        media = self.root / "fixture/source.mp4"
        media.write_bytes(b"modified")
        with self.assertRaises(ValueError):
            self.verify()
        media.unlink()
        with self.assertRaises(ValueError):
            self.verify()
        media.write_bytes(b"synthetic media")
        (self.root / "unexpected.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.verify()

    def test_absent_manifest_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            self.verify()
        (self.root / "link").symlink_to(self.root / "fixture/source.mp4")
        with self.assertRaises(ValueError):
            self.seal()

    def test_named_tests_require_exact_passed_identifiers(self):
        report = {
            "testNodes": [
                {
                    "nodeType": "Unit test bundle",
                    "name": "KriaTests",
                    "children": [
                        {
                            "nodeType": "Test Case",
                            "nodeIdentifier": "Example/testWorks()",
                            "result": "Passed",
                        }
                    ],
                }
            ]
        }
        evidence.verify_named_tests(report, {"KriaTests/Example/testWorks"})
        with self.assertRaises(ValueError):
            evidence.verify_named_tests(report, {"KriaTests/Other/testWorks"})
        report["testNodes"][0]["children"][0]["result"] = "Skipped"
        with self.assertRaises(ValueError):
            evidence.verify_named_tests(report, {"KriaTests/Example/testWorks"})


if __name__ == "__main__":
    unittest.main()
