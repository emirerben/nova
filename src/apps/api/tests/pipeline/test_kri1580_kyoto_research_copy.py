"""KRI-1580 on the Kyoto Torokko fixture (prod job 3930c822): researched copy in a design's shape.

Failure modes, written before the code:
1. Flag off changes the writer, layers or reviewer prompt, or the ops the shipped answer becomes.
2. A "Label: claim" line passes because the label check reads every fact, not the cited one
   ("Kyoto Demiryolu" over fact-11, "Hozukyo Geçidi" over fact-8).
3. A number or name the cited fact does not state passes (the number check skipped this path).
4. A subtitle ask (B1) leaves clips bare, as prod did with 3 of 7, or the correction round does
   not tell the writer which clips.
5. The hook is missing, is not the first row, lands outside the opening, or does not resolve to
   the hook role; facts start inside the hook.
6. Cross-faded clips (9.4 vs 9.52 s) make one line per clip collide as "overlapping".
7. A heading and its detail do not survive approval: the pinned replay drops the pair, the hook
   or the full fact set, or rejects the shared window as an overlap.
8. A call site ignores the design: the guided proposal, the preview and its approved render, or
   follow-up chat; or a render with no stamp is reshaped by the flag alone.
9. A design writes more rows than approval (12 texts) or the editor (16 ops) holds, and the
   preview or the render crashes instead of fitting or refusing.
10. A design places text where legacy refuses it: over a spoken edit with no speech timing, or
   in a first clip the creator's own text protects, so B1 is offered a window it must fill and
   placement always refuses.
11. The windows the writer is offered disagree with what placement accepts: a generated opening
   she deleted (an editor tombstone) offers a hook that placement then refuses, or a spoken
   edit offers more pauses than the row budget holds while the guide must fill every one.
12. Live answers the checks wrongly refuse: a framing opener before a name ("İşte Kyoto'nun"),
   a fact word with a Turkish ending ("Trenin" over "Tren"), German nouns read as names.
13. Limits a row slips past: "3 km" grounded by "7.3 km", a heading and detail over the window's
   reading time together, a titled fact with no heading, a fact set on every row outgrowing the
   editor state, a partly bare guide reported as her whole ask failing.

The writer answers in `research_copy_designed.json` are authored from the teardown, and reviews
are stubbed: assertions on them are structural, so a live capture can replace the file.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from app.kria import research_preview
from app.kria.brief import BriefRequirement, CreativeBrief
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.informative_text import informative_text_scope
from app.pipeline.phone_text_style import measure_box, text_role
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot
from app.schemas.research import PreparedResearch, ResearchEvidence
from app.services import (
    creation_text_composition,
    phone_montage_preflight,
    researched_text,
)
from app.services.creation_text_composition import CreationTextComposer, compose_creation_text
from app.services.kria_editor_ops import apply_text_lane_ops, project_editor_draft
from app.services.phone_text_styling import (
    PHONE_TEXT_STYLE_FIELD,
    phone_text_generation,
    phone_text_policy_scope,
    style_rows,
)
from app.services.researched_text import (
    DesignedCopyOutput,
    ResearchCopyAgent,
    ResearchCopyInput,
    ResearchCopyOutput,
    ResearchedLayer,
    ResearchLayersAgent,
    ResearchLayersInput,
    ResearchRequest,
    ResearchReviewAgent,
    ResearchReviewInput,
    ResearchReviewOutput,
    ResearchUnavailable,
    chat_text_design,
    design_opening,
    design_windows,
    designed_operations,
    pinned_research_operations,
    prepared_evidence_facts,
    prepared_research_texts,
    research_operations,
    research_text_design,
    research_windows,
    run_researched_revision,
    uncovered_design_windows,
    ungrounded_terms,
)
from app.tasks import generative_build
from app.text_fold import loose_text

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1585_kyoto"
_LABEL = re.compile(r"^[^\d:\n]{1,60}:\s")
_ASK = "Bu Kyoto'daki Toroko treni tren hakkında bilgilendirici alt yazı istiyorum"


def _load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _attempt() -> dict:
    """The writer call whose answer shipped (the second 10:15 UTC caption_binding)."""
    return _load("research.json")["caption_binding_attempts"][1]


def _snapshot() -> dict:
    """The snapshot research saw. The writer's copy drops each slot's cut and the beat windows;
    the shipped rows' timeline binding and the windows research offered restore them."""
    snapshot = dict(_attempt()["input"]["snapshot"])
    shipped = _load("plan.json")["approved_proposal"]["text_composition"]["operations"]
    binding = shipped[0]["research"]["timeline_binding"]
    snapshot["slots"] = [
        {**slot, "clip_index": cut["clip_index"], "in_s": cut["in_s"]}
        for slot, cut in zip(snapshot["slots"], binding, strict=True)
    ]
    snapshot["story_beat_windows"] = [
        {"start_s": row["start_s"], "end_s": row["end_s"]}
        for row in snapshot.pop("research_windows")
    ]
    return snapshot


def _evidence() -> ResearchEvidence:
    return ResearchEvidence.model_validate(_attempt()["input"]["evidence"])


def _request() -> str:
    return _attempt()["input"]["request"]


def _designed(design: str) -> DesignedCopyOutput:
    return DesignedCopyOutput.model_validate(_load("research_copy_designed.json")[design])


def _quote(window_id: str) -> str:
    window = next(row for row in design_windows(_snapshot(), "B1") if row["id"] == window_id)
    return window["seen"][0]["text"][:16]


def _policy() -> dict:
    return {**_load("plan.json")["variant"]["phone_text_style"], "informative_text_designs": True}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _level(row: dict) -> str:
    return row["source_params"]["research"]["design"]["level"]


# -- 1. flag off ---------------------------------------------------------------------------

# Computed on origin/main 9164c0b6 from this branch's fixture, before researched_text.py changed.
_MAIN_PROMPTS = {
    "copy": "d0a7849cd91b09b6870f4a8b1fb30fd04533191dcfc887f30cd0fb8fe9aecad5",
    "review": "3df9499a743c9e3c1860d2a5d1bf6b5d04f229b98debbcde5b178cb662402761",
    "review_layers": "1b876b5c6f8c825a126d217e4238da553e25e70212a65ff5b2010eea9d3bb233",
    "layers": "bdf966a9c120ff16633615a1f090f9a88bd81c7b24270cd97210b4404f5a1bca",
}


def test_flag_off_prompts_are_byte_identical_to_main():
    copy_input = ResearchCopyInput.model_validate(_attempt()["input"])
    shipped = ResearchCopyOutput.model_validate(_attempt()["output"])
    layer = ResearchedLayer(
        text="7.3 km", language="tr", position="bottom", start_s=4, end_s=9, evidence_ids=["fact-6"]
    )
    layers_input = ResearchLayersInput(
        request=copy_input.request,
        snapshot={
            "slots": _attempt()["input"]["snapshot"]["slots"],
            "total_duration_s": 60.001,
            "text_requirements": [],
        },
        evidence=_evidence(),
    )
    review = ResearchReviewAgent(None)
    assert {
        "copy": _sha(ResearchCopyAgent(None).render_prompt(copy_input)),
        "review": _sha(
            review.render_prompt(ResearchReviewInput(context=copy_input, proposed=shipped))
        ),
        "review_layers": _sha(
            review.render_prompt(ResearchReviewInput(context=copy_input, layers=[layer]))
        ),
        "layers": _sha(ResearchLayersAgent(None).render_prompt(layers_input)),
    } == _MAIN_PROMPTS


