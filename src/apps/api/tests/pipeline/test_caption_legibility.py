"""KRI-1343 / KRI-1344 (yorum3, job 7eba9903): captions must stay readable on any background.

yorum3 asked "captions small, white, lowercase, in the middle". The phone drew 42 px white text
with no outline and no shadow at y 0.62, on her cream sweater (unreadable), and during the
full-screen post screenshots the caption sat inside the post's own headline.

This drives the real phone compile (`compile_phone_subtitled_plan`) under a generation policy,
with the real environment sampler and the real legibility pass. Only I/O is faked: downloads
return media drawn here (a cream-sweater take, a post screenshot with its headline where the
caption sits, a calm photo, a page of text), and the face sampler returns her face. The judge
is independent of the pass: it reads the compiled text layers and measures them against the
pixels drawn here, and checks the caption against the text boxes this file drew itself.

Ways the fix could fail (written before the fix, full list in the session notes):
1. An asked colour ("white") still means no treatment: white stays outline-free on cream.
2. The fill is changed (her white is hers).
3. An explicit "no outline" ask is overridden (then a shadow is the treatment, never an outline).
4. Photos are not measured; the look is decided once before the full-screen cards are known.
5. A line over a post screenshot stays inside its headline.
6. A moved line lands on her face while the take is up.
7. A calm photo still moves the line (wrong-content control: only text/busy pictures move it).
8. A page of text gets a line drawn over it anyway (it must hide for that window).
9. The switch off changes anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledOverlayCard
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services import phone_text_styling as styling
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts"
TAKE = "clip-0"
POST, CALM, PAGE = "post-1", "calm-1", "page-1"
# The post screenshot's own text, canvas fractions (drawn below; the judge's ground truth).
# Filled in by `_draw_media` from the exact ink boxes of every line it draws on the post.
POST_TEXT: list[tuple[float, float, float, float]] = []
FACE = (0.30, 0.10, 0.70, 0.42)
CUES = [
    {"text": "okay, starting.", "start_s": 0.0, "end_s": 1.3},
    {"text": "ten minutes after lumo launched its", "start_s": 1.5, "end_s": 3.2},
    {"text": "first foldable phone, vela posted on", "start_s": 3.3, "end_s": 5.0},
    {"text": "first, welcome, lumo.", "start_s": 5.2, "end_s": 6.6},
    {"text": "we've been waiting for you", "start_s": 6.7, "end_s": 8.4},
    {"text": "for five years.", "start_s": 8.5, "end_s": 9.8},
]
WHITE_SMALL_MIDDLE = {"case": "lower", "size": "small", "color": "#FFFFFF", "position": "middle"}


def _font(size: int):
    for name in ("Inter-Bold.ttf", "PlayfairDisplay-Bold.ttf"):
        path = next(FONT.rglob(name), None)
        if path is not None:
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _draw_media(directory: Path) -> dict[str, Path]:
    """The pictures behind the captions, at 540x960 (cover-fit to the canvas)."""
    w, h = 540, 960
    # Her take: terracotta wall, dark hair/face in the upper half, a cream sweater from y 0.48.
    take = Image.new("RGB", (w, h), (176, 84, 62))
    d = ImageDraw.Draw(take)
    d.ellipse((int(0.30 * w), int(0.10 * h), int(0.70 * w), int(0.42 * h)), fill=(40, 30, 30))
    d.rectangle((int(0.10 * w), int(0.48 * h), int(0.90 * w), h), fill=(232, 222, 205))
    take.save(directory / "take.png")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(directory / "take.png")]
        + ["-t", "12", "-r", "2", "-vf", "format=yuv420p", str(directory / "take.mp4")],
        check=True,
    )
    # A post screenshot: black, white headline exactly where the middle caption sits.
    post = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(post)
    POST_TEXT.clear()
    plain_text = d.text

    def text_and_record(xy, line, **kwargs):
        x0, y0, x1, y1 = d.textbbox(xy, line, font=kwargs["font"])
        POST_TEXT.append((x0 / w, y0 / h, x1 / w, y1 / h))
        plain_text(xy, line, **kwargs)

    d.text = text_and_record
    lines = ["A hinge?", "Welcome, Lumo.", "We've been waiting", "for you for", "5 years."]
    for index, line in enumerate(lines):
        d.text(
            (int(0.06 * w), int((0.445 + index * 0.055) * h)), line, fill="white", font=_font(42)
        )
    d.text((int(0.04 * w), int(0.86 * h)), "24.1K likes", fill="white", font=_font(24))
    d.text((int(0.04 * w), int(0.91 * h)), "12 minutes ago", fill=(150, 150, 150), font=_font(20))
    d.text((int(0.06 * w), int(0.022 * h)), "20:14", fill="white", font=_font(22))
    d.text = plain_text
    post.save(directory / "post.png")
    # A calm photo: a smooth dark gradient, no text.
    calm = Image.linear_gradient("L").resize((w, h)).point(lambda v: 20 + v // 6).convert("RGB")
    calm.save(directory / "calm.png")
    # A page of text, top to bottom.
    page = Image.new("RGB", (w, h), (245, 245, 240))
    d = ImageDraw.Draw(page)
    for row in range(40):
        d.text(
            (20, 10 + row * 24),
            "Lorem ipsum dolor sit amet, consectetur " * 2,
            fill="black",
            font=_font(18),
        )
    page.save(directory / "page.png")
    return {
        "take": directory / "take.mp4",
        POST: directory / "post.png",
        CALM: directory / "calm.png",
        PAGE: directory / "page.png",
    }


def _binding() -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=TAKE,
        proxy_path=f"user/analysis-proxy-{TAKE}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=12.0,
            width=1080,
            height=1920,
            orientation_degrees=0,
            has_audio=True,
        ),
    )


def _visual(media_id: str) -> PhoneVisualBinding:
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/owner/pool/{media_id}.png",
        generation="7",
        sha256="b" * 64,
        byte_count=2048,
    )


def _card(media_id: str, start: float, end: float) -> SubtitledOverlayCard:
    return SubtitledOverlayCard(
        id=f"beat-{media_id}",
        media_id=media_id,
        gcs_path=f"users/owner/pool/{media_id}.png",
        generation="7",
        start_s=start,
        end_s=end,
        display_mode="fullscreen",
    )


@pytest.fixture
def media(monkeypatch, tmp_path):
    files = _draw_media(tmp_path)

    def download(remote, local, *args, **kwargs):  # noqa: ARG001
        remote = str(remote)
        source = files["take"] if "analysis-proxy" in remote else None
        for media_id in (POST, CALM, PAGE):
            if media_id in remote:
                source = files[media_id]
        if source is None:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(source.read_bytes())
        return local

    import app.pipeline.render_geometry as render_geometry
    import app.storage as storage
    from app.pipeline.render_geometry import ProtectedRegion

    monkeypatch.setattr(storage, "download_to_file", download)
    monkeypatch.setattr(
        render_geometry,
        "sample_face_regions",
        lambda path, points, **k: (
            [ProtectedRegion(start_s=0.0, end_s=12.0, box=_face_box(), kind="face")],
            {"decoded": len(points)},
        ),
    )
    monkeypatch.setattr(settings, "caption_legibility_enabled", True)
    return files


def _face_box():
    from app.pipeline.render_geometry import NormalizedBox

    return NormalizedBox(left=FACE[0], top=FACE[1], right=FACE[2], bottom=FACE[3])


def _compile(cards=(), *, ask=WHITE_SMALL_MIDDLE, requested=None):
    policy = {
        "version": 1,
        "preferences": {"all_text": {}},
        "requested": requested or {},
        "caption_ask": ask,
        "_source_bindings": [_binding().model_dump(mode="json")],
    }
    with styling.phone_text_policy_scope(policy):
        recipe = compile_phone_subtitled_plan(
            (_binding(),),
            caption_cues=styling.generation_caption_cues([dict(c) for c in CUES]),
            visuals=tuple(_visual(c.media_id) for c in cards),
            lanes=PhoneSubtitledLanes(overlays=list(cards)) if cards else None,
        )
        fields = styling.generation_style_fields()
    return recipe, fields


# ------------------------------------------------------------------ the independent judge


def _lum(level: float) -> float:
    v = level / 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _hex_lum(color: str) -> float:
    r, g, b = (_lum(int(color[i : i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ink_lum(ink) -> float:
    r, g, b = (_lum(round(channel * 255)) for channel in (ink.red, ink.green, ink.blue))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: float, b: float) -> float:
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _captions(recipe):
    return [layer for layer in recipe.text_layers if layer.id.startswith("caption-")]


def _ink_box(layer) -> tuple[float, float, float, float]:
    """The caption's drawn block (canvas fractions), conservatively: each run from its pen x
    over 0.62 em per character, from 0.85 em above its baseline to 0.3 em below, plus its
    outline (the phone lays shaped runs out itself; this over-covers, never under-covers)."""
    xs, ys = [], []
    for run in layer.runs:
        bleed = run.stroke_width / 2
        xs += [run.x - bleed, run.x + 0.62 * run.font_size * len(run.text) + bleed]
        ys += [
            run.baseline_y - 0.85 * run.font_size - bleed,
            run.baseline_y + 0.3 * run.font_size + bleed,
        ]
    return (min(xs) / 1080, min(ys) / 1920, max(xs) / 1080, max(ys) / 1920)


def _background_at(files, cards, at: float) -> Image.Image:
    card = next((c for c in cards if c.start_s <= at < c.end_s), None)
    path = files[card.media_id] if card else files["take"].with_suffix(".png")
    return Image.open(path).convert("L")


def _contrast(layer, background: Image.Image) -> float:
    """What the caption reaches: plain fill against the darkest/brightest tenth of the pixels
    under its block, or fill against its own outline when it has one (>= 2 px)."""
    run = layer.runs[0]
    fill = _ink_lum(run.fill)
    box = _ink_box(layer)
    w, h = background.size
    crop = background.crop(
        (int(box[0] * w), int(box[1] * h), int(box[2] * w) + 1, int(box[3] * h) + 1)
    )
    pixels = sorted(crop.getdata())
    dark, bright = _lum(pixels[len(pixels) // 10]), _lum(pixels[9 * len(pixels) // 10])
    plain = min(_ratio(fill, dark), _ratio(fill, bright))
    if (run.stroke_width or 0) >= 4:  # portable stroke is drawn 2x the outline px
        return max(plain, _ratio(fill, _ink_lum(run.stroke)))
    return plain


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ------------------------------------------------------------------ the tests


def test_white_small_captions_on_a_cream_sweater_gain_an_outline_and_keep_white(media):
    """1, 2: yorum3's ask on her sweater: still white, now outlined in black, >= 4.5:1."""
    recipe, fields = _compile()
    assert fields["caption_text_color"] == "#FFFFFF"
    assert fields["caption_size_px"] == 42
    assert fields["caption_stroke_width"] == 3
    assert fields["caption_editor_style"]["stroke_color"] == "#000000"
    assert fields["caption_legibility"]["treatment"] == "outline"
    assert fields["caption_legibility"]["contrast_before"] < 4.5
    background = _background_at(media, (), 0.0)
    for layer in _captions(recipe):
        assert _contrast(layer, background) >= 4.5, layer.id


