"""KRI-1556 A1: one placement decision per passage, as a matrix of failure modes.

Ways this can go wrong (written before the code):
 shuffle     the pool's order decides which picture goes where, so a re-uploaded pool differs;
 decoy       a picture that shares one word with the speech but shows something else is placed;
 none        nothing fits a passage and a picture is placed anyway (or the reply is silent);
 paraphrase  the creator says it in other words and the picture is lost;
 slideshow   an explicit order is shuffled or a fixed order is applied when none was asked for;
 pace        the asked pace is stretched to fill the take when pictures run out;
 after_cut   a speech cut removes the words a picture sits on and the picture stays.
 off         the flag off is not byte-identical to today (prompt, receipt, cards).

Model rows REPLAY recorded answers through the real `OverlayPlacementAgent` (`render_prompt`,
`parse`, the real client seam) into the real `ground_phone_subtitled_overlays`. `replays.json` is
hand-authored contract data: it pins what the code does with a well-formed answer, not what the
real model answers. `expected.json` says what each passage is about, independent of any matcher
output.

Known gap, pending A4 (KRI-1556): `heuristic_match` treats two shared tokens as a match, so a decoy
that shares two words is placed on the no-model path; its tokeniser is ASCII-only, so Turkish
diacritics split words. The `heuristic` rows below stay inside what it can do.
"""

from __future__ import annotations

import json
import uuid
from itertools import permutations
from pathlib import Path

import pytest

import app.agents._model_client as model_client
import app.services.phone_overlay_grounding as pg
from app.agents._runtime import ModelClient, ModelInvocation
from app.config import settings
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    SubtitledOverlayCard,
    remap_lanes_for_cut,
)
from app.services.passage_placement import (
    PlacementCandidate,
    PlacementRequest,
    passages_from_words,
    place_passages,
)
from tests.pipeline.test_phone_subtitled_plan import _cut_plan

