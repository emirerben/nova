"""CLI: take a new intro reference from screenshot or video to an approved teardown (KRI-1614).

Dev-time only. Taste keeps growing without turning each reference into a prompt edit: a person
drops a screenshot or a short video, a model DRAFTS a teardown, the engine checks the draft, and a
person approves it into `prompts/intro_teardowns.json`. Modelled on the `overlay_examples` library
(a versioned JSON fixture, never a table).

    python -m app.cli.intro_intake draft  --name "Folder sticker" --image ref6.png \\
        --en-title "how i use pinterest" --tr-title "pinterest nasıl kullanıyorum" [--live]
    python -m app.cli.intro_intake review .intro-intake/ref6/draft.json
    python -m app.cli.intro_intake approve .intro-intake/ref6/draft.json --by emir

* ``draft`` writes ``.intro-intake/<id>/{draft.json,frame-N.jpg}`` and never touches the teardowns
  file. Without ``--live`` or ``--replay`` it is a dry run: it prints the request and stops, so
  nothing paid runs by accident. ``--live`` is ONE vision call (a few frames, Gemini Flash, a few
  cents); run it only under an approved development spend (tests/evals/README.md, hard cap $2). It
  does not go through the cost ledger. ``--replay FILE`` reads a recorded reply (tests, demos).
* ``review`` recomputes everything: the schema, then, per language, the compile through
  `compile_intro_scene`, the real Skia render measured like `make verify-intro-scenes`, and the
  approval check. It writes ``review.json`` and ``preview.png`` (the reference frame beside the EN
  and TR renders: read it) and lists the engine moves that no teardown or limit names yet.
* ``approve`` re-runs the review, shows the preview path, asks y/N, then appends the entry, stamps
  ``approved_by`` / ``approved_on``, bumps the version, and writes the file. A reference that needs
  a move the contract lacks widens nothing: ``approve`` writes ``engine-tickets.json`` (printed;
  Linear `save_issue` shape) and, with ``--file-tickets`` and ``LINEAR_API_KEY``, files them.

The human supplies the EN and TR example titles. The model returns option ids and prose notes
under the closed schema: nothing it writes reaches a screen (the `IntroScene` has no text field,
and the example titles are fixtures for the render check, never shown at runtime).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

# The CLI reads no secrets besides the optional keys; importing the app config needs these.
os.environ.setdefault("STORAGE_BUCKET", "intro-intake")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: E402

from app.pipeline import intro_scene as scene_mod  # noqa: E402
from app.pipeline import intro_scene_pick as pick_mod  # noqa: E402
from app.pipeline import intro_teardowns as td  # noqa: E402

_FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
PROMPT_VERSION = "intro-intake-1"
DEFAULT_OUT = ".intro-intake"
MAX_FRAME_PX = 1280
VIDEO_FRACTIONS = (0.15, 0.5, 0.85)
LIVE_MODEL_FIELD = "gemini_model"

# The shape every engine ticket takes (Linear `save_issue` arguments; project rules in
# docs/runbooks/linear-workflow.md): a project, one Domain label, one type label, an owner.
TICKET_PROJECT = "AI-made intro scenes"
TICKET_TEAM = "Kria"
TICKET_LABELS = ("AI", "Improvement")


# --- the model's reply ---------------------------------------------------------------------


class DraftReply(BaseModel):
    """What the model may return: ids and prose notes only, under the closed schema."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    expressible: Literal["yes", "partial", "no"]
    footage_fit: str = Field(min_length=1, max_length=400)
    footage_plate: Literal["light_wall", "dark_room"]
    why_it_works: str = Field(min_length=1, max_length=400)
    missing_moves: list[td.Move] = []
    approximations: list[td.Move] = []
    scene_en: scene_mod.IntroScene | None = None
    scene_tr: scene_mod.IntroScene | None = None


