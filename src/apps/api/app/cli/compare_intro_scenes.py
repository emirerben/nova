# ruff: noqa: E501  (the HTML page template and usage lines)
"""KRI-1612: a blind compare of the model's intro scene against the seeded pick (Emir's to run).

    python -m app.cli.compare_intro_scenes build --demo --out .intro-compare          # tool demo
    python -m app.cli.compare_intro_scenes build --fixtures my.json --model live --out .intro-compare
    python -m app.cli.compare_intro_scenes score --key .intro-compare/key.json --picks picks.json

``build`` renders, for each Talking fixture, the seeded pick (the KRI-1611 arm) and the model pick
(the KRI-1612 arm) over the same take frame, through the REAL worker call
(`_phone_talking_title_rows`: the real `place_title_look`, the same fit and ink pass) and the real
Skia draw. It writes ``index.html``: one pair per fixture, two unlabeled frames side by side, the
pair order shuffled and the side of each arm random per pair, with radio buttons and a button that
downloads ``picks.json``. File names are neutral (``item-<hash>.png``). Which side was the model
is in ``key.json`` only, a separate file whose sha256 is printed on the page and checked by
``score``: do not open it before picking.

The key records what each side REALLY was, so the tally is honest: an arm that found no scene
draws today's title and is flagged ``no_scene`` (the product would show that); two identical frames
are flagged ``identical`` and left out of the tally; a model answer that fell back is named.

Fixtures (``--fixtures``): ``[{"id": "...", "title": "...", "language": "en"|"tr", "still":
"path.jpg" (a 9:16 frame of the take; kept OUT of git) or "plate": "light_wall"|"dark_room",
"face": [l, t, r, b] normalized (optional; the typical speaker box when absent), "notes": "..."}]``.
``--model``: ``live`` asks the real agent (one cheap Gemini flash call per fixture, paid, needs
GEMINI_API_KEY), ``replay`` takes ``"answer"`` from each fixture (an IntroScene JSON; no call).
``--demo`` renders six synthetic fixtures with built-in answers. A demo run is a check of this tool,
never the compare: the plates are synthetic and the answers hand-authored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("STORAGE_BUCKET", "intro-compare")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from PIL import Image  # noqa: E402

from app.cli import verify_intro_scenes as base  # noqa: E402
from app.pipeline import intro_scene as scene_mod  # noqa: E402
from app.pipeline import intro_scene_pick as pick_mod  # noqa: E402
from app.pipeline import overlay_verify as ov  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion  # noqa: E402

POLICY = {
    "version": 1,
    "preferences": {"all_text": {}},
    "requested": {},
    "informative_text_designs": True,
    "informative_text_shapes": True,
}
MIN_PAIRS = 5  # the ticket's bar: at least five real Talking edits


def _demo_fixtures() -> list[dict[str, Any]]:
    answers = {
        ("en", "light_wall"): {
            "decision": "compose",
            "voices": ["solid", "solid", "solid", "solid", "accent", "accent"],
            "font_pair": "grotesk_hand",
            "ink": "oxblood",
            "zone": "above_head",
            "entrance": "ink",
            "sound": "none",
        },
        ("en", "dark_room"): {
            "decision": "compose",
            "voices": ["solid", "solid", "solid", "solid", "accent"],
            "font_pair": "grotesk_loop",
            "ink": "chartreuse",
            "zone": "above_head",
            "entrance": "ink",
            "sound": "none",
        },
        ("tr", "light_wall"): {
            "decision": "compose",
            "voices": ["solid"] * 6 + ["accent"] * 2,
            "font_pair": "serif_italic",
            "ink": "oxblood",
            "zone": "above_head",
            "entrance": "fade",
            "sound": "none",
        },
        ("tr", "dark_room"): {
            "decision": "compose",
            "voices": ["solid", "solid", "accent"],
            "font_pair": "script_serif",
            "ink": "cream",
            "zone": "above_head",
            "entrance": "fade",
            "sound": "none",
        },
    }
    rows = [
        ("demo-1", "en", "light_wall", "the biggest design trend right now"),
        ("demo-2", "en", "dark_room", "how am i using pinterest"),
        ("demo-3", "tr", "light_wall", "bu yılın en büyük tasarım trendi şu an"),
        ("demo-4", "tr", "dark_room", "pinterest’i nasıl kullanıyorum"),
        ("demo-5", "en", "light_wall", "the one habit that fixed my mornings"),
        ("demo-6", "en", "dark_room", "day one"),  # the model declines it (a no_scene control)
    ]
    out = []
    for fid, language, plate, title in rows:
        answer = answers[(language, plate)]
        if fid == "demo-5":
            answer = {**answers[("en", "light_wall")], "voices": ["solid"] * 5 + ["accent"]}
        if fid == "demo-6":
            answer = {"decision": "decline", "decline_reason": "title_unsuited"}
        out.append(
            {"id": fid, "title": title, "language": language, "plate": plate, "answer": answer}
        )
    return out


def _background(fixture: dict[str, Any]) -> Image.Image:
    if fixture.get("still"):
        still = Image.open(fixture["still"]).convert("RGBA")
        width, height = PORTRAIT.width, PORTRAIT.height
        scale = max(width / still.width, height / still.height)
        resized = still.resize((round(still.width * scale), round(still.height * scale)))
        left, top = (resized.width - width) // 2, (resized.height - height) // 2
        return resized.crop((left, top, left + width, top + height))
    return base.plate(fixture.get("plate", "light_wall"))


def _frames_of(background: Image.Image) -> list[Any]:
    from app.pipeline import caption_legibility as legibility  # noqa: PLC0415

    grey = background.convert("L").resize((270, 480))
    return [legibility.GreyFrame(grey.tobytes(), 270, 480, 0.0, 30.0)]


def _face_of(fixture: dict[str, Any]) -> NormalizedBox:
    face = fixture.get("face")
    return NormalizedBox(*face) if face else scene_mod.TYPICAL_FACE


def real_title_rows(
    title: str, plan: dict[str, Any] | None, face: NormalizedBox, language: str
) -> list[dict[str, Any]]:
    """The rows the worker draws for ``title`` under ``plan`` (the stamped ask, or None = today's
    title): the REAL `_phone_talking_title_rows` with the speaker's face fixed to ``face``."""
    from app.pipeline import phone_subtitled_title as title_mod  # noqa: PLC0415
    from app.services.phone_text_styling import phone_text_policy_scope  # noqa: PLC0415
    from app.tasks.generative_build import _phone_talking_title_rows  # noqa: PLC0415

    def sample(_path, anchors, **_kw):  # noqa: ANN001, ANN202
        return [ProtectedRegion(0.0, 1e6, face, kind="face")], {"decoded": len(anchors)}

    from app.config import settings  # noqa: PLC0415

    original = title_mod.sample_face_regions
    title_mod.sample_face_regions = sample
    was = settings.title_look_enabled
    settings.title_look_enabled = True  # the worker only draws a scene with the look switch on
    try:
        with phone_text_policy_scope(dict(POLICY)):
            rows, _receipt = _phone_talking_title_rows(
                title,
                duration_s=None,
                cues=[],
                binding=SimpleNamespace(
                    original=SimpleNamespace(width=720, height=1280, orientation_degrees=0)
                ),
                clip_path="take.mp4",
                keep_segments=[(0.0, 30.0)],
                landscape_fit="fill",
                canvas=PORTRAIT,
                title_look_plan=plan,
                spoken_language=language,
            )
    finally:
        title_mod.sample_face_regions = original
        settings.title_look_enabled = was
    return rows