FIXTURES = Path(__file__).parents[1] / "fixtures" / "passage_placement"
REPLAYS = json.loads((FIXTURES / "replays.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
SENTENCE_STARTS_S = (3.0, 12.0, 21.0)
DURATION_S = 30.0

SENTENCES = {
    "en": {
        "p1": "First I mix the tahini with brown sugar",
        "p1_paraphrase": "First I whisk sesame paste into raw sugar",
        "p2": "Then the cookies bake for twelve minutes",
        "p3": "Honestly I just love Sunday mornings",
    },
    "tr": {
        "p1": "Önce tahini esmer şekerle karıştırıyorum",
        "p1_paraphrase": "Önce susam ezmesini ham şekere katıyorum",
        "p2": "Sonra kurabiyeler on iki dakika pişiyor",
        "p3": "Pazar sabahlarını gerçekten çok seviyorum",
    },
}
REQUEST = {"en": "add pictures", "tr": "görseller koy"}
POOL_TEXT = {
    "en": {
        "asset_tahini": (
            "tahini and brown sugar",
            "bowl of tahini paste next to brown sugar",
        ),
        "asset_cookies": ("cookies", "tray of cookies, oven timer set to twelve minutes"),
        "asset_decoy": ("brown leather sofa", "a brown leather sofa in a living room"),
        "asset_lamp": ("floor lamp", "a tall floor lamp in a corner"),
    },
    "tr": {
        "asset_tahini": ("tahini ve esmer şeker", "tahini ezmesi ve esmer şeker kasesi"),
        "asset_cookies": ("kurabiye tepsisi", "fırın zamanlayıcısı on iki dakika kurabiye tepsisi"),
        "asset_decoy": ("esmer deri koltuk", "salonda esmer deri koltuk"),
        "asset_lamp": ("ayaklı lamba", "köşede uzun bir ayaklı lamba"),
    },
}


def _words(lang: str, *keys: str) -> list[dict]:
    out: list[dict] = []
    for start, key in zip(SENTENCE_STARTS_S, keys, strict=True):
        for i, text in enumerate(SENTENCES[lang][key].split()):
            t = start + 0.4 * i
            out.append({"text": text, "start_s": t, "end_s": t + 0.35, "confidence": 1.0})
    return out


def _row(lang: str, asset_id: str) -> dict:
    subject, description = POOL_TEXT[lang][asset_id]
    return {
        "id": asset_id,
        "gcs_path": f"users/u1/plan/item1/pool/{asset_id}.png",
        "gcs_generation": "7",
        "kind": "image",
        "source_filename": "",
        "duration_s": None,
        "aspect": 1.0,
        "user_context": "",
        "analysis": {"subject": subject, "description": description},
    }


class _Replay(ModelClient):
    def __init__(self, answer: dict | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def invoke(self, *, model, prompt, **kwargs):  # noqa: ANN001, ANN003, ANN201, ARG002
        self.prompts.append(prompt)
        assert self.answer is not None, "no recorded answer was expected to be needed"
        return ModelInvocation(
            raw_text=json.dumps(self.answer), tokens_in=0, tokens_out=0, raw_response=None
        )


def _ground(
    monkeypatch,  # noqa: ANN001
    lang: str,
    keys: tuple[str, str, str],
    pool: list[str],
    *,
    answer: str | None,
    request: str = "",
    flag: bool = True,
):
    """`ground_phone_subtitled_overlays` as the Talking worker calls it, model answer replayed."""
    client = _Replay(REPLAYS[lang][answer] if answer else None)
    monkeypatch.setattr(model_client, "default_client", lambda: client)
    monkeypatch.setattr("app.agents._persistence.persist_agent_run", lambda **kw: None)
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key" if answer else "", raising=False)
    monkeypatch.setattr(settings, "passage_placement_enabled", flag, raising=False)
    rows = [_row(lang, a) for a in pool]
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: rows)
    result = pg.ground_phone_subtitled_overlays(
        lambda: None,
        job_id=str(uuid.uuid4()),
        words=_words(lang, *keys),
        duration_s=DURATION_S,
        clip_path=None,
        spoken_language=lang,
        **({"creator_request": request} if request else {}),
    )
    return result, client


def _by_passage(result) -> dict[str, str | None]:  # noqa: ANN001
    """Which picture sits on which sentence, read off the final cards' start times."""
    table: dict[str, str | None] = {f"{s:.1f}": None for s in SENTENCE_STARTS_S}
    for card in result.cards:
        sentence = max(s for s in SENTENCE_STARTS_S if s <= card.start_s)
        assert table[f"{sentence:.1f}"] is None, "two cards opened on one passage"
        table[f"{sentence:.1f}"] = card.media_id
    return table


def violations(table: dict[str, str | None], expected: dict[str, str | None]) -> list[str]:
    return [
        f"passage {k}: expected {expected[k]!r}, got {table.get(k)!r}"
        for k in expected
        if table.get(k) != expected[k]
    ]


LANGS = pytest.mark.parametrize("lang", ["en", "tr"])
MATCH = ("p1", "p2", "p3")
PARAPHRASE = ("p1_paraphrase", "p2", "p3")
POOL = ["asset_tahini", "asset_cookies", "asset_decoy"]


# --- the oracle itself ------------------------------------------------------------------------


@LANGS
def test_oracle_rejects_a_recorded_answer_that_places_the_decoy(monkeypatch, lang):
    """Wrong-content control: same count of cards on the same passage, wrong picture."""
    result, _ = _ground(monkeypatch, lang, MATCH, POOL, answer="decoy_placed")
    found = violations(_by_passage(result), EXPECTED["match"])
    assert any("asset_decoy" in v for v in found), found


# --- model rows (replayed answers through the real agent) -------------------------------------


@LANGS
def test_match_places_each_picture_on_its_passage_and_the_decoy_stays_unused(monkeypatch, lang):
    result, client = _ground(monkeypatch, lang, MATCH, POOL, answer="match")
    assert violations(_by_passage(result), EXPECTED["match"]) == []
    assert [u["media_id"] for u in result.receipt["unplaced"]] == ["asset_decoy"]
    assert len(client.prompts) == 1


@LANGS
def test_shuffle_changes_neither_the_prompt_nor_the_cards(monkeypatch, lang):
    prompts, tables = set(), []
    for order in permutations(POOL):
        result, client = _ground(monkeypatch, lang, MATCH, list(order), answer="match")
        prompts.update(client.prompts)
        tables.append(_by_passage(result))
    assert len(prompts) == 1, "the model saw a different prompt for a different pool order"
    assert all(t == EXPECTED["match"] for t in tables)


@LANGS
def test_none_fits_places_nothing_and_every_passage_keeps_the_speaker(monkeypatch, lang):
    result, _ = _ground(
        monkeypatch, lang, MATCH, ["asset_decoy", "asset_lamp"], answer="none", request="x"
    )
    assert result.cards == []
    assert violations(_by_passage(result), EXPECTED["none"]) == []
    assert {u["media_id"]: u["reason"] for u in result.receipt["unplaced"]} == {
        "asset_decoy": "no_spoken_match",
        "asset_lamp": "no_spoken_match",
    }
    decisions = result.receipt["passage_placement"]["decisions"]
    assert [d["asset_id"] for d in decisions] == [None, None, None]
    assert {d["rule"] for d in decisions} == {"none"}


@LANGS
def test_add_pictures_with_no_fit_places_nothing_and_the_model_was_told_the_ask(monkeypatch, lang):
    """The creator's words reach the model, and asking for pictures does not make one fit."""
    result, client = _ground(
        monkeypatch,
        lang,
        MATCH,
        ["asset_decoy", "asset_lamp"],
        answer="none",
        request=REQUEST[lang],
    )
    assert result.cards == []
    assert REQUEST[lang] in client.prompts[0]


@LANGS
def test_paraphrase_gets_its_picture(monkeypatch, lang):
    result, _ = _ground(monkeypatch, lang, PARAPHRASE, POOL, answer="paraphrase")
    assert violations(_by_passage(result), EXPECTED["paraphrase"]) == []


# --- no-model rows (the heuristic fallback) ---------------------------------------------------


def _placed_ids(result) -> set[str]:  # noqa: ANN001
    return {c.media_id for c in result.cards}


@LANGS
def test_heuristic_places_the_real_picture_and_not_the_one_keyword_decoy(monkeypatch, lang):
    result, _ = _ground(monkeypatch, lang, MATCH, POOL, answer=None)
    assert result.receipt["matcher"] == "heuristic"
    assert _by_passage(result)["3.0"] == "asset_tahini"
    assert "asset_decoy" not in _placed_ids(result)


@LANGS
def test_heuristic_shuffle_is_stable(monkeypatch, lang):
    seen = set()
    for order in permutations(POOL):
        result, _ = _ground(monkeypatch, lang, MATCH, list(order), answer=None)
        seen.add(tuple(sorted((c.media_id, c.start_s) for c in result.cards)))
    assert len(seen) == 1


@pytest.mark.xfail(
    strict=True,
    reason="KRI-1556 A4: heuristic_match opens a card at the start of its best 8-word window, "
    "which can be the last words of the sentence before the one it is about; there it overlaps "
    "the earlier card and is dropped",
)
@LANGS
def test_heuristic_opens_each_picture_on_its_own_passage(monkeypatch, lang):
    result, _ = _ground(monkeypatch, lang, MATCH, POOL, answer=None)
    assert violations(_by_passage(result), EXPECTED["match"]) == []


@pytest.mark.xfail(
    strict=True,
    reason="KRI-1556 A4: heuristic_match places any asset sharing two tokens with a window",
)
def test_heuristic_does_not_place_a_two_keyword_decoy(monkeypatch):
    lang = "en"
    POOL_TEXT[lang]["asset_band"] = (
        "Brown Sugar album cover",
        "the Brown Sugar album cover of a rock band",
    )
    try:
        result, _ = _ground(monkeypatch, lang, MATCH, ["asset_band"], answer=None)
    finally:
        del POOL_TEXT[lang]["asset_band"]
    assert result.cards == []


# --- explicit slideshow and pace ---------------------------------------------------------------


def _candidates(*ids: str) -> list[PlacementCandidate]:
    return [PlacementCandidate.from_pool_row(_row("en", i)) for i in ids]


def _windows(result) -> list[tuple[float, float, str]]:  # noqa: ANN001
    return [(d.passage.start_s, d.passage.end_s, d.asset_id) for d in result.decisions]


def _slideshow(ids, request, duration_s=DURATION_S):  # noqa: ANN001, ANN202
    words = _words("en", *MATCH)
    return place_passages(
        passages_from_words(
            [{"word": w["text"], "start_s": w["start_s"], "end_s": w["end_s"]} for w in words]
        ),
        _candidates(*ids),
        request,
        job_id="j",
        duration_s=duration_s,
    )


def test_slideshow_follows_the_asked_order_and_pace_over_the_pool_order():
    result = _slideshow(
        ["asset_tahini", "asset_cookies", "asset_lamp"],
        PlacementRequest.slideshow(order=("asset_lamp", "asset_tahini", "asset_cookies"), pace_s=3),
    )
    assert _windows(result) == [
        (0.0, 3.0, "asset_lamp"),
        (3.0, 6.0, "asset_tahini"),
        (6.0, 9.0, "asset_cookies"),
    ]
    assert {d.rule for d in result.decisions} == {"slideshow"}


def test_slideshow_without_a_pace_splits_the_take_evenly_in_the_asked_order():
    result = _slideshow(
        ["asset_tahini", "asset_cookies"],
        PlacementRequest.slideshow(order=("asset_cookies", "asset_tahini")),
        duration_s=10.0,
    )
    assert _windows(result) == [(0.0, 5.0, "asset_cookies"), (5.0, 10.0, "asset_tahini")]


def test_pace_with_fewer_pictures_than_slots_neither_stretches_nor_repeats():
    result = _slideshow(
        ["asset_tahini", "asset_cookies"],
        PlacementRequest.slideshow(order=("asset_tahini", "asset_cookies"), pace_s=2),
        duration_s=10.0,
    )
    assert _windows(result) == [(0.0, 2.0, "asset_tahini"), (2.0, 4.0, "asset_cookies")]


def test_pace_repeats_only_when_the_creator_asked_for_a_loop():
    result = _slideshow(
        ["asset_tahini", "asset_cookies"],
        PlacementRequest.slideshow(order=("asset_tahini", "asset_cookies"), pace_s=2, loop=True),
        duration_s=7.0,
    )
    assert _windows(result) == [
        (0.0, 2.0, "asset_tahini"),
        (2.0, 4.0, "asset_cookies"),
        (4.0, 6.0, "asset_tahini"),
        (6.0, 7.0, "asset_cookies"),
    ]


def test_pictures_past_the_end_of_the_take_are_reported_not_squeezed_in():
    result = _slideshow(
        ["asset_tahini", "asset_cookies", "asset_lamp"],
        PlacementRequest.slideshow(order=("asset_tahini", "asset_cookies", "asset_lamp"), pace_s=4),
        duration_s=8.0,
    )
    assert _windows(result) == [(0.0, 4.0, "asset_tahini"), (4.0, 8.0, "asset_cookies")]
    assert result.unused == (("asset_lamp", "past_end_of_take"),)


def test_a_slideshow_exists_only_for_an_explicit_order_or_pace():
    with pytest.raises(ValueError):
        PlacementRequest(mode="slideshow")
    with pytest.raises(ValueError):
        PlacementRequest(mode="meaning", pace_s=3.0)
    with pytest.raises(ValueError):
        PlacementRequest.slideshow(order=("asset_tahini",), pace_s=0)


# --- after a speech cut ------------------------------------------------------------------------


def _card_on(words_start_s: float, end_s: float) -> PhoneSubtitledLanes:
    return PhoneSubtitledLanes(
        overlays=[
            SubtitledOverlayCard(
                id="pip-0",
                media_id="asset_tahini",
                gcs_path="users/u1/plan/item1/pool/asset_tahini.png",
                generation="7",
                start_s=words_start_s,
                end_s=end_s,
            )
        ]
    )


def test_a_cut_before_the_words_moves_the_picture_with_them():
    plan = _cut_plan([(4.0, 6.0)], DURATION_S)
    lanes, dropped = remap_lanes_for_cut(_card_on(12.8, 14.75), plan)
    assert dropped == frozenset()
    assert lanes.overlays[0].start_s == pytest.approx(12.8 - 2.0)


def test_placing_on_the_cut_transcript_keeps_the_picture_on_its_words(monkeypatch):
    """After a cut the words have new times; deciding on them lands the picture on the words."""
    cut_words = [
        {**w, "start_s": w["start_s"] - 9.0, "end_s": w["end_s"] - 9.0}
        for w in _words("en", *MATCH)
        if w["start_s"] >= 12.0
    ]
    passages = passages_from_words(
        [{"word": w["text"], "start_s": w["start_s"], "end_s": w["end_s"]} for w in cut_words]
    )
    client = _Replay(
        {
            "placements": [
                {
                    "asset_id": "asset_cookies",
                    "slot": "top",
                    "start_s": 3.8,
                    "end_s": 5.75,
                    "confidence_tier": "confident",
                    "reason": "You say the cookies bake.",
                    "transcript_anchor": "the cookies bake",
                    "sfx_intent": "none",
                }
            ],
            "wishlist": [],
        }
    )
    monkeypatch.setattr(model_client, "default_client", lambda: client)
    monkeypatch.setattr("app.agents._persistence.persist_agent_run", lambda **kw: None)
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key", raising=False)
    result = place_passages(
        passages,
        _candidates("asset_cookies", "asset_tahini"),
        PlacementRequest.meaning(""),
        job_id="j",
        duration_s=21.0,
    )
    placed = [d for d in result.decisions if d.asset_id]
    assert [(d.asset_id, d.passage.start_s) for d in placed] == [("asset_cookies", 3.0)]
    assert "cookies" in " ".join(w.text for w in placed[0].passage.words)


@pytest.mark.xfail(
    strict=True,
    reason="KRI-1556 A4: remap_lanes_for_cut shifts a window, it does not re-anchor or drop it "
    "when the words it sat on were cut",
)
def test_a_picture_whose_anchor_words_were_cut_is_dropped():
    plan = _cut_plan([(12.7, 13.7)], DURATION_S)
    lanes, dropped = remap_lanes_for_cut(_card_on(12.8, 14.75), plan)
    assert "pip-0" in dropped or not lanes.overlays


# --- the flag off is today --------------------------------------------------------------------


@LANGS
def test_flag_off_is_byte_identical_to_the_unflagged_call(monkeypatch, lang):
    pool_in_id_order = sorted(POOL)
    on_prompts = _ground(monkeypatch, lang, MATCH, pool_in_id_order, answer="match", flag=True)[
        1
    ].prompts
    off, off_client = _ground(
        monkeypatch, lang, MATCH, pool_in_id_order, answer="match", flag=False
    )
    assert "passage_placement" not in off.receipt
    assert off_client.prompts == on_prompts
    assert _by_passage(off) == EXPECTED["match"]


def test_flag_off_ignores_a_creator_request(monkeypatch):
    _, client = _ground(
        monkeypatch, "en", MATCH, POOL, answer="match", request="add pictures", flag=False
    )
    assert "add pictures" not in client.prompts[0]


def test_the_flag_is_dark_by_default():
    assert settings.passage_placement_enabled is False
    env_example = (Path(__file__).parents[5] / ".env.example").read_text(encoding="utf-8")
    assert "PASSAGE_PLACEMENT_ENABLED=false" in env_example
