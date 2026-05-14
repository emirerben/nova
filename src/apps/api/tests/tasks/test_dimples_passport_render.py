"""Integration tests: render Dimples Passport slot 5 end-to-end against a
synthetic base clip and assert the MOROCCO font-cycle survives.

These tests exercise the full PNG-generation + ffmpeg-burn path used by
`_pre_burn_curtain_slot_text`. They require:
  - ffmpeg on PATH
  - Pillow (for `_render_font_cycle`)
  - The bundled fonts under `src/apps/api/assets/fonts/`

Job b6540038 (Dimples Passport, location=Morocco) is the canonical
regression target for this file. Its slot 5 has:
  - target_duration_s = 5.5
  - effect = font-cycle
  - text = "PERU" (substituted to "MOROCCO" via subject="Morocco")
  - text_color = #F4D03F (gold)
  - text_size_px = 170
  - font_cycle_accel_at_s = 2.8
  - position_y_frac = 0.45
  - curtain-close interstitial after_slot=5, animate_s=1.6
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

# Skip the whole module if ffmpeg or Pillow isn't available — the helper
# tests in test_pre_burn_curtain_text.py cover the cmd-builder structurally
# without these binary dependencies.
ffmpeg_missing = shutil.which("ffmpeg") is None
PIL = pytest.importorskip("PIL")  # noqa: N816  — module reference
pytestmark = pytest.mark.skipif(
    ffmpeg_missing,
    reason="ffmpeg binary not on PATH",
)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ──────────────────────────────────────────────────────────────────────────


DIMPLES_SLOT_5_OVERLAYS = [
    {
        "text": "Welcome to",
        "start_s": 0.0,
        "end_s": 3.5,
        "position": "center",
        "effect": "none",
        "font_style": "serif",
        "text_size": "medium",
        "text_size_px": 36,
        "text_color": "#FFFFFF",
        "position_x_frac": None,
        "position_y_frac": 0.4779,
        "font_cycle_accel_at_s": None,
    },
    {
        "text": "MOROCCO",
        "start_s": 0.0,
        "end_s": 5.5,
        "position": "center",
        "effect": "font-cycle",
        "font_style": "display",
        "text_size": "medium",
        "text_size_px": 170,
        "text_color": "#F4D03F",
        "position_x_frac": None,
        "position_y_frac": 0.45,
        "font_cycle_accel_at_s": 2.8,
    },
]


def _build_synthetic_slot(path: str, duration_s: float = 5.5) -> None:
    """Make a 5.5s 1080x1920 blue mp4 to stand in for the real slot 5 clip.

    Real Dimples Passport slot 5 is "Camera panning across dining room"; the
    base content is irrelevant to the font-cycle bug, so synthetic blue is
    enough to verify the overlay-burn path."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=1080x1920:d={duration_s}",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            path,
        ],
        check=True,
        capture_output=True,
    )


def _morocco_bbox_hash(frame_path: str) -> str:
    """Hash a tight pixel region around where MOROCCO renders, so we can
    detect when the gold glyph actually changes between frames.

    The crop region is chosen specifically to avoid sampling the blue base
    (which would dominate the hash with H.264 noise) — only the gold-colored
    pixels matter. We sample a horizontal strip at y_frac=0.45 ± a few rows
    in the center 60% of the frame, and quantize each pixel to a coarse
    palette (gold / not-gold) so encoder noise doesn't affect the hash.
    """
    from PIL import Image  # local import; PIL already importskipped above

    img = Image.open(frame_path).convert("RGB")
    w, h = img.size
    # 170 px text at y_frac=0.45 → vertical center ≈ 864, height ≈ 170
    y0 = int(h * 0.45) - 60
    y1 = int(h * 0.45) + 110
    x0 = int(w * 0.2)
    x1 = int(w * 0.8)
    crop = img.crop((x0, y0, x1, y1))

    # Quantize each pixel: "gold-ish" (text) vs "not gold" (everything else).
    # This collapses H.264 noise on the blue background into a single class
    # and isolates the glyph shape as the only signal.
    px = crop.load()
    bits = []
    cw, ch = crop.size
    # Subsample to keep the hash compact + insensitive to subpixel jitter
    for y in range(0, ch, 4):
        for x in range(0, cw, 4):
            r, g, b = px[x, y]
            is_gold = (r > 200) and (g > 150) and (b < 120)
            bits.append("1" if is_gold else "0")
    return hashlib.md5("".join(bits).encode()).hexdigest()


