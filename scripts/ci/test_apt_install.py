"""Exercise apt-install.sh's hard timeouts and retries against a stub apt-get."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().with_name("apt-install.sh")
ACQUIRE = (
    "-o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15"
)

# Records each call, then acts out the next line of `plan` (default: succeed).
STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/calls"
outcome=$(head -n 1 "$STUB_DIR/plan" 2>/dev/null || true)
sed -i.bak 1d "$STUB_DIR/plan" 2>/dev/null || true
case "$outcome" in
  hang) sleep 60 ;;
  fail) exit 100 ;;
esac
"""


@unittest.skipUnless(shutil.which("timeout"), "needs coreutils timeout")
class AptInstallTests(unittest.TestCase):
    def run_script(self, plan, *packages, attempts=4, archives=False):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp, "apt-get")
            stub.write_text(STUB)
            stub.chmod(0o755)
            Path(tmp, "plan").write_text("".join(f"{line}\n" for line in plan))
            env = {
                **os.environ,
                "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}",
                "STUB_DIR": tmp,
                "APT_ATTEMPTS": str(attempts),
                "APT_UPDATE_TIMEOUT": "1",
                "APT_DOWNLOAD_TIMEOUT": "1",
                "APT_RETRY_DELAY": "0",
            }
            env.pop("APT_ARCHIVES_DIR", None)
            if archives:
                env["APT_ARCHIVES_DIR"] = str(Path(tmp, "debs"))
            result = subprocess.run(
                ["bash", str(SCRIPT), *packages],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            calls_file = Path(tmp, "calls")
            calls = calls_file.read_text().splitlines() if calls_file.exists() else []
            self.partial_exists = Path(tmp, "debs", "partial").is_dir()
        return result, calls

    def test_hung_update_is_killed_and_retried(self):
        result, calls = self.run_script(["hang"], "ffmpeg", "libegl1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("apt-get update attempt 1/4 timed out after 1s", result.stdout)
        self.assertEqual(
            calls,
            [
                f"{ACQUIRE} update",
                f"{ACQUIRE} update",
                f"{ACQUIRE} install -y --no-install-recommends --download-only ffmpeg libegl1",
                "install -y --no-install-recommends --no-download ffmpeg libegl1",
            ],
        )

    def test_failed_download_is_retried_before_offline_install(self):
        result, calls = self.run_script(["ok", "fail"], "ffmpeg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("apt-get download attempt 1/4 failed (exit 100)", result.stdout)
        self.assertEqual(
            calls,
            [
                f"{ACQUIRE} update",
                f"{ACQUIRE} install -y --no-install-recommends --download-only ffmpeg",
                f"{ACQUIRE} install -y --no-install-recommends --download-only ffmpeg",
                "install -y --no-install-recommends --no-download ffmpeg",
            ],
        )

    def test_gives_up_without_installing_after_last_attempt(self):
        result, calls = self.run_script(["hang", "fail"], "ffmpeg", attempts=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::apt-get update failed after 2 attempts", result.stdout)
        self.assertEqual(calls, [f"{ACQUIRE} update", f"{ACQUIRE} update"])

    def test_requires_packages(self):
        result, calls = self.run_script([])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_download_exhaustion_never_starts_install(self):
        result, calls = self.run_script(["ok", "fail", "fail"], "ffmpeg", attempts=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "::error::apt-get download failed after 2 attempts", result.stdout
        )
        self.assertEqual(
            calls,
            [
                f"{ACQUIRE} update",
                f"{ACQUIRE} install -y --no-install-recommends --download-only ffmpeg",
                f"{ACQUIRE} install -y --no-install-recommends --download-only ffmpeg",
            ],
        )

    def test_archives_dir_keeps_debs_where_ci_can_cache_them(self):
        # CI restores ~/.cache/apt-debs between runs; both apt phases must read
        # and write that directory so a slow mirror only costs the first run.
        result, calls = self.run_script(["ok", "ok", "ok"], "ffmpeg", archives=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.partial_exists, "apt needs <archives>/partial")
        archives = [c for c in calls if "Dir::Cache::archives=" in c]
        self.assertEqual(len(archives), 2)
        self.assertRegex(
            archives[0],
            rf"^{ACQUIRE} -o Dir::Cache::archives=\S+/debs install -y "
            r"--no-install-recommends --download-only ffmpeg$",
        )
        self.assertRegex(
            archives[1],
            r"^-o Dir::Cache::archives=\S+/debs -o APT::Keep-Downloaded-Packages=true "
            r"install -y --no-install-recommends --no-download ffmpeg$",
        )
        self.assertEqual(calls[0], f"{ACQUIRE} update")

    def test_install_failure_is_propagated_without_retrying_dpkg(self):
        result, calls = self.run_script(["ok", "ok", "fail"], "ffmpeg")
        self.assertEqual(result.returncode, 100)
        self.assertEqual(
            calls,
            [
                f"{ACQUIRE} update",
                f"{ACQUIRE} install -y --no-install-recommends --download-only ffmpeg",
                "install -y --no-install-recommends --no-download ffmpeg",
            ],
        )


if __name__ == "__main__":
    unittest.main()
