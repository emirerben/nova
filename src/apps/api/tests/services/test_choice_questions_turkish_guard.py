"""KRI-630 guard: a Turkish chat never gets an English sentence from a choice question.

Failure modes written first:

* a builder gives ``UnresolvedChoice.intro`` / ``reason`` as a plain English string while
  the options are ``(en, tr)`` pairs; the renderer then glues Turkish connectors
  ("Maalesef ...") onto English text (the T1 incident);
* a new builder or kind is added later and nobody localizes it;
* an option label or description reaches the creator as raw English;
* the single-option statement text ("I can make it ...") stays English;
* a kind has no fixture here, so the sweep silently stops covering it.

Two nets: a runtime sweep that renders the real builders with ``reply_language="tr"`` and
fails on English function words, and a source check that every ``intro=`` / ``reason=``
handed to a choice builder goes through ``say()``.
"""

from __future__ import annotations

import ast
import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import app.services.choice_questions as cq
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from app.pipeline.caption_unclear import UnclearSpan
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_FREEZE_FRAME,
    CONFLICT_LENGTH_VS_NATURAL,
    CONFLICT_ORDER_BASIS,
    CONFLICT_PRIORITY,
    CONFLICT_SOURCE_COVERAGE,
    CONFLICT_TAKE_LINE_END,
    CONFLICT_TAKE_LINE_START,
    CONFLICT_UNCLEAR_WORD,
    CONFLICT_VOICE_VS_DURATION,
    CONFLICT_WHICH_VOICE,
    ChoiceCapability,
    build_choice_question,
    choice_question_text,
    collect_conflicts,
    unclear_word_choice,
)
from tests.kria.test_choice_voice_gate import VOICE, _voice_rows
from tests.kria.test_kri844_take_line_ask import CAP as _TAKE_CAP
from tests.kria.test_kri844_take_line_ask import SNAP as _TAKE_SNAP
from tests.kria.test_kri844_take_line_ask import STRATEGY as _TAKE_STRATEGY
from tests.kria.test_kri844_take_line_ask import brief as _take_brief

# Words only English uses. Quoted spans are the creator's own words and are skipped.
_ENGLISH = frozenset(
    """
    the your you is are was cannot can't but with without from that this will for and of
    to seconds second clips clip use used unfortunately choose add replace
    update leave fewer longer shorter window remaining recording footage selected
    """.split()
)
_QUOTED = re.compile(r"[\"“][^\"”]*[\"”]")
_WORD = re.compile(r"[a-zA-Z']+")


def _english_words(text: str) -> list[str]:
    bare = _QUOTED.sub(" ", text)
    return [w for w in _WORD.findall(bare) if w.lower() in _ENGLISH]


def _timing(seconds: float) -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="timing",
        scope="global",
        description=f"Keep {seconds:g} seconds",
        facts={"duration_s": seconds},
    )


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements))


def _dated(count: int, *, dated: bool = True) -> list[dict]:
    rows = []
    for i in range(count):
        row: dict = {"media_id": f"c{i:02d}", "kind": "video", "duration_s": 3.0}
        if dated:
            row["capture"] = {"capture_time": f"2026-09-20T09:{i % 60:02d}:00Z"}
        rows.append(row)
    return rows


_VOICE_STRATEGY = {
    "audio_strategy": "original_audio",
    "voice_mode": "continuous",
    "ordering_choice": "chronological",
    "montage_audio": {"preserve_source_audio": True, "source_media_ids": [VOICE]},
}
_VOICE_CAP = ChoiceCapability(voice_route=True)


def _coverage(code: str, **plan: object) -> dict:
    return {
        "length_plan": {
            "conflicts": [{"code": code}],
            "sources": [{"duration_s": 1.0}, {"duration_s": 1.0}],
            **plan,
        }
    }


