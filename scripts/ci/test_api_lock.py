"""Ways the API lockfile can cost CI time or change what CI tests, written first.

`src/apps/api/uv.lock` pins every package the API jobs install:

- a re-lock brings back the CUDA build of torch (about 2 GB of nvidia wheels no
  runner can use), which is what the unlocked `pip install` downloaded;
- torch and torchvision come from different indexes on Linux, so their C++
  operators do not match (`torchvision::nms does not exist` at first use);
- the shared install step is edited to resolve again instead of installing
  exactly what the lock says, so two jobs of one run can test different code;
- the lock holds several OpenCV distributions that all write the same `cv2`
  directory, uv unpacks them in parallel, and the last writer decides whether
  `cv2.CascadeClassifier` exists (only opencv-contrib-python has it in OpenCV
  5). About 1 install in 12 to 45 came out without it and three face-sampler
  tests failed (first seen 2026-11-21, one day after the lock was adopted).
"""

import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


class ApiLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packages = tomllib.loads((REPO / "src/apps/api/uv.lock").read_text())["package"]
        cls.action = (REPO / ".github/actions/api-env/action.yml").read_text()

    def test_no_gpu_wheels_are_locked(self):
        gpu = sorted(
            p["name"] for p in self.packages
            if p["name"].startswith(("nvidia-", "cuda-")) or p["name"] == "triton"
        )
        self.assertEqual(gpu, [])

    def test_torch_and_torchvision_come_from_the_cpu_index_on_linux(self):
        for name in ("torch", "torchvision"):
            linux = [
                p for p in self.packages
                if p["name"] == name
                and any("sys_platform == 'linux'" in m for m in p.get("resolution-markers", []))
            ]
            self.assertEqual(len(linux), 1, name)
            self.assertEqual(linux[0]["source"], {"registry": CPU_INDEX}, name)

    def test_the_install_step_uses_the_lock_as_written(self):
        self.assertIn("uv sync --frozen", self.action)
        # The one install besides the sync rewrites a package at the version the
        # lock already installed, without touching dependencies.
        installs = [line.strip() for line in self.action.splitlines() if "pip install" in line]
        self.assertEqual(
            installs,
            ['uv pip install --reinstall --no-deps --quiet "opencv-contrib-python==$contrib"'],
        )

    def test_the_lock_still_has_the_opencv_overlap_the_install_step_resolves(self):
        # If this stops being true the reinstall in the action can go.
        overlapping = sorted(p["name"] for p in self.packages if p["name"].startswith("opencv-"))
        self.assertIn("opencv-contrib-python", overlapping)
        self.assertGreater(len(overlapping), 1, overlapping)

    def test_opencv_contrib_is_written_last_and_the_result_is_checked(self):
        sync = self.action.index("uv sync --frozen")
        reinstall = self.action.index("uv pip install --reinstall --no-deps")
        check = self.action.index('hasattr(cv2, "CascadeClassifier")')
        self.assertLess(sync, reinstall)
        self.assertLess(reinstall, check)
        self.assertIn('"opencv-contrib-python==$contrib"', self.action)


if __name__ == "__main__":
    unittest.main()
