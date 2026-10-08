"""KRI-527: every phone montage writer draws the creator's pinned corner text.

Failure modes (written before the code):

* the spoken-excerpt / voice-behind-footage / voiceover writers build their own text lanes and
  never drew a pin, so a contracted job failed verification ("missing confirmed on-screen
  text") and an uncontracted one failed the brief receipt ("couldn't verify");
* a pin is drawn on the recipe but not on the variant row, so the editor preview (which
  compiles text from the row) shows nothing - or the row claims a pin the recipe never drew;
* a pin with no usable window (past the end, a clip that is not there) is claimed anyway;
* adding pin layers drops the intro/title layers or the bundled fonts they need;
* a pin-free job changes shape (text layers, variant text, record) - must stay identical;
* the opening title lands on top of a top-corner pin.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import plan_facts_from_speech_montage
from app.pipeline.phone_narrated_plan import with_pinned_text_layers
from app.schemas.edit_proposal import PinnedText
from app.services.device_render import DEVICE_RENDER_FIELD
from tests.pipeline.test_phone_voiceover_montage_plan import (
    compile_phone_voiceover_montage_plan,
    fixture,
)
from tests.services.test_phone_speech_montage_job import (  # noqa: F401
    _plan,
    _run,
    world,
)
from tests.services.test_phone_voice_behind_footage_job import (
    _recipe as vbf_recipe,
)
from tests.services.test_phone_voice_behind_footage_job import (
    _run as vbf_run,
)
from tests.services.test_phone_voice_behind_footage_job import (
    _strategy as vbf_strategy,
)
from tests.services.test_phone_voice_behind_footage_job import (
    _world as vbf_world,
)

TOP = {"text": "Part 1", "corner": "top_left"}
BOTTOM = {"text": "Day one", "corner": "bottom_right", "clip": 1}


def _verify_text_features(monkeypatch) -> None:
    """Pins are positioned text in the default Fraunces (a variable font => authoredText).
    Production verifies both for the unified montage's identical pins."""
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        sorted({*settings.phone_render_verified_features, "positionedText", "authoredText"}),
    )


@pytest.fixture
def speech_world(request, monkeypatch):
    """The spoken-excerpt job world, with the text features pins need verified."""
    state = request.getfixturevalue("world")
    _verify_text_features(monkeypatch)
    return state


def _texts(recipe) -> list[str]:  # noqa: ANN001
    # Runs are positioned words, so compare without spaces.
    return sorted(
        "".join(run.text for run in layer.runs).replace(" ", "") for layer in recipe.text_layers
    )


# -- spoken-excerpt montage ------------------------------------------------------------------


def _speech_plan():  # noqa: ANN202
    return _plan(
        {"kind": "montage", "duration_s": 3.0},
        {"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"},
    )


def test_the_spoken_excerpt_montage_draws_pins_on_recipe_row_and_record(speech_world) -> None:
    speech_world.candidates["creator_strategy"] = {"pinned_texts": [TOP, BOTTOM]}
    assert _run(speech_world, _speech_plan()) is True

    variant = speech_world.job.assembly_plan["variants"][0]
    assert [row["id"] for row in variant["text_elements"]] == [
        "guided-pinned-0",
        "guided-pinned-1",
    ]
    record = speech_world.job.assembly_plan["speech_montage"]
    assert [pin["text"] for pin in record["pinned_texts"]] == ["Part 1", "Day one"]

    recipe = speech_world.job.assembly_plan[DEVICE_RENDER_FIELD]["speech_montage"]["status"][
        "request"
    ]["recipe"]
    layers = recipe["text_layers"]
    assert len(layers) == 2 and "positionedText" in recipe["required_capabilities"]
    # The clip-scoped pin is on screen for its clip only, the other for the whole video.
    whole, scoped = layers
    total = variant["duration_s"]
    assert whole["start"] == 0.0 and whole["end"] == pytest.approx(total, abs=0.05)
    # "clip 1" is the first video clip on the timeline, not the whole video.
    first = next(t for t in recipe["tracks"] if t["kind"] == "video")["clips"][0]
    assert scoped["start"] == pytest.approx(first["timeline_start"], abs=0.05)
    assert scoped["end"] == pytest.approx(
        first["timeline_start"] + first["source_duration"], abs=0.05
    )

    # What the brief receipt reads: the lines actually drawn.
    assert plan_facts_from_speech_montage(record).texts == ("Part 1", "Day one")


def test_a_brief_text_requirement_is_met_by_a_drawn_pin_not_unverifiable(
    speech_world, monkeypatch
) -> None:
    from app.tasks import generative_build as gb

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r0", kind="text", scope="global", description="on-screen text", literal="Part 1"
            )
        ],
    )
    monkeypatch.setattr(
        gb,
        "_load_unified_montage_inputs",
        lambda _id: (speech_world.job.user_id, speech_world.assignments, brief),
    )
    speech_world.candidates["creator_strategy"] = {"pinned_texts": [TOP]}
    assert _run(speech_world, _speech_plan()) is True
    receipts = speech_world.job.assembly_plan["speech_montage"]["requirement_receipts"]
    [text_receipt] = [r for r in receipts if r["requirement_id"] == "r0"]
    assert text_receipt["status"] == "met"  # not "partial: that exact text isn't in this draft"


