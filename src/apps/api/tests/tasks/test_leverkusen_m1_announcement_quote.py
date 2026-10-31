"""A sound ask that names its clip by a quoted line keeps that line, so the line plays whole.

Incident (prod thread 1a2189cc, 2026-10-30, Leverkusen kit M1 run 2, job 93f662a7): "Keep the
sound only on the train clip with the announcement "Nächster Halt: Leverkusen Mitte" and mute
everything else." The brief writer recorded r4 as "Keep the sound only on the train clip with the
announcement and mute everything else": the creator's own sentence with the quoted line cut out.
KRI-1028 (`kept_sound_lines`) looks for the named line only in r4's words, found none, and treated
the train clip as kept without a line: no ``kept_lines``, so the length plan played the clip's
visual best moment (2.5-4.5 s, "train moving past lights"), the finished video says "...Leverkusen
Mitte." without "Nächster Halt", and r4 was "met" from the mute alone (approval card "Done").

Measured on the kit's source audio (``measured`` in the fixture) the line runs 1.9-4.1 s.

Ways this can fail, each pinned below:
1. The cut quote is not put back: the clip plays its visual moment and the line is cut.
2. The line is put back, but the window seated on the clip's timed segment (2.22-4.30 s, late
   at the start) still misses the measured line.
3. A quote from another sentence (the title "Pokalnacht in Leverkusen 🔴⚫") lands in the ask.
4. Words the creator didn't write are rewritten: a paraphrase, or a run said twice.
5. Restored words over the description limit break the whole brief batch.
6. A text requirement's quote is copied into its description (its shot key and display change).
7. A description that kept the quote (KRI-1028's own case) changes.
8. A later turn that changes the sound ask without repeating the quote drops it again.
9. A video too short for the line says "Done" instead of saying the line is cut.
10. The mute itself changes (the facts the clip-sound toggle reads).
11. The replay is not the incident: on the model's own words it must plan prod's exact windows.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_checks import PlanFacts, check_requirement
from app.kria.planner import _enrich_best_parts_snapshot

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "leverkusen_m1_train_announcement.json"
)
DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
MESSAGE = DATA["creator_message"]
TRAIN = "IMG_1002"
LINE = DATA["measured"]["line"]
SENTENCE = tuple(DATA["measured"]["sentence_s"])
FIRST_TURN = "78855290-993b-4696-ada2-dc121510ced2"
QUOTED = (
    'Keep the sound only on the train clip with the announcement "Nächster Halt: Leverkusen '
    'Mitte" and mute everything else'
)
CUT = "Keep the sound only on the train clip with the announcement and mute everything else"


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _rows() -> list[dict]:
    return copy.deepcopy(DATA["brief"]["requirements"])


def _first_turn_updates() -> list[BriefUpdate]:
    """What the brief writer returned for the creator's first message, in brief order."""
    return [
        BriefUpdate(
            kind=row["kind"],
            scope=row["scope"],
            literal=row["literal"],
            description=row["description"],
            facts=row["facts"],
        )
        for row in _rows()
        if row["source_turn_id"] == FIRST_TURN
    ]


def _audio(brief: CreativeBrief) -> BriefRequirement:
    (req,) = [r for r in brief.requirements if r.kind == "audio"]
    return req


def _approved_brief(description: str | None = None) -> CreativeBrief:
    """The approved v3 brief, its r4 worded as the first turn's write records it."""
    if description is None:
        written = apply_updates(
            None, _first_turn_updates(), source_turn_id=FIRST_TURN, creator_text=MESSAGE
        )
        description = _audio(written).description
    rows = _rows()
    for row in rows:
        if row["id"] == "r4":
            row["description"] = description
    return CreativeBrief(
        version=DATA["brief"]["version"], requirements=[BriefRequirement(**r) for r in rows]
    )


