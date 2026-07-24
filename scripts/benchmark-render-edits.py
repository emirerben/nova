#!/usr/bin/env python3
"""Benchmark cold generative render plus one warm music edit.

The script is intentionally API-driven so it can run against the local-render stack
or a dev server without importing application code. It writes both raw JSON and a
compact Markdown summary under ``.render-benchmarks/``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "http://localhost:8001"
DEFAULT_FIXTURE = "/private/tmp/nova-landscape-source-14.mp4"
DEFAULT_OUT_DIR = ".render-benchmarks"
TERMINAL = {"variants_ready", "variants_ready_partial", "variants_failed", "processing_failed"}


def _json_default(value: Any) -> str:
    return str(value)


def _request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, bytes]:
    req_data = (
        data
        if data is not None
        else (json.dumps(body).encode() if body is not None else None)
    )
    req_headers = dict(headers or {})
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=req_data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post_json(url: str, body: dict[str, Any], *, timeout: float = 60) -> dict[str, Any]:
    code, raw = _request("POST", url, body=body, timeout=timeout)
    if not 200 <= code < 300:
        raise RuntimeError(f"POST {url} failed: HTTP {code} {raw[:500]!r}")
    return json.loads(raw)


def _get_json(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 60
) -> dict[str, Any] | None:
    code, raw = _request("GET", url, headers=headers, timeout=timeout)
    if not 200 <= code < 300:
        return None
    return json.loads(raw)


def _content_type_for(path: Path) -> str:
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
    }.get(path.suffix.lower(), "application/octet-stream")


def _post_multipart(
    url: str, file_path: Path, field_name: str, content_type: str, timeout: float = 300
) -> dict[str, Any]:
    boundary = "----nova-render-benchmark-" + os.urandom(8).hex()
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{file_path.name}"\r\n'
    ).encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += file_path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    code, raw = _request("POST", url, data=bytes(body), headers=headers, timeout=timeout)
    if not 200 <= code < 300:
        raise RuntimeError(f"multipart POST {url} failed: HTTP {code} {raw[:500]!r}")
    return json.loads(raw)


def _upload_clip(api_url: str, clip: Path) -> str:
    payload = _post_multipart(
        f"{api_url}/music-jobs/upload-slot",
        clip,
        "file",
        _content_type_for(clip),
    )
    return str(payload["gcs_path"])


def _submit_generative(api_url: str, clip_paths: list[str]) -> str:
    payload = _post_json(f"{api_url}/generative-jobs", {"clip_gcs_paths": clip_paths})
    return str(payload["job_id"])


def _poll(api_url: str, job_id: str, *, timeout_s: float) -> tuple[dict[str, Any], float]:
    endpoint = f"{api_url}/generative-jobs/{job_id}/status"
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last_status: str | None = None
    while time.monotonic() < deadline:
        payload = _get_json(endpoint, timeout=15)
        if payload is None:
            time.sleep(5)
            continue
        status = str(payload.get("status") or "")
        if status != last_status:
            print(
                f"  [{time.strftime('%H:%M:%S')}] status={status} "
                f"phase={payload.get('current_phase') or '-'}"
            )
            last_status = status
        if status in TERMINAL:
            return payload, time.monotonic() - started
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for job {job_id}")


def _admin_debug(api_url: str, job_id: str) -> dict[str, Any] | None:
    token = os.environ.get("ADMIN_API_KEY") or os.environ.get("ADMIN_PROD_API_KEY")
    headers = {"X-Admin-Token": token} if token else None
    return _get_json(f"{api_url}/admin/jobs/{job_id}/debug", headers=headers, timeout=60)


def _ffprobe(path_or_url: str) -> dict[str, Any] | None:
    if path_or_url.startswith("http"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height,r_frame_rate:format=duration,bit_rate",
                "-of",
                "json",
                path_or_url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return {"error": proc.stderr[-500:]}
    return json.loads(proc.stdout or "{}")


def _pick_song_text(status_payload: dict[str, Any]) -> dict[str, Any]:
    for variant in status_payload.get("variants") or []:
        if variant.get("variant_id") == "song_text" and variant.get("music_track_id"):
            return dict(variant)
    raise RuntimeError("No eligible song_text variant with a music_track_id was rendered")


def _warm_swap_same_track(api_url: str, job_id: str, variant: dict[str, Any]) -> None:
    track_id = variant.get("music_track_id")
    if not track_id:
        raise RuntimeError("song_text has no music_track_id")
    _post_json(
        f"{api_url}/generative-jobs/{job_id}/variants/song_text/swap-song",
        {"new_track_id": track_id},
    )


def _summarize_phase_log(phase_log: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for entry in phase_log or []:
        out.append(
            {
                "name": entry.get("name"),
                "parent": entry.get("parent"),
                "elapsed_ms": entry.get("elapsed_ms"),
                "detail": entry.get("detail"),
            }
        )
    return out


def _summarize_trace(trace: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    interesting = {
        "clip_metadata_cache_hit",
        "clip_metadata_cache_miss",
        "preprocessed_sources_reused",
        "preprocessed_sources_stored",
        "timeline_override_path",
        "masonry_audio_only_swap",
        "music_window_audio_only_swap",
        "caption_lane_fast_path",
        "caption_reburn_start",
        "reburn",
        "full_render",
    }
    out = []
    for event in trace or []:
        if event.get("event") in interesting or event.get("stage") in {
            "render_perf",
            "audio_mix",
            "ingest",
        }:
            out.append(
                {
                    "stage": event.get("stage"),
                    "event": event.get("event"),
                    "data": event.get("data"),
                }
            )
    return out


def _debug_job_payload(debug: dict[str, Any] | None) -> dict[str, Any]:
    if not debug:
        return {}
    job = debug.get("job") or {}
    return {
        "phase_log": _summarize_phase_log(job.get("phase_log")),
        "pipeline_trace": _summarize_trace(job.get("pipeline_trace")),
        "assembly_plan": job.get("assembly_plan"),
    }


def _write_reports(out_dir: Path, report: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"render-benchmark-{slug}.json"
    md_path = out_dir / f"render-benchmark-{slug}.md"
    json_path.write_text(json.dumps(report, indent=2, default=_json_default), encoding="utf-8")

    cold = report["cold"]
    warm = report["warm"]
    lines = [
        "# Render Benchmark",
        "",
        f"- API: `{report['api_url']}`",
        f"- Fixture: `{report['fixture']}`",
        f"- Job: `{report['job_id']}`",
        f"- Cold render: `{cold['status']}` in `{cold['wall_time_s']:.1f}s`",
        f"- Warm music edit: `{warm['status']}` in `{warm['wall_time_s']:.1f}s`",
        "",
        "## Cold Phase Log",
        "",
    ]
    for entry in cold.get("phase_log") or []:
        lines.append(f"- `{entry.get('name')}`: `{entry.get('elapsed_ms')}` ms")
    lines.extend(["", "## Warm Phase Log", ""])
    for entry in warm.get("phase_log") or []:
        if entry.get("parent") or entry.get("name"):
            parent = f" ({entry.get('parent')})" if entry.get("parent") else ""
            lines.append(f"- `{entry.get('name')}`{parent}: `{entry.get('elapsed_ms')}` ms")
    lines.extend(["", "## Trace Events", ""])
    for event in warm.get("pipeline_trace") or []:
        lines.append(f"- `{event.get('stage')}.{event.get('event')}` {event.get('data')}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("LOCAL_RENDER_API_URL", DEFAULT_API_URL),
    )
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--timeout-s", type=float, default=1800)
    args = parser.parse_args()

    fixture = Path(args.fixture).expanduser().resolve()
    if not fixture.is_file():
        print(f"ERROR: fixture not found: {fixture}", file=sys.stderr)
        return 2

    api_url = str(args.api_url).rstrip("/")
    print(f"Uploading fixture: {fixture}")
    gcs_path = _upload_clip(api_url, fixture)
    print(f"Submitting cold render with {gcs_path}")
    job_id = _submit_generative(api_url, [gcs_path])
    cold_status, cold_wall = _poll(api_url, job_id, timeout_s=args.timeout_s)
    if cold_status.get("status") not in {"variants_ready", "variants_ready_partial"}:
        raise RuntimeError(f"Cold render did not produce variants: {cold_status.get('status')}")
    cold_debug = _admin_debug(api_url, job_id)

    song_text = _pick_song_text(cold_status)
    print("Dispatching warm same-track music edit for song_text")
    _warm_swap_same_track(api_url, job_id, song_text)
    warm_status, warm_wall = _poll(api_url, job_id, timeout_s=args.timeout_s)
    warm_debug = _admin_debug(api_url, job_id)

    cold_job = _debug_job_payload(cold_debug)
    warm_job = _debug_job_payload(warm_debug)
    report = {
        "api_url": api_url,
        "fixture": str(fixture),
        "job_id": job_id,
        "uploaded_gcs_path": gcs_path,
        "cold": {
            "status": cold_status.get("status"),
            "wall_time_s": round(cold_wall, 3),
            "phase_log": cold_job.get("phase_log")
            or _summarize_phase_log(cold_status.get("phase_log")),
            "pipeline_trace": cold_job.get("pipeline_trace") or [],
            "variants": cold_status.get("variants"),
        },
        "warm": {
            "status": warm_status.get("status"),
            "wall_time_s": round(warm_wall, 3),
            "phase_log": warm_job.get("phase_log")
            or _summarize_phase_log(warm_status.get("phase_log")),
            "pipeline_trace": warm_job.get("pipeline_trace") or [],
            "variants": warm_status.get("variants"),
        },
        "ffprobe": _ffprobe(str(fixture)),
    }
    _write_reports(Path(args.out_dir), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