def test_flag_off_the_shipped_answer_becomes_the_shipped_ops():
    assert research_windows(_snapshot()) == _attempt()["input"]["snapshot"]["research_windows"]
    ops = research_operations(
        _attempt()["output"]["captions"], _evidence(), _snapshot(), _request()
    )
    assert ops == _load("plan.json")["approved_proposal"]["text_composition"]["operations"]


# -- 2/3. prod's own wording, and names and numbers, under a design ------------------------------


def _reject(written: dict, text_design: str) -> str:
    with pytest.raises(ResearchUnavailable) as caught:
        designed_operations(
            DesignedCopyOutput.model_validate(written),
            _evidence(),
            _snapshot(),
            _request(),
            text_design,
        )
    return str(caught.value)


def _one(heading: str, text: str, evidence_ids: list[str]) -> dict:
    return {
        "hook": _designed("A2").hook.model_dump(),
        "captions": [
            {
                "window_id": "window-0",
                "heading": heading,
                "text": text,
                "evidence_ids": evidence_ids,
                "visual_quote": _quote("window-0"),
            }
        ],
    }


def test_the_shipped_colon_line_is_rejected_under_a_design():
    shipped = _attempt()["output"]["captions"][0]
    assert "colon" in _reject(_one("", shipped["text"], shipped["evidence_ids"]), "A2")


@pytest.mark.parametrize(
    ("index", "name"), [(1, "Kyoto"), (2, "Hozukyo")], ids=["kyoto-demiryolu", "hozukyo-gecidi"]
)
def test_a_shipped_heading_its_cited_fact_does_not_name_is_rejected(index, name):
    shipped = _attempt()["output"]["captions"][index]
    written = _one(shipped["context_label"], "Ahşap banklar", shipped["evidence_ids"])
    assert name in _reject(written, "A2")


@pytest.mark.parametrize(
    ("heading", "text", "name"),
    [
        ("Sagano treni", "Ahşap banklar", "Sagano"),
        ("Retro vagonlar", "Ahşap banklar, Arashiyama yakın", "Arashiyama"),
    ],
    ids=["heading-opens-with-a-name", "name-after-a-comma"],
)
def test_a_name_in_no_cited_fact_is_rejected_wherever_it_sits(heading, text, name):
    assert name in _reject(_one(heading, text, ["fact-11"]), "A2")


def test_the_grounded_shipped_label_passes_as_a_heading():
    shipped = _attempt()["output"]["captions"][0]
    written = DesignedCopyOutput.model_validate(
        _one(shipped["context_label"], "7,3 km'lik bir yol", shipped["evidence_ids"])
    )
    ops, _missing = designed_operations(written, _evidence(), _snapshot(), _request(), "A2")
    assert [op["text"] for op in ops][1:] == ["Torokko Saga İstasyonu", "7,3 km'lik bir yol"]


def test_a_number_its_cited_fact_does_not_state_is_rejected():
    written = _one("", "Manzara 4 mevsim değişiyor", ["fact-8"])
    assert "4" in _reject(written, "A2")


def test_the_teardown_train_name_is_in_no_fact_and_is_rejected():
    written = _one("", "İşte trenimiz, Sagano Romantik Treni", ["fact-11"])
    assert "Sagano" in _reject(written, "A2")


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        ("Every season is different", []),
        ("I love the 7.3 km ride", []),
        ("Ray, Hozukyo Geçidi boyunca kıvrılıyor", []),
        ("Kyoto Railway opened it", ["Kyoto", "Railway"]),
        ("Altyazısız olsun", []),
    ],
)
def test_grounding_reads_names_and_numbers_not_sentence_case(text, missing):
    fact = _evidence().facts[1]  # fact-6: Saga, Kameoka, Hozukyo Geçidi, 7.3, 25
    assert ungrounded_terms(text, [fact]) == missing


def _opened(text_design: str, hook: str, line: str, ids=("fact-6",)) -> dict:
    """One hook and one line (B1) or titled detail (A2) over window-0, all citing ``ids``."""
    caption = {
        "window_id": "window-0",
        "heading": "" if text_design == "B1" else "7,3 km",
        "text": line,
        "evidence_ids": ["fact-6"],
        "visual_quote": _quote("window-0"),
    }
    return {"hook": {"text": hook, "evidence_ids": list(ids)}, "captions": [caption]}


_FACT6_LINE = "Saga'dan Kameoka'ya 25 dakika"
# The authored Kyoto copy's sentence openers that are framing, not names ("İşte trenimiz").
_FRAMING_OPENERS = {"İşte", "Ray"}
# Endings the Kyoto copy puts on a fact's own word ("Tren" -> "Trenin", "Trenimiz").
_ORACLE_ENDINGS = ("", "i", "in", "imiz", "e", "de", "den", "le", "ler", "lar")


def _oracle_grounded(name: str, cited_text: str, cited_words: set[str]) -> bool:
    """A plain oracle, not the server's check: the name's first five letters are in the cited
    text, or it is a cited word plus one of the copy's own endings."""
    return name[: min(5, len(name))] in cited_text or any(
        name == word + ending for word in cited_words for ending in _ORACLE_ENDINGS
    )


@pytest.mark.parametrize("text_design", ["A2", "B1"])
@pytest.mark.parametrize(
    ("hook", "name"),
    [
        ("Arashiyama'nın en güzel 25 dakikası", "Arashiyama"),
        ("Nara'nın en güzel 25 dakikası", "Nara"),
        ("Osaka'dan 25 dakika uzakta", "Osaka"),
        ("Kyoto's most beautiful 25 minutes", "Kyoto"),
        ("Nara is 25 minutes away", "Nara"),
        ("Arashiyama treniyle 25 dakika", "Arashiyama"),
        ("Osaka to Kameoka in 25 minutes", "Osaka"),
    ],
)
def test_a_hook_that_opens_with_a_name_its_facts_do_not_state_is_rejected(text_design, hook, name):
    assert name in _reject(_opened(text_design, hook, _FACT6_LINE), text_design)


@pytest.mark.parametrize(
    ("text_design", "line", "name"),
    [
        ("B1", "Kyoto'nun treni Hozukyo Geçidi boyunca kıvrılıyor", "Kyoto"),
        ("B1", "Tren Sagano'dan Kameoka'ya gidiyor", "Sagano"),
        ("A2", "Tren Sagano'dan Kameoka'ya gidiyor", "Sagano"),
    ],
    ids=["line-opens-with-a-suffixed-name", "b1-stem-of-a-shorter-fact-word", "a2-detail-stem"],
)
def test_a_line_naming_what_its_facts_do_not_is_rejected(text_design, line, name):
    assert name in _reject(_opened(text_design, "Her mevsim başka", line), text_design)


def test_a_heading_sharing_a_short_facts_stem_is_rejected():
    """fact-6 says "Saga"; "Sagano" is a different name, in no fact."""
    assert "Sagano" in _reject(_one("Sagano treni", "Ahşap banklar", ["fact-6"]), "A2")


