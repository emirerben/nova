"""Offline contracts for the TestFlight release lane.

These tests intentionally inspect the release inputs as text.  They do not
need Xcode, Fastlane, Apple credentials, or network access, and tolerate
ordinary YAML/YAML-ish formatting changes while guarding the security
boundaries of the release path.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
ENTITLEMENTS = ROOT / "src/apps/ios/Kria/Kria.entitlements"
PROJECT = ROOT / "src/apps/ios/project.yml"
PRODUCTION_CONFIG = ROOT / "src/apps/ios/Config/Production.xcconfig"
ARCHIVE = ROOT / "scripts/ios/archive-testflight.sh"
FASTFILE = ROOT / "fastlane/Fastfile"
APPFILE = ROOT / "fastlane/Appfile"
WORKFLOW = ROOT / ".github/workflows/testflight.yml"


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


class FastlaneTests(unittest.TestCase):
    def test_lane_uses_app_store_distribution_and_external_group(self):
        text = FASTFILE.read_text()
        self.assertRegex(text, r"(?i)lane\s*:\s*[^\n]*testflight|lane\s+:\s*testflight")
        self.assertRegex(text, r"(?i)pilot|upload_to_testflight")
        self.assertRegex(text, r"(?i)groups?\s*:\s*[^\n]*(external|testflight)")
        self.assertRegex(text, r"(?i)app_store_build_number")
        self.assertRegex(text, r"(?i)initial_build_number\s*:\s*0")
        self.assertRegex(text, r"(?i)latest_build\s*>=\s*build_number")
        self.assertRegex(text, r"(?i)skip(?:ping)? duplicate|already has build")
        self.assertRegex(text, r"(?i)app_version\s*:\s*marketing_version")
        self.assertRegex(text, r"(?i)build_number\s*:\s*build_number")
        # The IPA is archived with App Store distribution before this upload
        # lane; keep the assertion coupled to the same release script.
        self.assertRegex(ARCHIVE.read_text(), r"(?i)app[-_ ]store")
        self.assertTrue(APPFILE.is_file(), "missing fastlane/Appfile")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_only_successful_main_ios_workflow_run_can_release(self):
        self.assertRegex(self.text, r"(?m)^\s*workflow_run\s*:")
        self.assertRegex(self.text, r"(?i)workflows?\s*:[^\n]*ios")
        self.assertRegex(self.text, r"(?i)types?\s*:\s*[^\n]*completed")
        self.assertRegex(self.text, r"workflow_run[^\n]*(?:conclusion|head_branch)|conclusion[^\n]*success")
        self.assertRegex(self.text, r"github\.event\.workflow_run\.conclusion\s*==\s*['\"]success['\"]")
        self.assertRegex(self.text, r"github\.event\.workflow_run\.head_branch\s*==\s*['\"]main['\"]")

    def test_checks_out_exact_successful_sha_and_rejects_conflicting_stale_ref(self):
        self.assertIn("github.event.workflow_run.head_sha", self.text)
        self.assertRegex(self.text, r"(?s)checkout@[^\n]+.*?ref:\s*\$\{\{\s*github\.event\.workflow_run\.head_sha")
        self.assertRegex(self.text, r"(?is)(stale|head_sha|rev-parse|merge-base|ancestor).*(?:exit|fail|error)|(?:exit|fail|error).*(?:stale|head_sha|rev-parse|merge-base|ancestor)")
        self.assertRegex(self.text, r'git diff --quiet "\$HEAD_SHA" "FETCH_HEAD" --')
        self.assertIn("Main advanced only outside release inputs", self.text)

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


if __name__ == "__main__":
    unittest.main()
