"""Guard: every source-length limit is read from `source_length_policy` (KRI-1554).

Ways this could fail, written before the guard:

1. A compiler, gate or preflight compares a duration to a bare limit (``probe.duration_s > 300.0``,
   ``duration <= 1800``) -> `test_no_limit_literal_outside_the_policy`.
2. A module re-declares a limit as its own constant (``_MAX_SPEAKER_CLIP_S = 300.0``) -> same.
3. A keyword argument or a pydantic bound names one (``max_video_duration_s=90.0``,
   ``duration_s: float = Field(le=1800)``) -> same.
4. Creator copy states the limit in words ("under 5 minutes") instead of formatting the policy,
   so the sentence lies after the number changes -> same (`_COPY`).
5. The guard scans nothing: a guarded file is renamed or a glob matches no file, and the test
   passes vacuously -> `test_guard_reads_every_guarded_file`.
6. The guard misses a planted literal of one of the shapes above -> `test_planted_literal_fails`.
7. A reader keeps its own copy wired in, so one edited row does not change it ->
   `test_one_row_changes_every_talking_reader` patches the row and drives the upload contract,
   upload registration, the dispatch gate's check, the phone compiler and the analysis window.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import source_length_policy as policy

API_ROOT = Path(__file__).resolve().parents[1]

# The compilers, gates, preflights and registration paths that check a source's length.
GUARDED = (
    "app/kria/media_sources.py",
    "app/pipeline/phone_*_plan.py",
    "app/pipeline/phone_talking_inserts.py",
    "app/pipeline/slide_post/profiles.py",
    "app/pipeline/speech_cleanup_apply.py",
    "app/pipeline/talking_head_assembler.py",
    "app/routes/creation_threads.py",
    "app/services/active_narration_source.py",
    "app/services/guided_speech_cleanup.py",
    "app/services/media_library_ingest.py",
    "app/services/phone_speech_montage_job.py",
    "app/services/plan_item_media.py",
    "app/services/speech_cleanup_preflight.py",
    "app/tasks/content_plan_build.py",
    "app/tasks/generative_build.py",
    "app/tasks/kria_runtime.py",
    "app/tasks/speech_cleanup_analysis.py",
)

_LIMITS = frozenset(rule.max_duration_s for rule in policy.SOURCE_LENGTH_POLICY.values())
# Names that hold a source length (assignment targets and keyword arguments).
_LENGTH_NAME = re.compile(
    r"(duration|_clip_s$|speaker|insert_s$|window|spine|cap_max|cut_cap)", re.I
)
# The limit in words, in any of the guarded files' copy.
_COPY = re.compile(
    r"\b(?:\d+|one|two|three|five|ten|thirty|ninety)\s+(?:minutes?|seconds?)\b"
    r"(?=[^.]*\b(?:clip|video|voiceover|song|long|cap|under|up to|longer|shorter)\b)"
    r"|\b(?:under|up to|at most|longer than|capped at)\s+(?:\d+|five|ten|thirty)\s+minutes?\b",
    re.I,
)


def _is_limit(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and float(node.value) in _LIMITS
    )


def _names_length(node: ast.AST) -> bool:
    return "duration" in ast.unparse(node).lower()


def limit_literals(source: str) -> list[tuple[int, str]]:
    """Every place ``source`` writes a source-length limit itself."""

    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_is_limit(side) for side in sides) and any(
                not _is_limit(side) and _names_length(side) for side in sides
            ):
                found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [ast.unparse(target) for target in targets]
            if not any(_LENGTH_NAME.search(name) for name in names):
                continue
            values = [node.value] if node.value is not None else []
            if isinstance(node.value, ast.Call):
                values += [keyword.value for keyword in node.value.keywords]
            if any(_is_limit(value) for value in values):
                found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.keyword):
            if node.arg and _LENGTH_NAME.search(node.arg) and _is_limit(node.value):
                found.append((node.value.lineno, f"{node.arg}={ast.unparse(node.value)}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _COPY.search(node.value):
                found.append((node.lineno, repr(node.value[:90])))
    return found


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for pattern in GUARDED:
        matched = sorted(API_ROOT.glob(pattern))
        assert matched, f"guarded path {pattern!r} matches no file: update GUARDED"
        files.extend(matched)
    return files


def test_guard_reads_every_guarded_file() -> None:
    files = _guarded_files()
    assert len(files) >= len(GUARDED)
    # The policy itself is the one place the numbers live, so it is never guarded.
    assert API_ROOT / "app/services/source_length_policy.py" not in files


def test_no_limit_literal_outside_the_policy() -> None:
    offenders = [
        f"{path.relative_to(API_ROOT)}:{line}: {text}"
        for path in _guarded_files()
        for line, text in limit_literals(path.read_text())
    ]
    assert not offenders, (
        "Read source-length limits from app.services.source_length_policy:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "planted",
    [
        "if probe.duration_s > 300.0:\n    raise ValueError('x')\n",
        "ok = visual.duration_s is not None and visual.duration_s <= 1800\n",
        "_MAX_SPEAKER_CLIP_S = 300.0\n",
        "MAX_INSERT_S = 300\n",
        "MAX_SNAPSHOT_WINDOW_S = 300.0\n",
        "_SPINE_CUT_CAP_MAX_S = 300.0\n",
        "spec = Spec(max_video_duration_s=90.0)\n",
        "class D(BaseModel):\n    duration_s: float = Field(gt=0, le=1800)\n",
        "MESSAGE = 'Use a shorter clip (under 5 minutes) and try again.'\n",
        "MESSAGE = 'Songs must be 10 minutes or shorter'\n",
    ],
)
def test_planted_literal_fails(planted: str) -> None:
    assert limit_literals(planted), planted


def test_legitimate_numbers_pass() -> None:
    clean = (
        "time_limit = 1800\n"
        "AUDIO_APPLY_TIMEOUT_S = 300\n"
        "tail = str(cause)[-300:]\n"
        "if int(original.orientation_degrees) % 180 == 90:\n    pass\n"
        "if exceeds(probe.duration_s, 'talking_clip'):\n    pass\n"
        "MESSAGE = 'Kria finished in 5 minutes'\n"
    )
    assert limit_literals(clean) == []


def _talking_binding(duration_s: float):  # noqa: ANN202
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.services.phone_sources import PhoneSourceBinding

    return PhoneSourceBinding(
        media_id="talk",
        proxy_path="user/analysis-proxy-talk.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=duration_s,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def test_one_row_changes_every_talking_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lower the Talking row to 120 s: a 150 s clip is now refused by every reader."""

    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
    from app.routes import creation_threads as routes
    from app.services.active_narration_source import (
        ActiveNarrationRequest,
        RegisteredNarrationMedia,
        resolve_active_narration_source,
    )

    talking = policy.rule("talking_clip")
    window = policy.rule("speech_analysis_window")
    monkeypatch.setitem(
        policy.SOURCE_LENGTH_POLICY,
        "talking_clip",
        dataclasses.replace(talking, max_duration_s=120.0),
    )
    monkeypatch.setitem(
        policy.SOURCE_LENGTH_POLICY,
        "speech_analysis_window",
        dataclasses.replace(window, max_duration_s=120.0),
    )

    # The upload contract the iPhone checks at pick time.
    item = SimpleNamespace(
        edit_format="subtitled",
        audio_mode="original",
        voiceover_gcs_path=None,
        song_gcs_path=None,
    )
    contract = routes._media_capabilities(item=item, clip_count=0, visual_count=0)
    assert contract["clips"]["max_duration_s"] == 120.0
    # Upload registration (old builds) and the dispatch gate's check.
    broken = policy.exceeded_pick_rule(150.0, "subtitled", "clip")
    assert broken is not None and broken.key == "talking_clip"
    assert "2 minutes" in policy.too_long_message("subtitled", "clip")
    assert policy.exceeds(150.0, "talking_clip")
    # A montage clip of the same length is unaffected.
    assert policy.exceeded_pick_rule(150.0, "montage", "clip") is None

    # The phone Talking compiler.
    from app.pipeline import phone_subtitled_plan

    with pytest.raises(UnsupportedPhonePlan, match="2 minutes"):
        phone_subtitled_plan.compile_phone_subtitled_plan(
            (_talking_binding(150.0),),
            caption_cues=[{"text": "Hello everyone", "start_s": 0.0, "end_s": 1.5}],
        )

    # Speech cleanup: a 150 s Talking take is reported too long, never analysed in part.
    media = RegisteredNarrationMedia(
        media_id="talk",
        storage_path="users/u/talk.mp4",
        generation="1",
        media_kind="video",
        duration_s=150.0,
        has_audio=True,
    )
    resolution = resolve_active_narration_source(
        ActiveNarrationRequest(
            edit_format="subtitled",
            audio_mode="original",
            detector_policy="test",
            clips=(media,),
        )
    )
    assert resolution.source is None and resolution.reason == "source_too_long"


