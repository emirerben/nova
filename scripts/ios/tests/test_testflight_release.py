"""Offline contracts for the TestFlight release lane.

These tests intentionally inspect the release inputs as text.  They do not
need Xcode, Fastlane, Apple credentials, or network access, and tolerate
ordinary YAML/YAML-ish formatting changes while guarding the security
boundaries of the release path.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
ENTITLEMENTS = ROOT / "src/apps/ios/Kria/Kria.entitlements"
PROJECT = ROOT / "src/apps/ios/project.yml"
PRODUCTION_CONFIG = ROOT / "src/apps/ios/Config/Production.xcconfig"
ARCHIVE = ROOT / "scripts/ios/archive-testflight.sh"
FASTFILE = ROOT / "fastlane/Fastfile"
APPFILE = ROOT / "fastlane/Appfile"
WORKFLOW = ROOT / ".github/workflows/testflight.yml"
RELEASE_INPUTS = ROOT / "scripts/ios/release_inputs.py"


def _section(text: str, heading: str, next_heading: str | None = None) -> str:
    """Return a named indented section without depending on exact spacing."""
    # Release is usually named once in the top-level config map and again in
    # the app target.  The latter is the signing/build-settings override.
    start = text.rfind(heading)
    if start < 0:
        return ""
    result = text[start:]
    if next_heading:
        end = result.find(next_heading, len(heading))
        if end >= 0:
            result = result[:end]
    return result


class EntitlementAndProjectTests(unittest.TestCase):
    def test_release_entitlement_enables_apple_sign_in(self):
        self.assertTrue(ENTITLEMENTS.is_file(), "missing Kria.entitlements")
        text = ENTITLEMENTS.read_text()
        self.assertRegex(text, r"com\.apple\.developer\.applesignin")
        self.assertRegex(text, r"\b(Default|Sign in with Apple)\b")

    def test_push_entitlement_is_production_only_in_release(self):
        self.assertRegex(ENTITLEMENTS.read_text(), r"aps-environment[\s\S]*\$\(KRIA_APS_ENVIRONMENT\)")
        self.assertRegex(PRODUCTION_CONFIG.read_text(), r"(?m)^KRIA_APS_ENVIRONMENT = production$")

    def test_project_references_entitlement_and_release_signing_settings(self):
        text = PROJECT.read_text()
        self.assertRegex(
            text,
            r"CODE_SIGN_ENTITLEMENTS\s*:\s*[^\n]*Kria(?:/|\\)Kria\.entitlements",
        )
        release = _section(text, "Release:")
        self.assertTrue(release, "project.yml must contain a Release config")
        # XcodeGen must consume CI's signing team and provisioning profile.
        self.assertRegex(text, r"DEVELOPMENT_TEAM\s*:\s*[^\n]+\$\([^)]*TEAM[^)]*\)")
        self.assertRegex(release, r"(?:PROVISIONING_PROFILE|PRODUCT_BUNDLE_IDENTIFIER)\s*:")
        self.assertRegex(
            release,
            r"CODE_SIGN_IDENTITY\s*:\s*Apple Distribution",
            "Release archives must use the installed Apple Distribution certificate",
        )

    def test_production_config_declares_runtime_google_settings(self):
        text = PRODUCTION_CONFIG.read_text()
        for name in ("KRIA_GOOGLE_CLIENT_ID", "KRIA_GOOGLE_REDIRECT_SCHEME"):
            with self.subTest(name=name):
                self.assertRegex(text, rf"(?m)^{name}\s*=")


class ArchiveScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = ARCHIVE.read_text()
        self.assertIn("xcodebuild", self.text)

    def test_required_environment_is_checked_before_archive(self):
        archive = self.text.find("xcodebuild")
        validation = self.text[:archive]
        self.assertRegex(validation, r"set\s+-euo\s+pipefail")
        # Signing and release identity must be supplied at runtime; permit a
        # loop over names as well as the conventional ${VAR:?message} form.
        required = (
            "KRIA_DEVELOPMENT_TEAM",
            "KRIA_PROVISIONING_PROFILE_SPECIFIER",
            "KRIA_GOOGLE_REDIRECT_SCHEME",
            "KRIA_MARKETING_VERSION",
            "KRIA_BUILD_NUMBER",
        )
        for name in required:
            with self.subTest(name=name):
                self.assertIn(name, validation)
        self.assertRegex(validation, r"\$\{[A-Z0-9_]+:?[^}]*\}|missing|required")

    def test_build_number_is_positive_numeric_before_archive(self):
        # Archive validates malformed and non-positive build numbers. Remote
        # monotonic/deduplication is deliberately owned by Fastlane.
        archive = self.text.find("xcodebuild")
        before_archive = self.text[:archive]
        self.assertRegex(before_archive, r"BUILD_NUMBER|CURRENT_PROJECT_VERSION")
        self.assertRegex(before_archive, r"[Nn]umeric|\[0-9\]|[0-9].*(?:regex|pattern)|^[^#]*[0-9]")
        self.assertRegex(before_archive, r"positive integer|\^\[1-9\]|BUILD_NUMBER.*[0-9]")

    def test_export_profile_uses_the_production_bundle_identifier(self):
        self.assertIn(
            'options["provisioningProfiles"]["com.emirerben.kria"]',
            self.text,
        )
        self.assertNotIn(
            'options["provisioningProfiles"]["com.kria.app"]',
            self.text,
        )

    def test_archive_allows_the_trusted_openapi_build_tool_plugin(self):
        self.assertIn("-skipPackagePluginValidation", self.text)


class FastlaneTests(unittest.TestCase):
    def test_lane_uses_app_store_distribution_and_external_group(self):
        text = FASTFILE.read_text()
        self.assertRegex(text, r"(?i)lane\s*:\s*[^\n]*testflight|lane\s+:\s*testflight")
        self.assertRegex(text, r"(?i)pilot|upload_to_testflight")
        self.assertRegex(text, r"(?i)groups?\s*:\s*[^\n]*(external|testflight)")
        self.assertRegex(text, r"(?i)app_store_build_number")
        self.assertRegex(text, r"(?i)initial_build_number\s*:\s*0")
        self.assertRegex(text, r"(?i)latest_build\s*>=\s*build_number\.to_i")
        self.assertRegex(text, r"(?i)skip(?:ping)? duplicate|already has build")
        self.assertRegex(text, r"(?i)app_version\s*:\s*marketing_version")
        self.assertRegex(text, r"(?i)build_number\s*:\s*build_number")
        self.assertRegex(
            text,
            r'build_number\s*=\s*ENV\.fetch\("KRIA_BUILD_NUMBER"\)(?!\.to_i)',
            "upload_to_testflight requires the build number to stay a String",
        )
        # Apple's Beta App Review (Guideline 2.1(a)) requires a demo account
        # when the app is sign-in-gated; KRI-111 fixed the 2026-09-16/17 rejection.
        self.assertRegex(text, r"demo_account_required:\s*true")
        self.assertRegex(
            text,
            r'demo_account_name:\s*ENV\.fetch\("KRIA_BETA_REVIEW_DEMO_USER"\)',
        )
        self.assertRegex(
            text,
            r'demo_account_password:\s*ENV\.fetch\("KRIA_BETA_REVIEW_DEMO_PASSWORD"\)',
        )
        # The IPA is archived with App Store distribution before this upload
        # lane; keep the assertion coupled to the same release script.
        self.assertRegex(ARCHIVE.read_text(), r"(?i)app[-_ ]store")
        self.assertTrue(APPFILE.is_file(), "missing fastlane/Appfile")


class ReleaseInputTests(unittest.TestCase):
    """TestFlight's eligibility list lives in scripts/ios/release_inputs.py,
    read by this workflow and by the iOS main-push selector (KRI-1547).

    Ways it can go wrong: the move changes what TestFlight releases; the
    workflow's shell drops or splits a path, or treats an empty list as
    "nothing changed"; a failure to read the list is taken as "no release
    input changed" instead of stopping the release.
    """

    # What testflight.yml listed inline before KRI-1547. Change it only on
    # purpose: it decides which main commits become a TestFlight build.
    BEFORE = (
        "src/apps/ios",
        "scripts/ios",
        ".github/workflows/ios.yml",
        ".github/workflows/testflight.yml",
        ".claude/worktrees",
        "src/apps/api/app/config.py",
        "src/apps/api/app/services/mobile_auth.py",
    )

    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_the_list_is_the_one_testflight_always_used(self):
        result = subprocess.run(
            [sys.executable, str(RELEASE_INPUTS)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tuple(result.stdout.splitlines()), self.BEFORE)

    def eligibility_block(self):
        match = re.search(
            r'(?ms)^( +git show "\$GITHUB_SHA:scripts/ios/release_inputs\.py".*?^ +echo "eligible=true".*?\n +fi\n)',
            self.text,
        )
        self.assertIsNotNone(match, "missing release-input eligibility block")
        return textwrap.dedent(match.group(1))

    def run_block(self, changed, release_inputs=None):
        """Run the workflow's own eligibility lines on a scratch repository."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            temp = Path(tmp) / "runner-temp"
            temp.mkdir()
            helper = repo / "scripts/ios/release_inputs.py"
            helper.parent.mkdir(parents=True)
            helper.write_text(release_inputs or RELEASE_INPUTS.read_text())

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "ci@example.invalid")
            git("config", "user.name", "CI tests")
            git("add", "-A")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            target = repo / changed
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("changed")
            git("add", "-A")
            git("commit", "-qm", "change")
            head = git("rev-parse", "HEAD")
            output = Path(tmp) / "github-output"
            output.touch()
            result = subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + self.eligibility_block()],
                cwd=repo,
                env={
                    **os.environ,
                    "GITHUB_SHA": head,
                    "HEAD_SHA": head,
                    "base": base,
                    "RUNNER_TEMP": str(temp),
                    "GITHUB_OUTPUT": str(output),
                },
                capture_output=True,
                text=True,
            )
            return result, output.read_text()

    def test_the_workflow_reads_the_shared_list_and_decides_from_it(self):
        for changed, eligible in (
            ("src/apps/api/app/config.py", "true"),
            ("src/apps/ios/Kria/App.swift", "true"),
            ("src/apps/api/app/services/mobile_auth.py", "true"),
            ("src/apps/api/app/pipeline/render.py", "false"),
            ("src/apps/ios-tools/x.sh", "false"),
        ):
            with self.subTest(changed=changed):
                result, output = self.run_block(changed)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f"eligible={eligible}\n")

    def test_an_unreadable_or_empty_list_stops_the_release_step(self):
        for broken in ("raise SystemExit(3)\n", "pass\n"):
            with self.subTest(broken=broken):
                result, output = self.run_block("src/apps/ios/Kria/App.swift", broken)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("eligible=", output)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_only_a_dispatched_main_commit_can_release(self):
        # ios.yml dispatches this after a main commit's build and unit tests
        # pass; nothing else (PR, push, workflow_run) may start a release.
        self.assertRegex(self.text, r"(?m)^\s*workflow_dispatch\s*:")
        self.assertRegex(self.text, r"(?m)^\s*head_sha\s*:")
        self.assertNotRegex(self.text, r"(?m)^\s*(workflow_run|pull_request|push)\s*:")
        self.assertRegex(self.text, r"if:\s*github\.ref\s*==\s*['\"]refs/heads/main['\"]")

    def test_checks_out_exact_successful_sha_and_rejects_conflicting_stale_ref(self):
        self.assertIn("inputs.head_sha", self.text)
        self.assertRegex(self.text, r"(?s)checkout@[^\n]+.*?ref:\s*\$\{\{\s*inputs\.head_sha")
        self.assertRegex(self.text, r"(?is)(stale|head_sha|rev-parse|merge-base|ancestor).*(?:exit|fail|error)|(?:exit|fail|error).*(?:stale|head_sha|rev-parse|merge-base|ancestor)")
        # Serialized, in-order releases: refuse only a SHA that left main, and diff
        # release inputs from the last uploaded commit (not just HEAD^).
        self.assertRegex(self.text, r'git merge-base --is-ancestor "\$HEAD_SHA" FETCH_HEAD')
        self.assertIn("Upload and distribute to external TestFlight", self.text)
        self.assertRegex(self.text, r'git diff --quiet "\$base" "\$HEAD_SHA" --')
        self.assertNotIn("Main advanced only outside release inputs", self.text)

    def test_backend_route_changes_wait_for_matching_api_deploy(self):
        """A native release that calls a new API route cannot outrun Fly."""
        gate = re.search(
            r'if git diff --quiet "\$base" "\$HEAD_SHA" -- \\\n+(?P<paths>[^;]*)\; then\n\s*echo "requires_api_deploy=false".*?\n\s*else\n\s*echo "requires_api_deploy=true"',
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(gate, "missing requires-api-deploy diff guard")
        assert gate is not None
        self.assertRegex(gate.group("paths"), r"(?m)^\s*src/apps/api\s*\\$")
        self.assertIn("Wait for a production API deploy that contains this commit", self.text)
        self.assertRegex(self.text, r"compare/\$HEAD_SHA\.\.\.\$deployed_sha")
        self.assertRegex(
            self.text,
            r"if: steps\.gate\.outputs\.eligible == 'true' && steps\.gate\.outputs\.requires_api_deploy == 'true'",
        )

    def test_release_uses_protected_environment_and_never_echoes_secrets(self):
        self.assertRegex(self.text, r"(?m)^\s*environment\s*:\s*testflight-production\s*$")
        # Secret values may be passed to actions/scripts, but not interpolated
        # into logs or enabled shell tracing.
        self.assertNotRegex(self.text, r"(?i)echo\s+[^\n]*\$\{?\{?\s*secrets\.")
        self.assertNotRegex(self.text, r"(?m)^\s*set\s+-x\b")

    def test_preflight_receives_every_secret_it_requires(self):
        match = re.search(
            r"- name: Validate release configuration\n(?P<body>.*?)(?=\n\s*- name:)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "missing release-configuration preflight")
        assert match is not None  # Helps static checkers narrow the capture.
        self.assertRegex(
            match.group("body"),
            r"SIGNING_KEYCHAIN_PASSWORD:\s*\$\{\{\s*secrets\.SIGNING_KEYCHAIN_PASSWORD\s*\}\}",
        )

    def test_pins_a_bundler_version_supported_by_the_pinned_fastlane_client(self):
        self.assertIn("gem install bundler:2.7.2 --no-document", self.text)
        self.assertRegex(self.text, r"bundle _2\.7\.2_ install --gemfile fastlane/Gemfile")
        self.assertRegex(self.text, r"bundle _2\.7\.2_ exec fastlane ios testflight")

    def test_preflight_and_upload_receive_the_beta_review_demo_credentials(self):
        # Fastlane's beta_app_review_info reads these two secrets (KRI-111);
        # both must be validated before the archive step and passed to the
        # upload step that actually calls `fastlane ios testflight`.
        preflight = re.search(
            r"- name: Validate release configuration\n(?P<body>.*?)(?=\n\s*- name:)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(preflight, "missing release-configuration preflight")
        assert preflight is not None
        for name in ("KRIA_BETA_REVIEW_DEMO_USER", "KRIA_BETA_REVIEW_DEMO_PASSWORD"):
            with self.subTest(name=name):
                self.assertRegex(
                    preflight.group("body"),
                    rf"{name}:\s*\$\{{\{{\s*secrets\.{name}\s*\}}\}}",
                )
        required_match = re.search(
            r"required=\((?P<body>.*?)\)", self.text, re.DOTALL
        )
        self.assertIsNotNone(required_match, "missing required=(...) secret list")
        assert required_match is not None
        for name in ("KRIA_BETA_REVIEW_DEMO_USER", "KRIA_BETA_REVIEW_DEMO_PASSWORD"):
            with self.subTest(name=name):
                self.assertIn(name, required_match.group("body"))

        upload = re.search(
            r"- name: Upload and distribute to external TestFlight\n(?P<body>.*?)(?=\n\s*- name:|\Z)",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(upload, "missing TestFlight upload step")
        assert upload is not None
        for name in ("KRIA_BETA_REVIEW_DEMO_USER", "KRIA_BETA_REVIEW_DEMO_PASSWORD"):
            with self.subTest(name=name):
                self.assertRegex(
                    upload.group("body"),
                    rf"{name}:\s*\$\{{\{{\s*secrets\.{name}\s*\}}\}}",
                )


if __name__ == "__main__":
    unittest.main()
