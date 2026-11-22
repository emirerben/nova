"""Ways an environment can end up with the wrong `cv2`, written first.

Four OpenCV distributions (`opencv-python`, `-headless`, `-contrib-python`,
`-contrib-python-headless`) unpack into the same `cv2/` directory. Installers
do not know they overlap, so:

- two or more are installed and the last one written decides which `cv2`
  loads (the face sampler lost `cv2.CascadeClassifier` this way, 2026-11-21);
- one is uninstalled and takes the shared files with it, so a distribution is
  listed whose files are gone;
- one is installed over another and a file of the old one stays behind
  (`cv2/` then holds files no installed distribution owns);
- the one that is left is not the one the project names;
- the check reads a different environment from the one it was asked about.

pip cannot be told to leave a requirement out (scenedetect requires
`opencv-python`, mediapipe requires `opencv-contrib-python`), so after a pip
install `--only` removes every OpenCV distribution and puts the wanted one back
alone. Ways that can go wrong:

- only the unwanted ones are removed, which deletes the files they share with
  the wanted one;
- the wanted one comes back at another version than the install resolved, or
  brings dependencies with it;
- the wanted one was never installed, and everything is removed anyway;
- pip fails half way and the step still reports success.

`--only` reads records and files only. It runs in jobs that never load `cv2`
and have no libGL for it, so it must not import it. `--require` also loads
`cv2`, because one distribution on disk that cannot be imported, or has no
`CascadeClassifier`, is still a broken environment.
"""

import base64
import contextlib
import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "opencv_install", REPO / "scripts/ci/opencv-install.py"
)
opencv_install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(opencv_install)


def record_hash(data: bytes) -> str:
    return (
        "sha256="
        + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    )


