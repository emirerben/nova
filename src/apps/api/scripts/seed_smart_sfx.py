#!/usr/bin/env python3
"""Seed Nova's first clean, voice-free Smart sound-design role library.

Assets are generated procedurally with FFmpeg, so provenance and licensing are
unambiguous. Run after migration 0065 from ``src/apps/api``:

    python scripts/seed_smart_sfx.py
"""

from __future__ import annotations

import datetime
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.database import sync_session
from app.models import SoundEffect
from app.services.audio_download import probe_duration
from app.services.sfx_analysis import analyze_sound_effect
from app.storage import upload_public_read

_UTC = datetime.UTC
_RECIPES = {
    "visual_enter_soft": (
        "Smart soft pop",
        "sine=frequency=760:duration=0.18:sample_rate=48000",
        "afade=t=out:st=0.025:d=0.155,volume=0.34",
    ),
    "visual_enter_accent": (
        "Smart accent pop",
        "sine=frequency=1040:duration=0.22:sample_rate=48000",
        "afade=t=out:st=0.035:d=0.185,volume=0.46",
    ),
    "chapter_number_pop": (
        "Smart number pop",
        "sine=frequency=920:duration=0.16:sample_rate=48000",
        "afade=t=out:st=0.02:d=0.14,volume=0.42",
    ),
    "keyword_typewriter_tick": (
        "Smart keyboard tick",
        "sine=frequency=1840:duration=0.045:sample_rate=48000",
        "afade=t=out:st=0.005:d=0.04,volume=0.22",
    ),
    "transition_whip": (
        "Smart clean whoosh",
        "anoisesrc=color=pink:duration=0.42:sample_rate=48000",
        "highpass=f=260,lowpass=f=6200,afade=t=in:d=0.08,afade=t=out:st=0.18:d=0.24,volume=0.32",
    ),
    "badge_enter": (
        "Smart badge click",
        "sine=frequency=1320:duration=0.075:sample_rate=48000",
        "afade=t=out:st=0.01:d=0.065,volume=0.26",
    ),
    "cta_click": (
        "Smart CTA click",
        "sine=frequency=1160:duration=0.095:sample_rate=48000",
        "afade=t=out:st=0.012:d=0.083,volume=0.30",
    ),
}


def _render(source: str, audio_filter: str, output: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            source,
            "-af",
            audio_filter,
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"FFmpeg seed failed: {detail}")


def main() -> None:
    now = datetime.datetime.now(_UTC)
    with tempfile.TemporaryDirectory(prefix="nova_smart_sfx_seed_") as tmpdir:
        with sync_session() as db:
            for role, (name, source, audio_filter) in _RECIPES.items():
                effect_id = f"smart-{role.replace('_', '-')}-v1"
                local = Path(tmpdir) / f"{effect_id}.wav"
                _render(source, audio_filter, local)
                gcs_path = f"sound-effects/{effect_id}/audio.wav"
                upload_public_read(str(local), gcs_path, content_type="audio/wav")
                effect = db.execute(
                    select(SoundEffect).where(SoundEffect.id == effect_id)
                ).scalar_one_or_none()
                if effect is None:
                    effect = SoundEffect(id=effect_id, name=name)
                    db.add(effect)
                analysis = analyze_sound_effect(str(local))
                effect.name = name
                effect.audio_gcs_path = gcs_path
                effect.duration_s = probe_duration(str(local))
                effect.status = "ready"
                effect.source_filename = local.name
                effect.role_tags = [role]
                effect.contains_voice = False
                effect.vocal_probability = 0.0
                effect.provenance = f"procedural:ffmpeg:{role}:v1"
                effect.license = "project-owned"
                effect.quality_tier = "core"
                effect.manual_audit_status = "approved"
                effect.published_at = effect.published_at or now
                effect.archived_at = None
                for field, value in analysis.items():
                    setattr(effect, field, value)
            db.commit()


if __name__ == "__main__":
    main()
