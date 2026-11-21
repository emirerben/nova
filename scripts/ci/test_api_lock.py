"""Ways the API lockfile can cost CI time or change what CI tests, written first.

`src/apps/api/uv.lock` pins every package the API jobs install:

- a re-lock brings back the CUDA build of torch (about 2 GB of nvidia wheels no
  runner can use), which is what the unlocked `pip install` downloaded;
- torch and torchvision come from different indexes on Linux, so their C++
  operators do not match (`torchvision::nms does not exist` at first use);
- the shared install step is edited to resolve again instead of installing
  exactly what the lock says, so two jobs of one run can test different code;
- the cache key forgets the lock, so a changed lock restores the old packages.
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
        self.assertNotIn("pip install", self.action)

    def test_the_environment_cache_is_keyed_on_the_lock(self):
        self.assertIn("hashFiles('src/apps/api/uv.lock')", self.action)


if __name__ == "__main__":
    unittest.main()
