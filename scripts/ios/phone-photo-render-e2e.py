#!/usr/bin/env python3
"""Prepare the KRI-121 on-device Visuals render check.

Compiles two phone recipes with the server's real `compile_phone_guided_plan`
(footage crossfading into a Visuals photo, the same photo as a supporting card,
a Visuals video and a transparent cutout; and a project made only of Visuals),
writes them as the device receives them, and
prints the simulator test command that renders it through the production
device resolver and exporter (`DevicePhotoRenderE2ETests`).

Run with the API's Python environment (it imports `app`):
    src/apps/api/.venv/bin/python scripts/ios/phone-photo-render-e2e.py [OUT_DIR]
Needs ffmpeg on PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for name, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE, TextElement
from app.config import settings
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderFingerprint
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.schemas.edit_proposal import GUIDED_TITLE_HOLD_S, NarrationTrack
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import (
    PhoneSourceBinding,
    PhoneVisualBinding,
)

VERIFIED = [
    "basicComposition",
    "positionedText",
    "audioMix",
    "local1080Export",
    "crossfade",
]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def _caption_region(
    layer, *, canvas_width: int, canvas_height: int, pad: float = 56.0
) -> list[int]:
    """Generous pixel bbox ([x0, y0, x1, y1], top-left origin) around every
    run in a compiled caption `layer`. Ported from `_caption_region` in
    `scripts/ios/phone-montage-render-e2e.py` -- see that module for the full
    rationale (the per-run geometry is already fully-anchored absolute canvas
    pixels; only each run's width needs a generous estimate)."""
    lefts: list[float] = []
    rights: list[float] = []
    tops: list[float] = []
    bottoms: list[float] = []
    for run in layer.runs:
        est_width = len(run.text) * run.font_size * 0.66
        lefts.append(run.x)
        rights.append(run.x + est_width)
        tops.append(run.baseline_y - run.font_size * 1.1)
        bottoms.append(run.baseline_y + run.font_size * 0.4)
    x0 = max(0.0, min(lefts) - pad)
    x1 = min(float(canvas_width), max(rights) + pad)
    y0 = max(0.0, min(tops) - pad)
    y1 = min(float(canvas_height), max(bottoms) + pad)
    return [round(x0), round(y0), round(x1), round(y1)]


def _moment(moment_id: str, start: float, end: float, **fields: object) -> dict:
    return {
        "beat_id": "beat",
        "topic": "Scene",
        "layout": "fullscreen",
        "moment_id": moment_id,
        "output_start_s": start,
        "output_end_s": end,
        "duration_s": round(end - start, 3),
        "transition_after": "cut",
        **fields,
    }


def _plan(
    moments: list[dict],
    media_ids: list[str],
    *,
    source_audio: bool,
    narration: NarrationTrack | None = None,
    text_elements: list[dict] | None = None,
    compiler_version: int = 6,
) -> GuidedStoryExecutionPlan:
    duration = moments[-1]["output_end_s"]
    return GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": compiler_version,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "guided_story",
            "goal": "Show the scene",
            "pace": "balanced",
            "approved_duration_s": duration,
            "resolved_duration_s": duration,
            "selected_media_ids": media_ids,
            "editor_revision_number": 1,
            **(
                {
                    "montage_audio": {"preserve_source_audio": True},
                    "editor_audio_level": 1.0,
                }
                if source_audio
                else {}
            ),
            **({"narration": narration.model_dump(mode="json")} if narration is not None else {}),
            "story_timeline": moments,
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": duration,
                    "resolved_duration_s": duration,
                    "start_s": 0,
                    "end_s": duration,
                }
            ],
            "text_elements": text_elements or [],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
        }
    )


def _status(
    plan: GuidedStoryExecutionPlan,
    sources: tuple[PhoneSourceBinding, ...],
    visuals: tuple[PhoneVisualBinding, ...],
    *,
    narration: PhoneNarrationBed | None = None,
    allow_editor_media: bool = False,
) -> tuple[dict, EditRecipeV2]:
    kwargs = {"narration": narration} if narration is not None else {}
    recipe = compile_phone_guided_plan(
        plan, sources, visuals, allow_editor_media=allow_editor_media, **kwargs
    )
    validate_phone_pilot_recipe(recipe, allow_editor_media=allow_editor_media)
    request = make_device_request(
        job_id=uuid.uuid4(), variant_id="guided_story", revision=1, recipe=recipe
    )
    status = DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(mode="json")
    return status, recipe