def _plan(brief: CreativeBrief, length_s: float = 21) -> dict:
    strategy = {**DATA["strategy"], "creator_duration_s": length_s, "target_duration_s": length_s}
    snapshot = {
        "clip_assignments": [
            {
                **copy.deepcopy(c),
                "gcs_path": f"users/u/{c['media_id']}.mp4",
                "storage_generation": "1",
            }
            for c in DATA["clips"]
        ],
        "visuals": copy.deepcopy(DATA["visuals"]),
    }
    return _enrich_best_parts_snapshot(
        strategy,
        snapshot,
        brief,
        visual_photo_ids=DATA["visual_ids"],
        attached_visual_ids=[v["media_id"] for v in DATA["visuals"]],
    )


def _window(snapshot: dict) -> tuple[float, float]:
    (row,) = [w for w in snapshot["length_plan"]["windows"] if w["media_id"] == TRAIN]
    return row["source_start_s"], row["source_end_s"]


def _holds(start: float, end: float, span: tuple[float, float]) -> bool:
    return start <= span[0] + 0.06 and end >= span[1] - 0.06


def _facts(snapshot: dict) -> PlanFacts:
    return PlanFacts(
        phone_montage=True,
        media_snapshot=snapshot,
        clip_ids=tuple(c["media_id"] for c in DATA["clips"]),
    )


# -- the incident ----------------------------------------------------------------------


def test_replay_on_the_models_words_is_the_incidents_plan() -> None:
    """11: the brief as persisted plans prod's approved windows, the train clip at 2.5-4.5 s,
    and claims no line for it (the incident, not a lookalike)."""
    snapshot = _plan(_approved_brief(CUT))
    approved = [
        (w["media_id"], w["source_start_s"], w["source_end_s"]) for w in DATA["approved_windows"]
    ]
    assert [
        (w["media_id"], w["source_start_s"], w["source_end_s"])
        for w in snapshot["length_plan"]["windows"]
    ] == approved
    assert _window(snapshot) == (2.5, 4.5)
    assert not _holds(*_window(snapshot), SENTENCE)
    assert "kept_lines" not in snapshot["length_plan"]
    assert snapshot["length_plan"]["conflicts"] == []


def test_the_brief_keeps_the_quoted_announcement() -> None:
    """1, 3, 10: the sound ask keeps the creator's quoted line; nothing else changes."""
    updates = _first_turn_updates()
    brief = apply_updates(None, updates, source_turn_id=FIRST_TURN, creator_text=MESSAGE)
    assert _audio(brief).description == QUOTED
    assert _audio(brief).facts == next(u.facts for u in updates if u.kind == "audio")
    others = [
        (r.kind, r.scope, r.literal, r.description, r.facts)
        for r in brief.requirements
        if r.kind != "audio"
    ]
    assert others == [
        (u.kind, u.scope, u.literal, u.description, u.facts) for u in updates if u.kind != "audio"
    ]


def test_the_approved_plan_plays_the_whole_announcement() -> None:
    """1, 2: the train clip's window holds the measured line and the plan records it held."""
    snapshot = _plan(_approved_brief())
    start, end = _window(snapshot)
    assert _holds(start, end, SENTENCE), (start, end)
    (record,) = snapshot["length_plan"]["kept_lines"]
    assert record["media_id"] == TRAIN and record["requirement_id"] == "r4"
    assert record["held"] is True
    assert snapshot["length_plan"]["conflicts"] == []
    total = sum(w["source_end_s"] - w["source_start_s"] for w in snapshot["length_plan"]["windows"])
    assert total == pytest.approx(snapshot["length_plan"]["target_duration_s"], abs=0.05)


def test_the_draft_says_the_line_plays_whole() -> None:
    brief = _approved_brief()
    receipt = check_requirement(_audio(brief), _facts(_plan(brief)))
    assert receipt.status == "met"
    assert receipt.reason == (
        'Clip sound off except that clip; "Nächster Halt Leverkusen Mitte" plays whole'
    )


