"""KRI-605 failure contracts, written before the workflow implementation."""

import importlib.util
from pathlib import Path
import tempfile
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


class ThroughputTests(unittest.TestCase):
    def test_only_main_push_defers_native_regression(self):
        for event in ("pull_request", "schedule", "workflow_dispatch", "unknown"):
            self.assertTrue(ci.native_validation(event, {"ios"}, False))
            self.assertTrue(ci.native_validation(event, {"api"}, True))
            self.assertFalse(ci.native_validation(event, {"api"}, False))
        self.assertFalse(ci.native_validation("push", {"ios"}, True))
        self.assertEqual(ci.selection_details("push", "", "")[2], "smoke")
        self.assertEqual(ci.ui_shards("push", "full"), 1)
        self.assertEqual(ci.ui_shards("schedule", "full"), 3)

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
                },
            },
            "journey-mapping": {
                "result": "success" if journey == "true" else "skipped"
            },
            "journey-offline": {
                "result": "success" if journey == "true" else "skipped"
            },
            "journey-native": {"result": "success" if native == "true" else "skipped"},
        }

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


class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "fixture").mkdir()
        (self.root / "fixture/e2e.json").write_text("{}")
        (self.root / "fixture/source.mp4").write_bytes(b"synthetic media")

    def seal(self):
        evidence.seal_bundle(self.root, "a" * 40, "123", "1")

    def verify(self, head="a" * 40, run="123", attempt="1"):
        evidence.verify_bundle(self.root, head, run, attempt)

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
