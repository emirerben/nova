"""Sound-effects audio pipeline module.

Mixes one or more timed sound-effect clips on top of a finished variant's audio
track using the proven adelay+amix+loudnorm FFmpeg primitive.

Audio approach:
- Base video at input 0 (stream-copied — no video re-encode).
- Each SFX clip as a separate -i input.
- Per clip: optional atrim, adelay={ms}|{ms} (places clip at at_s seconds),
  volume={gain}, then labelled output.
- All inputs merged with amix=inputs=N:duration=first:normalize=0.
- Final: loudnorm + aresample=48000 (mandatory — avoids AAC 96kHz pitch bug).
- Output: -c:v copy (video untouched), -c:a aac -b:a 192k.

Duration semantics: `duration=first` pins output audio length to the base video's
audio track. With -c:v copy the video length is fixed; a sound effect whose tail
extends past the video end is truncated rather than extending the container.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
Encoder policy: this module uses -c:v copy (no _encoding_args call), so the
tests/test_encoder_policy.py AST gate does not apply.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import structlog

from app import storage
from app.agents._schemas.sound_effect import (
    SoundEffectPlacement,
    validate_sfx_gcs_path,
)

log = structlog.get_logger()


class SoundEffectsError(Exception):
    """Raised when the SFX audio apply-pass fails unrecoverably."""


def build_sound_effects_command(
    base_video: str,
    effects: list[SoundEffectPlacement],
    effect_local_paths: list[str],
    output_path: str,
    mute_intervals: list[tuple[float, float]] | None = None,
) -> list[str]:
    """Build the ffmpeg command to mix sound effects into a video's audio track.

    Pure function — no I/O. Inputs are resolved local paths.
    `effect_local_paths[i]` is the local audio file for `effects[i]`.

    Filter graph (N effects):
      For each effect i (input i+1):
        [i+1:a]
          atrim=start:end (if trim set)
          asetpts=PTS-STARTPTS (after trim)
          adelay={ms}|{ms}  (place at at_s seconds)
          volume={gain}
        → [fx{i}]
      [0:a][fx0]..[fxN]amix=inputs=N+1:duration=first:normalize=0[mix]
      [mix]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]
      -map 0:v -c:v copy -map [aout] -c:a aac -b:a 192k
    """
    assert len(effects) == len(effect_local_paths), "effects and paths must be same length"
    assert effects, "build_sound_effects_command requires at least one effect"

    # One decoder input per unique asset. Repeated role hits reuse it through
    # asplit, avoiding N downloads/decoders for the same clean pop or whoosh.
    unique_paths = list(dict.fromkeys(effect_local_paths))
    inputs: list[str] = ["-i", base_video]
    for lp in unique_paths:
        inputs += ["-i", lp]

    filter_parts: list[str] = []
    fx_labels: list[str] = []
    path_to_input = {path: index + 1 for index, path in enumerate(unique_paths)}
    uses_by_path = {
        path: [index for index, candidate in enumerate(effect_local_paths) if candidate == path]
        for path in unique_paths
    }
    source_by_effect: dict[int, str] = {}
    for path_index, path in enumerate(unique_paths):
        input_index = path_to_input[path]
        uses = uses_by_path[path]
        if len(uses) == 1:
            source_by_effect[uses[0]] = f"[{input_index}:a]"
            continue
        labels = [f"sfxsrc{path_index}_{branch}" for branch in range(len(uses))]
        filter_parts.append(
            f"[{input_index}:a]asplit={len(uses)}" + "".join(f"[{label}]" for label in labels)
        )
        for effect_index, label in zip(uses, labels):
            source_by_effect[effect_index] = f"[{label}]"

    for i, (eff, _lp) in enumerate(zip(effects, effect_local_paths)):
        source = source_by_effect[i]
        ms = round(eff.at_s * 1000)
        label = f"fx{i}"

        chain_parts: list[str] = []

        # Trim within the source clip if requested.
        if eff.trim_start_s is not None or eff.trim_end_s is not None:
            ts = eff.trim_start_s or 0.0
            if eff.trim_end_s is not None:
                chain_parts.append(
                    f"{source}atrim=start={ts:.3f}:end={eff.trim_end_s:.3f}"
                    f",asetpts=PTS-STARTPTS"
                )
            else:
                chain_parts.append(f"{source}atrim=start={ts:.3f},asetpts=PTS-STARTPTS")
        else:
            chain_parts.append(f"{source}anull")

        # Place at timestamp via adelay (stereo: two identical values in ms).
        # Pad with silence so the clip doesn't cut short if it precedes at_s.
        chain_parts.append(f"adelay={ms}|{ms}")
        # Per-placement volume.
        chain_parts.append(f"volume={eff.gain:.4f}")
        for start_s, end_s in mute_intervals or []:
            chain_parts.append(f"volume=0:enable='between(t,{start_s:.6f},{end_s:.6f})'")

        # The output pad label attaches DIRECTLY to the last filter, with no
        # separating comma — "volume=1.0[fx0]", never "volume=1.0,[fx0]". A comma
        # makes ffmpeg parse "[fx0]" as a new (empty) filter → "Filter not found"
        # (rc=8). Joining the label as a chain element was the bug.
        filter_parts.append(",".join(chain_parts) + f"[{label}]")
        fx_labels.append(f"[{label}]")

    # Mix: base audio + all SFX streams.
    n_inputs = len(effects) + 1  # base + N effects
    mix_inputs = "[0:a]" + "".join(fx_labels)
    # CRITICAL: normalize=0 — default normalize=1 divides by sum-of-weights and
    # drops each stream ~6 dB. duration=first pins output length to the base
    # audio (video is stream-copied so the container length is fixed).
    filter_parts.append(f"{mix_inputs}amix=inputs={n_inputs}:duration=first:normalize=0[mix]")
    # Final loudness normalization + mandatory aresample (AAC 96kHz pitch bug).
    filter_parts.append("[mix]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

    cmd = [
        "ffmpeg",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        # Stream-copy video (no re-encode).
        "-map",
        "0:v",
        "-c:v",
        "copy",
        # Mixed audio output.
        "-map",
        "[aout]",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-y",
        output_path,
    ]
    return cmd


def apply_sound_effects(
    base_gcs_path: str,
    effects: list[SoundEffectPlacement],
    output_gcs_path: str,
    job_id: str | None = None,
    mute_intervals: list[tuple[float, float]] | None = None,
) -> str:
    """Download base variant, mix SFX into audio track, upload result.

    Returns the new signed output URL.

    Best-effort per effect: an effect with a missing/unreadable asset is dropped
    with a logged warning rather than failing the variant. If ALL effects fail to
    load the function raises SoundEffectsError.

    Args:
        base_gcs_path: GCS object key for the variant to mix into.
        effects: validated SoundEffectPlacement list.
        output_gcs_path: GCS key for the output (typically same as base, overwriting).
        job_id: for structured log context.
    """
    if not effects:
        raise SoundEffectsError("apply_sound_effects called with empty effects list")

    with tempfile.TemporaryDirectory(prefix="nova_sfx_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        storage.download_to_file(base_gcs_path, base_local)

        # Download each effect audio file.
        ready_effects: list[SoundEffectPlacement] = []
        local_paths: list[str] = []

        downloaded_by_path: dict[str, str] = {}
        for i, eff in enumerate(effects):
            try:
                validate_sfx_gcs_path(eff.src_gcs_path)
            except ValueError as exc:
                log.warning(
                    "sfx_invalid_path",
                    job_id=job_id,
                    effect_id=eff.id,
                    error=str(exc),
                )
                continue

            local = downloaded_by_path.get(eff.src_gcs_path)
            if local is None:
                ext = os.path.splitext(eff.src_gcs_path)[-1].lower() or ".mp3"
                local = os.path.join(tmpdir, f"sfx_{len(downloaded_by_path)}{ext}")
                try:
                    storage.download_to_file(eff.src_gcs_path, local)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "sfx_download_failed",
                        job_id=job_id,
                        effect_id=eff.id,
                        src=eff.src_gcs_path,
                        error=str(exc),
                    )
                    continue
                downloaded_by_path[eff.src_gcs_path] = local

            ready_effects.append(eff)
            local_paths.append(local)

        if not ready_effects:
            log.warning("sfx_all_effects_failed", job_id=job_id)
            raise SoundEffectsError("All SFX assets failed to load — skipping apply-pass")

        output_local = os.path.join(tmpdir, "output.mp4")
        cmd = build_sound_effects_command(
            base_local,
            ready_effects,
            local_paths,
            output_local,
            mute_intervals=mute_intervals,
        )
        log.info(
            "sfx_applying",
            job_id=job_id,
            effect_count=len(ready_effects),
            cmd_len=len(cmd),
        )
        result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
            raise SoundEffectsError(
                f"ffmpeg SFX mix failed (rc={result.returncode}): {stderr_tail}"
            )

        signed_url = storage.upload_public_read(
            output_local, output_gcs_path, content_type="video/mp4"
        )
        log.info("sfx_applied", job_id=job_id, gcs_path=output_gcs_path)
        return signed_url
