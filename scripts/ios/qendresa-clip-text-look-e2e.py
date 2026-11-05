#!/usr/bin/env python3
"""Before/after simulator render harness for Qendresa's "one pinned line in TikTok's look".

Her ask (silent 11 s clip, one text line pinned in the middle, small white TikTok-style
text, nothing else) is compiled through the same chain the worker runs for a one-clip phone
montage, once with the strategy today's Main Creator writes (flag off) and once with the
strategy the new prompt teaches (flag `clip_text_tiktok_look_enabled` on):

    plan_unified_montage -> compile_execution_plan -> phone text-style pass
    (`style_guided_generation` inside `phone_text_policy_scope`) ->
    `compile_phone_guided_repaired` (guided phone compiler + `validate_phone_pilot_recipe`)

Subcommands (run with the API venv; it imports `app`; needs ffmpeg on PATH):

    prepare --clip <mp4> --out <DIR> [--face-boxes <json>] [--cases a,b]
        Cuts the first 11.0 s of --clip (audio removed, H.264, 1080x1920) into
        DIR/source/clip.mp4 and writes DIR/cases/<name>/{recipe.json,assets.json,plan.json}
        for: before, before_pin_only, after, after_with_title and (with --face-boxes)
        after_face.
    score DIR
        Runs Downloads/kria-qendresa-test/tools/check_result.sh (type Q3) on every
        cases/<name>/phone.mp4, prints a table of the report rules, writes DIR/score.json.

Where this is not the production code path:
  * The face sampler. Locally the OpenCV cascade is missing, so the sampler finds no faces.
    For `after_face` the sampler is replaced (`build_phone_face_sampler`) with one that returns
    the regions from --face-boxes. THOSE BOXES ARE MEASURED EXTERNALLY (Apple Vision on the
    clip) AND INJECTED; they are not produced by the production sampler.
  * `download_to_file` is patched to copy the local clip instead of fetching the proxy.
  * The font-picking model call (`select_fonts` -> PhoneTypographyAgent, a paid Gemini call in
    production) is replaced with "no pick", so a title/pin keeps the style pass's own default
    font. No model call is ever made by this script.
  * No database: the job's stored values (strategy, bindings, approved length) are supplied
    directly, and the approved length plan is not built (the strategy's `target_duration_s`
    plus `creator_duration_proven=True` is what the planner reads for "Make it 11 seconds").
  * The source clip has its audio removed, so "Mute my sound" has nothing left to mute and no
    mute field is written (there is no `clip_sound` strategy field in this tree; the worker
    mutes through the brief-driven sound toggle lane, which needs a bound brief).
"""

from __future__ import annotations

# ruff: noqa: E402 -- env + sys.path must be set before app imports
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for _name, _value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(_name, _value)

KIT = Path("/Users/yasinberkyesilyurt/Downloads/kria-qendresa-test")
CHECK = KIT / "tools/check_result.sh"

PROMPT = (
    'Just put this text in the middle for the whole video: "The prompt I run at the end of '
    "every Claude session\". Small white text like TikTok's, no captions and nothing else. "
    "Make it 11 seconds. Mute my sound, I'll add a song on TikTok."
)
TEXT = "The prompt I run at the end of every Claude session"
CLIP_S = 11.0
MEDIA_ID = "clip-1"

