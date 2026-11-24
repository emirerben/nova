"""nova.compose.intro_scene (KRI-1612): a small agent that composes one creator's intro scene
from the options the server issued, or declines.

**The agent picks, the engine builds.** Its only WRITE is an ``IntroScene`` (KRI-1610): a decision,
one voice per word of her approved title, and an option id each for font pair, ink, zone and
entrance (sound is always ``none``: an automatic sound is an open question on the project). The
schema has no field that reaches the screen as text and none that holds a coordinate, size or
time (``tests/pipeline/test_intro_scene_schema.py`` walks it), and the response schema sent with
the call is closed over THIS call's issued ids, so even a provider that ignores it cannot widen
the answer: ``parse`` re-checks every id with ``intro_scene.check_scene`` and the voices against
the issued splits. Hard rules (one ink, no Fraunces, stroke limits, glyph coverage, clearance from
the face and captions) stay in the engine (`compile_intro_scene` + the real `place_title_look`),
never in the prompt. Gemini's footage notes and her title words are UNTRUSTED data: they sit in
one ``untrusted`` object and nothing the model returns can carry them back as text.

Prompt: ``prompts/intro_scene.txt`` + the ``intro-design`` skill (``load_skill``) + the five
teardowns in ``prompts/intro_teardowns.json`` as examples. The version is a fingerprint of those
files and of the issued catalogue, so editing any of them moves it (the prompt-change rule).

Failure modes this was written against (each has a replay cassette in
``tests/evals/test_intro_scene_agent_evals.py``):

1. an id the server never issued (a face that cannot draw Turkish, ``pen`` (never issued, see
   `build_input`), an unknown ink)                         -> the answer is void
2. a split that drops, adds or reorders a word, or one the picker never issued
                                                           -> void
3. a smuggled field (``text``, ``why``, a number) or a sound -> void
4. a decline that carries choices, or a half-filled compose  -> void (``IntroScene`` refuses it)
5. a decline                                                 -> honoured: today's title, once
6. provider error, timeout, truncation, malformed JSON       -> never raises out of the worker
   (``intro_scene_pick.resolve_model_scene``): today's title, recorded
7. English and Turkish titles                                -> menu issued per title
No retry: a second paid call would not make a void answer less void (``schema_retry_limit=0``).
"""

from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents._prompt_fingerprint import prompt_fingerprint
from app.agents._runtime import Agent, AgentSpec, SchemaError, TerminalError
from app.agents._skills import load_skill
from app.pipeline import intro_scene as scene_mod
from app.pipeline.prompt_loader import load_prompt

_TEARDOWNS = Path(__file__).resolve().parents[2] / "prompts" / "intro_teardowns.json"

# Files whose bytes are the prompt; editing any moves ``prompt_version``.
INTRO_SCENE_PROMPT_SOURCES: tuple[str, ...] = (
    "prompts/intro_scene.txt",
    "prompts/intro_teardowns.json",
    "app/agents/skills/intro-design/SKILL.md",
)
# The issued catalogue is rendered into the prompt from code, so it is hashed in too.
_CATALOGUE = json.dumps(
    {
        "pairs": {k: [v.solid.family, v.accent.family] for k, v in scene_mod.FONT_PAIRS.items()},
        "inks": list(scene_mod.INKS),
        "zones": list(scene_mod.ZONES),
        "entrances": list(scene_mod.ENTRANCES),
    },
    sort_keys=True,
)
INTRO_SCENE_PROMPT_VERSION = prompt_fingerprint("is", INTRO_SCENE_PROMPT_SOURCES, extra=_CATALOGUE)

MAX_WORDS = 24
MAX_NOTES_CHARS = 300
Brightness = Literal["light", "mid", "dark", "unknown"]


