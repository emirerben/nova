"""Regression tests for the shared HDR-to-SDR tonemap filter chain."""

import re


def test_hdr_tonemap_pins_explicit_desat_not_ffmpeg_default() -> None:
    """The `tonemap` stage must always carry an explicit `desat=`.

    FFmpeg's `tonemap` filter defaults desat=2, a highlight-desaturation knee
    that bleaches bright+saturated pixels (e.g. a sunlit sky) toward white.
    Without an explicit value, a future edit to _ZSCALE_SDR_PIPELINE could
    silently drop back to that default and reintroduce the bleached-sky
    regression (job 2cfb57f1, clip IMG_5169.MOV: sky blue chroma dropped from
    139.5 to 130.9 while luma held steady at ~204).
    """
    from app.config import settings
    from app.pipeline.reframe import _ZSCALE_SDR_PIPELINE

    match = re.search(r"tonemap=tonemap=mobius:desat=([0-9.]+)", _ZSCALE_SDR_PIPELINE)
    assert match is not None, (
        f"tonemap stage must pin desat= explicitly; got {_ZSCALE_SDR_PIPELINE!r}"
    )
    assert float(match.group(1)) == settings.hdr_tonemap_desat


def test_hdr_tonemap_desat_defaults_to_zero_disabling_highlight_bleach() -> None:
    """Default must disable the desaturation knee, not restore FFmpeg's `2`.

    `2.0` is the documented rollback value (byte-identical to pre-fix
    behavior) for `fly secrets set HDR_TONEMAP_DESAT=2`, not the default.
    """
    from app.config import Settings

    assert Settings.model_fields["hdr_tonemap_desat"].default == 0.0


def test_hdr_tonemap_forces_even_dimensions_before_subsampled_zscale() -> None:
    """Round both resized axes even before later zscale stages.

    Production job 6c6c27c4 received a 632×894 HLG clip. Fitting it inside
    the 1920² box produced 1357×1920, and the following subsampled zscale
    failed with libzimg code 1027 before the trailing crop could run.
    """
    from app.pipeline.reframe import _ZSCALE_SDR_PIPELINE

    pipeline = _ZSCALE_SDR_PIPELINE
    resize_idx = pipeline.index(",scale=")
    even_idx = pipeline.index("force_divisible_by=2")
    float_idx = pipeline.index("format=gbrpf32le")
    subsampled_zscale_idx = pipeline.index("zscale=p=bt709")

    assert resize_idx < even_idx < float_idx < subsampled_zscale_idx, (
        "HDR resize must force even dimensions before float conversion "
        f"and subsampled zscale stages; got {pipeline!r}"
    )
