#!/usr/bin/env python3
# ruff: noqa: E402 -- executable harness configures sys.path and environment before app imports.
"""Prepare the item-19 on-device check: numbered list labels on a phone Talking screen walkthrough.

Compiles the REAL phone Talking (subtitled) recipe the way `_run_phone_subtitled_job` does for an
approved strategy with a ``background_media`` sequence (same setup as
`phone-background-windows-e2e.py`: fake pool rows, the take's whisper words as ``raw_words``,
`_strategy_background_request` + `_resolve_strategy_background`), then -- with ``--labels on`` --
turns five brief ``text`` requirements ("1. Spec-kit" ... "5. Claude code game studio") into label
rows with `list_label_asks` + `_phone_talking_list_label_rows` and compiles them in as spoken-text
rows. ``--labels off`` is today's output: the identical compile with no label rows.

It writes what the device receives (`status-subtitled-background-windows.json`, `e2e.json`, case
``subtitled_background_windows``) so `DeviceMontageRenderE2ETests
.testSubtitledBackgroundWindowsRenderOnTheIPhone` renders it through the production exporter, plus
``labels.json`` (each label next to the take time its item is introduced, and the background
windows).

Footage is NOT made here: OUT_DIR must already hold REAL files (never symlinks, never in git)

  talk.mp4        portrait 1080x1920 take with the TTS audio
  words.json      [{"text", "start_s", "end_s"}] whisper words of that take
  rec1.mp4..rec5.mp4   the five "screen recordings"

usage:
  src/apps/api/.venv/bin/python scripts/ios/qendresa-list-labels-e2e.py OUT_DIR --labels on|off [--case q2|q1]

``--case q1`` is the full-frame talking head (talk.mp4 + words.json only): ONE typed example
("1. which company it sends data to?") and the worker finds the other items in the take; it
writes the plain ``subtitled_sentence`` device case instead (status-subtitled-sentence.json).

Item 17's ``background_follows_talk_enabled`` is ON in both runs; only the labels flag differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
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

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

CASE_ID = "subtitled_background_windows"
CANVAS = {"width": 1080, "height": 1920}
REC_S = 10.0

# (file, pool id, brief label, token that introduces the item in the take, first-token occurrence
# is taken AFTER the lead-in words in INTRO_LEADS)
RECORDINGS = [
    ("rec1.mp4", "00000000-0000-4000-8000-000000000001", "1. Spec-kit"),
    ("rec2.mp4", "00000000-0000-4000-8000-000000000002", "2. Impeccable"),
    ("rec3.mp4", "00000000-0000-4000-8000-000000000003", "3. TradingAgents"),
    ("rec4.mp4", "00000000-0000-4000-8000-000000000004", "4. Agents"),
    ("rec5.mp4", "00000000-0000-4000-8000-000000000005", "5. Claude code game studio"),
]

# How each item is introduced in the take: (lead-in tokens, word that names it, words in the
# background entry's spoken phrase starting at the lead-in (or at the name when no lead-in)).
INTRODUCTIONS = [
    (("first", "one", "is"), "speckit", 1, False),
    (("next", "one", "is"), "impeccable", 1, False),
    (("third", "one", "is"), "trading", 2, False),
    (
        ("fourth", "one", "is"),
        "agents",
        4,
        True,
    ),  # the entry includes its lead-in words
    (("last", "one", "is"), "cloud", 4, False),
]


def _sha(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _token(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _cues(words: list[dict]) -> list[dict]:
    """Sentence captions: a cue ends at sentence punctuation or after 7 words."""
    cues: list[dict] = []
    group: list[dict] = []
    for word in words:
        group.append(word)
        if word["text"].rstrip().endswith((".", "?", "!")) or len(group) >= 7:
            cues.append(
                {
                    "text": " ".join(w["text"] for w in group),
                    "start_s": group[0]["start_s"],
                    "end_s": group[-1]["end_s"],
                }
            )
            group = []
    if group:
        cues.append(
            {
                "text": " ".join(w["text"] for w in group),
                "start_s": group[0]["start_s"],
                "end_s": group[-1]["end_s"],
            }
        )
    return cues


def _introductions(words: list[dict]) -> list[tuple[int, float]]:
    """(index of the naming word, its start) per item: the name right after its lead-in words."""
    tokens = [_token(w["text"]) for w in words]
    out: list[tuple[int, float]] = []
    for lead, name, _count, _lead_in_phrase in INTRODUCTIONS:
        for i in range(len(tokens) - len(lead)):
            if tuple(tokens[i : i + len(lead)]) == lead and tokens[
                i + len(lead)
            ].startswith(name):
                # the item is introduced at its lead-in ("Fourth one is Agents"), not the bare name
                at = words[i if lead == ("fourth", "one", "is") else i + len(lead)][
                    "start_s"
                ]
                out.append((i + len(lead), float(at)))
                break
        else:
            raise SystemExit(f"{lead} + {name!r} is not spoken in words.json")
    return out


def _spoken_phrases(words: list[dict]) -> list[str]:
    """Each entry's phrase as whisper wrote it (what the planner copies from the transcript)."""
    phrases: list[str] = []
    for (lead, _name, count, with_lead), (index, _at) in zip(
        INTRODUCTIONS, _introductions(words), strict=True
    ):
        first = index - len(lead) if with_lead else index
        phrases.append(
            " ".join(
                w["text"].strip().rstrip(".,!?") for w in words[first : first + count]
            )
        )
    return phrases