def draw(rows: list[dict[str, Any]], background: Image.Image) -> Image.Image:
    frame = background.copy()
    for row in rows:
        frame = Image.alpha_composite(
            frame, ov.render_overlay_frame(scene_mod.overlay_for_row(row))
        )
    return frame


def signature(rows: list[dict[str, Any]]) -> str:
    return json.dumps(
        [
            [
                r.get("text"),
                r.get("font_family"),
                r.get("color"),
                round(float(r.get("y_frac", 0)), 3),
                r.get("effect"),
                r.get("size_px"),
            ]
            for r in rows
        ],
        ensure_ascii=False,
    )


def _arm_seeded(fixture: dict[str, Any], background: Image.Image, face: NormalizedBox):
    title = fixture["title"]
    seed = hashlib.sha256(f"compare|{fixture['id']}|{title}".encode()).hexdigest()[:16]
    ask = {
        "version": pick_mod.INTRO_SCENE_VERSION,
        "seed": seed,
        "inks": pick_mod.ink_order(seed),
        "candidates": pick_mod.pick_candidates(title, seed=seed),
    }
    plan = {pick_mod.INTRO_SCENE_ASK_FIELD: ask} if ask["candidates"] else None
    rows = real_title_rows(title, plan, face, fixture.get("language", "en"))
    scene = bool(rows) and len(rows) >= 2 and "intro_" in json.dumps(rows[0].get("source_params"))
    if scene:
        rows = pick_mod.intro_ink_pass(rows, _frames_of(background), canvas=PORTRAIT)
    return rows, {"arm": "seeded", "scene": scene}


