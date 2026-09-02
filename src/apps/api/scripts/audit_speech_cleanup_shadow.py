"""Audition timing-only speech-cleanup shadow receipts against owned source audio.

This is an admin-only, read-only operator-side tool.  It fetches one existing
``GET /admin/jobs/{job_id}/debug`` payload, resolves a receipt's non-reversible
``source_tag`` against the Job's current path/source-instance vector, downloads
that exact durable object, and plays original-versus-cut audio windows.  No media
is uploaded or persisted: source and WAV files live in a ``TemporaryDirectory``
and are deleted on success, refusal, Ctrl-C, or subprocess failure.

The admin credential is deliberately unavailable as a CLI argument.  Configure
``ADMIN_API_KEY`` for this local process, then enter the same value at the hidden
prompt.  It is compared with ``secrets.compare_digest`` before any network or
storage call and is sent only to Nova's fixed production HTTPS origin as the
``X-Admin-Token`` header.  Plaintext loopback APIs are deliberately unsupported:
they cannot authenticate the server receiving this global credential.

Usage:
  python scripts/audit_speech_cleanup_shadow.py --job-id <uuid> --prod
  python scripts/audit_speech_cleanup_shadow.py --job-id <uuid> \
      --attempt-id <opaque-id> --source-tag <16-hex> --prod
  python scripts/audit_speech_cleanup_shadow.py --job-id <uuid> --no-play --prod
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.speech_cleanup_identity import (
    speech_cleanup_rollout_fingerprint,
    speech_cleanup_source_tag,
    validate_clip_source_identity,
)

PROD_BASE_URL = "https://nova-video.fly.dev"
_MAX_DEBUG_BYTES = 32 * 1024 * 1024
_SUPPORTED_SCHEMA_VERSION = 1
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_SOURCE_TAG_RE = re.compile(r"^[0-9a-f]{16}$")
_AUDITION_REASONS = frozenset({"bilateral_silence"})
_AUDITION_DISPOSITIONS = frozenset(
    {
        "selected_full",
        "promoted_protected",
        "dropped_budget",
        "dropped_max_removals",
        "dropped_min_cut",
        "dropped_micro_gap",
        "dropped_safety_bailout",
        "not_candidate",
    }
)
_MEDIA_ENV_ALLOWLIST = frozenset(
    {
        "AV_LOG_FORCE_NOCOLOR",
        "DISPLAY",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LC_ALL",
        "PATH",
        "SDL_AUDIODRIVER",
        "TMPDIR",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
    }
)


class AuditRefusal(RuntimeError):
    """The requested audition cannot be proven safe and exact."""


@dataclass(frozen=True)
class AuditionWindow:
    window_start_ms: int
    window_end_ms: int
    island_start_ms: int
    island_end_ms: int
    reason: str
    disposition: str

    @property
    def window_duration_s(self) -> float:
        return (self.window_end_ms - self.window_start_ms) / 1000


@dataclass(frozen=True)
class ResolvedAudition:
    attempt_id: str
    source_tag: str
    object_path: str
    windows: tuple[AuditionWindow, ...]


def _record(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _bounded_string(value: object, *, max_length: int = 160) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    return value


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _opaque_id(value: object) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_ID_RE.fullmatch(value) else None


def _authenticate_admin(
    *,
    prompt: Callable[[str], str] = getpass.getpass,
    configured_key: str | None = None,
) -> str:
    """Require a hidden, constant-time local credential check before any IO."""

    expected = _configured_admin_key() if configured_key is None else configured_key
    if not isinstance(expected, str) or not expected:
        raise AuditRefusal("ADMIN_API_KEY is not configured")
    supplied = prompt("Admin API key: ")
    if not isinstance(supplied, str) or not supplied:
        raise AuditRefusal("admin credential is required")
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise AuditRefusal("admin credential rejected")
    return supplied


def _configured_admin_key() -> str:
    """Read the explicit environment first, then the app's Settings loader."""

    from_environment = os.environ.get("ADMIN_API_KEY", "")
    if from_environment:
        return from_environment
    try:
        from app.config import settings  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - report the auth prerequisite, not config internals
        return ""
    return settings.admin_api_key


