"""High-level creative planner for the durable Main Creator Agent controller.

The model sees only a descriptive, server-resolved capability manifest and
returns an inert strategy. A deterministic compiler and authenticated route are
the only code allowed to turn that strategy into typed product operations.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    CREATOR_REQUEST_MAX_CHARS,
    CreatorAgentOutput,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_DRAFT_GUIDED_PROPOSAL,
    UserSongUnavailableError,
)
from app.config import settings
from app.kria.brief import BriefUpdate, parse_brief_updates
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import (
    MontageCadenceConstraint,
    recognize_cadence_reuse_policy,
    recognize_explicit_cadence_reuse_policy,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
    rejects_round_robin_cadence,
    resolve_video_reuse_policy,
)
from app.services.creator_capabilities import CAPABILITY_REACTION_BEATS

# Named sound effects are model-read with verbatim licensed_sfx evidence (v35).
# KRI-178: reaction beats (name/word-triggered photo/sticker + sound pop-ins,
# plus a held closing shot) on a phone `subtitled` (Talking) edit (v36).
# KRI-188: Creative Brief requirement extraction (`brief_updates`, taught only
# when the brief is on for the creator) -- v37.
# KRI-189: when/where clip facts with provenance (v38).
# KRI-190: brief `facts` (distance/activity/start/end) and an `order` requirement
# are always captured when the creator asks the edit to follow a route or sequence (v39).
# KRI-244: descriptive footage chronology remains creative context unless the
# creator actually asks the edit to order, group, label, include, or caption it (v40).
# KRI-374: a creator-uploaded song as the music (`audio_strategy="user_song"` +
# `song_sync`), taught only when the manifest carries a usable song (v42).
# KRI-422: dictated per-shot texts are one per_clip brief entry per shot (they all
# stay in force); `brief_updates` cap 8 -> 16 (v43).
# KRI-459: stable IDs make changes and removals unambiguous (v44).
# KRI-470: a stated video length ALWAYS becomes a `timing` requirement; the clarification
# gate cannot compare a number the brief lost (v45; brief_extractor v2 shares the section).
# KRI-506: delegated creative copy is proposed for a separate server approval (v46).
# KRI-479: `voice_mode` (continuous | excerpts) for a montage that keeps one clip's camera
# audio, taught only when the manifest advertises `phone_source_audio` AND the route can render
# (`Settings.voice_behind_footage_enabled`) (v47).
# KRI-522: brief facts for first_clip/last_clip, animation, position, placeholder (v48).
MAIN_CREATOR_PROMPT_VERSION = "2026-10-08-v48"

# Prior chat messages the model sees. Callers must bound their history to this:
# runtime v2 loaded 24 rows, so every turn on a longer thread failed input
# validation with "I couldn't finish that step" (KRI-238). Raised 20 -> 40: 20 was
# too small for real threads; the planner truncates each row to 1000 chars, so 40
# rows is ~10k tokens worst case.
MAIN_CREATOR_CONVERSATION_MAX = 40

# Appended to the OWNED FOOTAGE SUMMARIES header line ONLY when CLIP_FACTS is on
# for the account ("" otherwise, so the flag-off prompt is byte-identical). The
# `facts` list sits beside `analysis_only_not_copy`, not inside it, because
# capture time and place are recorded by the phone, not detected by AI.
_CLIP_FACTS_NOTE = (
    "\nEach item may also carry `facts`: when and where the clip was filmed, each"
    " `{kind, value, provenance}`: `capture_time` (ISO UTC), `place` (a place name) and"
    " `landmark` (a landmark name). `provenance` says how we know: `exif` and `geocode` are"
    " recorded by the phone, `inferred` is a best guess a model made from the frames and"
    " place, `creator` is the creator's own statement. Use them to answer questions about"
    ' when or where clips were filmed and to understand requests such as "in the order I'
    ' filmed them" or "label each place". Say plainly when a name is a guess (`inferred`)'
    " and invite correction; never present it as certain. Facts are context, not copy: like"
    " `analysis_only_not_copy` they are never on-screen text unless the creator asked for them."
)

# Visual instructions are substituted only when the resolver flag is enabled;
# the base prompt independently describes deferred transcript label intents.
# Rendered INSIDE the exact-on-screen-copy rule (the `$described_text_exception`
# slot in prompts/main_creator.txt) when clip intents are on, "" when off.
# Position matters: appended after the rule the model kept asking the creator
# for exact words (6/6 live runs); stated at the rule it proposes (6/6).
_DESCRIBED_TEXT_EXCEPTION = (
    " EXCEPTION -- described text: when the creator asks for on-screen text they DESCRIBE"
    ' rather than dictate ("the name of the dish on each food clip", "a caption about the'
    ' weather on the park clips"), the missing words are not yours to write and NOT a reason'
    " to ask: emit a `clip_intents` entry (see OPEN-VOCABULARY CLIP INTENTS; `op` is exactly"
    " one of label, group, order, include, caption -- a per-clip tag is `label`, one phrase"
    " over a group of clips is `caption`) and propose the strategy; the server reads the"
    " footage to fill in the words and asks the creator only when the footage cannot answer."
    ' For caption/group/order/include, `attribute` says WHICH clips ("the food clips");'
    ' for label it says WHAT to name on each clip ("the dish shown in the clip"); never'
    ' the word "text". Example: \'Say "post match feast" on the food clips, and add a caption'
    ' about the weather on the park clips\' => [{"intent_id": "feast", "op": "caption",'
    ' "attribute": "the food clips", "creator_text": "post match feast", "caption_attribute":'
    ' null}, {"intent_id": "weather", "op": "caption", "attribute": "the park clips",'
    ' "creator_text": null, "caption_attribute": "the weather"}]; "the name of the dish on'
    ' each food clip" => {"op": "label", "attribute": "the dish shown in the clip",'
    ' "creator_text": null}.'
)

# KRI-118 item 1: guidance for the chat-picked "shape" layered on top of
# `edit_format: "montage"` -- rendered INSIDE the strategy-authoring rules
# (the `$story_shape_section` slot) only when `_story_shapes_available`
# below says the shape could actually render this turn; "" otherwise, so the
# rest of the prompt is untouched byte-for-byte.
_STORY_SHAPE_PROMPT_SECTION = """
STORY SHAPE
The Montage card is the only picker entry for this direction, but two shapes are available
UNDER it: set `archetype` (alongside `edit_format: "montage"`) to "day_vlog" when the
request or footage reads as "my day", "morning to night", or "a day at X" -- the edit is
cut in chronological (shooting) order. Set `archetype` to "single_hero" and `hero_media_id`
to the owned media id that should dominate when one clip clearly should carry the edit and
the rest are cutaways ("show off this shot", "make this clip the star"). Never set
`archetype` for any other request; leave it null. Whenever you pick a shape, `summary` MUST
name the choice in plain language (for example "I'm cutting this as a day vlog, in the order
you shot it." or "I'm building this around your clip, with the rest as cutaways.") -- never
pick a shape silently.
""".strip("\n")

_CLIP_INTENTS_PROMPT_SECTION = """
OPEN-VOCABULARY CLIP INTENTS
When the creator asks to label, name, group, order, include, or caption clips by ANY
attribute they describe in their own words -- add `clip_intents` to `strategy`:
a list of at most 6 objects, each
{"intent_id": "short-slug", "op": "label|group|order|include|caption", "attribute": "the
creator's described attribute -- WHICH clips this is about, in your own words",
"creator_text": "the creator's exact on-screen words for this intent, or null",
"caption_attribute": "op=\\"caption\\" with no creator_text ONLY -- what the caption should
be ABOUT (e.g. \\"the weather\\"), never which clips; null for every other case",
"position": "first|last (only for op=\\"order\\"), else null"}. Examples this covers
(diverse; treat every similarly-shaped request the same way, not only these): "put the name
of the dish on each food clip" (label), "group these by city" (group), "move the clips
where nobody is on screen to the end" (order, position "last"), "only use the clips with my
dog in them" (include), "put my product's name under the unboxing shots" (label), 'say
"post match feast" on the food clips' (caption; creator_text="post match feast",
attribute="the food clips"), "add a caption about the weather on the beach clips" (caption;
creator_text=null, attribute="the beach clips", caption_attribute="the weather").
Use label_source="clip" for footage-derived labels (including the sport being played).
Requests about spoken scores, participant placeholders, or spoken topics use the
transcript source described above instead; never send them through the visual source.
Never put a per-clip
answer, a media id, or label/caption text you invented into `clip_intents` -- the server
matches clips to the described attribute and verifies any on-screen value against the
footage before it can render. `creator_text` may ONLY be the creator's own exact written
words for that intent, copied verbatim; never your paraphrase or an inference from clip
metadata. `label` prints a short tag on EVERY matching clip; `caption` is different -- it is
ONE short on-screen phrase for the WHOLE group of matching clips (a chapter), never a
per-clip value, so its `attribute` still names which clips it's for while
`caption_attribute` (only when there is no `creator_text`) names what the one phrase should
say. `analysis_only_not_copy` evidence may inform which owned clips an attribute or
`caption_attribute` is about, but you never author the label or caption text yourself.
Not having that text is NEVER a reason to ask the creator: a described label or caption is
complete as an intent, the server reads each clip's footage to fill in the value, and it asks
the creator itself only when the footage cannot answer. Propose the strategy with the intent.
""".strip("\n")

# KRI-178: name/word-triggered photo/sticker + sound pop-ins on a phone
# `subtitled` (Talking) edit, plus a held closing shot. Rendered INSIDE the
# strategy-authoring rules (the `$reaction_beats_section` slot, right after
# `$clip_intents_section`) ONLY when the `reaction_beats` capability is
# available on this manifest; "" otherwise, so the rest of the prompt is
# untouched byte-for-byte on every other manifest (including flag off).
_REACTION_BEATS_PROMPT_SECTION = """
REACTION BEATS (iPhone Talking only)
When the creator wants a photo/sticker or sound effect to pop up at a specific spoken moment
("pop up his photo when I say his name", "play a buzzer when I say no after Mason Greenwood"),
set `reaction_beats`: up to 24 objects, each {"beat_id": "short-slug", "trigger": "the exact
words to listen for, in the creator's own language -- a name as they say it, or 'number three'
for a spoken number", "after": "only count `trigger` when heard after this phrase, else null"
(e.g. "no" after "Mason Greenwood"), "occurrence": "first"|"every", "visual_id": "an owned
image's media_id, or null", "visual_role": "photo"|"sticker", "sound": "a sound_effect
catalog_id from the manifest's catalog if one matches, else the creator's own words such as
\"buzzer\"/\"ding\", or null", "hold_s": seconds on screen, or null for the server default}.
Every beat needs a `visual_id` or a `sound` (or both). Image media ids are opaque (e.g.
"asset-3f1c2a..."), never a filename -- find `visual_id` by matching what the creator named
against the IMAGE entries in `media_context`: its `filename` (e.g. "02_greenwood.png"), its
`creator_context`, or its `analysis_only_not_copy` evidence, whichever one matches -- then copy
THAT entry's exact `media_id` verbatim. Never invent, shorten, or guess an id, and never use a
filename or label as the id itself. A player's own photo (filename e.g. "02_greenwood.png") is
`visual_role` "photo"; a sticker such as a check/X/badge (filename e.g. "03_reject_x.png") is
"sticker". When the creator wants to end on a specific photo ("finish on Salah's photo with
the GOAT badge, with no extra clip after it"), set `closing_media`: {"visual_id": "...",
"badge_visual_id": "... or null", "from_trigger": "the phrase to hold from the last time it's
said, or null for the last few seconds"} -- resolved the same way, by matching `media_context`.
Example -- "pop up his photo when I say Mason Greenwood; when I say no after that, show the
red X and play a buzzer" => reaction_beats: [{"beat_id": "greenwood", "trigger": "Mason
Greenwood", "visual_id": "<the matching media_context entry's media_id>", "visual_role":
"photo"}, {"beat_id": "greenwood-no", "trigger": "no", "after": "Mason Greenwood", "visual_id":
"<the X sticker entry's media_id>", "visual_role": "sticker", "sound": "buzzer"}].
Never set `licensed_sfx` on this edit once this capability is available -- beats place sound
at the exact moments the creator named instead. A bare "add fun sound effects" with no named
spoken moment adds NO beats; say in `summary` that only sounds tied to the moments the creator
named are added on iPhone. This edit always stays `edit_format: "subtitled"` with no
`opening_title`.
""".strip("\n")


# KRI-374: the creator's own uploaded song on an iPhone montage. Rendered INSIDE
# the strategy-authoring rules (the `$user_song_section` slot, on the `$clip_intents_section`
# line, followed by `$voice_mode_section`) ONLY when `manifest.has_user_song`; "" otherwise,
# so every prompt without a song is byte-identical to before this field existed
# (pinned by tests/agents/test_main_creator_user_song.py).
_USER_SONG_PROMPT_SECTION = """
UPLOADED SONG (iPhone montage only)
The creator attached their own song; the manifest's `user_song` gives its `duration_s` and
whether it has lyrics (`has_lyrics`). That song IS the music for this edit: set
`audio_strategy` to "user_song" (never "licensed_music", and never pick a catalog track) and
also set `song_sync` inside `strategy` to exactly one of:
- "lipsync" -- ONLY when the creator asks to lip sync or sing along to the song: for example
  "lip sync", "lip-sync", "lipsync", "lip syncing to this song", "singing along",
  "mouthing the words", "dancing and lip syncing to this song", in the creator's own language.
  The takes are then placed to the song's timeline and the camera sound is muted. Leave
  `archetype`, `hero_media_id`, `execution_contract` and `montage_audio` null, and never
  combine it with a voiceover.