def _arm_model(fixture: dict[str, Any], background: Image.Image, face: NormalizedBox, mode: str):
    from app.agents._runtime import RunContext  # noqa: PLC0415

    title, language = fixture["title"], fixture.get("language", "en")
    ctx = RunContext(extra={"skip_langfuse_trace": True, "skip_agent_run_persist": True})
    if mode == "live":
        pin = pick_mod.resolve_model_scene(
            title,
            language=language,
            footage_notes=str(fixture.get("notes", "")),
            brightness=pick_mod.take_brightness(_frames_of(background)),
            job_id=None,
            ctx=ctx,
        )
    else:
        from app.cli.verify_intro_scene_model import ReplayClient  # noqa: PLC0415

        pin = pick_mod.resolve_model_scene(
            title,
            language=language,
            footage_notes=str(fixture.get("notes", "")),
            brightness=pick_mod.take_brightness(_frames_of(background)),
            job_id=None,
            client=ReplayClient(json.dumps(fixture["answer"])),
            ctx=ctx,
        )
    ask = {
        "version": pick_mod.INTRO_SCENE_VERSION,
        "seed": "compare",
        "inks": pick_mod.ink_order("compare"),
        "candidates": pick_mod.pick_candidates(title, seed="compare") or [{}],
        pick_mod.MODEL_ASK_FIELD: {"version": pick_mod.MODEL_VERSION},
        pick_mod.MODEL_PICK_FIELD: pin,
    }
    rows = real_title_rows(title, {pick_mod.INTRO_SCENE_ASK_FIELD: ask}, face, language)
    scene = len(rows) >= 2 and "intro_" in json.dumps(rows[0].get("source_params"))
    if scene:
        rows = pick_mod.intro_ink_pass(rows, _frames_of(background), canvas=PORTRAIT)
    return rows, {"arm": "model", "scene": scene, "outcome": pin.get("outcome"), "pin": pin}


def build(fixtures: list[dict[str, Any]], out_dir: Path, *, model: str, seed: int | None) -> dict:
    rng = random.Random(seed if seed is not None else int.from_bytes(os.urandom(8), "big"))
    images = out_dir / "images"
    images.mkdir(parents=True, exist_ok=True)
    pairs: list[dict[str, Any]] = []
    for fixture in fixtures:
        background = _background(fixture)
        face = _face_of(fixture)
        seeded_rows, seeded_info = _arm_seeded(fixture, background, face)
        model_rows, model_info = _arm_model(fixture, background, face, model)
        model_left = rng.random() < 0.5
        left_arm, right_arm = (model_rows, seeded_rows) if model_left else (seeded_rows, model_rows)
        token = hashlib.sha256(f"{fixture['id']}|{rng.random()}".encode()).hexdigest()[:10]
        names = {"L": f"item-{token}-a.png", "R": f"item-{token}-b.png"}
        draw(left_arm, background).convert("RGB").save(images / names["L"])
        draw(right_arm, background).convert("RGB").save(images / names["R"])
        pairs.append(
            {
                "fixture": fixture["id"],
                "title": fixture["title"],
                "language": fixture.get("language", "en"),
                "files": names,
                "model_side": "L" if model_left else "R",
                "identical": signature(seeded_rows) == signature(model_rows),
                "model_no_scene": not model_info["scene"],
                "seeded_no_scene": not seeded_info["scene"],
                "model_outcome": model_info.get("outcome"),
                "model_pin": model_info.get("pin"),
                "source": fixture.get("still") and "real_still" or "synthetic_plate",
                "mode": model,
            }
        )
    rng.shuffle(pairs)  # the page order is not the fixture order
    for number, pair in enumerate(pairs, start=1):
        pair["n"] = number
    key = {"version": 1, "model_mode": model, "pairs": pairs}
    key_text = json.dumps(key, indent=2, ensure_ascii=False)
    (out_dir / "key.json").write_text(key_text)
    digest = hashlib.sha256(key_text.encode()).hexdigest()
    (out_dir / "index.html").write_text(_page(pairs, digest, demo=model != "live"))
    return {"pairs": len(pairs), "key_sha256": digest, "out": str(out_dir)}