def test_a_line_over_a_post_screenshot_moves_off_its_headline_and_her_face(media):
    """4, 5, 6: during the post the line goes to the post's calm black band, never into its
    headline; the part of a line still over the take keeps off her face."""
    cards = (_card(POST, 5.0, 9.0),)
    recipe, fields = _compile(cards)
    receipt = fields["caption_legibility"]
    assert receipt["pictures"] == 1 and receipt["moved"], receipt
    over_post = [layer for layer in _captions(recipe) if layer.start < 9.0 and layer.end > 5.0]
    assert over_post
    for layer in over_post:
        box = _ink_box(layer)
        assert not any(_overlaps(box, text) for text in POST_TEXT), (layer.id, box)
        if layer.start < 5.0 or layer.end > 9.0:
            assert not _overlaps(box, FACE), (layer.id, box)
        assert _contrast(layer, _background_at(media, cards, max(layer.start, 5.0))) >= 4.5
    # The editor and a Save keep the move: keyed by the persisted cue id.
    overrides = fields["caption_editor_style"]["line_overrides"]
    assert set(overrides) == {m["cue"] for m in receipt["moved"]}
    assert all(cue_id.startswith("native-caption-") for cue_id in overrides)


def test_a_calm_photo_leaves_every_line_where_she_asked(media):
    """7 (wrong-content control): the same card window with a calm photo moves nothing."""
    recipe, fields = _compile((_card(CALM, 5.0, 9.0),))
    assert fields["caption_legibility"]["moved"] == []
    assert fields["caption_legibility"]["hidden"] == []
    assert "line_overrides" not in fields["caption_editor_style"]
    ys = {round(layer.anchor_y / 1920, 2) for layer in _captions(recipe)}
    assert len(ys) <= 2  # one spot (a wrapped line may anchor one row higher)


