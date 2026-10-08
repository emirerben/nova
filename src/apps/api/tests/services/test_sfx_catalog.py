"""Deterministic SFX library selection for the AI placers (KRI-173)."""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.sfx_catalog import (
    SFX_CATEGORIES,
    SFX_CATEGORY_TERMS,
    SfxEntry,
    content_words,
    match_score,
    placement_catalog,
    planner_catalog,
    rank_for_request,
    resolve_described_effect,
    words,
)


def _row(
    effect_id: str,
    name: str,
    category: str | None = None,
    terms: tuple[str, ...] = (),
    **extra: object,
) -> dict:
    return {
        "id": effect_id,
        "name": name,
        "category": category,
        "search_terms": list(terms),
        "role_tags": extra.get("role_tags", []),
        "contains_voice": extra.get("contains_voice", False),
        "quality_tier": extra.get("quality_tier", "library"),
        "duration_s": extra.get("duration_s", 0.5),
        "catalog_rank": extra.get("catalog_rank"),
    }


LIBRARY = [
    _row("buzz", "Wrong buzzer", "rejection", ("buzzer", "wrong answer", "quiz")),
    _row("buzz-long", "Wrong buzzer long", "rejection", ("buzzer", "wrong answer")),
    _row("buzz-2", "Wrong buzzer double", "rejection", ("buzzer",)),
    _row("ding", "Correct ding", "approval", ("ding", "bell", "correct answer")),
    _row("cheer", "Crowd cheer", "approval", ("cheer", "crowd"), contains_voice=True),
    _row("roll", "Drum roll", "suspense", ("drum roll",), duration_s=4.3),
    _row("whoosh", "Whoosh fast", "transition", ("whoosh",)),
    _row("rewind", "Tape rewind", "transition", ("rewind", "cassette")),
    _row("boom", "Bass boom", "impact", ("vine boom", "boom")),
    _row("boing", "Boing", "comedy", ("boing", "bounce")),
    _row("pop", "Soft pop", "ui", ("pop",)),
    _row("whistle", "Referee whistle", "sports", ("whistle", "referee")),
    _row("cash", "Cash register", "money", ("ka ching",)),
    _row(
        "smart-pop",
        "Smart soft pop",
        None,
        (),
        role_tags=["visual_enter_soft"],
        quality_tier="core",
    ),
    _row("fah", "Fah", None, ()),
]


def _entries() -> list[SfxEntry]:
    return [SfxEntry.from_row(row) for row in LIBRARY]


def test_description_resolves_to_the_base_variant() -> None:
    assert resolve_described_effect(_entries(), "a buzzer").id == "buzz"
    assert resolve_described_effect(_entries(), "the wrong answer buzzer").id == "buzz"
    assert resolve_described_effect(_entries(), "vine boom").id == "boom"


def test_description_never_guesses_when_a_word_is_uncovered() -> None:
    assert resolve_described_effect(_entries(), "a buzzer trumpet") is None
    assert resolve_described_effect(_entries(), "the impossible") is None
    assert resolve_described_effect(_entries(), "a sound effect") is None


def test_words_match_whole_words_only() -> None:
    tape = SfxEntry.from_row(_row("rewind", "Tape rewind", "transition", ("rewind",)))
    assert match_score("tap", tape) == 0.0
    assert match_score("tape", tape) > 0.0


def test_ranking_is_deterministic_and_prefers_name_hits() -> None:
    ranked = [e.id for e in rank_for_request(_entries(), "buzzer for the wrong answer")]
    assert ranked[:3] == ["buzz", "buzz-long", "buzz-2"]
    shuffled = _entries()
    random.Random(7).shuffle(shuffled)
    assert [e.id for e in rank_for_request(shuffled, "buzzer for the wrong answer")] == ranked


def test_planner_catalog_is_request_independent_diverse_and_order_stable() -> None:
    first = [e.id for e in planner_catalog(LIBRARY, 10)]
    shuffled = list(LIBRARY)
    random.Random(3).shuffle(shuffled)
    assert [e.id for e in planner_catalog(shuffled, 10)] == first
    by_id = {row["id"]: row for row in LIBRARY}
    categories = {by_id[i]["category"] or "other" for i in first}
    assert categories == {*SFX_CATEGORIES, "other"}
    assert first[0] == "buzz"  # the base variant leads its category
    assert planner_catalog(LIBRARY, 0) == []


def test_placement_catalog_is_voice_free_one_shots_role_tagged_first() -> None:
    picked = placement_catalog(LIBRARY, 30)
    ids = [e.id for e in picked]
    assert ids[0] == "smart-pop"
    assert "cheer" not in ids  # voice
    assert "roll" not in ids  # a 4.3 s bed, not an accent
    assert len(placement_catalog(LIBRARY, 3)) == 3


