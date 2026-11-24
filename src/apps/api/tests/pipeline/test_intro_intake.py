"""KRI-1614: the reference intake takes a screenshot to an approved teardown, with a person in the
loop, and costs nothing in tests.

Written before the script. Each test is a way the intake could go wrong:

* I1 a model reply carries text or an id the server never issues, and it lands in a teardown
  (`test_a_reply_*`): the reply schema is the closed one, so both are refused.
* I2 a draft reaches the shipped file without a person (`test_draft_never_touches_the_file`,
  `test_approve_*`): `draft` writes only under the intake folder; `approve` needs a passing review
  and a yes.
* I3 a paid call runs by accident (`test_draft_is_a_dry_run_without_live_or_replay`): neither flag
  means no model call at all. Tests only ever use a recorded reply.
* I4 a draft that lies is approved: it claims "expressible" but names a move, or its scene does not
  fit its title, or compiles to nothing (`test_review_*`).
* I5 an approval reformats the file or loses an entry (`test_dumps_*`, `test_approve_adds_one_*`).
* I6 the same missing move is ticketed again and again, or a ticket lacks its project, owner or
  labels (`test_engine_tickets_*`).
* I7 the intake only works on five references (`test_a_sixth_reference_*`): the end to end run goes
  from a screenshot to an approved sixth teardown in a copy of the file and draws it.
* I8 the video path cannot read a frame (`test_a_video_gives_frames`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import skia  # noqa: F401  - required: the review draws through the real renderer
from PIL import Image
from pydantic import ValidationError

from app.cli import intro_intake as intake
from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_teardowns as td

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "intro_intake"
REPLY = FIXTURES / "sixth_reference_reply.json"
EN, TR = "how i stopped overthinking", "düşünmeyi nasıl bıraktım"


@pytest.fixture
def file_copy(tmp_path: Path) -> Path:
    target = tmp_path / "intro_teardowns.json"
    shutil.copy(td.TEARDOWNS_PATH, target)
    return target


@pytest.fixture
def screenshot(tmp_path: Path) -> Path:
    path = tmp_path / "ref6.png"
    Image.new("RGB", (1170, 2532), (30, 30, 36)).save(path)  # a synthetic phone screenshot
    return path


def _draft(tmp_path: Path, screenshot: Path, teardowns: Path, reply: Path = REPLY) -> Path:
    path = intake.draft(
        drafter=intake.replay_drafter(reply),
        frames=intake.read_frames([screenshot], []),
        name=None,
        en_title=EN,
        tr_title=TR,
        note="",
        reference_label=screenshot.name,
        teardowns=td.load(teardowns),
        out_dir=tmp_path / "intake",
        source_kind="replay",
    )
    assert path is not None
    return path


def _reply(**over) -> dict:
    return {**json.loads(REPLY.read_text()), **over}


# --- I1: the reply is the closed schema ----------------------------------------------------


def test_a_reply_with_a_free_text_field_is_refused():
    with pytest.raises(ValidationError):
        intake.parse_reply(_reply(title_text="BIG NEWS"))
    scene = _reply()["scene_en"] | {"text": "hello"}
    with pytest.raises(ValidationError):
        intake.parse_reply(_reply(scene_en=scene))


def test_a_reply_with_an_id_the_server_never_issues_is_refused():
    scene = _reply()["scene_en"] | {"font_pair": "comic_sans"}
    with pytest.raises(ValidationError):
        intake.parse_reply(_reply(scene_en=scene))


def test_a_reply_in_a_code_fence_is_read():
    fenced = "```json\n" + REPLY.read_text() + "\n```"
    assert intake.parse_reply(fenced).scene_en is not None


def test_the_prompt_offers_only_issued_ids_and_numbers_the_persons_words():
    prompt = intake.build_prompt(EN, TR)
    for pair in scene_mod.FONT_PAIRS:
        assert pair in prompt
    for option in (*scene_mod.INKS, *scene_mod.ZONES, *scene_mod.ENTRANCES, *scene_mod.SOUNDS):
        assert option in prompt
    assert "1:how, 2:i, 3:stopped, 4:overthinking" in prompt
    assert "never write text for the screen" in prompt.lower()


# --- I2, I3: a person decides, nothing paid by accident -----------------------------------


def test_draft_is_a_dry_run_without_live_or_replay(tmp_path, screenshot, file_copy, capsys):
    before = file_copy.read_bytes()
    code = intake.main(
        [
            "draft",
            "--image",
            str(screenshot),
            "--en-title",
            EN,
            "--tr-title",
            TR,
            "--teardowns",
            str(file_copy),
            "--out",
            str(tmp_path / "intake"),
        ]
    )
    assert code == 0 and "dry run" in capsys.readouterr().out
    assert (tmp_path / "intake" / "ref6" / "request.txt").exists()
    assert not (tmp_path / "intake" / "ref6" / "draft.json").exists()
    assert file_copy.read_bytes() == before


def test_draft_never_touches_the_file(tmp_path, screenshot, file_copy):
    before = file_copy.read_bytes()
    path = _draft(tmp_path, screenshot, file_copy)
    assert path.parent.parent == tmp_path / "intake"
    assert file_copy.read_bytes() == before


def test_approve_refuses_when_the_person_says_no(tmp_path, screenshot, file_copy):
    draft = _draft(tmp_path, screenshot, file_copy)
    before = file_copy.read_bytes()
    with pytest.raises(SystemExit, match="no"):
        intake.approve(draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: False)
    assert file_copy.read_bytes() == before


def test_approve_asks_with_the_preview_path(tmp_path, screenshot, file_copy):
    draft = _draft(tmp_path, screenshot, file_copy)
    asked: list[str] = []
    intake.approve(
        draft, by="emir", teardowns_path=file_copy, confirm=lambda q: asked.append(q) or True
    )
    assert "preview.png" in asked[0] and "ref6" in asked[0]


# --- I4: a draft that lies is not approved ------------------------------------------------


def test_review_refuses_an_expressible_draft_that_names_a_missing_move(
    tmp_path, screenshot, file_copy
):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_reply(expressible="yes")))
    draft = _draft(tmp_path, screenshot, file_copy, reply=bad)
    result = intake.review(draft, teardowns=td.load(file_copy))
    assert not result["ok"]
    assert next(c for c in result["checks"] if c["name"] == "schema")["ok"] is False


def test_review_refuses_voices_that_do_not_match_the_persons_words(tmp_path, screenshot, file_copy):
    scene = _reply()["scene_en"] | {"voices": ["solid", "accent"]}  # 2 voices, 4 words
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_reply(scene_en=scene)))
    draft = _draft(tmp_path, screenshot, file_copy, reply=bad)
    result = intake.review(draft, teardowns=td.load(file_copy))
    assert not result["ok"]
    detail = next(c for c in result["checks"] if c["name"] == "schema")["detail"]
    assert "voices_not_one_per_word" in detail


def test_review_refuses_a_scene_the_engine_declines(tmp_path, screenshot, file_copy):
    # A long accent run at 250px in the loop face cannot sit clear of the head (S11).
    scene = {
        "decision": "compose",
        "voices": ["solid", "accent", "accent", "accent"],
        "font_pair": "grotesk_loop",
        "ink": "chartreuse",
        "zone": "above_head",
        "entrance": "fade",
        "sound": "none",
    }
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_reply(scene_en=scene)))
    draft = _draft(tmp_path, screenshot, file_copy, reply=bad)
    result = intake.review(draft, teardowns=td.load(file_copy))
    assert not result["ok"]
    assert [c["name"] for c in result["checks"] if not c["ok"]] == ["en_compiles"]
    with pytest.raises(SystemExit, match="review fails"):
        intake.approve(draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: True)


def test_review_refuses_an_id_that_is_not_the_next_free_one(tmp_path, screenshot, file_copy):
    draft = _draft(tmp_path, screenshot, file_copy)
    intake.approve(draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: True)
    with pytest.raises(SystemExit, match="id"):  # the same draft again: ref6 is taken
        intake.approve(draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: True)


# --- I5: the file stays byte-stable --------------------------------------------------------


def test_dumps_writes_the_file_exactly_as_it_is_today():
    data = td.load()
    assert td.dumps(data) == td.TEARDOWNS_PATH.read_text(encoding="utf-8")


def test_the_shipped_teardowns_all_validate_and_round_trip():
    data = td.load()
    for entry, teardown in zip(data["teardowns"], td.validate(data), strict=True):
        assert td.teardown_dict(teardown) == entry


def test_approve_adds_one_entry_and_the_version_and_changes_nothing_else(
    tmp_path, screenshot, file_copy
):
    before = file_copy.read_text(encoding="utf-8")
    draft = _draft(tmp_path, screenshot, file_copy)
    done = intake.approve(
        draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: True, today="2026-11-24"
    )
    after = file_copy.read_text(encoding="utf-8")
    assert done["approved"] == "ref6" and done["version"] == "2026-11-24-intake-ref6"
    old, new = json.loads(before), json.loads(after)
    assert new["teardowns"][:5] == old["teardowns"]
    assert {k: v for k, v in new.items() if k not in ("version", "teardowns")} == {
        k: v for k, v in old.items() if k not in ("version", "teardowns")
    }
    sixth = new["teardowns"][5]
    assert sixth["approved_by"] == "emir" and sixth["approved_on"] == "2026-11-24"
    assert td.dumps(new) == after  # still the file's own format
    td.validate(new)


# --- I6: engine tickets ---------------------------------------------------------------------


def test_engine_tickets_name_only_the_moves_no_teardown_or_limit_names(
    tmp_path, screenshot, file_copy
):
    draft = _draft(tmp_path, screenshot, file_copy)
    result = intake.review(draft, teardowns=td.load(file_copy))
    # the flash cover is already an engine limit, the dim layer is new
    assert [g["piece"] for g in result["engine_gaps"]] == ["background dim behind the title"]
    [ticket] = intake.engine_tickets("ref6", result["engine_gaps"])
    assert ticket["project"] == "AI-made intro scenes" and ticket["assignee"] == "me"
    assert ticket["labels"] == ["AI", "Improvement"] and ticket["team"] == "Kria"
    assert "dim layer" in ticket["description"] and "ref6" in ticket["description"]


def test_a_move_a_teardown_already_names_is_not_ticketed_again(tmp_path, screenshot, file_copy):
    draft = _draft(tmp_path, screenshot, file_copy)
    intake.approve(draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: True)
    again = td.Teardown.model_validate(json.loads(draft.read_text())["teardown"] | {"id": "ref7"})
    assert td.new_engine_gaps(td.load(file_copy), again) == []


def test_filing_tickets_skips_a_title_that_exists():
    calls: list[str] = []

    def post(query: str, variables: dict) -> dict:
        calls.append(query.split("(")[0].strip().split()[0])
        if "teams(" in query:
            return {
                "data": {
                    "teams": {"nodes": [{"id": "T"}]},
                    "projects": {"nodes": [{"id": "P"}]},
                    "issueLabels": {"nodes": [{"id": "L1"}, {"id": "L2"}]},
                    "viewer": {"id": "ME"},
                }
            }
        if "issues(" in query:
            nodes = [{"identifier": "KRI-1"}] if variables["t"].endswith("old") else []
            return {"data": {"issues": {"nodes": nodes}}}
        assert variables["i"]["assigneeId"] == "ME" and variables["i"]["projectId"] == "P"
        assert variables["i"]["labelIds"] == ["L1", "L2"]
        return {"data": {"issueCreate": {"issue": {"identifier": "KRI-9", "url": "u"}}}}

    tickets = intake.engine_tickets(
        "ref6",
        [{"piece": "old", "move": "m"}, {"piece": "fresh", "move": "m"}],
    )
    out = intake.file_tickets(tickets, post=post)
    assert [o["status"] for o in out] == ["exists KRI-1", "filed KRI-9"]


# --- I7: the end to end run ----------------------------------------------------------------


def test_a_sixth_reference_goes_from_a_screenshot_to_an_approved_teardown(
    tmp_path, screenshot, file_copy
):
    """A screenshot, a recorded reply, the real compile + Skia render + approval check, a yes,
    and the file holds six teardowns that the existing checks accept."""
    draft = _draft(tmp_path, screenshot, file_copy)
    folder = draft.parent
    assert (folder / "frame-0.jpg").exists() and (folder / "request.txt").exists()

    result = intake.review(draft, teardowns=td.load(file_copy))
    assert result["ok"], result["checks"]
    assert {c["name"] for c in result["checks"]} >= {
        "schema",
        "id",
        "en_renders",
        "tr_renders",
        "en_approval_reads_title",
        "tr_approval_reads_title",
    }
    preview = Image.open(folder / "preview.png")
    assert preview.width >= 3 * 360 and preview.height > 600  # reference + EN + TR

    done = intake.approve(
        draft, by="emir", teardowns_path=file_copy, confirm=lambda _q: True, today="2026-11-24"
    )
    data = td.load(file_copy)
    teardowns = td.validate(data)
    assert [t.id for t in teardowns] == ["ref1", "ref2", "ref3", "ref4", "ref5", "ref6"]
    assert teardowns[5].expressible == "partial" and set(teardowns[5].examples) == {"en", "tr"}
    assert [t["title"] for t in done["tickets"]] == [
        "Intro engine: background dim behind the title"
    ]
    assert json.loads((folder / "engine-tickets.json").read_text()) == done["tickets"]


# --- I8: video ------------------------------------------------------------------------------


def test_an_image_is_shrunk_to_a_frame_the_model_can_take(screenshot):
    [frame] = intake.read_frames([screenshot], [])
    import io

    image = Image.open(io.BytesIO(frame.data))
    assert max(image.size) <= intake.MAX_FRAME_PX and image.format == "JPEG"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_video_gives_frames(tmp_path):
    clip = tmp_path / "ref.mp4"
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=360x640:d=2:r=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip),
        ],
        check=True,
    )
    frames = intake.read_frames([], [clip])
    assert len(frames) == len(intake.VIDEO_FRACTIONS) and all(f.data for f in frames)
