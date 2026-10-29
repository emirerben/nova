"""KRI-1027: the bars/crop a brief asks for comes from the recorded fact (KRI-667).

Failure modes (written before the code): a recorded fact ignored; an unknown fact value
trusted; a superseded requirement still applied; words guessed when no fact was recorded
(only the KRI-547 vertical reader Talking already uses may read them); an older opposite
ask outliving a later correction.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.landscape_fit_ask import brief_landscape_fit_ask, requirement_landscape_fit


def _req(rid="r1", description="make it all vertical", facts=None, kind="style", status="open"):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope="global",
        description=description,
        facts=dict(facts or {}),
        status=status,
    )


@pytest.mark.parametrize("fit", ["fill", "fit"])
def test_the_recorded_fact_is_the_ask(fit):
    assert requirement_landscape_fit(_req(facts={"landscape_fit": fit})) == fit


def test_the_fact_wins_over_the_words():
    # "vertical, but keep the black bars": the model records the bars.
    req = _req(
        description="make it vertical but keep the black bars", facts={"landscape_fit": "fit"}
    )
    assert requirement_landscape_fit(req) == "fit"


@pytest.mark.parametrize("value", ["crop", "bars", "", None, 1, True])
def test_an_unknown_fact_value_asks_nothing(value):
    assert (
        requirement_landscape_fit(_req(description="faster cuts", facts={"landscape_fit": value}))
        is None
    )


def test_no_fact_reads_only_the_kri547_vertical_words():
    assert requirement_landscape_fit(_req(description="make it vertical")) == "fill"
    assert requirement_landscape_fit(_req(description="no black bars")) == "fill"
    # As prod stored M1's r2 (v100): words the KRI-547 reader does not know stay unread.
    assert requirement_landscape_fit(_req(description="make it all vertical")) is None
    assert requirement_landscape_fit(_req(description="keep the black bars")) is None


def test_a_superseded_requirement_asks_nothing():
    req = _req(facts={"landscape_fit": "fill"}, status="superseded")
    assert requirement_landscape_fit(req) is None


def test_the_newest_ask_wins_and_cites_only_its_own_requirements():
    brief = CreativeBrief(
        version=1,
        requirements=[
            _req("r1", facts={"landscape_fit": "fill"}),
            _req("r2", description="no black bars"),
            _req("r3", description="faster cuts"),
        ],
    )
    ask = brief_landscape_fit_ask(brief)
    assert (ask.fit, ask.requirement_ids) == ("fill", ("r1", "r2"))
    brief.requirements.append(
        _req("r4", description="actually keep the black bars", facts={"landscape_fit": "fit"})
    )
    ask = brief_landscape_fit_ask(brief)
    assert (ask.fit, ask.requirement_ids) == ("fit", ("r4",))
    assert brief_landscape_fit_ask(None) is None
    assert brief_landscape_fit_ask(CreativeBrief()) is None