def test_curated_upload_order_and_base_variants_lead_each_category() -> None:
    start = datetime(2026, 9, 23, tzinfo=UTC)
    rows = [
        _row("buzz", "Wrong buzzer", "rejection", ("buzzer",)),
        _row("buzz-long", "Wrong buzzer long", "rejection", ("buzzer",)),
        _row("aww", "Crowd aww", "rejection", ("aww",), contains_voice=True),
        _row("ding", "Correct ding", "approval", ("ding",)),
        _row("double", "Double ding", "approval", ("ding", "ding ding")),
    ]
    for offset, row in enumerate(rows):
        row["created_at"] = start + timedelta(seconds=offset)
    # Upload order carries curation: shorter "Crowd aww" / "Double ding" lose.
    assert [e.id for e in planner_catalog(rows, 4)] == ["buzz", "ding", "aww", "double"]
    entries = [SfxEntry.from_row(r) for r in rows]
    assert resolve_described_effect(entries, "a ding").id == "ding"
    assert resolve_described_effect(entries, "ding ding").id == "double"


def test_primary_keyword_wins_ties_and_library_tier_beats_quiet_core() -> None:
    rows = [
        _row("check", "Checkmark pop", "approval", ("check", "pop")),
        _row("soft", "Soft pop", "ui", ("pop", "appear")),
        _row("smart", "Smart soft pop", role_tags=["visual_enter_soft"], quality_tier="core"),
    ]
    assert resolve_described_effect([SfxEntry.from_row(r) for r in rows], "a pop").id == "soft"


def _seeded(effect_id: str, name: str, category: str, terms: tuple[str, ...], **extra) -> dict:
    # Search terms as the seed script writes them: explicit, then category-wide.
    return _row(
        effect_id, name, category, (*terms, *SFX_CATEGORY_TERMS[category], category), **extra
    )


SEEDED = [
    _seeded("buzz", "Wrong buzzer", "rejection", ("buzzer", "wrong answer")),
    _seeded("fail", "Fail horn", "rejection", ("horn", "bwamp")),
    _seeded("ding", "Correct ding", "approval", ("ding", "bell", "correct answer")),
    _seeded("pop", "Soft pop", "ui", ("pop", "appear")),
]


def test_category_wide_words_and_refusals_name_no_effect() -> None:
    entries = [SfxEntry.from_row(r) for r in SEEDED]
    for vague in ("no", "the right", "a good", "bad", "text", "not a buzzer", "without a ding"):
        assert resolve_described_effect(entries, vague) is None, vague
    assert resolve_described_effect(entries, "a buzzer").id == "buzz"
    assert resolve_described_effect(entries, "a fail").id == "fail"  # a name word
    assert resolve_described_effect(entries, "the wrong answer").id == "buzz"


def test_catalog_rank_beats_upload_order_after_a_re_upload() -> None:
    rows = [
        _row("buzz", "Wrong buzzer", "rejection", ("buzzer",), catalog_rank=0),
        _row("aww", "Crowd aww", "rejection", ("aww",), catalog_rank=13),
    ]
    rows[0]["created_at"] = datetime(2026, 10, 1, tzinfo=UTC)  # re-uploaded later
    rows[1]["created_at"] = datetime(2026, 9, 23, tzinfo=UTC)
    rows[0]["catalog_rank"], rows[1]["catalog_rank"] = 0, 13
    assert [e.id for e in planner_catalog(rows, 2)] == ["buzz", "aww"]
    assert [e.id for e in placement_catalog(rows, 2)] == ["buzz", "aww"]


# --- KRI-540: Turkish sound descriptions against the English-only library -----

TR_LIBRARY = [
    _seeded("clink", "Glass clink", "impact", ("glass", "clink", "cheers", "toast", "cup")),
    _seeded("glass-break", "Glass break", "rejection", ("glass", "shatter", "smash", "break")),
    _seeded("bell", "Bell hit", "impact", ("bell", "gong", "ring", "round")),
    _seeded("ding", "Correct ding", "approval", ("ding", "bell", "quiz", "correct answer")),
    _seeded("applause", "Applause", "approval", ("applause", "clap", "clapping", "bravo", "crowd")),
    _seeded(
        "applause-long",
        "Applause long",
        "approval",
        ("applause", "clap", "clapping", "ovation", "crowd", "long"),
    ),
    _seeded("ref", "Referee whistle", "sports", ("whistle", "referee", "foul", "kick off")),
    _seeded("slide", "Slide whistle up", "comedy", ("slide whistle", "whistle", "rise")),
    _seeded("door", "Door slam", "impact", ("door", "slam", "shut", "leave", "angry")),
    _seeded("coins", "Coins", "money", ("coins", "change", "pocket", "jingle")),
    _seeded("cash", "Cash register", "money", ("ka ching", "register", "sale", "paid")),
    _seeded("buzz", "Wrong buzzer", "rejection", ("buzzer", "quiz", "wrong answer", "nope")),
]


