"""Guard for content-derived prompt versions (Main Creator + brief extractor).

Both versions are fingerprints of the files their prompt text comes from, so two
PRs never edit the same hand-numbered line. On the 2026-11-05 PR batch, 31 of 64
conflicting merges were only that line, its history comment and the tests that
pinned it (docs/reviews/pr-merge-speed-2026-11-05).

Ways this can fail, and the test that catches each:
  F1 a prompt file or skill the agent loads is missing from its source list, so
     the prompt changes and the version does not  -> test_every_loaded_*_is_a_source
  F2 a listed source is not shipped in the deploy image, so the version differs
     between CI and prod, or import fails           -> test_sources_exist_and_ship
  F3 the fingerprint depends on list order or line endings, so two machines
     disagree on the same code                      -> test_fingerprint_is_stable
  F4 a change to any source does not change the version
                                                    -> test_any_byte_change_moves_the_version
  F5 the agent spec stamps a different string than the module constant, so
     traces and stored runs disagree                -> test_specs_stamp_the_fingerprint
  F6 the two agents' versions can be mistaken for each other in traces
                                                    -> test_specs_stamp_the_fingerprint
  F7 a ``{{TOKEN}}`` vocabulary a listed source renders comes from a module that
     is not a source (Emir's #2133 review: TEXT_ANIMATION_EDGES is built from
     _schemas/text_animation_phases.py), so editing that vocabulary changes the
     prompt and not the version          -> test_every_rendered_vocabulary_is_a_source,
                                            test_prompt_vocab_sources_match_its_imports,
                                            test_changing_animation_vocabulary_moves_both_versions
Known limit, not guarded: prompt text built at render time from capability manifests
and other runtime inputs is not a file, so no fingerprint can cover it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from app.agents import _prompt_fingerprint as fp
from app.agents import prompt_vocab
from app.agents.brief_extractor import (
    BRIEF_EXTRACTOR_PROMPT_SOURCES,
    BRIEF_EXTRACTOR_PROMPT_VERSION,
    BriefExtractorAgent,
)
from app.agents.main_creator import (
    MAIN_CREATOR_PROMPT_SOURCES,
    MAIN_CREATOR_PROMPT_VERSION,
    MainCreatorAgent,
)

API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[2]
AGENTS = {
    "app/agents/main_creator.py": MAIN_CREATOR_PROMPT_SOURCES,
    "app/agents/brief_extractor.py": BRIEF_EXTRACTOR_PROMPT_SOURCES,
}


@pytest.mark.parametrize(("module", "sources"), AGENTS.items())
def test_every_loaded_prompt_file_is_a_source(module: str, sources: tuple[str, ...]) -> None:
    text = (API_ROOT / module).read_text(encoding="utf-8")
    loaded = set(re.findall(r'load_prompt\(\s*"([^"]+)"', text))
    assert loaded, f"{module} loads no prompt file; update this guard"
    assert module in sources
    for name in loaded:
        assert f"prompts/{name}.txt" in sources, f"{module} loads prompts/{name}.txt"


@pytest.mark.parametrize(("module", "sources"), AGENTS.items())
def test_every_loaded_skill_is_a_source(module: str, sources: tuple[str, ...]) -> None:
    text = (API_ROOT / module).read_text(encoding="utf-8")
    for name in re.findall(r'load_skill\(\s*"([^"]+)"', text):
        assert f"app/agents/skills/{name}/SKILL.md" in sources, f"{module} loads skill {name}"


def test_brief_extractor_follows_the_shared_brief_section() -> None:
    # The brief section the extractor renders is defined in main_creator.py.
    assert "app/agents/main_creator.py" in BRIEF_EXTRACTOR_PROMPT_SOURCES


@pytest.mark.parametrize("sources", AGENTS.values())
def test_sources_exist_and_ship(sources: tuple[str, ...]) -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for rel in sources:
        assert (API_ROOT / rel).is_file(), rel
        top = rel.split("/", 1)[0]
        assert f"COPY src/apps/api/{top} ./{top}" in dockerfile, f"{rel} is not in the prod image"


def test_fingerprint_is_stable(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    (tmp_path / "b.txt").write_bytes(b"three\r\nfour\r\n")
    first = fp.prompt_fingerprint("xx", ("a.txt", "b.txt"), root=tmp_path)
    assert first == fp.prompt_fingerprint("xx", ("b.txt", "a.txt"), root=tmp_path)
    (tmp_path / "b.txt").write_bytes(b"three\nfour\n")
    assert first == fp.prompt_fingerprint("xx", ("a.txt", "b.txt"), root=tmp_path)
    assert re.fullmatch(r"xx-[0-9a-f]{12}", first)


def test_any_byte_change_moves_the_version(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Teach the planner one rule.\n")
    (tmp_path / "b.txt").write_text("Second file.\n")
    before = fp.prompt_fingerprint("xx", ("a.txt", "b.txt"), root=tmp_path)
    (tmp_path / "b.txt").write_text("Second file!\n")
    changed = fp.prompt_fingerprint("xx", ("a.txt", "b.txt"), root=tmp_path)
    assert changed != before
    # Moving text from one file to another is a change too.
    (tmp_path / "a.txt").write_text("Teach the planner one rule.\nSecond file!\n")
    (tmp_path / "b.txt").write_text("")
    moved = fp.prompt_fingerprint("xx", ("a.txt", "b.txt"), root=tmp_path)
    assert moved not in (before, changed)


def test_specs_stamp_the_fingerprint() -> None:
    assert MainCreatorAgent.spec.prompt_version == MAIN_CREATOR_PROMPT_VERSION
    assert BriefExtractorAgent.spec.prompt_version == BRIEF_EXTRACTOR_PROMPT_VERSION
    assert MAIN_CREATOR_PROMPT_VERSION == fp.prompt_fingerprint("mc", MAIN_CREATOR_PROMPT_SOURCES)
    assert BRIEF_EXTRACTOR_PROMPT_VERSION == fp.prompt_fingerprint(
        "be", BRIEF_EXTRACTOR_PROMPT_SOURCES
    )
    assert MAIN_CREATOR_PROMPT_VERSION.startswith("mc-")
    assert BRIEF_EXTRACTOR_PROMPT_VERSION.startswith("be-")


def test_a_missing_source_changes_the_version_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("rule\n")
    present = fp.prompt_fingerprint("xx", ("a.txt", "gone.txt"), root=tmp_path)
    (tmp_path / "gone.txt").write_text("")
    assert fp.prompt_fingerprint("xx", ("a.txt", "gone.txt"), root=tmp_path) != present


_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


@pytest.mark.parametrize("sources", AGENTS.values())
def test_every_rendered_vocabulary_is_a_source(sources: tuple[str, ...]) -> None:
    used = {
        token
        for rel in sources
        for token in _TOKEN.findall((API_ROOT / rel).read_text(encoding="utf-8"))
        if token in prompt_vocab.PROMPT_VOCABULARIES
    }
    assert used, "no {{TOKEN}} found; update this guard if the prompts stopped using vocab tokens"
    assert "app/agents/prompt_vocab.py" in sources
    for token in used:
        assert prompt_vocab.PROMPT_VOCABULARY_SOURCES[token] in sources, token


def test_prompt_vocab_sources_match_its_imports() -> None:
    # Every vocabulary names the file it is built from, and those files are exactly
    # the app modules prompt_vocab imports, so a new vocabulary can't skip the map.
    assert set(prompt_vocab.PROMPT_VOCABULARY_SOURCES) == set(prompt_vocab.PROMPT_VOCABULARIES)
    text = (API_ROOT / "app/agents/prompt_vocab.py").read_text(encoding="utf-8")
    imported = {
        m.replace(".", "/") + ".py" for m in re.findall(r"^from (app\.[\w.]+) import", text, re.M)
    }
    assert set(prompt_vocab.PROMPT_VOCABULARY_SOURCES.values()) == imported


def test_changing_animation_vocabulary_moves_both_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vocab_rel = "app/agents/_schemas/text_animation_phases.py"
    edges = prompt_vocab.TEXT_ANIMATION_VOCAB
    # 1) The rendered prompt text changes when a displayed edge label changes.
    before = prompt_vocab.render_vocab_tokens("{{TEXT_ANIMATION_EDGES}}")
    monkeypatch.setitem(edges, "silkReveal", replace(edges["silkReveal"], label="Silky Reveal"))
    monkeypatch.setitem(
        prompt_vocab.PROMPT_VOCABULARIES, "TEXT_ANIMATION_EDGES", prompt_vocab._edges()
    )
    after = prompt_vocab.render_vocab_tokens("{{TEXT_ANIMATION_EDGES}}")
    assert after != before and "silkReveal (Silky Reveal)" in after
    # 2) The same edit to the file moves both fingerprints; an untouched copy does not.
    for rel in set(MAIN_CREATOR_PROMPT_SOURCES) | set(BRIEF_EXTRACTOR_PROMPT_SOURCES):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((API_ROOT / rel).read_bytes())
    assert fp.prompt_fingerprint("mc", MAIN_CREATOR_PROMPT_SOURCES, root=tmp_path) == (
        MAIN_CREATOR_PROMPT_VERSION
    )
    source = (tmp_path / vocab_rel).read_text(encoding="utf-8")
    edited = source.replace('EdgeVocab("Silk Reveal",', 'EdgeVocab("Silky Reveal",', 1)
    assert edited != source
    (tmp_path / vocab_rel).write_text(edited, encoding="utf-8")
    mc = fp.prompt_fingerprint("mc", MAIN_CREATOR_PROMPT_SOURCES, root=tmp_path)
    be = fp.prompt_fingerprint("be", BRIEF_EXTRACTOR_PROMPT_SOURCES, root=tmp_path)
    assert mc != MAIN_CREATOR_PROMPT_VERSION
    assert be != BRIEF_EXTRACTOR_PROMPT_VERSION
