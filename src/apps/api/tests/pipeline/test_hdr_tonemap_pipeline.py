"""Regression tests for the shared HDR-to-SDR tonemap filter chain."""


def test_hdr_tonemap_forces_even_dimensions_before_subsampled_zscale() -> None:
    """Round both resized axes even before later zscale stages.

    Production job 6c6c27c4 received a 632×894 HLG clip. Fitting it inside
    the 1920² box produced 1357×1920, and the following subsampled zscale
    failed with libzimg code 1027 before the trailing crop could run.
    """
    from app.pipeline.reframe import _ZSCALE_SDR_PIPELINE

    pipeline = _ZSCALE_SDR_PIPELINE
    resize_idx = pipeline.index("scale=")
    even_idx = pipeline.index("force_divisible_by=2")
    float_idx = pipeline.index("format=gbrpf32le")
    subsampled_zscale_idx = pipeline.index("zscale=p=bt709")

    assert resize_idx < even_idx < float_idx < subsampled_zscale_idx, (
        "HDR resize must force even dimensions before float conversion "
        f"and subsampled zscale stages; got {pipeline!r}"
    )
