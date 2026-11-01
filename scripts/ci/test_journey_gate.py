"""Policy tests for KRI-559's machine-readable journey evidence."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("journey-gate.py")
spec = importlib.util.spec_from_file_location("journey_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class JourneyGateTests(unittest.TestCase):
    head = "a" * 40

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name) / "fixture"
        self.fixture.mkdir()
        for name in (
            "e2e.json",
            "status-creation-words-retimed.json",
            "editor-draft.json",
            "saved-guided-plan.json",
        ):
            (self.fixture / name).write_text(json.dumps({"name": name}))
        self.write_proof()

    def write_proof(self, **overrides):
        proof = {
            "schema_version": 1,
            "head_sha": self.head,
            "fixture_provenance": {"creation": "saved transport fixture"},
            "model_prompt_configuration": {"creation_model": "fixture"},
            "evidence_level": "offline_replay_and_production_validation",
            "settled_spend_usd": 0,
            "stages": {stage: "passed" for stage in gate.INPUT_STAGES},
            "source_fixture_sha256": gate.digest(
                SCRIPT.parents[2]
                / "src/apps/api/tests/fixtures/prompt_coverage/creation_composition.json"
            ),
            "input_hashes": {
                name: gate.digest(self.fixture / name)
                for name in (
                    "e2e.json",
                    "status-creation-words-retimed.json",
                    "editor-draft.json",
                    "saved-guided-plan.json",
                )
            },
        }
        proof.update(overrides)
        (self.fixture / "journey-input-proof.json").write_text(json.dumps(proof))

    def native_evidence(self):
        xcresult = Path(self.temp.name) / "native.xcresult"
        xcresult.mkdir()
        (xcresult / "Info.plist").write_text("result")
        render = Path(self.temp.name) / "render.mp4"
        render.write_bytes(b"movie")
        app_build = Path(self.temp.name) / "app-build.txt"
        app_build.write_text("Xcode 99")
        output = Path(self.temp.name) / "evidence.json"
        results = Path(self.temp.name) / "results.json"
        results.write_text(
            json.dumps(
                {
                    "testIdentifier": "KriaTests/DeviceMontageRenderE2ETests/testCapturedCreationWordsRetimedThenExportOnTheIPhone",
                    "testStatus": "Success",
                }
            )
        )
        gate.record_native(
            self.fixture, self.head, xcresult, results, render, app_build, output
        )
        return output

    def test_valid_evidence_passes(self):
        evidence = self.native_evidence()
        gate.validate(evidence, self.head)

    def test_manifest_named_selectors_identify_individual_test_cases(self):
        journeys = gate.load(SCRIPT.with_name("journey-manifest.json"))["journeys"]
        for journey in journeys:
            for selector in journey.get("xctests", []):
                with self.subTest(journey=journey["id"], selector=selector):
                    self.assertRegex(selector, r"^\w+/\w+/test\w+$")

    def test_omitted_stage_cannot_claim_full_verification(self):
        evidence = self.native_evidence()
        value = json.loads(evidence.read_text())
        del value["stages"]["native_export"]
        evidence.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "native_export"):
            gate.validate(evidence, self.head)

    def test_omitted_render_cannot_claim_full_verification(self):
        evidence = self.native_evidence()
        value = json.loads(evidence.read_text())
        del value["native"]["render"]
        evidence.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "native render evidence is missing"):
            gate.validate(evidence, self.head)

    def test_input_gate_rejects_bypass_and_stale_hash(self):
        self.write_proof(
            stages={
                stage: "passed" for stage in gate.INPUT_STAGES - {"phone_validation"}
            }
        )
        with self.assertRaisesRegex(ValueError, "phone_validation"):
            gate.verify_input(self.fixture, self.head)
        self.write_proof()
        (self.fixture / "e2e.json").write_text("changed after proof")
        with self.assertRaisesRegex(ValueError, "input hash mismatch"):
            gate.verify_input(self.fixture, self.head)

    def test_stale_sha_rejected_for_both_boundaries(self):
        self.write_proof(head_sha="b" * 40)
        with self.assertRaisesRegex(ValueError, "stale"):
            gate.verify_input(self.fixture, self.head)
        self.write_proof()
        evidence = self.native_evidence()
        with self.assertRaisesRegex(ValueError, "stale"):
            gate.validate(evidence, "b" * 40)

    def test_named_native_tests_must_each_pass(self):
        results = Path(self.temp.name) / "thought-results.json"
        results.write_text(
            json.dumps(
                {
                    "tests": [
                        {
                            "testIdentifier": "KriaTests/ChatTimelineTests/testCompletedThoughtIsPlacedAfterItsUserRequestBeforeReply",
                            "testStatus": "Success",
                        },
                        {
                            "testIdentifier": "KriaTests/SlidePostTests/testProposalShowsStreamingSummaryBeforeReplyAndCompletesHistory",
                            "testStatus": "Skipped",
                        },
                    ]
                }
            )
        )
        gate.require_passed_test(
            results,
            "ChatTimelineTests/testCompletedThoughtIsPlacedAfterItsUserRequestBeforeReply",
        )
        with self.assertRaisesRegex(ValueError, "did not pass"):
            gate.require_passed_test(
                results,
                "SlidePostTests/testProposalShowsStreamingSummaryBeforeReplyAndCompletesHistory",
            )
        with self.assertRaisesRegex(ValueError, "does not contain"):
            gate.require_passed_test(
                results,
                "SlidePostChatEditTests/testCompletedChatEditThoughtRemainsWithReplyAfterReopening",
            )


class FixtureBundleTests(unittest.TestCase):
    """Sealing/verifying the portable bundle the offline job hands to the Mac.

    Failure modes covered (written before the fix):
    1. A journey that declares a fixture_dir whose generator silently did not
       run must still fail, even when another selected journey left files.
    2. A native journey with no offline fixtures (apns notification runtime)
       must seal a non-empty, head/run/attempt-bound bundle and verify.
    3. Mixed selection keeps the real fixtures and adds the provenance file.
    4. Stale head, run, attempt or selected-journey ids fail verification.
    5. A symlink or a byte changed after sealing is rejected.
    6. The native leg only needs fixture proofs for journeys that declare them.
    """

    head = "c" * 40
    run_id = "41307871421"
    attempt = "1"
    no_fixture = "apns-transport-contract,native-notification-runtime"
    with_fixture = "kri-524-creation"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "kri-559"
        self.root.mkdir()

    def seal(self, journeys, **overrides):
        args = dict(head=self.head, run=self.run_id, attempt=self.attempt)
        args.update(overrides)
        gate.seal_bundle(
            self.root, args["head"], args["run"], args["attempt"], journeys
        )

    def verify(self, journeys, **overrides):
        args = dict(head=self.head, run=self.run_id, attempt=self.attempt)
        args.update(overrides)
        gate.verify_bundle(
            self.root, args["head"], args["run"], args["attempt"], journeys
        )

    def write_fixture(self, name="fixture"):
        (self.root / name).mkdir(exist_ok=True)
        (self.root / name / "e2e.json").write_text('{"ok": true}')

    def test_fixture_journey_with_skipped_generator_still_fails(self):
        with self.assertRaisesRegex(ValueError, "kri-524-creation.*fixture"):
            self.seal(self.with_fixture)
        self.assertFalse((self.root / gate.BUNDLE_MANIFEST).exists())

    def test_fixture_journey_fails_when_only_another_journey_left_files(self):
        self.write_fixture("kri-648")
        with self.assertRaisesRegex(ValueError, "kri-524-creation.*fixture"):
            self.seal("kri-524-creation,kri-648-phone-text")

    def test_fixture_journey_fails_with_empty_fixture_directory(self):
        (self.root / "fixture").mkdir()
        with self.assertRaisesRegex(ValueError, "kri-524-creation.*fixture"):
            self.seal(self.with_fixture)

    def test_native_journey_without_fixtures_seals_and_verifies(self):
        self.seal(self.no_fixture)
        self.verify(self.no_fixture)
        record = json.loads((self.root / gate.PROVENANCE).read_text())
        self.assertEqual(record["head_sha"], self.head)
        self.assertEqual(record["run_id"], self.run_id)
        self.assertEqual(record["run_attempt"], self.attempt)
        self.assertEqual(
            record["journeys"], sorted(self.no_fixture.split(","))
        )
        self.assertEqual(record["fixtures"], "none required")

    def test_unknown_or_empty_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown journey"):
            self.seal("not-a-journey")
        with self.assertRaisesRegex(ValueError, "no journeys"):
            self.seal("")

    def test_empty_directory_is_still_not_a_bundle(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            gate.bundle_files(self.root)

    def test_mixed_selection_keeps_fixtures_and_adds_provenance(self):
        self.write_fixture()
        selection = f"{self.with_fixture},native-notification-runtime"
        self.seal(selection)
        self.verify(selection)
        manifest = json.loads((self.root / gate.BUNDLE_MANIFEST).read_text())
        self.assertIn("fixture/e2e.json", manifest["files"])
        self.assertIn(gate.PROVENANCE, manifest["files"])
        record = json.loads((self.root / gate.PROVENANCE).read_text())
        self.assertEqual(record["fixtures"], ["fixture"])

    def test_stale_head_run_attempt_rejected(self):
        self.seal(self.no_fixture)
        for key, value in (
            ("head", "d" * 40),
            ("run", "1"),
            ("attempt", "2"),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "stale"):
                    self.verify(self.no_fixture, **{key: value})

    def test_stale_provenance_rejected_even_if_manifest_is_resealed(self):
        self.seal(self.no_fixture)
        record = json.loads((self.root / gate.PROVENANCE).read_text())
        record["head_sha"] = "d" * 40
        (self.root / gate.PROVENANCE).write_text(json.dumps(record))
        manifest = json.loads((self.root / gate.BUNDLE_MANIFEST).read_text())
        manifest["files"] = gate.bundle_files(self.root)
        (self.root / gate.BUNDLE_MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "selected-journeys.*stale"):
            self.verify(self.no_fixture)

    def test_bundle_for_other_journeys_is_rejected(self):
        self.seal(self.no_fixture)
        with self.assertRaisesRegex(ValueError, "different journeys"):
            self.verify("apns-transport-contract")

    def test_verify_requires_declared_fixtures_to_still_exist(self):
        self.write_fixture()
        self.seal(self.with_fixture)
        (self.root / "fixture" / "e2e.json").unlink()
        with self.assertRaises(ValueError):
            self.verify(self.with_fixture)

    def test_tampering_after_sealing_is_rejected(self):
        self.seal(self.no_fixture)
        (self.root / gate.PROVENANCE).write_text("{}")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.verify(self.no_fixture)

    def test_symlink_after_sealing_is_rejected(self):
        self.seal(self.no_fixture)
        (self.root / "link.json").symlink_to(self.root / gate.PROVENANCE)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.verify(self.no_fixture)

    def test_cli_seals_and_verifies_a_native_journey_without_fixtures(self):
        base = [
            sys.executable,
            str(SCRIPT),
        ]
        common = [
            "--root", str(self.root), "--head", self.head,
            "--run", self.run_id, "--attempt", self.attempt,
            "--journeys", self.no_fixture,
        ]
        for command in ("seal-bundle", "verify-bundle"):
            done = subprocess.run(
                [*base, command, *common], capture_output=True, text=True
            )
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_manifest_fixture_dirs_match_the_workflow_generators(self):
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        declared = {
            row["id"]: row["fixture_dir"]
            for row in gate.load(SCRIPT.with_name("journey-manifest.json"))[
                "journeys"
            ]
            if "fixture_dir" in row
        }
        self.assertTrue(declared)
        for journey, directory in declared.items():
            with self.subTest(journey=journey):
                self.assertIn(f"test-results/kri-559/{directory}", workflow)

    def test_native_leg_requires_input_proof_only_for_the_creation_journey(self):
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        start = workflow.index("Check fixture provenance and transferred bytes")
        step = workflow[start : workflow.index("- name:", start + 10)]
        self.assertIn('*",kri-524-creation,"*', step)
        self.assertEqual(step.count("verify-input"), 1)
        self.assertIn('--journeys "$JOURNEY_IDS"', step)

    def test_seal_step_passes_the_selected_journeys(self):
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        start = workflow.index("Seal portable fixture inputs")
        step = workflow[start : workflow.index("- name:", start + 10)]
        self.assertIn("needs.changes.outputs.journey_ids", step)
        self.assertIn('--journeys "$JOURNEY_IDS"', step)


if __name__ == "__main__":
    unittest.main()
