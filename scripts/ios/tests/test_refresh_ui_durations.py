"""Offline checks for the ui-test-durations.json refresh (no network, no gh)."""

import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "refresh-ui-durations.py"
spec = importlib.util.spec_from_file_location("refresh_ui_durations", SCRIPT)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

LOG = """\
Test Case '-[KriaUITests.CreationUITests testLaunch]' started (Iteration 1 of 3).
Test Case '-[KriaUITests.CreationUITests testLaunch]' failed (62.018 seconds).
Test Case '-[KriaUITests.CreationUITests testLaunch]' started (Iteration 2 of 3).
Test Case '-[KriaUITests.CreationUITests testLaunch]' passed (31.842 seconds).
Test Case '-[KriaUITests.ProjectsUITests testGallery]' passed (6.700 seconds).
Test Case '-[KriaTests.UploadTests testRetry]' passed (0.010 seconds).
"""


class RefreshDurationsTests(unittest.TestCase):
    def test_only_passing_ui_attempts_are_samples(self):
        self.assertEqual(
            refresh.passed_durations(LOG),
            [
                ("KriaUITests/CreationUITests/testLaunch", 31.842),
                ("KriaUITests/ProjectsUITests/testGallery", 6.7),
            ],
        )

    def test_medians_cover_exactly_the_inventory(self):
        inventory = {
            "KriaUITests/A/testSampled",
            "KriaUITests/A/testUnsampled",
            "KriaUITests/A/testNew",
        }
        samples = {
            "KriaUITests/A/testSampled": [10.0, 30.0, 11.04],
            "KriaUITests/Gone/testDeleted": [5.0],
        }
        previous = {"KriaUITests/A/testUnsampled": 42.0, "KriaUITests/Gone/testDeleted": 5.0}
        self.assertEqual(
            refresh.medians(samples, inventory, previous),
            {"KriaUITests/A/testSampled": 11.0, "KriaUITests/A/testUnsampled": 42.0},
        )


if __name__ == "__main__":
    unittest.main()