def test_a_long_voiceover_is_accepted_and_only_cleanup_is_limited() -> None:
    """Tadic/Yasin 2026-11-21: 5 minutes limits speech cleanup, not the voiceover upload. A creator
    may record longer and use a shorter part; the 240 s cap is on the finished edit."""

    from app.routes import creation_threads as routes
    from app.services.active_narration_source import (
        ActiveNarrationRequest,
        RegisteredNarrationMedia,
        resolve_active_narration_source,
    )

    six_minutes = 360.0
    for edit_format in ("narrated_planned", "subtitled", "montage"):
        assert policy.exceeded_pick_rule(six_minutes, edit_format, "voiceover") is None
        assert policy.pick_limit_s(edit_format, "voiceover") == policy.library_max_s()
    item = SimpleNamespace(
        edit_format="narrated_planned",
        audio_mode="voiceover",
        voiceover_gcs_path=None,
        song_gcs_path=None,
    )
    contract = routes._media_capabilities(item=item, clip_count=0, visual_count=0)
    assert contract["voiceover"]["max_duration_s"] == policy.library_max_s()

    voiceover = RegisteredNarrationMedia(
        media_id="vo",
        storage_path="users/u/vo.m4a",
        generation="1",
        media_kind="audio",
        duration_s=six_minutes,
        has_audio=True,
    )
    resolution = resolve_active_narration_source(
        ActiveNarrationRequest(
            edit_format="narrated_planned",
            audio_mode="voiceover",
            detector_policy="test",
            voiceover=voiceover,
        )
    )
    assert resolution.source is None and resolution.reason == "source_too_long"
