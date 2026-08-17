from __future__ import annotations

from PIL import Image, ImageDraw

from app.pipeline.canvas import Canvas
from app.pipeline.text_overlay_skia import _sequence_alpha_evidence


def _sequence(tmp_path, element_id: str, *, box=None, alpha: int = 255) -> dict:  # noqa: ANN001
    path = tmp_path / f"{element_id}_0000.png"
    image = Image.new("RGBA", (100, 200), (0, 0, 0, 0))
    if box is not None:
        ImageDraw.Draw(image).rectangle(box, fill=(255, 255, 255, alpha))
    image.save(path)
    return {
        "element_id": element_id,
        "pattern": str(tmp_path / f"{element_id}_%04d.png"),
        "first_frame": str(path),
        "n_frames": 1,
    }


def test_text_evidence_requires_nonempty_alpha_inside_safe_area(tmp_path) -> None:
    evidence = _sequence_alpha_evidence(
        [_sequence(tmp_path, "thought", box=(20, 40, 80, 100))],
        required_element_ids=["thought"],
        canvas=Canvas(100, 200),
    )
    assert evidence == [
        {
            "element_id": "thought",
            "visible": True,
            "peak_alpha": 255,
            "pixel_bounds": [20, 40, 81, 101],
            "sampled_frames": 1,
        }
    ]


def test_text_evidence_rejects_transparent_off_canvas_and_missing_layers(tmp_path) -> None:
    evidence = _sequence_alpha_evidence(
        [
            _sequence(tmp_path, "transparent", box=(20, 40, 80, 100), alpha=0),
            _sequence(tmp_path, "clipped", box=(0, 40, 80, 100)),
        ],
        required_element_ids=["transparent", "clipped", "missing"],
        canvas=Canvas(100, 200),
    )
    assert {row["element_id"]: row["visible"] for row in evidence} == {
        "transparent": False,
        "clipped": False,
        "missing": False,
    }


def test_strict_burn_accepts_and_forwards_normal_renderer_context(tmp_path, monkeypatch) -> None:
    from app.pipeline import text_overlay_skia

    probe = object()
    matte = object()
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        text_overlay_skia,
        "_validate_input_canvas",
        lambda _path, _canvas, *, input_probe=None: calls.update(probe=input_probe),
    )

    def render_sequences(_overlays, _tmpdir, **kwargs):
        calls["render_kwargs"] = kwargs
        return (
            [
                {
                    "element_id": "title",
                    "pattern": str(tmp_path / "missing_%d.png"),
                    "n_frames": 1,
                }
            ],
            None,
        )

    monkeypatch.setattr(text_overlay_skia, "render_text_overlay_sequences", render_sequences)
    monkeypatch.setattr(
        text_overlay_skia,
        "_sequence_alpha_evidence",
        lambda *_args, **_kwargs: [{"element_id": "title", "visible": True}],
    )
    monkeypatch.setattr(
        text_overlay_skia,
        "_ffmpeg_burn_pngs",
        lambda *_args, **kwargs: calls.update(ffmpeg_kwargs=kwargs),
    )

    evidence = text_overlay_skia.burn_text_overlays_skia_with_evidence(
        "base.mp4",
        [{"text": "Corfu"}],
        "final.mp4",
        str(tmp_path),
        required_element_ids=["title"],
        matte=matte,
        canvas=Canvas(100, 200),
        input_probe=probe,
    )

    assert evidence == [{"element_id": "title", "visible": True}]
    assert calls["probe"] is probe
    assert calls["render_kwargs"]["matte"] is matte
    assert calls["ffmpeg_kwargs"]["canvas"] == Canvas(100, 200)