def test_a_pin_with_no_window_is_neither_drawn_nor_claimed(speech_world) -> None:
    speech_world.candidates["creator_strategy"] = {
        "pinned_texts": [
            {"text": "Ghost", "corner": "top_left", "clip": 40},
            {"text": "Late", "corner": "top_left", "start_s": 900},
        ]
    }
    assert _run(speech_world, _speech_plan()) is True
    assert speech_world.job.assembly_plan["variants"][0]["text_elements"] == []
    assert "pinned_texts" not in speech_world.job.assembly_plan["speech_montage"]


def test_a_pin_free_spoken_excerpt_montage_is_unchanged(speech_world) -> None:
    assert _run(speech_world, _speech_plan()) is True
    assert speech_world.job.assembly_plan["variants"][0]["text_elements"] == []
    assert "pinned_texts" not in speech_world.job.assembly_plan["speech_montage"]
    recipe = speech_world.job.assembly_plan[DEVICE_RENDER_FIELD]["speech_montage"]["status"][
        "request"
    ]["recipe"]
    assert recipe["text_layers"] == []


# -- voice behind footage --------------------------------------------------------------------


def test_voice_behind_footage_draws_pins_and_passes_the_contract(monkeypatch) -> None:
    strategy = vbf_strategy(pinned_texts=[TOP, BOTTOM])
    vworld = vbf_world(monkeypatch, strategy=strategy)
    _verify_text_features(monkeypatch)
    # The contract demands the exact pin text (role "any"): verification runs inside the job.
    assert {t.text for t in vworld.contract.exact_texts} >= {"Part 1", "Day one"}
    assert vbf_run(vworld) is True

    recipe = vbf_recipe(vworld)
    assert _texts(recipe) == sorted(["SummerinLisbon", "Part1", "Dayone"])
    ids = [layer.id for layer in recipe.text_layers]
    assert len(set(ids)) == len(ids)  # the title and the pins never share a layer id
    variant = vworld.job.assembly_plan["variants"][0]
    assert [row["id"] for row in variant["text_elements"]] == ["guided-pinned-0", "guided-pinned-1"]
    record = vworld.job.assembly_plan["speech_montage"]
    assert [p["text"] for p in record["pinned_texts"]] == ["Part 1", "Day one"]


def test_the_voice_behind_footage_title_steps_below_a_top_pin(monkeypatch) -> None:
    def title_anchor_y(strategy: dict) -> float:
        vworld = vbf_world(monkeypatch, strategy=strategy)
        _verify_text_features(monkeypatch)
        assert vbf_run(vworld) is True
        layer = next(layer for layer in vbf_recipe(vworld).text_layers if layer.id == "title-0")
        return layer.anchor_y

    plain = title_anchor_y(vbf_strategy())
    assert title_anchor_y(vbf_strategy(pinned_texts=[TOP])) > plain
    # A bottom pin never meets the title.
    assert title_anchor_y(vbf_strategy(pinned_texts=[BOTTOM])) == plain


# -- recorded-voiceover montage --------------------------------------------------------------


def test_the_voiceover_montage_keeps_its_intro_and_fonts_and_adds_pins() -> None:
    decision, bindings = fixture()
    plain = compile_phone_voiceover_montage_plan(decision, bindings, music=None)
    pins = [PinnedText(**TOP), PinnedText(text="Day two", corner="bottom_left", clip=2)]
    recipe, rows = with_pinned_text_layers(plain, pins)

    assert [row["id"] for row in rows] == ["guided-pinned-0", "guided-pinned-1"]
    # The intro (reveal + hold) is untouched and the pins are added after it.
    assert [layer.id for layer in recipe.text_layers[: len(plain.text_layers)]] == [
        layer.id for layer in plain.text_layers
    ]
    assert len(recipe.text_layers) == len(plain.text_layers) + 2
    assert len({layer.id for layer in recipe.text_layers}) == len(recipe.text_layers)
    # No bundled font the intro needed was lost when the manifest was rebuilt.
    fonts = lambda r: {a.id for a in r.asset_manifest.assets if a.id.startswith("font-")}  # noqa: E731
    assert fonts(plain) <= fonts(recipe)
    assert "positionedText" in recipe.required_capabilities
    recipe.model_validate(recipe.model_dump(mode="json"))  # still a valid recipe

    # "clip 2" is the second clip: it starts where the second step does.
    second_clip = recipe.tracks[0].clips[1]
    scoped = recipe.text_layers[-1]
    assert scoped.start == pytest.approx(second_clip.timeline_start, abs=0.05)


def test_a_pin_free_voiceover_montage_recipe_is_untouched() -> None:
    decision, bindings = fixture()
    plain = compile_phone_voiceover_montage_plan(decision, bindings, music=None)
    same, rows = with_pinned_text_layers(plain, None)
    assert same is plain and rows == []
