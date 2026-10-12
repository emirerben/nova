"""Post-wide size + per-photo Fit (KRI-606), rendered through real FFmpeg.

Failure modes this file guards (written before the code):
 F1  a default draft changes a cache key / digest / serialized form -> mass re-render
 F2  an older client's save wipes a size or Fit it cannot see
 F2b the web editor's legacy-text merge drops a stored Fit
 F4  a size/Fit renders at the wrong dimensions, stretches the photo, or the
     preview concat rejects the new canvas
 F4b the blur graph breaks inside the full filter_complex (look + text + watermark)
 F5  an export rendered by a worker that predates sizes reads as current
 F6  an invalid size is persisted (Instagram 9:16, a video slide with Fit)
 F8  Swift and Python drift apart on the size table / Fit placement
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.config import settings
from app.kria.framing import fit_placement
from app.pipeline.slide_post import profiles
from app.pipeline.slide_post.build import (
    FIT_BLUR_SIGMA_PX,
    _frame_filter,
    build_post_manifest,
    concat_preview_segments,
    edits_cache_digest,
    normalize_image_slide,
    probe_dimensions,
    render_preview_segment,
    slide_canvas,
)
from app.schemas.slide_post import (
    SlideEdits,
    SlideFraming,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
    merge_legacy_text_edits,
    merge_stored_edit_key,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
FIXTURE = (
    Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "slide-framing-parity" / "v1.json"
)
ASSET = "00000000-0000-0000-0000-000000000001"
RED, BLUE = (230, 20, 20), (20, 20, 230)


def _near(pixel: tuple[int, ...], expected: tuple[int, int, int], tol: int = 70) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(pixel[:3], expected, strict=True))


def _wide_photo(path: Path, width: int = 800, height: int = 400) -> None:
    """Left half red, right half blue: a landscape photo, so a portrait/square canvas
    has to either trim its sides (fill) or show bars above and below (fit)."""
    img = Image.new("RGB", (width, height))
    img.paste(RED, (0, 0, width // 2, height))
    img.paste(BLUE, (width // 2, 0, width, height))
    img.save(path)


def _slide(slide_id: str, edits: SlideEdits | None = None, kind: str = "image") -> SlideRef:
    return SlideRef(id=slide_id, asset_id=ASSET, kind=kind, edits=edits)


def _draft(profile: str = "tiktok_photo", **kwargs) -> SlidePostDraft:
    return SlidePostDraft(
        platform_profile=profile, slides=kwargs.pop("slides", [_slide("a")]), **kwargs
    )


# ── F1: defaults are byte-identical ──────────────────────────────────────────


class TestDefaultsAreUnchanged:
    def test_cache_digests_are_pinned(self):
        # The same pins test_slide_post_framing.py holds: a size/Fit field that leaked
        # into the serialized form would move these and re-render every cached slide.
        assert edits_cache_digest(SlideEdits()) == "a268757d51d16368"
        assert edits_cache_digest(SlideEdits(look_preset="golden_hour")) == "86380fdd5a4ec3aa"

    def test_unset_fit_and_aspect_are_not_serialized(self):
        assert "fit" not in SlideEdits().model_dump()
        assert "fit" not in json.loads(SlideEdits().model_dump_json())
        assert "aspect" not in _draft().model_dump(mode="json")
        assert "aspect" not in _draft(aspect=None).model_dump(mode="json")

    def test_fit_changes_the_digest(self):
        assert edits_cache_digest(SlideEdits(fit="blur")) != edits_cache_digest(SlideEdits())
        assert edits_cache_digest(SlideEdits(fit="blur")) != edits_cache_digest(
            SlideEdits(fit="white")
        )

    def test_fill_filter_is_the_historical_string(self):
        assert (
            _frame_filter((1080, 1920), None, None)
            == "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        )

    def test_the_default_size_is_normalized_away(self):
        # Choosing the profile's own default must equal never choosing: same dump.
        assert _draft("tiktok_photo", aspect="9:16").aspect is None
        assert _draft("instagram_carousel", aspect="4:5").aspect is None
        assert _draft("instagram_carousel", aspect="1:1").model_dump(mode="json")["aspect"] == "1:1"

    def test_manifest_only_names_a_non_default_size(self):
        base = {"platform_profile": "tiktok_photo", "caption": "", "cover_index": 0, "slides": []}
        assert "aspect" not in build_post_manifest(**base)
        assert build_post_manifest(**base, aspect="1:1")["aspect"] == "1:1"


# ── F6: invalid sizes never persist ──────────────────────────────────────────


class TestSizeValidation:
    def test_every_size_is_even_and_matches_its_ratio(self):
        for name, (w, h) in profiles.ASPECT_CANVASES.items():
            assert w % 2 == 0 and h % 2 == 0, name
            a, b = (float(x) for x in name.split(":"))
            assert w / h == pytest.approx(a / b, abs=0.003), name

    def test_instagram_has_no_full_screen_size(self):
        assert _draft("instagram_carousel", aspect="9:16").aspect is None
        assert profiles.post_canvas("instagram_carousel", "9:16") == (1080, 1350)

    def test_tiktok_has_no_wide_size(self):
        assert _draft("tiktok_photo", aspect="1.91:1").aspect is None

    def test_shared_sizes_are_allowed_on_both_platforms(self):
        for size in profiles.SHARED_ASPECTS:
            for profile in ("tiktok_photo", "instagram_carousel"):
                assert size in profiles.PLATFORM_PROFILES[profile].allowed_aspects

    def test_switching_platform_falls_back_to_that_platforms_default(self):
        draft = _draft("instagram_carousel", aspect="1.91:1")
        assert draft.aspect == "1.91:1"
        switched = SlidePostDraft.model_validate(
            {**draft.model_dump(mode="python"), "platform_profile": "tiktok_photo"}
        )
        assert switched.aspect is None
        assert profiles.post_canvas("tiktok_photo", switched.aspect) == (1080, 1920)

    def test_unknown_size_is_rejected_at_the_schema(self):
        with pytest.raises(ValueError, match="aspect"):
            _draft(aspect="7:3")  # type: ignore[arg-type]

    def test_a_video_slide_never_carries_fit(self):
        draft = _draft(
            "instagram_carousel",
            slides=[
                _slide("a", SlideEdits(fit="blur")),
                _slide("b", SlideEdits(fit="blur", look_preset="golden_hour"), kind="video"),
            ],
        )
        assert draft.slides[0].edits.fit == "blur"
        assert draft.slides[1].edits.fit is None
        assert draft.slides[1].edits.look_preset == "golden_hour"

    def test_landscape_slide_keeps_its_own_canvas_inside_a_sized_post(self):
        edits = SlideEdits(framing=SlideFraming(orientation="landscape"))
        assert slide_canvas("tiktok_photo", edits, "1:1") == (1920, 1080)
        assert slide_canvas("tiktok_photo", SlideEdits(), "1:1") == (1080, 1080)
        assert slide_canvas("tiktok_photo", None, None) == (1080, 1920)
        assert slide_canvas("instagram_carousel", edits, "3:4") == (1080, 1440)


# ── F4: real renders ─────────────────────────────────────────────────────────

_SIZES = [
    ("tiktok_photo", "9:16"),
    ("tiktok_photo", "3:4"),
    ("tiktok_photo", "4:5"),
    ("tiktok_photo", "1:1"),
    ("instagram_carousel", "4:5"),
    ("instagram_carousel", "3:4"),
    ("instagram_carousel", "1:1"),
    ("instagram_carousel", "1.91:1"),
]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestFitRender:
    @pytest.mark.parametrize(("profile", "size"), _SIZES)
    @pytest.mark.parametrize("fit", [None, "blur", "black", "white"])
    def test_every_size_and_fit_renders_at_the_post_canvas(self, tmp_path, profile, size, fit):
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _wide_photo(src)
        edits = SlideEdits(fit=fit) if fit else None
        canvas = slide_canvas(profile, edits, size)
        assert canvas == profiles.ASPECT_CANVASES[size]
        normalize_image_slide(str(src), str(out), canvas=canvas, edits=edits)
        assert probe_dimensions(str(out)) == canvas
        # The preview stitch must accept every size as a single canvas.
        seg = tmp_path / "seg.mp4"
        render_preview_segment(
            str(out), str(seg), kind="image", canvas=canvas, source_canvas=canvas, hold_s=0.4
        )
        joined = tmp_path / "joined.mp4"
        concat_preview_segments([str(seg), str(seg)], str(joined), canvas=canvas)
        assert probe_dimensions(str(joined)) == canvas

    @pytest.mark.parametrize(("fit", "bar"), [("black", (0, 0, 0)), ("white", (255, 255, 255))])
    def test_solid_bars_sit_above_and_below_the_whole_photo(self, tmp_path, fit, bar):
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _wide_photo(src)  # 2:1 into 1:1 -> 1080x540 photo, 270px bars top and bottom
        normalize_image_slide(str(src), str(out), canvas=(1080, 1080), edits=SlideEdits(fit=fit))
        with Image.open(out) as img:
            rgb = img.convert("RGB")
            assert _near(rgb.getpixel((540, 100)), bar, 25)  # top bar
            assert _near(rgb.getpixel((540, 980)), bar, 25)  # bottom bar
            # Nothing trimmed: BOTH halves of the photo are visible, undistorted.
            assert _near(rgb.getpixel((200, 400)), RED)
            assert _near(rgb.getpixel((900, 400)), BLUE)
            assert _near(rgb.getpixel((200, 700)), RED)

    def test_fill_trims_the_photo_where_fit_shows_it_whole(self, tmp_path):
        src, fill, fit = tmp_path / "s.png", tmp_path / "fill.jpg", tmp_path / "fit.jpg"
        _wide_photo(src)
        normalize_image_slide(str(src), str(fill), canvas=(1080, 1350))
        normalize_image_slide(
            str(src), str(fit), canvas=(1080, 1350), edits=SlideEdits(fit="black")
        )
        with Image.open(fill) as a, Image.open(fit) as b:
            # Fill covers the top edge with the photo; fit leaves a bar there.
            assert max(a.convert("RGB").getpixel((540, 60))) > 100
            assert max(b.convert("RGB").getpixel((540, 60))) < 25

    def test_blur_bars_continue_the_photo_colours(self, tmp_path):
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _wide_photo(src)
        normalize_image_slide(str(src), str(out), canvas=(1080, 1080), edits=SlideEdits(fit="blur"))
        with Image.open(out) as img:
            rgb = img.convert("RGB")
            # The bar is a soft copy of the photo: red on the left, blue on the right,
            # neither black nor one flat colour.
            assert _near(rgb.getpixel((80, 100)), RED, 110)
            assert _near(rgb.getpixel((1000, 100)), BLUE, 110)
            assert rgb.getpixel((80, 100)) != rgb.getpixel((1000, 100))
            assert max(rgb.getpixel((80, 100))) > 80
            # And the sharp photo is still centred and whole.
            assert _near(rgb.getpixel((200, 540)), RED)
            assert _near(rgb.getpixel((900, 540)), BLUE)

    def test_fit_applies_the_crop_first(self, tmp_path):
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _wide_photo(src)
        # Keep only the red half (a 1:1 region of the 2:1 photo) then fit it in 4:5.
        crop = {"x": 0, "y": 0, "width": 0.5, "height": 1.0}
        edits = SlideEdits(fit="black", framing=SlideFraming(source_crop=crop))
        normalize_image_slide(str(src), str(out), canvas=(1080, 1350), edits=edits)
        with Image.open(out) as img:
            rgb = img.convert("RGB")
            assert _near(rgb.getpixel((540, 675)), RED)  # the cropped region
            assert _near(rgb.getpixel((1000, 675)), RED)  # a square region fills the width
            assert max(rgb.getpixel((540, 100))) < 25  # bar above it: not the blue half

    def test_blur_survives_the_full_filter_complex(self, tmp_path, monkeypatch):
        # F4b: the blur graph is spliced into `[0:v]{vf}[b0]`, then a look fragment,
        # two text PNG overlays and the watermark follow it.
        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _wide_photo(src)
        edits = SlideEdits(
            fit="blur",
            look_preset="golden_hour",
            texts=[
                SlideTextElement(id="t1", text="Hello", position="top"),
                SlideTextElement(id="t2", text="World", position="bottom"),
            ],
        )
        normalize_image_slide(str(src), str(out), canvas=(1080, 1350), edits=edits)
        assert probe_dimensions(str(out)) == (1080, 1350)

    def test_look_grades_the_bars_too(self, tmp_path):
        src, plain, graded = tmp_path / "s.png", tmp_path / "p.jpg", tmp_path / "g.jpg"
        _wide_photo(src)
        normalize_image_slide(
            str(src), str(plain), canvas=(1080, 1080), edits=SlideEdits(fit="white")
        )
        normalize_image_slide(
            str(src),
            str(graded),
            canvas=(1080, 1080),
            edits=SlideEdits(fit="white", look_preset="smoky_split_tone"),
        )
        with Image.open(plain) as a, Image.open(graded) as b:
            assert a.convert("RGB").getpixel((540, 100)) != b.convert("RGB").getpixel((540, 100))

    def test_bar_colour_matches_between_fit_and_fill_paths(self, tmp_path):
        # `overlay` defaulting to yuv420 would shift Fit slides' colour against Fill ones.
        src = tmp_path / "s.png"
        Image.new("RGB", (800, 800), (120, 200, 60)).save(src)
        fill, fit = tmp_path / "fill.jpg", tmp_path / "fit.jpg"
        normalize_image_slide(str(src), str(fill), canvas=(1080, 1080))
        normalize_image_slide(str(src), str(fit), canvas=(1080, 1080), edits=SlideEdits(fit="blur"))
        with Image.open(fill) as a, Image.open(fit) as b:
            pa, pb = a.convert("RGB").getpixel((540, 300)), b.convert("RGB").getpixel((540, 300))
            assert all(abs(x - y) <= 4 for x, y in zip(pa, pb, strict=True)), (pa, pb)

    def test_blur_filter_is_one_unlabelled_chain_pair(self):
        graph = _frame_filter((1080, 566), None, "blur")
        assert graph.startswith("split=2[kfg][kbg];")
        assert graph.endswith("setsar=1")
        assert "[b0]" not in graph and "[wm]" not in graph
        assert "force_divisible_by" not in graph
        # 270x142 background at quarter size: even, and sigma = 80px / 4.
        assert "scale=270:142:force_original_aspect_ratio=increase,crop=270:142" in graph
        assert f"gblur=sigma={FIT_BLUR_SIGMA_PX // 4}" in graph

    def test_the_crop_prefixes_the_blur_graph(self):
        crop = {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}
        graph = _frame_filter((1080, 1080), crop, "blur")
        assert graph.startswith("crop=") and ",split=2[kfg][kbg];" in graph

    def test_unknown_fit_is_refused(self):
        from app.pipeline.slide_post.build import SlideBuildError  # noqa: PLC0415

        with pytest.raises(SlideBuildError):
            _frame_filter((1080, 1080), None, "sepia")


# ── F2 / F2b: older clients never wipe what they cannot see ─────────────────


def _stored(**edits) -> SlidePostDraft:
    return _draft(slides=[_slide("a", SlideEdits(**edits))])


def _wire(slide_id: str = "a", **edits_json) -> SlideRef:
    """A slide as an older client's JSON would decode it (only the keys it sent)."""
    payload: dict = {"id": slide_id, "asset_id": ASSET, "kind": "image"}
    if edits_json:
        payload["edits"] = edits_json
    return SlideRef.model_validate(payload)


