#!/usr/bin/env python3
"""Seed the Love-From-Moon templated music track via the admin API.

Posts to POST /admin/music-tracks/templated with:
  - audio:        TikTok-extracted m4a/mp3 (Hey babe, I love you from the moon)
  - asset_files:  TikTok thumbnail (used for slot 1)
  - recipe_json:  2-slot recipe — slot 1 fixed image with Ken Burns,
                  slot 2 user upload (video or image), no beat-snap.

Defaults assume the artifacts pre-extracted by yt-dlp at /tmp/tiktok-konna/
and a local API at http://localhost:8000. Override via env vars:

  API_URL=https://...      override the API base URL
  ADMIN_TOKEN=...          override the admin token
  AUDIO_PATH=/path/to.mp3  override the audio path
  THUMB_PATH=/path/to.jpg  override the thumbnail path
"""

import json
import os
import sys
from pathlib import Path

import urllib.error
import urllib.request

import http.client


def _read_env() -> dict[str, str]:
    """Read .env from repo root if it exists. Returns dict, never raises."""
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


def _build_recipe(audio_duration_s: float) -> dict:
    """Love-From-Moon recipe: slot 1 fixed image (4.23s — first beat after the
    spoken phrase ends), slot 2 user upload (cap at remainder; orchestrator
    shortens to the video's natural duration if shorter, audio mix trims to
    match)."""
    slot1_dur = 4.23
    slot2_dur = round(max(0.0, audio_duration_s - slot1_dur), 3)
    return {
        "shot_count": 2,
        "total_duration_s": round(audio_duration_s, 3),
        "hook_duration_s": slot1_dur,
        "audio_only": True,
        "creative_direction": "Spoken-quote intro with reveal — single user moment.",
        "color_grade": "none",
        "transition_style": "cut",
        "pacing_style": "deliberate",
        "sync_style": "fixed",
        "interstitials": [],
        "beat_timestamps_s": [],  # disable beat-snap
        "slots": [
            {
                "position": 1,
                "slot_type": "fixed_asset",
                "asset_kind": "image",
                # asset_gcs_path is filled in by the admin endpoint after upload.
                "target_duration_s": slot1_dur,
                "ken_burns": "zoom_in",
                "transition_in": "cut",
                "speed_factor": 1.0,
                "text_overlays": [],
            },
            {
                "position": 2,
                "slot_type": "user_upload",
                "accepts": ["video", "image"],
                "target_duration_s": slot2_dur,
                "ken_burns": "zoom_in",  # only used when user uploads an image
                "transition_in": "cut",
                "speed_factor": 1.0,
                "text_overlays": [],
            },
        ],
        "required_clips_min": 1,
        "required_clips_max": 1,
    }


def _probe_duration(path: str) -> float:
    """ffprobe duration in seconds. Raises on failure."""
    import subprocess
    res = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())


def _post_multipart(url: str, headers: dict, fields: dict, files: list) -> tuple[int, bytes]:
    """Tiny multipart POST without external deps. Returns (status, body)."""
    boundary = "----nova-seed-" + os.urandom(8).hex()
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
        **headers,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    env = _read_env()
    api_url = (
        os.environ.get("API_URL")
        or env.get("API_URL")
        or "http://localhost:8000"
    ).rstrip("/")
    admin_token = (
        os.environ.get("ADMIN_TOKEN")
        or env.get("ADMIN_API_KEY")
        or ""
    )
    if not admin_token:
        print("ERROR: ADMIN_TOKEN / ADMIN_API_KEY not set", file=sys.stderr)
        return 2

    audio_path = os.environ.get("AUDIO_PATH", "/tmp/tiktok-konna/7627553879604989207.mp3")
    thumb_path = os.environ.get("THUMB_PATH", "/tmp/tiktok-konna/7627553879604989207.jpeg")
    title = os.environ.get("TITLE", "Love From Moon")
    artist = os.environ.get("ARTIST", "Victor Glover · Artemis II")

    for label, p in [("audio", audio_path), ("thumb", thumb_path)]:
        if not os.path.exists(p):
            print(f"ERROR: {label} not found at {p}", file=sys.stderr)
            return 2

    duration = _probe_duration(audio_path)
    print(f"audio duration: {duration:.3f}s")

    recipe = _build_recipe(duration)
    audio_bytes = Path(audio_path).read_bytes()
    thumb_bytes = Path(thumb_path).read_bytes()
    audio_ext = Path(audio_path).suffix.lstrip(".") or "mp3"

    status, body = _post_multipart(
        f"{api_url}/admin/music-tracks/templated",
        headers={"X-Admin-Token": admin_token},
        fields={
            "recipe_json": json.dumps(recipe),
            "title": title,
            "artist": artist,
            "publish": "true",
        },
        files=[
            ("audio", f"audio.{audio_ext}", f"audio/{audio_ext}", audio_bytes),
            ("asset_files", "slot1.jpg", "image/jpeg", thumb_bytes),
        ],
    )

    print(f"HTTP {status}")
    try:
        decoded = json.loads(body.decode())
        print(json.dumps(decoded, indent=2))
    except Exception:
        print(body.decode(errors="replace"))

    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