@pytest.mark.parametrize("text_design", ["A2", "B1"])
@pytest.mark.parametrize(
    ("hook", "ids"),
    [
        ("Her mevsim başka bir 25 dakika", ["fact-6"]),
        ("İşte 25 dakikalık bir yolculuk", ["fact-6"]),
        ("That's 25 minutes of river views", ["fact-6"]),
        ("Let's ride for 25 minutes", ["fact-6"]),
        ("Today's 25 minutes of river views", ["fact-6"]),
        ("Bu 25 dakika başka", ["fact-6"]),
        ("Kameoka'ya 25 dakika", ["fact-6"]),
        ("Kyoto'nun en güzel 25 dakikası", ["fact-1", "fact-6"]),
    ],
)
def test_a_framing_hook_or_one_whose_facts_name_its_subject_passes(text_design, hook, ids):
    written = DesignedCopyOutput.model_validate(_opened(text_design, hook, _FACT6_LINE, ids))
    ops, _missing = designed_operations(written, _evidence(), _snapshot(), _request(), text_design)
    assert ops[0]["text"] == hook


@pytest.mark.parametrize(
    ("text", "facts"),
    [
        ("Tren, Hozukyo Geçidi boyunca kıvrılıyor", ["fact-6"]),
        ("Ahşap banklı retro vagonlar", ["fact-11"]),
        ("Mevsimden mevsime değişen manzaralar", ["fact-8"]),
    ],
)
def test_a_stem_the_cited_fact_carries_still_grounds_the_word(text, facts):
    cited = [fact for fact in _evidence().facts if fact.id in facts]
    assert ungrounded_terms(text, cited, heading=True) == []


# -- 4. coverage -----------------------------------------------------------------------------


def test_b1_windows_are_one_per_clip_after_the_hook_without_crossfade_overlap():
    windows = design_windows(_snapshot(), "B1")
    assert len(windows) == len(_snapshot()["slots"]) == 7
    assert windows[0]["start_s"] == 4.0
    assert [len(row["media_ids"]) for row in windows] == [1] * 7
    assert all(a["end_s"] <= b["start_s"] for a, b in zip(windows, windows[1:], strict=False))


def test_a_titled_design_is_offered_only_windows_it_can_place():
    """A 1.7 s clip is a B1 line; under A1-A3 (2.0 s minimum) it joins its neighbour."""
    snapshot = _snapshot()
    snapshot["slots"][2]["output_end_s"] = 20.0
    snapshot["slots"][3]["output_start_s"] = 19.88
    guide = design_windows(snapshot, "B1")
    assert min(row["end_s"] - row["start_s"] for row in guide) < 2.0
    for text_design in ("A1", "A2", "A3"):
        windows = design_windows(snapshot, text_design)
        assert min(row["end_s"] - row["start_s"] for row in windows) >= 2.0
        short = next(row for row in windows if row["start_s"] == 18.187)
        written = DesignedCopyOutput.model_validate(
            {
                "hook": _designed("A2").hook.model_dump(),
                "captions": [
                    {
                        "window_id": short["id"],
                        "heading": "Retro vagonlar",
                        "text": "Ahşap banklar",
                        "evidence_ids": ["fact-11"],
                        "visual_quote": short["seen"][0]["text"][:16],
                    }
                ],
            }
        )
        designed_operations(written, _evidence(), snapshot, _request(), text_design)


def test_a_spoken_edit_without_speech_timing_is_refused_under_a_design_as_in_legacy(monkeypatch):
    """Footage windows of a spoken edit would cover her recorded words (KRI-816)."""
    snapshot = {**_snapshot(), "spoken_edit": True}
    assert design_opening(snapshot)[0] is None
    for text_design in ("B1", "A2"):
        _stub_agents(monkeypatch, [_designed(text_design).model_dump()])
        with pytest.raises(ResearchUnavailable, match="no speech timing"):
            run_researched_revision(
                ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
                utterance=_request(),
                snapshot=snapshot,
                text_design=text_design,
            )


def _her_text_in_the_first_clip() -> dict:
    snapshot = _snapshot()
    snapshot["text_bars"] = [{"id": "her", "text": "Kyoto", "start_s": 5.0, "end_s": 8.0}]
    return snapshot


def test_her_text_in_the_first_clip_keeps_the_clip_and_the_guide_still_fills(monkeypatch):
    """Placement protects the whole first clip once her text is on it, so no design offers it."""
    snapshot = _her_text_in_the_first_clip()
    first_end = snapshot["slots"][0]["output_end_s"]
    assert design_opening(snapshot)[0] is not None  # her text starts after the hook window
    for text_design in ("A1", "A2", "A3", "B1"):
        assert min(row["start_s"] for row in design_windows(snapshot, text_design)) >= first_end
    calls = _stub_agents(monkeypatch, [])
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", _fill_every_window(calls))
    output = run_researched_revision(
        ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
        utterance=_request(),
        snapshot=snapshot,
        text_design="B1",
    )
    assert len(calls["writer"]) == 1
    assert not [op for op in output.ops if op["op"] == "remove_text"]
    assert uncovered_design_windows(output.ops, snapshot) == []


def _fit(text: str, limit: int) -> str:
    """The line cut to the window at a word boundary, as a writer shortens it."""
    return text if len(text) <= limit else text[: limit + 1].rsplit(" ", 1)[0].rstrip(",")


def _fill_every_window(calls: dict):
    """A writer that answers every offered window, as the B1 prompt asks."""
    lines = [(c.text, c.evidence_ids) for c in _designed("B1").captions]
    pairs = [(c.heading, c.text, c.evidence_ids) for c in _designed("A2").captions]

    def write(self, input, *, ctx=None):
        calls["writer"].append(input)
        windows = input.snapshot["research_windows"]
        guide = input.text_design == "B1"
        captions = []
        for index, window in enumerate(windows):
            heading, text, ids = (
                ("", *lines[index % len(lines)]) if guide else pairs[index % len(pairs)]
            )
            captions.append(
                {
                    "window_id": window["id"],
                    "heading": heading,
                    "text": _fit(text, window["max_characters"]),
                    "evidence_ids": ids,
                    "visual_quote": window["seen"][0]["text"][:16],
                }
            )
        hook = _designed("B1").hook.model_dump() if input.snapshot.get("hook_window") else None
        return self.parse(json.dumps({"hook": hook, "captions": captions}), input)

    return write


def _stub_agents(monkeypatch, answers: list[dict]) -> dict:
    calls = {"writer": [], "review": []}
    queue = list(answers)

    def write(self, input, *, ctx=None):
        calls["writer"].append(input)
        return self.parse(json.dumps(queue.pop(0)), input)

    def review(self, input, *, ctx=None):
        calls["review"].append(input)
        return ResearchReviewOutput(approved=True, reason="Supported")

    monkeypatch.setattr(researched_text.GroundedResearchAgent, "run", lambda *a, **k: _evidence())
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", write)
    monkeypatch.setattr(researched_text.ResearchReviewAgent, "run", review)
    return calls


def _run(text_design: str):
    return run_researched_revision(
        ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
        utterance=_request(),
        snapshot=_snapshot(),
        text_design=text_design,
    )


def _partial(keep: int) -> tuple[dict, set[str]]:
    full = _designed("B1").model_dump()
    kept = full["captions"][:keep]
    bare = {row["id"] for row in design_windows(_snapshot(), "B1")} - {c["window_id"] for c in kept}
    return {**full, "captions": kept}, bare