class TestFitSaveMerge:
    def test_omitted_key_keeps_the_stored_fit(self):
        out = merge_stored_edit_key(
            _stored(fit="blur"), [_wire(look_preset="none")], "fit", accept_incoming=True
        )
        assert out[0].edits.fit == "blur"

    def test_null_edits_keeps_the_stored_fit(self):
        out = merge_stored_edit_key(_stored(fit="white"), [_wire()], "fit", accept_incoming=True)
        assert out[0].edits.fit == "white"

    def test_a_framing_era_client_that_sends_framing_but_no_fit_keeps_fit(self):
        crop = {"orientation": "portrait", "source_crop": None}
        out = merge_stored_edit_key(
            _stored(fit="black"), [_wire(framing=crop)], "fit", accept_incoming=True
        )
        assert out[0].edits.fit == "black"
        assert out[0].edits.framing is not None

    def test_explicit_null_clears(self):
        out = merge_stored_edit_key(
            _stored(fit="blur"), [_wire(fit=None)], "fit", accept_incoming=True
        )
        assert out[0].edits.fit is None

    def test_explicit_value_sets(self):
        out = merge_stored_edit_key(_stored(), [_wire(fit="white")], "fit", accept_incoming=True)
        assert out[0].edits.fit == "white"

    def test_flag_off_ignores_incoming_fit(self):
        out = merge_stored_edit_key(
            _stored(fit="blur"), [_wire(fit="white")], "fit", accept_incoming=False
        )
        assert out[0].edits.fit == "blur"
        out = merge_stored_edit_key(_stored(), [_wire(fit="white")], "fit", accept_incoming=False)
        assert out[0].edits.fit is None

    def test_legacy_text_merge_keeps_fit_when_the_web_editor_omits_it(self):
        # F2b. The web editor has no `texts`, so every web save of a post with rich text
        # goes through merge_legacy_text_edits, which rebuilds SlideEdits by hand.
        stored = _stored(fit="blur", texts=[SlideTextElement(id="t", text="Hi", position="bottom")])
        web = _wire(look_preset="none", text={"content": "Hi", "position": "bottom"})
        merged = merge_legacy_text_edits(stored, [web])
        assert "fit" not in merged[0].edits.model_fields_set
        kept = merge_stored_edit_key(stored, merged, "fit", accept_incoming=True)
        assert kept[0].edits.fit == "blur"
        assert kept[0].edits.texts is not None

    def test_legacy_text_merge_carries_an_explicit_fit(self):
        stored = _stored(texts=[SlideTextElement(id="t", text="Hi", position="bottom")])
        sent = _wire(look_preset="none", text={"content": "Hi", "position": "bottom"}, fit="white")
        merged = merge_legacy_text_edits(stored, [sent])
        assert merged[0].edits.fit == "white"