class Site:
    """A throwaway site-packages with fake OpenCV distributions in it."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, relative: str, data: bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def install(
        self, name: str, files: dict[str, bytes], version: str = "5.1.0.95"
    ) -> None:
        """What an installer does: write the files, then the distribution's own record of them."""
        for relative, data in files.items():
            self.write(relative, data)
        info = f"{name.replace('-', '_')}-{version}.dist-info"
        self.write(
            f"{info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        )
        lines = [
            f"{relative},{record_hash(data)},{len(data)}"
            for relative, data in files.items()
        ]
        lines += [f"{info}/METADATA,,", f"{info}/RECORD,,"]
        self.write(f"{info}/RECORD", ("\n".join(lines) + "\n").encode())


CONTRIB = {
    "cv2/__init__.py": b"contrib init",
    "cv2/cv2.abi3.so": b"contrib binary",
    "cv2/data/__init__.py": b"",
}
HEADLESS = {
    "cv2/__init__.py": b"headless init",
    "cv2/cv2.abi3.so": b"headless binary",
    "cv2/data/__init__.py": b"",
}


class OpenCvInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.site = Site(Path(directory.name))

    def problems(self, want: str = "opencv-contrib-python") -> list[str]:
        return opencv_install.problems(opencv_install.inspect(self.site.root), want)

    def test_one_distribution_with_its_own_files_is_clean(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        self.assertEqual(self.problems(), [])

    def test_two_distributions_are_refused_even_when_the_wanted_one_is_on_top(self):
        self.site.install("opencv-python-headless", HEADLESS)
        self.site.install("opencv-contrib-python", CONTRIB)
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("opencv-contrib-python, opencv-python-headless", found[0])

    def test_the_report_says_which_distribution_is_on_top(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        self.site.install("opencv-python-headless", HEADLESS)
        report = opencv_install.inspect(self.site.root)
        self.assertEqual(report["on_top"], ["opencv-python-headless"])

    def test_the_only_distribution_must_be_the_one_the_project_names(self):
        self.site.install("opencv-python-headless", HEADLESS)
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("opencv-python-headless", found[0])

    def test_files_taken_by_another_uninstall_are_refused(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        (self.site.root / "cv2/cv2.abi3.so").unlink()
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("cv2/cv2.abi3.so", found[0])

    def test_a_file_rewritten_by_another_distribution_is_refused(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        self.site.write("cv2/cv2.abi3.so", b"headless binary")
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("cv2/cv2.abi3.so", found[0])

    def test_a_file_left_behind_by_a_removed_distribution_is_refused(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        self.site.write("cv2/qt/plugins/libqxcb.so", b"left behind")
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("cv2/qt/plugins/libqxcb.so", found[0])

    def test_bytecode_written_at_import_is_not_a_leftover(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        self.site.write("cv2/__pycache__/__init__.cpython-311.pyc", b"bytecode")
        self.assertEqual(self.problems(), [])

    def test_no_opencv_at_all_is_refused(self):
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("no OpenCV distribution", found[0])

    def test_names_are_compared_the_way_installers_normalise_them(self):
        self.site.install("opencv_contrib_python", CONTRIB)
        self.assertEqual(self.problems("OpenCV-Contrib-Python"), [])


class OnlyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.site = Site(Path(directory.name))
        self.commands: list[list[str]] = []

    def run_pip(self, command: list[str], fail_on: str | None = None) -> int:
        self.commands.append(command)
        return 1 if fail_on and fail_on in command else 0

    def test_every_opencv_distribution_is_removed_and_the_wanted_one_put_back_alone(
        self,
    ):
        self.site.install("opencv-python", HEADLESS)
        self.site.install("opencv-contrib-python", CONTRIB, version="5.1.0.95")
        status = opencv_install.only(
            self.site.root, "opencv-contrib-python", self.run_pip
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            self.commands,
            [
                ["uninstall", "--yes", "opencv-contrib-python", "opencv-python"],
                [
                    "install",
                    "--no-deps",
                    "--no-cache-dir",
                    "opencv-contrib-python==5.1.0.95",
                ],
            ],
        )

    def test_one_clean_distribution_is_left_alone(self):
        self.site.install("opencv-contrib-python", CONTRIB)
        self.assertEqual(
            opencv_install.only(self.site.root, "opencv-contrib-python", self.run_pip),
            0,
        )
        self.assertEqual(self.commands, [])

    def test_nothing_is_removed_when_the_wanted_one_is_not_installed(self):
        self.site.install("opencv-python", HEADLESS)
        self.assertEqual(
            opencv_install.only(self.site.root, "opencv-contrib-python", self.run_pip),
            1,
        )
        self.assertEqual(self.commands, [])

    def test_a_failed_uninstall_stops_before_the_install(self):
        self.site.install("opencv-python", HEADLESS)
        self.site.install("opencv-contrib-python", CONTRIB)
        status = opencv_install.only(
            self.site.root,
            "opencv-contrib-python",
            lambda command: self.run_pip(command, fail_on="uninstall"),
        )
        self.assertEqual(status, 1)
        self.assertEqual([command[0] for command in self.commands], ["uninstall"])

    def test_a_failed_install_is_reported(self):
        self.site.install("opencv-python", HEADLESS)
        self.site.install("opencv-contrib-python", CONTRIB)
        status = opencv_install.only(
            self.site.root,
            "opencv-contrib-python",
            lambda command: self.run_pip(command, fail_on="install"),
        )
        self.assertEqual(status, 1)


class FakeCv2:
    __version__ = "5.1.0"

    def __init__(self, file: Path, cascade: bool):
        self.__file__ = str(file)
        if cascade:
            self.CascadeClassifier = object


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.site = Site(Path(directory.name))
        self.site.install("opencv-contrib-python", CONTRIB)
        self.arguments = ["--site-packages", str(self.site.root)]

    def run_main(self, flag: str, load_cv2) -> int:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return opencv_install.main(
                [flag, "opencv-contrib-python", *self.arguments], load_cv2=load_cv2
            )

    def cv2(self, cascade: bool = True):
        return lambda: FakeCv2(self.site.root / "cv2/__init__.py", cascade)

    def refuse(self):
        raise AssertionError("cv2 must not be loaded")

    def broken(self):
        raise ImportError("libGL.so.1: cannot open shared object file")

    def test_only_does_not_load_cv2(self):
        self.assertEqual(self.run_main("--only", self.refuse), 0)

    def test_require_passes_when_cv2_loads_with_the_classifier(self):
        self.assertEqual(self.run_main("--require", self.cv2()), 0)

    def test_require_fails_when_cv2_does_not_import(self):
        self.assertEqual(self.run_main("--require", self.broken), 1)

    def test_require_fails_without_the_cascade_classifier(self):
        self.assertEqual(self.run_main("--require", self.cv2(cascade=False)), 1)

    def test_require_fails_on_a_second_distribution_even_when_cv2_loads(self):
        self.site.install("opencv-python", HEADLESS)
        self.assertEqual(self.run_main("--require", self.cv2()), 1)


if __name__ == "__main__":
    unittest.main()
