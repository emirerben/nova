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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents import _prompt_fingerprint as fp
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