def test_b1_correction_names_the_bare_clips_then_accepts_the_full_guide(monkeypatch):
    partial, bare = _partial(3)
    calls = _stub_agents(monkeypatch, [partial, _designed("B1").model_dump()])
    output = _run("B1")
    assert bare and all(window in calls["writer"][1].correction for window in bare)
    assert not output.unmet_requests
    lines = [op for op in output.ops if op["research"]["design"]["level"] == "line"]
    assert len(lines) == len(design_windows(_snapshot(), "B1"))


def test_b1_still_bare_after_correction_renders_its_lines_and_says_so(monkeypatch):
    partial, bare = _partial(3)
    _stub_agents(monkeypatch, [partial, partial])
    output = _run("B1")
    assert len(output.ops) == 1 + 3
    assert not output.unmet_requests  # an unmet request would become a question for her
    assert f"No sourced line for {len(bare)} of the clips" in output.reply_notes


# -- 5/7. the target shapes, through the real ops, the resolver and approval -------------------


def _rows(ops: list[dict]) -> list[dict]:
    return json.loads(json.dumps(apply_text_lane_ops([], _snapshot()["slots"], ops).text))


@pytest.mark.parametrize("text_design", ["B1", "A2"])
def test_kyoto_design_copy_opens_with_the_hook_then_grounded_facts(monkeypatch, text_design):
    calls = _stub_agents(monkeypatch, [_designed(text_design).model_dump()])
    rows = _rows(_run(text_design).ops)
    assert calls["review"][0].designed is not None

    with informative_text_scope(True):
        roles = [text_role(row) for row in rows]
    assert roles[0] == "hook" and set(roles[1:]) == {"research"}
    hook, facts = rows[0], rows[1:]
    assert (hook["start_s"], hook["end_s"]) == (0.0, 3.4) and hook["text"]
    assert min(row["start_s"] for row in facts) >= 4.0
    assert not [row["text"] for row in rows if _LABEL.match(row["text"])]
    every_fact = [fact.id for fact in _evidence().facts]
    assert [f["id"] for f in hook["source_params"]["research"]["evidence_facts"]] == every_fact
    assert not [row for row in facts if "evidence_facts" in row["source_params"]["research"]]
    for row in rows:
        research = row["source_params"]["research"]
        assert research["design"]["id"] == text_design
        cited = [
            fact for fact in _evidence().facts if fact.id in {f["id"] for f in research["facts"]}
        ]
        # Every capitalised word, the opener included: only the copy's framing words are free.
        # Read with a plain substring oracle, not the server's own check.
        cited_text = loose_text(" ".join(fact.text for fact in cited))
        cited_words = set(re.findall(r"\w+", cited_text))
        names = {
            re.split(r"['’]", word)[0]
            for word in re.findall(r"[^\W\d_][\w'’]*", row["text"])
            if word[:1].isupper()
        }
        if _level(row) in {"line", "detail"}:
            # The contract leaves a line's plain sentence opener to the writer ("Trende beş
            # vagon var"): one with no apostrophe and no capitalised word after it.
            names -= {
                match.group(1)
                for match in re.finditer(
                    r"(?:^|[.!?;]\s+)([^\W\d_]+)(?=\s+[^\W\d_A-ZÇĞİÖŞÜ]|,|$)", row["text"]
                )
            }
        unstated = {
            name
            for name in names - _FRAMING_OPENERS
            if len(name) >= 3 and not _oracle_grounded(loose_text(name), cited_text, cited_words)
        }
        assert not unstated, (row["text"], unstated)
        numbers = {n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)*", row["text"])}
        stated = {n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)*", cited_text)}
        assert numbers <= stated, row["text"]

    if text_design == "B1":
        assert {_level(row) for row in facts} == {"line"}
        clips = [slot["media_id"] for slot in _snapshot()["slots"]]
        assert [row["source_params"]["research"]["media_ids"] for row in facts] == [
            [clip] for clip in clips
        ]
    else:
        for heading, detail in zip(facts[::2], facts[1::2], strict=True):
            assert (_level(heading), _level(detail)) == ("heading", "detail")
            assert (
                heading["source_params"]["research"]["design"]["pair"]
                == (detail["source_params"]["research"]["design"]["pair"])
            )
            assert (heading["start_s"], heading["end_s"]) == (detail["start_s"], detail["end_s"])


@pytest.mark.parametrize("text_design", ["B1", "A2"])
def test_kyoto_design_copy_survives_approval_unchanged(text_design):
    ops, missing = designed_operations(
        _designed(text_design), _evidence(), _snapshot(), _request(), text_design
    )
    assert missing == []
    prepared = PreparedResearch(
        status="ready",
        requirement_id="r1",
        topic="Kyoto'daki Toroko treni",
        request_quote="bilgilendirici alt yazı",
        basis_digest="0" * 64,
        texts=prepared_research_texts(ops),
        retrieved_at=_evidence().retrieved_at,
        search_entry_html=_evidence().search_entry_html,
        evidence_facts=prepared_evidence_facts(ops),
    )
    approved = PreparedResearch.model_validate_json(prepared.model_dump_json())
    assert pinned_research_operations(approved, _snapshot()) == ops


