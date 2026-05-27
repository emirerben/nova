"""Injector gate tests — Layer-1 (null cached) + Layer-2 (source allowlist).

Adds the defense-in-depth coverage for the Beauty And A Beat PR (2026-05-27):
the injector must refuse non-publishable extractions even when an admin's
`lyrics_config.enabled=true` ticks through, because a stale `whisper_only`
blob in `lyrics_cached` would otherwise burn hallucinated lyrics onto a
rendered video.

Layer-1 is the production-correctness path: after migration, only publishable
extractions ever land in `lyrics_cached`. Layer-2 is the drift guard for the
narrow window where the migration hasn't run yet OR a future agent emits a
new source without a paired injector update.
"""

from __future__ import annotations

from app.pipeline.lyric_injector import inject_lyric_overlays


def _recipe(target_duration_s: float = 10.0) -> dict:
    return {
        "slots": [
            {
                "position": 1,
                "target_duration_s": target_duration_s,
                "text_overlays": [],
            }
        ]
    }


def _line(text: str, start: float, end: float) -> dict:
    return {
        "text": text,
        "start_s": start,
        "end_s": end,
        "words": [{"text": text, "start_s": start, "end_s": end}],
    }


# ── Layer 1: null lyrics_cached ──────────────────────────────────────────────


def test_layer1_null_cached_returns_recipe_unchanged() -> None:
    """Primary gate: no cached blob → nothing to inject, regardless of config.
    After 2026-05-27 migration, lyrics_cached is null for non-publishable
    rows — this is the silent quiet path, not a warning."""
    recipe = _recipe()
    out = inject_lyric_overlays(
        recipe,
        None,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    assert out["slots"][0]["text_overlays"] == []


def test_layer1_null_cached_even_with_enabled_true() -> None:
    """Defense in depth: even when admin had lyrics_config.enabled=true on
    a track whose status flipped to needs_manual_lyrics post-migration,
    the absence of lyrics_cached is what saves us. This pins the Layer-1
    independence from the enabled flag."""
    recipe = _recipe()
    out = inject_lyric_overlays(
        recipe,
        None,  # null cached
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    for slot in out["slots"]:
        assert slot["text_overlays"] == []


# ── Layer 2: source allowlist ────────────────────────────────────────────────


def test_layer2_rejects_whisper_only_source() -> None:
    """A `whisper_only` source must NEVER inject. This is the explicit
    Beauty And A Beat policy: no production renders against transcription-only
    output. Even if a stale or drifted row somehow has lyrics_cached
    populated with whisper_only, the injector refuses."""
    recipe = _recipe()
    cached = {
        "source": "whisper_only",
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    for slot in out["slots"]:
        assert slot["text_overlays"] == [], "whisper_only source must not reach output overlays"


def test_layer2_rejects_empty_source() -> None:
    """Defensive: a malformed cached blob with no `source` field (legacy
    pre-source-tracking? data corruption?) must refuse to inject. Trusting
    an empty source as 'probably fine' is exactly the kind of silent
    permissive default that ships bugs to prod."""
    recipe = _recipe()
    cached = {"lines": [_line("Hello", 0.5, 1.5)]}  # no source key
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    for slot in out["slots"]:
        assert slot["text_overlays"] == []


def test_layer2_rejects_unknown_source() -> None:
    """Future-source defense: a brand-new source value emitted by an agent
    update that hasn't been paired with an injector allowlist update must
    refuse rather than guess. Forces a conscious decision when adding a
    new source."""
    recipe = _recipe()
    cached = {
        "source": "future_provider+whisper",  # not in allowlist
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    for slot in out["slots"]:
        assert slot["text_overlays"] == []


def test_layer2_accepts_lrclib_synced() -> None:
    """Baseline: the canonical publishable source must still inject normally."""
    recipe = _recipe()
    cached = {
        "source": "lrclib_synced+whisper",
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    # At least the karaoke overlay must be present.
    assert any(slot["text_overlays"] for slot in out["slots"])


def test_layer2_accepts_lrclib_plain() -> None:
    """The other LRCLIB-derived source — used when LRCLIB row has no synced
    lyrics — must also pass through."""
    recipe = _recipe()
    cached = {
        "source": "lrclib_plain+whisper",
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    assert any(slot["text_overlays"] for slot in out["slots"])


def test_layer2_accepts_legacy_genius_whisper_source() -> None:
    """Backwards-compat: pre-LRCLIB rows with `source='genius+whisper'`
    survived the 2026-05-27 migration (the migration only flips
    `whisper_only` rows). Those legacy rows must continue to inject —
    they were quality-checked by the earlier Whisper alignment process
    and re-extracting them is a separate workflow."""
    recipe = _recipe()
    cached = {
        "source": "genius+whisper",
        "genius_url": "https://genius.com/old-song",
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    assert any(slot["text_overlays"] for slot in out["slots"])


def test_layer2_accepts_manual_source() -> None:
    """Future admin manual-paste source — currently unused, allowlisted so
    when it lands no injector edit is needed."""
    recipe = _recipe()
    cached = {
        "source": "manual",
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": True, "style": "karaoke"},
    )
    assert any(slot["text_overlays"] for slot in out["slots"])


# ── Interaction with enabled flag ────────────────────────────────────────────


def test_enabled_false_short_circuits_before_source_check() -> None:
    """When lyrics_config.enabled is false, the injector returns
    immediately — source gate doesn't even run. This is the documented
    behavior of the `enabled` toggle (per-track admin gate)."""
    recipe = _recipe()
    cached = {
        "source": "whisper_only",  # would normally be rejected
        "lines": [_line("Hello", 0.5, 1.5)],
    }
    out = inject_lyric_overlays(
        recipe,
        cached,
        best_start_s=0.0,
        best_end_s=10.0,
        lyrics_config={"enabled": False, "style": "karaoke"},
    )
    for slot in out["slots"]:
        assert slot["text_overlays"] == []
