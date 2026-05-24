#!/usr/bin/env python3
"""Drive a template or music job against the local-render stack.

Pairs with `docker-compose.local-render.yml` and the `make local-render` target.
The point is to put a real video through the same image Fly.io runs and write
the resulting MP4 to disk for inspection.

Usage:
    python3 scripts/local-render.py --clip <path> --template <uuid> \\
        [--mode template|music] [--api-url URL] [--out-dir DIR]

    # generative mode (no template; auto-matched song; downloads all 3 variants):
    python3 scripts/local-render.py --mode generative \\
        --clip a.mp4 --clip b.mp4 --clip c.mp4 [--target-duration 20]

Env overrides:
    LOCAL_RENDER_API_URL=http://localhost:8001
    LOCAL_RENDER_OUT_DIR=./.local-render
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
import uuid
from pathlib import Path

DEFAULT_API_URL = "http://localhost:8001"
DEFAULT_OUT_DIR = ".local-render"

_TEMPLATE_TERMINAL = {"template_ready", "processing_failed", "done"}
_MUSIC_TERMINAL = {"music_ready", "processing_failed"}
_GENERATIVE_TERMINAL = {
    "variants_ready",
    "variants_ready_partial",
    "variants_failed",
    "processing_failed",
}


def _post_json(url: str, body: dict, timeout: float = 60) -> tuple[int, bytes]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get(url: str, timeout: float = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _put_bytes(url: str, content: bytes, content_type: str, timeout: float = 300) -> int:
    req = urllib.request.Request(
        url, data=content, headers={"Content-Type": content_type}, method="PUT"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _post_multipart(
    url: str, file_path: Path, field_name: str, content_type: str, timeout: float = 300
) -> tuple[int, bytes]:
    boundary = "----nova-local-render-" + os.urandom(8).hex()
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
    req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _wait_for_api(api_url: str, timeout_s: float = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            code, _ = _get(f"{api_url}/health", timeout=2)
            if 200 <= code < 300:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    print(f"ERROR: API at {api_url} did not become ready within {timeout_s}s", file=sys.stderr)
    sys.exit(2)


def _content_type_for(path: Path) -> str:
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".heic": "image/heic",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


def _upload_via_presigned(api_url: str, clip: Path) -> str:
    """Two-step presigned upload used by the template-jobs flow."""
    size = clip.stat().st_size
    code, body = _post_json(
        f"{api_url}/presigned-urls",
        {
            "files": [
                {
                    "filename": clip.name,
                    "content_type": _content_type_for(clip),
                    "file_size_bytes": size,
                }
            ]
        },
    )
    if not (200 <= code < 300):
        print(f"ERROR: /presigned-urls failed: HTTP {code} {body[:300]!r}", file=sys.stderr)
        sys.exit(1)
    payload = json.loads(body)
    item = payload["urls"][0]
    upload_url, gcs_path = item["upload_url"], item["gcs_path"]
    put_code = _put_bytes(upload_url, clip.read_bytes(), _content_type_for(clip))
    if not (200 <= put_code < 300):
        print(f"ERROR: signed PUT failed: HTTP {put_code}", file=sys.stderr)
        sys.exit(1)
    return gcs_path


def _upload_via_slot(api_url: str, clip: Path) -> str:
    """Multipart upload used by the music-jobs flow."""
    code, body = _post_multipart(
        f"{api_url}/music-jobs/upload-slot", clip, "file", _content_type_for(clip)
    )
    if not (200 <= code < 300):
        print(f"ERROR: /music-jobs/upload-slot failed: HTTP {code} {body[:300]!r}", file=sys.stderr)
        sys.exit(1)
    return json.loads(body)["gcs_path"]


def _submit_template_job(api_url: str, template_id: str, gcs_paths: list[str], inputs: dict) -> str:
    code, body = _post_json(
        f"{api_url}/template-jobs",
        {
            "template_id": template_id,
            "clip_gcs_paths": gcs_paths,
            "inputs": inputs,
        },
    )
    if not (200 <= code < 300):
        print(
            f"ERROR: POST /template-jobs failed: HTTP {code} {body[:500]!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(body)["job_id"]


def _submit_music_job(api_url: str, track_id: str, gcs_paths: list[str]) -> str:
    code, body = _post_json(
        f"{api_url}/music-jobs",
        {"music_track_id": track_id, "clip_gcs_paths": gcs_paths},
    )
    if not (200 <= code < 300):
        print(f"ERROR: POST /music-jobs failed: HTTP {code} {body[:500]!r}", file=sys.stderr)
        sys.exit(1)
    return json.loads(body)["job_id"]


def _submit_generative_job(api_url: str, gcs_paths: list[str], target_duration_s: float) -> str:
    code, body = _post_json(
        f"{api_url}/generative-jobs",
        {"clip_gcs_paths": gcs_paths, "target_duration_s": target_duration_s},
    )
    if not (200 <= code < 300):
        print(f"ERROR: POST /generative-jobs failed: HTTP {code} {body[:500]!r}", file=sys.stderr)
        sys.exit(1)
    return json.loads(body)["job_id"]


_MODE_ENDPOINT = {"template": "template-jobs", "music": "music-jobs", "generative": "generative-jobs"}
_MODE_TERMINAL = {
    "template": _TEMPLATE_TERMINAL,
    "music": _MUSIC_TERMINAL,
    "generative": _GENERATIVE_TERMINAL,
}


def _poll(api_url: str, job_id: str, mode: str, timeout_s: float = 1800) -> dict:
    """Block until job reaches a terminal status. Returns final status payload."""
    terminal = _MODE_TERMINAL[mode]
    endpoint = f"{api_url}/{_MODE_ENDPOINT[mode]}/{job_id}/status"
    deadline = time.time() + timeout_s
    last_status: str | None = None
    while time.time() < deadline:
        code, body = _get(endpoint, timeout=10)
        if 200 <= code < 300:
            data = json.loads(body)
            s = data.get("status")
            if s != last_status:
                phase = data.get("current_phase") or "-"
                print(f"  [{time.strftime('%H:%M:%S')}] status={s} phase={phase}")
                last_status = s
            if s in terminal:
                return data
        else:
            print(f"  poll HTTP {code} (transient?)")
        time.sleep(5)
    print(f"ERROR: timed out polling {endpoint} after {timeout_s}s", file=sys.stderr)
    sys.exit(1)


def _download_output(output_url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(output_url, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def _ffprobe_summary(path: Path) -> None:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height,r_frame_rate:format=duration,bit_rate",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(out.stdout.strip() or out.stderr.strip())
    except FileNotFoundError:
        print("  (ffprobe not installed on host — skip)")


def _print_settings_snapshot(api_url: str) -> None:
    """Read flags that affect render output back from the running API.

    We don't have a dedicated admin endpoint for this yet — read env from the
    api container directly via `docker compose exec` if available. Falls back to
    a notice if docker isn't reachable from where the script runs.
    """
    flags = [
        "TEXT_OVERLAY_V2_ENABLED",
        "ORIENTATION_NORMALIZE_ENABLED",
        "SINGLE_PASS_ENCODE_ENABLED",
        "TEXT_RENDERER_SKIA_ENABLED",
        "ENABLE_AUTO_MUSIC_MODE",
        "GEMINI_MODEL",
    ]
    try:
        proc = subprocess.run(
            [
                "docker-compose",
                "-f",
                "docker-compose.local-render.yml",
                "exec",
                "-T",
                "api",
                "env",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            print("  (settings snapshot: docker compose exec failed, skipping)")
            return
        env_lines = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in proc.stdout.splitlines()
            if "=" in line
        }
        for k in flags:
            print(f"  {k}={env_lines.get(k, '<unset>')}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  (settings snapshot: docker not available, skipping)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument(
        "--clip",
        action="append",
        dest="clips",
        required=True,
        help="Input video/image file. Repeat for multiple clips (generative mode takes 1-20).",
    )
    p.add_argument(
        "--template",
        default=None,
        help="Template UUID (template mode) or music_track UUID (music mode). "
        "Not used in generative mode (the song is auto-matched).",
    )
    p.add_argument(
        "--mode",
        choices=["template", "music", "generative"],
        default="template",
        help="Pipeline path to exercise (default: template)",
    )
    p.add_argument(
        "--target-duration",
        type=float,
        default=20.0,
        help="Target edit length in seconds (generative mode only, default: 20)",
    )
    p.add_argument(
        "--inputs",
        default="{}",
        help='Template inputs as JSON, e.g. \'{"location":"Tokyo"}\'',
    )
    p.add_argument(
        "--api-url",
        default=os.environ.get("LOCAL_RENDER_API_URL", DEFAULT_API_URL),
        help=f"API base URL (default: {DEFAULT_API_URL})",
    )
    p.add_argument(
        "--out-dir",
        default=os.environ.get("LOCAL_RENDER_OUT_DIR", DEFAULT_OUT_DIR),
        help=f"Where to write the rendered MP4 (default: {DEFAULT_OUT_DIR}/)",
    )
    args = p.parse_args()

    clips = [Path(c).expanduser().resolve() for c in args.clips]
    for c in clips:
        if not c.is_file():
            print(f"ERROR: clip not found: {c}", file=sys.stderr)
            return 2
    if args.mode != "generative" and len(clips) != 1:
        print(f"ERROR: --mode {args.mode} takes exactly one --clip", file=sys.stderr)
        return 2

    if args.mode == "generative":
        if args.template is not None:
            print("Note: --template is ignored in generative mode (song auto-matched).")
    else:
        if not args.template:
            print(f"ERROR: --mode {args.mode} requires --template <uuid>", file=sys.stderr)
            return 2
        try:
            uuid.UUID(args.template)
        except ValueError:
            print(f"ERROR: --template must be a UUID, got {args.template!r}", file=sys.stderr)
            return 2

    try:
        inputs = json.loads(args.inputs)
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: --inputs is not valid JSON object: {exc}", file=sys.stderr)
        return 2

    print(f"clips     : {', '.join(str(c) for c in clips)}")
    print(f"mode      : {args.mode}")
    if args.mode != "generative":
        print(f"target    : {args.template}")
    print(f"api       : {args.api_url}")

    print("\n[1/5] waiting for API…")
    _wait_for_api(args.api_url)

    print("\n[2/5] settings snapshot (read from running api container):")
    _print_settings_snapshot(args.api_url)

    print(f"\n[3/5] uploading {len(clips)} clip(s)…")
    gcs_paths: list[str] = []
    for c in clips:
        # generative + music both land under music-uploads/ via the slot endpoint
        # (the allowlisted prefix). Template uses the presigned two-step flow.
        gcs = _upload_via_presigned(args.api_url, c) if args.mode == "template" else _upload_via_slot(args.api_url, c)
        print(f"  → {gcs}")
        gcs_paths.append(gcs)

    print(f"\n[4/5] creating {args.mode} job…")
    if args.mode == "template":
        job_id = _submit_template_job(args.api_url, args.template, gcs_paths, inputs)
    elif args.mode == "music":
        job_id = _submit_music_job(args.api_url, args.template, gcs_paths)
    else:
        job_id = _submit_generative_job(args.api_url, gcs_paths, args.target_duration)
    print(f"  → job_id: {job_id}")

    print("\n[5/5] polling status…")
    final = _poll(args.api_url, job_id, args.mode)
    status = final.get("status")

    if args.mode == "generative":
        if status not in {"variants_ready", "variants_ready_partial"}:
            print(f"\nFAILED: status={status}")
            print(f"  error_detail   : {final.get('error_detail')}")
            return 1
        variants = final.get("variants") or []
        ok = [v for v in variants if v.get("ok") and v.get("output_url")]
        if not ok:
            print("ERROR: job terminal but no successful variant with an output_url", file=sys.stderr)
            for v in variants:
                print(f"  {v.get('variant_id')}: {v.get('render_status')} — {v.get('error')}")
            return 1
        out_paths = []
        for v in variants:
            label = v.get("variant_id", "variant")
            if not (v.get("ok") and v.get("output_url")):
                print(f"\n  [skip] {label}: {v.get('render_status')} — {v.get('error')}")
                continue
            out_path = Path(args.out_dir) / f"{job_id}-{label}.mp4"
            song = v.get("track_title") or "original audio"
            print(f"\ndownloading {label} ({v.get('text_mode')} / {song}) → {out_path}")
            _download_output(v["output_url"], out_path)
            print(f"  wrote {out_path.stat().st_size:,} bytes")
            _ffprobe_summary(out_path)
            out_paths.append(out_path)
        print(f"\n✓ generative render complete: {len(out_paths)} variant(s) in {args.out_dir}/")
        for pth in out_paths:
            print(f"    {pth}")
        return 0

    if status not in {"template_ready", "music_ready", "done"}:
        print(f"\nFAILED: status={status}")
        print(f"  error_detail   : {final.get('error_detail')}")
        print(f"  failure_reason : {final.get('failure_reason')}")
        return 1

    output_url = (final.get("assembly_plan") or {}).get("output_url")
    if not output_url:
        print("ERROR: job ready but no output_url on assembly_plan", file=sys.stderr)
        return 1

    out_path = Path(args.out_dir) / f"{job_id}.mp4"
    print(f"\ndownloading → {out_path}")
    _download_output(output_url, out_path)
    print(f"  wrote {out_path.stat().st_size:,} bytes")

    print("\nffprobe:")
    _ffprobe_summary(out_path)

    print(f"\n✓ local render complete: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