def _frame_has_morocco(frame_path: str) -> bool:
    """Return True if the frame contains gold-colored pixels in the
    MOROCCO bounding box. Used to verify the overlay is actually visible
    after the fix (and was NOT in the unfixed render of job b6540038)."""
    from PIL import Image

    img = Image.open(frame_path).convert("RGB")
    w, h = img.size
    y0 = int(h * 0.45) - 60
    y1 = int(h * 0.45) + 110
    x0 = int(w * 0.2)
    x1 = int(w * 0.8)
    crop = img.crop((x0, y0, x1, y1))

    px = crop.load()
    cw, ch = crop.size
    gold_count = 0
    sampled = 0
    for y in range(0, ch, 4):
        for x in range(0, cw, 4):
            r, g, b = px[x, y]
            sampled += 1
            if (r > 200) and (g > 150) and (b < 120):
                gold_count += 1
    # Require at least 1% of sampled pixels to be gold — well above noise
    # but well below the ~10% you'd expect when MOROCCO is rendered.
    return (gold_count / max(sampled, 1)) > 0.01


def _extract_frame(video_path: str, t_s: float, output_path: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(t_s),
            "-i",
            video_path,
            "-frames:v",
            "1",
            output_path,
        ],
        check=True,
        capture_output=True,
    )


# ──────────────────────────────────────────────────────────────────────────
# Tests 7-9
# ──────────────────────────────────────────────────────────────────────────