def main() -> None:
    out = Path(
        sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="kria-photo-e2e-")
    )
    out.mkdir(parents=True, exist_ok=True)
    video, photo = out / "video.mp4", out / "photo.jpg"
    pool_video, editor_video, cutout = (
        out / "pool-video.mp4",
        out / "editor-video.mp4",
        out / "cutout.png",
    )
    # Blue footage and a yellow Visuals video, both with a tone. A landscape
    # photo whose halves are red and green, so cover-cropping onto the portrait
    # canvas keeps both halves and the card shows the whole photo. A cutout
    # whose transparent center hides white under magenta edges.
    for path, color, size in (
        (video, "blue", "1920x1080"),
        (pool_video, "yellow", "1080x1920"),
    ):
        _ffmpeg(
            *("-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d=10"),
            *("-f", "lavfi", "-i", "sine=frequency=440:duration=10"),
            *(
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ),
        )
    # Four one-second color bars make the editor-overlay video's source trim
    # observable: the [2s, 4s] trim below must render cyan, never the first
    # red/yellow seconds or a frozen terminal frame.
    _ffmpeg(
        *(
            "-f", "lavfi", "-i",
            "color=c=red:s=1080x1920:r=30:d=1",
            "-f", "lavfi", "-i",
            "color=c=yellow:s=1080x1920:r=30:d=1",
            "-f", "lavfi", "-i",
            "color=c=cyan:s=1080x1920:r=30:d=2",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(editor_video),
        )
    )
    _ffmpeg(
        *("-f", "lavfi", "-i", "color=c=red:s=2016x3024"),
        *("-f", "lavfi", "-i", "color=c=0x00ff00:s=2016x3024"),
        *("-filter_complex", "hstack", "-frames:v", "1", "-q:v", "2", str(photo)),
    )
    from PIL import Image

    art = Image.new("RGBA", (1080, 1920), (255, 0, 255, 255))
    art.paste((255, 255, 255, 0), (270, 480, 810, 1440))
    art.save(cutout)

    def visual(path: Path, **probe: object) -> PhoneVisualBinding:
        sha, size = _fingerprint(path)
        media_id = str(uuid.uuid4())
        return PhoneVisualBinding(
            media_id=media_id,
            gcs_path=f"users/owner/plan/item/pool/{media_id}{path.suffix}",
            generation="202",
            sha256=sha,
            byte_count=size,
            **probe,
        )

    video_sha, video_bytes = _fingerprint(video)
    source = PhoneSourceBinding(
        media_id="analysis-proxy-e2e.mp4",
        proxy_path="users/owner/creation/analysis-proxy-e2e.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=video_sha,
            byte_count=video_bytes,
            duration_s=10,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )
    still, art_still = visual(photo), visual(cutout)
    clip = visual(pool_video, kind="video", duration_s=10, width=1080, height=1920)
    editor_clip = visual(editor_video, kind="video", duration_s=4, width=1080, height=1920)

    def pooled(
        binding: PhoneVisualBinding, moment_id: str, start: float, end: float, **fields
    ):
        return _moment(
            moment_id,
            start,
            end,
            media_id=binding.media_id,
            lane="asset",
            kind=binding.kind,
            gcs_path=binding.gcs_path,
            generation=binding.generation,
            source_start_s=1 if binding.kind == "video" else 0,
            source_end_s=round(end - start + (1 if binding.kind == "video" else 0), 3),
            **fields,
        )

    settings.phone_render_verified_features = [
        *VERIFIED,
        "stillImages",
        "visualVideos",
        "visualBlocks",
        "alphaOverlay",
        # KRI-132: the narrated_story case's hard-replace voiceover track and
        # its (fade-in) title text. "authoredText" is required too -- the
        # title's "Fraunces" face is a variable font, so its compiled run
        # carries `font_variations`, which EditRecipeV2's own validator maps
        # to that capability regardless of the effect used.
        "narrationAudio",
        "animatedText",
        "authoredText",
    ]
    footage = _moment(
        "video",
        0,
        3,
        media_id=source.media_id,
        lane="clip",
        kind="video",
        gcs_path=source.proxy_path,
        generation=source.generation,
        source_start_s=2,
        source_end_s=5,
        transition_after="crossfade",
        transition_duration_s=0.3,
    )
    mixed = _plan(
        [
            footage,
            pooled(still, "photo", 2.7, 4.7),
            pooled(still, "card", 4.7, 6.7, layout="supporting_card"),
            pooled(clip, "pool-video", 6.7, 8.7),
            pooled(art_still, "cutout", 8.7, 10.7),
        ],
        [source.media_id, still.media_id, clip.media_id, art_still.media_id],
        source_audio=True,
    )
    # A project made only of Visuals renders on the iPhone with no originals.
    visuals_only = _plan(
        [pooled(still, "photo", 0, 2), pooled(clip, "pool-video", 2, 4)],
        [still.media_id, clip.media_id],
        source_audio=False,
    )
    mixed_status, _ = _status(mixed, (source,), (still, clip, art_still))
    (out / "status.json").write_text(json.dumps(mixed_status, indent=2))
    visuals_only_status, _ = _status(visuals_only, (), (still, clip))
    (out / "status-visuals-only.json").write_text(json.dumps(visuals_only_status, indent=2))

    # Opt-in native editor-media qualification: both image transforms use the
    # same pooled photo, and a z=2 video overlays the contained image between
    # 2s and 3s. The video trim is exactly [2s, 4s], whose cyan source bars
    # let the device test prove that it honors the server-approved trim.
    editor_media_plan = _plan(
        [
            _moment(
                "editor-base", 0, 6, media_id=source.media_id, lane="clip", kind="video",
                gcs_path=source.proxy_path, generation=source.generation,
                source_start_s=2, source_end_s=8,
            )
        ],
        [source.media_id],
        source_audio=True,
    )
    editor_media_plan.editor_visual_blocks = [
        {
            "id": "contain-photo", "kind": "media", "asset_id": still.media_id,
            "src_gcs_path": still.gcs_path, "media_kind": "image",
            "start_s": 1, "end_s": 3, "display_mode": "fullscreen", "z": 1,
            "transform": {"fit_mode": "contain", "focal_x": 0.5, "focal_y": 0.5, "zoom": 1},
        },
        {
            "id": "trimmed-video", "kind": "media", "asset_id": editor_clip.media_id,
            "src_gcs_path": editor_clip.gcs_path, "media_kind": "video",
            "source_duration_s": 4, "trim_start_s": 2, "trim_end_s": 4,
            "start_s": 2, "end_s": 4, "display_mode": "overlay", "x_frac": 0.5,
            "y_frac": 0.5, "scale": 0.4, "z": 2,
            "transform": {"fit_mode": "cover", "focal_x": 0.5, "focal_y": 0.5, "zoom": 1},
        },
        {
            "id": "cover-photo", "kind": "media", "asset_id": still.media_id,
            "src_gcs_path": still.gcs_path, "media_kind": "image",
            "start_s": 4, "end_s": 5, "display_mode": "fullscreen", "z": 1,
            "transform": {"fit_mode": "cover", "focal_x": 0.5, "focal_y": 0.5, "zoom": 1},
        },
    ]
    editor_media_status, editor_media_recipe = _status(
        editor_media_plan, (source,), (still, editor_clip), allow_editor_media=True
    )
    (out / "status-editor-media.json").write_text(json.dumps(editor_media_status, indent=2))

    # --- KRI-132: narrated_story -- a voiceover-timed guided story --------
    # 5 clips + 5 Visuals-pool photos tile the whole 48s narration duration
    # contiguously (clips at a fixed 5s each; photos absorb the remainder --
    # the cloud's own water-fill, kept simple here as a flat split), a
    # hard-replace narration audio track (compile_phone_guided_plan's new
    # `narration=` kwarg forces `audio.original_volume == 0`, unlike the
    # montage/narrated compilers' mixed footage bed), one opening title, and
    # one static caption per spoken word-group at y=0.82 (shaped exactly like
    # `_narration_caption_elements` in app/pipeline/guided_story.py, but
    # pre-grouped by hand here since this script drives the plan directly
    # rather than through the proposal compiler's point-timestamp grouping).
    NARRATED_CLIP_COLORS = ["red", "blue", "lime", "yellow", "cyan"]
    NARRATED_PHOTO_COLORS = ["orange", "purple", "white", "teal", "pink"]
    NARRATED_CLIP_DURATION_S = 5.0
    NARRATED_TOTAL_DURATION_S = 48.0
    narrated_photo_duration_s = round(
        (NARRATED_TOTAL_DURATION_S - NARRATED_CLIP_DURATION_S * len(NARRATED_CLIP_COLORS))
        / len(NARRATED_PHOTO_COLORS),
        3,
    )

    narrated_clip_paths = [out / f"narrated-clip-{i}.mp4" for i in range(5)]
    for path, color in zip(narrated_clip_paths, NARRATED_CLIP_COLORS):
        _ffmpeg(
            *(
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=1080x1920:r=30:d={NARRATED_CLIP_DURATION_S}",
            ),
            *("-f", "lavfi", "-i", f"sine=frequency=440:duration={NARRATED_CLIP_DURATION_S}"),
            *("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)),
        )
    narrated_photo_paths = [out / f"narrated-photo-{i}.png" for i in range(5)]
    for path, color in zip(narrated_photo_paths, NARRATED_PHOTO_COLORS):
        _ffmpeg(
            *("-f", "lavfi", "-i", f"color=c={color}:s=1080x1920"),
            *("-frames:v", "1", str(path)),
        )
    voiceover_path = out / "voiceover.m4a"
    _ffmpeg(
        *("-f", "lavfi", "-i", f"sine=frequency=220:duration={NARRATED_TOTAL_DURATION_S}"),
        *("-c:a", "aac", str(voiceover_path)),
    )

    narrated_clip_bindings = []
    for index, path in enumerate(narrated_clip_paths):
        sha, size = _fingerprint(path)
        narrated_clip_bindings.append(
            PhoneSourceBinding(
                media_id=f"narrated-clip-{index}",
                proxy_path=f"users/owner/creation/analysis-proxy-narrated-{index}.mp4",
                generation="101",
                original=OriginalMediaDescriptor(
                    sha256=sha,
                    byte_count=size,
                    duration_s=NARRATED_CLIP_DURATION_S,
                    width=1080,
                    height=1920,
                    has_audio=True,
                ),
            )
        )
    narrated_photo_bindings = [visual(path) for path in narrated_photo_paths]

    def footage_moment(
        binding: PhoneSourceBinding, moment_id: str, start: float, end: float
    ) -> dict:
        return _moment(
            moment_id,
            start,
            end,
            media_id=binding.media_id,
            lane="clip",
            kind="video",
            gcs_path=binding.proxy_path,
            generation=binding.generation,
            source_start_s=0,
            source_end_s=round(end - start, 3),
        )

    narrated_moments = []
    narrated_media_ids = []
    narrated_cursor = 0.0
    for index in range(5):
        clip_start, clip_end = narrated_cursor, round(narrated_cursor + NARRATED_CLIP_DURATION_S, 3)
        narrated_moments.append(
            footage_moment(narrated_clip_bindings[index], f"clip{index}", clip_start, clip_end)
        )
        narrated_media_ids.append(narrated_clip_bindings[index].media_id)
        photo_start, photo_end = clip_end, round(clip_end + narrated_photo_duration_s, 3)
        narrated_moments.append(
            pooled(narrated_photo_bindings[index], f"photo{index}", photo_start, photo_end)
        )
        narrated_media_ids.append(narrated_photo_bindings[index].media_id)
        narrated_cursor = photo_end

    title_element = TextElement(
        id="guided-title",
        text="Since 1882",
        start_s=0.0,
        end_s=GUIDED_TITLE_HOLD_S,
        role="generative_intro",
        position="custom",
        x_frac=0.5,
        y_frac=0.16,
        font_family="Fraunces",
        size_px=104,
        color="#FFF8F0",
        highlight_color="#D9FF70",
        stroke_width=0,
        shadow_enabled=True,
        shadow_style="standard",
        effect="fade-in",
        alignment="center",
        letter_spacing=-0.025,
        line_spacing=1.0,
        max_width_frac=0.8,
    ).model_dump(mode="json", exclude_none=True)

    # ~24 words in 6 spoken groups of 4, each group's words contiguous and
    # every group separated from its neighbours by a >=1.0s silent gap.
    NARRATED_CAPTION_GROUPS = [
        ["Since", "eighteen", "eighty", "two"],
        ["this", "harbor", "has", "thrived"],
        ["ships", "arrived", "every", "morning"],
        ["families", "built", "their", "lives"],
        ["the", "lighthouse", "still", "stands"],
        ["today", "the", "story", "continues"],
    ]
    NARRATED_CAPTION_GROUP_STARTS = [1.0, 9.0, 17.0, 25.0, 33.0, 41.0]
    NARRATED_WORD_DURATION_S = 0.6

    def word_group(words: list[str], group_start: float) -> list[dict]:
        result = []
        word_cursor = group_start
        for word in words:
            end = round(word_cursor + NARRATED_WORD_DURATION_S, 3)
            result.append(
                {"text": word, "start_s": round(word_cursor, 3), "end_s": end, "confidence": 1.0}
            )
            word_cursor = end
        return result

    narrated_word_groups = [
        word_group(words, start)
        for words, start in zip(NARRATED_CAPTION_GROUPS, NARRATED_CAPTION_GROUP_STARTS)
    ]
    narrated_words = [word for group in narrated_word_groups for word in group]

    # Same construction `_narration_caption_elements` uses per group (see
    # app/pipeline/guided_story.py:1501-1555): id `narration-caption-N`, role
    # generative_sequence, position custom at y_frac 0.82, Inter-Bold 58px,
    # white with a 6px stroke, effect static, word_timings + the shared
    # CAPTION_CUE_SOURCE marker.
    narrated_caption_elements = []
    for index, group in enumerate(narrated_word_groups):
        narrated_caption_elements.append(
            TextElement(
                id=f"narration-caption-{index + 1}",
                text=" ".join(word["text"] for word in group),
                start_s=group[0]["start_s"],
                end_s=group[-1]["end_s"],
                role="generative_sequence",
                position="custom",
                x_frac=0.5,
                y_frac=0.82,
                font_family="Inter-Bold",
                size_px=58,
                color="#FFFFFF",
                highlight_color="#FFFFFF",
                stroke_width=6,
                shadow_enabled=True,
                effect="static",
                alignment="center",
                max_width_frac=0.84,
                word_timings=group,
                source_params={
                    "source": CAPTION_CUE_SOURCE,
                    "key": str(index),
                    "identity": f"pinned-narration-caption-{index}",
                },
            ).model_dump(mode="json", exclude_none=True)
        )

    narrated_narration_track = NarrationTrack(
        gcs_path="voiceover-uploads/e2e/voice.m4a",
        generation="e2e-gen-1",
        duration_s=NARRATED_TOTAL_DURATION_S,
        words=narrated_words,
    )
    voice_sha, voice_bytes = _fingerprint(voiceover_path)
    narrated_plan_item_id = "e2e-narrated-story-item"
    narrated_narration_bed = PhoneNarrationBed(
        plan_item_id=narrated_plan_item_id,
        generation="e2e-gen-1",
        fingerprint=RenderFingerprint(sha256=voice_sha, byte_count=voice_bytes),
        duration_s=NARRATED_TOTAL_DURATION_S,
    )
    narrated_story_plan = _plan(
        narrated_moments,
        narrated_media_ids,
        source_audio=False,
        narration=narrated_narration_track,
        text_elements=[title_element, *narrated_caption_elements],
        compiler_version=7,
    )
    narrated_story_status, narrated_story_recipe = _status(
        narrated_story_plan,
        tuple(narrated_clip_bindings),
        tuple(narrated_photo_bindings),
        narration=narrated_narration_bed,
    )
    (out / "status-narrated-story.json").write_text(json.dumps(narrated_story_status, indent=2))

    # Caption regions come from the COMPILED recipe's own text-layer geometry
    # (see `_caption_region`): text_layers[0] is the title (role
    # generative_intro sorts before generative_sequence, stably); the six
    # caption groups follow in their chronological input order.
    narrated_caption_regions = [
        _caption_region(layer, canvas_width=1080, canvas_height=1920)
        for layer in narrated_story_recipe.text_layers[1:]
    ]
    assert len(narrated_caption_regions) == len(narrated_word_groups)

    narrated_caption_samples = []
    for index, group in enumerate(narrated_word_groups):
        region = narrated_caption_regions[index]
        on_t = round((group[0]["start_s"] + group[-1]["end_s"]) / 2, 3)
        if index + 1 < len(narrated_word_groups):
            next_start = narrated_word_groups[index + 1][0]["start_s"]
            gap_t = round((group[-1]["end_s"] + next_start) / 2, 3)
        else:
            gap_t = round(group[-1]["end_s"] + 2.0, 3)
        narrated_caption_samples.append(
            {"name": f"cue{index}_on", "t": on_t, "region": region, "expect_text": True}
        )
        narrated_caption_samples.append(
            {"name": f"cue{index}_gap", "t": gap_t, "region": region, "expect_text": False}
        )

    narrated_story_entry = {
        "status_file": "status-narrated-story.json",
        "duration_s": narrated_story_recipe.duration,
        "required_capabilities": sorted(narrated_story_recipe.required_capabilities),
        "drop_capability": "narrationAudio",
        "clips": [
            {"media_id": binding.media_id, "file": path.name}
            for binding, path in zip(narrated_clip_bindings, narrated_clip_paths)
        ],
        "voiceover_asset_id": f"voiceover-{narrated_plan_item_id}",
        "voiceover_file": voiceover_path.name,
        "expects_narration_audio": True,
        # Hard replace (audio.original_volume == 0): the clips' own embedded
        # tone never survives into the mix, unlike montage's voiceover case.
        "expects_source_audio": False,
        "samples": [
            {"name": "clip0", "t": 2.5, "x": 540, "y": 960, "rgb": [255, 0, 0]},
            {"name": "clip2", "t": 21.7, "x": 540, "y": 960, "rgb": [0, 255, 0]},
            {"name": "photo0", "t": 7.3, "x": 540, "y": 960, "rgb": [255, 165, 0]},
            {"name": "photo3", "t": 36.1, "x": 540, "y": 960, "rgb": [0, 128, 128]},
        ],
        "caption_samples": narrated_caption_samples,
    }

    (out / "e2e.json").write_text(
        json.dumps(
            {
                "video_media_id": source.media_id,
                "verified_features": settings.phone_render_verified_features,
                "visual_files": {
                    still.render_asset().id: photo.name,
                    clip.render_asset().id: pool_video.name,
                    art_still.render_asset().id: cutout.name,
                    editor_clip.render_asset().id: editor_video.name,
                    **{
                        binding.render_asset().id: path.name
                        for binding, path in zip(narrated_photo_bindings, narrated_photo_paths)
                    },
                },
                "editor_media": {
                    "status_file": "status-editor-media.json",
                    "duration_s": editor_media_recipe.duration,
                    "required_capabilities": sorted(editor_media_recipe.required_capabilities),
                    "drop_capability": "visualBlocks",
                    "samples": [
                        {"name": "base-before", "t": 0.5, "x": 540, "y": 960, "rgb": [0, 0, 255]},
                        {"name": "contain-left", "t": 1.5, "x": 180, "y": 960, "rgb": [255, 0, 0]},
                        {"name": "contain-right", "t": 1.5, "x": 900, "y": 960, "rgb": [0, 255, 0]},
                        {"name": "overlap-video", "t": 2.5, "x": 540, "y": 960, "rgb": [0, 255, 255]},
                        {"name": "overlap-photo", "t": 2.5, "x": 180, "y": 960, "rgb": [255, 0, 0]},
                        {"name": "cover-left", "t": 4.5, "x": 270, "y": 960, "rgb": [255, 0, 0]},
                        {"name": "cover-right", "t": 4.5, "x": 810, "y": 960, "rgb": [0, 255, 0]},
                        {"name": "base-after", "t": 5.5, "x": 540, "y": 960, "rgb": [0, 0, 255]},
                    ],
                },
                "narrated_story": narrated_story_entry,
            },
            indent=2,
        )
    )
    print(f"Wrote {out}. Render it on a simulator from src/apps/ios:")
    print(
        f"  TEST_RUNNER_KRIA_E2E_DIR={out} xcodebuild -project Kria.xcodeproj -scheme Kria "
        "-skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO "
        '-destination "platform=iOS Simulator,name=<iPhone>" '
        "-only-testing:KriaTests/DevicePhotoRenderE2ETests test"
    )
    print(f"Frames and the rendered MP4s land in {out / 'frames'}.")
    print(
        f"narrated_story: duration={narrated_story_recipe.duration:.3f}s "
        f"required_capabilities={sorted(narrated_story_recipe.required_capabilities)}"
    )
    print(f"narrated_story caption regions: {narrated_caption_regions}")
    print(f"verified_features (union): {sorted(settings.phone_render_verified_features)}")


if __name__ == "__main__":
    main()