def test_a_page_of_text_hides_the_line_for_its_window(media):
    """8: no calm band on a page of text: no caption is drawn while it is up."""
    recipe, fields = _compile((_card(PAGE, 5.0, 9.0),))
    assert fields["caption_legibility"]["hidden"]
    for layer in _captions(recipe):
        assert layer.end <= 5.0 + 1e-6 or layer.start >= 9.0 - 1e-6, (
            layer.id,
            layer.start,
            layer.end,
        )


def test_an_explicit_no_outline_ask_is_kept_and_a_shadow_helps_instead(media):
    """3: "no outline" is hers; the treatment is a firm shadow in the readable ink."""
    recipe, fields = _compile(requested={"caption": {"stroke_width": 0}})
    assert fields["caption_stroke_width"] == 0
    assert fields["caption_shadow_enabled"] is True
    assert fields["caption_editor_style"]["shadow_color"] == "#000000"
    assert fields["caption_legibility"]["treatment"] == "shadow"


def test_switch_off_is_byte_identical(media, monkeypatch):
    """9: off, the recipe and the persisted fields are exactly today's."""
    cards = (_card(POST, 5.0, 9.0),)
    monkeypatch.setattr(settings, "caption_legibility_enabled", False)
    off_recipe, off_fields = _compile(cards)
    assert "caption_legibility" not in off_fields
    assert off_fields["caption_stroke_width"] == 0
    assert "line_overrides" not in off_fields["caption_editor_style"]
    monkeypatch.setattr(settings, "caption_legibility_enabled", True)
    on_recipe, _ = _compile(cards)
    assert off_recipe.model_dump() != on_recipe.model_dump()


