"""Unit tests for the upload-time 10-bit HDR pre-flight in
``app/services/template_validation.validate_clips_processable``.

Empirical justification for the guard:

  - Three jobs on 2026-05-17 (5f897cca @ 15:16, d1b9b9d8 @ 19:39,
    71d22917 @ 20:26) timed out on the same source clip — HEVC Main 10
    yuv420p10le, 1920×1080, 223s, 399 MB. PR #208 raised the
    subprocess timeout 300s → 600s and added a 2-permit re-encode
    semaphore; the third job still hit the new ceiling.
  - Uncontended normalize wall-time on Fly worker for that clip is
    110-328s. Under realistic 8-thread + 2-permit-semaphore load,
    wall time exceeds 600s. Code-level downscale to 720p was empirically
    tested and did NOT prevent timeout (901s TOTAL on the 15-clip
    batch).
  - 60s is the longest 10-bit clip the deployed pipeline is empirically
    proven to fit inside its own timeout (24s 10-bit clip processed
    in ~150s under semaphore; linear extrapolation gives 60s at ~375s).

These tests cover the boundary cases of the guard without spawning any
ffprobe or GCS calls — both ``probe_video`` and ``signed_get_url`` are
mocked.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.pipeline.probe import ProbeError, VideoProbe
from app.services.template_validation import (
    MAX_HDR_DURATION_S,
    _is_high_bit_pix_fmt,
    validate_clips_processable,
)


def _probe(duration_s: float, pix_fmt: str) -> VideoProbe:
    """Build a minimal VideoProbe with the two fields the guard cares about."""
    return VideoProbe(
        duration_s=duration_s,
        fps=30.0,
        width=1920,
        height=1080,
        has_audio=True,
        codec="hevc",
        aspect_ratio="16:9",
        file_size_bytes=100_000_000,
        pix_fmt=pix_fmt,
    )


@pytest.mark.asyncio
async def test_rejects_10bit_over_60s():
    """The exact failure mode that produced job 71d22917: 223s HEVC 10-bit."""
    with (
        patch(
            "app.services.template_validation.signed_get_url",
            return_value="https://signed.example/clip_008",
        ),
        patch(
            "app.services.template_validation.probe_video",
            return_value=_probe(duration_s=223.7, pix_fmt="yuv420p10le"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_clips_processable(["dev-user/abc/clip_008.MOV"])

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["code"] == "clip_too_long_for_10bit"
    assert detail["clip_index"] == 0
    assert detail["duration_s"] == 223.7
    assert detail["limit_s"] == MAX_HDR_DURATION_S
    assert detail["pix_fmt"] == "yuv420p10le"
    assert _is_high_bit_pix_fmt(detail["pix_fmt"])
    # Empirical remediation copy must appear so the user knows what to do.
    assert "trim" in detail["message"].lower()
    assert "8-bit" in detail["message"]
    assert detail["offenders"] == [{"clip_index": 0, "duration_s": 223.7, "pix_fmt": "yuv420p10le"}]


@pytest.mark.asyncio
async def test_accepts_10bit_under_60s():
    """A 24s 10-bit clip (matching clip_007 from the same batch) must NOT be rejected.
    Under the 2-permit semaphore that clip processed in ~150s in our sim — fits
    in the 600s budget with margin."""
    with (
        patch(
            "app.services.template_validation.signed_get_url",
            return_value="https://signed.example/clip_007",
        ),
        patch(
            "app.services.template_validation.probe_video",
            return_value=_probe(duration_s=24.2, pix_fmt="yuv420p10le"),
        ),
    ):
        await validate_clips_processable(["dev-user/abc/clip_007.MOV"])  # no raise


@pytest.mark.asyncio
async def test_accepts_8bit_over_60s():
    """An 8-bit clip of any reasonable duration must pass — 8-bit decode is
    significantly faster than 10-bit and the deployed pipeline can handle it.
    A 300s 8-bit HEVC clip is below the empirical cost cliff that motivated
    this guard."""
    with (
        patch(
            "app.services.template_validation.signed_get_url",
            return_value="https://signed.example/clip_8bit_long",
        ),
        patch(
            "app.services.template_validation.probe_video",
            return_value=_probe(duration_s=300.0, pix_fmt="yuv420p"),
        ),
    ):
        await validate_clips_processable(["dev-user/abc/clip_long_8bit.MP4"])  # no raise


@pytest.mark.asyncio
async def test_error_message_contains_remediation():
    """Surface the actual remediation steps in the user-visible message — not
    a generic 'job failed' string. This is the contract the frontend renders."""
    with (
        patch(
            "app.services.template_validation.signed_get_url",
            return_value="https://signed.example/big",
        ),
        patch(
            "app.services.template_validation.probe_video",
            return_value=_probe(duration_s=100.0, pix_fmt="yuv420p10le"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_clips_processable(["dev-user/abc/big.MOV"])

    msg = exc_info.value.detail["message"]
    # Must tell the user what failed AND what to do.
    assert "10-bit" in msg
    assert "trim" in msg.lower()
    assert "8-bit" in msg
    # And it must mention the budget so they know how short to trim.
    assert str(MAX_HDR_DURATION_S) in msg


@pytest.mark.asyncio
async def test_probe_failure_does_not_block_upload():
    """If a signed URL is briefly unreachable or ffprobe transient-fails on
    one clip, we MUST NOT reject the whole upload — the worker will probe
    again and surface the real error there. Over-rejecting on infra blips
    would be worse than letting the worker handle it."""
    with (
        patch(
            "app.services.template_validation.signed_get_url",
            return_value="https://signed.example/unreachable",
        ),
        patch(
            "app.services.template_validation.probe_video",
            side_effect=ProbeError("ffprobe failed (rc=1): connection reset"),
        ),
    ):
        await validate_clips_processable(["dev-user/abc/transient.MOV"])  # no raise


@pytest.mark.asyncio
async def test_multiple_offenders_all_surfaced():
    """If the user uploads two long 10-bit clips, the response must list both
    in `offenders` so the frontend can flag each one rather than playing
    whack-a-mole through three submissions."""
    probes_by_idx = {
        0: _probe(duration_s=10.0, pix_fmt="yuv420p"),  # fine
        1: _probe(duration_s=120.0, pix_fmt="yuv420p10le"),  # offender
        2: _probe(duration_s=200.0, pix_fmt="yuv420p10le"),  # offender
    }

    def fake_probe(url: str):
        # The mock signed_get_url tags the URL with the index suffix so we
        # can resolve back to the per-clip fixture.
        idx = int(url.rsplit("/", 1)[-1])
        return probes_by_idx[idx]

    def fake_signed_url(path: str, expiration_minutes: int = 5):
        idx = int(path.rsplit("_", 1)[-1].split(".")[0])
        return f"https://signed.example/{idx}"

    with (
        patch(
            "app.services.template_validation.signed_get_url",
            side_effect=fake_signed_url,
        ),
        patch(
            "app.services.template_validation.probe_video",
            side_effect=fake_probe,
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_clips_processable(
                [
                    "dev-user/abc/clip_0.MOV",
                    "dev-user/abc/clip_1.MOV",
                    "dev-user/abc/clip_2.MOV",
                ]
            )

    offenders = exc_info.value.detail["offenders"]
    assert {o["clip_index"] for o in offenders} == {1, 2}
    # Headline error points at the lowest-index offender so the frontend
    # has a deterministic primary target to focus on.
    assert exc_info.value.detail["clip_index"] == 1


@pytest.mark.asyncio
async def test_empty_paths_is_noop():
    """Defensive: an empty paths list must not raise. The pydantic schema
    rejects this upstream but the validator should not depend on that to
    avoid an IndexError if called from another caller."""
    await validate_clips_processable([])  # no raise


@pytest.mark.asyncio
async def test_signed_url_failure_does_not_block_upload():
    """GCS auth or DNS hiccup → can't generate signed URL → don't reject the
    upload. The worker will fail on download anyway and surface the real
    error there."""
    with patch(
        "app.services.template_validation.signed_get_url",
        side_effect=RuntimeError("boom"),
    ):
        await validate_clips_processable(["dev-user/abc/anything.MOV"])  # no raise


@pytest.mark.asyncio
async def test_under_load_subprocess_errors_do_not_block_upload():
    """Under fork rate-limits / FD exhaustion / memory pressure, subprocess
    operations can raise BlockingIOError, OSError(EMFILE), MemoryError, etc.
    The narrow (ProbeError, ProbeTimeout) catch in the original implementation
    let these escape asyncio.gather and turned the whole POST into a 500 —
    the exact opposite of the validator's 'tolerate probe failures' contract.
    Broad-catch keeps the guard non-fatal."""
    with (
        patch(
            "app.services.template_validation.signed_get_url",
            return_value="https://signed.example/x",
        ),
        patch(
            "app.services.template_validation.probe_video",
            side_effect=BlockingIOError("fork rate-limited"),
        ),
    ):
        await validate_clips_processable(["dev-user/abc/load_test.MOV"])  # no raise


@pytest.mark.asyncio
async def test_kill_switch_disables_preflight():
    """When ORIENTATION_NORMALIZE_ENABLED=false, the cost cliff this preflight
    defends against isn't enforced — so the preflight MUST be a no-op too.
    Otherwise ops killing orientation locks users out of a path that would
    now succeed."""
    import os
    from unittest.mock import patch as _patch

    with (
        _patch.dict(os.environ, {"ORIENTATION_NORMALIZE_ENABLED": "false"}),
        patch(
            "app.services.template_validation.signed_get_url",
            side_effect=AssertionError(
                "signed_get_url must not be called when kill switch is active"
            ),
        ),
        patch(
            "app.services.template_validation.probe_video",
            side_effect=AssertionError("probe_video must not be called when kill switch is active"),
        ),
    ):
        # A 10-bit + > 60s clip that WOULD normally be rejected.
        await validate_clips_processable(["dev-user/abc/big_10bit.MOV"])  # no raise


def test_pix_fmt_pattern_match_covers_10bit_12bit_16bit():
    """Bypass-resistance: the bit-depth check must cover every 10-bit / 12-bit
    / 16-bit format an adversary can produce, not just the canonical
    yuv420p10le from iPhone HLG."""
    # 10-bit family — the one we originally tested + the ones an adversary
    # might re-export with to bypass an exact-match check.
    assert _is_high_bit_pix_fmt("yuv420p10le")  # iPhone HLG (the failing case)
    assert _is_high_bit_pix_fmt("yuv422p10le")  # ProRes 422
    assert _is_high_bit_pix_fmt("yuv444p10le")  # ProRes 4444
    assert _is_high_bit_pix_fmt("yuv420p10be")  # big-endian variant
    # 12-bit family
    assert _is_high_bit_pix_fmt("yuv420p12le")
    assert _is_high_bit_pix_fmt("yuv444p12le")
    # NVENC / QSV / VAAPI hardware encoder output
    assert _is_high_bit_pix_fmt("p010le")
    assert _is_high_bit_pix_fmt("p012le")
    assert _is_high_bit_pix_fmt("p016le")
    # 16-bit (exotic but supported by ffmpeg)
    assert _is_high_bit_pix_fmt("yuv420p16le")
    # 8-bit must NOT match — these are the formats we serve cheaply.
    assert not _is_high_bit_pix_fmt("yuv420p")
    assert not _is_high_bit_pix_fmt("yuvj420p")
    assert not _is_high_bit_pix_fmt("nv12")
    assert not _is_high_bit_pix_fmt("rgb24")
    assert not _is_high_bit_pix_fmt("")