Q1_LABEL = "1. which company it sends data to?"
Q1_LEADS = ("which", "what", "what", "what")  # first word of each item, in take order


def _q1_item_words(words: list[dict]) -> list[tuple[str, float]]:
    """(word, start) of each item's first word: "which", then the next three "what" after it."""
    tokens = [_token(w["text"]) for w in words]
    at = next(
        (
            i
            for i, t in enumerate(tokens[:-1])
            if t == "which" and tokens[i + 1].startswith("compan")
        ),
        None,
    )
    if at is None:
        raise SystemExit("'which companies' is not spoken in words.json")
    out = [("which", float(words[at]["start_s"]))]
    for i in range(at + 1, len(tokens)):
        if tokens[i] == "what":
            out.append(("what", float(words[i]["start_s"])))
        if len(out) == 4:
            break
    if len(out) < 4:
        raise SystemExit("fewer than three 'what' after 'which' in words.json")
    return out


def _main_q1(args: argparse.Namespace, out: Path) -> None:
    """Q1: a full-frame talking head, ONE typed example; the worker finds the rest of the list."""
    from app.services import phone_rollout
    from app.tasks import generative_build as gb

    case_id = "subtitled_sentence"
    words = json.loads((out / "words.json").read_text())
    talk = out / "talk.mp4"
    take_s = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(talk),
            ]
        )
    )
    settings.phone_subtitled_title_enabled = True
    settings.phone_subtitled_spoken_text_enabled = True
    settings.spoken_list_labels_enabled = args.labels == "on"
    settings.phone_render_verified_features = sorted(
        {*settings.phone_render_verified_features, "positionedText", "animatedText"}
    )
    if args.labels == "on":
        assert phone_rollout.phone_list_labels_supported(), "list-label gate is closed"

    sha, size = _sha(talk)
    speaker = PhoneSourceBinding(
        media_id="listq1-talk",
        proxy_path="users/owner/creation/analysis-proxy-listq1-talk.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=sha,
            byte_count=size,
            duration_s=take_s,
            width=CANVAS["width"],
            height=CANVAS["height"],
            has_audio=True,
        ),
    )
    cues = _cues(words)

    from app.pipeline.phone_list_labels import list_label_asks

    requirements = [
        BriefRequirement(
            id="req-label-1", kind="text", scope="global", literal=Q1_LABEL, facts={}
        )
    ]
    asks = list_label_asks(requirements)
    label_rows: list[dict] = []
    receipt: dict | None = None
    if args.labels == "on":
        label_rows, receipt = gb._phone_talking_list_label_rows(
            asks,
            anchors=[],
            words=words,
            cues=cues,
            caption_style="sentence",
            caption_look=None,
            shown_rows=[],
            cards=[],
            timeline_duration_s=take_s,
            binding=speaker,
            clip_path=str(talk),
            keep_segments=[(0.0, take_s)],
            landscape_fit="fit",
            position_x=0.0,
            speaker_frame=None,
        )
        print(f"label rows: {len(label_rows)}")

    recipe = compile_phone_subtitled_plan(
        (speaker,),
        caption_cues=cues,
        caption_style="sentence",
        text_elements=label_rows,
        text_elements_user_edited=bool(label_rows),
    )
    verified = sorted(set(recipe.required_capabilities) | {"positionedText"})
    settings.phone_render_verified_features = verified
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)
    # The plain Talking recipe already needs positionedText (captions), so dropping it
    # must send the render to the cloud with or without label rows.
    assert "positionedText" in recipe.required_capabilities

    request_obj = make_device_request(
        job_id=uuid.uuid4(), variant_id=case_id, revision=1, recipe=recipe
    )
    status = DeviceRenderStatus(
        phase="awaiting_device", request=request_obj
    ).model_dump(mode="json")
    (out / "status-subtitled-sentence.json").write_text(json.dumps(status, indent=2))
    e2e = {
        "verified_features": verified,
        "cases": {
            case_id: {
                "status_file": "status-subtitled-sentence.json",
                "duration_s": recipe.duration,
                "required_capabilities": sorted(recipe.required_capabilities),
                "drop_capability": "positionedText",
                "clips": [{"media_id": "listq1-talk", "file": "talk.mp4"}],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": False,
                "samples": [],
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2))

    items = _q1_item_words(words)
    receipt_rows = (receipt or {}).get("rows", [])
    labels_report = []
    for i, row in enumerate(label_rows):
        info = receipt_rows[i] if i < len(receipt_rows) else {}
        placement = info.get("placement") or {}
        labels_report.append(
            {
                "text": row.get("text"),
                "start_s": row["start_s"],
                "end_s": row["end_s"],
                "y_frac": row.get("y_frac"),
                "size_px": row.get("size_px"),
                "placement_status": placement.get("status"),
                "item_first_word": items[i][0] if i < len(items) else None,
                "item_first_word_s": items[i][1] if i < len(items) else None,
            }
        )
    (out / "labels.json").write_text(
        json.dumps(
            {
                "case": "q1",
                "labels_flag": args.labels,
                "take_s": take_s,
                "recipe_duration_s": recipe.duration,
                "item_first_words": [{"word": w, "start_s": t} for w, t in items],
                "labels": labels_report,
                "receipt": receipt,
            },
            indent=2,
        )
    )
    print(
        f"Wrote {out} (case=q1 labels={args.labels}); duration={recipe.duration:.3f}s"
    )
    print(f"required_capabilities={sorted(recipe.required_capabilities)}")
    for r in labels_report:
        print(
            f"{r['text']!r:45} first={r['item_first_word']}@{r['item_first_word_s']:.2f} "
            f"start={r['start_s']:.2f} end={r['end_s']:.2f} y={r['y_frac']} "
            f"px={r['size_px']} place={r['placement_status']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir")
    parser.add_argument("--labels", choices=("on", "off"), required=True)
    parser.add_argument(
        "--case",
        choices=("q2", "q1"),
        default="q2",
        help="q2 = screen walkthrough with background windows (default); "
        "q1 = full-frame talking head, one typed example",
    )
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if args.case == "q1":
        _main_q1(args, out)
        return

    import app.services.phone_overlay_grounding as pool_mod
    from app.pipeline.phone_list_labels import list_label_asks
    from app.tasks import generative_build as gb

    words = json.loads((out / "words.json").read_text())
    talk = out / "talk.mp4"
    take_s = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(talk),
            ]
        )
    )

    # Gates open (outside an HTTP request the client-protocol gate is open too).
    settings.speaker_background_enabled = True
    settings.background_follows_talk_enabled = True
    settings.phone_subtitled_title_enabled = True
    settings.phone_subtitled_spoken_text_enabled = True
    settings.spoken_list_labels_enabled = args.labels == "on"
    settings.phone_render_verified_features = sorted(
        {
            *settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
            "positionedText",
            "animatedText",
        }
    )
    from app.services import phone_rollout

    if args.labels == "on":
        assert phone_rollout.phone_list_labels_supported(), "list-label gate is closed"

    phrases = _spoken_phrases(words)
    intros = _introductions(words)
    print(f"phrases: {phrases}")

    # --- the strategy's background_media, exactly as the planner stores it ------------------
    strategy_background = {
        "media_id": f"asset-{RECORDINGS[0][1]}",
        "source_start_s": 0.0,
        "sequence": [
            {"media_id": f"asset-{pool_id}", "spoken_phrase": phrase}
            for (_file, pool_id, _label), phrase in zip(
                RECORDINGS, phrases, strict=True
            )
        ],
        "speaker_frame": "small_bottom_right",
    }
    all_candidates = {
        "creator_strategy": {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "background_media": strategy_background,
        }
    }
    pool_rows = [
        {
            "id": pool_id,
            "kind": "video",
            "gcs_path": f"users/owner/plan/item/pool/{pool_id}.mp4",
            "gcs_generation": "202",
        }
        for _file, pool_id, _label in RECORDINGS
    ]
    pool_mod._load_ready_pool_assets = lambda *a, **k: pool_rows  # noqa: SLF001

    # --- the brief's five list labels ---------------------------------------------------------
    requirements = [
        BriefRequirement(
            id=f"req-label-{i + 1}",
            kind="text",
            scope="global",
            literal=label,
            facts={},
        )
        for i, (_f, _p, label) in enumerate(RECORDINGS)
    ]
    asks = list_label_asks(requirements)
    assert len(asks) == 5, asks

    request = gb._strategy_background_request(all_candidates)
    assert request is not None, "the strategy's background_media did not parse"
    lane_drops: list[dict] = []
    layout: dict = {}
    background, reserved = gb._resolve_strategy_background(
        request,
        job_id=str(uuid.uuid4()),
        lane_drops=lane_drops,
        words=words,
        keep_segments=None,
        take_s=gb._background_take_s(None, take_s),
        layout_out=layout if args.labels == "on" else None,
    )
    print(f"gb loaded from {gb.__file__}")
    print(f"lane_drops={lane_drops} reserved={sorted(reserved)}")
    assert background is not None and background.windows, (
        "no background windows resolved"
    )

    # --- bindings ------------------------------------------------------------------------------
    sha, size = _sha(talk)
    speaker = PhoneSourceBinding(
        media_id="bgwin-talk",
        proxy_path="users/owner/creation/analysis-proxy-bgwin-talk.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=sha,
            byte_count=size,
            duration_s=take_s,
            width=CANVAS["width"],
            height=CANVAS["height"],
            has_audio=True,
        ),
    )
    visuals = []
    for file, pool_id, _label in RECORDINGS:
        v_sha, v_size = _sha(out / file)
        visuals.append(
            PhoneVisualBinding(
                media_id=pool_id,
                gcs_path=f"users/owner/plan/item/pool/{pool_id}.mp4",
                generation="202",
                sha256=v_sha,
                byte_count=v_size,
                kind="video",
                duration_s=REC_S,
                width=1080,
                height=1920,
            )
        )

    from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes

    cues = _cues(words)
    lanes = PhoneSubtitledLanes(background=background)

    # --- the label rows (flag on) --------------------------------------------------------------
    label_rows: list[dict] = []
    receipt: dict | None = None
    if args.labels == "on":
        frame = background.speaker_frame
        label_rows, receipt = gb._phone_talking_list_label_rows(
            asks,
            anchors=layout.get("anchors") or [],
            words=words,
            cues=cues,
            caption_style="sentence",
            caption_look=None,
            shown_rows=[],
            cards=[],
            timeline_duration_s=take_s,
            binding=speaker,
            clip_path=str(talk),
            keep_segments=[(0.0, take_s)],
            landscape_fit="fit",
            position_x=0.0,
            speaker_frame=(
                (frame.width_fraction, frame.x_fraction, frame.y_fraction)
                if frame is not None
                else None
            ),
        )
        print(f"label rows: {len(label_rows)}")

    recipe = compile_phone_subtitled_plan(
        (speaker,),
        caption_cues=cues,
        caption_style="sentence",
        visuals=tuple(visuals),
        lanes=lanes,
        text_elements=label_rows,
        text_elements_user_edited=bool(label_rows),
    )
    layer = recipe.speaker_background
    assert layer is not None and layer.windows, (
        "the recipe carries no background windows"
    )

    verified = sorted(
        set(recipe.required_capabilities)
        | {"speakerBackground", "speakerBackgroundWindows"}
    )
    settings.phone_render_verified_features = verified
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)

    # --- fixtures the Swift test reads ---------------------------------------------------------
    request_obj = make_device_request(
        job_id=uuid.uuid4(), variant_id=CASE_ID, revision=1, recipe=recipe
    )
    status = DeviceRenderStatus(
        phase="awaiting_device", request=request_obj
    ).model_dump(mode="json")
    (out / "status-subtitled-background-windows.json").write_text(
        json.dumps(status, indent=2)
    )

    e2e = {
        "verified_features": verified,
        "cases": {
            CASE_ID: {
                "status_file": "status-subtitled-background-windows.json",
                "duration_s": recipe.duration,
                "required_capabilities": sorted(recipe.required_capabilities),
                "drop_capability": "speakerBackgroundWindows",
                "clips": [{"media_id": "bgwin-talk", "file": "talk.mp4"}],
                "visual_files": {
                    f"visual-{pool_id}": file for file, pool_id, _l in RECORDINGS
                },
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": False,
                "samples": [],
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2))

    # --- labels.json ---------------------------------------------------------------------------
    by_text = {row["source_params"]["requirement_id"]: row for row in label_rows}
    receipt_by_id = {r["requirement_id"]: r for r in (receipt or {}).get("rows", [])}
    labels_report = []
    for i, ((_f, _p, label), (_idx, intro_s)) in enumerate(
        zip(RECORDINGS, intros, strict=True)
    ):
        rid = f"req-label-{i + 1}"
        row = by_text.get(rid)
        info = receipt_by_id.get(rid, {})
        placement = info.get("placement") or {}
        labels_report.append(
            {
                "label": label,
                "introduced_at_s": intro_s,
                "status": info.get("status"),
                "basis": info.get("basis"),
                "start_s": row["start_s"] if row else None,
                "end_s": row["end_s"] if row else None,
                "y_frac": row.get("y_frac") if row else None,
                "size_px": row.get("size_px") if row else None,
                "placement_status": placement.get("status"),
                "face_collision": info.get("face_collision"),
            }
        )
    windows_report = []
    number = {pool_id: i + 1 for i, (_f, pool_id, _l) in enumerate(RECORDINGS)}
    for window in layer.windows:
        windows_report.append(
            {
                "recording": number[window.asset_id.removeprefix("visual-")],
                "start_s": round(window.start, 3),
                "end_s": round(window.end, 3),
            }
        )
    (out / "labels.json").write_text(
        json.dumps(
            {
                "labels_flag": args.labels,
                "take_s": take_s,
                "recipe_duration_s": recipe.duration,
                "phrases": phrases,
                "lane_drops": lane_drops,
                "labels": labels_report,
                "background_windows": windows_report,
                "receipt": receipt,
            },
            indent=2,
        )
    )

    print(f"Wrote {out} (labels={args.labels}); duration={recipe.duration:.3f}s")
    print(f"required_capabilities={sorted(recipe.required_capabilities)}")
    print(
        f"{'label':30} {'intro_s':>8} {'start':>7} {'end':>7} {'y':>5} {'px':>5}  status/place"
    )
    for r in labels_report:
        start = "-" if r["start_s"] is None else f"{r['start_s']:.2f}"
        end = "-" if r["end_s"] is None else f"{r['end_s']:.2f}"
        print(
            f"{r['label']:30} {r['introduced_at_s']:8.2f} {start:>7} {end:>7} "
            f"{r['y_frac'] or 0:5.2f} {r['size_px'] or 0:5}  {r['status']}/{r['placement_status']}"
        )
    for w in windows_report:
        print(f"  rec{w['recording']}: {w['start_s']:.2f}-{w['end_s']:.2f}")


if __name__ == "__main__":
    main()
