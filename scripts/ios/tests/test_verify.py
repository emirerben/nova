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
        value = {'Staging': 'https://staging.usekria.com', 'Release': 'https://nova-video.fly.dev'}[config]
        print(' API_BASE_URL = ' + ('https:' if mode == 'bad_url' else value))
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
        sys.exit(25 if mode == 'test_failure' else 0)
    elif args[-1] == 'build':
        sys.exit(26 if mode == 'build_only_failure' else 0)
elif name == 'xcrun':
    if args[:3] == ['simctl', 'list', 'devices']:
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
        for name in ("verify.sh", "generate-project.sh", "cache-inputs.py"):
            shutil.copy2(original / name, scripts / name)
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
    ):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HARNESS_ROOT": str(self.root),
            "HARNESS_MODE": mode,
            "KRIA_IOS_TEST_MODE": suite,
            "KRIA_SIMULATOR_ID": simulator_id,
            "KRIA_RESTORE_INPUT_TIMES": "1" if restore_times else "0",
            "KRIA_SKIP_SIMULATOR_TESTS": "1" if build_only else "0",
        }
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
        self.assertEqual(self.actions, ["build-for-testing", "test-without-building"])
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

    def test_unit_mode_selects_only_unit_bundle_at_build_and_test(self):
        result = self.run_verify(suite="unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions, ["build-for-testing", "test-without-building"])
        for call in self.calls:
            if call[0] == "xcodebuild" and call[-1] in self.actions:
                self.assertIn("-only-testing:KriaTests", call)
        self.assertFalse(
            (self.root / "src/apps/ios/.derived-data/.ci-ui-build").exists()
        )

    def test_prepare_then_ui_reuses_same_build_and_destination(self):
        prepared = self.run_verify(suite="prepare-ui", restore_times=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        build = next(call for call in self.calls if call[-1] == "build-for-testing")
        unit = next(call for call in self.calls if call[-1] == "test-without-building")
        self.assertFalse(any(arg.startswith("-only-testing:") for arg in build))
        self.assertIn("-only-testing:KriaTests", unit)
        ui = self.run_verify(suite="ui")
        self.assertEqual(ui.returncode, 0, ui.stderr)
        self.assertEqual(self.actions, ["test-without-building"])
        call = next(call for call in self.calls if call[-1] == "test-without-building")
        self.assertIn("-only-testing:KriaUITests", call)
        self.assertEqual(
            call[call.index("-destination") + 1], "platform=iOS Simulator,id=selected"
        )
        self.assertFalse(any(call[0] in ("xcodegen", "xcrun") for call in self.calls))
        self.assertNotEqual(self.run_verify(suite="ui").returncode, 0)

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

    def test_bad_api_url_stops_before_build(self):
        result = self.run_verify("bad_url")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected Staging API_BASE_URL=", result.stderr)
        self.assertEqual(self.actions, [])

    def test_generation_failure_stops_before_xcode(self):
        self.assertEqual(self.run_verify("generation_failure").returncode, 12)
        self.assertFalse(any(call[0] == "xcodebuild" for call in self.calls))


if __name__ == "__main__":
    unittest.main()