BASE_STRATEGY = {
    "edit_format": "montage",
    "audio_strategy": "original_audio",
    "direction": "fast_montage",
    "target_duration_s": 11,
}
CASES: dict[str, dict] = {
    "before": {
        "flag": False,
        "strategy": {
            **BASE_STRATEGY,
            "pinned_texts": [{"text": TEXT, "corner": "top_center"}],
            "opening_title": TEXT,
        },
    },
    "before_pin_only": {
        "flag": False,
        "strategy": {
            **BASE_STRATEGY,
            "pinned_texts": [{"text": TEXT, "corner": "top_center"}],
        },
    },
    "after": {
        "flag": True,
        "strategy": {
            **BASE_STRATEGY,
            "pinned_texts": [
                {"text": TEXT, "corner": "middle_center", "look": "tiktok"}
            ],
        },
    },
    "after_with_title": {
        "flag": True,
        "strategy": {
            **BASE_STRATEGY,
            "pinned_texts": [
                {"text": TEXT, "corner": "middle_center", "look": "tiktok"}
            ],
            "opening_title": TEXT,
        },
    },
    "after_face": {
        "flag": True,
        "needs_faces": True,
        "strategy": {
            **BASE_STRATEGY,
            "pinned_texts": [
                {"text": TEXT, "corner": "middle_center", "look": "tiktok"}
            ],
        },
    },
}
PLAN_ROW_FIELDS = (
    "id",
    "text",
    "x_frac",
    "y_frac",
    "size_px",
    "font_family",
    "color",
    "shadow_enabled",
    "shadow_color",
    "shadow_blur",
    "shadow_offset_x",
    "shadow_offset_y",
    "stroke_width",
    "start_s",
    "end_s",
    "source_params",
)


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args], check=True
    )


def make_source(clip: Path, out: Path) -> Path:
    """First 11.0 s of `clip`, 1080x1920, H.264, audio removed."""
    target = out / "source/clip.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "fps=30,format=yuv420p"
    )
    _ffmpeg(
        *("-i", str(clip), "-t", str(CLIP_S), "-an", "-vf", vf),
        *(
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-movflags",
            "+faststart",
        ),
        str(target),
    )
    return target


def _binding(path: Path):
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.services.phone_sources import PhoneSourceBinding

    return PhoneSourceBinding(
        media_id=MEDIA_ID,
        proxy_path=f"users/owner/creation/analysis-proxy-{MEDIA_ID}.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            byte_count=path.stat().st_size,
            duration_s=CLIP_S,
            width=1080,
            height=1920,
            has_audio=False,
        ),
    )


def _face_sampler(boxes: list[dict]):
    """A `build_phone_face_sampler` stand-in returning externally measured face regions."""
    from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion

    regions = [
        ProtectedRegion(
            start_s=float(row["start_s"]),
            end_s=float(row["end_s"]),
            box=NormalizedBox(*(float(v) for v in row["box"])),
            kind=row.get("kind", "face"),
        )
        for row in boxes
    ]

    def build(plan, bindings, *, workdir, download, sample=None, observe_frames=None):
        def sampler(anchors, timeout_s):
            receipt = {
                "attempted": len(anchors),
                "decoded": len(anchors),
                "detected": len(regions),
                "timed_out": False,
                "partial": False,
                "sources_sampled": 1,
                "injected_external_boxes": True,
            }
            return list(regions), receipt

        return sampler

    return build


def _set_flag(value: bool) -> None:
    from app.config import settings

    # The field may not exist before the feature lands; plain setattr on a pydantic
    # settings object rejects unknown names, so bypass validation for that case.
    try:
        settings.clip_text_tiktok_look_enabled = value
    except (ValueError, AttributeError):
        object.__setattr__(settings, "clip_text_tiktok_look_enabled", value)