# ── F5: a worker that predates sizes must never look current ─────────────────


def _rendered_variant(renderer_version: int) -> dict:
    from app.pipeline.slide_post.build import (  # noqa: PLC0415
        SLIDE_IMAGE_NORMALIZER_VERSION,
        SLIDE_WATERMARK_VERSION,
    )

    return {
        "render_status": "ready",
        "slides": [{"asset_id": ASSET, "asset_gcs_path": "generative-jobs/j/slides/n.jpg"}],
        "slide_post": {
            "validation": {"ok": True, "errors": []},
            "normalizer_version": SLIDE_IMAGE_NORMALIZER_VERSION,
            "watermark_version": SLIDE_WATERMARK_VERSION,
            "renderer_version": renderer_version,
        },
    }


class TestExportCurrency:
    @staticmethod
    def _current(draft: SlidePostDraft, variant: dict) -> bool:
        from app.routes.plan_items import _slide_post_export_is_current  # noqa: PLC0415

        return _slide_post_export_is_current(
            draft.model_copy(update={"rendered_version": draft.version}), variant
        )

    def test_a_default_draft_is_current_whatever_the_worker_version(self):
        # F5b: nothing sized or fitted -> an older worker's output is still right.
        assert self._current(_draft(), _rendered_variant(2))

    def test_a_sized_draft_rendered_by_an_older_worker_is_stale(self):
        assert not self._current(_draft(aspect="1:1"), _rendered_variant(2))
        assert self._current(_draft(aspect="1:1"), _rendered_variant(3))

    def test_a_fit_draft_rendered_by_an_older_worker_is_stale(self):
        draft = _draft(slides=[_slide("a", SlideEdits(fit="blur"))])
        assert not self._current(draft, _rendered_variant(2))
        assert self._current(draft, _rendered_variant(3))

    def test_dispatch_stamp_is_unchanged_so_a_mixed_deploy_never_rejects_jobs(self):
        from app.agents._schemas.edit_format import (  # noqa: PLC0415
            SLIDES_RENDERER_VERSION,
            SLIDES_SIZES_RENDERER_VERSION,
        )

        assert SLIDES_RENDERER_VERSION == 2
        assert SLIDES_SIZES_RENDERER_VERSION == 3