class IntroSceneInput(BaseModel):
    language: Literal["en", "tr"] = "en"
    # What the server issued for this title. The model may use only these ids and splits.
    splits: list[list[scene_mod.Voice]] = Field(min_length=1, max_length=48)
    font_pairs: list[str] = Field(min_length=1, max_length=len(scene_mod.FONT_PAIRS))
    inks: list[str] = Field(min_length=1, max_length=len(scene_mod.INKS))
    zones: list[str] = Field(min_length=1, max_length=len(scene_mod.ZONES))
    entrances: list[str] = Field(min_length=1, max_length=len(scene_mod.ENTRANCES))
    # Measured by the server from the real take frames: a trusted category, never model text.
    brightness: Brightness = "unknown"
    # UNTRUSTED: her title words (the voices line up with them) and what Gemini wrote about the
    # footage. Data for taste; nothing returned can carry either back as text.
    title_words: list[str] = Field(min_length=2, max_length=MAX_WORDS)
    footage_notes: str = Field(default="", max_length=MAX_NOTES_CHARS)

    def menu(self) -> scene_mod.IntroMenu:
        return scene_mod.IntroMenu(
            font_pairs=tuple(self.font_pairs),
            inks=tuple(self.inks),
            zones=tuple(self.zones),
            entrances=tuple(self.entrances),
            sounds=("none",),  # an automatic sound is an open question on the project
        )


def build_input(
    title: str,
    *,
    language: str | None,
    footage_notes: str = "",
    brightness: str = "unknown",
) -> IntroSceneInput | None:
    """What to ask for ``title``, or ``None`` when the server has nothing to issue (no accent
    split, or no face that draws her words): nothing to choose, so no call."""
    from app.pipeline.intro_scene_pick import accent_splits  # noqa: PLC0415

    words = [word.text for word in scene_mod.title_words(title)]
    splits = accent_splits(title)
    menu = scene_mod.issue_menu(title)
    if not splits or not menu.font_pairs or not (2 <= len(words) <= MAX_WORDS):
        return None
    try:
        return IntroSceneInput(
            language="tr" if language == "tr" else "en",
            splits=[list(voices) for voices in splits],
            font_pairs=list(menu.font_pairs),
            inks=list(menu.inks),
            zones=list(menu.zones),
            # The handwriting effect ("pen") draws stroke paths with no text layer, so the approval
            # check cannot find her words in the drawn rows (KRI-1612 found it through the real
            # worker). It is not issued to the model until the check reads it.
            entrances=[entrance for entrance in menu.entrances if entrance != "pen"],
            brightness=brightness if brightness in ("light", "mid", "dark") else "unknown",
            title_words=[word[:40] for word in words],
            footage_notes=" ".join(footage_notes.split())[:MAX_NOTES_CHARS],
        )
    except ValidationError:
        return None


def response_schema_for(input: IntroSceneInput) -> dict[str, Any]:  # noqa: A002
    """The response schema, closed over this call's issued ids: every string is an enum, there is
    no free text or number, and an unknown property is refused."""
    count = len(input.title_words)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["compose", "decline"]},
            "decline_reason": {"type": "string", "enum": list(_decline_reasons())},
            "voices": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {"type": "string", "enum": ["solid", "accent"]},
            },
            "font_pair": {"type": "string", "enum": list(input.font_pairs)},
            "ink": {"type": "string", "enum": list(input.inks)},
            "zone": {"type": "string", "enum": list(input.zones)},
            "entrance": {"type": "string", "enum": list(input.entrances)},
            "sound": {"type": "string", "enum": ["none"]},
        },
        "required": ["decision"],
    }


def _decline_reasons() -> tuple[str, ...]:
    from typing import get_args  # noqa: PLC0415

    return get_args(scene_mod.DeclineReason)


@lru_cache(maxsize=1)
def _teardowns() -> list[dict[str, Any]]:
    return list(json.loads(_TEARDOWNS.read_text(encoding="utf-8"))["teardowns"])


