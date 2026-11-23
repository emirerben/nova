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
  The lock now holds one; what brings a second one back:
  - a dependency that requires another OpenCV distribution by name
    (scenedetect requires `opencv-python`), unless pyproject.toml excludes it;
  - a place that installs with pip, which ignores that exclusion: the
    production image and three workflows. Each installs once into a fresh
    environment and must run `scripts/ci/opencv-install.py --only` afterwards;
  - the check after the locked install is dropped, so a wrong `cv2` is found
    by whichever test happens to need the missing function.
"""

import re
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
OPENCV = "opencv-contrib-python"


class ApiLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packages = tomllib.loads((REPO / "src/apps/api/uv.lock").read_text())[
            "package"
        ]
        cls.action = (REPO / ".github/actions/api-env/action.yml").read_text()

    def test_no_gpu_wheels_are_locked(self):
        gpu = sorted(
            p["name"]
            for p in self.packages
            if p["name"].startswith(("nvidia-", "cuda-")) or p["name"] == "triton"
        )
        self.assertEqual(gpu, [])

    def test_torch_and_torchvision_come_from_the_cpu_index_on_linux(self):
        for name in ("torch", "torchvision"):
            linux = [
                p
                for p in self.packages
                if p["name"] == name
                and any(
                    "sys_platform == 'linux'" in m
                    for m in p.get("resolution-markers", [])
                )
            ]
            self.assertEqual(len(linux), 1, name)
            self.assertEqual(linux[0]["source"], {"registry": CPU_INDEX}, name)

    def test_the_install_step_uses_the_lock_as_written(self):
        self.assertIn("uv sync --frozen", self.action)
        # Nothing is installed on top of the lock.
        installs = [
            line.strip() for line in self.action.splitlines() if "pip install" in line
        ]
        self.assertEqual(installs, [])

    def test_the_lock_holds_one_opencv_distribution(self):
        opencv = sorted(
            p["name"] for p in self.packages if p["name"].startswith("opencv-")
        )
        self.assertEqual(opencv, [OPENCV])

    def test_the_project_names_that_distribution_and_excludes_the_others(self):
        project = tomllib.loads((REPO / "src/apps/api/pyproject.toml").read_text())
        named = [
            d for d in project["project"]["dependencies"] if d.startswith("opencv-")
        ]
        self.assertEqual(len(named), 1, named)
        self.assertTrue(named[0].startswith(OPENCV + ">="), named)
        self.assertEqual(
            sorted(project["tool"]["uv"]["exclude-dependencies"]),
            [
                "opencv-contrib-python-headless",
                "opencv-python",
                "opencv-python-headless",
            ],
        )

    def test_the_locked_install_is_checked_for_one_opencv(self):
        sync = self.action.index("uv sync --frozen")
        check = self.action.index(f'scripts/ci/opencv-install.py" --require {OPENCV}')
        self.assertLess(sync, check)

    def test_every_pip_install_of_the_api_is_followed_by_the_opencv_repair(self):
        # pip cannot exclude a requirement, so each of these ends with two
        # distributions in cv2/ until the repair runs.
        repair = f"opencv-install.py --only {OPENCV}"
        installs = re.compile(
            r"pip install (?:--no-cache-dir )?(?:-e )?(?:\.|['\"]\.\[|-r /tmp/requirements\.txt)"
        )
        files = [
            *sorted((REPO / ".github/workflows").glob("*.yml")),
            REPO / "Dockerfile",
        ]
        found = 0
        for path in files:
            text = path.read_text()
            positions = [match.start() for match in installs.finditer(text)]
            found += len(positions)
            repairs = [match.start() for match in re.finditer(re.escape(repair), text)]
            self.assertEqual(len(repairs), len(positions), path.name)
            for install, fix in zip(positions, repairs):
                self.assertLess(install, fix, path.name)
        # The pattern still finds them: the image and three workflows
        # (fly-deploy, ios, single-pass-parity). KRI-1551 moved
        # agent-evals and request-following-regressions to the locked install.
        # Not the Makefile: a venv that lives on gets `opencv-python` back, on top,
        # from the next plain pip install, so repairing it once would only move
        # the problem. Local pip venvs keep their overlap, as before.
        self.assertEqual(found, 4)

    def test_the_built_image_is_checked_for_one_opencv(self):
        build = (REPO / ".github/workflows/docker-build.yml").read_text()
        self.assertIn(f"opencv-install.py --require {OPENCV}", build)


if __name__ == "__main__":
    unittest.main()