def test_a2_pairs_and_the_hook_style_apart_under_the_stamped_policy():
    """PR1's resolver places each heading and its detail as two rows that never collide.

    Drawing the pair as one stack (heading over detail, the design's fonts) is KRI-1581.
    """
    ops, _missing = designed_operations(_designed("A2"), _evidence(), _snapshot(), _request(), "A2")
    with phone_text_policy_scope(_policy()):
        styled = style_rows(_rows(ops), _policy())

    def overlap(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    pairs: dict[int, list[tuple]] = {}
    for row in styled:
        pair = row["source_params"]["research"]["design"].get("pair")
        if pair is not None:
            pairs.setdefault(pair, []).append(measure_box(row))
    assert pairs and all(len(boxes) == 2 for boxes in pairs.values())
    assert not [pair for pair, boxes in pairs.items() if overlap(*boxes)]


def test_existing_per_clip_text_is_replaced_by_the_guide_not_refused():
    """Clip-aligned text runs 0.12 s past each trimmed design window; it is replaced."""
    snapshot = _snapshot()
    snapshot["text_bars"] = [
        {
            "id": f"label-{i}",
            "text": "x",
            "start_s": s["output_start_s"],
            "end_s": s["output_end_s"],
        }
        for i, s in enumerate(snapshot["slots"])
    ]
    assert design_opening(snapshot)[0] is None  # her text opens the video: no hook
    written = _designed("B1").model_copy(deep=True)
    written.captions = written.captions[1:]
    for caption, window in zip(written.captions, design_windows(snapshot, "B1"), strict=False):
        caption.window_id = window["id"]
    ops, missing = designed_operations(written, _evidence(), snapshot, _request(), "B1")
    removed = [op["bar_index"] for op in ops if op["op"] == "remove_text"]
    assert sorted(removed) == list(range(1, 7)) and missing == []


# -- 8. call sites ---------------------------------------------------------------------------


def test_kyoto_subtitle_ask_defaults_to_b1_and_her_choice_wins():
    assert research_text_design(None, _ASK) == "B1"
    assert research_text_design("A3", _ASK) == "A3"
    assert research_text_design(None, "Bu tren hakkında bilgi ver") == "A2"


@pytest.mark.parametrize(
    ("request_text", "design"),
    [
        ("Add informative subtitles about this train", "B1"),
        ("altyazı istemiyorum, sadece bilgi ver", "A2"),
        ("Add facts about this train without subtitles", "A2"),
        ("Informative Untertitel zum Zug", "B1"),
        ("Fakten zum Zug, keine Untertitel", "A2"),
        ("Ich will keinen Untertitel, nur Fakten", "A2"),
    ],
)
def test_subtitle_default_reads_the_whole_request_and_its_negations(request_text, design):
    assert research_text_design(None, request_text) == design


def _plan(clips: int = 3, target_s: float = 24):
    seen = [slot["seen"]["text"] for slot in _attempt()["input"]["snapshot"]["slots"][:3]]
    return plan_unified_montage(
        [
            UnifiedClip(
                media_id=f"kyoto-{index}",
                proxy_path=f"private/kyoto-{index}.mp4",
                generation="1",
                duration_s=9,
                analysis={"understanding": {"summary": seen[index % len(seen)]}},
            )
            for index in range(clips)
        ],
        BriefView(target_duration_s=target_s),
    )


def _route_and_write(monkeypatch, *, lines_per_answer: int | None = None) -> dict:
    """The composer asks for research; the writer answers each design window it is offered."""
    seen = {"writer": []}
    lines = [(c.text, c.evidence_ids) for c in _designed("B1").captions]

    def route(self, input, *, ctx=None):
        ask = {"research_request": {"topic": "Kyoto'daki Toroko treni", "request_quote": _ASK}}
        return self.parse(json.dumps(ask), input)

    def write(self, input, *, ctx=None):
        seen["writer"].append(input)
        if input.text_design is None:
            return ResearchCopyOutput(captions=[])
        windows = input.snapshot["research_windows"][:lines_per_answer]
        answer = {
            "hook": _designed("B1").hook.model_dump(),
            "captions": [
                {
                    "window_id": window["id"],
                    "text": _fit(lines[index % len(lines)][0], window["max_characters"]),
                    "evidence_ids": lines[index % len(lines)][1],
                    "visual_quote": window["seen"][0]["text"][:16],
                }
                for index, window in enumerate(windows)
            ],
        }
        return self.parse(json.dumps(answer), input)

    monkeypatch.setattr(CreationTextComposer, "run", route)
    monkeypatch.setattr(researched_text.GroundedResearchAgent, "run", lambda *a, **k: _evidence())
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", write)
    monkeypatch.setattr(
        researched_text.ResearchReviewAgent,
        "run",
        lambda *a, **k: ResearchReviewOutput(approved=True, reason="Supported"),
    )
    return seen


def _compiled_rows(snapshot) -> list[dict]:
    restored = EditProposalSnapshot.model_validate_json(snapshot.model_dump_json())
    return compile_proposal_execution_plan(restored)["text_elements"]


def test_guided_composition_before_approval_writes_the_design_when_the_flag_is_on(monkeypatch):
    seen = _route_and_write(monkeypatch)
    drops: list = []
    composed = compose_creation_text(
        _plan().snapshot, creator_request=_ASK, informative_text_designs=True, drops=drops
    )
    rows = _compiled_rows(composed)
    assert seen["writer"][0].text_design == "B1"
    assert rows[0]["source_params"]["phone_text_role"] == "hook"
    lines = [row for row in rows if _level(row) == "line"]
    assert lines and min(row["start_s"] for row in lines) >= 4.0
    assert not [row["text"] for row in rows if _LABEL.match(row["text"])]
    assert not drops


def test_a_render_without_a_stamp_stays_legacy(monkeypatch):
    """No phone policy (a cloud render, an unstamped job): the flag alone never reshapes it."""
    seen = _route_and_write(monkeypatch)
    monkeypatch.setattr("app.config.settings.informative_text_designs_enabled", True)
    with pytest.raises(creation_text_composition.CreationTextCompositionError):
        compose_creation_text(_plan().snapshot, creator_request=_ASK, drops=[])
    assert seen["writer"][0].text_design is None


def test_a_bare_clip_after_correction_renders_with_a_note_not_a_question(monkeypatch):
    _route_and_write(monkeypatch, lines_per_answer=1)
    drops: list = []
    composed = compose_creation_text(
        _plan().snapshot, creator_request=_ASK, informative_text_designs=True, drops=drops
    )
    assert [_level(row) for row in _compiled_rows(composed)] == ["hook", "line"]
    assert drops and {drop.reason for drop in drops} == {"bare_clip"}


_RESEARCH_ASK = BriefRequirement(
    id="r0",
    kind="text",
    scope="global",
    description="Informative subtitles about the Torokko train",
    facts={"research": {"topic": "Kyoto'daki Toroko treni", "request_quote": _ASK}},
)


def _preview(monkeypatch, plan) -> PreparedResearch:
    monkeypatch.setattr(phone_montage_preflight, "preflight_montage_plan", lambda **_: (plan, {}))
    return research_preview.prepare(
        research_preview.ResearchInputs(
            creator_id="creator-1",
            thread_id="thread-1",
            item_id="item-1",
            edit_format="montage",
            brief=CreativeBrief(requirements=[_RESEARCH_ASK]),
            strategy={},
            media_snapshot={},
            creator_request=_ASK,
        ),
        visuals=[],
    )


def test_before_approval_a_job_the_approval_will_not_stamp_gets_legacy_research(monkeypatch):
    """Phone text styling off: the approval stamps nothing, so the render would be legacy."""
    seen = _route_and_write(monkeypatch)
    monkeypatch.setattr("app.config.settings.informative_text_designs_enabled", True)
    monkeypatch.setattr("app.config.settings.phone_text_styling_enabled", False)
    _preview(monkeypatch, _plan())
    with pytest.raises(creation_text_composition.CreationTextCompositionError):
        compose_creation_text(
            _plan().snapshot,
            creator_request=_ASK,
            informative_text_designs=researched_text.approval_text_designs(),
            drops=[],
        )
    assert seen["writer"] and {call.text_design for call in seen["writer"]} == {None}


def test_preview_pins_the_design_and_the_stamped_render_replays_it(monkeypatch):
    ask = _RESEARCH_ASK
    plan = _plan()
    _route_and_write(monkeypatch)
    monkeypatch.setattr("app.config.settings.informative_text_designs_enabled", True)
    monkeypatch.setattr("app.config.settings.phone_text_styling_enabled", True)
    prepared = _preview(monkeypatch, plan)
    assert prepared.status == "ready"
    assert prepared.texts[0].design.level == "hook"
    assert [f.id for f in prepared.evidence_facts] == [f.id for f in _evidence().facts]

    for agent in ("GroundedResearchAgent", "ResearchCopyAgent", "ResearchReviewAgent"):
        monkeypatch.setattr(
            getattr(researched_text, agent),
            "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("approved research re-ran")),
        )
    approved = PreparedResearch.model_validate_json(prepared.model_dump_json())
    with phone_text_policy_scope(_policy()):
        composed = compose_creation_text(
            plan.snapshot, creator_request=_ASK, text_requirements=[ask], prepared_research=approved
        )
    rows = _compiled_rows(composed)
    assert [row["text"] for row in rows] == [text.text for text in prepared.texts]
    assert rows[0]["source_params"]["phone_text_role"] == "hook"


def _guided_clips(writer_input) -> set[str]:
    """Clips a running guide must reach: every one the writer saw still on screen after the hook."""
    slots = writer_input.snapshot["slots"]
    assert len(slots) > 12
    return {str(slot["media_id"]) for slot in slots if slot["output_end_s"] > 4.0}


@pytest.mark.parametrize("clips", [13, 18])
def test_a_guide_on_more_clips_than_approval_holds_still_covers_every_clip(monkeypatch, clips):
    """Preview, its approved replay, and a stamped render's fresh research all fit and cover."""
    plan = _plan(clips, target_s=3 * clips)
    seen = _route_and_write(monkeypatch)
    monkeypatch.setattr("app.config.settings.informative_text_designs_enabled", True)
    monkeypatch.setattr("app.config.settings.phone_text_styling_enabled", True)
    prepared = _preview(monkeypatch, plan)
    assert prepared.status == "ready", prepared.reason
    lines = [text for text in prepared.texts if text.design.level == "line"]
    clips = _guided_clips(seen["writer"][-1])
    assert {media for text in lines for media in text.media_ids} == clips

    def covered(rows):
        return {
            media
            for row in rows
            if _level(row) == "line"
            for media in row["source_params"]["research"]["media_ids"]
        }

    approved = PreparedResearch.model_validate_json(prepared.model_dump_json())
    with phone_text_policy_scope(_policy()):
        replayed = _compiled_rows(
            compose_creation_text(
                plan.snapshot,
                creator_request=_ASK,
                text_requirements=[_RESEARCH_ASK],
                prepared_research=approved,
            )
        )
    assert [row["text"] for row in replayed] == [text.text for text in prepared.texts]
    assert covered(replayed) == clips

    drops: list = []

    # The phone writers' own scope (`_run_phone_unified_montage_job` is wrapped by it): the
    # job's stamp, not an approval-time argument, decides the design.
    @phone_text_generation
    def render(job_id, snapshot):
        return _compiled_rows(
            compose_creation_text(plan.snapshot, creator_request=_ASK, drops=drops)
        )

    rows = render("job-1", {PHONE_TEXT_STYLE_FIELD: _policy()})
    assert seen["writer"][-1].text_design == "B1"
    assert covered(rows) == _guided_clips(seen["writer"][-1]) == clips and not drops
    assert generative_build._run_phone_unified_montage_job.__wrapped__ is not None


def test_more_replaced_text_than_one_edit_holds_is_refused_not_a_crash(monkeypatch):
    """Twelve lines that each replace her text on their clip are 24 ops, over the editor's 16."""
    snapshot = _snapshot()
    base = snapshot["slots"]
    snapshot["slots"] = [
        {
            **base[index % len(base)],
            "media_id": f"clip-{index}",
            "output_start_s": 3.0 * index,
            "output_end_s": 3.0 * index + 3.0,
        }
        for index in range(13)
    ]
    snapshot["total_duration_s"] = 39.0
    snapshot["story_beat_windows"] = []
    snapshot["text_bars"] = [
        {"id": f"her-{index}", "text": "x", "start_s": 3.0 * index, "end_s": 3.0 * index + 3.0}
        for index in range(13)
    ]
    calls = _stub_agents(monkeypatch, [])
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", _fill_every_window(calls))
    with pytest.raises(ResearchUnavailable, match="at most 16"):
        run_researched_revision(
            ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
            utterance=_request(),
            snapshot=snapshot,
            text_design="B1",
        )
    assert len(calls["writer"]) == 2


def test_a_titled_design_filling_every_window_is_corrected_to_what_approval_holds(monkeypatch):
    """Kyoto has 7 windows; a hook and 7 heading/detail pairs would be 15 approved texts."""
    calls = _stub_agents(monkeypatch, [])
    over = _fill_every_window(calls)
    answers = iter([over, None])

    def write(self, input, *, ctx=None):
        step = next(answers)
        if step is None:
            calls["writer"].append(input)
            return self.parse(_designed("A2").model_dump_json(), input)
        return step(self, input, ctx=ctx)

    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", write)
    output = _run("A2")
    assert len(calls["writer"]) == 2 and "at most" in calls["writer"][1].correction
    assert len(prepared_research_texts(output.ops)) <= 12

    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", over)
    with pytest.raises(ResearchUnavailable):
        _run("A2")


def test_follow_up_chat_keeps_the_jobs_design_through_the_editor_projection():
    """The planner's chat target is `project_editor_draft` of the job's variant."""
    ops, _missing = designed_operations(_designed("A2"), _evidence(), _snapshot(), _request(), "A2")
    variant = {**_load("plan.json")["variant"], "phone_text_style": _policy()}
    variant["text_elements"] = _rows(ops)
    projected = project_editor_draft(variant, {})
    assert chat_text_design(projected, _ASK) == "A2"  # her design, not the subtitle default
    projected["text_elements"] = []
    assert chat_text_design(projected, _ASK) == "B1"
    unstamped = {**projected, "phone_text_style": _load("plan.json")["variant"]["phone_text_style"]}
    assert chat_text_design(unstamped, _ASK) is None
    # A variant without its own copy of the stamp reads the job's, as a Save does.
    bare = {key: value for key, value in variant.items() if key != "phone_text_style"}
    assert chat_text_design(project_editor_draft(bare, {}), _ASK, job_policy=_policy()) == "A2"
    assert chat_text_design(project_editor_draft(bare, {}), _ASK, job_policy=None) is None


# -- 11. what the writer is offered, placement accepts -------------------------------------


def _editor_snapshot(monkeypatch, text_elements: list[dict]) -> tuple[dict, dict]:
    """The copilot's real snapshot of the stamped Kyoto montage (`build_editor_snapshot`)."""
    from types import SimpleNamespace  # noqa: PLC0415

    from app.services import kria_editor_ops  # noqa: PLC0415

    binding = _load("plan.json")["approved_proposal"]["text_composition"]["operations"][0][
        "research"
    ]["timeline_binding"]
    kyoto = _attempt()["input"]["snapshot"]["slots"]
    variant = {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "g1",
        "resolved_archetype": "montage",
        "phone_text_style": _policy(),
        "text_elements": text_elements,
        "ai_timeline": {
            "beat_grid": [],
            "slots": [
                {
                    "slot_id": f"slot-{i}",
                    "clip_index": cut["clip_index"],
                    "source_gcs_path": f"users/u/{i}.mp4",
                    "source_duration_s": 30.0,
                    "in_s": cut["in_s"],
                    "duration_beats": None,
                    "duration_s": cut["duration_s"],
                    "removed": False,
                    "transition_after": cut.get("transition_after") or "cut",
                    "transition_duration_s": cut.get("transition_duration_s"),
                }
                for i, cut in enumerate(binding)
            ],
        },
    }
    job = SimpleNamespace(
        assembly_plan={"variants": [variant], "phone_text_style": _policy()},
        all_candidates={"clip_paths": [f"users/u/{i}.mp4" for i in range(len(binding))]},
    )
    monkeypatch.setattr(
        kria_editor_ops,
        "_editor_capabilities",
        lambda _job, _variant: {"text_elements": True, "timeline": True},
    )
    snapshot = kria_editor_ops.build_editor_snapshot(job, variant)
    # The planner's clip context: what each slot shows, as research reads it.
    for slot, shown in zip(snapshot["slots"], kyoto, strict=True):
        for key in ("seen", "visible_text", "media_id", "output_start_s", "output_end_s"):
            slot[key] = shown.get(key, "")
    snapshot["total_duration_s"] = kyoto[-1]["output_end_s"]
    return snapshot, variant


def test_a_deleted_generated_opening_still_gets_the_hook_and_keeps_its_tombstone(monkeypatch):
    """A Save tombstones the generated title she deleted (`_tombstone_for`); it is not her text."""
    from app.agents._schemas.text_element import TextElement, _tombstone_for  # noqa: PLC0415

    title = TextElement.model_validate(
        {
            "id": "intro-1",
            "text": "Kyoto",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
            "source_params": {"identity": "intro:0"},
        }
    )
    tombstone = _tombstone_for(title)
    snapshot, variant = _editor_snapshot(monkeypatch, [tombstone])
    removed = {i for i, bar in enumerate(snapshot["text_bars"]) if bar.get("removed")}
    assert removed  # the tombstone reaches the copilot's snapshot
    design = chat_text_design(variant, _ASK, job_policy=_policy())
    assert design == "B1" and design_opening(snapshot)[0] is not None
    calls = _stub_agents(monkeypatch, [])
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", _fill_every_window(calls))
    output = run_researched_revision(
        ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
        utterance=_request(),
        snapshot=snapshot,
        text_design=design,
    )
    assert len(calls["writer"]) == 1
    assert output.ops[0]["research"]["design"]["level"] == "hook"
    # Removing the tombstone would let the deleted title project back.
    assert not [op for op in output.ops if op["op"] == "remove_text" and op["bar_index"] in removed]
    assert uncovered_design_windows(output.ops, snapshot) == []
    rows = apply_text_lane_ops([tombstone], snapshot["slots"], output.ops).text
    assert [row["id"] for row in rows if row.get("removed")] == [tombstone["id"]]
    assert (rows[1]["start_s"], rows[1]["end_s"]) == (0.0, 3.4) and rows[1]["text"]
    prepared = PreparedResearch(
        status="ready",
        requirement_id="r1",
        topic="Kyoto'daki Toroko treni",
        request_quote="bilgilendirici alt yazı",
        basis_digest="0" * 64,
        texts=prepared_research_texts(output.ops),
        retrieved_at=_evidence().retrieved_at,
        search_entry_html=_evidence().search_entry_html,
        evidence_facts=prepared_evidence_facts(output.ops),
    )
    assert pinned_research_operations(prepared, snapshot) == output.ops


def test_a_spoken_guide_is_offered_only_the_pauses_its_rows_hold(monkeypatch):
    """A spoken edit's pauses cannot be joined across her words, so the guide gets fewer."""
    seen = _attempt()["input"]["snapshot"]["slots"][0]["seen"]
    cues = [
        {"id": f"c{i}", "text": f"line {i}", "start_s": 4.5 * i, "end_s": 4.5 * i + 1.5}
        for i in range(14)
    ]
    snapshot = {
        "spoken_edit": True,
        "slots": [
            {
                "media_id": "talk",
                "clip_index": 0,
                "in_s": 0,
                "duration_s": 63.0,
                "output_start_s": 0.0,
                "output_end_s": 63.0,
                "seen": seen,
            }
        ],
        "captions": {"cues": cues},
        "text_bars": [],
        "total_duration_s": 63.0,
    }
    assert len(research_windows(snapshot)) > 12
    calls = _stub_agents(monkeypatch, [])
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", _fill_every_window(calls))
    output = run_researched_revision(
        ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
        utterance=_request(),
        snapshot=snapshot,
        text_design="B1",
    )
    assert len(calls["writer"]) == 1
    offered = calls["writer"][0].snapshot["research_windows"]
    assert len(offered) <= calls["writer"][0].snapshot["max_captions"] <= 12
    assert "No sourced line" not in output.reply_notes
    assert uncovered_design_windows(output.ops, snapshot) == []


# -- 12. live answers the checks refused, and the limits a design row must keep ---------------


@pytest.mark.parametrize("text_design", ["A2", "B1"])
@pytest.mark.parametrize(
    ("hook", "ids"),
    [
        ("İşte Kyoto'nun en güzel 25 dakikası", ["fact-1", "fact-6"]),
        ("This Kyoto ride takes 25 minutes", ["fact-1", "fact-6"]),
    ],
)
def test_a_framing_opener_before_a_grounded_name_passes_in_a_hook(text_design, hook, ids):
    """Live re-record (2.33): 2 of 3 first answers opened "İşte Kyoto'nun ..."."""
    written = DesignedCopyOutput.model_validate(_opened(text_design, hook, _FACT6_LINE, ids))
    ops, _missing = designed_operations(written, _evidence(), _snapshot(), _request(), text_design)
    assert ops[0]["text"] == hook


@pytest.mark.parametrize(
    ("text_design", "line"),
    [
        ("B1", "İşte Hozukyo Geçidi, 25 dakika"),
        ("B1", "The Hozukyo gorge, 25 minutes"),
        ("A2", "İşte Hozukyo Geçidi, 25 dakika"),
        ("A2", "The Hozukyo gorge, 25 minutes"),
        # Live A2 r1, first answer: "Burası" ("this place") opens a line naming its fact.
        ("A2", "Burası Hozukyo Geçidi, 25 dakika"),
        ("B1", "Burası Hozukyo Geçidi, 25 dakika"),
    ],
)
def test_a_framing_opener_before_a_grounded_name_passes_in_a_line(text_design, line):
    written = DesignedCopyOutput.model_validate(_opened(text_design, "Her mevsim başka", line))
    ops, _missing = designed_operations(written, _evidence(), _snapshot(), _request(), text_design)
    assert ops[-1]["text"] == line


@pytest.mark.parametrize("text_design", ["A2", "B1"])
@pytest.mark.parametrize(
    ("hook", "line"),
    [
        ("İşte Nara'nın en güzel 25 dakikası", _FACT6_LINE),
        ("Her mevsim başka", "İşte Sagano Geçidi, 25 dakika"),
        ("This Nara ride takes 25 minutes", _FACT6_LINE),
    ],
)
def test_the_name_after_a_framing_opener_is_still_checked(text_design, hook, line):
    unstated = _reject(_opened(text_design, hook, line), text_design).split("do not: ")[1]
    assert unstated in {"Nara", "Sagano"}


@pytest.mark.parametrize(
    ("heading", "text", "ids", "text_design"),
    [
        ("Trenin retro tasarımı", "Ahşap banklar", ["fact-11"], "A2"),
        ("Trenimiz", "Ahşap banklı vagonlar", ["fact-11"], "A2"),
        ("", "Trenimiz Hozukyo Geçidi boyunca kıvrılıyor", ["fact-6"], "B1"),
    ],
)
def test_a_turkish_suffix_on_the_facts_word_still_grounds_it(heading, text, ids, text_design):
    """Live re-record (2.34): fact-11 says "Tren"; "Trenin" ("the train's") is that word."""
    written = _one(heading, text, ids)
    ops, _missing = designed_operations(
        DesignedCopyOutput.model_validate(written),
        _evidence(),
        _snapshot(),
        _request(),
        text_design,
    )
    assert ops[-1]["text"] == text


@pytest.mark.parametrize(
    ("heading", "ids", "name"),
    [("Sagano treni", ["fact-6"], "Sagano"), ("Sagaon treni", ["fact-6"], "Sagaon")],
)
def test_a_longer_name_sharing_a_short_facts_word_is_still_rejected(heading, ids, name):
    assert name in _reject(_one(heading, "Ahşap banklar", ids), "A2")


def test_a_number_inside_a_decimal_its_fact_states_is_not_grounded():
    """fact-6 states 7.3 km; "3 km" is a different number, "7,3 km" the same one."""
    assert "3" in _reject(_one("Ray boyunca", "Ray boyunca 3 km", ["fact-6"]), "A2")
    assert "3" in _reject(_one("3 km", "Saga'dan Kameoka'ya", ["fact-6"]), "A2")
    written = DesignedCopyOutput.model_validate(_one("7,3 km", "Saga'dan Kameoka'ya", ["fact-6"]))
    designed_operations(written, _evidence(), _snapshot(), _request(), "A2")


def test_a_text_layer_under_a_design_reads_decimals_too():
    from app.services.researched_text import validate_research_layers  # noqa: PLC0415

    snapshot = {"slots": _snapshot()["slots"], "total_duration_s": 60.0}
    layer = ResearchedLayer(
        text="Ray boyunca 3 km",
        language="tr",
        position="bottom",
        start_s=4,
        end_s=9,
        evidence_ids=["fact-6"],
    )
    with pytest.raises(ResearchUnavailable, match="3"):
        validate_research_layers([layer], _evidence(), snapshot, _request(), text_design="A2")


def test_a_heading_and_its_detail_share_the_windows_reading_time():
    """15 characters a second is for what the window shows, heading and detail together."""
    window = design_windows(_snapshot(), "A2")[0]
    room = window["max_characters"]
    heading = "Retro vagonlar ahşap banklar"
    detail = ("Ahşap banklar, eski usul tasarım " * 4)[: room - len(heading) + 5].strip()
    assert len(heading) <= room and len(detail) <= room < len(heading) + len(detail)
    reason = _reject(_one(heading, detail, ["fact-11"]), "A2")
    assert str(room) in reason


@pytest.mark.parametrize("text_design", ["A1", "A2", "A3"])
def test_a_titled_design_never_shows_a_detail_without_its_heading(text_design):
    reason = _reject(_one("", "Ahşap banklar, eski usul tasarım", ["fact-11"]), text_design)
    assert "heading" in reason


_GERMAN_B1 = {
    "hook": {"text": "Die schönsten 25 Minuten am Fluss", "evidence_ids": ["fact-6"]},
    "captions": [
        ("window-0", "Der Zug fährt 7,3 km durch die Schlucht", ["fact-6"]),
        ("window-1", "Fünf Wagen, einer davon ganz offen", ["fact-12"]),
        ("window-2", "Holzbänke im Retro-Design der Wagen", ["fact-11"]),
        ("window-3", "Die Strecke folgt dem Fluss durch die Schlucht", ["fact-6"]),
        ("window-4", "Die Landschaft wechselt mit der Jahreszeit", ["fact-8"]),
        ("window-5", "Mitten in der Schlucht, 25 Minuten Fahrt", ["fact-6"]),
        ("window-6", "Im Frühling Kirschblüten, im Herbst rotes Laub", ["fact-8"]),
    ],
}


def _german(**replace: str) -> dict:
    return {
        "hook": _GERMAN_B1["hook"],
        "captions": [
            {
                "window_id": window,
                "text": replace.get(window, text),
                "evidence_ids": ids,
                "visual_quote": _quote(window),
            }
            for window, text, ids in _GERMAN_B1["captions"]
        ],
    }


def test_german_nouns_are_not_names_but_german_numbers_are_still_checked():
    """German capitalises every noun ("Zug", "Schlucht"), so capitals do not mark a name."""
    written = DesignedCopyOutput.model_validate(_german())
    ops, missing = designed_operations(written, _evidence(), _snapshot(), _request(), "B1")
    assert missing == [] and ops[0]["text"] == _GERMAN_B1["hook"]["text"]
    reason = _reject(_german(**{"window-0": "Der Zug fährt 3 km durch die Schlucht"}), "B1")
    assert "3" in reason


def test_a_partly_bare_guide_names_the_bare_clips_on_the_card_and_after_the_render(monkeypatch):
    """Not "I couldn't do <her whole ask>": the lines render, only some clips stayed bare."""
    from app.services.creation_text_degrade import (  # noqa: PLC0415
        TEXT_DROPS_FIELD,
        finished_render_notes,
        text_drop_notes,
        text_drops_record,
    )

    seen = _route_and_write(monkeypatch, lines_per_answer=1)
    drops: list = []
    compose_creation_text(
        _plan().snapshot, creator_request=_ASK, informative_text_designs=True, drops=drops
    )
    bare = len(seen["writer"][-1].snapshot["research_windows"]) - 1
    note = [f"No sourced line for {bare} of the clips."]
    assert bare > 0 and text_drop_notes(drops) == note
    reloaded = {"unified_montage": {TEXT_DROPS_FIELD: text_drops_record("g1", drops)}}
    assert finished_render_notes(reloaded, {"g1"}) == note


def test_a_full_guide_with_a_full_fact_set_fits_the_editor_state(monkeypatch):
    """Twelve rows each carrying 30 facts would push the text lane past the turn's 256 KB."""
    from app.kria.api_schemas import EditorStateIn  # noqa: PLC0415

    base = _evidence()
    facts = [
        fact.model_copy(update={"id": f"{fact.id}-{copy}" if copy else fact.id})
        for copy in range(5)
        for fact in base.facts
    ]
    evidence = base.model_copy(update={"facts": facts})
    snapshot = _snapshot()
    slots = snapshot["slots"]
    snapshot["slots"] = [
        {
            **slots[index % len(slots)],
            "media_id": f"clip-{index}",
            "output_start_s": 3.0 * index,
            "output_end_s": 3.0 * index + 3.0,
        }
        for index in range(13)
    ]
    snapshot["total_duration_s"] = 39.0
    snapshot["story_beat_windows"] = []
    calls = _stub_agents(monkeypatch, [])
    monkeypatch.setattr(researched_text.GroundedResearchAgent, "run", lambda *a, **k: evidence)
    monkeypatch.setattr(researched_text.ResearchCopyAgent, "run", _fill_every_window(calls))
    output = run_researched_revision(
        ResearchRequest.model_validate(_load("research.json")["public_facts"]["input"]),
        utterance=_request(),
        snapshot=snapshot,
        text_design="B1",
    )
    rows = json.loads(json.dumps(apply_text_lane_ops([], snapshot["slots"], output.ops).text))
    assert len(rows) == 12
    EditorStateIn.model_validate(
        {"base_generation": "g1", "client_state_id": "s1", "lanes": {"text_elements": rows}}
    )
    assert [fact.id for fact in prepared_evidence_facts(output.ops)] == [f.id for f in facts]
