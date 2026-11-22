"""Score the seven generated phone montage fixtures and a wrong-content control.

Run with the API Python. Inputs come from scripts/ios/phone-montage-render-e2e.py
and DeviceMontageRenderE2ETests. Never reads production or calls a model.
"""

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))

from app.models import EDIT_FEEDBACK_DIMENSIONS, EDIT_FEEDBACK_RATINGS  # noqa: E402
from app.services.grader_calibration import (  # noqa: E402
    GroundTruth,
    build_confusion_matrix,
    evaluate_gate,
)

CASES = (
    "narration",
    "subtitled_sentence",
    "subtitled_word",
    "subtitled_landscape_fit",
    "subtitled_fullscreen_visuals",
    "talking_head",
    "narrated",
)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def snapshot(folder):
    manifest = json.loads((folder / "e2e.json").read_text())["cases"]
    if set(manifest) != set(CASES):
        raise ValueError("Expected all seven synthetic montage fixture cases")
    cases = {}
    for name, case in manifest.items():
        status = json.loads((folder / case["status_file"]).read_text())
        recipe = status["request"]["recipe"]
        recipe["required_capabilities"].sort()
        sources = [item["file"] for item in case["clips"]]
        sources.extend(
            case[key] for key in ("music_file", "voiceover_file") if case.get(key)
        )
        sources.extend(case.get("visual_files", {}).values())
        cases[name] = {
            "expectations": case,
            "recipe_sha256": hashlib.sha256(
                json.dumps(recipe, sort_keys=True).encode()
            ).hexdigest(),
            "source_sha256": {file: digest(folder / file) for file in sources},
        }
    return cases


def score(movie, case):
    metadata = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                movie,
            ]
        )
    )
    checks = {
        "duration": abs(float(metadata["format"]["duration"]) - case["duration_s"])
        <= 0.1
    }
    checks["aac_audio"] = any(s["codec_name"] == "aac" for s in metadata["streams"])
    checks["portrait_video"] = any(
        s.get("width") == 1080 and s.get("height") == 1920 for s in metadata["streams"]
    )
    for sample in case["samples"]:
        # The native oracle measures sRGB. The export is tagged with the BT.709 transfer.
        crop = f"colorspace=all=bt709:trc=srgb,format=rgb24,crop=1:1:{sample['x']}:{sample['y']}"
        pixel = subprocess.check_output(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(sample["t"]),
                "-i",
                movie,
                "-vf",
                crop,
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ]
        )
        checks[sample["name"]] = len(pixel) == 3 and all(
            abs(v - expected) <= 40 for v, expected in zip(pixel, sample["rgb"])
        )
    return checks


def load_ratings(file, rows):
    ratings = {}
    exports = {row["video_sha256"] for row in rows}
    with file.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if set(reader.fieldnames or ()) != {"video_sha256", "dimension", "rating"}:
            raise ValueError(f"Expected video_sha256,dimension,rating in {file}")
        seen = set()
        for record in reader:
            video = record["video_sha256"]
            dimension = record["dimension"]
            rating = record["rating"]
            key = (video, dimension)
            if video not in exports or dimension not in EDIT_FEEDBACK_DIMENSIONS:
                raise ValueError(f"Unknown video or dimension in {file}: {key}")
            if key in seen or (rating != "" and rating not in EDIT_FEEDBACK_RATINGS):
                raise ValueError(f"Duplicate or invalid rating in {file}: {key}")
            seen.add(key)
            if rating != "":
                ratings[key] = rating
    return ratings


