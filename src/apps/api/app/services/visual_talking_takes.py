"""KRI-1340: a talk-to-camera video attached through Visuals is one of the Talking edit's takes.

Çiğdem S3 Yorum1 (prod thread 855274e8, 2026-11-08): "There are 3 videos, two at home and one
in the car". Take A went in as footage, takes B and C landed in Visuals. Nothing listens to a
Visual's speech, so the render contract could not seat "two at home" and held every ask, and
"café" (said only in take C) was never heard. Emir: where the creator attached a talking video
must not matter.

So when a Visuals video's analysis lands and it says someone speaks to the camera, the server
attaches it as footage the way the phone would have: an analysis proxy plus a clip row whose
``pool_source`` tells the phone to fetch the take's bytes from the Visuals pool
(`app.tasks.visual_talking_takes`). From then on every reader of ``clip_assignments`` (planner,
render contract, clip names and order, readiness, cleanup, captions, photo-on-word grounding,
the phone job) sees a normal take. The creator's Visuals tray is left as it is; the planner no
longer lists the adopted Visual (``adopted_asset_ids``) so it is never also used as a cutaway.

This module is the rule only: pure functions over rows and stored analysis, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# The phone renderer calls a Talking clip a speaker from 6 heard words
# (`phone_talking_cutaways.classify_talking_clips`); fewer is never a take.
MIN_TAKE_WORDS = 6
# The proxy the server makes for an adopted take: the phone's own analysis proxy size.
PROXY_LONG_SIDE = 568
PROXY_MAX_FPS = 30
VISUAL_TAKE_PROXY_PREFIX = "analysis-proxy-visual-"

# Why a Visual stays a Visual (logged; never shown to the creator).
SKIP_SWITCH_OFF = "switch_off"
SKIP_NOT_TALKING = "not_talking_format"
SKIP_MULTI_CLIP_OFF = "multi_clip_off"
SKIP_VOICEOVER = "voiceover_recorded"
SKIP_NOT_VIDEO = "not_video"
SKIP_NOT_READY = "not_ready"
SKIP_NOT_TO_CAMERA = "not_to_camera"
SKIP_ADOPTED = "already_adopted"
SKIP_FULL = "talking_clip_cap"


def _words(text: object) -> int:
    return len(text.split()) if isinstance(text, str) else 0


def is_talking_take(analysis: object) -> bool:
    """A Visuals video whose stored analysis says someone talks to the camera.

    Reads clip understanding's ``speech`` block (`clip_understanding.understanding_payload`,
    the same Gemini analysis footage gets): ``to_camera`` and enough words to be a speaker.
    """
    if not isinstance(analysis, Mapping):
        return False
    understanding = analysis.get("understanding")
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    if not isinstance(speech, Mapping) or speech.get("to_camera") is not True:
        return False
    if speech.get("has_speech") is False:
        return False
    heard = max(_words(speech.get("transcript")), _words(analysis.get("transcript")))
    return heard >= MIN_TAKE_WORDS


def adopted_asset_ids(clip_assignments: object) -> set[str]:
    """The Visuals rows already adopted as takes: their ids from each row's ``pool_source``."""
    out: set[str] = set()
    for row in clip_assignments if isinstance(clip_assignments, list) else ():
        source = row.get("pool_source") if isinstance(row, Mapping) else None
        if isinstance(source, Mapping) and source.get("asset_id"):
            out.add(str(source["asset_id"]))
    return out


def without_adopted(assets: Iterable[Any], clip_assignments: object) -> list[Any]:
    """``assets`` (Visuals rows) minus the ones adopted as takes: what the planner lists."""
    adopted = adopted_asset_ids(clip_assignments)
    return [asset for asset in assets if str(getattr(asset, "id", "")) not in adopted]


def skip_reason(
    *,
    enabled: bool,
    edit_format: object,
    multi_clip_supported: bool,
    has_voiceover: bool,
    asset_kind: object,
    asset_status: object,
    analysis: object,
    asset_id: str,
    clip_assignments: object,
    max_clips: int,
) -> str | None:
    """Why this Visual must stay a Visual, or None when it is a take to adopt."""
    if not enabled:
        return SKIP_SWITCH_OFF
    if edit_format != "subtitled":
        return SKIP_NOT_TALKING
    if not multi_clip_supported:
        return SKIP_MULTI_CLIP_OFF
    if has_voiceover:
        return SKIP_VOICEOVER
    if asset_kind != "video":
        return SKIP_NOT_VIDEO
    if asset_status != "ready":
        return SKIP_NOT_READY
    if not is_talking_take(analysis):
        return SKIP_NOT_TO_CAMERA
    if asset_id in adopted_asset_ids(clip_assignments):
        return SKIP_ADOPTED
    clips = clip_assignments if isinstance(clip_assignments, list) else []
    if len(clips) >= max_clips:
        return SKIP_FULL
    return None


def same_bytes_as_a_clip(sha256: str, clip_assignments: object) -> bool:
    """The Visual is a copy of a take already attached as footage (it would play twice)."""
    for row in clip_assignments if isinstance(clip_assignments, list) else ():
        contract = row.get("upload_contract") if isinstance(row, Mapping) else None
        proxy = contract.get("proxy") if isinstance(contract, Mapping) else None
        original = proxy.get("original") if isinstance(proxy, Mapping) else None
        if isinstance(original, Mapping) and str(original.get("sha256") or "") == sha256:
            return True
    return False


def proxy_media_id(asset_id: str, pool_generation: str) -> str:
    """The adopted take's media id and proxy file name: one per Visual and pool generation."""
    return f"{VISUAL_TAKE_PROXY_PREFIX}{asset_id}-{pool_generation}.mp4"


def proxy_video_filter(source_fps: float) -> str:
    """ffmpeg ``-vf`` for the proxy: rotation already applied (ffmpeg autorotates), long side
    ``PROXY_LONG_SIDE`` with even sides, at most ``PROXY_MAX_FPS``."""
    side = PROXY_LONG_SIDE
    fps = min(float(PROXY_MAX_FPS), source_fps) if source_fps > 0 else float(PROXY_MAX_FPS)
    return f"scale='if(gt(iw,ih),{side},-2)':'if(gt(iw,ih),-2,{side})',fps={fps:g}"


def take_row(
    *,
    asset: Any,
    media_id: str,
    proxy_path: str,
    proxy_generation: str,
    proxy: Mapping[str, Any],
    original: Mapping[str, Any],
) -> dict[str, Any]:
    """The clip row for an adopted take: what `creation_threads._clip_assignment_row` writes for
    a phone upload, plus the Visual's own analysis and its ``pool_source``."""
    analysis = getattr(asset, "analysis", None)
    capture = getattr(asset, "capture", None)
    row: dict[str, Any] = {
        "gcs_path": proxy_path,
        "media_id": media_id,
        "kind": "video",
        "shot_id": None,
        "storage_generation": proxy_generation,
        "duration_s": float(proxy["duration_s"]),
        "has_audio": bool(original["has_audio"]),
        "manifest_identity": media_id,
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {**dict(proxy), "original": dict(original)},
        },
        "pool_source": {
            "asset_id": str(asset.id),
            "gcs_path": str(asset.gcs_path),
            "generation": str(asset.gcs_generation),
        },
    }
    if isinstance(analysis, Mapping) and analysis:
        row["analysis"] = dict(analysis)
    if name := getattr(asset, "source_filename", None):
        row["source_filename"] = str(name)
    if isinstance(capture, Mapping) and capture:
        row["capture"] = dict(capture)
    return row