def _tr_entries() -> list[SfxEntry]:
    return [SfxEntry.from_row(row) for row in TR_LIBRARY]


def _resolved_id(description: str) -> str | None:
    found = resolve_described_effect(_tr_entries(), description)
    return found.id if found else None


def test_turkish_descriptions_resolve_to_the_english_effect() -> None:
    assert _resolved_id("küçük bir fincan sesi") == "clink"  # the prod miss
    assert _resolved_id("fincan sesi") == "clink"
    assert _resolved_id("zil sesi") == "bell"
    assert _resolved_id("alkış") == "applause"
    assert _resolved_id("alkışlar") == "applause"  # plural suffix
    assert _resolved_id("düdük") == "ref"
    assert _resolved_id("hakem düdüğü") == "ref"  # soft-g possessive
    assert _resolved_id("kapı çarpması") == "door"
    assert _resolved_id("bozuk para sesi") == "coins"
    assert _resolved_id("kasa sesi") == "cash"
    assert _resolved_id("yanlış") == "buzz"
    assert _resolved_id("cam kırılması") == "glass-break"
    # ASCII-typed Turkish works the same (phone keyboards drop diacritics).
    assert _resolved_id("kucuk bir fincan sesi") == "clink"
    assert _resolved_id("DÜDÜK") == "ref"
    assert _resolved_id("İstanbul") is None


def test_turkish_filler_and_refusals_name_no_effect() -> None:
    assert _resolved_id("bir ses ekle") is None
    assert _resolved_id("küçük bir ses") is None
    assert _resolved_id("ses olmasın") is None
    assert _resolved_id("zil sesi istemiyorum") is None
    assert _resolved_id("sesi kaldır") is None
    # An uncovered Turkish word still means "don't guess".
    assert _resolved_id("küçük bir fincan çıngırak sesi") is None


def test_english_behaviour_is_unchanged_by_the_turkish_vocabulary() -> None:
    entries = _tr_entries()
    assert resolve_described_effect(entries, "cup sound").id == "clink"
    assert resolve_described_effect(entries, "buzzer").id == "buzz"
    assert resolve_described_effect(entries, "a small cup sound") is None
    # Words that are also Turkish ("can", "her", "cam") stay plain English.
    assert content_words("the can sound") == ["can"]
    assert content_words("her laugh") == ["her", "laugh"]
    assert resolve_described_effect(entries, "no sound") is None
    assert resolve_described_effect(entries, "yok") is None  # Turkish "none"


def test_words_tokenisation_is_ascii_identical_and_folds_turkish() -> None:
    assert words("Tape rewind") == ["tape", "rewind"]
    for text in ("Wrong buzzer long", "ka-ching! 8 bit", "A  mix_of 3 THINGS", ""):
        assert words(text) == re.findall(r"[a-z0-9]+", text.casefold())
    assert words("küçük bir fincan sesi") == ["kucuk", "bir", "fincan", "sesi"]
    assert words("İstanbul ışık ğ Şöför") == ["istanbul", "isik", "g", "sofor"]
    assert words(None) == []


def test_turkish_vocabulary_targets_words_the_real_library_uses() -> None:
    from app.services.sfx_catalog import _TR_VOCAB

    english = {
        "cup", "glass", "break", "bell", "applause", "whistle", "referee", "door", "slam",
        "click", "crowd", "explosion", "drum", "money", "cash", "register", "laugh", "horn",
        "camera", "photo", "keyboard", "win", "wrong", "correct", "answer", "heart", "clock",
        "page", "hit", "punch", "notification", "message", "goal",
    }  # fmt: skip
    assert {w for targets in _TR_VOCAB.values() for w in targets} <= english


PROD_DUMP = Path(
    "/private/tmp/claude-501/-Users-yasinberkyesilyurt-projects-nova/"
    "600a1d9e-15cc-4dfe-947a-28cb3e01b121/scratchpad/sfx_library.json"
)


@pytest.mark.skipif(not PROD_DUMP.exists(), reason="prod SFX library dump not available")
def test_turkish_descriptions_against_the_prod_library_dump() -> None:
    rows = [{**r, "created_at": None} for r in json.loads(PROD_DUMP.read_text())["effects"]]
    entries = [SfxEntry.from_row(r) for r in rows]

    def name(description: str) -> str | None:
        found = resolve_described_effect(entries, description)
        return found.name if found else None

    assert name("küçük bir fincan sesi") == "Glass clink"
    assert name("zil sesi") == "Bell hit"
    assert name("alkış") == "Applause"
    assert name("hakem düdüğü") == "Referee whistle"
    assert name("kapı çarpması") == "Door slam"
    assert name("ses olmasın") is None