def _page(pairs: list[dict[str, Any]], digest: str, *, demo: bool) -> str:
    cards = []
    for pair in pairs:
        n = pair["n"]
        cards.append(
            f'<section><h2>{n}</h2><div class="row">'
            f'<img src="images/{pair["files"]["L"]}" alt="left"><img src="images/{pair["files"]["R"]}" alt="right">'
            f'</div><div class="pick" data-n="{n}">'
            f'<label><input type="radio" name="p{n}" value="L"> Left</label>'
            f'<label><input type="radio" name="p{n}" value="R"> Right</label>'
            f'<label><input type="radio" name="p{n}" value="tie"> Cannot tell</label></div></section>'
        )
    banner = (
        '<p class="warn">Tool demo: synthetic plates and hand-authored answers. This is not the compare.</p>'
        if demo
        else ""
    )
    return f"""<!doctype html><meta charset="utf-8"><title>Intro scenes, blind compare</title>
<meta name="key-sha256" content="{digest}">
<style>body{{font:15px system-ui;background:#111;color:#eee;margin:24px}}.row{{display:flex;gap:16px}}
img{{height:640px;border-radius:8px}}section{{margin-bottom:32px}}label{{margin-right:16px}}.warn{{color:#fb4}}</style>
<h1>Which intro would you ship?</h1>{banner}
<p>Pick one per pair, or "Cannot tell". Do not open key.json until you have downloaded your picks.
Key fingerprint (sha256): <code>{digest}</code></p>
{"".join(cards)}
<button onclick="dl()">Download picks.json</button>
<script>function dl(){{const o={{}};document.querySelectorAll('.pick').forEach(e=>{{const c=e.querySelector('input:checked');o[e.dataset.n]=c?c.value:null}});
const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(o,null,1)],{{type:'application/json'}}));a.download='picks.json';a.click()}}</script>"""


def score(key_path: Path, picks_path: Path) -> dict[str, Any]:
    key_text = key_path.read_text()
    index = key_path.with_name("index.html")
    if index.exists():
        committed = index.read_text().split('name="key-sha256" content="')[1].split('"')[0]
        if hashlib.sha256(key_text.encode()).hexdigest() != committed:
            raise SystemExit("key.json does not match the fingerprint on the page: it was changed")
    key = json.loads(key_text)
    picks = json.loads(picks_path.read_text())
    tally = {"model": 0, "seeded": 0, "cannot_tell": 0, "identical_excluded": 0, "unanswered": 0}
    detail = []
    for pair in key["pairs"]:
        choice = picks.get(str(pair["n"]))
        if pair["identical"]:
            tally["identical_excluded"] += 1
            continue
        if choice is None:
            tally["unanswered"] += 1
        elif choice == "tie":
            tally["cannot_tell"] += 1
        else:
            winner = "model" if choice == pair["model_side"] else "seeded"
            tally[winner] += 1
            detail.append(
                {
                    "n": pair["n"],
                    "fixture": pair["fixture"],
                    "winner": winner,
                    "model_no_scene": pair["model_no_scene"],
                }
            )
    counted = tally["model"] + tally["seeded"] + tally["cannot_tell"]
    tally["pairs_counted"] = counted
    tally["enough_pairs"] = counted >= MIN_PAIRS
    tally["detail"] = detail
    tally["model_no_scene_pairs"] = [p["fixture"] for p in key["pairs"] if p["model_no_scene"]]
    tally["demo"] = key.get("model_mode") != "live"
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare_intro_scenes", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", default=".intro-compare")
    b.add_argument("--fixtures")
    b.add_argument("--demo", action="store_true")
    b.add_argument("--model", choices=["live", "replay"], default="replay")
    b.add_argument("--seed", type=int, default=None)
    s = sub.add_parser("score")
    s.add_argument("--key", required=True)
    s.add_argument("--picks", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "build":
        if args.demo == bool(args.fixtures):
            parser.error("give exactly one of --demo or --fixtures")
        fixtures = _demo_fixtures() if args.demo else json.loads(Path(args.fixtures).read_text())
        if args.demo and args.model == "live":
            parser.error("--demo has hand-authored answers; use --model replay")
        result = build(fixtures, Path(args.out).resolve(), model=args.model, seed=args.seed)
        print(f"wrote {result['pairs']} pair(s) to {result['out']}/index.html")
        print(f"key fingerprint {result['key_sha256']} (key.json: do not open before picking)")
        if result["pairs"] < MIN_PAIRS:
            print(f"note: the ticket asks for at least {MIN_PAIRS} real Talking edits")
        return 0
    print(json.dumps(score(Path(args.key), Path(args.picks)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
