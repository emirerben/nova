"""Clean up speech for guided narrated stories: the pinned cleaned derivative.

The fixture is a synthetic preflight snapshot with the structure of a real
48.6 s narrated voiceover (9 keep segments, 41.59 s cleaned). Audio is a
generated tone so the FFmpeg cut, ffprobe and storage steps run for real.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import storage
from app.config import settings
from app.pipeline.speech_cleanup_apply import cut_fingerprint, hydrate_speech_cleanup_snapshot
from app.schemas.edit_proposal import NarrationSpeechCleanup, NarrationTrack
from app.services.guided_speech_cleanup import (
    GuidedSpeechCleanupError,
    build_cleaned_narration,
    derivative_item_prefix,
    derivative_path_ok,
    load_clean_consent_snapshot,
    require_guided_cleanup_binding,
    reusable_cleaned_narration,
    source_identity,
)
from app.services.speech_cleanup import SpeechCleanupFailure

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "speech_cleanup"
    / "guided_voiceover_mixed_gap_v2.json"
)


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def fixture_ids(fixture: dict) -> tuple[uuid.UUID, uuid.UUID, str]:
    return (
        uuid.UUID(fixture["owner_id"]),
        uuid.UUID(fixture["plan_item_id"]),
        fixture["snapshot"]["analysis_id"],
    )


def raw_narration(fixture: dict) -> NarrationTrack:
    source = fixture["snapshot"]["source"]
    return NarrationTrack(
        gcs_path=source["storage_path"],
        generation=source["generation"],
        duration_s=source["window_end_s"],
        caption_style="sentence",
    )


def guided_voiceover_item(fixture: dict, **overrides) -> SimpleNamespace:  # noqa: ANN003
    raw = raw_narration(fixture)
    values = {
        "id": uuid.UUID(fixture["plan_item_id"]),
        "edit_format": "narrated",
        "audio_mode": "voiceover",
        "voiceover_gcs_path": raw.gcs_path,
        "voiceover_generation": raw.generation,
        "voiceover_duration_s": raw.duration_s,
        "clip_assignments": [],
        "clip_gcs_paths": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def analysis_row(item: SimpleNamespace, fixture: dict, **overrides) -> SimpleNamespace:  # noqa: ANN003
    """A current, ready preflight row for ``item``'s active voiceover source."""

    from app.services.plan_item_media import current_detector_policy, resolve_item_narration
    from app.services.speech_cleanup_preflight import (
        SPEECH_CLEANUP_ENGINE_VERSION,
        SPEECH_CLEANUP_PAYLOAD_VERSION,
    )
    from app.services.speech_cleanup_selection import DETECTOR_VERSION

    source = resolve_item_narration(item, detector_policy=current_detector_policy()).source
    assert source is not None
    analysis = copy.deepcopy(fixture["snapshot"]["analysis"])
    # The payload binds to the fingerprint of the source it analyzed.
    analysis["source_fingerprint"] = source.source_policy_fingerprint
    values = {
        "id": uuid.UUID(fixture["snapshot"]["analysis_id"]),
        "plan_item_id": item.id,
        "source_kind": source.source_kind,
        "source_media_identity": source.media_id,
        "source_storage_path": source.storage_path,
        "source_generation": source.generation,
        "window_start_s": source.window_start_s,
        "window_end_s": source.window_end_s,
        "source_policy_fingerprint": source.source_policy_fingerprint,
        "engine_version": SPEECH_CLEANUP_ENGINE_VERSION,
        "detector_version": DETECTOR_VERSION,
        "analysis_payload_version": SPEECH_CLEANUP_PAYLOAD_VERSION,
        "status": "ready",
        "superseded_at": None,
        "candidate_count": fixture["expected"]["removal_count"],
        "analysis_payload": analysis,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RowDb:
    """The one ``db.get`` the consent loader performs."""

    def __init__(self, row: SimpleNamespace | None) -> None:
        self.row = row
        self.gets: list[dict] = []

    def get(self, _model, identifier, **kwargs):  # noqa: ANN001, ANN003
        self.gets.append({"id": identifier, **kwargs})
        if self.row is not None and self.row.id == identifier:
            return self.row
        return None


def write_tone_voiceover(path: Path, *, duration_s: float, generation: str) -> None:
    """A real AAC voiceover with a pinned local-storage generation (mtime ns)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration_s}",
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ],
        check=True,
    )
    os.utime(path, ns=(int(generation), int(generation)))


@pytest.fixture
def local_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "bucket"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(root))
    return root


@pytest.fixture
def cleanup_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "guided_voiceover_speech_cleanup_enabled", True)
    monkeypatch.setattr(settings, "silence_cut_enabled", True)


def test_fixture_is_the_mixed_gap_structure_of_the_reported_voiceover() -> None:
    fixture = load_fixture()
    snapshot = hydrate_speech_cleanup_snapshot(fixture["snapshot"])
    keep = snapshot.cut_plan.keep_segments

    assert snapshot.detector_version == "mixed-gap-v2"
    assert len(keep) == fixture["expected"]["keep_segment_count"] == 9
    assert sum(end - start for start, end in keep) == pytest.approx(41.59, abs=1e-3)
    assert len(snapshot.transcript(apply_cut=True).words) == 91


def test_cut_fingerprint_is_stable_and_binds_source_and_cut() -> None:
    fixture = load_fixture()
    snapshot = hydrate_speech_cleanup_snapshot(fixture["snapshot"])
    fingerprint = cut_fingerprint(snapshot)
    round_tripped = hydrate_speech_cleanup_snapshot(json.loads(json.dumps(fixture["snapshot"])))

    assert fingerprint == cut_fingerprint(round_tripped)
    assert len(fingerprint) == 64
    other_generation = copy.deepcopy(fixture["snapshot"])
    other_generation["source"]["generation"] = "1700000000000002"
    assert cut_fingerprint(hydrate_speech_cleanup_snapshot(other_generation)) != fingerprint
    keep = list(snapshot.cut_plan.keep_segments)
    stub = SimpleNamespace(
        storage_path=snapshot.storage_path,
        generation=snapshot.generation,
        window_start_s=snapshot.window_start_s,
        window_end_s=snapshot.window_end_s,
        cut_plan=SimpleNamespace(keep_segments=keep),
    )
    assert cut_fingerprint(stub) == fingerprint
    # Sub-microsecond float noise is not a different cut; a moved boundary is.
    stub.cut_plan.keep_segments = [(start + 1e-9, end) for start, end in keep]
    assert cut_fingerprint(stub) == fingerprint
    stub.cut_plan.keep_segments = [(keep[0][0] + 0.01, keep[0][1]), *keep[1:]]
    assert cut_fingerprint(stub) != fingerprint


def test_derivative_path_is_scoped_to_owner_item_and_analysis() -> None:
    owner, item, analysis = fixture_ids(load_fixture())
    good = f"users/{owner}/plan/{item}/speech-cleanup/{analysis}/{'0a' * 16}.wav"

    assert derivative_path_ok(good, owner_id=owner, item_id=item, analysis_id=analysis)
    assert derivative_path_ok(good, owner_id=str(owner), item_id=str(item), analysis_id=analysis)
    for path in (
        good.replace(str(owner), str(uuid.uuid4())),
        good.replace(str(item), str(uuid.uuid4())),
        good.replace(analysis, str(uuid.uuid4())),
        good.replace(".wav", ".wav/x.wav"),
        good.replace("0a" * 16, "0A" * 16),
        f"users/{owner}/plan/{item}/speech-cleanup/{analysis}/../{'0a' * 16}.wav",
        f"users/{owner}/plan/{item}/voiceover.m4a",
    ):
        assert not derivative_path_ok(path, owner_id=owner, item_id=item, analysis_id=analysis)
    assert not derivative_path_ok(good, owner_id=None, item_id=item, analysis_id=analysis)
    assert not derivative_path_ok(good, owner_id="../x", item_id=item, analysis_id=analysis)


def test_source_identity_reads_provenance_or_the_narration_itself() -> None:
    raw = {"gcs_path": "users/u/v.m4a", "generation": "7", "duration_s": 4.5}
    cleaned = {
        "gcs_path": "users/u/plan/i/speech-cleanup/a/b.wav",
        "generation": "9",
        "duration_s": 3.0,
        "speech_cleanup": {
            "analysis_id": "0f0f0f0f-3333-4333-8333-333333333333",
            "source_gcs_path": "users/u/v.m4a",
            "source_generation": "7",
            "source_duration_s": 4.5,
            "cut_sha256": "c" * 64,
        },
    }

    assert source_identity(raw) == ("users/u/v.m4a", "7", 4.5)
    assert source_identity(cleaned) == ("users/u/v.m4a", "7", 4.5)
    assert source_identity(NarrationTrack.model_validate(cleaned)) == ("users/u/v.m4a", "7", 4.5)
    assert source_identity({**cleaned, "speech_cleanup": {"analysis_id": "x"}}) == ("", "", 0.0)
    assert source_identity({**raw, "duration_s": "nan?"}) == ("", "", 0.0)


def test_build_cleaned_narration_pins_a_content_addressed_wav(local_storage: Path) -> None:
    fixture = load_fixture()
    owner, item, analysis = fixture_ids(fixture)
    raw = raw_narration(fixture)
    write_tone_voiceover(
        local_storage / raw.gcs_path, duration_s=raw.duration_s, generation=raw.generation
    )

    cleaned = build_cleaned_narration(raw, fixture["snapshot"], owner_id=owner, item_id=item)

    assert derivative_path_ok(cleaned.gcs_path, owner_id=owner, item_id=item, analysis_id=analysis)
    stored = local_storage / cleaned.gcs_path
    assert stored.read_bytes()[:4] == b"RIFF"
    # No INFO chunk (encoder stamp, source tags) can change the
    # content-addressed name of the same cut on the same host.
    assert b"LIST" not in stored.read_bytes()[:4096]
    assert cleaned.generation == str(stored.stat().st_mtime_ns)
    assert cleaned.duration_s == pytest.approx(fixture["expected"]["cleaned_duration_s"], abs=0.05)
    assert len(cleaned.words) == fixture["expected"]["cleaned_word_count"]
    assert cleaned.words[-1].end_s == pytest.approx(
        fixture["expected"]["cleaned_last_word_end_s"], abs=1e-3
    )
    assert cleaned.words[-1].end_s <= cleaned.duration_s
    assert cleaned.language == "en"
    assert cleaned.caption_style == "sentence"
    assert cleaned.speech_cleanup == NarrationSpeechCleanup(
        analysis_id=analysis,
        source_gcs_path=raw.gcs_path,
        source_generation=raw.generation,
        source_duration_s=raw.duration_s,
        cut_sha256=cut_fingerprint(hydrate_speech_cleanup_snapshot(fixture["snapshot"])),
    )
    # A re-driven attempt resolves to the same immutable object.
    assert (
        build_cleaned_narration(raw, fixture["snapshot"], owner_id=owner, item_id=item) == cleaned
    )
    assert len(list(stored.parent.iterdir())) == 1


@pytest.mark.parametrize(
    "case",
    [
        "match",
        "no_provenance",
        "other_owner",
        "other_analysis",
        "other_cut",
        "raw_replaced",
        "object_replaced",
        "object_missing",
    ],
)
def test_redrive_reuses_only_its_own_still_stored_derivative(
    local_storage: Path, case: str
) -> None:
    """A retry keeps its pinned WAV instead of a re-cut that may hash differently."""
    fixture = load_fixture()
    owner, item, _analysis = fixture_ids(fixture)
    raw = raw_narration(fixture)
    write_tone_voiceover(
        local_storage / raw.gcs_path, duration_s=raw.duration_s, generation=raw.generation
    )
    cleaned = build_cleaned_narration(raw, fixture["snapshot"], owner_id=owner, item_id=item)
    saved, owner_id = cleaned, owner
    provenance = cleaned.speech_cleanup
    if case == "no_provenance":
        saved = cleaned.model_copy(update={"speech_cleanup": None})
    elif case == "other_owner":
        owner_id = uuid.uuid4()
    elif case == "other_analysis":
        other = provenance.model_copy(update={"analysis_id": str(uuid.uuid4())})
        saved = cleaned.model_copy(update={"speech_cleanup": other})
    elif case == "other_cut":
        other = provenance.model_copy(update={"cut_sha256": "0" * 64})
        saved = cleaned.model_copy(update={"speech_cleanup": other})
    elif case == "raw_replaced":
        raw = raw.model_copy(update={"generation": "999"})
    elif case == "object_replaced":
        os.utime(local_storage / cleaned.gcs_path, ns=(1, 1))
    elif case == "object_missing":
        (local_storage / cleaned.gcs_path).unlink()

    reused = reusable_cleaned_narration(
        saved, raw, fixture["snapshot"], owner_id=owner_id, item_id=item
    )

    assert reused == (cleaned if case == "match" else None)


@pytest.mark.parametrize(
    ("case", "code", "retryable"),
    [
        ("replaced_raw_generation", "speech_cleanup_changed", False),
        ("snapshot_for_another_file", "speech_cleanup_changed", False),
        ("snapshot_for_another_generation", "speech_cleanup_changed", False),
        ("window_shorter_than_recording", "speech_cleanup_changed", False),
        ("storage_unavailable", "speech_cleanup_unavailable", True),
        ("download_failed", "speech_cleanup_unavailable", True),
        ("ffmpeg_killed", "speech_cleanup_unavailable", True),
        ("upload_failed", "speech_cleanup_unavailable", True),
    ],
)
def test_build_cleaned_narration_fails_closed_with_typed_codes(
    monkeypatch: pytest.MonkeyPatch,
    local_storage: Path,
    case: str,
    code: str,
    retryable: bool,
) -> None:
    fixture = load_fixture()
    owner, item, _analysis = fixture_ids(fixture)
    raw = raw_narration(fixture)
    payload = copy.deepcopy(fixture["snapshot"])
    write_tone_voiceover(
        local_storage / raw.gcs_path, duration_s=raw.duration_s, generation=raw.generation
    )
    if case == "replaced_raw_generation":
        os.utime(local_storage / raw.gcs_path, ns=(1, 1))
    elif case == "snapshot_for_another_file":
        payload["source"]["storage_path"] = raw.gcs_path.replace("voiceover", "other")
    elif case == "snapshot_for_another_generation":
        payload["source"]["generation"] = "1700000000000002"
    elif case == "window_shorter_than_recording":
        raw = raw.model_copy(update={"duration_s": raw.duration_s + 30})
    elif case == "storage_unavailable":
        monkeypatch.setattr(
            storage, "object_metadata", MagicMock(side_effect=ConnectionError("down"))
        )
    elif case == "download_failed":
        monkeypatch.setattr(
            storage, "download_generation_to_file", MagicMock(side_effect=OSError("disk"))
        )
    elif case == "ffmpeg_killed":
        # A non-zero exit (OOM kill, full disk) of an already-validated cut is
        # resource trouble, not "the pauses changed".
        monkeypatch.setattr(
            "app.pipeline.speech_cleanup_apply.subprocess.run",
            MagicMock(return_value=subprocess.CompletedProcess([], -9, b"", b"")),
        )
    else:
        monkeypatch.setattr(
            storage, "upload_local_file_immutable", MagicMock(side_effect=ConnectionError("x"))
        )

    with pytest.raises(GuidedSpeechCleanupError) as failure:
        build_cleaned_narration(raw, payload, owner_id=owner, item_id=item)

    assert (failure.value.code, failure.value.retryable) == (code, retryable)
    assert not (local_storage / f"users/{owner}/plan/{item}/speech-cleanup").exists()


def test_consent_snapshot_is_the_exact_current_whole_voiceover_analysis(cleanup_enabled) -> None:
    from app.services.speech_cleanup_preflight import analysis_snapshot

    fixture = load_fixture()
    item = guided_voiceover_item(fixture)
    row = analysis_row(item, fixture)
    db = RowDb(row)

    payload = load_clean_consent_snapshot(db, item, fixture["snapshot"]["analysis_id"])

    assert payload == analysis_snapshot(row)
    assert db.gets == [{"id": row.id, "populate_existing": True}]
    snapshot = hydrate_speech_cleanup_snapshot(payload)
    assert snapshot.storage_path == item.voiceover_gcs_path
    assert snapshot.generation == item.voiceover_generation


@pytest.mark.parametrize(
    ("case", "code", "message"),
    [
        ("flag_off", "speech_cleanup_disabled", "isn't available"),
        ("silence_cut_off", "speech_cleanup_disabled", "isn't available"),
        ("missing_row", "speech_cleanup_changed", "pauses changed"),
        ("other_item", "speech_cleanup_changed", "pauses changed"),
        ("superseded", "speech_cleanup_changed", "pauses changed"),
        ("not_ready", "speech_cleanup_changed", "pauses changed"),
        ("no_candidates", "speech_cleanup_changed", "pauses changed"),
        ("embedded_source", "speech_cleanup_changed", "pauses changed"),
        ("voiceover_replaced", "speech_cleanup_changed", "pauses changed"),
        ("fingerprint_drift", "speech_cleanup_changed", "pauses changed"),
        ("capped_window", "speech_cleanup_unavailable", "up to 5 minutes"),
    ],
)
def test_consent_snapshot_fails_closed_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_enabled,
    case: str,
    code: str,
    message: str,
) -> None:
    fixture = load_fixture()
    item = guided_voiceover_item(fixture)
    row = analysis_row(item, fixture)
    if case == "flag_off":
        monkeypatch.setattr(settings, "guided_voiceover_speech_cleanup_enabled", False)
    elif case == "silence_cut_off":
        monkeypatch.setattr(settings, "silence_cut_enabled", False)
    elif case == "missing_row":
        row = None
    elif case == "other_item":
        row.plan_item_id = uuid.uuid4()
    elif case == "superseded":
        row.superseded_at = object()
    elif case == "not_ready":
        row.status = "running"
    elif case == "no_candidates":
        row.candidate_count = 0
    elif case == "embedded_source":
        row.source_kind = "embedded_spine"
    elif case == "voiceover_replaced":
        item.voiceover_generation = "1700000000000009"
    elif case == "fingerprint_drift":
        row.source_policy_fingerprint = "d" * 64
    else:
        # A 400 s recording is analyzed only in its first 300 s window.
        item = guided_voiceover_item(fixture, voiceover_duration_s=400.0)
        row = analysis_row(item, fixture)

    with pytest.raises(GuidedSpeechCleanupError) as failure:
        load_clean_consent_snapshot(RowDb(row), item, fixture["snapshot"]["analysis_id"])

    assert failure.value.code == code
    assert failure.value.retryable is False
    assert message in failure.value.message


def _job_plan(fixture: dict, contract: str = "required_v1") -> dict:
    return {
        "speech_cleanup_contract": contract,
        "speech_cleanup_preflight_contract": "snapshot_v1",
        "_speech_cleanup_internal": {"preflight_snapshot": copy.deepcopy(fixture["snapshot"])},
    }


def _provenance_narration(fixture: dict, **provenance) -> dict:  # noqa: ANN003
    owner, item, analysis = fixture_ids(fixture)
    source = fixture["snapshot"]["source"]
    return {
        "gcs_path": f"users/{owner}/plan/{item}/speech-cleanup/{analysis}/{'0a' * 16}.wav",
        "generation": "5",
        "duration_s": 41.59,
        "speech_cleanup": {
            "analysis_id": analysis,
            "source_gcs_path": source["storage_path"],
            "source_generation": source["generation"],
            "source_duration_s": source["window_end_s"],
            "cut_sha256": cut_fingerprint(hydrate_speech_cleanup_snapshot(fixture["snapshot"])),
            **provenance,
        },
    }


def test_guided_cleanup_binding_returns_the_applied_outcome_context() -> None:
    fixture = load_fixture()

    context = require_guided_cleanup_binding(_job_plan(fixture), _provenance_narration(fixture))

    assert context["analysis_attempt_id"] == fixture["snapshot"]["analysis_id"]
    assert context["analysis_view"] == "full_clip"
    assert context["output_removal_count"] == fixture["expected"]["removal_count"]
    typed = NarrationTrack.model_validate(_provenance_narration(fixture))
    assert require_guided_cleanup_binding(_job_plan(fixture), typed) == context
    raw = {"gcs_path": "users/u/v.m4a", "generation": "1", "duration_s": 3.0}
    assert require_guided_cleanup_binding(_job_plan(fixture, "off_v1"), raw) is None
    assert require_guided_cleanup_binding({}, raw) is None
    # A markerless required_v1 Job (legacy item toggle, no consented CutPlan)
    # keeps the raw narration it always rendered.
    assert require_guided_cleanup_binding({"speech_cleanup_contract": "required_v1"}, raw) is None


def test_derivative_item_prefix_holds_every_derivative_of_the_item() -> None:
    fixture = load_fixture()
    owner, item, analysis = fixture_ids(fixture)
    prefix = derivative_item_prefix(owner_id=owner, item_id=item)
    assert prefix == f"users/{owner}/plan/{item}/speech-cleanup/"
    assert _provenance_narration(fixture)["gcs_path"].startswith(prefix)
    with pytest.raises(ValueError):
        derivative_item_prefix(owner_id="../x", item_id=item)


@pytest.mark.parametrize(
    "case",
    ["raw_narration", "other_analysis", "other_cut", "other_generation", "off_v1", "no_snapshot"],
)
def test_guided_cleanup_binding_refuses_mismatched_or_uncut_audio(case: str) -> None:
    fixture = load_fixture()
    plan = _job_plan(fixture)
    narration = _provenance_narration(fixture)
    if case == "raw_narration":
        narration = {"gcs_path": "users/u/v.m4a", "generation": "1", "duration_s": 48.6}
    elif case == "other_analysis":
        narration = _provenance_narration(fixture, analysis_id=str(uuid.uuid4()))
    elif case == "other_cut":
        narration = _provenance_narration(fixture, cut_sha256="e" * 64)
    elif case == "other_generation":
        narration = _provenance_narration(fixture, source_generation="1700000000000002")
    elif case == "off_v1":
        plan = _job_plan(fixture, "off_v1")
    else:
        plan.pop("_speech_cleanup_internal")

    with pytest.raises(SpeechCleanupFailure) as failure:
        require_guided_cleanup_binding(plan, narration)

    assert failure.value.reason == "snapshot_mismatch"


def test_immutable_upload_never_overwrites_and_verifies_an_existing_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "storage_provider", "gcs")
    local = tmp_path / "cleaned.wav"
    local.write_bytes(b"RIFF" + b"\0" * 20)
    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    bucket.get_blob.return_value = MagicMock(
        generation=77, etag="e", size=24, content_type="audio/wav", md5_hash=None
    )
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        created = storage.upload_local_file_immutable(str(local), "users/u/x.wav", "audio/wav")
        blob.upload_from_filename.side_effect = storage.PreconditionFailed("exists")
        reused = storage.upload_local_file_immutable(str(local), "users/u/x.wav", "audio/wav")
        bucket.get_blob.return_value.size = 23
        with pytest.raises(ValueError, match="size"):
            storage.upload_local_file_immutable(str(local), "users/u/x.wav", "audio/wav")

    blob.upload_from_filename.assert_called_with(
        str(local), content_type="audio/wav", if_generation_match=0
    )
    assert created.generation == reused.generation == "77"