def compile_case(name: str, spec: dict, source: Path, face_boxes: list[dict] | None):
    """The worker's chain for a one-clip phone montage. Returns (recipe, styled_plan_dict)."""
    import app.pipeline.phone_guided_text_placement as placement
    import app.storage as storage
    from app.config import settings
    from app.pipeline.guided_story import (
        GuidedStoryExecutionPlan,
        compile_execution_plan,
    )
    from app.pipeline.phone_plan_repair import compile_phone_guided_repaired
    from app.pipeline.unified_montage import (
        UnifiedClip,
        brief_view,
        plan_unified_montage,
        skia_font_covers,
    )
    from app.services.phone_text_styling import (
        phone_text_policy_scope,
        requested_style,
        style_guided_generation,
    )

    settings.phone_render_verified_features = [
        "basicComposition",
        "positionedText",
        "audioMix",
        "local1080Export",
        "crossfade",
        "animatedText",
        "authoredText",
    ]
    _set_flag(bool(spec["flag"]))
    binding = _binding(source)
    strategy = spec["strategy"]
    clip = UnifiedClip(
        media_id=MEDIA_ID,
        proxy_path=binding.proxy_path,
        generation=binding.generation,
        duration_s=CLIP_S,
        width=1080,
        height=1920,
    )
    mp = plan_unified_montage(
        [clip],
        brief_view(None),
        strategy=strategy,
        clip_intents_enabled=settings.clip_intents_enabled,
        font_covers=skia_font_covers,
        # "Make it 11 seconds." -- the worker trusts a length only with a bound brief;
        # here the creator length is supplied as proven.
        creator_duration_proven=True,
    )
    raw_plan = compile_execution_plan(mp.guided_edit(), track=None)
    plan = GuidedStoryExecutionPlan.model_validate(raw_plan)

    def local_download(object_path, local_path, *, timeout_s=None):
        shutil.copy2(source, local_path)

    import app.services.phone_text_styling as styling

    saved = (
        storage.download_to_file,
        placement.build_phone_face_sampler,
        styling.select_fonts,
    )
    storage.download_to_file = local_download
    styling.select_fonts = lambda *a, **k: {}
    if spec.get("needs_faces"):
        placement.build_phone_face_sampler = _face_sampler(face_boxes or [])
    try:
        policy = {
            "version": 1,
            "preferences": {"all_text": {}},
            "requested": requested_style(strategy, PROMPT),
        }
        with phone_text_policy_scope(policy):
            styled = style_guided_generation(plan, (binding,), job_id=str(uuid.uuid4()))
    finally:
        (
            storage.download_to_file,
            placement.build_phone_face_sampler,
            styling.select_fonts,
        ) = saved
    plan = GuidedStoryExecutionPlan.model_validate(styled)
    compiled = compile_phone_guided_repaired(plan, (binding,), contract=None)
    return compiled.recipe, styled, binding


def plan_rows(styled: dict) -> list[dict]:
    rows = []
    for lane in (
        "text_elements",
        "context_label_text_elements",
        "narration_label_text_elements",
    ):
        for row in styled.get(lane) or []:
            item = {key: row.get(key) for key in PLAN_ROW_FIELDS if key in row}
            item["lane"] = lane
            rows.append(item)
    return rows


