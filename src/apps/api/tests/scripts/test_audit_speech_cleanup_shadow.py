"""Focused safety tests for the local speech-cleanup shadow audition tool."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_speech_cleanup_shadow.py"
spec = importlib.util.spec_from_file_location("audit_speech_cleanup_shadow", _SCRIPT_PATH)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_speech_cleanup_shadow"] = audit
spec.loader.exec_module(audit)

JOB_ID = "00000000-0000-4000-8000-000000000042"
SOURCE_ID = "00000000-0000-4000-8000-000000000007"
SOURCE_PATH = f"generative-jobs/{JOB_ID}/sources/copy-attempts/attempt-x/slot-0000.mp4"


def _source_tag(source_id: str = SOURCE_ID) -> str:
    fingerprint = audit.speech_cleanup_rollout_fingerprint(JOB_ID, source_id)
    return audit.speech_cleanup_source_tag(fingerprint)


def _receipt(**overrides):
    data = {
        "schema_version": 1,
        "detector_version": "mixed-gap-v1",
        "analysis_attempt_id": "attempt-a",
        "analysis_view": "full_clip",
        "assignment_status": "assigned",
        "source_tag": _source_tag(),
        "candidate_status": "ready",
        "effective_mode": "shadow",
        "duration_ms": 10_000,
        "inputs": {
            "asr_word_spans_omitted": 0,
            "silence_spans_omitted": 0,
            "lexical_candidates_omitted": 0,
        },
        "mixed_gap_scan": {
            "records": [
                {
                    "window_start_ms": 6200,
                    "window_end_ms": 8400,
                    "island_start_ms": 7406,
                    "island_end_ms": 7978,
                    "detection": "eligible",
                    "reason": "bilateral_silence",
                    "plan_disposition": "selected_full",
                }
            ],
            "records_omitted": 0,
        },
        "baseline_plan": {"removed_spans_omitted": 0},
        "candidate_plan": {"removed_spans_omitted": 0},
    }
    data.update(overrides)
    return {
        "ts": "2026-09-01T10:00:00Z",
        "stage": "silence_cut",
        "event": "silence_cut_mixed_gap_analysis",
        "data": data,
    }


def _payload(*events, path=SOURCE_PATH, source_ids=None, job_id=JOB_ID):
    return {
        "job": {
            "id": job_id,
            "user_id": "00000000-0000-4000-8000-000000000099",
            "content_plan_item_id": "00000000-0000-4000-8000-000000000098",
            "mode": "generative",
            "assembly_plan": {},
            "all_candidates": {
                "clip_paths": [path],
                "clip_source_instance_ids": source_ids or [SOURCE_ID],
            },
            "pipeline_trace": list(events or [_receipt()]),
        }
    }


def test_auth_requires_configured_nonempty_key_before_prompt():
    prompt = Mock(side_effect=AssertionError("must not prompt without configured auth"))
    with pytest.raises(audit.AuditRefusal, match="not configured"):
        audit._authenticate_admin(prompt=prompt, configured_key="")
    prompt.assert_not_called()


def test_auth_uses_protected_prompt_and_constant_time_compare(monkeypatch):
    compared = Mock(return_value=True)
    monkeypatch.setattr(audit.secrets, "compare_digest", compared)
    prompt = Mock(return_value="operator-secret")

    assert (
        audit._authenticate_admin(prompt=prompt, configured_key="operator-secret")
        == "operator-secret"
    )
    prompt.assert_called_once_with("Admin API key: ")
    compared.assert_called_once_with(b"operator-secret", b"operator-secret")


def test_auth_rejects_wrong_hidden_credential():
    with pytest.raises(audit.AuditRefusal, match="credential rejected"):
        audit._authenticate_admin(
            prompt=lambda _label: "wrong",
            configured_key="operator-secret",
        )


def test_cli_has_no_credential_argument():
    with pytest.raises(SystemExit):
        audit._parse_args(["--job-id", JOB_ID, "--admin-key", "leak"])


@pytest.mark.parametrize(
    "value",
    [
        "https://nova-video.fly.dev",
        "https://nova-video.fly.dev:443/",
    ],
)
def test_admin_api_url_allows_only_production_https(value):
    assert audit._validated_base_url(value) == audit.PROD_BASE_URL


@pytest.mark.parametrize(
    "value",
    [
        "https://attacker.example",
        "http://nova-video.fly.dev",
        "https://nova-video.fly.dev.evil.test",
        "https://token@nova-video.fly.dev",
        "https://nova-video.fly.dev/path",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://localhost:8443",
        "http://localhost:bad-port",
    ],
)
def test_admin_api_url_rejects_credential_exfiltration_targets(value):
    with pytest.raises(audit.AuditRefusal, match="base URL"):
        audit._validated_base_url(value)


def test_resolves_receipt_tag_to_exact_current_instance_not_display_slot():
    exists = Mock(return_value=True)
    resolved = audit.resolve_audition(
        _payload(),
        requested_job_id=JOB_ID,
        timeline_editor_enabled=True,
        object_exists=exists,
    )

    assert resolved.object_path == SOURCE_PATH
    assert resolved.source_tag == _source_tag()
    assert resolved.windows == (
        audit.AuditionWindow(
            6200,
            8400,
            7406,
            7978,
            "bilateral_silence",
            "selected_full",
        ),
    )
    exists.assert_called_once_with(SOURCE_PATH)


def test_audition_parser_accepts_the_real_mixed_gap_receipt_contract():
    from app.pipeline.silence_cut import build_cut_plan_comparison
    from app.services.speech_cleanup_identity import SpeechCleanupAssignment
    from app.services.speech_cleanup_selection import (
        build_mixed_gap_receipt,
        select_mixed_gap_mode,
    )

    words = [
        SimpleNamespace(text="first", start_s=2.040, end_s=2.530),
        SimpleNamespace(text="second", start_s=3.255, end_s=4.359),
        SimpleNamespace(text="third", start_s=8.293, end_s=9.060),
    ]
    silences = [
        (0.0, 1.215),
        (1.758, 1.875),
        (2.529, 3.255),
        (4.359, 5.779),
        (6.210, 7.406),
        (7.978, 8.293),
        (9.678, 10.0),
    ]
    comparison = build_cut_plan_comparison(words, silences, 10.0)
    fingerprint = audit.speech_cleanup_rollout_fingerprint(JOB_ID, SOURCE_ID)
    selection = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="shadow",
        rollout_percent=0,
        assignment=SpeechCleanupAssignment(0, fingerprint, "assigned"),
    )
    receipt = build_mixed_gap_receipt(
        selection=selection,
        analysis_attempt_id="attempt-contract",
        analysis_view="full_clip",
        analysis_policy="required_v1",
        candidate_status="ready",
        silence_detection_status="ok",
        duration_s=10.0,
        words=words,
        silence_spans=silences,
        baseline_plan=comparison.baseline,
        candidate_plan=comparison.candidate,
        selected_plan="baseline",
    )

    windows = audit._parse_audition_windows(receipt)
    assert any(
        window.island_start_ms == 7406 and window.island_end_ms == 7978 for window in windows
    )


def test_refuses_payload_for_another_job():
    with pytest.raises(audit.AuditRefusal, match="does not match"):
        audit.resolve_audition(
            _payload(job_id="00000000-0000-4000-8000-000000000043"),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
        )


def test_refuses_when_durable_copy_flag_is_disabled_without_storage_io():
    exists = Mock(side_effect=AssertionError("storage must not be touched"))
    with pytest.raises(audit.AuditRefusal, match="copying is disabled"):
        audit.resolve_audition(
            _payload(),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=False,
            object_exists=exists,
        )
    exists.assert_not_called()


def test_refuses_missing_durable_object():
    with pytest.raises(audit.AuditRefusal, match="no longer exists"):
        audit.resolve_audition(
            _payload(),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
            object_exists=lambda _path: False,
        )


def test_refuses_removed_or_replaced_source_instead_of_using_slot_zero():
    replacement_id = "00000000-0000-4000-8000-000000000008"
    with pytest.raises(audit.AuditRefusal, match="removed, replaced"):
        audit.resolve_audition(
            _payload(source_ids=[replacement_id]),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
            object_exists=lambda _path: True,
        )


def test_refuses_duplicate_or_malformed_source_vector_before_storage():
    payload = _payload()
    payload["job"]["all_candidates"] = {
        "clip_paths": [SOURCE_PATH, SOURCE_PATH.replace("slot-0000", "slot-0001")],
        "clip_source_instance_ids": [SOURCE_ID, SOURCE_ID],
    }
    exists = Mock(side_effect=AssertionError("storage must not be touched"))
    with pytest.raises(audit.AuditRefusal, match="duplicate_source_instance"):
        audit.resolve_audition(
            payload,
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
            object_exists=exists,
        )
    exists.assert_not_called()


def test_refuses_non_owned_or_non_durable_current_path():
    with pytest.raises(audit.AuditRefusal, match="not an owned durable object"):
        audit.resolve_audition(
            _payload(path="slot-uploads/user/clip.mp4"),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
            object_exists=lambda _path: True,
        )


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (_receipt(mixed_gap_scan=None), "records are missing"),
        (
            _receipt(
                mixed_gap_scan={
                    "records": [],
                    "records_omitted": 1,
                }
            ),
            "timing arrays are truncated",
        ),
    ],
)
def test_refuses_missing_or_truncated_receipts(event, message):
    with pytest.raises(audit.AuditRefusal, match=message):
        audit.resolve_audition(
            _payload(event),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
            object_exists=lambda _path: True,
        )


def test_refuses_ambiguous_receipt_without_exact_selection():
    second = _receipt(analysis_attempt_id="attempt-b")
    with pytest.raises(audit.AuditRefusal, match="selection is ambiguous"):
        audit.resolve_audition(
            _payload(_receipt(), second),
            requested_job_id=JOB_ID,
            timeline_editor_enabled=True,
            object_exists=lambda _path: True,
        )

    resolved = audit.resolve_audition(
        _payload(_receipt(), second),
        requested_job_id=JOB_ID,
        attempt_id="attempt-b",
        source_tag=_source_tag(),
        timeline_editor_enabled=True,
        object_exists=lambda _path: True,
    )
    assert resolved.attempt_id == "attempt-b"


def test_temp_media_is_deleted_and_logs_only_timing_receipt(monkeypatch, capsys):
    resolved = audit.resolve_audition(
        _payload(),
        requested_job_id=JOB_ID,
        timeline_editor_enabled=True,
        object_exists=lambda _path: True,
    )
    captured_temp_dir: Path | None = None

    def download(_object_path: str, local_path: str) -> None:
        nonlocal captured_temp_dir
        target = Path(local_path)
        captured_temp_dir = target.parent
        target.write_bytes(b"source")

    def render_pair(_source, output_dir, _window, index):
        original = output_dir / f"{index}-original.wav"
        removed = output_dir / f"{index}-removed.wav"
        original.write_bytes(b"a")
        removed.write_bytes(b"b")
        return original, removed

    monkeypatch.setattr(audit, "_render_window_pair", render_pair)
    audit.run_audition(resolved, play=False, download=download)

    assert captured_temp_dir is not None
    assert not captured_temp_dir.exists()
    output = capsys.readouterr().out
    assert "6200–8400 ms" in output
    assert "7406–7978 ms" in output
    assert SOURCE_PATH not in output
    assert SOURCE_ID not in output
    assert "Temporary audition media deleted." in output


def test_ffmpeg_pair_uses_exact_receipt_boundaries(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        audit,
        "_run_checked",
        lambda command, phase: commands.append((command, phase)),
    )
    window = audit.AuditionWindow(
        6200,
        8400,
        7406,
        7978,
        "bilateral_silence",
        "selected_full",
    )

    audit._render_window_pair(tmp_path / "source", tmp_path, window, 1)

    original_command = list(commands[0][0])
    removed_command = list(commands[1][0])
    assert original_command[original_command.index("-ss") + 1] == "6.200"
    assert original_command[original_command.index("-t") + 1] == "2.200"
    graph = removed_command[removed_command.index("-filter_complex") + 1]
    assert removed_command[removed_command.index("-ss") + 1] == "6.200"
    assert "start=0:end=1.206" in graph
    assert "start=1.778:end=2.200" in graph


def test_media_subprocess_does_not_inherit_admin_or_storage_secrets(monkeypatch):
    run = Mock()
    monkeypatch.setenv("ADMIN_API_KEY", "must-not-leak")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "must-not-leak")
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setattr(audit.subprocess, "run", run)

    audit._run_checked(("ffmpeg", "-version"), phase="ffmpeg check")

    child_env = run.call_args.kwargs["env"]
    assert child_env["PATH"] == "/safe/bin"
    assert "ADMIN_API_KEY" not in child_env
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" not in child_env


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools unavailable",
)
def test_real_ffmpeg_pair_has_exact_original_and_removed_durations(tmp_path):
    source = tmp_path / "source.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            str(source),
        ],
        check=True,
    )
    window = audit.AuditionWindow(
        6200,
        8400,
        7406,
        7978,
        "bilateral_silence",
        "selected_full",
    )
    original, removed = audit._render_window_pair(source, tmp_path, window, 1)

    def duration(path: Path) -> float:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())

    assert duration(original) == pytest.approx(2.200, abs=0.02)
    assert duration(removed) == pytest.approx(1.628, abs=0.02)


def test_main_refuses_http_loopback_before_credential_or_network(monkeypatch, capsys):
    authenticate = Mock(side_effect=AssertionError("credential must not be read"))
    fetch = Mock(side_effect=AssertionError("network must not be touched"))
    monkeypatch.setattr(audit, "_authenticate_admin", authenticate)
    monkeypatch.setattr(audit, "_fetch_debug_payload", fetch)

    assert (
        audit.main(
            [
                "--job-id",
                JOB_ID,
                "--base-url",
                "http://localhost:8000",
                "--no-play",
            ]
        )
        == 2
    )

    authenticate.assert_not_called()
    fetch.assert_not_called()
    assert "loopback endpoints cannot safely receive ADMIN_API_KEY" in capsys.readouterr().err


def test_main_refuses_implicit_local_mode_before_credential_or_network(monkeypatch, capsys):
    authenticate = Mock(side_effect=AssertionError("credential must not be read"))
    fetch = Mock(side_effect=AssertionError("network must not be touched"))
    monkeypatch.setattr(audit, "_authenticate_admin", authenticate)
    monkeypatch.setattr(audit, "_fetch_debug_payload", fetch)

    assert audit.main(["--job-id", JOB_ID, "--no-play"]) == 2

    authenticate.assert_not_called()
    fetch.assert_not_called()
    assert "local audit mode is disabled" in capsys.readouterr().err


def test_main_production_https_authenticates_before_fetching(monkeypatch):
    order = []
    monkeypatch.setattr(audit, "_authenticate_admin", lambda: order.append("auth") or "secret")
    fetch = Mock(side_effect=lambda **_kwargs: order.append("fetch") or _payload())
    monkeypatch.setattr(audit, "_fetch_debug_payload", fetch)
    monkeypatch.setattr(
        audit,
        "resolve_audition",
        lambda *_args, **_kwargs: order.append("resolve") or Mock(),
    )
    monkeypatch.setattr(audit, "run_audition", lambda *_args, **_kwargs: order.append("run"))

    assert audit.main(["--job-id", JOB_ID, "--no-play", "--prod"]) == 0
    assert order == ["auth", "fetch", "resolve", "run"]
    assert fetch.call_args.kwargs == {
        "job_id": JOB_ID,
        "base_url": audit.PROD_BASE_URL,
        "credential": "secret",
    }