def _cases() -> dict[str, tuple[str, list]]:
    """name -> (conflict kind, collected choices)."""

    cases: dict[str, tuple[str, list]] = {}

    def add(name: str, kind: str, *args: object, **kwargs: object) -> None:
        found = [c for c in collect_conflicts(*args, **kwargs) if c.kind == kind]  # type: ignore[arg-type]
        assert found, f"fixture {name} no longer produces a {kind} question"
        cases[name] = (kind, found)

    add(
        "order_basis",
        CONFLICT_ORDER_BASIS,
        {},
        _brief(
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description="in the order I filmed them",
                facts={"key": "capture_time"},
            )
        ),
        {"clip_assignments": _dated(4, dated=False)},
    )
    add(
        "duration_vs_count",
        CONFLICT_DURATION_VS_COUNT,
        {},
        _brief(_timing(15)),
        {"clip_assignments": _dated(166)},
    )
    add(
        "length_song",
        CONFLICT_LENGTH_VS_NATURAL,
        {"audio_strategy": "user_song"},
        _brief(_timing(20)),
        {"song_duration_s": 14},
    )
    add(
        "length_song_window",
        CONFLICT_LENGTH_VS_NATURAL,
        {"audio_strategy": "user_song"},
        _brief(_timing(20)),
        {"song_duration_s": 30, "song_trim_supported": True},
    )
    add(
        "length_take",
        CONFLICT_LENGTH_VS_NATURAL,
        {"audio_strategy": "original_audio", "edit_format": "talking_head"},
        _brief(_timing(30)),
        {"clip_assignments": [{"media_id": "take", "duration_s": 12}]},
    )
    add(
        "which_voice",
        CONFLICT_WHICH_VOICE,
        {**_VOICE_STRATEGY, "montage_audio": {"preserve_source_audio": True}},
        _brief(_timing(30)),
        {"clip_assignments": _voice_rows(voice_s=20, speech={"talk2": "second speaker here"})},
        _VOICE_CAP,
    )
    add(
        "voice_short",
        CONFLICT_VOICE_VS_DURATION,
        _VOICE_STRATEGY,
        _brief(_timing(30)),
        {"clip_assignments": _voice_rows(voice_s=14)},
        _VOICE_CAP,
    )
    add(
        "voice_implicit_length",
        CONFLICT_VOICE_VS_DURATION,
        _VOICE_STRATEGY,
        _brief(),
        {"clip_assignments": _voice_rows(voice_s=75)},
        _VOICE_CAP,
    )
    # KRI-825: timed photos the creator's total cannot fit (extendable, and not).
    for name, needed in (("holds_extend", 12.0), ("holds_keep_only", 400.0)):
        add(
            name,
            "holds_vs_length",
            {},
            _brief(_timing(5)),
            {
                "still_hold_conflict": {
                    "requested_s": 5.0,
                    "needed_s": needed,
                    "held_s": 6.0,
                    "requirement_ids": [],
                }
            },
        )
    # KRI-844: the quoted start / end line a Spanish take does not hold (a phone Talking edit).
    with ExitStack() as pilot:
        for name, value in (
            ("phone_rendering_enabled", True),
            ("phone_render_user_ids", []),
            ("subtitled_archetype_enabled", True),
        ):
            pilot.enter_context(patch.object(settings, name, value))
        for kind in (CONFLICT_TAKE_LINE_START, CONFLICT_TAKE_LINE_END):
            add(kind, kind, _TAKE_STRATEGY, _take_brief(), _TAKE_SNAP, _TAKE_CAP)
    for code, strategy in (
        ("single_hero_order_conflict", {}),
        ("insufficient_media", {}),
        ("source_count_limit", {}),
        ("cap_below_coverage", {}),
        ("no_usable_sources", {}),
        ("insufficient_capacity", {}),
        ("target_below_coverage", {"montage_cadence": "fast"}),
    ):
        plan: dict = {"max_source_count": 1} if code == "source_count_limit" else {}
        if code == "target_below_coverage":
            plan["target_duration_s"] = 4
        if code == "insufficient_capacity":
            plan.update(natural_owner="voiceover", natural_duration_s=40)
        add(
            f"coverage_{code}",
            CONFLICT_SOURCE_COVERAGE,
            strategy,
            _brief(),
            _coverage(code, **plan),
        )
    # KRI-849: the unclear-word question. Built from a span the way the detector hands it over
    # (the detector itself needs the phone-rendering settings; its end-to-end cases are in
    # tests/kria/test_kri849_dj_t1_unclear_word_replay.py). The two heard texts are the clip's own
    # words, not copy: no English function words in them, or they would sit in quotes.
    span = UnclearSpan(
        8.8, 9.7, "salma y limón", "sal marina", 0, 2, ("salma", "y", "limon", "|", "sal", "marina")
    )
    choice = unclear_word_choice(span, 0, ("r3",), ["take"], "the third sauce he names")
    assert choice is not None and choice.kind == CONFLICT_UNCLEAR_WORD
    cases["unclear_word"] = (CONFLICT_UNCLEAR_WORD, [choice])
    # KRI-900: a freeze on a named moment asks which frame (one moment) or which moment (a
    # tie), on a phone montage that can hold a frame.
    freeze_strategy = {"edit_format": "montage", "audio_strategy": "original_audio"}
    freeze_ask = BriefRequirement(
        id="r3",
        kind="timing",
        scope="global",
        description="Freeze the frame for one second on Atakan's bite",
        facts={"freeze_frame": True},
    )

    def take(media_id: str, *descriptions: str) -> dict:
        return {
            "media_id": media_id,
            "kind": "video",
            "duration_s": 68.0,
            "analysis": {
                "best_moments": [
                    {"start_s": 10.0 * i + 5, "end_s": 10.0 * i + 8, "description": text}
                    for i, text in enumerate(descriptions)
                ]
            },
        }

    with (
        patch.object(settings, "phone_rendering_enabled", True),
        patch.object(settings, "phone_render_user_ids", []),
        patch.object(
            settings,
            "phone_render_verified_features",
            [*settings.phone_render_verified_features, "freezeFrame"],
        ),
    ):
        for name, rows in (
            ("freeze_one_moment", [take("a", "man takes first bite of dessert")]),
            ("freeze_tied_moments", [take("a", "his bite"), take("b", "another bite")]),
        ):
            add(
                name,
                CONFLICT_FREEZE_FRAME,
                freeze_strategy,
                _brief(freeze_ask),
                {
                    "clip_assignments": rows,
                    "clip_paths": [
                        f"users/u/creation-threads/t/analysis-proxy-ios-{i}.mp4"
                        for i in range(len(rows))
                    ],
                },
                # KRI-898: sent from an app build that plays a freeze hold.
                ChoiceCapability(creator_id="u", client_protocol=16),
            )
    return cases