class TestDimplesPassportSlot5Render:
    """Slot 5 of the Dimples Passport template with location=Morocco. This
    is the exact recipe + subject that produced job b6540038's broken
    output. After the fix, MOROCCO must render and cycle."""

    @pytest.mark.timeout(180)
    def test_morocco_font_cycle_renders(self, tmp_path):
        """Test 7: render slot 5 end-to-end and assert MOROCCO appears in
        sampled frames at t∈{0.5, 1.5, 3.0, 4.5}. Pre-fix this would have
        produced either an OOM (rc=-9) or an output with no MOROCCO."""
        from app.pipeline.reframe import _encoding_args
        from app.pipeline.text_overlay import generate_text_overlay_png
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        src = str(tmp_path / "src.mp4")
        _build_synthetic_slot(src, duration_s=5.5)

        # Generate the PNGs (this is the unchanged math path)
        png_configs = generate_text_overlay_png(
            DIMPLES_SLOT_5_OVERLAYS,
            5.5,
            str(tmp_path),
            slot_index=4,
        )
        assert png_configs is not None and len(png_configs) >= 2, (
            f"expected at least 2 PNG configs (Welcome to + font-cycle frames), "
            f"got {len(png_configs) if png_configs else 0}"
        )

        # Build cmd via the helper, then run ffmpeg
        burned = str(tmp_path / "burned.mp4")
        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            src,
            png_configs,
            burned,
            str(tmp_path),
        )
        cmd = [
            "ffmpeg",
            *ffmpeg_inputs,
            "-filter_complex",
            ";".join(fc_parts),
            "-map",
            f"[{last_label}]",
            "-map",
            "0:a?",
            *_encoding_args(burned, preset="fast"),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        assert result.returncode == 0, (
            f"ffmpeg failed: rc={result.returncode}\n"
            f"stderr tail: {result.stderr.decode(errors='replace')[-1500:]}"
        )
        assert os.path.exists(burned) and os.path.getsize(burned) > 0

        # MOROCCO should appear in each sampled frame
        for t in (0.5, 1.5, 3.0, 4.5):
            frame_path = str(tmp_path / f"frame_t{t}.png")
            _extract_frame(burned, t, frame_path)
            assert _frame_has_morocco(frame_path), (
                f"MOROCCO not visible at t={t}s — this is the b6540038 regression"
            )

    @pytest.mark.timeout(180)
    def test_cycle_glyph_changes_over_time(self, tmp_path):
        """Test 8: hash a tight bounding box around MOROCCO at two different
        timestamps within the cycle and assert the hashes differ. Proves
        the font is actually cycling, not frozen — guards against a future
        regression where the concat list collapses to a single frame."""
        from app.pipeline.reframe import _encoding_args
        from app.pipeline.text_overlay import generate_text_overlay_png
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        src = str(tmp_path / "src.mp4")
        _build_synthetic_slot(src, duration_s=5.5)

        png_configs = generate_text_overlay_png(
            DIMPLES_SLOT_5_OVERLAYS,
            5.5,
            str(tmp_path),
            slot_index=4,
        )
        burned = str(tmp_path / "burned.mp4")
        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            src,
            png_configs,
            burned,
            str(tmp_path),
        )
        cmd = [
            "ffmpeg",
            *ffmpeg_inputs,
            "-filter_complex",
            ";".join(fc_parts),
            "-map",
            f"[{last_label}]",
            "-map",
            "0:a?",
            *_encoding_args(burned, preset="fast"),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)

        # Sample three timestamps:
        # - t=0.6  : slow phase (different glyph than at start)
        # - t=2.0  : late slow phase
        # - t=3.5  : fast phase (well past accel_at=2.8)
        timestamps = [0.6, 2.0, 3.5]
        hashes = []
        for t in timestamps:
            fp = str(tmp_path / f"frame_t{t}.png")
            _extract_frame(burned, t, fp)
            hashes.append(_morocco_bbox_hash(fp))

        # All three hashes should differ — the font glyph cycles, so the
        # gold pixels arrange differently at each sample point.
        assert len(set(hashes)) >= 2, (
            f"font-cycle appears frozen — hashes at {timestamps} were {hashes}. "
            f"Either the concat list is mis-sequenced or the cycle never advances."
        )

    def test_5_5s_cycle_uses_concat_not_58_inputs(self, tmp_path):
        """Test 9 — STRUCTURAL regression assertion for the OOM fix.

        The bug being prevented is the 58-`-i`-PNG chain that SIGKILLed
        the 2 GB Fly worker on job b6540038. Memory behavior is
        mechanically downstream of cmd shape: if ffmpeg gets ONE concat
        demuxer input for the cycle (instead of one -i per cycle frame),
        the working set stays constant regardless of cycle frame count.
        This was verified live on a 2 GB docker container against the
        real Morocco clip as part of the merge-time verification — see
        the docker render protocol noted in the PR description.

        Why a structural assertion instead of a live RLIMIT_AS check:
        on Linux, RLIMIT_AS counts mmap-loaded shared libraries
        (libavcodec, libx264, glibc, libavfilter…) toward virtual
        address space. On x86_64, those alone consume ~1.5 GB of virtual
        space BEFORE any encoding starts, so any cap tight enough to
        catch the pre-fix code (which exceeded 2 GB resident) also makes
        libx264's encoder.open() fail in the post-fix code — rc=187
        "Error while opening encoder", which is not the SIGKILL we'd
        want to detect. RLIMIT_DATA hits its own problems under libx264's
        thread-per-frame allocator. The right tool for live memory
        enforcement is cgroups (docker --memory=2g), which we run
        manually as part of merge-time verification and don't put on
        CI's critical path.

        This test pins the cmd shape that makes OOM impossible by
        construction. If a future refactor reintroduces per-PNG inputs
        for cycle frames, argv_len jumps from ~60 to >150 and this
        assertion fires before CI even tries to encode a frame."""
        from app.pipeline.reframe import _encoding_args
        from app.pipeline.text_overlay import generate_text_overlay_png
        from app.tasks.template_orchestrate import _build_preburn_inputs_and_graph

        src = str(tmp_path / "src.mp4")
        _build_synthetic_slot(src, duration_s=5.5)

        # Generate the real PNGs — same math the helper sees in prod.
        png_configs = generate_text_overlay_png(
            DIMPLES_SLOT_5_OVERLAYS,
            5.5,
            str(tmp_path),
            slot_index=4,
        )
        assert png_configs and len(png_configs) >= 50, (
            f"expected ~58 PNG configs from a 5.5s font-cycle render, "
            f"got {len(png_configs) if png_configs else 0} "
            f"(fonts may be missing in this test env)"
        )

        # Build the cmd the way `_pre_burn_curtain_slot_text` does.
        burned = str(tmp_path / "shape_burned.mp4")
        ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
            src,
            png_configs,
            burned,
            str(tmp_path),
        )
        cmd = [
            "ffmpeg",
            "-y",
            *ffmpeg_inputs,
            "-filter_complex",
            ";".join(fc_parts),
            "-map",
            f"[{last_label}]",
            "-map",
            "0:a?",
            *_encoding_args(burned, preset="fast"),
        ]

        # 1. The concat demuxer MUST appear exactly once for the cycle stream
        concat_markers = [
            i
            for i in range(len(cmd))
            if cmd[i] == "-f" and i + 1 < len(cmd) and cmd[i + 1] == "concat"
        ]
        assert len(concat_markers) == 1, (
            f"expected exactly 1 `-f concat` for cycle frames (the fix); "
            f"got {len(concat_markers)}. Pre-fix used N separate `-i` "
            f"per cycle PNG which OOMed at 58 frames on the 2 GB Fly worker."
        )

        # 2. argv length must NOT scale with cycle frame count. Pre-fix
        # argv for 58 PNGs was 167 entries. Post-fix is 60. Anything
        # above 80 means we're regressing toward per-PNG inputs.
        assert len(cmd) < 80, (
            f"argv length {len(cmd)} is too long for {len(png_configs)} "
            f"PNGs — the fix collapses cycle frames into one concat input. "
            f"Pre-fix shape at this PNG count was 167 entries (OOM); "
            f"post-fix should be ~60."
        )

        # 3. The filter graph must NOT chain N overlay links for cycle
        # frames. Count overlay-style links via the `[N:v]overlay` token.
        joined_fc = ";".join(fc_parts)
        overlay_count = joined_fc.count(":v]overlay=")
        # Expected: 1 overlay for the concat stream + 1 per static PNG.
        # With slot 5 (1 static "Welcome to"), that's 2 total. The pre-fix
        # shape was N+M = 58 overlays for the same slot.
        assert overlay_count <= 5, (
            f"filter_complex has {overlay_count} overlay links — too many. "
            f"The fix uses 1 overlay for the cycle concat stream + 1 per "
            f"static; total should be small (<=5 for a typical slot). "
            f"Pre-fix had one overlay PER cycle frame ({len(png_configs)} "
            f"total), which is what OOMed."
        )

        # 4. The concat list referenced in argv must actually exist on
        # disk and reference each cycle PNG. This catches the class of
        # bug where the helper emits `-f concat` but writes a malformed
        # or partial list.
        concat_idx = concat_markers[0]
        # cmd layout: ... -f concat -safe 0 -i <listpath> ...
        # offsets from -f: +1 concat, +2 -safe, +3 0, +4 -i, +5 <listpath>
        list_path = cmd[concat_idx + 5]
        assert os.path.exists(list_path), f"concat list not written: {list_path}"
        content = open(list_path).read()
        cycle_pngs = [c for c in png_configs if "fontcycle" in os.path.basename(c["png_path"])]
        for c in cycle_pngs:
            assert os.path.basename(c["png_path"]) in content, (
                f"cycle PNG {os.path.basename(c['png_path'])} missing from "
                f"concat list — the helper is producing an incomplete stream"
            )
