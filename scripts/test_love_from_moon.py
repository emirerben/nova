#!/usr/bin/env python3
"""End-to-end test of the Love-From-Moon templated track.

Steps:
  1. POST /music-jobs/upload-slot with the test video → get gcs_path
  2. POST /music-jobs with {music_track_id, [gcs_path]} → get job_id
  3. Poll /music-jobs/{id}/status until terminal (music_ready / processing_failed)
  4. Print the resulting output_url

Usage:
  python3 scripts/test_love_from_moon.py <track_id>

Env overrides:
  API_URL=http://localhost:8000
  CLIP_PATH=/path/to/video.mp4 or /path/to/photo.jpg
"""

import json
import os
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request


def _read_env() -> dict[str, str]:
    repo = Path(__file__).resolve().parent.parent
    env_path = repo / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _post_json(url: str, body: dict, headers: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get_json(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_multipart(url: str, fields: dict, files: list, headers: dict | None = None) -> tuple[int, bytes]:
    boundary = "----nova-test-" + os.urandom(8).hex()
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    for name, filename, content_type, data in files:
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'
        ).encode()
        body += f"Content-Type: {content_type}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {
        **(headers or {}),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: test_love_from_moon.py <track_id>", file=sys.stderr)
        return 2
    track_id = sys.argv[1]

    env = _read_env()
    api_url = (
        os.environ.get("API_URL")
        or env.get("API_URL")
        or "http://localhost:8000"
    ).rstrip("/")
    clip_path = os.environ.get(
        "CLIP_PATH", "/Users/yasinberk/Desktop/brazil/db.MOV"
    )

    if not os.path.exists(clip_path):
        print(f"ERROR: clip not found at {clip_path}", file=sys.stderr)
        return 2

    print(f"track_id : {track_id}")
    print(f"api      : {api_url}")
    print(f"clip     : {clip_path}")

    # 1. Upload the user clip
    print("\n[1/3] uploading slot clip…")
    ext = Path(clip_path).suffix.lower()
    ct = {
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".m4v": "video/x-m4v", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    status, body = _post_multipart(
        f"{api_url}/music-jobs/upload-slot",
        fields={},
        files=[("file", os.path.basename(clip_path), ct, Path(clip_path).read_bytes())],
    )
    print(f"  HTTP {status}: {body.decode(errors='replace')[:300]}")
    if not (200 <= status < 300):
        return 1
    upload = json.loads(body)
    gcs_path = upload["gcs_path"]
    print(f"  → gcs_path: {gcs_path} ({upload['kind']})")

    # 2. Create the music job
    print("\n[2/3] creating music job…")
    status, body = _post_json(
        f"{api_url}/music-jobs",
        {"music_track_id": track_id, "clip_gcs_paths": [gcs_path]},
    )
    print(f"  HTTP {status}: {body.decode(errors='replace')[:300]}")
    if not (200 <= status < 300):
        return 1
    job = json.loads(body)
    job_id = job["job_id"]
    print(f"  → job_id: {job_id}")

    # 3. Poll status
    print("\n[3/3] polling status…")
    terminal = {"music_ready", "processing_failed"}
    deadline = time.time() + 600
    while time.time() < deadline:
        status, body = _get_json(f"{api_url}/music-jobs/{job_id}/status")
        if 200 <= status < 300:
            data = json.loads(body)
            s = data.get("status")
            print(f"  status={s}")
            if s in terminal:
                print(f"\nfinal status: {s}")
                if s == "music_ready":
                    output_url = (data.get("assembly_plan") or {}).get("output_url")
                    print(f"output_url: {output_url}")
                else:
                    print(f"error_detail: {data.get('error_detail')}")
                return 0 if s == "music_ready" else 1
        else:
            print(f"  poll HTTP {status}")
        time.sleep(3)
    print("ERROR: timed out waiting for job", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
