#!/usr/bin/env python3
# ruff: noqa: E402 -- executable harness configures sys.path and environment before app imports.
"""Prepare the item-17 on-device check: do the background recordings line up with the words
that name them?

Compiles the REAL phone Talking (subtitled) recipe the way `_run_phone_subtitled_job` does for an
approved strategy with a ``background_media`` sequence: the strategy dict goes through
`_strategy_background_request` + `_resolve_strategy_background` (the pool lookup faked with five
ready video rows, the take's whisper words as ``raw_words``, no speech cut), then into
`compile_phone_subtitled_plan` with the resolved background lane and
`validate_phone_pilot_recipe`. It writes what the device receives (`status-*.json`, `e2e.json`
case ``subtitled_background_windows``) so `DeviceMontageRenderE2ETests
.testSubtitledBackgroundWindowsRenderOnTheIPhone` renders it through the production device
resolver and exporter, plus ``windows.json`` (each window next to the word time of its name).

Footage is NOT made here: OUT_DIR must already hold (never in git)

  talk.mp4        portrait 1080x1920 take with the TTS audio
  words.json      [{"text", "start_s", "end_s"}] whisper words of that take
  rec1.mp4..rec5.mp4   the five "screen recordings"

usage:
  src/apps/api/.venv/bin/python scripts/ios/phone-background-windows-e2e.py OUT_DIR [--flag on|off]
      [--phrases labels|spoken] [--opening on|off] [--title TEXT] [--client-protocol N]

``--flag`` sets ``background_follows_talk_enabled`` (default off = today's behaviour).
``--phrases labels`` (default) feeds her typed label spellings as ``spoken_phrase``; ``spoken``
feeds each name's words exactly as whisper wrote them in words.json (what the planner is taught
to copy from the take's own transcript).

Qendresa item 16: ``--opening on`` asks to start full screen (``background_media
.full_screen_opening``) with ``SPEAKER_FULL_OPENING_ENABLED`` and ``speakerFrameWindows``
verified, for the app build ``--client-protocol`` (default 38); ``off`` is today's corner from
0 s. ``--title`` adds the Talking hook title row (`talking_title_element`), moved off the face
by the real `place_talking_title`; the face boxes come from OUT_DIR/face_boxes.json
(``[[x, y, w, h], ...]`` normalised, top-left origin, e.g. Apple Vision on the take) when
present, else from the real face sampler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

CASE_ID = "subtitled_background_windows"
CANVAS = {"width": 1080, "height": 1920}
REC_S = 10.0

# (file, pool id, her label spelling, token that names it in the take, which occurrence)
RECORDINGS = [
    ("rec1.mp4", "00000000-0000-4000-8000-000000000001", "Spec-kit", "spec", 1),
    ("rec2.mp4", "00000000-0000-4000-8000-000000000002", "Impeccable", "impeccable", 1),
    ("rec3.mp4", "00000000-0000-4000-8000-000000000003", "TradingAgents", "trading", 1),
    (
        "rec4.mp4",
        "00000000-0000-4000-8000-000000000004",
        "number four Agents",
        "agents",
        3,  # "Trading Agents", "AI agents", then "Number 4, Agents"
    ),
    (
        "rec5.mp4",
        "00000000-0000-4000-8000-000000000005",
        "Claude code game studio",
        "cla",  # whisper hears "Claude" or "Clawed"
        1,
    ),
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


# Where each recording is named in the take: (first token prefix, required next token
# prefixes or None, words in the phrase), in RECORDINGS order.
SPOKEN_SPANS = [
    ("spec", None, 2),
    ("impeccable", None, 1),
    ("trading", None, 2),
    ("number", ("4", "four"), 3),
    ("cla", ("code",), 4),
]


def _spoken_phrases(words: list[dict]) -> list[str]:
    """Each recording's name as whisper wrote it (trailing punctuation dropped)."""
    out: list[str] = []
    for first, nxt, count in SPOKEN_SPANS:
        for i, word in enumerate(words):
            if not _token(word["text"]).startswith(first):
                continue
            if nxt is not None and not (
                i + 1 < len(words) and _token(words[i + 1]["text"]).startswith(nxt)
            ):
                continue
            out.append(
                " ".join(w["text"].strip().rstrip(".,!?") for w in words[i : i + count])
            )
            break
        else:
            raise SystemExit(f"{first!r} is not spoken in words.json")
    return out