def parse_reply(raw: str | dict[str, Any]) -> DraftReply:
    """A reply as text (code fences allowed) or a dict. Raises ValidationError when it does not
    fit the closed schema: a free field is a refusal, never a guess."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.rsplit("```", 1)[0]
        raw = json.loads(text)
    return DraftReply.model_validate(raw)


# --- the request ---------------------------------------------------------------------------


class Frame:
    def __init__(self, data: bytes, label: str) -> None:
        self.data = data
        self.label = label


def _jpeg(image: Image.Image) -> bytes:
    import io  # noqa: PLC0415

    image = image.convert("RGB")
    image.thumbnail((MAX_FRAME_PX, MAX_FRAME_PX))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def _video_frames(path: Path) -> list[Frame]:
    probe = subprocess.run(  # noqa: S603
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError as exc:
        raise SystemExit(f"cannot read the duration of {path} (is ffprobe installed?)") from exc
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, fraction in enumerate(VIDEO_FRACTIONS):
            target = Path(tmp) / f"f{index}.png"
            result = subprocess.run(  # noqa: S603
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{duration * fraction:.2f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-y",
                    str(target),
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0 or not target.exists():
                raise SystemExit(f"ffmpeg could not read a frame from {path}")
            with Image.open(target) as image:
                frames.append(Frame(_jpeg(image), f"{path.name}@{fraction:.0%}"))
    return frames


def read_frames(images: Sequence[Path], videos: Sequence[Path]) -> list[Frame]:
    frames: list[Frame] = []
    for path in images:
        with Image.open(path) as image:
            frames.append(Frame(_jpeg(image), path.name))
    for path in videos:
        frames.extend(_video_frames(path))
    if not frames:
        raise SystemExit("give at least one --image or --video")
    return frames


_MENU_NOTES = {
    "font_pair": {
        pair_id: f"solid rows in {pair.solid.family}"
        f"{' (set in capitals)' if pair.solid.text_case else ''}, accent in {pair.accent.family}"
        for pair_id, pair in scene_mod.FONT_PAIRS.items()
    },
    "ink": dict(scene_mod.INKS),
}


def build_prompt(en_title: str, tr_title: str, note: str = "") -> str:
    """The dev-time instruction. Words are numbered: the model chooses a voice for each."""

    def numbered(title: str) -> str:
        return ", ".join(f"{i}:{w.text}" for i, w in enumerate(scene_mod.title_words(title), 1))

    ref = next(t for t in td.load()["teardowns"] if t["id"] == "ref1")
    example = {k: ref[k] for k in ("name", "expressible", "footage_fit", "footage_plate")}
    example |= {"why_it_works": ref["why_it_works"], "missing_moves": ref["missing_moves"]}
    example["scene_en"] = ref["examples"]["en"]["scene"]
    return f"""You tear down ONE creator intro (the attached frames) into a closed option set. You
are drafting for a person who will check it. Reply with one JSON object and nothing else.

Rules:
- You never write text for the screen. The two example titles are given by the person; you choose
  an IntroScene for each: option ids and one voice per title word, in order.
