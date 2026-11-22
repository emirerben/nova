"""Prepare blank reviews of local evaluation exports and compare independent ratings."""

import argparse
import csv
import json
from pathlib import Path

from score_offline_exports import (
    EDIT_FEEDBACK_DIMENSIONS,
    calibration,
    digest,
    load_ratings,
)


def prepare(directory, videos):
    rows = [
        {"video": str(video.resolve(strict=True)), "video_sha256": digest(video)}
        for video in videos
    ]
    if len({row["video_sha256"] for row in rows}) != len(rows):
        raise ValueError("Each export must have distinct contents")
    directory.mkdir()
    (directory / "exports.json").write_text(json.dumps(rows, indent=2) + "\n")
    for filename in ("human.csv", "grader.csv"):
        with (directory / filename).open("x", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("video_sha256", "dimension", "rating"))
            for row in rows:
                for dimension in EDIT_FEEDBACK_DIMENSIONS:
                    writer.writerow((row["video_sha256"], dimension, ""))


def compare(directory):
    rows = json.loads((directory / "exports.json").read_text())
    if not rows or len({row["video_sha256"] for row in rows}) != len(rows):
        raise ValueError("Expected a nonempty pack of distinct exports")
    for row in rows:
        if digest(Path(row["video"])) != row["video_sha256"]:
            raise ValueError(f"Export changed since review preparation: {row['video']}")
    human_path = directory / "human.csv"
    judge_path = directory / "grader.csv"
    if human_path.samefile(judge_path):
        raise ValueError("Use independent human and grader reviews")
    human = load_ratings(human_path, rows)
    judge = load_ratings(judge_path, rows)
    report = calibration(human, judge, scope="submitted local export pack")
    report["agreement_gate_passed"] = report.pop("fixture_gate_passed")
    report["pack_size"] = len(rows)
    report["human_ratings"] = len(human)
    report["grader_ratings"] = len(judge)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("directory", type=Path)
    prepare_command.add_argument("videos", type=Path, nargs="+")
    commands.add_parser("compare").add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.directory, args.videos)
        else:
            print(json.dumps(compare(args.directory), indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