def _name_time(words: list[dict], token: str, occurrence: int) -> float | None:
    seen = 0
    for word in words:
        if _token(word["text"]) == token or _token(word["text"]).startswith(token):
            seen += 1
            if seen == occurrence:
                return word["start_s"]
    return None


def _title_row(
    text: str, words: list[dict], take_s: float, talk: Path, out: Path
) -> dict:
    """The Talking hook title row, placed off the face the way the worker places it."""
    from app.pipeline.phone_subtitled_title import (
        place_talking_title,
        talking_title_element,
    )
    from app.pipeline.render_geometry import (
        NormalizedBox,
        ProtectedRegion,
        _face_protection_box,
    )

    class _Canvas:
        width = CANVAS["width"]
        height = CANVAS["height"]

    row = talking_title_element(
        text,
        duration_s=None,
        first_word_end_s=words[0]["end_s"] if words else None,
        timeline_duration_s=take_s,
        canvas=_Canvas(),
    )
    assert row is not None, "no title row"
    sample = None
    boxes_file = out / "face_boxes.json"
    if boxes_file.exists():
        boxes = json.loads(boxes_file.read_text())

        def sample(_path, anchors, **_kw):  # noqa: ANN001, ANN202
            regions = [
                ProtectedRegion(
                    start_s=0.0,
                    end_s=take_s,
                    box=_face_protection_box(
                        NormalizedBox(left=x, top=y, right=x + w, bottom=y + h)
                    ),
                    kind="face",
                )
                for x, y, w, h in boxes
            ]
            return regions, {"detected": len(regions), "source": "face_boxes.json"}

    placed, receipt = place_talking_title(
        row,
        clip_path=str(talk),
        keep_segments=[(0.0, take_s)],
        display_width=CANVAS["width"],
        display_height=CANVAS["height"],
        canvas=_Canvas(),
        sample=sample,
    )
    print(f"title placed: y_frac={placed.get('y_frac')} receipt={receipt}")
    return placed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir")
    parser.add_argument("--flag", choices=("on", "off"), default="off")
    parser.add_argument("--phrases", choices=("labels", "spoken"), default="labels")
    parser.add_argument("--opening", choices=("on", "off"), default="off")
    parser.add_argument("--title", default=None)
    parser.add_argument("--client-protocol", type=int, default=38)
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    import app.services.phone_overlay_grounding as pool_mod
    from app.tasks import generative_build as gb

    words = json.loads((out / "words.json").read_text())
    talk = out / "talk.mp4"
    import subprocess

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

    # Gates open (outside an HTTP request the client-protocol gate is open too), flag per --flag.
    settings.speaker_background_enabled = True
    settings.background_follows_talk_enabled = args.flag == "on"
    settings.speaker_full_opening_enabled = args.opening == "on"
    settings.phone_render_verified_features = sorted(
        {
            *settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
            *(("speakerFrameWindows",) if args.opening == "on" else ()),
        }
    )

    phrases = (
        _spoken_phrases(words)
        if args.phrases == "spoken"
        else [label for _f, _p, label, _t, _o in RECORDINGS]
    )
    print(f"phrases ({args.phrases}): {phrases}")

    # --- the strategy's background_media, exactly as the planner stores it ------------------
    strategy_background = {
        "media_id": f"asset-{RECORDINGS[0][1]}",
        "source_start_s": 0.0,
        "sequence": [
            {"media_id": f"asset-{pool_id}", "spoken_phrase": phrase}
            for (_file, pool_id, _label, _tok, _occ), phrase in zip(
                RECORDINGS,
                phrases,
                strict=True,
            )
        ],
        "speaker_frame": "small_bottom_right",
        **({"full_screen_opening": True} if args.opening == "on" else {}),
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
        for _file, pool_id, _label, _tok, _occ in RECORDINGS
    ]
    pool_mod._load_ready_pool_assets = lambda *a, **k: pool_rows  # noqa: SLF001

    request = gb._strategy_background_request(all_candidates)
    assert request is not None, "the strategy's background_media did not parse"
    title_row = _title_row(args.title, words, take_s, talk, out) if args.title else None
    lane_drops: list[dict] = []
    background, reserved = gb._resolve_strategy_background(
        request,
        job_id=str(uuid.uuid4()),
        lane_drops=lane_drops,
        words=words,
        keep_segments=None,
        take_s=gb._background_take_s(None, take_s),
        creator_client_protocol=args.client_protocol,
        title_end_s=float(title_row["end_s"]) if title_row else None,
    )
    print(f"gb loaded from {gb.__file__}")
    print(f"lane_drops={lane_drops} reserved={sorted(reserved)}")
    assert background is not None and background.windows, (
        "no background windows resolved"
    )

    # --- bindings + the real compile ---------------------------------------------------------
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
    for file, pool_id, _label, _tok, _occ in RECORDINGS:
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

    recipe = compile_phone_subtitled_plan(
        (speaker,),
        caption_cues=_cues(words),
        visuals=tuple(visuals),
        lanes=PhoneSubtitledLanes(background=background),
        text_elements=(title_row,) if title_row else (),
    )
    layer = recipe.speaker_background
    assert layer is not None and layer.windows, (
        "the recipe carries no background windows"
    )

    verified = sorted(
        set(recipe.required_capabilities)
        | {"speakerBackground", "speakerBackgroundWindows"}
        | ({"speakerFrameWindows"} if args.opening == "on" else set())
    )
    settings.phone_render_verified_features = verified
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)

    # --- fixtures the Swift test reads ---------------------------------------------------------
    job_id = uuid.uuid4()
    request_obj = make_device_request(
        job_id=job_id, variant_id=CASE_ID, revision=1, recipe=recipe
    )
    status = DeviceRenderStatus(
        phase="awaiting_device", request=request_obj
    ).model_dump(mode="json")
    (out / "status-subtitled-background-windows.json").write_text(
        json.dumps(status, indent=2)
    )

    by_pool = {
        pool_id: (file, label, tok, occ)
        for file, pool_id, label, tok, occ in RECORDINGS
    }
    number = {pool_id: i + 1 for i, (_f, pool_id, *_rest) in enumerate(RECORDINGS)}
    windows_report = []
    for window in layer.windows:
        pool_id = window.asset_id.removeprefix("visual-")
        _file, label, tok, occ = by_pool[pool_id]
        windows_report.append(
            {
                "recording": number[pool_id],
                "label": label,
                "start_s": round(window.start, 3),
                "end_s": round(window.end, 3),
                "name_word_s": _name_time(words, tok, occ),
            }
        )
    (out / "windows.json").write_text(
        json.dumps(
            {
                "flag": args.flag,
                "phrases": phrases,
                "opening": args.opening,
                "full_screen_windows": [
                    [round(w.start, 3), round(w.end, 3)]
                    for w in layer.full_screen_windows or []
                ],
                "title": title_row,
                "take_s": take_s,
                "recipe_duration_s": recipe.duration,
                "lane_drops": lane_drops,
                "fill": layer.asset_id,
                "windows": windows_report,
            },
            indent=2,
        )
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
                    f"visual-{pool_id}": file for file, pool_id, *_ in RECORDINGS
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

    print(f"Wrote {out} (flag={args.flag}); duration={recipe.duration:.3f}s")
    print(f"required_capabilities={sorted(recipe.required_capabilities)}")
    for row in windows_report:
        print(
            f"  rec{row['recording']} {row['label']!r}: {row['start_s']:.2f}-{row['end_s']:.2f} "
            f"(name at {row['name_word_s']})"
        )


if __name__ == "__main__":
    main()