- voices: one of "solid" or "accent" per word. The accent is one run of 1-3 consecutive words
  (the reference's handwritten or contrasting voice). The rest are "solid".
- font_pair, ink, zone, entrance, sound: ids only, from these menus.
  font_pair: {json.dumps(_MENU_NOTES["font_pair"])}
  ink (one ink per scene; pick the nearest): {json.dumps(_MENU_NOTES["ink"])}
  zone: above_head (the empty space above the speaker's head), top_left, mid_frame
  entrance: fade, rise, pop, ink (the accent wipes in), pen (handwriting; only with grotesk_pen)
  sound: {", ".join(scene_mod.SOUNDS)}
- Be honest about what the menus cannot say. Photo cards, stickers, a folder prop, a flash cover,
  a staggered entrance, a lower third, per-element sound, an ink sampled from the footage: if the
  reference needs one, list it in missing_moves as {{"piece", "move"}} (the move the ENGINE would
  need) and set expressible to "partial" (the closest scene still works) or "no" (nothing
  works; then scene_en and scene_tr are null). Do not invent ids and do not stretch an id to
  cover a missing move. "yes" means no missing move at all.
- footage_plate: the synthetic footage the render check draws on: light_wall when the reference
  sits on a light wall where one saturated ink reads, else dark_room.
- Prose fields are short plain English, no quotation of on-screen text.

Titles (word index:word)
  en: {numbered(en_title)}
  tr: {numbered(tr_title)}
{f"Note from the person: {note}" if note else ""}
Keys: name, expressible, footage_fit, footage_plate, why_it_works, missing_moves, approximations,
scene_en, scene_tr. A finished example for another reference (English title only):
{json.dumps(example, ensure_ascii=False)}
"""


Drafter = Callable[[str, Sequence[Frame]], "str | dict[str, Any]"]


def replay_drafter(path: Path) -> Drafter:
    return lambda _prompt, _frames: json.loads(Path(path).read_text(encoding="utf-8"))


def live_drafter() -> Drafter:  # pragma: no cover - the one paid call; never run by tests
    from google.genai import types  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.pipeline.agents.gemini_analyzer import _get_client  # noqa: PLC0415

    if not settings.gemini_api_key:
        raise SystemExit("--live needs GEMINI_API_KEY")

    def call(prompt: str, frames: Sequence[Frame]) -> str:
        parts: list[Any] = [prompt]
        parts += [types.Part.from_bytes(data=f.data, mime_type="image/jpeg") for f in frames]
        response = _get_client().models.generate_content(
            model=getattr(settings, LIVE_MODEL_FIELD),
            contents=parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.2
            ),
        )
        return response.text or ""

    return call


# --- draft ---------------------------------------------------------------------------------


def draft(
    *,
    drafter: Drafter | None,
    frames: Sequence[Frame],
    name: str | None,
    en_title: str,
    tr_title: str,
    note: str,
    reference_label: str,
    teardowns: dict[str, Any],
    out_dir: Path,
    source_kind: str,
    ref_id: str | None = None,
) -> Path | None:
    """Write ``<out_dir>/<id>/draft.json`` (and the frames) from the drafter's reply; ``None``
    for a dry run. Never touches the teardowns file."""
    ref_id = ref_id or td.next_id(teardowns)
    prompt = build_prompt(en_title, tr_title, note)
    target = out_dir / ref_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "request.txt").write_text(prompt, encoding="utf-8")
    names = []
    for index, frame in enumerate(frames):
        (target / f"frame-{index}.jpg").write_bytes(frame.data)
        names.append(f"frame-{index}.jpg")
    if drafter is None:
        return None
    reply = parse_reply(drafter(prompt, frames))
    examples: dict[str, Any] = {}
    if reply.expressible != "no":
        for language, title, scene in (
            ("en", en_title, reply.scene_en),
            ("tr", tr_title, reply.scene_tr),
        ):
            if scene is not None:
                examples[language] = {"title": title, "scene": scene}
    entry: dict[str, Any] = {
        "id": ref_id,
        "name": name or reply.name,
        "reference_image": reference_label,
        "expressible": reply.expressible,
        "footage_fit": reply.footage_fit,
        "footage_plate": reply.footage_plate,
        "why_it_works": reply.why_it_works,
        "missing_moves": [m.model_dump() for m in reply.missing_moves],
        "examples": {
            k: {"title": v["title"], "scene": v["scene"].model_dump(mode="json", exclude_none=True)}
            for k, v in examples.items()
        },
    }
    if reply.approximations:
        entry["approximations"] = [m.model_dump() for m in reply.approximations]
    path = target / "draft.json"
    path.write_text(
        json.dumps(
            {
                "draft_version": 1,
                "prompt_version": PROMPT_VERSION,
                "source": {"kind": source_kind, "frames": names},
                "teardown": entry,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# --- review --------------------------------------------------------------------------------


def _teardown_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def review(
    draft_path: Path, *, teardowns: dict[str, Any], today: str | None = None
) -> dict[str, Any]:
    """Check a draft against the engine and draw it. Writes ``review.json`` and ``preview.png``
    beside the draft and returns the review. ``ok`` only when nothing the draft claims is false."""
    from app.cli import verify_intro_scenes as v  # noqa: PLC0415

    folder = draft_path.parent
    document = json.loads(draft_path.read_text(encoding="utf-8"))
    entry = document["teardown"]
    checks: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    tiles: list[tuple[str, Image.Image]] = []
    teardown: td.Teardown | None = None
    try:
        teardown = td.Teardown.model_validate(entry)
        checks.append(_check("schema", True))
    except ValidationError as exc:
        checks.append(_check("schema", False, "; ".join(e["msg"] for e in exc.errors())[:300]))
    taken = {t["id"] for t in teardowns["teardowns"]}
    checks.append(
        _check(
            "id",
            entry.get("id") == td.next_id(teardowns),
            f"{entry.get('id')} (next free {td.next_id(teardowns)}; taken {sorted(taken)})",
        )
    )
    if teardown is not None:
        gaps = [m.model_dump() for m in td.new_engine_gaps(teardowns, teardown)]
        for language, example in teardown.examples.items():
            title = example.title
            out = scene_mod.compile_intro_scene(
                example.scene,
                title,
                canvas=v.PORTRAIT,
                timeline_duration_s=30.0,
                language=pick_mod.language_of(title, language),
            )
            if out.rows is None:
                checks.append(_check(f"{language}_compiles", False, f"declined: {out.reason}"))
                continue
            placed, receipt = v._place(out.rows, out.position)
            verdict, reasons, frame = v.verify_rows(
                placed,
                receipt,
                face=v.FACE_BOX,
                scene=example.scene,
                footage=teardown.footage_plate,
            )
            checks.append(_check(f"{language}_renders", verdict == "PASS", "; ".join(reasons)))
            approved = bool(v.approval_matches(placed, title))
            checks.append(
                _check(f"{language}_approval_reads_title", approved, "" if approved else title)
            )
            tiles.append((f"{language}: {title}", frame.convert("RGB")))
    preview = _preview(folder, document, tiles)
    result = {
        "id": entry.get("id"),
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "engine_gaps": gaps,
        "teardown_sha256": _teardown_hash(entry),
        "preview": preview,
        "reviewed_on": today or date.today().isoformat(),
    }
    (folder / "review.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def _preview(folder: Path, document: dict[str, Any], tiles: list[tuple[str, Image.Image]]) -> str:
    """The reference frame beside the EN and TR renders, labelled. The content check."""
    width, height = 360, 640
    cells: list[tuple[str, Image.Image]] = []
    names = document.get("source", {}).get("frames") or []
    if names and (folder / names[0]).exists():
        with Image.open(folder / names[0]) as ref:
            cells.append(("reference", ref.convert("RGB").resize((width, height))))
    cells += [(label, tile.resize((width, height))) for label, tile in tiles]
    sheet = Image.new("RGB", (width * max(len(cells), 1), height + 40), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(_FONTS / "Inter-Bold.ttf"), 18)  # draws Turkish letters
    for index, (label, image) in enumerate(cells):
        sheet.paste(image, (index * width, 40))
        draw.text((index * width + 8, 10), label[:40], fill=(235, 235, 235), font=font)
    path = folder / "preview.png"
    sheet.save(path)
    return path.name


# --- engine tickets ------------------------------------------------------------------------


def engine_tickets(ref_id: str, gaps: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    """One ticket per new missing move, in the shape of Linear `save_issue` (What / Why / How)."""
    return [
        {
            "team": TICKET_TEAM,
            "project": TICKET_PROJECT,
            "assignee": "me",
            "labels": list(TICKET_LABELS),
            "title": f"Intro engine: {gap['piece']}"[:120],
            "description": (
                f"**What.** The contract cannot say: {gap['piece']}.\n\n"
                f"**Why.** Reference {ref_id} needs it; no teardown or engine limit names it yet. "
                "The teardown ships with the closest scene instead.\n\n"
                f"**How.** The engine move: {gap['move']}\n\n"
                "**Effort.** Not estimated."
            ),
        }
        for gap in gaps
    ]


def file_tickets(
    tickets: Sequence[dict[str, Any]], *, post: Callable[[str, dict[str, Any]], dict[str, Any]]
) -> list[dict[str, str]]:
    """File ``tickets`` through Linear's GraphQL API via ``post(query, variables)``, skipping a
    title that already exists in the project. Returns what happened to each."""
    ids = post(
        """query($team:String!,$project:String!,$labels:[String!]){
          teams(filter:{name:{eq:$team}}){nodes{id}}
          projects(filter:{name:{eq:$project}}){nodes{id}}
          issueLabels(filter:{name:{in:$labels}}){nodes{id}}
          viewer{id}
        }""",
        {"team": TICKET_TEAM, "project": TICKET_PROJECT, "labels": list(TICKET_LABELS)},
    )["data"]
    team, project = ids["teams"]["nodes"][0]["id"], ids["projects"]["nodes"][0]["id"]
    labels = [n["id"] for n in ids["issueLabels"]["nodes"]]
    out = []
    for ticket in tickets:
        found = post(
            "query($p:ID!,$t:String!){issues(filter:{project:{id:{eq:$p}},title:{eq:$t}})"
            "{nodes{identifier}}}",
            {"p": project, "t": ticket["title"]},
        )["data"]["issues"]["nodes"]
        if found:
            out.append({"title": ticket["title"], "status": f"exists {found[0]['identifier']}"})
            continue
        created = post(
            "mutation($i:IssueCreateInput!){issueCreate(input:$i){issue{identifier url}}}",
            {
                "i": {
                    "teamId": team,
                    "projectId": project,
                    "assigneeId": ids["viewer"]["id"],
                    "labelIds": labels,
                    "title": ticket["title"],
                    "description": ticket["description"],
                }
            },
        )["data"]["issueCreate"]["issue"]
        out.append({"title": ticket["title"], "status": f"filed {created['identifier']}"})
    return out


def linear_post(
    api_key: str,
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:  # pragma: no cover
    import urllib.request  # noqa: PLC0415

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310
            "https://api.linear.app/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read())

    return post


# --- approve -------------------------------------------------------------------------------


def approve(
    draft_path: Path,
    *,
    by: str,
    teardowns_path: Path,
    confirm: Callable[[str], bool],
    today: str | None = None,
) -> dict[str, Any]:
    """Re-review the draft, ask a person, then append it to the teardowns file. Raises
    ``SystemExit`` with the reason when the review fails or the person says no; the file is then
    untouched."""
    today = today or date.today().isoformat()
    data = td.load(teardowns_path)
    result = review(draft_path, teardowns=data, today=today)
    if not result["ok"]:
        failed = "; ".join(f"{c['name']}: {c['detail']}" for c in result["checks"] if not c["ok"])
        raise SystemExit(f"not approved, the review fails: {failed}")
    entry = json.loads(draft_path.read_text(encoding="utf-8"))["teardown"]
    teardown = td.Teardown.model_validate({**entry, "approved_by": by, "approved_on": today})
    question = (
        f"Add {teardown.id} '{teardown.name}' ({teardown.expressible}) to {teardowns_path.name}? "
        f"Preview: {draft_path.parent / result['preview']}  [y/N] "
    )
    if not confirm(question):
        raise SystemExit("not approved: no")
    updated = td.add_teardown(data, teardown, today=today)
    td.validate(updated)
    tmp = teardowns_path.with_suffix(".json.tmp")
    tmp.write_text(td.dumps(updated), encoding="utf-8")
    tmp.replace(teardowns_path)
    tickets = engine_tickets(teardown.id, result["engine_gaps"])
    (draft_path.parent / "engine-tickets.json").write_text(
        json.dumps(tickets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"approved": teardown.id, "version": updated["version"], "tickets": tickets}


# --- main ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intro_intake", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_draft = sub.add_parser("draft", help="draft a teardown from a screenshot or video")
    p_draft.add_argument("--image", action="append", default=[], type=Path)
    p_draft.add_argument("--video", action="append", default=[], type=Path)
    p_draft.add_argument("--en-title", required=True)
    p_draft.add_argument("--tr-title", required=True)
    p_draft.add_argument("--name")
    p_draft.add_argument("--note", default="")
    p_draft.add_argument("--id")
    mode = p_draft.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="ONE paid vision call (see --help)")
    mode.add_argument("--replay", type=Path, help="a recorded reply (JSON); no model call")
    p_review = sub.add_parser("review", help="check and draw a draft")
    p_review.add_argument("draft", type=Path)
    p_approve = sub.add_parser("approve", help="a person approves a draft into the file")
    p_approve.add_argument("draft", type=Path)
    p_approve.add_argument("--by", required=True, help="who approves (recorded in the file)")
    p_approve.add_argument("--yes", action="store_true", help="skip the y/N question")
    p_approve.add_argument("--file-tickets", action="store_true", help="needs LINEAR_API_KEY")
    for p in (p_draft, p_review, p_approve):
        p.add_argument("--teardowns", type=Path, default=td.TEARDOWNS_PATH)
    p_draft.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    args = parser.parse_args(argv)

    if args.command == "draft":
        data = td.load(args.teardowns)
        frames = read_frames(args.image, args.video)
        drafter = (
            live_drafter() if args.live else replay_drafter(args.replay) if args.replay else None
        )
        label = (args.image + args.video)[0].name
        path = draft(
            drafter=drafter,
            frames=frames,
            name=args.name,
            en_title=args.en_title,
            tr_title=args.tr_title,
            note=args.note,
            reference_label=label,
            teardowns=data,
            out_dir=args.out,
            source_kind="live" if args.live else "replay",
            ref_id=args.id,
        )
        if path is None:
            print(f"dry run: request written to {args.out}; add --live or --replay to draft")
            return 0
        print(f"draft: {path}\nnext: python -m app.cli.intro_intake review {path}")
        return 0
    if args.command == "review":
        result = review(args.draft, teardowns=td.load(args.teardowns))
        for check in result["checks"]:
            print(f"  [{'PASS' if check['ok'] else 'FAIL'}] {check['name']} {check['detail']}")
        for gap in result["engine_gaps"]:
            print(f"  new engine gap: {gap['piece']}: {gap['move']}")
        print(f"review: {'PASS' if result['ok'] else 'FAIL'}; read {args.draft.parent}/preview.png")
        return 0 if result["ok"] else 1
    done = approve(
        args.draft,
        by=args.by,
        teardowns_path=args.teardowns,
        confirm=(lambda _q: True) if args.yes else (lambda q: input(q).strip().lower() == "y"),
    )
    print(f"approved {done['approved']} -> version {done['version']}")
    for ticket in done["tickets"]:
        print(f"  engine ticket: {ticket['title']}")
    if args.file_tickets and done["tickets"]:
        key = os.environ.get("LINEAR_API_KEY")
        if not key:
            raise SystemExit("--file-tickets needs LINEAR_API_KEY")
        for item in file_tickets(done["tickets"], post=linear_post(key)):
            print(f"  {item['status']}: {item['title']}")
    elif done["tickets"]:
        print(f"  (dry run: tickets are in {args.draft.parent / 'engine-tickets.json'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