- "background" -- EVERYTHING ELSE, including merely mentioning a song, a concert, dancing,
  singing, a band or the artist ("a video from the concert", "we were dancing all night",
  "my favorite song"). The song is the music bed and the cuts follow its beat.
Decide by meaning: filming at a concert or dancing does not mean the creator wants their lips
synced to the song; only an explicit request to lip sync / sing along / mouth the words does.
Never ask the creator which mode they want; choose from their words. Use licensed music
instead only if the creator explicitly asks for it. `summary` MUST say in plain words which
mode you chose (for example "I'll use your song as the background music and cut to its beat."
or "I'll lip-sync your takes to your song, placing each one where it fits the music."). Never
invent or quote lyrics, and never promise cuts matched to lyrics.
""".strip("\n")


# KRI-479: how a named camera-audio clip is used under a montage. Rendered into the
# `$voice_mode_section` slot (the last on the `$clip_intents_section` line) ONLY when the
# manifest advertises `phone_source_audio` and the route can render for new jobs
# (`Settings.voice_behind_footage_enabled`); "" otherwise, so every prompt without that
# capability is byte-identical to before this field existed (pinned by
# tests/agents/test_main_creator_user_song.py).
_VOICE_MODE_PROMPT_SECTION = """
VOICE MODE (iPhone montage that keeps a clip's own sound)
When `montage_audio.preserve_source_audio` is true you may also set `voice_mode` inside
`strategy`. Leaving it null is the safe default, and it is the right answer whenever you are
not sure.
- "continuous" -- set it ONLY when the creator clearly asks for ONE particular clip's voice to
  play straight through, under the whole edit, while their other clips are the picture (for
  example "use the voice from my talk-to-camera video behind a fast montage of the rest").
  `montage_audio.source_media_ids` MUST then name exactly that one clip. That clip's own
  picture is not shown. Leave `archetype` and `hero_media_id` null for a continuous edit and
  do not call it a day vlog in `summary`: it is a plain montage with that clip's voice under it.
- "excerpts" -- chosen lines or quotes from the speaker cut over the footage. Use it when the
  creator asks for particular lines, quotes or moments from what someone says.
Do NOT set "continuous" when the creator asks you to pick the best quote, line or moment; asks
for a talking-head or subtitled edit, or to cut to the speaker and back; wants the sound of
several clips; names no particular clip as the voice; or the request is ambiguous. Leave
`voice_mode` null in those cases ("excerpts" only for chosen lines). Never invent a clip as the
voice. Say in `summary`, in plain words, which clip's voice plays and whether it plays straight
through.
""".strip("\n")


# KRI-188: Creative Brief extraction. Rendered into the `$brief_section` slot
# (appended to the clip-intents line, so "" adds no bytes) ONLY when
# `MainCreatorInput.brief_enabled` is true; flag off is byte-identical.
_BRIEF_PROMPT_SECTION = """
CREATIVE BRIEF
The FULL CREATOR REQUEST CONTRACT lists every requirement the creator has stated so far. In
ADDITION to `action`, return a top-level `brief_updates` list (at most 16 objects, in the same
JSON object as `action`) holding ONLY the requirements the CURRENT USER MESSAGE newly states or
changes -- never re-list a requirement that is already in the contract and unchanged. For a new
requirement use `operation`: "add" and include `kind`, `scope`, `literal`, `description`, and
`facts`. An add object is: {"operation":"add", "kind": "text|order|select|timing|audio|style",
"scope":
"title|per_clip|clip:<media_id>|global", "literal": "the creator's exact words to print, or
null", "description": "what is wanted in the creator's own framing, or null", "facts": {}}.
`literal` is ONLY text the creator wrote out; described text ("the landmark on each clip") goes
in `description` with `literal` null. Put structured details in `facts` (for order: {"key":
"capture_time"}; for timing: {"duration_s": 20}; for a route or distance: {"distance_km": 20,
"start": "...", "end": "..."}). Keep the creator's language and spelling (Turkish stays
Turkish).

The brief stores creator INSTRUCTIONS, not incidental descriptions of the footage. A sentence
such as "I took the sunset pictures walking to the bus and the night ones cycling home" supplies
creative context; it does NOT ask to group or order clips. "Come up with creative ideas" does not
turn those descriptive facts into operations. Use that context when proposing `action`, but emit
no `brief_updates` for it. Only add a requirement when the creator asks the output to do something
with the material or supplies exact on-screen copy.

When a real requirement is present, ALWAYS record the structured facts that belong to it, even
when the same words also sit in a title or sentence: a distance, activity, start point, or end
point ("I ran 20K from Arnavutköy to Eminönü") go in `facts` as {"distance_km": 20,
"activity": "run", "start": "Arnavutköy", "end": "Eminönü"} on the requested text/order
requirement they describe. Never invent a requirement only to store background facts. ALWAYS add
an `order` requirement when the creator ASKS the edit to follow a sequence ("put them in the order
I filmed", "order them chronologically", "start the edit at X and finish at Y"): {"kind":
"order", "scope": "global", "facts": {"key": "capture_time"}} plus "start"/"end" when named.
A clip the creator wants FIRST or LAST ("start with the video that is blue", "end on the
sunset") goes in that SAME order requirement as `first_clip` / `last_clip`: the creator's own
words for the clip ({"key": "capture_time", "first_clip": "the video that is blue"}); the
`description` alone is not enough. How on-screen text should look goes in the `facts` of its text
requirement, only what the creator said: `animation` ("typewriter", "fade", "pop" or "slide") for
"animated with typewriter"; `position` ("top_left", "top", "top_right", "middle", "bottom_left",
"bottom", "bottom_right") for "bottom left"; `placeholder` true when they ask for placeholder
text to fill in later ("placeholder location").
Merely narrating that footage was captured "from A to B", at sunset and then at night, or during
two activities is not such an ask. Compatible requirements with the same kind and scope coexist.
ALWAYS add a `timing` requirement ({"kind": "timing", "scope": "global", "facts": {"duration_s":
N}}) in the SAME turn's `brief_updates` whenever the creator states a length for the video: any
number of seconds or minutes ("a 60 second montage", "under 15 seconds", "about half a minute" =>
30), converted to seconds. Do it even when you also set `target_duration_s`, even for long
lengths, and even when the message carries other requirements ("all my clips", "in chronological
order"): never drop a stated number because other requirements are present. When the creator
states no length, emit NO timing requirement; a length you chose yourself in `target_duration_s`
is never the creator's.
The exception is exact text the creator dictates for particular shots ("1. The
bookshop photo: "..." 2. The bowling video: "..."): add one {"operation":"add", "kind": "text",
"scope":
"per_clip"} object PER SHOT with `literal` = that shot's exact words and `description` = the
shot as the creator named it. All of them stay in force. To change one requirement, use
`operation`: "change", its exact `target_requirement_id` and `expected_version` from the
contract, plus the complete replacement kind, scope, literal, description and facts. To withdraw
one requirement, use only `operation`: "remove", `target_requirement_id`, and
`expected_version`. Never guess a target; ask one concise question when the request does not name
an unambiguous requirement. Never mutate one target twice in a batch. A message that only asks to
redo the edit ("do it again based on my prompt") adds no
requirements -- propose a full strategy that honours EVERY requirement in the contract. Example:
"Title it 20K Koşu, put the landmark name on each clip and order them by the time I filmed
them" => brief_updates:
[{"operation":"add", "kind": "text", "scope": "title", "literal": "20K Koşu",
"description": null, "facts": {}}, {"operation":"add", "kind": "text", "scope": "per_clip",
"literal": null, "description": "the landmark shown in each clip", "facts": {}},
{"operation":"add", "kind": "order", "scope": "global", "literal": null,
"description": "chronological by filming time", "facts": {"key": "capture_time"}}].
Example: "Start with the video that is blue. Add a hook there, animated with typewriter. Then
everything chronologically. Add placeholder location to each video to the bottom left" =>
brief_updates: [{"operation":"add", "kind": "order", "scope": "global", "literal": null,
"description": "chronological, starting with the blue video", "facts": {"key": "capture_time",
"first_clip": "the video that is blue"}}, {"operation":"add", "kind": "text", "scope": "title",
"literal": null, "description": "a hook animated with typewriter", "facts": {"animation":
"typewriter"}}, {"operation":"add", "kind": "text", "scope": "per_clip", "literal": null,
"description": "placeholder location at the bottom left", "facts": {"placeholder": true,
"position": "bottom_left"}}].
""".strip("\n")


class MainCreatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
    creator_request: str = Field(default="", max_length=CREATOR_REQUEST_MAX_CHARS)
    creator_context: str = Field(default="", max_length=4000)
    creator_direction: str = Field(default="", max_length=4000)
    item_context: str = Field(default="", max_length=4000)
    media_context: list[dict] = Field(default_factory=list, max_length=50)
    conversation: list[dict] = Field(default_factory=list, max_length=MAIN_CREATOR_CONVERSATION_MAX)
    capability_manifest: ResolvedCreatorManifest
    # Durable, event-folded state is separate from the bounded chat window.
    creative_copy_state: list[dict] = Field(default_factory=list, max_length=2)
    # KRI-188: True only when the Creative Brief is on for this creator. Off =>
    # the prompt is byte-identical and no `brief_updates` are read from output.
    brief_enabled: bool = False


class MainCreatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: CreatorAgentOutput
    # KRI-188: requirements newly stated/changed by the current message. Empty
    # (and omitted from dumps) unless the brief is enabled for this creator.
    brief_updates: list[BriefUpdate] = Field(default_factory=list, exclude_if=lambda v: not v)
    creative_decision: CreativeCopyDecision | None = Field(
        default=None, exclude_if=lambda v: v is None
    )


class CreativeCopyDecision(BaseModel):
    """A model suggestion/classification; it never approves wording."""

    model_config = ConfigDict(extra="forbid")

    target: Literal["opening_title", "closing_title"]
    status: Literal["unresolved", "delegated", "candidate", "creator_supplied", "cancelled"]
    proposed_text: str | None = Field(default=None, max_length=280)
    source_evidence: str | None = Field(default=None, max_length=1200)
    language: str = Field(default="en", max_length=16)


class MainCreatorAgent(Agent[MainCreatorInput, MainCreatorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.creator.main",
        prompt_id="main_creator",
        prompt_version=MAIN_CREATOR_PROMPT_VERSION,
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-3.6-flash",),
        # KRI-118 item 4: bumped from 2 -> 3 alongside `schema_retry_limit`
        # below -- `_run_on_model`'s loop bounds EVERY retry path (transient/
        # refusal/schema) by `max_attempts`, so a `schema_retry_limit` above
        # `max_attempts - 1` is otherwise unreachable dead configuration.
        max_attempts=3,
        backoff_s=(2.0,),
        # Generation time grows with thinking + answer tokens: ~4.7 s + 5.7 ms
        # per token in prod, up to 2 s slower (the latency model in
        # tests/agents/test_thinking_budget.py). At that worst case a
        # heavy-thinking reaction-beat plan takes ~38 s, and the full
        # `max_output_tokens` budget runs out (~54 s) before this deadline, so a
        # runaway call truncates (retryable) instead of ending outcome-unknown.
        timeout_s=60.0,
        # Reserve output capacity for the full source manifest.
        thinking_level="low",
        sensitive_io=True,
        # This agent's output schema (bounded editorial choices across many
        # optional fields, typed evidence, clip intents) is wide enough that
        # one clarification retry sometimes isn't enough headroom to recover
        # from a single missed constraint.
        schema_retry_limit=2,
    )
    Input = MainCreatorInput
    Output = MainCreatorOutput
    response_json = True
    # Gemini 3 counts thinking against this budget. A reaction-beat plan alone
    # runs ~2.4k answer tokens (KRI-172 football prompt), so 4,096 truncated any
    # such turn that thought for more than ~1.7k tokens (prod thread 9b6594a6
    # thought 3,047 and failed at MAX_TOKENS on 2026-09-24).
    max_output_tokens = 8_192

    def required_fields(self) -> list[str]:
        return ["action"]

    def render_prompt(self, input: MainCreatorInput) -> str:  # noqa: A002
        # Storage identity is needed for the confirmation/execution fence but
        # is not useful creative context and must not be shown to the model.
        prompt_manifest = input.capability_manifest.model_dump_json(
            exclude_none=True,
            exclude={"narration": True},
        )
        return load_prompt(
            "main_creator",
            creator_context=input.creator_context or "(not available)",
            creator_direction=input.creator_direction or "(none)",
            item_context=input.item_context or "(not available)",
            media_context=json.dumps(input.media_context, ensure_ascii=False),
            # Rendered only when a clip carries facts (the flag gates their
            # presence in media_context), so a flag-off prompt is unchanged.
            clip_facts_note=(
                _CLIP_FACTS_NOTE if any(row.get("facts") for row in input.media_context) else ""
            ),
            capability_manifest=prompt_manifest,
            conversation=json.dumps(input.conversation, ensure_ascii=False),
            creator_request=input.creator_request or input.user_message,
            user_message=input.user_message,
            creative_copy_state=json.dumps(input.creative_copy_state, ensure_ascii=False),
            # Renders to "" (flag off) on the one blank template line it
            # occupies, so the rest of the prompt is untouched byte-for-byte.
            clip_intents_section=(
                _CLIP_INTENTS_PROMPT_SECTION if settings.clip_intents_enabled else ""
            ),
            described_text_exception=(
                _DESCRIBED_TEXT_EXCEPTION if settings.clip_intents_enabled else ""
            ),
            # KRI-118 item 1: only mention story shapes when they could
            # actually render this turn (rollout flag + guided proposal
            # capability + no recorded voiceover) -- defense in depth, since
            # `compile_strategy_to_plan` (`repair_creator_strategy_shape`)
            # repairs an unavailable shape away regardless of whether the
            # model saw this guidance.
            story_shape_section=(
                _STORY_SHAPE_PROMPT_SECTION
                if _story_shapes_available(input.capability_manifest)
                else ""
            ),
            # KRI-178: only teach reaction beats when the manifest actually
            # advertises the capability -- "" otherwise, byte-identical to
            # before this field existed (flag off, cloud, non-subtitled, or
            # any other reason the capability is unavailable). The template
            # concatenates this slot directly onto `$clip_intents_section`'s
            # line (no line of its own) so an empty value never adds a blank
            # line; the leading "\n" here supplies the separator only when
            # there is real content to show.
            reaction_beats_section=(
                "\n" + _REACTION_BEATS_PROMPT_SECTION
                if _reaction_beats_available(input.capability_manifest)
                else ""
            ),
            # KRI-188: "" (flag off) adds no bytes; same line-suffix trick.
            brief_section=("\n" + _BRIEF_PROMPT_SECTION if input.brief_enabled else ""),
            # KRI-374: "" (no usable song) adds no bytes; same line-suffix trick.
            user_song_section=(
                "\n" + _USER_SONG_PROMPT_SECTION if input.capability_manifest.has_user_song else ""
            ),
            # KRI-479: "" (no `phone_source_audio`) adds no bytes; same line-suffix trick.
            voice_mode_section=(
                "\n" + _VOICE_MODE_PROMPT_SECTION
                if _voice_mode_available(input.capability_manifest)
                else ""
            ),
        )

    def parse(self, raw_text: str, input: MainCreatorInput) -> MainCreatorOutput:  # noqa: A002
        self._schema_feedback = ""
        try:
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            data = _hoist_misplaced_brief_updates(data)
            raw_action = _repair_action_envelope(data.get("action"))
            if isinstance(raw_action, dict) and raw_action.get("kind") == "propose_strategy":
                raw_strategy = raw_action.get("strategy")
                if not isinstance(raw_strategy, dict) or "target_duration_s" not in raw_strategy:
                    raise ValueError("propose_strategy must explicitly choose target_duration_s")
                rationale = raw_strategy.get("rationale")
                if not isinstance(rationale, str) or not rationale.strip():
                    raise ValueError("propose_strategy must explain its duration in rationale")
            action = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(raw_action)
            if isinstance(action, ProposeStrategy):
                # Share the compiler's exact policy: guided planning never
                # echoes opaque IDs, while native planning remains bounded to
                # owned non-asset media.
                from app.agents._schemas.creator_policy import (  # noqa: PLC0415
                    explicit_scope_from_stated_media_count,
                    normalize_creator_strategy_media,
                )

                user_messages = [
                    str(turn.get("content") or "")
                    for turn in input.conversation
                    if isinstance(turn, dict) and turn.get("role") == "user"
                ]
                # KRI-188: with the brief on, `creator_request` is the rendered
                # ledger, whose model-authored descriptions ("shown in each
                # clip") would trip the "each clip" scope recognisers. Read only
                # creator-authored text (history + latest message) instead.
                request_contract = (
                    input.user_message
                    if input.brief_enabled
                    else input.creator_request or input.user_message
                )
                timing = recognize_mixed_media_timing("\n".join([*user_messages, request_contract]))
                combined_request = "\n".join([*user_messages, request_contract])
                latest_cut_s = recognize_round_robin_cadence(input.user_message)
                cadence_cut_s = (
                    None
                    if rejects_round_robin_cadence(input.user_message)
                    else latest_cut_s or recognize_round_robin_cadence(combined_request)
                )
                videos = [
                    media
                    for media in input.capability_manifest.media
                    if media.kind == "video" and media.duration_s is not None
                ]
                cadence = None
                if cadence_cut_s is not None and len(videos) == 2:
                    reuse_policy = recognize_explicit_cadence_reuse_policy(
                        input.user_message
                    ) or recognize_cadence_reuse_policy(combined_request)
                    cadence = MontageCadenceConstraint(
                        source_media_ids=[media.media_id for media in videos],
                        cut_duration_s=cadence_cut_s,
                        reuse_policy=reuse_policy,
                    )
                reuse = "once"
                for message in [request_contract, *user_messages, input.user_message]:
                    reuse = resolve_video_reuse_policy(message, reuse)
                reuse = resolve_video_reuse_policy(input.user_message, reuse, cadence)
                if reuse == "once":
                    # A numeric cadence structurally requires reusing (or at
                    # least re-cutting between) the same two sources more
                    # than once; "once" only happens here when the creator's
                    # own latest wording explicitly forbade any repeat/loop,
                    # which is incompatible with that cadence. Falling back
                    # to an ordinary montage (not erroring) mirrors the
                    # route's identical rule (creator_agent.py, same "once"
                    # check), so this is existing, intentional policy rather
                    # than a silent drop of unrelated creator intent.
                    cadence = None
                # KRI-129 part C: the regex is evidence FOR an explicit scope,
                # never a veto over what the model itself read from the full
                # conversation. Regex silence must not erase a model-authored
                # "all"/"selected" the creator did state some other way.
                explicit_scope = _explicit_media_scope_from_request(combined_request)
                if explicit_scope is None and explicit_scope_from_stated_media_count(
                    combined_request, input.capability_manifest
                ):
                    # "Continue with 16 clips" naming the whole manifest size
                    # is just as explicit as literally saying "all" -- apply
                    # the same rule the route applies post-hoc, so it holds
                    # even before `_apply_explicit_render_intent` runs.
                    explicit_scope = "all"
                resolved_scope = (
                    explicit_scope if explicit_scope is not None else action.strategy.media_scope
                )
                strategy = action.strategy.model_copy(
                    update={
                        # Server-owned approval provenance.  Read this from
                        # the raw payload before the normalizer copies the
                        # default 24s onto every strategy; a model-provided
                        # marker is never trusted.
                        "target_duration_requested": (
                            True if "target_duration_s" in raw_strategy else None
                        ),
                        # KRI-374: server-owned, like `resolved_clip_intents`: a
                        # model-authored per-take song placement is never trusted.
                        "resolved_song_takes": None,
                        # KRI-476: server-owned conflict answers, never model-authored.
                        "choice_answers": None,
                        "mixed_media_timing": timing,
                        "montage_cadence": cadence,
                        "video_reuse_policy": reuse,
                        "media_scope": resolved_scope,
                    }
                )
                action = action.model_copy(
                    update={
                        "strategy": normalize_creator_strategy_media(
                            input.capability_manifest,
                            strategy,
                            repair_model_output=True,
                        )
                    }
                )
            return MainCreatorOutput(
                action=action,
                creative_decision=(
                    CreativeCopyDecision.model_validate(data["creative_decision"])
                    if data.get("creative_decision") is not None
                    else None
                ),
                brief_updates=(
                    parse_brief_updates(data.get("brief_updates")) if input.brief_enabled else []
                ),
            )
        except ValidationError as exc:
            # Tell the retry which contract fields failed, without echoing
            # private field values or the model's full response into logs.
            self._schema_feedback = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                for error in exc.errors(include_input=False, include_context=False)[:8]
            )[:1000]
            raise SchemaError(f"main_creator: invalid output: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, UserSongUnavailableError):
                # KRI-374: the creator-facing copy is a question for the creator;
                # the retry needs the rule it broke.
                self._schema_feedback = (
                    'audio_strategy "user_song" needs a usable uploaded song on this '
                    "manifest (manifest.user_song); choose another audio_strategy"
                )
            elif isinstance(exc, ValueError):
                # Server policy refusals ("all-media scope requires the guided
                # proposal capability") carry fixed, value-free messages. Name
                # the rule so the retry can change course instead of repeating
                # the same strategy blind (KRI-238).
                self._schema_feedback = str(exc)[:300]
            raise SchemaError(f"main_creator: invalid output: {exc}") from exc

    def schema_clarification(self) -> str:
        feedback = getattr(self, "_schema_feedback", "")
        return (
            "\nReturn only the documented JSON envelope with one valid action object."
            + (f"\nCorrect these schema errors: {feedback}." if feedback else "")
            + " Use only documented fields, exact enum values, and #RRGGBB colors."
        )


def _story_shapes_available(manifest: ResolvedCreatorManifest) -> bool:
    """KRI-118 item 1: whether the STORY SHAPE prompt guidance is worth
    showing this turn -- rollout flag, guided proposal capability, and no
    recorded voiceover (a shape only ever renders on a guided montage).
    Mirrors, but does not replace, the server-side repair in
    `app.agents._schemas.creator_policy.repair_creator_strategy_shape` --
    this only controls whether the MODEL is invited to propose one; the
    compiler never trusts the model's own choice alone.
    """

    if not settings.creator_montage_shapes_enabled or manifest.has_voiceover:
        return False
    guided = manifest.capabilities.get(CAPABILITY_DRAFT_GUIDED_PROPOSAL)
    return bool(guided is not None and guided.available)


def _voice_mode_available(manifest: ResolvedCreatorManifest) -> bool:
    """KRI-479: teach `voice_mode` only on a phone manifest that can keep source audio.

    `compile_strategy_to_plan` (`repair_creator_voice_mode`) drops a stray value
    regardless of whether the model saw this guidance.
    """

    if not settings.voice_behind_footage_enabled:
        return False  # the route cannot render for new jobs: never advertise it
    capability = manifest.capabilities.get("phone_source_audio")
    return bool(capability is not None and capability.available)


def _reaction_beats_available(manifest: ResolvedCreatorManifest) -> bool:
    """KRI-178: whether the REACTION BEATS prompt guidance is worth showing
    this turn -- purely a read of the manifest's own resolved capability
    (`app.services.creator_capabilities.resolve_creator_manifest` is the
    single source of truth for the flag/format/device gate). This only
    controls whether the MODEL is invited to author beats; the compiler
    (`compile_strategy_to_plan`'s `_repair_creator_reaction_beats`) never
    trusts the model's own choice alone and repairs an unavailable/unresolved
    beat away regardless of whether the model saw this guidance.
    """

    capability = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
    return bool(capability is not None and capability.available)


def _repair_action_envelope(action: object) -> object:
    """Repair only the known harmless nested-field envelope typos.

    Some model responses put the proposal `summary` (a string) or
    `render_intent_evidence` (an object) inside ``strategy`` although the
    documented envelope puts both beside ``strategy`` (the evidence one was
    seen on a phone Talking "Add captions" turn, KRI-238). Move each only when
    its destination is absent; all other malformed or unknown fields remain
    subject to the strict adapter and fail closed.
    """

    if not isinstance(action, dict) or action.get("kind") != "propose_strategy":
        return action
    strategy = action.get("strategy")
    if not isinstance(strategy, dict):
        return action
    moved = {
        key: strategy[key]
        for key, kind in (("summary", str), ("render_intent_evidence", dict))
        if key not in action and isinstance(strategy.get(key), kind)
    }
    if not moved:
        return action
    repaired_strategy = {key: value for key, value in strategy.items() if key not in moved}
    return {**action, "strategy": repaired_strategy, **moved}


def _hoist_misplaced_brief_updates(data: dict) -> dict:
    """Move a `brief_updates` list the model nested under the action to the top level.

    The documented envelope puts `brief_updates` BESIDE `action`; a Flash reply
    on a phone narrated turn (KRI-456, 2026-10-06) put the whole list inside
    `action` (``propose_strategy.brief_updates``), which the strict adapter
    rejects as an extra field and the turn then fails terminally. The list is
    moved only when the top level has none (and only out of `action` or its
    `strategy`); its entries still go through `parse_brief_updates` unchanged.
    """

    if data.get("brief_updates"):
        return data
    action = data.get("action")
    if not isinstance(action, dict) or action.get("kind") != "propose_strategy":
        return data
    updates = action.get("brief_updates")
    repaired_action = {key: value for key, value in action.items() if key != "brief_updates"}
    strategy = action.get("strategy")
    if not isinstance(updates, list) and isinstance(strategy, dict):
        updates = strategy.get("brief_updates")
        if isinstance(updates, list):
            repaired_action["strategy"] = {
                key: value for key, value in strategy.items() if key != "brief_updates"
            }
    if not isinstance(updates, list):
        return data
    return {**data, "action": repaired_action, "brief_updates": updates}


__all__ = [
    "MAIN_CREATOR_CONVERSATION_MAX",
    "MAIN_CREATOR_PROMPT_VERSION",
    "MainCreatorAgent",
    "MainCreatorInput",
    "MainCreatorOutput",
    "_repair_action_envelope",
]


def _explicit_media_scope_from_request(request: str) -> str | None:
    """Keep the model from turning ordinary editorial selection into all-media scope."""

    normalized = " ".join(str(request or "").casefold().split())
    if re.search(
        r"\b(?:do not|don't|dont|never|without|no)\b.{0,40}"
        r"\b(?:use|include|keep|select)\b.{0,20}\b(?:all|everything|every)\b"
        r"|\b(?:all|everything|every)\b.{0,20}\b(?:not|excluded|omit)\b",
        normalized,
    ):
        return "selected"
    if re.search(
        r"\b(?:all|every|each)\s+(?:the\s+)?(?:images?|photos?|videos?|clips?|media|footage)\b"
        r"|\buse\s+(?:all|everything)\b(?!\s+(?:of\s+)?(?:the\s+|my\s+)?(?:overlays?|visuals?)\b)"
        r"|\b(?:use|include|keep)\s+(?:these|those)(?:\s+\d+)?\s+"
        r"(?:images?|photos?|videos?|clips?|files?|pieces?\s+of\s+media)\b"
        r"|\b(?:all|every)\s+(?:uploaded|provided)\s+(?:media|files?|images?|photos?|videos?)\b",
        normalized,
    ):
        return "all"
    if re.search(
        r"\b(?:only|just)\s+(?:the\s+)?(?:selected|specified|chosen|listed)\b"
        r"|\bselected\s+(?:media|files?|clips?)\b",
        normalized,
    ):
        return "selected"
    return None