# ── F8: shared fixture ───────────────────────────────────────────────────────


class TestParityFixture:
    def test_fit_placement_matches_fixture(self):
        cases = json.loads(FIXTURE.read_text())["fit_placements"]
        assert len(cases) >= 5
        for case in cases:
            got = fit_placement(tuple(case["image"]), case["crop"], tuple(case["canvas"]))
            assert got == pytest.approx(tuple(case["expected"]), abs=0.01), case["name"]

    def test_fit_never_trims_what_fill_would(self):
        for case in json.loads(FIXTURE.read_text())["fit_placements"]:
            x, y, w, h = fit_placement(tuple(case["image"]), case["crop"], tuple(case["canvas"]))
            crop = case["crop"] or {"x": 0, "y": 0, "width": 1, "height": 1}
            cw, ch = case["canvas"]
            left = x + crop["x"] * w
            right = x + (crop["x"] + crop["width"]) * w
            top = y + crop["y"] * h
            bottom = y + (crop["y"] + crop["height"]) * h
            assert left >= -0.01 and top >= -0.01, case["name"]
            assert right <= cw + 0.01 and bottom <= ch + 0.01, case["name"]

    def test_sizes_table_matches_profiles(self):
        sizes = json.loads(FIXTURE.read_text())["sizes"]
        assert {k: tuple(v) for k, v in sizes["canvases"].items()} == profiles.ASPECT_CANVASES
        for name, spec in profiles.PLATFORM_PROFILES.items():
            assert sizes["profiles"][name] == {
                "default": spec.default_aspect,
                "allowed": list(spec.allowed_aspects),
            }

    def test_blur_sigma_matches_fixture(self):
        assert json.loads(FIXTURE.read_text())["fit_blur_sigma_px"] == FIT_BLUR_SIGMA_PX


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_ffmpeg_has_the_filters_the_blur_graph_needs():
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=True
    ).stdout
    for name in ("gblur", "split", "pad", "overlay"):
        assert f" {name} " in out