def _marked_title(title: str, voices: list[str]) -> str:
    """Her words with the accent run in brackets: ``the biggest design trend [right now]``."""
    out: list[str] = []
    open_run = False
    for word, voice in zip(scene_mod.title_words(title), voices, strict=False):
        if voice == "accent" and not open_run:
            out.append("[" + word.text)
            open_run = True
        elif voice != "accent" and open_run:
            out[-1] += "]"
            out.append(word.text)
            open_run = False
        else:
            out.append(word.text)
    if open_run:
        out[-1] += "]"
    return " ".join(out)


def examples_for(language: str) -> list[dict[str, Any]]:
    """The teardowns that have a scene as taste examples. No sound (always none for the model)."""
    out: list[dict[str, Any]] = []
    for teardown in _teardowns():
        examples = teardown.get("examples") or {}
        example = examples.get(language) or examples.get("en")
        if not example or example["scene"].get("decision") != "compose":
            continue
        scene = example["scene"]
        out.append(
            {
                "footage_fit": teardown["footage_fit"],
                "why_it_works": teardown["why_it_works"],
                "title": _marked_title(example["title"], scene["voices"]),
                "choice": {
                    key: scene[key]
                    for key in ("font_pair", "ink", "zone", "entrance")
                    if key in scene
                },
            }
        )
    return out


class IntroSceneDesignerAgent(Agent[IntroSceneInput, scene_mod.IntroScene]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.intro_scene",
        prompt_id="intro_scene",
        prompt_version=INTRO_SCENE_PROMPT_VERSION,
        model="gemini-2.5-flash",
        timeout_s=12,
        # A failed or void answer is today's title, not a second paid call.
        max_attempts=1,
        enable_clarification_retries=False,
        schema_retry_limit=0,
        thinking_budget=256,
        # Her title words and the footage notes are the creator's private material.
        sensitive_io=True,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = IntroSceneInput
    Output = scene_mod.IntroScene
    max_output_tokens: ClassVar[int] = 384
    response_json: ClassVar[bool] = True

    def run(self, input: IntroSceneInput | dict, *, ctx=None):  # noqa: A002, ANN001, ANN201
        try:
            validated = self._validate_input(input)
        except Exception as exc:
            # Pydantic's message would quote her title.
            raise TerminalError("nova.compose.intro_scene: invalid input") from exc
        # The schema depends on what the server issued for this call, so it is bound per run.
        self.spec = dataclasses.replace(
            type(self).spec, response_schema=response_schema_for(validated)
        )
        return super().run(validated, ctx=ctx)

    def required_fields(self) -> list[str]:
        return ["decision"]

    def render_prompt(self, input: IntroSceneInput) -> str:  # noqa: A002
        payload = {
            "splits": input.splits,
            "menu": {
                "font_pair": input.font_pairs,
                "ink": input.inks,
                "zone": input.zones,
                "entrance": input.entrances,
            },
            "footage": {"brightness": input.brightness},
            "untrusted": {"title_words": input.title_words, "footage_notes": input.footage_notes},
            "examples": examples_for(input.language),
        }
        return load_prompt(
            "intro_scene",
            skill=load_skill("intro-design"),
            language=input.language,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def parse(self, raw_text: str, input: IntroSceneInput) -> scene_mod.IntroScene:  # noqa: A002
        try:
            data = json.loads(raw_text)
            scene = scene_mod.IntroScene.model_validate(data)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise SchemaError("intro scene returned invalid typed output") from exc
        if scene.decision == "decline":
            return scene
        if scene.sound != "none":
            raise SchemaError("intro scene may not choose a sound")
        if list(scene.voices) not in input.splits:
            raise SchemaError("intro scene split is not one the server issued")
        if reason := scene_mod.check_scene(scene, " ".join(input.title_words), input.menu()):
            raise SchemaError(f"intro scene uses an option the server did not issue: {reason}")
        return scene


__all__ = [
    "INTRO_SCENE_PROMPT_SOURCES",
    "INTRO_SCENE_PROMPT_VERSION",
    "IntroSceneDesignerAgent",
    "IntroSceneInput",
    "build_input",
    "examples_for",
    "response_schema_for",
]
