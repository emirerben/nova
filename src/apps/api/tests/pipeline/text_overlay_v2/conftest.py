"""Shared fixtures for the Layer-2 pipeline pure-logic tests."""

from __future__ import annotations

from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon


def make_detection(
    t: float,
    text: str,
    *,
    x_center: float = 0.5,
    y_center: float = 0.5,
    w: float = 0.2,
    h: float = 0.05,
    confidence: float = 1.0,
) -> FrameDetection:
    """Build a `FrameDetection` at the given normalized center + size.

    Returns an axis-aligned bbox so downstream tests can predict IoU and
    AABB-union outcomes without arithmetic gymnastics. The polygon is
    constructed clockwise from top-left to match the schema contract.
    """
    half_w = w / 2.0
    half_h = h / 2.0
    left = x_center - half_w
    right = x_center + half_w
    top = y_center - half_h
    bottom = y_center + half_h
    points = [(left, top), (right, top), (right, bottom), (left, bottom)]
    return FrameDetection(
        frame_t_s=t,
        text=text,
        polygon=OcrPolygon(points=points),
        confidence=confidence,
    )
