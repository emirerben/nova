"""The creator sound-effect library build (KRI-173).

Catalog invariants run offline. Synthesized effects are rendered and mastered
in memory; one encode round trip proves the delivered .m4a meets the iPhone
contract. CC0 recordings are not downloaded here (the seed script verifies
their pinned SHA-256 when it builds).
"""

from __future__ import annotations

import json
import re
import shutil

import numpy as np
import pytest

from app.services.sfx_catalog import SFX_CATEGORIES
from app.smart_edit.compiler import _clean_sfx_rows
from scripts.sfx_library import catalog, master, sources, synth

LIB = catalog.LIBRARY


def test_library_is_large_unique_and_categorised() -> None:
    assert len(LIB) >= 80
    slugs = [e.slug for e in LIB]
    names = [e.name.casefold() for e in LIB]
    assert len(set(slugs)) == len(slugs)
    assert len(set(names)) == len(names), "the creator agent resolves effects by name"
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug) for slug in slugs)
    counts = {c: sum(1 for e in LIB if e.category == c) for c in SFX_CATEGORIES}
    assert all(count >= 5 for count in counts.values()), counts


def test_issue_headline_sounds_exist() -> None:
    names = {e.name for e in LIB}
    for required in (
        "Wrong buzzer",
        "Correct ding",
        "Drum roll + crash",
        "Sad trombone",
        "Applause",
        "Crowd cheer",
        "Referee whistle",
        "Stadium roar",
        "Cash register",
        "Bass boom",
        "Record scratch",
    ):
        assert required in names


def test_every_source_is_pinned_and_every_recipe_is_used() -> None:
    used = set()
    for effect in LIB:
        for layer in effect.layers:
            source = layer.source
            if isinstance(source, catalog.Synth):
                assert source.recipe in synth.RECIPES
                used.add(source.recipe)
            elif isinstance(source, catalog.Kenney):
                assert source.pack in sources.KENNEY_PACKS
            else:
                assert source.sound_id in sources.FREESOUND
    assert used == set(synth.RECIPES), "dead or missing synth recipes"


def test_voice_metadata_matches_what_the_placers_filter() -> None:
    for effect in LIB:
        if re.search(r"\b(crowd|laugh|cheer|chant|stadium)\b", effect.name.casefold()):
            assert effect.contains_voice, effect.slug
    clean = _clean_sfx_rows([{"name": e.name, "contains_voice": e.contains_voice} for e in LIB])
    assert {row["name"] for row in clean} == {e.name for e in LIB if not e.contains_voice}


def test_search_terms_carry_the_category_and_stay_bounded() -> None:
    for effect in LIB:
        terms = effect.search_terms
        assert effect.category in terms
        assert len(terms) <= 32
        assert all(term == term.casefold().strip() for term in terms)


def test_master_trims_fades_levels_and_caps() -> None:
    t = np.arange(int(0.4 * synth.SR)) / synth.SR
    tone = np.sin(2 * np.pi * 440 * t) * np.exp(-t / 0.1)
    padded = np.concatenate([np.zeros(int(0.12 * synth.SR)), tone, np.zeros(int(0.5 * synth.SR))])
    out, report = master.master(padded, "one_shot")
    onset = np.flatnonzero(np.max(np.abs(out), axis=1) > 10 ** (-50 / 20))[0]
    assert onset / synth.SR * 1000 < 1.0
    assert np.all(out[-1] == 0.0)
    assert report.duration_s < 0.5
    assert abs(report.loudness_lufs - master.TARGET_LUFS) < 0.2 or report.peak_limited
    assert report.true_peak_dbtp <= master.TRUE_PEAK_CEILING_DBTP + 0.01

    long_bed = np.sin(2 * np.pi * 220 * np.arange(12 * synth.SR) / synth.SR)
    capped, bed = master.master(long_bed, "bed")
    assert bed.capped and len(capped) == 10 * synth.SR


