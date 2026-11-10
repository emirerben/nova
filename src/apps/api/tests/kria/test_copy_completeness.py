"""Every catalog language covers every chat sentence, with the same template fields (KRI-1362).

``app/kria/copy/tr.json`` is generated from the code and lists every sentence ``say()`` can look
up, keyed by its English text. A catalog language (``de.json`` first, written by a translator)
must cover all of them. Ways it can go wrong:

1. A key missing from the language file: that sentence silently shows in English.
2. A translation that drops, renames or adds a ``{field}`` (or changes its format spec): the
   creator sees a raw ``{names}``, a missing name, or ``say()`` raises ``KeyError`` mid-turn.
3. An empty translation: the catalog skips it (``copy_catalog.catalog``), so it falls back to
   English while looking translated.
4. ``say()`` with German bound not reading the file, reading it by the filled sentence instead
   of the template, or not falling back to English for a key the file lacks.
"""

from __future__ import annotations

import json
import string
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import settings
from app.kria import copy_catalog
from app.kria import reply_language as reply_language_module
from app.kria.reply_language import reply_language_for, say

_COPY = Path(copy_catalog.__file__).parent / "copy"
_TR = _COPY / "tr.json"
_NOT_LANGUAGES = {"tr", "conflicts"}


def _messages(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def _languages() -> list[object]:
    found = sorted(p.stem for p in _COPY.glob("*.json") if p.stem not in _NOT_LANGUAGES)
    if not found:
        return [
            pytest.param(
                None,
                marks=pytest.mark.skip(
                    reason="no catalog language file yet (app/kria/copy/de.json is written by "
                    "the translator); nothing to check"
                ),
                id="no-catalog-language",
            )
        ]
    return [pytest.param(language, id=language) for language in found]


def _fields(text: str) -> list[tuple[str, str | None, str | None]]:
    return sorted(
        (name, conv or "", spec or "")
        for _lit, name, spec, conv in string.Formatter().parse(text)
        if name is not None
    )


@pytest.mark.parametrize("language", _languages())
def test_catalog_language_has_every_key(language: str) -> None:
    keys = _messages(_TR)
    have = _messages(_COPY / f"{language}.json")
    missing = sorted(set(keys) - set(have))
    assert not missing, f"{language}.json misses {len(missing)} key(s):\n" + "\n".join(missing)


@pytest.mark.parametrize("language", _languages())
def test_translations_keep_the_template_fields(language: str) -> None:
    problems: list[str] = []
    for english, text in _messages(_COPY / f"{language}.json").items():
        if not isinstance(text, str) or not text.strip():
            problems.append(f"empty translation: {english!r}")
            continue
        try:
            want, got = _fields(english), _fields(text)
        except ValueError as exc:  # an unescaped brace
            problems.append(f"bad braces ({exc}): {english!r} -> {text!r}")
            continue
        if want != got:
            problems.append(f"fields {got} != {want}: {english!r} -> {text!r}")
    assert not problems, f"{language}.json:\n" + "\n".join(problems)


# ---------------------------------------------------------------- say() reads the file


@pytest.fixture
def german_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """A real ``de.json`` on disk with a sample of keys (not monkeypatched lookups)."""
    keys = _messages(_TR)
    templated = next(k for k in keys if _fields(k))
    plain = [k for k in keys if not _fields(k)][:3]
    sample = {key: f"[de] {key}" for key in plain}
    sample[templated] = "[de] " + templated
    (tmp_path / "de.json").write_text(
        json.dumps({"messages": sample}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(copy_catalog, "CATALOG_DIR", tmp_path)
    copy_catalog.catalog.cache_clear()
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    yield sample
    copy_catalog.catalog.cache_clear()


def test_german_say_reads_de_json_and_falls_back_to_english(
    german_catalog: dict[str, str],
) -> None:
    keys = _messages(_TR)
    absent = next(k for k in keys if k not in german_catalog and not _fields(k))
    with reply_language_for("de"):
        for key, text in german_catalog.items():
            fields = {name: f"<{name}>" for name, _conv, _spec in _fields(key)}
            if not fields:
                assert say(en=key, tr=keys[key]) == text
                continue
            filled = say(en=key, tr=keys[key], **fields)
            assert filled == text.format(**fields)  # looked up by the template, then filled
            assert filled.startswith("[de] ")
        assert say(en=absent, tr=keys[absent]) == absent
    with reply_language_for("tr"):
        assert say(en=absent, tr=keys[absent]) == keys[absent]


@pytest.mark.parametrize("language", _languages())
def test_bound_language_reads_its_real_file(language: str) -> None:
    """With the language bound, ``say()`` returns the shipped file's text for a sample of keys."""
    keys = _messages(_TR)
    have = _messages(_COPY / f"{language}.json")
    sample = [k for k in sorted(have) if k in keys and not _fields(k)][:20]
    assert sample, f"{language}.json shares no plain sentence with tr.json"
    token = reply_language_module._CURRENT.set(language)  # any catalog language, switch or not
    try:
        for key in sample:
            assert say(en=key, tr=keys[key]) == have[key]
    finally:
        reply_language_module._CURRENT.reset(token)