def test_a_hidden_line_stays_hidden_through_editor_save_reopen_and_export(
    media, monkeypatch, tmp_path
):
    """KRI-1344 review (Emir, #2258): the hide over a page of text is persisted on the cue
    (`hidden_spans`) and honoured on every later path: the editor's Save (the whole cue list
    comes back through the caption whitelist, one line's text edited), the phone recipe the
    Save rebuilds, and the cloud caption burn. Failure modes: the whitelist strips the field on
    the first text edit; generation persists the cues without it; the Save compile ignores it;
    the cloud burn ignores it."""
    import app.pipeline.captions as captions_mod
    import app.pipeline.narrated_assembler as assembler
    from app.pipeline.caption_legibility import visible_cues
    from app.services.device_render import device_status
    from app.tasks import generative_build as gb
    from tests.routes.test_phone_subtitled_editor_commit import phone_job, save

    cards = (_card(PAGE, 5.0, 9.0),)
    policy = {
        "version": 1,
        "preferences": {"all_text": {}},
        "requested": {},
        "caption_ask": WHITE_SMALL_MIDDLE,
        "_source_bindings": [_binding().model_dump(mode="json")],
    }
    with styling.phone_text_policy_scope(policy):
        cues = styling.generation_caption_cues([dict(c) for c in CUES])
        compile_phone_subtitled_plan(
            (_binding(),),
            caption_cues=cues,
            visuals=tuple(_visual(c.media_id) for c in cards),
            lanes=PhoneSubtitledLanes(overlays=list(cards)),
        )
        fields = styling.generation_style_fields()
        persisted = styling.generation_persisted_cues(cues)
    hidden = {c["id"]: c["hidden_spans"] for c in persisted if c.get("hidden_spans")}
    assert hidden, "generation must persist the hide on the cue"
    assert fields["caption_legibility"]["hidden"]

    # The editor (the switch is irrelevant from here on: the data carries the decision).
    monkeypatch.setattr(settings, "caption_legibility_enabled", False)
    job = phone_job(monkeypatch)
    job.assembly_plan["variants"][0].update({**fields, "caption_cues": persisted})
    edited = [dict(c) for c in persisted]
    edited[0]["text"] = "okay, here we go."
    save(job, caption_cues=edited)
    variant = job.assembly_plan["variants"][0]
    assert {
        c["id"]: c["hidden_spans"] for c in variant["caption_cues"] if c.get("hidden_spans")
    } == (hidden)
    recipe = device_status(job, "subtitled").request.recipe
    captions = _captions(recipe)
    assert captions
    for layer in captions:
        assert layer.end <= 5.0 + 1e-6 or layer.start >= 9.0 - 1e-6, (layer.id, layer.start)

    # Reopen + Save again with no edit: still hidden (nothing drops the field on a re-save).
    import app.routes.generative_jobs as gj

    reopened = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=reopened["render_generation_id"],
            caption_cues=[dict(c) for c in reopened["caption_cues"]],
        ),
        user_id="owner",
        plan_item_id="item",
    )
    for layer in _captions(device_status(job, "subtitled").request.recipe):
        assert layer.end <= 5.0 + 1e-6 or layer.start >= 9.0 - 1e-6, (layer.id, layer.start)

    # The cloud burn of the same variant draws the same visible parts.
    burned: list[list[dict]] = []

    def capture(cues, path, **kwargs):  # noqa: ARG001
        burned.append(list(cues))
        Path(path).write_text("")

    monkeypatch.setattr(captions_mod, "generate_ass_from_cues", capture)
    monkeypatch.setattr(assembler, "burn_captions_on_video", lambda *a, **k: None)
    gb._burn_persisted_captions_onto_base(
        str(tmp_path / "base.mp4"), str(tmp_path / "out.mp4"), variant, str(tmp_path)
    )
    assert burned and burned[0] == visible_cues(variant["caption_cues"])
    for cue in burned[0]:
        assert float(cue["end_s"]) <= 5.0 + 1e-6 or float(cue["start_s"]) >= 9.0 - 1e-6, cue
