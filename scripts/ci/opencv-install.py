#!/usr/bin/env python3
"""Report which OpenCV distributions a Python environment holds, and fail unless it is exactly one.

The OpenCV distributions all unpack into the same `cv2/` directory and no
installer knows they overlap, so the last one written wins. This reads the
environment's own records (each distribution's RECORD) against the files on
disk. Standard library only: it runs inside the production image.

    python scripts/ci/opencv-install.py                       # report only
    python scripts/ci/opencv-install.py --require opencv-contrib-python
    python scripts/ci/opencv-install.py --only opencv-contrib-python   # after a pip install

`--only` is for a fresh pip install (an image layer, a CI job). pip cannot leave
a requirement out, and a later plain `pip install` in the same environment
brings the removed distribution back, on top. It removes every
OpenCV distribution and installs the named one again, alone, at the version
that was installed, then checks the files against its record. It does not
load `cv2` (some jobs that install the API have no libGL); `--require` does,
and also fails when `cv2` does not import or has no `CascadeClassifier`. A uv
install from the lock needs no repair (pyproject.toml `[tool.uv]
exclude-dependencies`).

Guard: scripts/ci/test_opencv_install.py.
"""

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import sysconfig
from pathlib import Path


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _record_hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def inspect(site_packages: Path) -> dict:
    """What is installed under `site_packages` and whether `cv2/` on disk agrees with it."""
    on_disk = {
        path.relative_to(site_packages).as_posix(): _record_hash(path)
        for path in sorted((site_packages / "cv2").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    distributions = []
    owned: set[str] = set()
    for distribution in importlib.metadata.distributions(path=[str(site_packages)]):
        name = _canonical(distribution.metadata["Name"] or "")
        if not name.startswith("opencv-"):
            continue
        # RECORD read directly: `distribution.files` drops entries whose file is gone
        # (Python 3.12 and later), which is exactly the case this has to see.
        recorded = {
            path: digest.removeprefix("sha256=")
            for path, digest, _size in csv.reader(
                (distribution.read_text("RECORD") or "").splitlines()
            )
            if path.startswith("cv2/") and digest and "__pycache__" not in path
        }
        owned.update(recorded)
        distributions.append(
            {
                "name": name,
                "version": distribution.version,
                "files": len(recorded),
                "missing": sorted(file for file in recorded if file not in on_disk),
                "rewritten": sorted(
                    file
                    for file, digest in recorded.items()
                    if on_disk.get(file, digest) != digest
                ),
            }
        )
    distributions.sort(key=lambda item: item["name"])
    return {
        "site_packages": str(site_packages),
        "distributions": distributions,
        # A distribution is on top when every file it recorded is on disk as it wrote it.
        "on_top": [
            item["name"]
            for item in distributions
            if not item["missing"] and not item["rewritten"]
        ],
        "unowned": sorted(file for file in on_disk if file not in owned),
        "cv2_files": len(on_disk),
    }


def problems(report: dict, want: str) -> list[str]:
    """Why this environment is not exactly the one wanted OpenCV distribution; empty when it is."""
    names = [item["name"] for item in report["distributions"]]
    if not names:
        return ["no OpenCV distribution is installed"]
    if len(names) > 1:
        return [
            f"{len(names)} OpenCV distributions share cv2/: {', '.join(names)} (on top: {', '.join(report['on_top']) or 'none'})"
        ]
    only = report["distributions"][0]
    if only["name"] != _canonical(want):
        return [
            f"the installed OpenCV distribution is {only['name']}, not {_canonical(want)}"
        ]
    found = []
    if only["missing"]:
        found.append(
            f"{only['name']} lists files that are not on disk: {', '.join(only['missing'][:5])}"
        )
    if only["rewritten"]:
        found.append(
            f"{only['name']} files were rewritten by something else: {', '.join(only['rewritten'][:5])}"
        )
    if report["unowned"]:
        found.append(
            f"cv2/ holds files no installed distribution owns: {', '.join(report['unowned'][:5])}"
        )
    return found


def only(site_packages: Path, want: str, run_pip) -> int:
    """Leave `want` as the only OpenCV distribution. `run_pip(arguments)` returns pip's exit status."""
    report = inspect(site_packages)
    if not problems(report, want):
        return 0
    versions = {item["name"]: item["version"] for item in report["distributions"]}
    wanted = _canonical(want)
    if wanted not in versions:
        print(
            f"::error::{wanted} is not installed; nothing was removed", file=sys.stderr
        )
        return 1
    # All of them, the wanted one included: removing only the others deletes the
    # files they share.
    if run_pip(["uninstall", "--yes", *sorted(versions)]) != 0:
        return 1
    pinned = f"{wanted}=={versions[wanted]}"
    return 1 if run_pip(["install", "--no-deps", "--no-cache-dir", pinned]) else 0


def _load_cv2():
    import cv2

    return cv2


def main(argv: list[str], load_cv2=_load_cv2) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require",
        metavar="DISTRIBUTION",
        help="fail unless this is the only OpenCV distribution and cv2 loads",
    )
    parser.add_argument(
        "--only",
        metavar="DISTRIBUTION",
        help="with pip: make this the only OpenCV distribution; does not load cv2",
    )
    parser.add_argument(
        "--site-packages", type=Path, default=Path(sysconfig.get_paths()["purelib"])
    )
    args = parser.parse_args(argv)
    if args.only:
        pip = [sys.executable, "-m", "pip"]
        if only(
            args.site_packages,
            args.only,
            lambda arguments: subprocess.call(pip + arguments),
        ):
            return 1
    report = inspect(args.site_packages)
    found = problems(report, args.only) if args.only else []
    if args.require:
        found += problems(report, args.require)
        try:
            cv2 = load_cv2()
        except ImportError as error:
            found.append(f"cv2 does not import: {error}")
        else:
            binaries = sorted(Path(cv2.__file__).parent.glob("cv2*.so"))
            report["cv2"] = {
                "version": cv2.__version__,
                "file": cv2.__file__,
                "cascade_classifier": hasattr(cv2, "CascadeClassifier"),
                "binary_sha256": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in binaries
                },
            }
            if not report["cv2"]["cascade_classifier"]:
                found.append("cv2 has no CascadeClassifier")
    print(json.dumps(report, indent=2))
    for problem in found:
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
