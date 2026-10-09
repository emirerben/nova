"""Policy tests for KRI-559's machine-readable journey evidence."""

import importlib.util
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
