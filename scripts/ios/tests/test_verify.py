"""Offline behavioral tests for verify.sh; no Xcode or simulator is launched.

Run: python3 -m unittest discover -s scripts/ios/tests -v
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
root = pathlib.Path(os.environ['HARNESS_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'calls').open('a') as log:
    log.write(json.dumps([name, *args]) + '\n')
mode = os.environ.get('HARNESS_MODE', '')
if name == 'git':
    if args[0] == 'ls-files':
        if mode == 'fingerprint_failure':
            sys.exit(28)
        sys.stdout.write('src/apps/ios/source.swift\0')
    else:
        print(root)
elif name == 'xcodegen':
    sys.exit(12 if mode == 'generation_failure' else 0)
elif name == 'xcodebuild':
    if '-showBuildSettings' in args:
        config = args[args.index('-configuration') + 1]
        value = {'Debug': 'http://localhost:8000', 'Staging': 'https://staging.usekria.com', 'Release': 'https://nova-video.fly.dev'}[config]
        bad = (mode == 'bad_url' and config == 'Staging') or (mode == 'bad_debug_url' and config == 'Debug')
        print(' API_BASE_URL = ' + ('https:' if bad else value))
        sys.exit(29 if mode == 'settings_failure' else 0)
    elif args[-1] == 'build-for-testing':
        deadline = time.monotonic() + 5
        while not (root / 'boot_started').exists():
            if time.monotonic() > deadline:
                sys.exit(99)
            time.sleep(.01)
        sys.exit(23 if mode == 'build_failure' else 0)
    elif args[-1] == 'test-without-building':
        if not (root / 'boot_finished').exists():
            sys.exit(98)
        bundle = pathlib.Path(args[args.index('-resultBundlePath') + 1])
        bundle.mkdir()
        (bundle / 'args.json').write_text(json.dumps(args))
        sys.exit(25 if mode == 'test_failure' else 0)
    elif args[-1] == 'build':
        sys.exit(26 if mode == 'build_only_failure' else 0)
elif name == 'xcrun':
    if args[:4] == ['xcresulttool', 'get', 'test-results', 'tests']:
        bundle = pathlib.Path(args[args.index('--path') + 1])
        test_args = json.loads((bundle / 'args.json').read_text())
        manifest = json.loads((root / 'scripts/ios/ui-test-groups.json').read_text())
        tests = sorted(set(t for values in manifest['groups'].values() for t in values))
        selected = [arg.removeprefix('-only-testing:') for arg in test_args if arg.startswith('-only-testing:')]
        if selected != ['KriaUITests']:
            tests = selected
        if mode == 'empty_results':
            tests = []
        print(json.dumps({'testNodes': [{'nodeType': 'UI test bundle', 'name': 'KriaUITests', 'children': [
            {'nodeType': 'Test Case', 'nodeIdentifier': t.removeprefix('KriaUITests/') + '()', 'result': 'Passed'} for t in tests]}]}))
    elif args[:3] == ['simctl', 'list', 'devices']:
        if mode == 'list_failure':
            sys.exit(27)
        devices = {} if mode == 'no_simulator' else {
            'watchOS': [{'name': 'iPhone fake', 'isAvailable': True, 'udid': 'wrong'}],
            'iOS-18': [{'name': 'iPhone old', 'isAvailable': True, 'udid': 'old'}],
            'iOS-26': [
                {'name': 'iPad', 'isAvailable': True, 'udid': 'ipad'},
                {'name': 'iPhone unavailable', 'isAvailable': False, 'udid': 'off'},
                {'name': 'iPhone selected', 'isAvailable': True, 'udid': 'selected'}]}
        print(json.dumps({'devices': devices}))
    elif args[1] == 'bootstatus':
        if mode == 'build_failure':
            def stopped(*_):
                (root / 'boot_stopped').touch()
                sys.exit(0)
            signal.signal(signal.SIGTERM, stopped)
        (root / 'boot_started').touch()
        if mode == 'build_failure':
            signal.alarm(10)  # Bound the stub lifetime even if cleanup regresses.
            signal.pause()
        if mode == 'boot_failure':
            sys.exit(24)
        (root / 'boot_finished').touch()
"""


class VerifyShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kria verify ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts/ios"
        scripts.mkdir(parents=True)
        (self.root / "src/apps/ios").mkdir(parents=True)
        original = Path(__file__).resolve().parents[1]
        for name in (
            "verify.sh",
            "generate-project.sh",
            "cache-inputs.py",
            "ui_tests.py",
            "ui-test-groups.json",
        ):
            shutil.copy2(original / name, scripts / name)
        shutil.copytree(
            original.parents[1] / "src/apps/ios/Tests/KriaUITests",
            self.root / "src/apps/ios/Tests/KriaUITests",
        )
        (self.root / "src/apps/ios/source.swift").write_text("let value = 1")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("git", "xcodebuild", "xcrun", "xcodegen"):
            path = self.bin / name
            path.write_text(STUB)
            path.chmod(0o755)

    def run_verify(
        self,
        mode="",
        build_only=False,
        suite="full",
        restore_times=False,
        simulator_id="",
        groups="full",
    ):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HARNESS_ROOT": str(self.root),
            "HARNESS_MODE": mode,
            "KRIA_IOS_TEST_MODE": suite,
            "KRIA_IOS_UI_GROUPS": groups,
            "KRIA_SIMULATOR_ID": simulator_id,
            "KRIA_RESTORE_INPUT_TIMES": "1" if restore_times else "0",
            "KRIA_SKIP_SIMULATOR_TESTS": "1" if build_only else "0",
        }
        if groups is None:
            env.pop("KRIA_IOS_UI_GROUPS")
        result = subprocess.run(
            ["bash", str(self.root / "scripts/ios/verify.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.calls = [
            json.loads(line) for line in (self.root / "calls").read_text().splitlines()
        ]
        (self.root / "calls").unlink()
        self.actions = [
            call[-1]
            for call in self.calls
            if call[0] == "xcodebuild" and "-showBuildSettings" not in call
        ]
        return result

    def test_full_gate_builds_once_then_tests_serially_on_same_destination(self):
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.actions,
            ["build-for-testing", "test-without-building", "test-without-building"],
        )
        for call in self.calls:
            if call[0] == "xcodebuild" and call[-1] in self.actions:
                self.assertEqual(
                    call[call.index("-destination") + 1],
                    "platform=iOS Simulator,id=selected",
                )
                self.assertEqual(
                    call[call.index("-derivedDataPath") + 1],
                    str(self.root / "src/apps/ios/.derived-data"),
                )
        test = next(call for call in self.calls if call[-1] == "test-without-building")
        self.assertEqual(test[test.index("-parallel-testing-enabled") + 1], "NO")

    def test_unit_mode_filters_ui_tests_for_both_xcode_actions(self):
        result = self.run_verify(suite="unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions, ["build-for-testing", "test-without-building"])
        for call in self.calls:
            if call[0] == "xcodebuild" and call[-1] in self.actions:
                self.assertIn("-skip-testing:KriaUITests", call)
        self.assertFalse(
            (self.root / "src/apps/ios/.derived-data/.ci-ui-build").exists()
        )

    def test_prepare_then_ui_reuses_same_build_and_destination(self):
        prepared = self.run_verify(suite="prepare-ui", restore_times=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        build = next(call for call in self.calls if call[-1] == "build-for-testing")
        unit = next(call for call in self.calls if call[-1] == "test-without-building")
        self.assertFalse(
            any(arg.startswith(("-only-testing:", "-skip-testing:")) for arg in build)
        )
        self.assertIn("-skip-testing:KriaUITests", unit)
        ui = self.run_verify(suite="ui")
        self.assertEqual(ui.returncode, 0, ui.stderr)
        self.assertEqual(self.actions, ["test-without-building"])
        call = next(call for call in self.calls if call[-1] == "test-without-building")
        self.assertIn("-only-testing:KriaUITests", call)
        self.assertEqual(
            call[call.index("-destination") + 1], "platform=iOS Simulator,id=selected"
        )
        self.assertFalse(
            any(
                call[0] == "xcodegen" or call[:2] == ["xcrun", "simctl"]
                for call in self.calls
            )
        )
        self.assertNotEqual(self.run_verify(suite="ui").returncode, 0)

    def test_focused_filters_are_exact_and_deduplicated(self):
        self.assertEqual(self.run_verify(suite="prepare-ui").returncode, 0)
        result = self.run_verify(suite="ui", groups="smoke,creation")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((self.root / "scripts/ios/ui-test-groups.json").read_text())
        expected = set(data["groups"]["smoke"] + data["groups"]["creation"])
        call = next(c for c in self.calls if c[-1] == "test-without-building")
        filters = [
            a.removeprefix("-only-testing:")
            for a in call
            if a.startswith("-only-testing:")
        ]
        self.assertEqual(filters, sorted(expected))
        self.assertEqual(call[call.index("-parallel-testing-enabled") + 1], "NO")

    def test_invalid_or_empty_selection_never_runs_xcode(self):
        for groups in (
            None,
            "",
            "none",
            "smoke,typo",
            "creation",
            "smoke,editor,creation",
        ):
            with self.subTest(groups=groups):
                self.assertEqual(self.run_verify(suite="prepare-ui").returncode, 0)
                self.assertNotEqual(
                    self.run_verify(suite="ui", groups=groups).returncode, 0
                )
                self.assertEqual(self.actions, [])

    def test_zero_executed_tests_cannot_pass(self):
        self.assertEqual(self.run_verify(suite="prepare-ui").returncode, 0)
        result = self.run_verify("empty_results", suite="ui")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("coverage mismatch", result.stderr)

    def test_full_gate_has_distinct_result_bundles_and_timings(self):
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [c for c in self.calls if c[-1] == "test-without-building"]
        paths = [c[c.index("-resultBundlePath") + 1] for c in calls]
        self.assertEqual(
            [Path(p).name for p in paths], ["unit.xcresult", "ui.xcresult"]
        )
        self.assertIn("-skip-testing:KriaUITests", calls[0])
        self.assertIn("-only-testing:KriaUITests", calls[1])
        self.assertIn("Compilation", result.stdout)
        self.assertIn("Unit execution", result.stdout)
        self.assertIn("UI execution", result.stdout)

    def test_ui_requires_successful_current_build(self):
        self.assertNotEqual(self.run_verify(suite="ui").returncode, 0)
        self.assertEqual(self.run_verify(suite="prepare-ui").returncode, 0)
        (self.root / "src/apps/ios/source.swift").write_text("let value = 2")
        self.assertNotEqual(self.run_verify(suite="ui").returncode, 0)
        self.assertEqual(self.actions, [])

    def test_failed_unit_phase_does_not_authorize_ui(self):
        self.assertEqual(self.run_verify(suite="prepare-ui").returncode, 0)
        self.assertEqual(
            self.run_verify("test_failure", suite="prepare-ui").returncode, 25
        )
        self.assertNotEqual(self.run_verify(suite="ui").returncode, 0)

    def test_fingerprint_failure_does_not_authorize_ui(self):
        self.assertNotEqual(
            self.run_verify("fingerprint_failure", suite="prepare-ui").returncode, 0
        )
        self.assertNotEqual(self.run_verify(suite="ui").returncode, 0)

    def test_ui_failure_propagates(self):
        self.assertEqual(self.run_verify(suite="prepare-ui").returncode, 0)
        self.assertEqual(self.run_verify("test_failure", suite="ui").returncode, 25)

    def test_explicit_simulator_is_used_and_invalid_override_fails(self):
        result = self.run_verify(suite="unit", simulator_id="old")
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.calls:
            if call[0] == "xcodebuild" and call[-1] in self.actions:
                self.assertEqual(
                    call[call.index("-destination") + 1],
                    "platform=iOS Simulator,id=old",
                )
        self.assertNotEqual(self.run_verify(simulator_id="missing").returncode, 0)
        self.assertEqual(self.actions, [])

    def test_invalid_mode_stops_before_build(self):
        self.assertEqual(self.run_verify(suite="typo").returncode, 2)
        self.assertEqual(self.actions, [])

    def test_build_failure_stops_background_boot_and_never_tests(self):
        result = self.run_verify("build_failure")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.actions, ["build-for-testing"])
        self.assertTrue((self.root / "boot_stopped").exists())

    def test_boot_failure_never_tests(self):
        result = self.run_verify("boot_failure")
        self.assertEqual(result.returncode, 24, result.stderr)
        self.assertEqual(self.actions, ["build-for-testing"])

    def test_test_failure_propagates(self):
        self.assertEqual(self.run_verify("test_failure").returncode, 25)
        self.assertEqual(self.actions, ["build-for-testing", "test-without-building"])

    def test_build_only_never_touches_simulator(self):
        result = self.run_verify(build_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions, ["build"])
        self.assertFalse(any(call[0] == "xcrun" for call in self.calls))
        build = next(call for call in self.calls if call[-1] == "build")
        self.assertEqual(
            build[build.index("-destination") + 1], "generic/platform=iOS Simulator"
        )

    def test_configuration_queries_use_the_cached_package_directory(self):
        self.assertEqual(self.run_verify(build_only=True).returncode, 0)
        queries = [call for call in self.calls if "-showBuildSettings" in call]
        self.assertEqual(len(queries), 3)
        for query in queries:
            self.assertEqual(
                query[query.index("-derivedDataPath") + 1],
                str(self.root / "src/apps/ios/.derived-data"),
            )
            self.assertIn("-skipPackagePluginValidation", query)

    def test_build_only_failure_propagates(self):
        self.assertEqual(
            self.run_verify("build_only_failure", build_only=True).returncode, 26
        )
        self.assertEqual(self.actions, ["build"])

    def test_no_simulator_stops_before_build(self):
        result = self.run_verify("no_simulator")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No available iPhone simulator found", result.stderr)
        self.assertEqual(self.actions, [])

    def test_simulator_list_failure_stops_before_build(self):
        self.assertNotEqual(self.run_verify("list_failure").returncode, 0)
        self.assertEqual(self.actions, [])

    def test_failed_settings_query_cannot_pass_with_valid_stdout(self):
        result = self.run_verify("settings_failure")
        self.assertEqual(result.returncode, 29)
        self.assertEqual(self.actions, [])

    def test_bad_api_url_stops_before_build(self):
        result = self.run_verify("bad_url")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected Staging API_BASE_URL=", result.stderr)
        self.assertEqual(self.actions, [])

    def test_truncated_debug_api_url_stops_before_build(self):
        result = self.run_verify("bad_debug_url")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Expected Debug to resolve a complete HTTP(S) API URL", result.stderr
        )
        self.assertEqual(self.actions, [])

    def test_generation_failure_stops_before_xcode(self):
        self.assertEqual(self.run_verify("generation_failure").returncode, 12)
        self.assertFalse(any(call[0] == "xcodebuild" for call in self.calls))


if __name__ == "__main__":
    unittest.main()