def cmd_prepare(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    face_boxes = (
        json.loads(Path(args.face_boxes).read_text()) if args.face_boxes else None
    )
    wanted = [c for c in (args.cases.split(",") if args.cases else CASES) if c]
    unknown = [c for c in wanted if c not in CASES]
    if unknown:
        print(f"unknown case(s): {unknown}; known: {list(CASES)}")
        return 2
    source = make_source(Path(args.clip), out)
    ok: list[str] = []
    for name in wanted:
        spec = CASES[name]
        if spec.get("needs_faces") and face_boxes is None:
            print(f"[{name}] skipped: needs --face-boxes")
            continue
        directory = out / "cases" / name
        shutil.rmtree(directory, ignore_errors=True)
        try:
            recipe, styled, binding = compile_case(name, spec, source, face_boxes)
        except Exception as exc:  # noqa: BLE001 - report each case, keep going
            print(f"[{name}] FAILED to compile ({type(exc).__name__}): {exc}")
            if spec["flag"]:
                print(
                    f"[{name}] expected until the clip-text TikTok-look feature lands "
                    "(unknown corner/look or missing flag)."
                )
            continue
        rows = plan_rows(styled)
        pins = [r for r in rows if "pinned" in str(r.get("id"))]
        if "pinned_texts" in spec["strategy"] and not pins:
            print(
                f"[{name}] FAILED: the pinned line was dropped before it reached the plan "
                "(the planner does not know this corner/look yet -- feature not landed?)."
            )
            continue
        directory.mkdir(parents=True)
        (directory / "recipe.json").write_text(
            json.dumps(recipe.model_dump(mode="json"), indent=2, sort_keys=True)
        )
        assets = {asset.id: str(source) for asset in recipe.asset_manifest.assets}
        (directory / "assets.json").write_text(json.dumps(assets, indent=2))
        (directory / "plan.json").write_text(
            json.dumps(
                {
                    "case": name,
                    "flag": spec["flag"],
                    "strategy": spec["strategy"],
                    "rows": rows,
                },
                indent=2,
            )
        )
        print(f"[{name}] ok: duration={recipe.duration:.3f}s text rows={len(rows)}")
        for row in rows:
            print(
                f"    {row['id']}: {row.get('text')!r} x={row.get('x_frac')} y={row.get('y_frac')} "
                f"size={row.get('size_px')} font={row.get('font_family')} "
                f"color={row.get('color')} {row.get('start_s')}-{row.get('end_s')}s"
            )
        ok.append(name)
    print(
        f"\nPrepared {ok} under {out / 'cases'}. Render on the simulator, from src/apps/ios:"
    )
    print(
        f"  TEST_RUNNER_KRIA_CLIP_TEXT_LOOK_E2E_DIR={out} xcodebuild test "
        "-project Kria.xcodeproj -scheme Kria "
        "-destination 'platform=iOS Simulator,id=$KRIA_SIMULATOR_ID' "
        "-only-testing:KriaTests/DeviceClipTextLookRenderE2ETests "
        "ONLY_ACTIVE_ARCH=YES ARCHS=arm64 COMPILER_INDEX_STORE_ENABLE=NO "
        "-collect-test-diagnostics never"
    )
    print(f"Then: python3 {Path(__file__).name} score {out}")
    return 0 if ok else 1


def cmd_score(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    results: dict[str, dict] = {}
    for video in sorted((root / "cases").glob("*/phone.mp4")):
        name = video.parent.name
        print(f"== scoring {name}", flush=True)
        proc = subprocess.run(
            [
                str(CHECK),
                str(video),
                "Q3",
                "--ignore-tail-s",
                "1.6",
                "--source-s",
                "11.0",
            ],
            capture_output=True,
            text=True,
        )
        report = video.with_suffix("").with_suffix(".check") / "report.json"
        if not report.exists():
            report = Path(str(video.with_suffix("")) + ".check") / "report.json"
        if proc.returncode != 0 or not report.exists():
            print(f"   check failed (exit {proc.returncode}): {proc.stderr[-400:]}")
            results[name] = {"error": proc.stderr[-400:] or "no report"}
            continue
        results[name] = json.loads(report.read_text())
    (root / "score.json").write_text(json.dumps(results, indent=2))
    _print_table(results)
    return 0


def _rules(report: dict) -> dict[str, str]:
    """Flatten whatever shape the report's rules take into {rule: status/value}."""
    rules = report.get("rules", report.get("checks", report))
    out: dict[str, str] = {}
    if isinstance(rules, list):
        for row in rules:
            if isinstance(row, dict):
                key = str(row.get("rule") or row.get("id") or row.get("name"))
                val = row.get(
                    "status", row.get("pass", row.get("ok", row.get("verdict")))
                )
                out[key] = str(val)
    elif isinstance(rules, dict):
        for key, row in rules.items():
            if isinstance(row, dict):
                val = row.get(
                    "status", row.get("pass", row.get("ok", row.get("verdict", row)))
                )
            else:
                val = row
            out[str(key)] = str(val)
    return out


def _print_table(results: dict[str, dict]) -> None:
    flat = {name: _rules(rep) for name, rep in results.items() if "error" not in rep}
    keys = ["text_place", "one_text", "text_short"]
    keys += sorted({k for r in flat.values() for k in r} - set(keys))
    width = max([len(k) for k in keys] + [4])
    names = list(flat)
    print("\n" + "rule".ljust(width) + "  " + "  ".join(n.ljust(18) for n in names))
    for key in keys:
        print(
            key.ljust(width)
            + "  "
            + "  ".join(flat[n].get(key, "-")[:18].ljust(18) for n in names)
        )
    for name, rep in results.items():
        if "error" in rep:
            print(f"{name}: ERROR {rep['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--clip", required=True)
    prep.add_argument("--out", required=True)
    prep.add_argument("--face-boxes")
    prep.add_argument("--cases", help="comma-separated subset, default all")
    score = sub.add_parser("score")
    score.add_argument("dir")
    args = parser.parse_args()
    tempfile.tempdir = None
    return cmd_prepare(args) if args.cmd == "prepare" else cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