@pytest.mark.parametrize("recipe", sorted(synth.RECIPES))
def test_every_synth_recipe_masters_within_spec(recipe: str) -> None:
    effect = next(
        e for e in LIB if any(getattr(layer.source, "recipe", None) == recipe for layer in e.layers)
    )
    out, report = master.master(catalog.render_raw(effect), effect.kind)
    assert np.all(np.isfinite(out))
    assert report.duration_s <= master.MAX_DURATION_S[effect.kind] + 1e-3
    assert report.true_peak_dbtp <= master.TRUE_PEAK_CEILING_DBTP + 0.01
    assert report.loudness_lufs <= master.TARGET_LUFS + 0.2
    assert report.peak_limited or abs(report.loudness_lufs - master.TARGET_LUFS) < 0.3


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("slug", ["wrong-buzzer", "correct-ding"])
def test_delivered_m4a_meets_the_iphone_contract(slug: str, tmp_path) -> None:
    effect = catalog.by_slug()[slug]
    mastered, _ = master.master(catalog.render_raw(effect), effect.kind)
    qa = master.deliver(mastered, effect.kind, tmp_path / effect.filename)
    assert qa["problems"] == []
    assert qa["codec"] == "aac" and qa["sample_rate"] == 48_000
    assert qa["leading_silence_ms"] < 1.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_qa_flags_an_unfaded_end_despite_aac_end_padding(tmp_path) -> None:
    # 0.3 s of tone that stops dead: AAC appends end padding the player never
    # plays, so QA must judge the playable end, not the padding.
    t = np.arange(int(0.3 * synth.SR)) / synth.SR
    tone = synth.stereo(0.5 * np.sin(2 * np.pi * 440 * t))
    path = tmp_path / "cut.m4a"
    master.encode_m4a(tone, path)
    assert any("tail not faded" in p for p in master.qa_encoded(path, "one_shot")["problems"])


def test_layers_cut_from_a_longer_source_are_ramped() -> None:
    whole = synth.render("whoosh_slow")
    middle = catalog.Effect(
        "cut", "Cut", "transition", (catalog.Layer(catalog.Synth("whoosh_slow"), 0.0, 0.3, 0.4),)
    )
    out = catalog.render_raw(middle)
    assert np.max(np.abs(out[:2])) < 0.01 * np.max(np.abs(whole))
    assert np.max(np.abs(out[-2:])) < 0.01 * np.max(np.abs(whole))


def _manifest_entry(slug: str, sha: str = "a" * 64, problems: list | None = None) -> dict:
    return {
        "slug": slug,
        "name": slug.replace("-", " ").capitalize(),
        "file": f"{slug}.m4a",
        "sha256": sha,
        "rank": 0,
        "qa": {"problems": problems or []},
    }


def _admin_row(row_id: str, slug: str, *, sha: str, tagged: bool, **extra) -> dict:
    return {
        "id": row_id,
        "name": extra.get("name", slug.replace("-", " ").capitalize()),
        "source_filename": f"{slug}.m4a",
        "sha256": sha,
        "provenance": f"{catalog.LIBRARY_VERSION}:{slug} synth" if tagged else None,
        "status": extra.get("status", "ready"),
        "archived_at": None,
        "created_at": extra.get("created_at", "2026-09-23T00:00:00Z"),
    }


def test_upload_plan_only_touches_rows_the_library_owns() -> None:
    from scripts import seed_sfx_library as seed

    entries = [_manifest_entry("applause"), _manifest_entry("tap"), _manifest_entry("coins")]
    existing = [
        _admin_row("lib", "applause", sha="a" * 64, tagged=True),
        # An admin page upload that happens to share the file name: never ours.
        _admin_row("admin", "applause", sha="b" * 64, tagged=False, created_at="2026-10-01"),
        # Interrupted run: confirmed, never PATCHed, byte-identical audio.
        _admin_row("resume", "tap", sha="a" * 64, tagged=False),
        # A pending row whose PUT never landed has no audio.
        _admin_row("stuck", "coins", sha="a" * 64, tagged=True, status="pending"),
    ]
    plan, notes = seed._plan(entries, existing, replace_audio=True)
    actions = {entry["slug"]: (action, row and row["id"]) for action, entry, row in plan}
    assert actions == {
        "applause": ("update", "lib"),
        "tap": ("update", "resume"),
        "coins": ("create", None),
    }
    assert any("admin" in note and "not ours" in note for note in notes)


def test_upload_refuses_a_manifest_with_qa_failures(tmp_path, monkeypatch) -> None:
    from scripts import seed_sfx_library as seed

    (tmp_path / "manifest.json").write_text(
        json.dumps([_manifest_entry("tap", problems=["tail not faded"])])
    )
    monkeypatch.setattr(seed, "_client", lambda **_: pytest.fail("contacted the API"))
    with pytest.raises(SystemExit, match="QA failures"):
        seed.upload(tmp_path, prod=True, dry_run=False, yes=True, replace_audio=False)
