#!/usr/bin/env python3
"""Extend the native render kit with generation-pinned KRI-648 text journeys.

Uses the production compiler and existing asset/export harness. Synthetic face
regions verify the layout contract; they do not constitute live-face or platform
clearance evidence. No provider calls or account writes occur.
"""

from __future__ import annotations

# ruff: noqa: E402 -- anchor app imports to this task worktree before loading them

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))
os.environ["PYTHONPATH"] = str(ROOT / "src/apps/api") + (
    os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)

from app.agents._schemas.text_element import TextElement
from app.pipeline.phone_narrated_plan import (
    NarratedPhoneStep,
    compile_phone_narrated_plan,
    narrated_title_text_elements,
    with_pinned_text_layers,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_text_style import animation_box, measure_box, resolve_phone_text_style
from app.pipeline.phone_voiceover_montage_plan import compile_phone_voiceover_montage_plan
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.phone_text_styling import generation_style_fields, phone_text_policy_scope
from app.schemas.phone_text_style import CURATED_PHONE_FONTS
from app.schemas.edit_proposal import PinnedText


def main():
    out = Path(sys.argv[1]).resolve()
    spec = importlib.util.spec_from_file_location(
        "montage_e2e", ROOT / "scripts/ios/phone-montage-render-e2e.py"
    )
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    base.main()
    meta = json.loads((out / "e2e.json").read_text())
    reports = []
    bindings = (base._binding(out / "crossfade-c0.mp4", "crossfade-c0"),)
    voice_sha, voice_size = base._fingerprint(out / "voiceover.m4a")
    voice = base.PhoneNarrationBed(
        plan_item_id="e2e-item",
        generation="9",
        fingerprint=base.RenderFingerprint(sha256=voice_sha, byte_count=voice_size),
        duration_s=10,
    )

    def add(case_id, recipe, rows, *, title_preview=False):
        assert recipe.text_layers and all(
            run.stroke_width == 0 and not run.blur_layers
            for layer in recipe.text_layers
            for run in layer.runs
        ), case_id
        status_file = "status-" + case_id.replace("_", "-") + ".json"
        (out / status_file).write_text(
            json.dumps(base._status(uuid.uuid4(), case_id, recipe), indent=2)
        )
        case = {
            "status_file": status_file,
            "duration_s": recipe.duration,
            "required_capabilities": sorted(recipe.required_capabilities),
            "drop_capability": "positionedText",
            "clips": [{"media_id": "crossfade-c0", "file": "crossfade-c0.mp4"}],
            "samples": [{"name": "style", "t": 0.8, "x": 8, "y": 8, "rgb": [0, 0, 255]}],
            "requires_off_effects": True,
            "resolved_text_elements": rows,
            "caption_samples": [
                {
                    "name": "text-on",
                    "t": 0.8,
                    "region": base._caption_region(
                        recipe.text_layers[0], canvas_width=1080, canvas_height=1920
                    ),
                    "expect_text": True,
                }
            ],
        }
        if any(track.kind == "audio" for track in recipe.tracks):
            case.update(voiceover_asset_id="voiceover-e2e-item", voiceover_file="voiceover.m4a")
        if title_preview:
            case.update(
                title_element=rows[0],
                title_region=case["caption_samples"][0]["region"],
                title_preview_samples=[0.4, 0.8, 1.2],
                exported_title_prefix="intro-",
            )
        meta["cases"][case_id] = case
        meta["verified_features"] = sorted(
            set(meta["verified_features"]) | recipe.required_capabilities
        )
        reports.append(
            {
                "case": case_id,
                "duration_s": recipe.duration,
                "bounds": [
                    {
                        "id": row["id"],
                        "box": measure_box(row),
                        "animation_box": animation_box(row, measure_box(row)),
                        "font": row["font_family"],
                        "stroke_width": row["stroke_width"],
                        "shadow_enabled": row["shadow_enabled"],
                    }
                    for row in rows
                ],
            }
        )

    for name, wording in [
        ("short", "Lisbon"),
        ("medium", "A slow morning in Lisbon"),
        ("long", "The best part of Lisbon was getting lost in its beautiful streets"),
        ("turkish", "İstanbul'da güzel bir gün"),
    ]:
        title = TextElement(
            id="voiceover-intro-0",
            text=wording,
            start_s=0,
            end_s=2.8,
            size_px=104,
            role="generative_intro",
            effect="fade-in",
        ).model_dump(mode="json", exclude_none=True)
        pin = TextElement(
            id="guided-pinned-0",
            text="DAY ONE",
            start_s=0,
            end_s=2.8,
            position="custom",
            x_frac=0.06,
            y_frac=0.70,
            alignment="left",
            size_px=52,
            effect="static",
        ).model_dump(mode="json", exclude_none=True)
        preferences = (
            {"title": {"favorite_fonts": ["Big Curls"]}}
            if name == "turkish"
            else {"title": {"font_family": "Inter Medium"}}
        )
        faces = [
            {"start_s": 0, "end_s": 2.8, "box": (0.32, 0.1, 0.66, 0.5)},
            {"start_s": 0.8, "end_s": 2.8, "box": (0.40, 0.15, 0.74, 0.52)},
        ]
        rows = resolve_phone_text_style([title, pin], preferences=preferences, excluded_boxes=faces)
        assert [r["text"] for r in rows] == [wording, "DAY ONE"]
        decision = base._decision(
            name,
            [base._step("crossfade-c0")],
            {"crossfade-c0": "crossfade-c0"},
            text_mode="none",
            intro_overlay_params=None,
            mix=0.4,
            voiceover_gcs_path="voiceover-uploads/direct/u/e2e-item/voice.m4a",
        )
        recipe = compile_phone_voiceover_montage_plan(
            decision, bindings, narration=voice, resolved_text_elements=rows
        )
        add("kri648_" + name, recipe, rows, title_preview=True)

    for index, font in enumerate(CURATED_PHONE_FONTS):
        row = TextElement(
            id="voiceover-intro-0",
            text="Lisbon",
            start_s=0,
            end_s=2.8,
            size_px=104,
            effect="fade-in",
        ).model_dump(mode="json", exclude_none=True)
        rows = resolve_phone_text_style([row], requested={"title": {"font_family": font}})
        recipe = compile_phone_voiceover_montage_plan(
            decision, bindings, narration=voice, resolved_text_elements=rows
        )
        add(f"kri648_palette_{index:02}", recipe, rows, title_preview=True)

    policy = {
        "version": 1,
        "preferences": {},
        "requested": {},
        "selected_fonts": {
            "caption": "Inter Medium",
            "title": "Inter Medium",
            "pinned": "Inter Medium",
        },
    }
    cues = [{"id": "cue-one", "text": "İstanbul'da bir gün", "start_s": 0.3, "end_s": 2.8}]
    with phone_text_policy_scope(policy):
        recipe = compile_phone_subtitled_plan(bindings, caption_cues=cues)
        add("kri648_captions", recipe, [])
        fields = generation_style_fields()
        meta["cases"]["kri648_captions"]["caption_snapshot"] = {
            **fields,
            "edit_format": "subtitled",
            "caption_cues": cues,
        }
        from app.pipeline.phone_captions import caption_look_from_variant, compile_caption_layers

        replay = compile_caption_layers(
            cues,
            canvas_width=1080,
            canvas_height=1920,
            style="sentence",
            look=caption_look_from_variant(fields),
            timeline_duration_s=recipe.duration,
        )
        assert replay == recipe.text_layers
        from app.routes.generative_jobs import EditorCommitCaptionMeta

        EditorCommitCaptionMeta.model_validate(
            {
                "font": fields["voiceover_caption_font"],
                "font_set": True,
                "stroke_width": fields["caption_stroke_width"],
                "shadow_enabled": fields["caption_shadow_enabled"],
                "appearance": fields["caption_editor_style"],
            }
        )
        (out / "kri648-caption-style.json").write_text(
            json.dumps(generation_style_fields(), indent=2)
        )
    with phone_text_policy_scope(policy):
        recipe = compile_phone_narrated_plan(
            [NarratedPhoneStep(step_id="one", media_id="crossfade-c0", start_s=0, end_s=3)],
            bindings,
            voice,
            voiceover_duration_s=3,
            caption_cues=cues,
            opening_title="Lisbon diary",
            opening_title_end_s=2.8,
        )
        rows = narrated_title_text_elements(recipe, "Lisbon diary", end_s=2.8)
        add("kri648_narrated", recipe, rows)
        recipe, pin_rows = with_pinned_text_layers(
            recipe, [PinnedText(text="DAY ONE", corner="bottom_left")]
        )
        rows = narrated_title_text_elements(recipe, "Lisbon diary", end_s=2.8)
        add("kri648_narrated_pins", recipe, [*rows, *pin_rows])
        meta["cases"]["kri648_narrated_pins"].update(
            title_element=rows[0],
            title_region=base._caption_region(
                recipe.text_layers[0], canvas_width=1080, canvas_height=1920
            ),
            title_preview_samples=[0.4, 0.8, 1.2],
            exported_title_prefix="title-",
        )
    with phone_text_policy_scope({**policy, "requested": {"caption": {"position": "top_left"}}}):
        recipe = compile_phone_subtitled_plan(bindings, caption_cues=cues)
        fields = generation_style_fields()
        add("kri648_caption_corner", recipe, [])
        meta["cases"]["kri648_caption_corner"]["caption_snapshot"] = {
            **fields,
            "edit_format": "subtitled",
            "caption_cues": cues,
        }
        assert (
            compile_caption_layers(
                cues,
                canvas_width=1080,
                canvas_height=1920,
                style="sentence",
                look=caption_look_from_variant(fields),
                timeline_duration_s=recipe.duration,
            )
            == recipe.text_layers
        )

    try:
        resolve_phone_text_style(
            [TextElement(id="title", text="İstanbul", start_s=0, end_s=3).model_dump(mode="json")],
            requested={"title": {"font_family": "Big Curls"}},
        )
        raise AssertionError("Explicit limited-coverage font must require clarification")
    except CreatorRenderContractError as exc:
        assert exc.decline_reason == "needs_choice"

    if len(sys.argv) > 2:
        # Optional installed sample photograph: pan a real face over changing
        # backgrounds and use the actual OpenCV/proxy sampler, not supplied boxes.
        import shutil
        from app.config import settings
        from app.services.phone_text_styling import current_phone_text_policy, generation_style_rows
        from app.storage import local_object_path

        image_path = Path(sys.argv[2]).resolve(strict=True)
        face_clip = out / "moving-face.mp4"
        base._ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "color=c=0x101010:s=1080x1920:r=30:d=10",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-filter_complex",
            "[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=0x444444:t=fill:enable='gte(t,1.4)'[bg];[1:v]scale=480:-1[face];[bg][face]overlay=x='100+70*t':y=160:shortest=1[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-t",
            "10",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            str(face_clip),
        )
        face_binding = base._binding(face_clip, "moving-face")
        old = (settings.storage_provider, settings.e2e_fixtures, settings.local_storage_root)
        try:
            settings.storage_provider, settings.e2e_fixtures, settings.local_storage_root = (
                "local",
                True,
                str(out / "proxies"),
            )
            proxy = local_object_path(face_binding.proxy_path)
            proxy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(face_clip, proxy)
            face_decision = base._decision(
                "face",
                [base._step("moving-face")],
                {"moving-face": "moving-face"},
                text_mode="none",
                intro_overlay_params=None,
                mix=0.4,
                voiceover_gcs_path="voiceover-uploads/direct/u/e2e-item/voice.m4a",
            )
            with phone_text_policy_scope(
                {**policy, "_source_bindings": [face_binding.model_dump(mode="json")]}
            ):
                compile_phone_voiceover_montage_plan(
                    face_decision, (face_binding,), narration=voice
                )
                row = TextElement(
                    id="voiceover-intro-0",
                    text="A moving portrait",
                    start_s=0,
                    end_s=2.8,
                    size_px=104,
                    effect="fade-in",
                ).model_dump(mode="json", exclude_none=True)
                rows = generation_style_rows([row])
                environment = current_phone_text_policy()
                assert environment["_face_boxes"], (
                    "The actual sampler must detect the supplied face"
                )
                swept = animation_box(rows[0], measure_box(rows[0]))
                for face in environment["_face_boxes"]:
                    assert not (
                        swept[0] < face["box"][2]
                        and face["box"][0] < swept[2]
                        and swept[1] < face["box"][3]
                        and face["box"][1] < swept[3]
                    )
                recipe = compile_phone_voiceover_montage_plan(
                    face_decision, (face_binding,), narration=voice, resolved_text_elements=rows
                )
                add("kri648_moving_face", recipe, rows, title_preview=True)
                meta["cases"]["kri648_moving_face"].update(
                    clips=[{"media_id": "moving-face", "file": "moving-face.mp4"}],
                    samples=[{"name": "style", "t": 0.8, "x": 8, "y": 8, "rgb": [16, 16, 16]}],
                )
                reports[-1].update(
                    actual_face_boxes=environment["_face_boxes"],
                    sampled_background_count=len(environment["_background_frames"]),
                )
        finally:
            settings.storage_provider, settings.e2e_fixtures, settings.local_storage_root = old
    (out / "e2e.json").write_text(json.dumps(meta, indent=2))
    from app.pipeline.generative_overlays import build_overlays_from_text_elements

    (out / "kri648-overlay-recipe.json").write_text(
        json.dumps(
            {
                "slots": [
                    {
                        "name": name,
                        "text_overlays": build_overlays_from_text_elements(
                            [
                                TextElement.model_validate(row)
                                for row in case["resolved_text_elements"]
                            ],
                            video_duration_s=case["duration_s"],
                            independent_box_alignment=True,
                        ),
                    }
                    for name, case in meta["cases"].items()
                    if name.startswith("kri648_") and case.get("resolved_text_elements")
                ]
            },
            indent=2,
        )
    )
    (out / "kri648-measurements.json").write_text(
        json.dumps(
            {"synthetic_face_regions": True, "platform_clearance": "unverified", "cases": reports},
            indent=2,
        )
    )
    print(f"KRI-648 native render input and measurement report: {out}")


if __name__ == "__main__":
    main()
