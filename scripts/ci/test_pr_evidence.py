"""Failure modes first: exercise the public gate with actual Git repositories.

A missing/malformed receipt, a receipt for a different patch, or an unverifiable
artifact must fail. A rebase, amend or merge of the base branch that leaves the
patch unchanged must not. A partial fix may name unfinished work, but cannot
claim complete fulfillment.
Changing, renaming, deleting, or leaving runtime code untracked cannot bypass
scope selection. PR text is data: commands in it must never run.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("pr-evidence.py").resolve()
KINDS = (
    "incident_replay",
    "wrong_content_control",
    "recovery",
    "request_fulfillment",
    "export_fidelity",
    "rollout",
)


class EvidenceCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "tests@invalid.local")
        self.git("config", "user.name", "Evidence test")
        self.write("src/apps/api/app/kria/planner.py", "old behavior\n")
        self.write("reports/incident.json", '{"incident":"six requested actions"}\n')
        self.write(
            "beklentiler/gerçek.json", '{"dördüncü_kesit":"57. saniyedeki ısırık"}\n'
        )
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.write("src/apps/api/app/kria/planner.py", "fixed behavior\n")
        self.commit()

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE, text=True
        ).strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "change")
        self.head = self.git("rev-parse", "HEAD")

    def patch_id(self, base=None, head=None):
        result = self.run_gate(None, "--patch-id", base=base, head=head)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def receipt(self):
        checks = []
        for kind in KINDS:
            checks.append(
                {
                    "kind": kind,
                    "status": "pending",
                    "evidence_level": "pending",
                    "command": "",
                    "artifact": "",
                    "expectation_source": "",
                    "reason": "Native export remains to be checked on the cafe fixture.",
                    "owner_issue": "KRI-723",
                }
            )
        checks[0].update(
            status="passed",
            evidence_level="offline_replay",
            command="python3 replay.py --fixture cafe",
            artifact="reports/incident.json",
            expectation_source="beklentiler/gerçek.json",
            reason="",
            owner_issue="",
        )
        return {
            "schema_version": 1,
            "patch_id": self.patch_id(),
            "claim": "local_fix",
            "issue": "KRI-577",
            "original_request": "Use the six named cafe moments with a photo after the wave.",
            "request_artifact": "reports/incident.json",
            "request_provenance": "Synthetic reproduction derived from KRI-775; no private media.",
            "expected_outcome": "The fourth exported cut visibly shows the bite at source 57 seconds.",
            "root_cause": "Brief construction discarded the requested moment count.",
            "checks": checks,
        }

    def body(self, receipt):
        return "```request-evidence\n" + json.dumps(receipt) + "\n```\n"

    def run_gate(self, body=None, *extra, base=None, head=None):
        args = [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(self.repo),
            "--base",
            base or self.base,
            "--head",
            head or self.head,
        ]
        if body is not None:
            path = self.repo / ".git" / "pr-body.txt"
            path.write_text(body)
            args.extend(["--body-file", str(path)])
        return subprocess.run([*args, *extra], text=True, capture_output=True)

    def assertFails(self, body, expected):
        result = self.run_gate(body)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)

    def test_scoped_partial_fix_passes_with_named_gaps(self):
        result = self.run_gate(self.body(self.receipt()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local_fix", result.stdout)
        self.assertIn("does not verify", result.stdout)

    def test_omitted_malformed_duplicate_and_hidden_blocks_fail(self):
        valid = self.body(self.receipt())
        for body in (
            "No evidence",
            "```request-evidence\n{broken}\n```",
            valid + valid,
            "<!--\n" + valid + "-->",
        ):
            with self.subTest(body=body[:40]):
                self.assertNotEqual(self.run_gate(body).returncode, 0)

    def test_duplicate_json_keys_fail(self):
        value = self.body(self.receipt()).replace(
            '"claim": "local_fix"',
            '"claim": "request_fulfillment", "claim": "local_fix"',
        )
        self.assertFails(value, "duplicate JSON key")

    def test_template_placeholders_and_comment_only_fields_fail(self):
        for field, text in (
            ("expected_outcome", "TODO: fill observable outcome"),
            ("root_cause", "<!-- a long explanation hidden from readers -->"),
            ("request_provenance", "<replace with provenance>"),
        ):
            value = self.receipt()
            value[field] = text
            self.assertFails(self.body(value), field)

    def test_receipt_must_name_this_prs_patch(self):
        value = self.receipt()
        value["patch_id"] = "0" * 40
        self.assertFails(self.body(value), "patch_id does not match")

    def test_missing_or_double_identity_fails(self):
        value = self.receipt()
        del value["patch_id"]
        self.assertFails(self.body(value), "requires fields")
        value = self.receipt()
        value["head_sha"] = self.head
        self.assertFails(self.body(value), "requires fields")

    def branch_pr(self):
        """Turn the committed history into base <- pr, with main free to advance."""
        self.git("branch", "pr")
        self.git("checkout", "-q", "-B", "main", self.base)

    def advance_main(self, name="docs/elsewhere.md", text="unrelated main work\n"):
        self.write(name, text)
        self.git("add", "-A")
        self.git("commit", "-qm", "main moves")
        return self.git("rev-parse", "HEAD")

    def evidence_passes(self, receipt, *, base, head):
        result = self.run_gate(self.body(receipt), base=base, head=head)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rebase_with_identical_patch_keeps_evidence_valid(self):
        receipt = self.receipt()
        self.branch_pr()
        new_base = self.advance_main()
        self.git("checkout", "-q", "pr")
        self.git("rebase", "-q", new_base)
        new_head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(new_head, self.head)
        self.evidence_passes(receipt, base=new_base, head=new_head)

    def test_rebase_over_neighbouring_edits_in_the_same_file_keeps_evidence(self):
        self.write(
            "src/apps/api/app/kria/planner.py", "\n".join(map(str, range(40))) + "\n"
        )
        self.git("add", "-A")
        self.git("commit", "-qm", "wider file")
        self.base = self.git("rev-parse", "HEAD")
        lines = (self.repo / "src/apps/api/app/kria/planner.py").read_text().split("\n")
        lines[5] = "pr edit"
        self.write("src/apps/api/app/kria/planner.py", "\n".join(lines))
        self.commit()
        receipt = self.receipt()
        self.branch_pr()
        lines[8] = "main edit two lines clear of the PR hunk"
        lines[5] = "5"
        self.write("src/apps/api/app/kria/planner.py", "\n".join(lines))
        self.git("add", "-A")
        self.git("commit", "-qm", "main edits a neighbouring line")
        new_base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "pr")
        self.git("rebase", "-q", new_base)
        self.evidence_passes(receipt, base=new_base, head=self.git("rev-parse", "HEAD"))

    def commit_over_receipt(self, base, before, after):
        """PR adds `before` over `base`, receipt recorded; a later commit makes it `after`."""
        path = "src/apps/api/app/kria/planner.py"
        self.write(path, base)
        self.commit()
        self.base = self.head
        self.write(path, before)
        self.commit()
        receipt = self.receipt()
        self.write(path, after)
        self.commit()
        self.assertFails(self.body(receipt), "patch_id does not match")

    def test_reindenting_a_line_invalidates_evidence(self):
        self.commit_over_receipt(
            "def reserve(s):\n    if not s.control:\n        log()\n    return\n",
            "def reserve(s):\n    if not s.control:\n        log()\n    raise Policy()\n    return\n",
            "def reserve(s):\n    if not s.control:\n        log()\n        raise Policy()\n    return\n",
        )

    def test_moving_an_added_line_to_another_place_invalidates_evidence(self):
        base = "def health():\n    log()\n    return ok\n\n\ndef startup():\n    boot()\n    return up\n"
        self.commit_over_receipt(
            base,
            base.replace("    return ok", "    audit()\n    return ok"),
            base.replace("    return up", "    audit()\n    return up"),
        )

    def test_git_diff_config_cannot_change_the_patch_id(self):
        path = "src/apps/api/app/kria/planner.py"
        before = "  x\n  x\na\n{\n  y\nend\n  x\nb\n{\n}\nend\na\n  y\na\n"
        after = "  x\n  x\na\n{\n  y\nend\n  x\nb\n{\nend\n}\nend\na\na\n  y\nb\na\n"
        self.write(path, before)
        self.commit()
        self.base = self.head
        self.write(path, after)
        self.commit()
        expected = self.patch_id()
        home = self.repo / ".git" / "fake-home"
        home.mkdir()
        (home / ".gitconfig").write_text("[diff]\n\talgorithm = histogram\n")
        env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
        same_flags = [
            "diff",
            "--no-color",
            "--indent-heuristic",
            "--inter-hunk-context=0",
            "--binary",
            "-U1",
            self.base,
            self.head,
        ]

        def raw_id():
            diff = subprocess.run(
                ["git", "-C", str(self.repo), *same_flags],
                env={**os.environ, **env},
                capture_output=True,
            ).stdout
            return (
                subprocess.run(
                    ["git", "patch-id", "--verbatim"], input=diff, capture_output=True
                )
                .stdout.split()[0]
                .decode()
            )

        self.assertNotEqual(
            raw_id(), expected, "fixture no longer separates histogram from myers"
        )
        with mock.patch.dict("os.environ", env):
            self.assertEqual(self.patch_id(), expected, "global git config leaked in")
            for key, value in (
                ("diff.algorithm", "histogram"),
                ("diff.indentHeuristic", "false"),
                ("diff.suppressBlankEmpty", "true"),
                ("diff.context", "7"),
                ("diff.interHunkContext", "9"),
                ("color.ui", "always"),
                ("diff.noprefix", "true"),
            ):
                self.git("config", key, value)
            self.assertEqual(self.patch_id(), expected, "repo git config leaked in")

    def test_rebase_that_changes_the_patch_invalidates_evidence(self):
        receipt = self.receipt()
        self.branch_pr()
        new_base = self.advance_main()
        self.git("checkout", "-q", "pr")
        self.git("rebase", "-q", new_base)
        self.write("src/apps/api/app/kria/planner.py", "fixed behavior, then changed\n")
        self.git("commit", "-qam", "conflict resolution rewrote the fix")
        result = self.run_gate(
            self.body(receipt), base=new_base, head=self.git("rev-parse", "HEAD")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("patch_id does not match", result.stderr)

    def test_any_new_code_commit_invalidates_evidence(self):
        receipt = self.receipt()
        self.write(
            "src/apps/api/app/kria/planner.py", "fixed behavior\nplus one line\n"
        )
        self.commit()
        result = self.run_gate(self.body(receipt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("patch_id does not match", result.stderr)

    def test_changed_binary_artifact_invalidates_evidence(self):
        (self.repo / "reports").mkdir(exist_ok=True)
        (self.repo / "reports/out.mp4").write_bytes(b"\x00\x01frame-a")
        self.commit()
        receipt = self.receipt()
        (self.repo / "reports/out.mp4").write_bytes(b"\x00\x01frame-b")
        self.commit()
        self.assertFails(self.body(receipt), "patch_id does not match")

    def test_mode_only_change_invalidates_evidence(self):
        receipt = self.receipt()
        self.git("update-index", "--chmod=+x", "src/apps/api/app/kria/planner.py")
        self.git("commit", "-qm", "make executable")
        self.head = self.git("rev-parse", "HEAD")
        self.assertFails(self.body(receipt), "patch_id does not match")

    def test_amend_and_force_push_with_the_same_patch_keep_evidence(self):
        receipt = self.receipt()
        old_head = self.head
        self.git("commit", "--amend", "-qm", "reworded after review")
        self.head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.head, old_head)
        self.evidence_passes(receipt, base=self.base, head=self.head)

    def test_squashing_the_same_changes_keeps_evidence(self):
        self.write("docs/note.md", "second commit\n")
        self.commit()
        receipt = self.receipt()
        self.git("reset", "--soft", self.base)
        self.git("commit", "-qm", "squashed")
        self.evidence_passes(
            receipt, base=self.base, head=self.git("rev-parse", "HEAD")
        )

    def test_merging_main_into_the_branch_keeps_evidence(self):
        receipt = self.receipt()
        self.branch_pr()
        new_base = self.advance_main()
        self.git("checkout", "-q", "pr")
        self.git("merge", "-q", "--no-ff", "-m", "merge main", new_base)
        merged = self.git("rev-parse", "HEAD")
        self.assertEqual(
            len(self.git("rev-list", "--parents", "-n1", merged).split()), 3
        )
        self.evidence_passes(receipt, base=new_base, head=merged)

    def test_evidence_copied_from_another_pr_is_rejected(self):
        other = self.receipt()
        self.git("checkout", "-q", "-B", "other-pr", self.base)
        self.write("src/apps/api/app/kria/planner.py", "a different fix entirely\n")
        self.commit()
        self.head = self.git("rev-parse", "HEAD")
        self.assertFails(self.body(other), "patch_id does not match")

    def test_evidence_for_an_unrelated_commit_is_rejected(self):
        value = self.receipt()
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.git("checkout", "-q", "--orphan", "unrelated")
        self.git("rm", "-rqf", ".")
        self.write("reports/incident.json", '{"other":"history"}\n')
        self.git("add", "-A")
        self.git("commit", "-qm", "unrelated root")
        stray = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", branch)
        value["checks"][0]["artifact"] = (
            f"https://github.com/emirerben/nova/commit/{stray}"
        )
        self.assertFails(self.body(value), "artifact")
        value["checks"][0]["artifact"] = (
            f"https://github.com/emirerben/nova/blob/{stray}/reports/incident.json"
        )
        self.assertFails(self.body(value), "artifact")

    def test_empty_patch_has_nothing_to_fingerprint(self):
        result = self.run_gate(None, "--patch-id", base=self.head, head=self.head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty diff", result.stderr)

    def test_empty_body_fails_for_affected_changes(self):
        self.assertFails("", "request-evidence")

    def test_legacy_head_sha_receipts_still_validate_against_the_exact_head(self):
        value = self.receipt()
        del value["patch_id"]
        value["head_sha"] = self.head
        result = self.run_gate(self.body(value))
        self.assertEqual(result.returncode, 0, result.stderr)
        value["head_sha"] = self.base
        self.assertFails(self.body(value), "head_sha")

    def test_missing_and_fake_artifacts_fail(self):
        for artifact in (
            "",
            "reports/missing.json",
            "../incident.json",
            "https://example.com/report",
            "https://github.com/emirerben/nova/blob/main/report.json",
            "https://github.com/emirerben/nova/actions/runs/123",
            "reports/empty.json",
        ):
            value = self.receipt()
            value["checks"][0]["artifact"] = artifact
            self.assertFails(self.body(value), "artifact")

    def test_empty_or_symlink_git_artifacts_fail(self):
        self.write("reports/empty.json", "")
        (self.repo / "reports/link.json").symlink_to("incident.json")
        self.commit()
        for artifact in ("reports/empty.json", "reports/link.json"):
            value = self.receipt()
            value["checks"][0]["artifact"] = artifact
            self.assertFails(self.body(value), "artifact")

    def test_pending_requires_named_owner_and_na_requires_explanation(self):
        value = self.receipt()
        value["checks"][1]["owner_issue"] = ""
        self.assertFails(self.body(value), "owner_issue")
        value = self.receipt()
        value["checks"][1].update(
            status="not_applicable", evidence_level="not_applicable", reason="n/a"
        )
        self.assertFails(self.body(value), "reason")

    def test_complete_claim_cannot_have_pending_or_offline_fulfillment(self):
        value = self.receipt()
        value["claim"] = "request_fulfillment"
        self.assertFails(self.body(value), "request_fulfillment")
        for row in value["checks"]:
            row.update(
                status="passed",
                evidence_level="offline_replay",
                command="python3 replay.py --fixture cafe",
                artifact="reports/incident.json",
                expectation_source="beklentiler/gerçek.json",
            )
        value["checks"][-1].update(
            status="not_applicable",
            evidence_level="not_applicable",
            reason="Local proof only; production rollout is outside this claim.",
        )
        self.assertFails(self.body(value), "native_output or production")

    def test_complete_native_claim_with_explicit_rollout_limit_passes(self):
        value = self.receipt()
        value["claim"] = "request_fulfillment"
        for row in value["checks"]:
            row.update(
                status="passed",
                evidence_level="native_output",
                command="xcodebuild test --fixture cafe",
                artifact="reports/incident.json",
                expectation_source="beklentiler/gerçek.json",
            )
        value["checks"][-1].update(
            status="not_applicable",
            evidence_level="not_applicable",
            reason="Local native proof only; production rollout is outside this claim.",
        )
        result = self.run_gate(self.body(value))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_kind_omissions_duplicates_and_count_only_expectation_fail(self):
        value = self.receipt()
        value["checks"].pop()
        self.assertFails(self.body(value), "exactly one")
        value = self.receipt()
        value["checks"].append(copy.deepcopy(value["checks"][0]))
        self.assertFails(self.body(value), "exactly one")
        value = self.receipt()
        value["checks"][0]["expectation_source"] = ""
        self.assertFails(self.body(value), "expectation_source")

    def test_git_diff_failures_are_closed_even_without_body(self):
        result = self.run_gate(None, "--requirements", base="0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git", result.stderr)

    def test_removed_and_renamed_runtime_paths_remain_required(self):
        # A real rename into documentation cannot hide the removed runtime path.
        (self.repo / "docs").mkdir(exist_ok=True)
        self.git("mv", "src/apps/api/app/kria/planner.py", "docs/planner.txt")
        self.commit()
        result = self.run_gate(None, "--requirements")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["required"])
        self.assertIn(
            "src/apps/api/app/kria/planner.py",
            json.loads(result.stdout)["affected_paths"],
        )

    def test_working_untracked_runtime_is_included(self):
        self.base = self.head
        self.write("src/apps/web/src/app/plan/new-flow.tsx", "export default null;\n")
        result = self.run_gate(None, "--requirements", "--working")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["required"])
        result = self.run_gate(None, "--requirements")
        self.assertFalse(json.loads(result.stdout)["required"])

    def test_docs_only_skip_without_reading_missing_body(self):
        self.base = self.head
        self.write("docs/review.md", "A documented policy clarification.\n")
        self.commit()
        result = self.run_gate(None, "--body-file", str(self.repo / "missing.md"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not required", result.stdout)

    def test_all_pending_or_not_applicable_is_not_a_verified_local_fix(self):
        value = self.receipt()
        value["checks"][0].update(
            status="pending",
            evidence_level="pending",
            reason="Replay has not yet been executed on the incident fixture.",
            owner_issue="KRI-577",
        )
        self.assertFails(self.body(value), "at least one passed")

    def test_immutable_artifact_urls_and_current_git_blob_are_accepted(self):
        for artifact in (
            f"https://github.com/emirerben/nova/blob/{self.head}/reports/incident.json",
            f"https://github.com/emirerben/nova/commit/{self.head}",
            "https://github.com/emirerben/nova/actions/runs/123/attempts/2",
            "https://github.com/emirerben/nova/actions/runs/123/artifacts/456",
        ):
            value = self.receipt()
            value["checks"][0]["artifact"] = artifact
            result = self.run_gate(self.body(value))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_artifact_urls_with_credentials_queries_or_stale_head_fail(self):
        for artifact in (
            f"https://github.com/emirerben/nova/commit/{self.base}",
            f"https://github.com/emirerben/nova/commit/{self.head}?token=private",
            "https://user:secret@github.com/emirerben/nova/actions/runs/123/attempts/2",
        ):
            value = self.receipt()
            value["checks"][0]["artifact"] = artifact
            self.assertFails(self.body(value), "artifact")

    def test_fulfilled_claim_requires_an_original_request_fixture(self):
        value = self.receipt()
        value["claim"] = "request_fulfillment"
        value.pop("request_artifact")
        self.assertFails(self.body(value), "request_artifact")

    def test_ci_policy_and_test_files_require_evidence(self):
        self.base = self.head
        self.write("scripts/ci/pr-evidence.py", "# altered gate behavior\n")
        self.commit()
        result = self.run_gate(None, "--requirements")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["required"])

    def test_staged_and_unstaged_runtime_changes_are_included(self):
        self.base = self.head
        self.write("src/apps/api/app/kria/planner.py", "an unstaged correction\n")
        result = self.run_gate(None, "--requirements", "--working")
        self.assertTrue(json.loads(result.stdout)["required"])
        self.git("add", "src/apps/api/app/kria/planner.py")
        result = self.run_gate(None, "--requirements", "--working")
        self.assertTrue(json.loads(result.stdout)["required"])

    def test_untrusted_commands_are_never_executed(self):
        value = self.receipt()
        sentinel = self.repo / "MUST_NOT_EXIST"
        value["checks"][0]["command"] = f"touch {sentinel}; $(touch {sentinel})"
        result = self.run_gate(self.body(value))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())

    def test_event_exact_head_is_enforced_and_valid_event_passes(self):
        event = {
            "repository": {"full_name": "emirerben/nova"},
            "pull_request": {
                "body": self.body(self.receipt()),
                "head": {"sha": self.head, "repo": {"full_name": "yasin/nova"}},
                "base": {"sha": self.base, "repo": {"full_name": "emirerben/nova"}},
            },
        }
        path = self.repo / "event.json"
        path.write_text(json.dumps(event))
        result = self.run_gate(None, "--event", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_gate(None, "--event", str(path), head=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("base/head SHA", result.stderr)

    def test_production_claim_cannot_skip_rollout_verification(self):
        value = self.receipt()
        value["claim"] = "request_fulfillment"
        for row in value["checks"]:
            row.update(
                status="passed",
                evidence_level="production",
                command="verify deployed cafe output",
                artifact="reports/incident.json",
                expectation_source="beklentiler/gerçek.json",
            )
        value["checks"][-1].update(
            status="not_applicable",
            evidence_level="not_applicable",
            reason="No production rollout has been verified in this session.",
        )
        self.assertFails(self.body(value), "production evidence requires")

    def complete_receipt(self):
        value = self.receipt()
        value["claim"] = "request_fulfillment"
        for row in value["checks"]:
            row.update(
                status="passed",
                evidence_level="native_output",
                command="xcodebuild test --fixture cafe",
                artifact="reports/incident.json",
                expectation_source="beklentiler/gerçek.json",
            )
        value["checks"][-1].update(
            status="not_applicable",
            evidence_level="not_applicable",
            reason="Local native output only; production rollout remains outside this claim.",
        )
        return value

    def test_non_english_expectation_reference_passes_without_magic_words(self):
        result = self.run_gate(self.body(self.receipt()))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_magic_english_prose_is_not_an_expectation_artifact(self):
        value = self.receipt()
        value["checks"][0]["expectation_source"] = (
            "Human reviewed source fixture at the exact timestamp."
        )
        self.assertFails(self.body(value), "expectation_source")

    def test_wrong_content_and_fulfillment_must_separate_expected_and_actual(self):
        for kind in ("wrong_content_control", "request_fulfillment"):
            for reference in (
                "reports/incident.json",
                f"https://github.com/emirerben/nova/blob/{self.head}/reports/incident.json",
            ):
                value = self.complete_receipt()
                row = next(row for row in value["checks"] if row["kind"] == kind)
                row["expectation_source"] = reference
                self.assertFails(self.body(value), "distinct")

    def test_full_claim_cannot_use_unit_export_fidelity(self):
        value = self.complete_receipt()
        next(row for row in value["checks"] if row["kind"] == "export_fidelity")[
            "evidence_level"
        ] = "unit"
        self.assertFails(
            self.body(value), "export_fidelity requires native_output or production"
        )

    def test_any_production_evidence_requires_passed_rollout(self):
        for kind in ("incident_replay", "export_fidelity"):
            value = self.complete_receipt()
            next(row for row in value["checks"] if row["kind"] == kind)[
                "evidence_level"
            ] = "production"
            self.assertFails(self.body(value), "production evidence requires")
        value = self.receipt()
        value["checks"][0]["evidence_level"] = "production"
        self.assertFails(self.body(value), "production evidence requires")

    def test_rollout_alone_is_not_a_verified_implementation(self):
        value = self.receipt()
        value["checks"][0].update(
            status="pending",
            evidence_level="pending",
            reason="Incident replay has not yet been executed.",
            owner_issue="KRI-577",
        )
        value["checks"][-1].update(
            status="passed",
            evidence_level="production",
            command="verify account rollout flag",
            artifact="reports/incident.json",
        )
        self.assertFails(self.body(value), "passed implementation check")

    def test_pending_and_na_cannot_advertise_executed_evidence_levels(self):
        for status, level in (
            ("pending", "production"),
            ("pending", "native_output"),
            ("not_applicable", "unit"),
        ):
            value = self.receipt()
            value["checks"][1].update(status=status, evidence_level=level)
            self.assertFails(self.body(value), "evidence_level must match")

    def test_working_validation_cannot_reuse_head_after_runtime_rewrite(self):
        self.write(
            "src/apps/api/app/kria/planner.py", "uncommitted different behavior\n"
        )
        result = self.run_gate(self.body(self.receipt()), "--working")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("commit affected working changes", result.stderr)

    def test_working_validation_allows_unrelated_dirty_docs(self):
        self.write("docs/ordinary-readme.md", "An unrelated documentation note.\n")
        result = self.run_gate(self.body(self.receipt()), "--working")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_focused_policy_documents_cannot_bypass_own_gate(self):
        self.base = self.head
        paths = (
            "CLAUDE.md",
            "AGENTS.md",
            "docs/runbooks/request-to-output-standard.md",
            "docs/runbooks/pr-acceptance-checklist.md",
            "docs/reviews/kri-559-journey-verification.md",
            "docs/runbooks/agent-navigation.md",
        )
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "Updated acceptance policy evidence requirements.\n")
                self.commit()
                result = self.run_gate(None, "--requirements")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["required"])
                self.base = self.head
        self.write("README.md", "An ordinary project overview clarification.\n")
        self.commit()
        result = self.run_gate(None, "--requirements")
        self.assertFalse(json.loads(result.stdout)["required"])

    def test_authorized_live_model_receipt_has_a_distinct_evidence_level(self):
        value = self.receipt()
        value["checks"][0]["evidence_level"] = "live_model"
        value["request_provenance"] = (
            "Synthetic cafe request; KRI-577 approved the bounded paid model replay."
        )
        result = self.run_gate(self.body(value))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_event_identity_mismatches_fail(self):
        event = {
            "repository": {"full_name": "emirerben/nova"},
            "pull_request": {
                "body": self.body(self.receipt()),
                "head": {"sha": self.head, "repo": {"full_name": "yasin/nova"}},
                "base": {"sha": self.base, "repo": {"full_name": "different/nova"}},
            },
        }
        path = self.repo / "event.json"
        path.write_text(json.dumps(event))
        result = self.run_gate(None, "--event", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
