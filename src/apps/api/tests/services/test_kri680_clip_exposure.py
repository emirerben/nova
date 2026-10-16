"""KRI-680: "skip the very dark clips" matches the clips that are measurably dark.

Prod thread 133BDDED (2026-10-15, boat kit M1): "Çok karanlık, hiçbir şeyin görünmediği
iki klibi kullanma." Every clip of that night set was described as "dark ..." by the
analyzer, so nothing matched and both junk clips (M1_04 black sky, M1_08 near-black seat
shuffle) rendered. The clip record now carries a measured brightness line
(``ClipUnderstanding.visibility``), from FFmpeg on the analysis copy.

The measurement is honest: on the real footage the "Hadi gidelim" pier clip (M1_11, asked
FIRST) and the boat-won't-start clip (M1_02, own sound kept) are as dark as the junk.
Those two stay because an exclusion never removes a clip another ask names or the
strategy plays (``clip_scope.excluded_clips``), so the edit loses exactly M1_04 and M1_08.

Ways this could go wrong, written down before the code (each has a case below):

1. A dim but visible night clip (torch on trees, first shore lights, a lit house) is
   called "very dark" and a "skip the dark ones" ask removes footage the creator wanted.
2. A near-black clip is measured as visible, so the ask still matches nothing.
3. The dark clips the creator asked for elsewhere (first clip, own sound) are removed.
4. Flag off drifts: something is measured, or a record / resolver input changes.
5. A measurement stored while the flag was on still reaches prompts after the kill switch.
6. A malformed or older stored block crashes the record or prints nonsense.
7. The measurement fails (unreadable file, no ffmpeg, timeout) and takes the clip
   analysis down with it.
8. A clip with very few keyframes (long GOP) is judged on one or two frames.
9. The measured words become groundable on-screen text.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.tasks.autoplace as ap
from app.agents._runtime import RunContext
from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents.clip_question import ClipQuestionAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.config import settings
from app.kria.clip_scope import apply_clip_exclusions
from app.schemas.clip_intents import ClipIntent, footage_lines
from app.services.clip_exposure import EXPOSURE_KEY, exposure_words, measure_exposure
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.clip_understanding import clip_record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "kri680_tigre_m1_clip_records.json").read_text())
# Any valid manifest shape; only its media list matters to the exclusion.
_MANIFEST_BASE = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())[
    "main_creator_input"
]["capability_manifest"]

BY_NAME = {c["filename"][:5]: c for c in CASE["clips"]}
JUNK = {"M1_04", "M1_08"}
DARK_BUT_ASKED = {"M1_02", "M1_11"}  # own sound / first clip
VERY_DARK = JUNK | DARK_BUT_ASKED
DARK_ASK = "Çok karanlık, hiçbir şeyin görünmediği iki klibi kullanma."


def _media(name: str) -> str:
    return BY_NAME[name]["media_id"]


def _analysis(clip: dict[str, Any]) -> dict[str, Any]:
    return {"understanding": copy.deepcopy(clip["understanding"]), EXPOSURE_KEY: clip["exposure"]}


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind="video",
            analysis=_analysis(c),
            gcs_path=f"users/u/clips/{c['media_id']}",
        )
        for c in CASE["clips"]
    ]


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "clip_exposure_fact_enabled", True)


@pytest.fixture(autouse=True)
def _no_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        raise AssertionError("a confident darkness match never pays for a vision check")

    monkeypatch.setattr(ClipQuestionAgent, "run", _run)


# -- the measured words on the real M1 footage (1, 2) ------------------------------------


def test_only_the_near_black_clips_read_very_dark() -> None:
    words = {name: clip_record(_analysis(c)).visibility for name, c in BY_NAME.items()}
    assert {n for n, w in words.items() if w.startswith("very dark")} == VERY_DARK
    # 1: every other night clip is called dark but visible, never "very dark".
    for name in set(words) - VERY_DARK:
        assert words[name].startswith("dark, low light but visible"), (name, words[name])
    assert words["M1_08"] == (
        "very dark, almost nothing visible "
        "(measured mean brightness 4/255, 95% of sampled frames near-black)"
    )


# -- the M1 turn: recorded clip records -> resolver input -> the edit's pick (3) ---------


def _stand_in_resolver(monkeypatch: pytest.MonkeyPatch) -> list[ClipRequestResolverInput]:
    """Stands in for the resolver model with the reading the record now allows: the dark
    ask matches the clips whose measured line says "very dark"; the two order asks match
    the clips the creator named by their spoken lines. No live model call."""
    seen: list[ClipRequestResolverInput] = []

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        seen.append(input)
        alias = {c.record.get("summary"): c.alias for c in input.clips}
        by_media = {name: alias[c["understanding"]["summary"]] for name, c in BY_NAME.items()}
        dark = [
            ResolverAssignment(media=c.alias, confidence=0.9, evidence=c.record["visibility"])
            for c in input.clips
            if str(c.record.get("visibility", "")).startswith("very dark")
        ]
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(intent_id="skip-dark", assignments=dark),
                ResolverIntentOut(
                    intent_id="first",
                    assignments=[ResolverAssignment(media=by_media["M1_11"], confidence=0.9)],
                ),
                ResolverIntentOut(
                    intent_id="last",
                    assignments=[ResolverAssignment(media=by_media["M1_03"], confidence=0.9)],
                ),
            ]
        )

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _run)
    return seen


INTENTS = [
    ClipIntent(
        intent_id="skip-dark",
        op="exclude",
        attribute="çok karanlık, hiçbir şeyin görünmediği klipler",
        source_quote="Çok karanlık, hiçbir şeyin görünmediği iki klibi kullanma",
    ),
    ClipIntent(
        intent_id="first",
        op="order",
        position="first",
        attribute="'Hadi gidelim' dediğimiz iskele klibi",
    ),
    ClipIntent(
        intent_id="last",
        op="order",
        position="last",
        attribute="'tam 20 dakika sürdü' dediğim klip",
    ),
]


def _manifest() -> ResolvedCreatorManifest:
    data = copy.deepcopy(_MANIFEST_BASE)
    data["media"] = [
        {"kind": "video", "label": None, "media_id": c["media_id"], "duration_s": c["duration_s"]}
        for c in CASE["clips"]
    ]
    return ResolvedCreatorManifest.model_validate(data)


# The M1 strategy shape: every clip, the boat clip and the arrival clip keep their sound.
STRATEGY = CreativeStrategy.model_validate(
    {
        "edit_format": "montage",
        "media_scope": "all",
        "montage_audio": {
            "source_media_ids": [_media("M1_02"), _media("M1_03")],
            "preserve_source_audio": True,
        },
    }
)


async def _resolve(monkeypatch: pytest.MonkeyPatch):
    seen = _stand_in_resolver(monkeypatch)
    resolution = await resolve_clip_intents_for_turn(
        intents=INTENTS,
        creator_request=CASE["creator_request"],
        clips=_clips(),
        run_context=RunContext(),
    )
    return seen, resolution


async def test_the_m1_dark_ask_leaves_out_exactly_the_two_junk_clips(monkeypatch) -> None:
    seen, resolution = await _resolve(monkeypatch)
    assert DARK_ASK in seen[0].creator_request
    lines = {c.alias: c.record.get("visibility", "") for c in seen[0].clips}
    assert all(lines.values()), "every measured clip carries its brightness line"

    [skip] = [i for i in resolution.intents if i.intent_id == "skip-dark"]
    assert (skip.op, skip.status) == ("exclude", "resolved")
    assert set(skip.media_ids()) == {_media(n) for n in VERY_DARK}

    narrowed = apply_clip_exclusions(_manifest(), STRATEGY, resolved_intents=resolution.intents)
    played = set(narrowed.selected_media_ids)
    # 3: the first clip (named by an order ask) and the boat clip (its own sound plays)
    # are as dark as the junk, and stay.
    assert {_media(n) for n in BY_NAME} - played == {_media(n) for n in JUNK}


async def test_flag_off_the_records_say_nothing_and_nothing_is_left_out(monkeypatch) -> None:
    # 4, 5: the same stored measurements, kill switch off.
    monkeypatch.setattr(settings, "clip_exposure_fact_enabled", False)
    seen, resolution = await _resolve(monkeypatch)
    assert all("visibility" not in c.record for c in seen[0].clips)
    [skip] = [i for i in resolution.intents if i.intent_id == "skip-dark"]
    assert skip.media_ids() == []
    narrowed = apply_clip_exclusions(_manifest(), STRATEGY, resolved_intents=resolution.intents)
    assert narrowed is STRATEGY


def test_flag_off_record_is_byte_identical(monkeypatch) -> None:
    clip = BY_NAME["M1_04"]
    plain = clip_record({"understanding": copy.deepcopy(clip["understanding"])})
    assert clip_record(_analysis(clip)).visibility
    monkeypatch.setattr(settings, "clip_exposure_fact_enabled", False)
    off = clip_record(_analysis(clip))
    assert off.model_dump() == plain.model_dump()
    assert "visibility" not in off.model_dump()
    assert off.prompt_view() == plain.prompt_view()


# -- stored blocks and the measurement itself (6, 7, 8, 9) -------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "dark",
        {},
        {"version": 99, "mean_luma": 4.0, "near_black_share": 1.0},
        {"version": 1, "mean_luma": "?", "near_black_share": 1.0},
        {"version": 1, "mean_luma": 400.0, "near_black_share": 1.0},
        {"version": 1, "mean_luma": 4.0, "near_black_share": -1},
    ],
)
def test_a_malformed_stored_block_is_unknown(raw: object) -> None:
    assert exposure_words(raw) == ""
    record = clip_record({"subject": "pier", EXPOSURE_KEY: raw})
    assert record.subject == "pier" and record.visibility == ""


def test_the_words_never_ground_on_screen_text() -> None:
    record = clip_record(_analysis(BY_NAME["M1_04"]))
    assert record.visibility
    assert not any("dark, almost" in line or "brightness" in line for line in footage_lines(record))


def _lavfi(path: Path, source: str, *, seconds: int = 6, gop: int = 15) -> str:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source,
            "-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", str(gop),
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return str(path)


def test_a_black_frame_with_one_small_light_is_very_dark(tmp_path: Path) -> None:
    path = _lavfi(
        tmp_path / "black.mp4",
        "color=c=black:s=360x640:r=30,drawbox=x=170:y=300:w=6:h=6:color=white:t=fill",
    )
    assert exposure_words(measure_exposure(path)).startswith("very dark")


def test_a_dim_but_lit_scene_is_dark_not_very_dark(tmp_path: Path) -> None:
    # 1: a dim frame with a lit stretch across it (a shoreline, a lit house).
    path = _lavfi(
        tmp_path / "dim.mp4",
        "color=c=0x101010:s=360x640:r=30,drawbox=x=0:y=280:w=360:h=40:color=0x909090:t=fill",
    )
    assert exposure_words(measure_exposure(path)).startswith("dark, low light but visible")


def test_a_long_gop_clip_is_sampled_over_its_frames(tmp_path: Path) -> None:
    # 8: one keyframe in 6 s; the fallback samples it instead of judging one frame.
    path = _lavfi(tmp_path / "gop.mp4", "color=c=gray:s=360x640:r=30", gop=600)
    measured = measure_exposure(path)
    assert measured is not None and measured["frames"] >= 8
    assert exposure_words(measured).startswith("normal brightness")


def test_an_unreadable_file_is_unknown_never_an_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    assert measure_exposure(str(bad)) is None
    assert measure_exposure(str(tmp_path / "missing.mp4")) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_the_clip_analysis_stores_the_measurement_only_when_on(
    monkeypatch, tmp_path: Path, enabled: bool
) -> None:
    # 4, 7: measured beside the analysis; off = not measured; a failed measurement keeps
    # the analysis.
    monkeypatch.setattr(settings, "clip_exposure_fact_enabled", enabled)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-key")
    path = _lavfi(tmp_path / "black.mp4", "color=c=black:s=360x640:r=30")
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
        lambda _path: SimpleNamespace(uri="provider://file", mime_type="video/mp4"),
    )
    meta = SimpleNamespace(
        failed=False, best_moments=[], detected_subject="night sky", transcript="", brands=[]
    )
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.analyze_clip", lambda *a, **k: meta)

    analysis, *_ = ap._analyze_video(path)
    assert analysis is not None
    assert (EXPOSURE_KEY in analysis) is enabled
    if enabled:
        assert clip_record(analysis).visibility.startswith("very dark")

    def _timeout(*_a: object, **_k: object) -> list[bytes]:
        raise subprocess.TimeoutExpired("ffmpeg", 30)

    monkeypatch.setattr("app.services.clip_exposure._ffmpeg_gray_frames", _timeout)
    analysis, *_ = ap._analyze_video(path)
    assert analysis is not None and EXPOSURE_KEY not in analysis