def _configured_timeline_editor_enabled() -> bool:
    try:
        from app.config import settings  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - config detail can contain sensitive values
        raise AuditRefusal("application settings are unavailable") from exc
    return settings.GENERATIVE_TIMELINE_EDITOR_ENABLED


def _fetch_debug_payload(
    *, job_id: str, base_url: str, credential: str, timeout_s: float = 30
) -> dict[str, Any]:
    base_url = _validated_base_url(base_url)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/admin/jobs/{job_id}/debug",
        headers={"X-Admin-Token": credential},
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout_s) as response:
            encoded = response.read(_MAX_DEBUG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AuditRefusal(f"admin debug request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AuditRefusal("admin debug request was unavailable") from exc
    if len(encoded) > _MAX_DEBUG_BYTES:
        raise AuditRefusal("admin debug response exceeded the local safety limit")
    try:
        parsed = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditRefusal("admin debug response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AuditRefusal("admin debug response was not an object")
    return parsed


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the admin header across an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _validated_base_url(value: str) -> str:
    """Allow only Nova's fixed, authenticated production HTTPS origin."""

    try:
        parsed = urllib.parse.urlparse(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise AuditRefusal("admin API base URL is invalid") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AuditRefusal("admin API base URL is not allowed")
    if parsed.path not in {"", "/"}:
        raise AuditRefusal("admin API base URL must not contain a path")
    is_prod = parsed.scheme == "https" and hostname == "nova-video.fly.dev" and port in {None, 443}
    if not is_prod:
        raise AuditRefusal(
            "admin API base URL must be Nova production over HTTPS; "
            "loopback endpoints cannot safely receive ADMIN_API_KEY"
        )
    return PROD_BASE_URL


def _receipt_is_truncated(data: Mapping[str, Any]) -> bool:
    inputs = _record(data.get("inputs")) or {}
    scan = _record(data.get("mixed_gap_scan")) or {}
    baseline = _record(data.get("baseline_plan")) or {}
    candidate = _record(data.get("candidate_plan")) or {}
    omitted_fields = (
        inputs.get("asr_word_spans_omitted"),
        inputs.get("silence_spans_omitted"),
        inputs.get("lexical_candidates_omitted"),
        scan.get("records_omitted"),
        baseline.get("removed_spans_omitted"),
        candidate.get("removed_spans_omitted"),
    )
    parsed: list[int] = []
    for value in omitted_fields:
        count = _integer(value)
        if count is None or count < 0:
            raise AuditRefusal("receipt truncation metadata is missing or invalid")
        parsed.append(count)
    return any(count > 0 for count in parsed)


def _parse_audition_windows(data: Mapping[str, Any]) -> tuple[AuditionWindow, ...]:
    duration_ms = _integer(data.get("duration_ms"))
    if duration_ms is None or duration_ms <= 0:
        raise AuditRefusal("receipt duration is missing or invalid")
    scan = _record(data.get("mixed_gap_scan"))
    records = scan.get("records") if scan else None
    if not isinstance(records, list):
        raise AuditRefusal("receipt mixed-gap records are missing")
    if _receipt_is_truncated(data):
        raise AuditRefusal("receipt timing arrays are truncated")

    windows: list[AuditionWindow] = []
    for raw in records:
        record = _record(raw)
        if record is None or record.get("detection") != "eligible":
            continue
        window_start = _integer(record.get("window_start_ms"))
        window_end = _integer(record.get("window_end_ms"))
        island_start = _integer(record.get("island_start_ms"))
        island_end = _integer(record.get("island_end_ms"))
        reason = record.get("reason")
        disposition = record.get("plan_disposition")
        if None in (window_start, window_end, island_start, island_end):
            raise AuditRefusal("eligible receipt record has invalid timing")
        assert window_start is not None
        assert window_end is not None
        assert island_start is not None
        assert island_end is not None
        if not (0 <= window_start < island_start < island_end < window_end <= duration_ms):
            raise AuditRefusal("eligible receipt record is out of bounds")
        if reason not in _AUDITION_REASONS or disposition not in _AUDITION_DISPOSITIONS:
            raise AuditRefusal("eligible receipt record has invalid decision metadata")
        windows.append(
            AuditionWindow(
                window_start_ms=window_start,
                window_end_ms=window_end,
                island_start_ms=island_start,
                island_end_ms=island_end,
                reason=reason,
                disposition=disposition,
            )
        )
    if not windows:
        raise AuditRefusal("receipt contains no eligible mixed-gap windows")
    return tuple(windows)


def _matching_receipts(
    payload: Mapping[str, Any],
    *,
    attempt_id: str | None,
    source_tag: str | None,
) -> list[Mapping[str, Any]]:
    job = _record(payload.get("job"))
    events = job.get("pipeline_trace") if job else None
    if not isinstance(events, list):
        raise AuditRefusal("job has no pipeline trace")
    matches: list[Mapping[str, Any]] = []
    for raw in events:
        event = _record(raw)
        data = _record(event.get("data")) if event else None
        if (
            event is None
            or event.get("stage") != "silence_cut"
            or event.get("event") != "silence_cut_mixed_gap_analysis"
            or data is None
            or data.get("schema_version") != _SUPPORTED_SCHEMA_VERSION
            or data.get("detector_version") != "mixed-gap-v1"
            or data.get("analysis_view") not in {"full_clip", "talking_head_spine_capped"}
            or data.get("assignment_status") != "assigned"
            or data.get("candidate_status") != "ready"
            or data.get("effective_mode") != "shadow"
        ):
            continue
        if attempt_id is not None and data.get("analysis_attempt_id") != attempt_id:
            continue
        if source_tag is not None and data.get("source_tag") != source_tag:
            continue
        matches.append(data)
    return matches


def _durable_prefix(payload: Mapping[str, Any], job_id: str) -> str:
    job = _record(payload.get("job"))
    if job is None:
        raise AuditRefusal("debug payload is missing the job")
    assembly_plan = _record(job.get("assembly_plan")) or {}
    all_candidates = _record(job.get("all_candidates")) or {}
    manual = (
        job.get("mode") == "manual_draft"
        or assembly_plan.get("manual_draft") is True
        or all_candidates.get("manual_draft") is True
    )
    if manual:
        user_id = _bounded_string(job.get("user_id"))
        item_id = _bounded_string(job.get("content_plan_item_id"))
        if not user_id or not item_id:
            raise AuditRefusal("manual-draft durable ownership is incomplete")
        return f"users/{user_id}/plan/{item_id}/"
    return f"generative-jobs/{job_id}/sources/"


def _safe_owned_object_path(path: str, prefix: str) -> bool:
    candidate = PurePosixPath(path)
    return (
        path.startswith(prefix)
        and not candidate.is_absolute()
        and all(part not in {"", ".", ".."} for part in candidate.parts)
        and "://" not in path
    )


def resolve_audition(
    payload: Mapping[str, Any],
    *,
    requested_job_id: str,
    attempt_id: str | None = None,
    source_tag: str | None = None,
    timeline_editor_enabled: bool | None = None,
    object_exists: Callable[[str], bool] | None = None,
) -> ResolvedAudition:
    """Prove receipt→current source identity→durable object without slot fallback."""

    try:
        canonical_job_id = str(uuid.UUID(requested_job_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AuditRefusal("job ID is invalid") from exc
    job = _record(payload.get("job"))
    if job is None or job.get("id") != canonical_job_id:
        raise AuditRefusal("debug payload job does not match the requested job")
    enabled = (
        _configured_timeline_editor_enabled()
        if timeline_editor_enabled is None
        else timeline_editor_enabled
    )
    if not enabled:
        raise AuditRefusal("durable source copying is disabled")

    matches = _matching_receipts(
        payload,
        attempt_id=attempt_id,
        source_tag=source_tag,
    )
    if not matches:
        raise AuditRefusal("no matching complete assigned shadow receipt")
    if len(matches) != 1:
        raise AuditRefusal("receipt selection is ambiguous; provide attempt and source tag")
    receipt = matches[0]
    receipt_attempt = _opaque_id(receipt.get("analysis_attempt_id"))
    receipt_tag = receipt.get("source_tag")
    if (
        receipt_attempt is None
        or not isinstance(receipt_tag, str)
        or _SOURCE_TAG_RE.fullmatch(receipt_tag) is None
    ):
        raise AuditRefusal("receipt identity is invalid")
    windows = _parse_audition_windows(receipt)

    all_candidates = _record(job.get("all_candidates"))
    if all_candidates is None:
        raise AuditRefusal("job source identity is unavailable")
    identity = validate_clip_source_identity(all_candidates)
    if not identity.valid:
        raise AuditRefusal(f"job source identity is unavailable ({identity.status})")
    matching_pairs = []
    for path, instance_id in identity.pairs:
        fingerprint = speech_cleanup_rollout_fingerprint(canonical_job_id, instance_id)
        if speech_cleanup_source_tag(fingerprint) == receipt_tag:
            matching_pairs.append((path, instance_id))
    if len(matching_pairs) != 1:
        raise AuditRefusal("receipt source was removed, replaced, duplicated, or is ambiguous")
    object_path = matching_pairs[0][0]
    prefix = _durable_prefix(payload, canonical_job_id)
    if not _safe_owned_object_path(object_path, prefix):
        raise AuditRefusal("matched source is not an owned durable object")
    if object_exists is None:
        try:
            from app import storage  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - never echo config credentials
            raise AuditRefusal("storage configuration is unavailable") from exc
        object_exists = storage.object_exists
    try:
        exists = object_exists(object_path)
    except Exception as exc:  # noqa: BLE001 - storage uncertainty must fail closed
        raise AuditRefusal("durable source existence could not be verified") from exc
    if not exists:
        raise AuditRefusal("durable source object no longer exists")

    return ResolvedAudition(
        attempt_id=receipt_attempt,
        source_tag=receipt_tag,
        object_path=object_path,
        windows=windows,
    )


def _run_checked(command: Sequence[str], *, phase: str) -> None:
    try:
        subprocess.run(
            list(command),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            env={key: value for key, value in os.environ.items() if key in _MEDIA_ENV_ALLOWLIST},
        )
    except FileNotFoundError as exc:
        raise AuditRefusal(f"{phase} executable is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuditRefusal(f"{phase} timed out") from exc
    except subprocess.CalledProcessError as exc:
        # FFmpeg/player stderr can contain source paths. Keep it out of operator
        # output; the bounded phase name is enough to diagnose this local tool.
        raise AuditRefusal(f"{phase} failed") from exc


def _render_window_pair(
    source: Path,
    output_dir: Path,
    window: AuditionWindow,
    index: int,
) -> tuple[Path, Path]:
    original = output_dir / f"window-{index:03d}-original.wav"
    removed = output_dir / f"window-{index:03d}-removed.wav"
    window_start_s = window.window_start_ms / 1000
    island_start_s = window.island_start_ms / 1000
    island_end_s = window.island_end_ms / 1000
    relative_island_start_s = island_start_s - window_start_s
    relative_island_end_s = island_end_s - window_start_s
    _run_checked(
        (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{window_start_s:.3f}",
            "-i",
            str(source),
            "-t",
            f"{window.window_duration_s:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(original),
        ),
        phase="ffmpeg original-window extraction",
    )
    filter_graph = (
        f"[0:a]atrim=start=0:end={relative_island_start_s:.3f},"
        "asetpts=PTS-STARTPTS[before];"
        f"[0:a]atrim=start={relative_island_end_s:.3f}:end={window.window_duration_s:.3f},"
        "asetpts=PTS-STARTPTS[after];"
        "[before][after]concat=n=2:v=0:a=1[out]"
    )
    _run_checked(
        (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{window_start_s:.3f}",
            "-i",
            str(source),
            "-filter_complex",
            filter_graph,
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(removed),
        ),
        phase="ffmpeg removed-window extraction",
    )
    return original, removed


def _play(path: Path, *, label: str) -> None:
    print(f"  Playing {label}…")
    _run_checked(
        ("ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)),
        phase="ffplay audition",
    )


def run_audition(
    resolved: ResolvedAudition,
    *,
    play: bool = True,
    download: Callable[[str, str], None] | None = None,
) -> None:
    """Download, render, optionally play, and always delete local media."""

    with tempfile.TemporaryDirectory(prefix="nova-speech-audit-") as temp_name:
        temp_dir = Path(temp_name)
        source = temp_dir / "source-media"
        if download is None:
            try:
                from app import storage  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001 - never echo config credentials
                raise AuditRefusal("storage configuration is unavailable") from exc
            download = storage.download_to_file
        try:
            download(resolved.object_path, str(source))
        except Exception as exc:  # noqa: BLE001 - never echo path-bearing storage errors
            raise AuditRefusal("durable source download failed") from exc
        print(
            f"Audition {resolved.attempt_id} / {resolved.source_tag}: "
            f"{len(resolved.windows)} eligible window(s)"
        )
        for index, window in enumerate(resolved.windows, start=1):
            original, removed = _render_window_pair(source, temp_dir, window, index)
            print(
                f"Window {index}: context {window.window_start_ms}–{window.window_end_ms} ms; "
                f"candidate cut {window.island_start_ms}–{window.island_end_ms} ms; "
                f"{window.reason}/{window.disposition}"
            )
            if play:
                _play(original, label="original context")
                _play(removed, label="candidate removal")
    print("Temporary audition media deleted.")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, help="Exact Job UUID to audit.")
    parser.add_argument("--attempt-id", help="Exact opaque analysis attempt ID.")
    parser.add_argument("--source-tag", help="Exact 16-hex receipt source tag.")
    parser.add_argument(
        "--prod",
        action="store_true",
        help=(
            "Use Nova's fixed production HTTPS API. Required unless --base-url "
            "selects the same approved origin; plaintext local mode is refused."
        ),
    )
    parser.add_argument(
        "--base-url",
        help=(
            "Admin API base URL override. Security policy currently accepts only "
            "https://nova-video.fly.dev; loopback endpoints are refused."
        ),
    )
    parser.add_argument(
        "--no-play",
        action="store_true",
        help="Validate and extract windows without launching ffplay.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        canonical_job_id = str(uuid.UUID(args.job_id))
    except (ValueError, TypeError, AttributeError):
        print("ERROR: job ID is invalid", file=sys.stderr)
        return 2
    try:
        if args.base_url:
            base_url = _validated_base_url(args.base_url)
        elif args.prod:
            base_url = _validated_base_url(PROD_BASE_URL)
        else:
            raise AuditRefusal(
                "local audit mode is disabled because plaintext loopback cannot "
                "safely receive ADMIN_API_KEY; rerun with --prod"
            )
        credential = _authenticate_admin()
        payload = _fetch_debug_payload(
            job_id=canonical_job_id,
            base_url=base_url,
            credential=credential,
        )
        resolved = resolve_audition(
            payload,
            requested_job_id=canonical_job_id,
            attempt_id=args.attempt_id,
            source_tag=args.source_tag,
        )
        run_audition(resolved, play=not args.no_play)
    except AuditRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; temporary audition media deleted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
