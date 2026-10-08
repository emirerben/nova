"""Recorded live requests replayed through real parsers and destination compilers.

Expectations below are request outcomes, independent of the model's operation choices.
Synthetic storage is the only mocked save boundary; no render/export is claimed.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.routes.generative_jobs import prepare_editor_commit
from app.schemas.slide_post import SlideEdits, SlidePostDraft, SlideRef, SlideTextElement
from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from app.services.slide_post_chat_edit import compile_slide_post_ops
from tests.evals.runners.snapshot_variant import build_synthetic_job, build_synthetic_variant

CAPTURE = Path(__file__).resolve().parents[2] / "fixtures/prompt_coverage/system_composition.json"


def load_cases(path=CAPTURE):
    return json.loads(path.read_text())["cases"]


def parse_capture(row):
    # Retain the full response, including refusals, confidence and unmet requests.
    return EditCopilotAgent(ModelClient()).parse(
        row["calls"][-1]["raw_text"], EditCopilotInput.model_validate(row["input"])
    )


def slide_draft(snapshot):
    slides = []
    for slot in snapshot["slots"]:
        sid = slot["media_id"]
        texts = []
        for bar in snapshot.get("text_bars", []):
            if not (slot["output_start_s"] <= bar["start_s"] < slot["output_end_s"]):
                continue
            is_label = bar["id"].startswith("clip-label-media-")
            fields = {
                k: v for k, v in bar.items() if k in SlideTextElement.model_fields and v is not None
            }
            fields.update(id=bar["id"].split(":")[-1], role="label" if is_label else "text")
            texts.append(SlideTextElement.model_validate(fields))
        slides.append(
            SlideRef(
                id=sid,
                asset_id=uuid.uuid5(uuid.NAMESPACE_URL, sid),
                kind=slot["media_kind"],
                edits=SlideEdits(texts=texts),
            )
        )
    return SlidePostDraft(slides=slides, **snapshot["post"])


def replay_capture(row):
    parsed = parse_capture(row)
    assert parsed.outcome == row["expected_outcome"], (row["case"], parsed)
    if row["expected_outcome"] != "proposed":
        assert not parsed.ops
        assert parsed.reply.strip()
        if row["expected_outcome"] == "clarification":
            assert parsed.needs_clarification
        return {"parsed": parsed, "tier": "parser_control"}
    assert parsed.ops, row["case"]
    snapshot = row["input"]["variant_snapshot"]
    if snapshot.get("surface") == "slide_post":
        before = slide_draft(snapshot)
        after = compile_slide_post_ops(before, parsed.ops).draft
        # Exercise stored schema normalization as well as the live compiler.
        after = SlidePostDraft.model_validate(after.model_dump(mode="json"))
        return {"parsed": parsed, "before": before, "after": after, "tier": "slide_compile"}
    variant = build_synthetic_variant(snapshot)
    if snapshot.get("captions"):
        variant["resolved_archetype"] = "subtitled"
    variant["original_audio_level"] = snapshot.get("mix", {}).get("original_level", 1.0)
    job = build_synthetic_job(variant)
    # Use durable synthetic sources with original identities/timing intact.
    for i, slot in enumerate(variant["ai_timeline"]["slots"]):
        slot["source_gcs_path"] = f"generative-jobs/{job.id}/sources/{i}.mp4"
    job.all_candidates["clip_paths"] = [
        s["source_gcs_path"] for s in variant["ai_timeline"]["slots"]
    ]
    before = copy.deepcopy(variant)
    compiled = compile_editor_ops(job, variant, parsed.ops)
    after = project_editor_draft(variant, compiled.payload.model_dump(mode="json"), job)
    # Full save is attempted for representable legacy snapshots. Device/guided
    # captures need their persisted recipe, absent from path-free snapshots.
    save_verified = False
    if (
        snapshot.get("render_destination") != "device"
        and not snapshot.get("guided_revision")
        and not compiled.payload.sound_effects
    ):
        with patch("app.routes.generative_jobs.storage.object_exists", return_value=True):
            prepare_editor_commit(
                job,
                variant["variant_id"],
                compiled.payload,
                user_id=uuid.UUID("00000000-0000-0000-0000-000000000524"),
            )
        after = job.assembly_plan["variants"][0]
        save_verified = True
    return {
        "parsed": parsed,
        "before": before,
        "after": after,
        "payload": compiled.payload,
        "job": job,
        "tier": "compiler_save" if save_verified else "compiler_only",
    }


def independent_assertions(row, result):
    name = row["case"]
    if row["expected_outcome"] != "proposed":
        return
    after = result["after"]
    before = result["before"]
    if name.startswith("slide_"):

        def texts(draft):
            return [t for s in draft.slides for t in (s.edits.texts if s.edits else [])]

        if name == "slide_caption_and_remove":
            assert [s.id for s in after.slides] == [
                s.id for i, s in enumerate(before.slides) if i != 1
            ]
            assert after.caption == "Two days in Istanbul"
        elif name == "slide_chrono_and_location":
            assert [s.id for s in after.slides] == [
                "slide-5",
                "slide-2",
                "slide-3",
                "slide-4",
                "slide-1",
            ]
            assert {t.text for t in texts(after)} >= {"Beşiktaş", "Kadıköy", "Üsküdar"}
        elif name == "slide_same_style_all_text":
            assert [t.text for t in texts(after)] == [t.text for t in texts(before)]
            assert all(t.color == "#FFFFFF" and 0 < t.stroke_width <= 3 for t in texts(after))
        elif name == "slide_set_cover":
            assert after.slides[after.cover_index].id == before.slides[2].id
            assert [s.id for s in after.slides] == [s.id for s in before.slides]
        elif name == "slide_text_on_every_slide":
            assert all(any(t.text == "Summer 2026" for t in s.edits.texts) for s in after.slides)
        else:
            raise AssertionError(f"missing independent assertion: {name}")
        return

    def slots(v):
        return [
            s for s in (v.get("user_timeline") or v["ai_timeline"])["slots"] if not s.get("removed")
        ]

    a, b = slots(after), slots(before)
    if name == "caption_fix_typo":
        assert after["caption_cues"][0]["text"] == "hello there"
    elif name == "caption_emphasize_named_entity":
        assert after["caption_cues"][0]["smart_emphasis"] is True
    elif name == "caption_word_style_meta":
        assert after["voiceover_caption_style"] == "word" and after["caption_margin_v"] < round(
            (1 - before["caption_y_frac"]) * 1920
        )
    elif name == "captions_toggle_off":
        assert after["captions_enabled"] is False
    elif name == "compound_multi_family":
        assert result["payload"].title == "coffee launch"
        assert any(
            s["sound_effect_id"] == "pop" and s["at_s"] == 0
            for s in result["payload"].sound_effects
        )
    elif name == "kria_v2_audio_music_quieter":
        assert 0 <= after["mix"] < before["mix"]
        assert after["original_audio_level"] == before["original_audio_level"]
    elif name == "kria_v2_audio_original_mute_device":
        assert after["original_audio_level"] == 0
    elif name == "kria_v2_audio_remove_music_tr":
        assert after["music_track_id"] is None
    elif name == "kria_v2_capture_order_and_hour":
        assert [s["clip_index"] for s in a] == [1, 3, 2, 0]
        assert {"12:05", "14:47", "17:32"} <= {
            hour for t in after["text_elements"] for hour in t["text"].split()
        }
    elif name == "kria_v2_timeline_all_clips_2s":
        assert all(s["duration_s"] == 2 for s in a) and len(a) == len(b)
    elif name == "kria_v2_timeline_crossfade_every_clip":
        assert all(s["transition_after"] == "crossfade" for s in a[:-1])
    elif name == "kria_v2_timeline_cut_first_2_seconds":
        assert a[0]["in_s"] == b[0]["in_s"] + 2
    elif name == "kria_v2_timeline_make_it_20s":
        assert abs(sum(s["duration_s"] for s in a) - 20) < 0.001
    elif name == "kria_v2_timeline_move_bridge_first":
        assert [s["clip_index"] for s in a] == [3, 0, 1, 2]
    elif name == "kria_v2_timeline_remove_clip_4":
        assert [s["clip_index"] for s in a] == [0, 1, 2]
    elif name == "kria_v2_timeline_speed_up_clip":
        assert a[1]["playback_rate"] > b[1].get("playback_rate", 1)
        assert a[0] == b[0] and a[2:] == b[2:]
    elif name == "story_compound_trim_remove_music":
        assert a[0]["in_s"] == 1 and after["music_track_id"] is None
    elif name == "story_mute_music_not_remove":
        assert after["mix"] == 0 and after["music_track_id"] == before["music_track_id"]
    else:
        raise AssertionError(f"missing independent assertion: {name}")
    if (
        name.startswith("caption")
        or name.startswith("kria_v2_audio")
        or name == "story_mute_music_not_remove"
    ):
        assert a == b, "non-timeline request changed source ranges/order"