def test_a_video_too_short_for_the_line_says_so() -> None:
    """9: a 3 s video can't hold the 3.2 s window: partial, never "Done"."""
    brief = _approved_brief()
    snapshot = _plan(brief, length_s=3)
    assert snapshot["length_plan"]["kept_lines"][0]["held"] is False
    receipt = check_requirement(_audio(brief), _facts(snapshot))
    assert receipt.status == "partial"
    assert receipt.reason == (
        "\"Nächster Halt Leverkusen Mitte\" doesn't fit whole in the video's length, so part "
        "of it is cut"
    )


# -- what the brief write leaves alone -------------------------------------------------


def _written(description: str, message: str, **update: object) -> BriefRequirement:
    fields = {
        "kind": "audio",
        "scope": "global",
        "literal": None,
        "description": description,
        "facts": {"clip_sound": "off", "sound_except": [{"sound_media": TRAIN}]},
        **update,
    }
    brief = apply_updates(None, [BriefUpdate(**fields)], source_turn_id="t", creator_text=message)
    return brief.requirements[0]


@pytest.mark.parametrize(
    ("description", "message", "expected"),
    [
        pytest.param(
            "Keep the sound of the train announcement",
            'Keep the sound of the train announcement "Nächster Halt: Leverkusen Mitte". Mute '
            "the rest.",
            'Keep the sound of the train announcement "Nächster Halt: Leverkusen Mitte"',
            id="quote-right-after-the-words",
        ),
        pytest.param(
            "Sadece anonsun olduğu tren klibinin sesi kalsın",
            'Sadece "Nächster Halt: Leverkusen Mitte" anonsun olduğu tren klibinin sesi kalsın, '
            "gerisi sessiz olsun.",
            'Sadece "Nächster Halt: Leverkusen Mitte" anonsun olduğu tren klibinin sesi kalsın',
            id="tr-quote-inside",
        ),
        pytest.param(
            "Only the train clip keeps its sound",
            'Keep the sound only on the train clip with "Nächster Halt: Leverkusen Mitte".',
            "Only the train clip keeps its sound",
            id="4-paraphrase",
        ),
        pytest.param(
            "Keep the sound only on the train clip",
            'Keep the sound only on the train clip. Title: "Pokalnacht in Leverkusen".',
            "Keep the sound only on the train clip",
            id="3-quote-from-another-sentence",
        ),
        pytest.param(
            "keep the sound on the train clip",
            'keep the sound on the train clip "Nächster Halt: Leverkusen Mitte", I said keep '
            'the sound on the train clip "Aachen Hauptbahnhof"',
            "keep the sound on the train clip",
            id="4-said-twice",
        ),
        pytest.param(
            CUT,
            f"{CUT.replace('announcement', 'announcement ' + chr(34) + 'x ' * 140 + chr(34))}",
            CUT,
            id="5-too-long",
        ),
        pytest.param(QUOTED, f"{QUOTED}.", QUOTED, id="7-quote-kept"),
    ],
)
def test_only_a_quote_cut_from_the_creators_own_words_is_put_back(
    description: str, message: str, expected: str
) -> None:
    assert _written(description, message).description == expected


def test_a_text_requirement_is_never_touched() -> None:
    """6: the title's quote is its literal; its description stays the model's."""
    req = _written(
        "The title",
        'The title "Pokalnacht in Leverkusen" please',
        kind="text",
        scope="title",
        literal="Pokalnacht in Leverkusen",
        facts={},
    )
    assert req.description == "The title"


def test_a_later_change_keeps_the_quote() -> None:
    """8: "keep the goal clip's sound too" rewrites r4 without the quote; the line stays."""
    first = apply_updates(
        None, _first_turn_updates(), source_turn_id=FIRST_TURN, creator_text=MESSAGE
    )
    r4 = _audio(first)
    change = BriefUpdate(
        operation="change",
        target_requirement_id=r4.id,
        expected_version=first.version,
        kind="audio",
        scope="global",
        description=CUT,
        facts={
            "clip_sound": "off",
            "sound_except": [{"sound_media": TRAIN}, {"sound_media": "IMG_1037"}],
        },
    )
    later = apply_updates(
        first, [change], source_turn_id="t2", creator_text="Keep the goal clip's sound too."
    )
    assert _audio(later).description == QUOTED