def calibration(human, judge, *, scope="synthetic fixture review only"):
    pairs = []
    dimensions = {}
    for dimension in EDIT_FEEDBACK_DIMENSIONS:
        decided = [
            (truth, judge[key])
            for key, truth in human.items()
            if key[1] == dimension
            and key in judge
            and (truth, judge[key]) != ("not_applicable", "not_applicable")
        ]
        matches = sum(truth == prediction for truth, prediction in decided)
        dimensions[dimension] = {
            "paired": len(decided),
            "agree": matches,
            "agreement": matches / len(decided) if decided else None,
        }
        if dimension == "overall_quality":
            for truth, prediction in decided:
                if truth in ("good", "bad"):
                    label = (
                        GroundTruth.LIKED if truth == "good" else GroundTruth.DISLIKED
                    )
                    pairs.append(
                        (
                            label,
                            {
                                "good": "auto_pass",
                                "bad": "auto_reject",
                                "mixed": "escalate",
                                "not_applicable": "escalate",
                            }[prediction],
                        )
                    )
    matrix = build_confusion_matrix(pairs)
    passed, reasons = evaluate_gate(matrix)
    measured = {
        d: result["agreement"] for d, result in dimensions.items() if result["paired"]
    }
    return {
        "scope": scope,
        "decided_edits": matrix.decided,
        "agreement_pct": matrix.band_agreement_pct if matrix.decided else None,
        "fixture_gate_passed": passed,
        "gate_reasons": reasons if matrix.decided else ["No paired decisive ratings"],
        "dimensions": dimensions,
        "weakest_dimensions": [
            d for d in measured if measured[d] == min(measured.values())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered", type=Path)
    parser.add_argument("regenerated", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--human-ratings", type=Path)
    parser.add_argument("--grader-ratings", type=Path)
    args = parser.parse_args()
    if bool(args.human_ratings) != bool(args.grader_ratings):
        parser.error("Supply both independent ratings files")
    if args.human_ratings and args.human_ratings.samefile(args.grader_ratings):
        parser.error("Human and grader ratings must come from independent reviews")
    if args.rendered.samefile(args.regenerated):
        parser.error("Supply two independently generated fixture directories")
    inventory = snapshot(args.rendered)
    repeated = snapshot(args.regenerated)
    rows = []
    for name in CASES:
        movie = args.rendered / "frames" / f"{name}.mp4"
        case = inventory[name]
        try:
            checks = score(movie, case["expectations"])
            video_digest = digest(movie)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            video_digest = None
            checks = {"readable_export": False}
            print(f"{name}: {exc}", file=sys.stderr)
        rows.append(
            {
                "case": name,
                "coverage": "synthetic_native_export",
                "reproduced_inputs_and_recipe": case == repeated[name],
                "recipe": str(
                    (args.rendered / case["expectations"]["status_file"]).resolve()
                ),
                "recipe_sha256": case["recipe_sha256"],
                "sources": case["source_sha256"],
                "video": str(movie.resolve()),
                "video_sha256": video_digest,
                "checks": checks,
            }
        )

    # Both are six-second captioned exports with AAC and the same canvas.
    control = {}
    if next(row for row in rows if row["case"] == "subtitled_word")["video_sha256"]:
        control = score(
            args.rendered / "frames/subtitled_word.mp4",
            inventory["subtitled_sentence"]["expectations"],
        )
    rejected = (
        control.get("duration", False)
        and control["aac_audio"]
        and control["portrait_video"]
        and not control["c0"]
    )
    human = load_ratings(args.human_ratings, rows) if args.human_ratings else {}
    judge = load_ratings(args.grader_ratings, rows) if args.grader_ratings else {}
    for row in rows:
        row["rubric"] = {
            d: judge.get((row["video_sha256"], d)) for d in EDIT_FEEDBACK_DIMENSIONS
        }
    report = {
        "base_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "cases": rows,
        "wrong_content_control": {
            "expected": "subtitled_sentence",
            "used": "subtitled_word",
            "checks": control,
            "rejected": rejected,
        },
        "real_incident_edits": 0,
        "paid_model_calls": 0,
        "creator_requirements_met": None,
        "overclaims": None,
        "calibration": calibration(human, judge),
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    return (
        0
        if rejected
        and all(
            row["reproduced_inputs_and_recipe"] and all(row["checks"].values())
            for row in rows
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
