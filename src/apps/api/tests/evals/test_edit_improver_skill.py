"""Edit-improver taste skill: a model-free checker, hand-authored golden cases, negative controls.

``check_picks`` scores a candidate ``SuggestionAgentOutput`` against the rules written in
``app/agents/skills/edit-improver/SKILL.md`` without calling a model (replay only). It proves the
STRUCTURAL rules. Whether a pick is tasteful beyond them is the rubric judge's job
(``rubrics/suggestion_agent.md``, replay with ``--with-judge``); nothing here claims
creative quality.

Ways this unit can fail, written before the checker (each maps to a failure code):

 1. A polish card outranks a fix card, or default_edit is not rank 1
    -> tier_order / default_not_first
 2. A card for a kind already in the edit or already dismissed -> already_present
 3. A card that undoes what the creator asked for (KRI-129) -> request_conflict
 4. A card with no measured evidence behind its target (cosmetic filler) -> no_evidence
 5. An unissued kind or target, or a card spanning two changes
    -> unknown_kind / target_not_issued / arity
 6. A ``why`` that is long, hype, a verdict, quotes footage, invents a number, is ungrounded
    or is in the wrong language -> why_*
 7. Hook copy that reuses footage text verbatim (copy laundering), is not grounded in what the
    footage shows, reads as cringe, or sits on a kind that carries no copy -> copy_*
 8. More than five cards, rank gaps, a schema break -> schema / rank_gap
 9. The checker itself passing everything: golden BAD cases and mutations of GOOD cases must
    each be rejected with the intended code.

The input shape here (``issued`` + ``evidence``) is the contract the checker reads; the agent lane
adapts its prompt input to it or maps it. Fixtures: ``tests/fixtures/agent_evals/edit_improver``.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.agents._skills import load_skill
from app.services.suggestion_chip_copy import STOPLIST
from app.services.suggestions import registry
from app.services.suggestions.models import (
    MAX_SUGGESTIONS,
    WHOLE_VIDEO_HANDLE,
    InvalidAgentOutput,
    parse_agent_output,
)

CASES_DIR = Path(__file__).parents[1] / "fixtures" / "agent_evals" / "edit_improver"
RUBRIC = Path(__file__).parent / "rubrics" / "suggestion_agent.md"
SKILL_FILE = Path(__file__).parents[2] / "app" / "agents" / "skills" / "edit-improver" / "SKILL.md"

MAX_WHY = 48
MAX_COPY_WORDS = 8

# Skill tiers: lower number = more impact. default_edit is handled separately (always rank 1).
TIER: dict[str, int] = {
    "default_edit": -1,
    "mute_clip": 0,
    "hook": 0,
    "duplicates": 0,
    "filler_words": 0,
    "captions": 1,
    "opener_clip": 1,
    "sfx": 2,
    "transition": 2,
    "music": 2,
    "title_animation": 2,
    "generated_overlay": 2,
    "pinned_text": 2,
}
# One change per card: how many target handles a kind may carry.
ARITY: dict[str, int] = {
    "mute_clip": 1,
    "duplicates": 2,
    "opener_clip": 1,
    "generated_overlay": 1,
}  # every other kind is whole-video: exactly ["all"]

# Failure code -> rubric dimension (rubrics/suggestion_agent.md).
DIMENSION: dict[str, str] = {
    "schema": "contract",
    "unknown_kind": "contract",
    "unknown_target": "contract",
    "target_not_issued": "contract",
    "rank_gap": "contract",
    "arity": "one_change_per_card",
    "tier_order": "impact_order",
    "default_not_first": "impact_order",
    "already_present": "restraint",
    "no_evidence": "evidence_grounding",
    "request_conflict": "request_fidelity",
    "why_length": "why_voice",
    "why_hype": "why_voice",
    "why_number": "evidence_grounding",
    "why_ungrounded": "evidence_grounding",
    "why_quotes_footage": "why_voice",
    "why_language": "why_voice",
    "copy_on_non_hook": "contract",
    "copy_missing": "hook_craft",
    "copy_too_long": "hook_craft",
    "copy_reuses_footage_text": "hook_craft",
    "copy_ungrounded": "hook_craft",
    "copy_cringe": "hook_craft",
    "copy_language": "hook_craft",
}

# KRI-129: kind -> phrases (folded text) where the creator asked for the opposite.
REQUEST_LOCKS: dict[str, re.Pattern[str]] = {
    "captions": re.compile(
        r"\bno (captions?|subtitles?)\b|without (captions?|subtitles?)"
        r"|(don t|do not) (add|want|need) (any )?(captions?|subtitles?)"
        r"|altyazi (istemiyorum|olmasin|yok|koyma|ekleme)"
    ),
    "mute_clip": re.compile(
        r"keep (the )?(original|natural|camera|clip|real) (sound|audio)|keep (the )?(sound|audio)"
        r"|(don t|do not) mute|sesi (kapatma|koru)|orijinal ses(i)? (kalsin|olsun|koru)"
    ),
    "duplicates": re.compile(
        r"keep (all|every|both)( of)?( the)? clips?|(don t|do not) (remove|cut|drop) any clips?"
        r"|hicbir klip(i)? (silme|cikarma)|tum klipler(i)? (kalsin|kullan)|hepsini (kullan|koru)"
    ),
    "music": re.compile(
        r"(keep|leave) (the )?music (loud|as is|up)|(don t|do not) (touch|change|lower) the music"
        r"|muzigi (yuksek tut|degistirme|kisma)|muzik (yuksek|aynen) kalsin"
    ),
    "hook": re.compile(
        r"\bno (title|hook|text)\b|without (a )?(title|text)|use my (own )?(title|text|wording)"
        r"|(metin|baslik) istemiyorum|kendi (basligim|yazim)"
    ),
    "sfx": re.compile(r"\bno (sound effects|sfx)\b|efekt istemiyorum"),
    "transition": re.compile(
        r"\bno transitions?\b|hard cuts only|gecis (istemiyorum|koyma|ekleme)"
    ),
    "filler_words": re.compile(
        r"keep (the )?(ums|filler)|keep it raw|(don t|do not) cut (my )?(words|pauses)"
        r"|dolgu(lari)? (kalsin|silme)|oldugu gibi birak"
    ),
    "generated_overlay": re.compile(
        r"\bno (ai|generated) (images?|overlays?)\b|ai gorsel istemiyorum"
    ),
}

_JUDGEMENT = frozenset(
    "best better great good nice boring weak bad awful stunning beautiful cringe "
    "guzel harika muhtesem sikici kotu zayif mukemmel".split()
)
_CRINGE_PHRASES = (
    "nobody tells you",
    "nobody told you",
    "you won t believe",
    "you wont believe",
    "changed my life",
    "game changer",
    "here s why",
    "what i learned",
    "taught me",
    "kimse sana",
    "inanamayacaksin",
    "hayatimi degistir",
    "oyun degistirici",
    "ogrendigim",
)
_CRINGE_STEMS = (
    "lesson",
    "secret",
    "journey",
    "transform",
    "mindset",
    "hustle",
    "grind",
    "success",
    "unlock",
    "ders",
    "sir",
    "yolculuk",
    "donusum",
    "basari",
    "motivasyon",
    "ogretti",
)
_EN_FUNC = frozenset(
    "the a an in on of is are and or for with at to from by this that it its my your clip clips"
    " noise wind music cuts cut between over words word speech filler hard shots shot same two"
    " no from".split()
)
_TR_FUNC = frozenset(
    "ve bir bu su icin ile de da var yok klip klipte klipler yerde ses sesi muzik eller el hamur"
    " arasinda kesme kelime dolgu ayni iki cekim".split()
)
_STOP = frozenset("with this that from have been your into over only same their about".split())
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿]")


def fold(text: str) -> str:
    text = text.replace("İ", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return fold(text).split()


def _content(text: str) -> set[str]:
    return {t for t in _tokens(text) if len(t) >= 4 and t not in _STOP and not t.isdigit()}


def _stem_overlap(a: set[str], b: set[str]) -> bool:
    return any(x == y or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5]) for x in a for y in b)


def _shingles(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _shares_run(copy_tokens: list[str], texts: list[str], n: int) -> bool:
    mine = _shingles(copy_tokens, n)
    return any(mine & _shingles(_tokens(text), n) for text in texts)


def _language_ok(text: str, language: str) -> bool:
    folded_tokens = set(_tokens(text))
    diacritics = sum(ch in "çğıöşüâîÇĞİÖŞÜ" for ch in text)
    tr_score = diacritics + len(folded_tokens & _TR_FUNC)
    en_score = len(folded_tokens & _EN_FUNC)
    if language == "tr":
        return tr_score > 0 and tr_score >= en_score
    return diacritics == 0 and tr_score <= en_score


def _hype_or_cringe(text: str, phrases: tuple[str, ...], stems: tuple[str, ...]) -> bool:
    folded = fold(text)
    if any(p in folded for p in phrases):
        return True
    return any(
        any(t.startswith(s) for s in stems if len(s) > 3) or t == s
        for t in folded.split()
        for s in stems
    )


@dataclass(frozen=True)
class Report:
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def dimensions(self) -> dict[str, int]:
        failed = {DIMENSION[f] for f in self.failures}
        dims = set(DIMENSION.values())
        return {d: (1 if d in failed else 5) for d in sorted(dims)}


def _issued(inp: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    return {
        row["kind"]: {t["id"]: list(t.get("evidence", [])) for t in row["targets"]}
        for row in inp["issued"]
    }


def check_picks(output: dict[str, Any], inp: dict[str, Any]) -> Report:
    """Failure codes for one candidate answer; an empty list of failures is a pass."""
    issued = _issued(inp)
    all_targets = sorted({t for row in issued.values() for t in row} - {WHOLE_VIDEO_HANDLE})
    try:
        parsed = parse_agent_output(output, kinds=sorted(issued), target_ids=all_targets)
    except InvalidAgentOutput as exc:
        return Report((exc.reason,))

    fails: list[str] = []
    language = inp["language"]
    facts = inp["evidence"]
    untrusted = [str(t) for t in inp.get("untrusted_texts", [])]
    blocked = set(inp.get("already_in_edit", [])) | set(inp.get("dismissed", []))
    request = fold(inp.get("creator_request", ""))
    picks = sorted(parsed.ranked, key=lambda p: p.rank)

    if [p.rank for p in picks] != list(range(1, len(picks) + 1)):
        fails.append("rank_gap")
    if len(picks) > MAX_SUGGESTIONS:
        fails.append("schema")

    last_tier = -99
    for pick in picks:
        kind = pick.kind
        if kind in blocked:
            fails.append("already_present")
        lock = REQUEST_LOCKS.get(kind)
        if lock and lock.search(request):
            fails.append("request_conflict")

        if kind == "default_edit":
            if pick.rank != 1:
                fails.append("default_not_first")
        else:
            tier = TIER[kind]
            if tier < last_tier:
                fails.append("tier_order")
            last_tier = max(last_tier, tier)

        handles = list(pick.target_ids)
        want = ARITY.get(kind)
        if want is None:
            if handles != [WHOLE_VIDEO_HANDLE]:
                fails.append("arity")
        elif len(handles) != want or WHOLE_VIDEO_HANDLE in handles:
            fails.append("arity")
        if any(h not in issued[kind] for h in handles):
            fails.append("target_not_issued")
            continue
        evidence_ids = [e for h in handles for e in issued[kind][h]]
        evidence_ids = [e for e in evidence_ids if e in facts]
        if not handles or any(not issued[kind][h] for h in handles) or not evidence_ids:
            fails.append("no_evidence")
        fact_text = " ".join(facts[e]["fact"] for e in evidence_ids)

        _check_why(pick.why, language, fact_text, untrusted, fails)
        _check_copy(pick, language, fact_text, evidence_ids, facts, untrusted, fails)
    return Report(tuple(dict.fromkeys(fails)))


def _check_why(why: str, language: str, fact_text: str, untrusted: list[str], fails: list[str]):
    text = " ".join(why.split())
    if not text or len(text) > MAX_WHY:
        fails.append("why_length")
    folded = fold(text)
    padded = f" {folded} "
    judged = bool(set(folded.split()) & _JUDGEMENT) or "en iyi" in folded
    stop = any(item in padded or item in folded and " " in item for item in STOPLIST)
    marks = (
        "!" in text
        or "#" in text
        or "@" in text
        or _EMOJI.search(text)
        or any(q in text for q in "\"'`“”‘’«»")
    )
    if judged or stop or marks:
        fails.append("why_hype")
    numbers = {t for t in folded.split() if t.isdigit()}
    if numbers - {t for t in fold(fact_text).split() if t.isdigit()}:
        fails.append("why_number")
    if text and not _stem_overlap(_content(text), _content(fact_text)):
        fails.append("why_ungrounded")
    if _shares_run(_tokens(text), untrusted, 3):
        fails.append("why_quotes_footage")
    if text and not _language_ok(text, language):
        fails.append("why_language")


def _check_copy(pick, language, fact_text, evidence_ids, facts, untrusted, fails):
    text = " ".join((pick.copy_text or "").split())
    if pick.kind != "hook":
        if pick.copy_text is not None:
            fails.append("copy_on_non_hook")
        return
    if not text:
        fails.append("copy_missing")
        return
    tokens = _tokens(text)
    if len(tokens) > MAX_COPY_WORDS or "\n" in (pick.copy_text or ""):
        fails.append("copy_too_long")
    if _shares_run(tokens, untrusted, 3) or any(
        fold(text) in fold(u) or (fold(u) and fold(u) in fold(text)) for u in untrusted
    ):
        fails.append("copy_reuses_footage_text")
    visual = " ".join(facts[e]["fact"] for e in evidence_ids if facts[e].get("signal") == "visual")
    if not _stem_overlap(_content(text), _content(visual or fact_text)):
        fails.append("copy_ungrounded")
    bad_chars = (
        "!" in text or "#" in text or _EMOJI.search(text) or any(q in text for q in "\"'`“”‘’«»")
    )
    numbers = {t for t in tokens if t.isdigit()}
    invented = numbers - {t for t in fold(fact_text).split() if t.isdigit()}
    if (
        bad_chars
        or invented
        or _hype_or_cringe(text, _CRINGE_PHRASES, _CRINGE_STEMS)
        or any(item in f" {fold(text)} " for item in STOPLIST)
    ):
        fails.append("copy_cringe")
    if not _language_ok(text, language):
        fails.append("copy_language")


# ── cases ────────────────────────────────────────────────────────────────────────────


def _load() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]


CASES = _load()
GOOD = [c for c in CASES if c["expect"] == "pass"]
BAD = [c for c in CASES if c["expect"] != "pass"]


def _id(case: dict[str, Any]) -> str:
    return case["id"]


@pytest.mark.parametrize("case", GOOD, ids=_id)
def test_good_picks_pass(case):
    report = check_picks(case["output"], case["input"])
    assert report.passed, report.failures


@pytest.mark.parametrize("case", BAD, ids=_id)
def test_bad_picks_are_rejected_with_the_intended_code(case):
    report = check_picks(case["output"], case["input"])
    assert set(report.failures) == set(case["expect_failures"]), (
        case["why_bad"],
        report.failures,
    )


def test_corpus_covers_both_languages_and_both_outcomes():
    assert len(CASES) >= 12
    assert len({c["id"] for c in CASES}) == len(CASES)
    for outcome in (GOOD, BAD):
        assert {c["input"]["language"] for c in outcome} == {"en", "tr"}
    assert any(not c["output"]["ranked"] for c in GOOD), "an empty set must be a valid answer"
    assert any(c["input"]["state"] == "first_prompt" for c in CASES)
    assert any(c["input"]["state"] == "ready" for c in CASES)


def test_every_failure_code_is_exercised_by_a_bad_case():
    exercised = {code for c in BAD for code in c["expect_failures"]}
    missing = set(DIMENSION) - exercised
    assert not missing, f"no negative control for {sorted(missing)}"


# ── negative controls: mutate a known-good answer, the checker must reject it ─────────


def _picks(case):
    return case["output"]["ranked"]


MUTABLE = [c for c in GOOD if _picks(c)]


@pytest.mark.parametrize("case", MUTABLE, ids=_id)
def test_marking_a_picked_kind_as_present_is_rejected(case):
    inp = copy.deepcopy(case["input"])
    inp["already_in_edit"] = [*inp.get("already_in_edit", []), _picks(case)[0]["kind"]]
    assert "already_present" in check_picks(case["output"], inp).failures


@pytest.mark.parametrize("case", MUTABLE, ids=_id)
def test_stripping_the_evidence_is_rejected(case):
    inp = copy.deepcopy(case["input"])
    for row in inp["issued"]:
        for target in row["targets"]:
            target["evidence"] = []
    assert "no_evidence" in check_picks(case["output"], inp).failures


@pytest.mark.parametrize("case", MUTABLE, ids=_id)
def test_a_hype_why_is_rejected(case):
    out = copy.deepcopy(case["output"])
    out["ranked"][0]["why"] = "Amazing fix!"
    assert "why_hype" in check_picks(out, case["input"]).failures


@pytest.mark.parametrize("case", MUTABLE, ids=_id)
def test_a_sixth_card_is_rejected(case):
    out = copy.deepcopy(case["output"])
    extra = [
        {**out["ranked"][0], "target_ids": list(out["ranked"][0]["target_ids"]), "rank": n}
        for n in range(1, 7)
    ]
    out["ranked"] = extra
    assert check_picks(out, case["input"]).failures == ("schema",)


@pytest.mark.parametrize(
    "case", [c for c in MUTABLE if len({TIER[p["kind"]] for p in _picks(c)}) > 1], ids=_id
)
def test_reversing_a_fix_and_polish_order_is_rejected(case):
    out = copy.deepcopy(case["output"])
    n = len(out["ranked"])
    for pick in out["ranked"]:
        pick["rank"] = n + 1 - pick["rank"]
    assert {"tier_order", "default_not_first"} & set(check_picks(out, case["input"]).failures)


@pytest.mark.parametrize("case", [c for c in GOOD if any(p["copy"] for p in _picks(c))], ids=_id)
def test_laundering_the_transcript_into_hook_copy_is_rejected(case):
    out = copy.deepcopy(case["output"])
    inp = copy.deepcopy(case["input"])
    hook = next(p for p in out["ranked"] if p["kind"] == "hook")
    inp["untrusted_texts"] = [hook["copy"]]
    assert "copy_reuses_footage_text" in check_picks(out, inp).failures


# ── skill and rubric stay wired to the checker ───────────────────────────────────────


def test_skill_loads_inside_the_runtime_budget():
    body = load_skill("edit-improver")
    assert 0 < len(body) <= 6000
    assert "description:" not in body
    assert "grounded-hook" in body  # craft is referenced, not duplicated
    assert len(body) < len(load_skill("grounded-hook")) * 3  # stays compact (SkillOpt)
    source = SKILL_FILE.read_text(encoding="utf-8")
    assert source.startswith("---\nname: edit-improver\n")


def test_skill_tiers_match_the_checker_table():
    body = load_skill("edit-improver")
    sentence = re.search(r"Fix tier:(.*?)Never rank", body, re.S)
    assert sentence, "skill must state the tier order"
    fix, essential, polish = re.split(r"Essential tier:|Polish tier:", sentence.group(1))
    for tier, chunk in ((0, fix), (1, essential), (2, polish)):
        for kind in re.findall(r"[a-z_]+", chunk):
            if kind in TIER:
                assert TIER[kind] == tier, kind


def test_every_registered_kind_has_a_tier_and_is_named_by_the_skill():
    body = load_skill("edit-improver")
    for kind in registry.all_kinds():
        assert kind.kind in TIER, f"{kind.kind}: add it to TIER and the skill"
        assert re.search(rf"\b{kind.kind}\b", body), f"{kind.kind}: the skill does not mention it"


def test_rubric_names_every_checker_dimension_and_the_pass_bar():
    text = RUBRIC.read_text(encoding="utf-8")
    for dim in set(DIMENSION.values()):
        assert dim in text, dim
    assert "avg >= 4.0" in text
    assert "scores" in text


def test_report_dimensions_flag_only_the_broken_rule():
    report = check_picks(BAD[0]["output"], BAD[0]["input"])
    dims = report.dimensions()
    assert any(v == 1 for v in dims.values())
    assert all(1 <= v <= 5 for v in dims.values())
