"""KRI-479 review P2-4: the REAL rendered text of every question kind reads as English.

Failure modes: a capitalised reason dropped into the 'Unfortunately {reason}' template
('Unfortunately Only one recording...'), 'The most I can do is: 60 seconds' where 33 works, a
doubled space, a missing full stop. A table over every kind the collector can produce.
"""

from __future__ import annotations

import re

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.choice_questions import (
    ChoiceCapability,
    choice_question_text,
    collect_conflicts,
    title_text_choice,
)

CAP = ChoiceCapability(voice_route=True)


def _rows(count, *, dated=True, seconds=12.0):
    rows = []
    for i in range(count):
        row = {"media_id": f"c{i:02d}", "kind": "video", "duration_s": seconds}
        if dated:
            row["capture"] = {"capture_time": f"2026-06-01T{9 + i // 60:02d}:{i % 60:02d}:00Z"}
        rows.append(row)
    return rows


def _voice(voice_s=147.7, pictures=6, seconds=12.0):
    voice = {
        "media_id": "talk",
        "kind": "video",
        "duration_s": voice_s,
        "capture": {"capture_time": "2026-06-01T08:00:00Z"},
        "analysis": {
            "understanding": {
                "speech": {"has_speech": True, "to_camera": True, "transcript": "hello there you"}
            }
        },
    }
    return [voice, *_rows(pictures, seconds=seconds)]


def _brief(*reqs):
    return CreativeBrief(version=1, requirements=list(reqs))


def _timing(seconds):
    return BriefRequirement(
        id="t", kind="timing", scope="global", description=f"Keep {seconds} seconds",
        facts={"duration_s": seconds},
    )  # fmt: skip


def _order():
    return BriefRequirement(
        id="o", kind="order", scope="global", description="in the order I filmed",
        facts={"key": "capture_time"},
    )  # fmt: skip


def _strategy(**update):
    base = {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "voice_mode": "continuous",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["talk"]},
        "ordering_choice": "chronological",
        "target_duration_s": 30,
        "target_duration_requested": True,
    }
    return CreativeStrategy.model_validate({**base, **update})


def _texts() -> dict[str, str]:
    out: dict[str, str] = {}

    def add(name, strategy, brief, rows):
        found = collect_conflicts(strategy, brief, {"clip_assignments": rows}, CAP)
        assert found, name
        out[name] = choice_question_text(found[0].candidate())

    plain = CreativeStrategy(
        edit_format="montage", target_duration_s=15, target_duration_requested=True
    )
    add("duration_vs_count", plain, _brief(_timing(15)), _rows(30, seconds=3.0))
    add(
        "order_basis",
        CreativeStrategy(edit_format="montage", ordering_choice="chronological"),
        _brief(_order()),
        _rows(5, dated=False),
    )
    add("voice_short", _strategy(), _brief(_timing(30), _order()), _voice(voice_s=20.0))
    add("how_long_many", _strategy(), _brief(_order()), _voice())
    add("how_long_single", _strategy(), _brief(_order()), _voice(pictures=41, seconds=0.9))
    two = _strategy(
        montage_audio={"preserve_source_audio": True, "source_media_ids": ["talk", "c00"]}
    )
    add("which_voice", two, _brief(_timing(30)), _voice())
    out["title_text"] = choice_question_text(title_text_choice(["r1"]).candidate())
    return out


TEXTS = _texts()


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_no_capitalised_reason_after_unfortunately_and_no_stray_spacing(name):
    text = TEXTS[name]
    assert not re.search(r"Unfortunately [A-Z]", text), text
    assert "  " not in text and " ." not in text and ".." not in text, text
    assert text.strip() == text
    assert all(line.strip() for line in text.splitlines()), text
    assert text.splitlines()[0].endswith((".", "?", "!", ":")), text


def test_the_voice_questions_read_as_written():
    assert TEXTS["which_voice"].splitlines()[0] == (
        "More than one of your clips has someone talking. Unfortunately only one recording "
        "can be the voice that plays under the rest. Which do you prefer?"
    )
    assert TEXTS["how_long_many"].splitlines()[0] == (
        "Your voice runs 147.7 seconds. How long should the video be?"
    )
    assert "5 seconds (recommended)" in TEXTS["how_long_many"]


def test_a_single_length_is_a_statement_not_the_most_i_can_do():
    assert TEXTS["how_long_single"] == (
        "Your voice runs 147.7 seconds. 41 clips need at least 32.8 seconds to each be seen. "
        'I can make it 33 seconds. Reply "33 seconds" to go with that, or change your request.'
    )