def _creator_text(choice: object) -> list[str]:
    """Everything the creator can read for one choice, rendered in Turkish."""

    candidate = choice.candidate()  # type: ignore[attr-defined]
    payload = build_choice_question(candidate)
    texts = [choice.intro, choice.reason, choice_question_text(candidate)]  # type: ignore[attr-defined]
    for option in payload["options"]:
        texts += [option["label"], option.get("description") or ""]
    # The single-option statement path has its own sentence.
    one = candidate.__class__(
        conflict_id=candidate.conflict_id,
        reason=candidate.reason,
        intro=candidate.intro,
        options=candidate.options[:1],
        kind=candidate.kind,
        input_digest=candidate.input_digest,
    )
    texts.append(choice_question_text(one))
    return texts


def test_every_choice_kind_has_a_fixture_in_this_sweep() -> None:
    covered = {kind for kind, _found in _cases().values()}
    # Text-placement and title questions are built from ``say()`` pairs in their own
    # builders and are exercised by tests/services/test_kri520_choice_turkish.py.
    expected = set(CONFLICT_PRIORITY) - {"text_placement", "title_text"}
    assert expected <= covered, f"no Turkish fixture for {sorted(expected - covered)}"


def test_turkish_chat_gets_no_english_sentence_from_any_choice() -> None:
    leaks = []
    with reply_language_for("tr"):
        # Built under Turkish: the builders pick their copy when they run.
        for name, (_kind, found) in _cases().items():
            for choice in found:
                for text in _creator_text(choice):
                    if _english_words(text):
                        leaks.append((name, _english_words(text), text))
    assert not leaks, leaks


def test_the_detector_fires_on_the_english_copy_of_the_same_questions() -> None:
    """Control: without Turkish bound the same sweep finds English (it can fail)."""

    hits = sum(
        any(_english_words(t) for t in _creator_text(choice))
        for _kind, found in _cases().values()
        for choice in found
    )
    assert hits >= 5


def test_source_hands_every_choice_intro_and_reason_through_say() -> None:
    tree = ast.parse(Path(cq.__file__).read_text())
    problems: list[str] = []

    def is_say(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "say"
        )

    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if func.name == "ask_user_choice":
            continue  # the model wrote that question; the prompt carries the language
        for call in [n for n in ast.walk(func) if isinstance(n, ast.Call)]:
            callee = getattr(call.func, "id", None)
            if callee not in {"UnresolvedChoice", "ConflictCandidate"}:
                continue
            for kw in call.keywords:
                if kw.arg not in {"intro", "reason"}:
                    continue
                value = kw.value
                if is_say(value) or isinstance(value, ast.Attribute):
                    continue  # ``self.reason`` re-passes already-localized copy
                if isinstance(value, ast.Name):
                    # Assigned in the same function from ``say()`` or a ``say()`` tuple-pick.
                    assigns = [
                        n
                        for n in ast.walk(func)
                        if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == value.id for t in n.targets)
                    ]
                    if assigns and all(
                        is_say(a.value)
                        or (
                            isinstance(a.value, ast.IfExp)
                            and is_say(a.value.body)
                            and is_say(a.value.orelse)
                        )
                        for a in assigns
                    ):
                        continue
                problems.append(f"{func.name}:{kw.arg}@{call.lineno}")
    assert not problems, f"intro/reason not localized via say(): {problems}"
