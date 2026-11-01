"""High-level creative planner for the durable Main Creator Agent controller.

The model sees only a descriptive, server-resolved capability manifest and
returns an inert strategy. A deterministic compiler and authenticated route are
the only code allowed to turn that strategy into typed product operations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, ClassVar, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, ModelClient, SchemaError
from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    CREATOR_REQUEST_MAX_CHARS,
    PLANNER_BOUNDARY_DECLINES_CONTEXT,
    STORY_LINE_MAX_CHARS,
    CreativeStrategy,
    CreatorAgentOutput,
    DraftVoiceoverScript,
    PlannerDecline,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_DRAFT_GUIDED_PROPOSAL,
    UserSongUnavailableError,
    native_voiceover_only,
)
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.agents._skills import load_skill
from app.agents._writing_context import turkish_audience_instruction
from app.agents.prompt_vocab import render_vocab_tokens
from app.config import settings
from app.kria.brief import BriefUpdate, parse_brief_updates
from app.kria.reply_language import prompt_language_line
from app.kria.take_trim import refile_take_trim
from app.kria.text_sequence import last_quoted_literal, refile_text_sequences
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import (
    MAX_OPENING_TITLE_DURATION_S,
    MixedMediaTimingProfile,
    MontageCadenceConstraint,
    recognize_cadence_reuse_policy,
    recognize_explicit_cadence_reuse_policy,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
    rejects_round_robin_cadence,
    resolve_video_reuse_policy,
    turkish_media_scope,
)
from app.schemas.text_style_intent import normalize_label_position, normalize_title_animation
from app.services.creator_capabilities import (
    CAPABILITY_REACTION_BEATS,
    CAPABILITY_TIMED_CARDS,
    ending_clip_candidates,
    speaker_background_open,
)
from app.services.length_policy import creator_duration_s

log = structlog.get_logger()

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
# KRI-523: `pinned_texts` (whole-video corner text) + choice-answer brief rule (v49).
# KRI-520: a Turkish chat gets a reply-language line at the end of the prompt; English is
# byte-identical (v50).
# KRI-519: reaction beats on an iPhone Voiceover edit, worded for the voiceover (v51).
# KRI-525/526: pins may carry `start_s`/`end_s` or `clip`; the 4-line / 120-character limits are
# taught so an over-long ask becomes a question, not a schema failure (v52).
# KRI-598: no `execution_contract` where `guided_voiceover` is unavailable (runtime v2); on a
# Voiceover edit a cue for an own clip is `include`, never `order`, and beats persist (v55).
# KRI-614: a clip's `filename` (the name the app shows) identifies it when the creator writes it
# (v56).
# KRI-667: brief `facts` carry what an ask means in English keys and values (`triggers`,
# `keep_whole_take`, `closing`, `speech_cleanup`, `captions`, `caption_language`) whatever the
# creator's language, so the server checks them without re-reading the words (v58).
# v59 combines these facts with KRI-669 heading groups and relative sizes.
# KRI-700: a brighten / darken ask carries `footage_light`, so the server can say no draft
# changes the footage's light instead of the summary promising it (v60).
# v61 (KRI-674 live verification): per-clip camera sound is not spoken excerpts.
# KRI-793: a captions ask that lists words to spell records them in `spellings`, which the
# phone Talking writer applies to its captions and the render-ready review checks (v65).
# KRI-791: an on-screen text timed to a spoken word carries `triggers` too (it was taught only
# for photos, videos, stickers and sounds, so "Show MEROPE ... the first time she says Merope"
# arrived with no trigger), plus `speaker` when someone other than the creator says it.
# KRI-806: optional editorial plans pair only with licensed music and no kept clip sound.
# v67 retains caption spellings, spoken triggers/speaker and the editorial audio pairing rule.
# v66 combines both authored changes without losing either prompt instruction.
# v66 (KRI-792): named spoken cuts carry cut_lines; caption spellings remain intact.
# KRI-789: quoted start / end LINES inside a take are a `timing` trim (`start_line` /
# `end_line`), never an `order` requirement; route places keep `order` (v67).
# v68 combines caption spelling, spoken-text triggers/speaker, quoted trims and named cuts.
# v69 retains all shared brief instructions plus editorial audio pairing.
# v70 (KRI-851): the Talking reaction-beats section no longer forbids `opening_title`; a requested
# title / closing text still goes in `opening_title` / `closing_title` alongside the beats.
# KRI-814: a typed `delegates_all_choices` declaration for a message that states no requirement
# and hands every choice to Kria, so readiness need not match the stock sentence (v71).
# v72 (KRI-814): earlier creator lines never read into the brief are read with the current one.
# v74 combines delegation v72 with subtitle-semantics v73, retaining all instructions.
# v75 adds N2 fullscreen photo-then-original sequences to the complete v74 union.
# v76 (KRI-816): a typed `starts_fresh` declaration withdraws the saved brief ("Start fresh").
# v76 (KRI-815): `$voiceover_script_section` teaches `draft_voiceover_script` when footage is
# attached and no voiceover is (a script request returns a script, not a doomed narrated draft).
# v77 combines both v76 branches (KRI-816 starts_fresh + KRI-815 voiceover script section).
# v77 (KRI-827): "no text on her face or on the plate" => `keep_text_off` facts.
# v78 combines main's v77 (KRI-815 + KRI-816) with KRI-827's v77 (keep_text_off).
# v77 (KRI-821): "mute the camera / crowd sound" is a typed `source_muted` audio intent.
# v78 (KRI-821): a named cut of a repeated line records which take (`cut_line_occurrences`).
# v79 combines main's v77 (KRI-815 + KRI-816) with KRI-821's v78.
# v80 combines main's v79 (KRI-815/816/821) with KRI-827's keep_text_off (branch v78).
# KRI-724: the Nth time a quoted phrase is said carries `speech_remove` {quote, occurrence}
# beside its cut_lines, and an engine/motor noise ask carries `noise_reduction: "engine"`, so
# the phone Talking worker cuts / filters from facts, never from the words (KRI-667) (v76,
# on top of v75).
# v76 (KRI-840): Talking reaction beats can ask for a photo full screen at a word
# (`display_mode` "fullscreen"), and the shared brief records `display_mode`.
# v76 (KRI-841): `ending_clip` (a video added after the talk, with its own sound, Talking only) and
# the brief fact that records it next to `last_clip` (brief_extractor v21 shares the section).
# v76 (KRI-842) adds the `clip_sound` fact: one clip's own sound under, or after, a voiceover.
# KRI-847: a ceiling ("at most 15 seconds") carries `duration_bound: "at_most"` beside
# `duration_s`, so a shorter draft meets it instead of missing 15 s +/- 10 % (v76).
# v76 (KRI-849): "if you can't tell what he says, ask me instead of guessing" is a captions fact
# (`ask_if_unclear`), and the creator's later answer is `unclear_answer` ("omit" or their own
# words); the server compares two hearings of the speech and asks, it never reads the words.
# v92 (KRI-881): main's v91 plus a research / sources ask as a typed `text` requirement with
# `facts.research` ({topic, request_quote}); the server researches it before approval.
# v93 combines main v92 research-before-approval with runtime creative skills.
# v95 (KRI-689): the shared brief section records a clip-speed ask as `facts.playback_rate`
# (0.25-4; "slow motion" = 0.5, "speed up" = 2) on a `style` requirement, never for pacing /
# text / music or a speed ramp; the phone montage plans the rate onto the named clips.
# v96: exact-timed small cards (`timed_cards`, flag PHONE_TIMED_CARDS_ENABLED) on phone
# Montage / Voiceover edits, and the shared brief records a `timed_card` fact for the ask.
# v97 (KRI-945): the described-text exception covers text on particular clips only; an
# intro, explanation or facts for the whole video is never a clip intent.
# v98 (KRI-942): the shared brief records clip sound on / off (`clip_sound` "on"/"off" with
# `sound_clip`, `sound_from_s`/`sound_to_s`, `sound_line`, `sound_except`) for montages.
# v99 (KRI-957): an after-voice clip ask's `triggers` is the line said in that clip, never the
# creator's own last voiceover words ("after my last words \"before we eat\"").
# v100 (KRI-994): a select ask records the media kind it restricts to / includes
# (`only_media_kind` "video", `include_media_kind` "photo"), so the server can judge "clips
# only" on the finished timeline and withdraw an earlier photo ask a later clips-only ask replaces.
# v102 (29 Ekim lane D, KRI-1038): words shown a piece at a time are a `text_sequence`
# requirement, not `captions: word_by_word`; "say" plus a quote with no voiceover is on-screen
# text. (v101 is lane B's order-key edit; they merge in that order.)
# v103 (KRI-1027): the shared brief records how sideways clips sit in a vertical video
# (`landscape_fit` "fill" / "fit"), which preselects crop or black bars on the confirm screen.
# v104 (30 Ekim): prompt-cache layout only, no rule change. The per-turn INPUT block
# (creator context, footage, manifest, conversation, message) moved to
# prompts/main_creator_turn.txt at the end so the rules above it form a shared prefix.
# v105 (KRI-1077): title animation names come from the vocab registry (all designed edges).
# v106 (prompt fidelity, animation): an UNNAMED animation ask ("with animations", "animated
# text") is a `text` requirement carrying `animation: "any"`, not prose only; a named one
# keeps its id. One fact for both: the disclosure unit's interim `animated: true` fact is gone.
# v107 (KRI-1117): `background_media` (a photo / looping video behind the Talking speaker), taught
# only where the manifest advertises `speaker_background`; every other prompt is byte-identical.
# v108 (KRI-1093): the Lip-sync picker card (`manifest.chosen_format`) defaults an attached song
# to `lipsync`; the song section gains one paragraph only on that manifest, so every other
# render is byte-identical.
# v109 (KRI-1097 wave 2, planner boundary): `render_intent_evidence` gains `mixed_media_timing`,
# `montage_cadence` and `video_reuse_policy` quotes, taught only with
# PLANNER_BOUNDARY_HONEST_ENABLED; off, the prompt is byte-identical to v108 and a run reports
# v108 (`MainCreatorAgent.__init__`).
MAIN_CREATOR_PROMPT_VERSION = "2026-11-01-v109"
_PRE_BOUNDARY_PROMPT_VERSION = "2026-11-01-v108"

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
    " EXCEPTION -- described text on clips: when the creator asks for on-screen text ON"
    ' PARTICULAR CLIPS that they DESCRIBE rather than dictate ("the name of the dish on each'
    ' food clip", "a caption about the weather on the park clips"), the missing words are not'
    " yours to write and NOT a reason"
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
    ' "creator_text": null}. Text for the WHOLE video is never a `clip_intents` entry, even'
    " when described: an introduction, an explanation or facts about the place or topic"
    ' ("some facts about Lisbon to show why people should go, an introductory explanation")'
    " follows Researched explanations, and a hook is the opening title."
)

# KRI-1097 wave 2: the quotes that keep a typed timing, cadence or reuse ask the server's
# English/Turkish recognizers do not read (`_timing_cadence_reuse`). Appended to the
# `licensed_sfx` evidence line (the `$boundary_evidence_keys` slot) only with
# PLANNER_BOUNDARY_HONEST_ENABLED; "" off, so that prompt is byte-identical.
_BOUNDARY_EVIDENCE_KEYS = (
    ',\n      "mixed_media_timing": "the creator\'s whole sentence, first word to end mark, word'
    " for word in their language, asking for the photo and video timing you set (seconds and"
    ' their unit as they wrote them), or null",'
    '\n      "montage_cadence": "the creator\'s whole sentence, first word to end mark, word for'
    " word in their language, asking for the alternating cut and its seconds (repeating"
    ' footage needs its own video_reuse_policy sentence), or null",'
    '\n      "video_reuse_policy": "the creator\'s whole sentence, first word to end mark, word'
    ' for word in their language, asking to repeat, loop or alternate footage, or null"'
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
named are added on iPhone. A beat's photo shows as a small corner card unless the creator asks
for it full screen: only then set `display_mode` "fullscreen" ("show the grill photo full screen
when I say Argentinian", "'oblea' dediğinde fotoğraf tam ekran gelsin"); a small card, "in the
corner" or no mention is "pip" (omit it). Do this only when the manifest advertises
`media_overlays:fullscreen`; otherwise keep the photo a corner card and say so plainly in
`summary`. "For a moment", "briefly" or "bir an" means `hold_s` 1.0 to 1.5. A full-screen photo
at a spoken word is a beat with `display_mode` "fullscreen", never `overlay_display`
"fullscreen_images" (that spreads photos evenly, not at a word).
This edit always stays `edit_format: "subtitled"`. Beats never
replace a title: when the creator asks for a hook/opening title or closing text, still set
`opening_title` / `closing_title` by the title rules above, alongside the beats.
""".strip("\n")

# KRI-841: a video appended AFTER the talk, with its own sound, on a phone `subtitled` (Talking)
# edit. Appended to the `$reaction_beats_section` slot (the slot after `$clip_intents_section`, so
# "" adds no bytes) ONLY when `ending_clip_candidates(manifest)` names a video the phone can end
# on -- a Visuals video the device plays, or another footage clip with multi-clip Talking. The
# repair (`creator_capabilities.repair_creator_ending_clip`) never trusts the model's choice alone.
_ENDING_CLIP_PROMPT_SECTION = """
VIDEO AT THE VERY END (iPhone Talking only)
When the creator asks to ADD a video after the talk, at the very end, playing its own sound
("At the very end, add the cheers video with its own sound", "En sona Atakan'ın ilk ısırık
videosunu kendi sesiyle ekle"), set `ending_clip`: {"media_id": "<the matching media_context
VIDEO entry's exact media_id>", "keep_audio": true}. Find the video by matching what the creator
named against the VIDEO entries in `media_context` (its `filename`, its `creator_context`, or its
`analysis_only_not_copy` evidence), then copy THAT entry's exact `media_id`; never invent, shorten
or guess an id. It is a Visuals video or, when the project has several clips, one of the
project's own clips other than the one the creator speaks in (keep that clip in
`selected_media_ids` too). `keep_audio` is true unless the creator asks for it silent. The talk
plays to its end and this video follows it, so never also use that video in `reaction_beats`,
`closing_media` or as a cutaway: it plays only at the end, with its own sound. `closing_media`
stays for a photo held over the last seconds of the talk, and a plain "end on ..." about the talk
itself is not an `ending_clip`. Say in `summary`, in plain words, that the video is added after
the talk with its own sound.
""".strip("\n")

# KRI-1117: a photo or looping video shown BEHIND the speaker for the whole talk (a green screen
# without the green). Appended to the `$reaction_beats_section` slot ONLY when the manifest
# advertises `speaker_background`, so every other prompt stays byte-identical. The repair
# (`creator_capabilities.repair_creator_speaker_background`) never trusts the model's choice alone
# and writes the creator's camera pick in when the model leaves it out.
_SPEAKER_BACKGROUND_PROMPT_SECTION = """
PHOTO OR VIDEO BEHIND THE SPEAKER (iPhone Talking only)
When the creator asks to put a photo or video BEHIND them for the whole talk, like a green
screen ("put this photo behind me", "use the beach video as my background", "arkamda bu
fotoğraf dursun"), set `background_media`: {"media_id": "<the matching media_context entry's
exact media_id>"}. Find it by matching what the creator named against the IMAGE or VIDEO
entries in `media_context` (its `filename`, its `creator_context`, or its
`analysis_only_not_copy` evidence), then copy THAT entry's exact `media_id`; never invent,
shorten or guess an id, and never use one of the speaker's own clips. A video background plays
muted and loops; add `source_start_s` only when the creator names where it should start. When
the manifest carries `speaker_background_pick`, the creator already chose that background in the
camera: set `background_media` to that exact id unless the creator now names a different one.
A background is only the backdrop: never also use the same photo or video in `reaction_beats`,
`closing_media` or as a cutaway. A plain "background" about audio ("background music") is not
this. Say in `summary`, in plain words, that it sits behind them for the whole video.
""".strip("\n")

# Exact-timed small cards (flag PHONE_TIMED_CARDS_ENABLED) on a phone Montage / Voiceover
# edit. Appended to the `$reaction_beats_section` slot ONLY when the manifest advertises
# `timed_cards`; "" otherwise, so every other prompt stays byte-identical.
_TIMED_CARDS_PROMPT_SECTION = """
SMALL CARDS AT AN EXACT TIME (iPhone Montage and Voiceover)
When the creator wants a Visuals photo (or video) shown as a SMALL picture-in-picture card at a
stated moment of the finished video -- "show the menu photo as a small card at 3 seconds for 2
seconds", "put the receipt in the corner during the second clip", "menü fotoğrafı 3. saniyede 2
saniye küçük kart olarak gelsin" -- set `timed_cards`: up to 12 objects, each {"card_id":
"short-slug", "visual_id": "the matching media_context IMAGE (or VIDEO) entry's exact media_id",
"label": "the creator's own words for it, e.g. 'menu photo'", "at_s": seconds into the finished
video, or null, "clip_id": "the media_id of one of the project's own clips the card should show
during, or null", "offset_s": seconds after that clip starts (default 0), "hold_s": seconds on
screen (default 2.0, 0.5 to 8)}. Exactly one of `at_s` / `clip_id` per card. Find `visual_id`
exactly as for reaction beats: match what the creator named against `media_context` and copy
THAT entry's `media_id` verbatim; never invent or shorten an id. A photo timed to a SPOKEN word
("when I say X") is a reaction beat (where available), never a timed card; a photo that should
fill the whole screen or play as its own moment is a selected media / clip intent, never a timed
card. Say in `summary`, in plain words, where and for how long each small card shows.
""".strip("\n")

# KRI-519: the same beats on a phone Voiceover edit, grounded against the recorded
# voiceover's words. Shares the Talking section's field contract verbatim (sliced, so
# the two can never drift and the Talking prompt stays byte-identical); only the
# opening request and the closing edit rules differ.
_FIELDS_START = _REACTION_BEATS_PROMPT_SECTION.index("set `reaction_beats`:")
_FIELDS_END = _REACTION_BEATS_PROMPT_SECTION.index("Never set `licensed_sfx`")
_VOICEOVER_REACTION_BEATS_PROMPT_SECTION = (
    """
REACTION BEATS (iPhone Voiceover)
When the creator wants a photo/sticker or sound effect to pop up when their recorded voiceover
says something ("when I say the medal, show my medal photo", "show the watch photo at four
hours and twelve minutes"),
"""[1:]
    + _REACTION_BEATS_PROMPT_SECTION[_FIELDS_START:_FIELDS_END]
    + """
Triggers are matched against the words of the recorded voiceover, so copy the phrase exactly as
the creator quoted it. On this edit a photo or sticker timed to the voiceover is ALWAYS a
reaction beat: never set `execution_contract` for it, and never leave such a request out. It is
never a clip intent or a selected media id. Every proposal keeps each beat the FULL CREATOR
REQUEST CONTRACT still asks for, also when the latest message is about something else ("use all
7 clips"). A cue that names one of the project's own CLIPS instead ("when I say Deniz, show Deniz
at the laptop", "'mutfak' dediğimde mutfağı göster") is never a beat and never an `order`:
give each such clip one `clip_intents` entry with `op` "include" and `attribute` naming the clip
as the creator described it. Keep every own clip selected unless the creator limited them. Asking
the clips to follow the voiceover ("follow my voiceover: river, kitchen, attic") is that same
`include` per named clip. Only "start/end with X" is an `order` (with `position`), and only
another sequence the creator explicitly asks for ("in this order: A, B, C") is an `order`.
For a normal photo request, make its reaction beat `display_mode` "fullscreen" so it fills
the frame; use "pip" when the creator explicitly says small, corner, overlay, or
picture-in-picture. Stickers keep their normal small "pip" placement unless fullscreen is
explicitly requested. Never add a title, label, or other decorative text for this. When they ask
for a photo then one of their own clips ("when I say river, show the river photo first then the
river clip"), make the photo beat fullscreen with `occurrence` "first" and
`following_clip_id` equal to that selected original video's exact media id; also add the clip's
`include` intent, with the same actual quoted spoken cue in its attribute so the aligner selects
the right clip moment. `following_clip_id` is only for this photo-then-own-video relationship:
never use a Visuals video, an unselected video, or it for Deniz/end cues. A photo beat normally
fills the frame, then the edit continues with the voiceover footage. "End with Deniz" remains an
`order` intent with `position` "last", never a following clip. Titles are separate from the
voiceover subtitles: never turn a spoken cue into a title or add decorative text.
Never set `licensed_sfx` on this edit once this capability is available -- beats place sound at
the exact moments the creator named instead. A bare "add fun sound effects" with no named
spoken moment adds NO beats. Keep the edit's own voiceover `edit_format`.
""".strip("\n")
)


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


# KRI-1093: appended to the song section ONLY when the thread was started from the Lip-sync
# picker card (`manifest.chosen_format == "lipsync"`) and a song is usable; nothing otherwise.
_USER_SONG_LIPSYNC_CARD_SECTION = """
The creator picked the Lip-sync video type, which counts as asking to lip sync: choose
"lipsync" even when their words never say so. Choose "background" only when their words
clearly ask for the song as music behind the edit (for example "just use it as background
music", "keep it as the music bed"): their words win over the card. Always set `song_sync`
explicitly.
""".strip("\n")


# KRI-815: rendered into `$voiceover_script_section` (same line-suffix trick as the sections
# above) ONLY when the server says a script can be drafted (footage attached, no voiceover,
# flag on); "" otherwise, so every other prompt is byte-identical.
_VOICEOVER_SCRIPT_PROMPT_SECTION = """
VOICEOVER SCRIPT (footage attached, no voiceover recorded yet)
When the creator asks you to WRITE the words they will say over their clips (a voiceover or
narration script, "what should I say", "write me a script for these clips"), or asks to change
the script you drafted earlier in this conversation ("make it shorter", "say it in Turkish"),
answer with this action instead of `ask_user` or `propose_strategy`:
{"action": {"kind": "draft_voiceover_script", "request_evidence": "verbatim excerpt of the
creator's message asking for the script or the change", "revision_note": "the creator's
requested change to the earlier draft, verbatim, or null for a new script"}}
Words the creator asks to SHOW on screen, or a sentence they quote to display, are not a script
request: never offer to write or record them as a voiceover.
Do not write the script yourself: the server writes it from what the clips show. Do not
propose a narrated or voiceover edit before the creator has recorded or imported a voiceover;
that edit cannot render yet. Any other request (a montage, music, text, an edit that keeps the
clips' own sound) is planned exactly as usual.
""".strip("\n")


# Camera-audio guidance uses `$voice_mode_section` only on a phone source-audio
# manifest. Per-clip sound needs plan authority; the spoken modes additionally
# need the voice route. Other manifests keep the same prompt bytes.
_PER_CLIP_AUDIO_PROMPT_SECTION = """
PER-CLIP CAMERA AUDIO
When `montage_audio.preserve_source_audio` is true and the creator wants each picture clip's
own sound kept with that clip, set `voice_mode`: "per_clip" inside `strategy`. This includes
ambient sound, crowd noise, and speech simply kept with its own picture. For example,
"simple cuts with the original camera audio" means "per_clip"; nobody needs to be talking.
Keep `montage_audio.source_media_ids` when the creator names particular audio sources; never
invent or clear those ids to change the route. This mode does not select spoken quotes,
extract sentences, or put one clip's voice under other footage. Those are the spoken modes
below when available. Choose from what the creator asks for, not merely whether speech was
detected. Say plainly in `summary` that each clip keeps its own sound.
""".strip("\n")

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
for a talking-head or subtitled edit, or to cut to the speaker and back; names no particular
clip as the voice; or the request is ambiguous. Use "per_clip" for each clip's own sound with
its picture, "excerpts" for chosen lines, and null when the request is ambiguous. Never invent
a clip as the voice. For a spoken mode, say in `summary`, in plain words, which clip's voice
plays and whether it plays straight through.
""".strip("\n")


# KRI-188: Creative Brief extraction. Rendered into the `$brief_section` slot
# (appended to the clip-intents line, so "" adds no bytes) ONLY when
# `MainCreatorInput.brief_enabled` is true; flag off is byte-identical.
_BRIEF_PROMPT_SECTION = render_vocab_tokens(
    """
CREATIVE BRIEF
The FULL CREATOR REQUEST CONTRACT lists every requirement the creator has stated so far. In
ADDITION to `action`, return a top-level `brief_updates` list (at most 16 objects, in the same
JSON object as `action`) holding ONLY the requirements the CURRENT USER MESSAGE newly states or
changes -- never re-list a requirement that is already in the contract and unchanged. For a new
requirement use `operation`: "add" and include `kind`, `scope`, `literal`, `description`, and
`facts`. An add object is: {"operation":"add", "kind":
"text|order|select|timing|audio|style|text_sequence", "scope":
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

Preserve EVERY independently requested behavior, including modifiers and relationships between
parts of a compound request. An entrance animation does not describe segmentation, ordering,
overlap, duration, or when one text replaces another. Keep these instructions in the requirement's
complete description and structured facts; if they need separate verification, record a separate
style or timing requirement as well. Never shorten a compound request to the one field the
strategy happens to expose. Descriptions may retain behavior requiring composition of operations.
Do not duplicate an opening hook as a per-clip caption unless the creator requests both.

Captions and subtitles are the speech lane (Turkish: "altyazı") and only that; titles,
labels, callouts and decorative words are separate authored text. Record speech subtitles as a
`style` requirement with `facts.captions` and, when requested, `facts.caption_language`, never as
literal text to print. Keep its description about subtitles; record source-audio and
visual-cue instructions as separate requirements. "English subtitles; title X; no text
except the title" and
"İngilizce altyazı; başlık X; başlık dışında başka yazı olmasın" keep BOTH that subtitle
requirement and the title. Record the added-text restriction separately as a `style`
requirement with its complete description and null literal; the restriction itself is
not on-screen copy. Do not infer `captions: "off"`, remove/change a caption requirement,
or ask a contradiction question because added text is restricted. This also applies to
follow-up requests to remove extra text: preserve existing subtitle language and style.
Only an explicit request to remove captions/subtitles, or all writing INCLUDING
subtitles, turns off that speech-caption requirement. A named smaller heading under a
title remains authored text. Keep explicit wording, scope and later corrections intact.

Words shown on screen a piece at a time are NOT captions. When the creator quotes words, or
says show / display / title / write, and asks for them one word (or phrase) at a time ("show
each word one by one and say “…”", "yazıyı kelime kelime göster", "split it into pieces, word
by word"), record ONE `text_sequence` requirement: {"kind": "text_sequence", "scope": "title",
"literal": "<the quoted words, exactly as written>", "description": "<their ask>", "facts":
{"unit": "word", "mode": "agent_choice"}}. Keep the whole quoted sentence in `literal`, never
shortened or translated, and never record it as a `style` captions requirement. `unit` is
"phrase" when they ask for phrases or chunks. `mode` is "replace" when each piece takes the last
one's place, "cumulative" when the words stay and build up, otherwise "agent_choice": the editor
chooses and says which in its reply. A follow-up that points back at the words ("show each word
after the other in the text") keeps the earlier quoted words as `literal`. "One word at a
time" or "word by word" is a captions ask ONLY when it is about speech that exists in the
footage or a recorded voiceover, or the creator names captions, subtitles, karaoke or "altyazı".
"Say “…”" with a quoted line and no recorded voiceover means on-screen text, not speech. A
voiceover is only what the creator asks for with record, read aloud or voiceover; never offer
to record one for words the creator asked to see.

When a real requirement is present, ALWAYS record the structured facts that belong to it, even
when the same words also sit in a title or sentence: a distance, activity, start point, or end
point ("I ran 20K from Arnavutköy to Eminönü") go in `facts` as {"distance_km": 20,
"activity": "run", "start": "Arnavutköy", "end": "Eminönü"} on the requested text/order
requirement they describe. Never invent a requirement only to store background facts. ALWAYS add
an `order` requirement when the creator ASKS the edit to follow a sequence of clips or places
("put them in the order I filmed", "order them chronologically", "start the edit in Arnavutköy
and finish in Eminönü"): {"kind": "order", "scope": "global", "facts": {"key": "capture_time"}}
plus "start"/"end" when named. A line someone SAYS in the footage is never a clip order:
`Start at "Hello everyone" and end right after "see you tomorrow"` trims the speech, so it is
ONE `timing` requirement {"kind": "timing", "scope": "global", "facts": {"start_line": "Hello
everyone", "end_line": "see you tomorrow"}} with the quoted words exactly as the creator wrote
them (just `start_line` or just `end_line` when only one line is named), never an `order`
requirement and never `triggers`. Record it even when no length is stated; a stated length
stays its own `timing` requirement.
A request for no particular sequence is an `order` requirement too, with the key the server
checks: "show everything in random order", "shuffle them", "rastgele sırayla" =>
{"kind": "order", "scope": "global", "facts": {"key": "random"}}; "any order is fine", "sıra
fark etmez" => {"key": "any_order"}. Never put the creator's negation in that key: "keep my order,
not random" is the attachment order, not `random`. Showing every attached clip ("show all
content", "tüm içeriği göster") is a `select` requirement {"kind": "select", "scope": "global",
"facts": {"all": true}}, not a request to re-plan.
A clip the creator wants FIRST or LAST ("start with the video that is blue", "end on the
sunset") goes in that SAME order requirement as `first_clip` / `last_clip`: the creator's own
words for the clip ({"key": "capture_time", "first_clip": "the video that is blue"}); the
`description` alone is not enough. On a talking-to-camera edit (one speaker's take plays first), a
VIDEO the creator asks to ADD after the talk, at the very end, to play with its own sound ("at the
very end, add the cheers video with its own sound", "en sona Atakan'ın ilk ısırık videosunu kendi
sesiyle ekle") is not a clip order: record it as a `style` requirement with {"ending_clip": "<the
creator's words for that video>", "keep_audio": true} (`keep_audio` false only when they want it
silent), whatever language they write in, and never as `last_clip` or `closing`. On a montage,
adding or ending on a clip keeps `last_clip` as above. How on-screen text should look goes in the
`facts` of its text requirement, only what the creator said: `animation` (an id from
{{TEXT_ANIMATION_EDGES}}, never none or the name in brackets) for "animated with typewriter";
`position` ("top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right")
for "bottom left"; `placeholder` true when they ask for placeholder text to fill in later
("placeholder location"). Animation asked WITHOUT a name ("with animations", "animated text",
"make the text animate") is still a `text` requirement carrying the fact, never description
alone: {"kind": "text", "scope": "global", "facts": {"animation": "any"}} ("any" lets the
server pick the entrance; "explain why people should visit Assos, with animations" => that
text requirement for the explanation, animation "any"). A named one keeps its id ("fade in the
captions" => animation "fade"). Record it only when the creator asked for animation.
For requested heading stacks, preserve `heading_level` (1 title, 2 smaller subtitle,
3 subordinate detail), `stack_id` (shared only by explicitly connected lines), `below_text`
(exact predecessor words) and `smaller_than` (exact larger text words when smaller is asked)
in each line's facts. Never reduce "smaller" or "under it" to position alone. For "Gameday",
"Aston Villa v Fenerbahçe", "6 am" top left, emit levels 1/2/3 and stack_id "match";
the subtitle facts include below_text "Gameday" and smaller_than "Gameday", and the detail
facts include below_text "Aston Villa v Fenerbahçe". Keep the modifier in description too.
The server checks these asks from `facts`, not from the description, so ALWAYS record them
with these English keys and values, whatever language the creator writes in: a photo, video,
sticker, sound or on-screen text timed to spoken words ("show the medal photo when I say medal",
"her 'kahve' kelimesinde fincan sesi") => {"triggers": ["medal"]}, the spoken words exactly as
the creator wrote them, never translated, plus "speaker" (the creator's words for who) only when
someone other than the creator says them: "Show MEROPE in big letters the first time she says
Merope" => a text requirement, literal "MEROPE", facts {"triggers": ["Merope"], "speaker":
"she"}; a photo or video shown full screen ("tam ekran", "full screen") or as a small card
("in the corner") => add "display_mode": "fullscreen" or "pip" to those same facts, nothing when
the creator says neither; two photos shown differently in one message => ONE requirement per
photo, each with its own display_mode; keep a take whole, uncut =>
{"keep_whole_take": true}; end on a
particular shot, photo or badge => {"closing": "<the creator's words for it>"}; remove pauses,
filler sounds or retakes from the speech => {"speech_cleanup": true}, keep them =>
{"speech_cleanup": false}; cut out particular spoken words (a slip, a half question, a line, "the
part where I say ...") => {"speech_cleanup": true, "cut_lines": ["with bread, no,"]}, one item per
stretch in the creator's exact words, quoted or described, never translated; when they say
which time a repeated line to cut ("cut the FIRST take of 'welcome to my channel'", "the last
time I say ...") also add {"cut_line_occurrences": {"welcome to my channel": "first"}} with
"first", "second", "third" or "last", and keep that word in the description. For a numbered
occurrence also add {"speech_remove": {"quote": "Neredesin", "occurrence": 2}}: the quoted
words exactly as written, never translated, and the time it is said (1 = the first).
Quoted speech
bounds use the timing requirement's "start_line"/"end_line", never a cut line; ordered clips
or places still use the order requirement's "start"/"end". Captions =>
{"captions": "on"}, {"captions": "off"} or {"captions":
"word_by_word"} (karaoke: the words of speech that exists light up as they are said), plus
{"caption_language": "en"} (a two-letter code) when they name the captions' language
("Türkçe altyazı" => "tr"), plus {"spellings": ["Oblea de la Casa", "Merope"]} when they list
words or names to spell a certain way in the captions, each exactly as the creator wrote it
(never corrected or translated), plus
{"ask_if_unclear": "the third sauce he names"} (the creator's own words for WHICH word) when they
say they could not make out a word and want to be asked instead of having it guessed ("I didn't
catch the third sauce he names: if you can't tell what he says, ask me"), on the captions
requirement, or on a `style` requirement of its own when they asked for no captions; when they
later answer that question in their own words, record it on that same requirement as
{"unclear_answer": "omit"} for "leave it out" / "I don't know it either, leave that word out", or
{"unclear_answer": "<the exact word or words they typed>"} when they give the word, never
translated; brighten or darken
the footage itself ("brighten the dark clips", "klipleri aydınlat") => a `style` requirement with
{"footage_light": "brighter"} or {"footage_light": "darker"}; no edit can change the footage's
light yet, so never say in `summary` that it is brightened or darkened; how sideways
(horizontal) clips sit in a vertical video => a `style` requirement with {"landscape_fit": "fill"}
when the creator wants them cropped to fill the frame ("make it all vertical", "no black bars",
"hepsini dikey yap", "siyah bant olmasın") or {"landscape_fit": "fit"} when they want them whole
with black bars ("keep the black bars", "don't crop the clips", "siyah bantlar kalsın"); a
vertical camera move, vertical text or picking a clip filmed vertically records no
`landscape_fit`; keep text off something
in the frame ("no text on her face or on the plate", "yüzüme ve tabağa yazı gelmesin") => a
`style` requirement with {"keep_text_off": [{"kind": "face"}, {"kind": "object", "label":
"plate"}]}, each object in the creator's own words, never translated (the app asks the creator to
mark the object on a frame; never describe where it is yourself); a photo or video shown as a
SMALL card at an exact moment ("show the menu photo as a small card at 3 seconds for 2 seconds",
"put the receipt in the corner during the second clip", "menü fotoğrafı 3. saniyede 2 saniye
küçük kart olarak gelsin") => a `style` requirement with {"timed_card": {"visual": "<the
creator's own words for the photo or video>", "at_s": 3.0 or null, "clip": "<the clip they named,
in their words, or null>", "hold_s": 2.0 or null}}; a photo at a SPOKEN word is a pop-in, not a
timed_card. Play footage faster or
slower ("speed the river shots up 2x", "slow motion on the pour", "klipleri 2 kat hızlandır",
"time-lapse") => a `style` requirement with {"playback_rate": 2}: source seconds per output
second, 2 = twice as fast, 0.5 = half speed, between 0.25 and 4; "slow motion" / "ağır çekim"
with no number = 0.5, "speed up" / "time-lapse" with no number = 2. Scope it `clip:<media_id>`
when the creator names one attached clip, else "global" with the creator's words for the footage
kept in `description` ("the river shots"); the server picks the clips from those words. Never
`playback_rate` for pacing, cuts, text, music or a speed ramp (a change of speed inside one
clip): those keep their words in `description` only.
Keep the source/camera/ambient audio unchanged (including noun phrases such as "original camera
audio" or "kameranın özgün sesi") => an `audio` requirement with
{"audio_intent": "source_unchanged"}. This fact describes ONLY unprocessed source sound.
Mute or remove the footage's own sound (camera, crowd, ambient, original audio) so only the
voice is heard ("mute the crowd noise under my voice", "kamera sesini kapat") => an `audio`
requirement with {"audio_intent": "source_muted"}; "keep it low / quieter under my voice" is
not a mute and records no audio_intent.
Research, fact checking or sources for the on-screen explanations ("include sources for the
historical facts", "research it online", "kaynakları ekle") => ONE `text` requirement, scope
"global", the ask in `description`, with {"research": {"topic": "<a public topic, never a link or
file name>", "request_quote": "<the creator's exact words asking for research or sources, copied
verbatim>"}}. Never write the facts or sources yourself: the server researches before approval.
Record any requested processing, noise/wind removal, volume change, music or speech selection
as a SEPARATE requirement with its complete description; never certify those changes with
`source_unchanged`. Lowering or removing the engine/motor noise in the footage's own sound
("reduce the engine noise", "motor sesini kıs") is such an `audio` requirement with
{"noise_reduction": "engine"}; never for music, muting or keeping the noise. For "original
camera audio, remove only the wind noise", record both the unchanged-source intent and a
separate wind-removal requirement. After an explicit simpler
choice drops wind removal, preserve the source-audio requirement and its typed intent.
On a voiceover edit (the creator records their own voice over the clips), the creator may want
ONE clip's own sound kept: that clip's sound faintly under the voice => an `audio` requirement
scoped `clip:<media_id>` of THAT clip with {"clip_sound": "under_voice"} (only the named clips
play their own sound under the voice; the rest stay silent under it); a clip that plays on with
its own sound once the voice has ended, such as the last clip so a spoken line is heard =>
{"clip_sound": "after_voice", "triggers": ["bye bye"]}, the line exactly as written, no
`triggers` when none is named. `triggers` holds only a line said IN THAT CLIP: the creator's own
voiceover words that mark where the voice ends ("after my last words ...", "son sözümden sonra")
are never a trigger and are not recorded. Keep the creator's words in `description`; use these
English keys and values whatever the language: "Seslendirme net duyulsun ama ısırık klibinin
kendi sesi hafifçe altta kalsın" => scope clip:<the bite clip's media_id>,
{"clip_sound": "under_voice"}; "After my voiceover ends, let the last clip play so Caro's "bye
bye" is heard" => scope clip:<the last clip's media_id>, {"clip_sound": "after_voice",
"triggers": ["bye bye"]}; "After my last words "before we eat", play Atakan's "Super excited!"
with its own sound" => scope clip:<Atakan's clip's media_id>, {"clip_sound": "after_voice",
"triggers": ["Super excited!"]} ("before we eat" is the creator's voiceover, not the clip).
On a montage or any other edit that keeps the clips' own sound, turning that sound OFF or ON
for the whole video, a clip, a stretch of seconds or a spoken line => an `audio` requirement with
{"clip_sound": "off"} or {"clip_sound": "on"} plus where, only what the creator said: scope
`clip:<media_id>` for one attached clip they name (every time it plays); {"sound_clip": "last"}
or "first" for the last / first clip of the edit; {"sound_from_s": 5, "sound_to_s": 8} for seconds
of the video, or of that clip when one is named ("the first 2 seconds of the grill clip" => scope
clip:<the grill clip's media_id>, {"sound_from_s": 0, "sound_to_s": 2}); {"sound_line": "Super
excited!"} for only where that line is said, exactly as written, never translated. Parts that get
the opposite go in "sound_except", a list of objects with the same keys ("sound_media" names a
clip there): "Clip sound off except that last line" (the last clip's line is "Super excited!") =>
{"clip_sound": "off", "sound_except": [{"sound_clip": "last", "sound_line": "Super excited!"}]};
"sadece kasap klibinin sesi kalsın" => {"clip_sound": "off", "sound_except": [{"sound_media":
"<the butcher clip's media_id>"}]}; "mute the last clip" / "son klibin sesini kapat" =>
{"clip_sound": "off", "sound_clip": "last"}; "5. saniyeden 8. saniyeye kadar ses olmasın" =>
{"clip_sound": "off", "sound_from_s": 5, "sound_to_s": 8}. Lowering or raising the volume is not
on/off and records no `clip_sound`. A later message that moves where the sound is on or off
changes THIS requirement (`operation`: "change"); a message about anything else leaves it as it is.
Merely narrating that footage was captured "from A to B", at sunset and then at night, or during
two activities is not such an ask. Compatible requirements with the same kind and scope coexist.
ALWAYS add a `timing` requirement ({"kind": "timing", "scope": "global", "facts": {"duration_s":
N}}) in the SAME turn's `brief_updates` whenever the creator states a length for the video: any
number of seconds or minutes ("a 60 second montage", "about half a minute" => 30), converted to
seconds. Do it even when you also set `target_duration_s`, even for long
lengths, and even when the message carries other requirements ("all my clips", "in chronological
order"): never drop a stated number because other requirements are present. When the creator
states no length, emit NO timing requirement; a length you chose yourself in `target_duration_s`
is never the creator's. A ceiling is not an exact length: "under 15 seconds", "less than 15
seconds", "shorter than 15 seconds", "15 saniyenin altında", "at most 15 s", "max 15 s", "no more
than 15 seconds", "up to 15 seconds", "15 seconds or less", "en fazla 15 saniye" and "15 saniyeyi
geçmesin" all become {"duration_s": 15, "duration_bound": "at_most"}; every other length has no
`duration_bound` (omitting it means exact), and a change to a timing requirement must restate
`duration_bound` if it still applies.
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
Text the creator wants pinned in a corner or on screen for the whole video (see `pinned_texts`)
is recorded as one {"kind": "text", "scope": "global"} add per line with that line's exact words
as `literal`, never as `per_clip` or a `clip:` scope. When the creator's latest message simply
picks one of the alternatives you offered in your previous question, their original request has
changed to that alternative: record the superseded requirements with `change` or `remove`
(exact `target_requirement_id` and `expected_version` from the contract) and plan the chosen
alternative using the original request's exact words.
A `select` requirement about WHICH KIND of media the edit may use records one typed fact,
whatever language the creator wrote in. "Video clips only", "no photos", "sadece videolar" =>
{"kind": "select", "scope": "global", "facts": {"only_media_kind": "video"}}. A request to
INCLUDE a photo ("use my steak photo too", "fotoğrafı da kullan") => {"kind": "select",
"scope": "global", "facts": {"include_media_kind": "photo"}}. Add neither key to any other
select ask (which clips to use or skip, "only about the steak"), and add `only_media_kind` only
when the creator asks for video clips ONLY: "also use the steak photo, the rest can stay video"
is `include_media_kind` alone. When the creator withdraws an earlier ask ("forget the steak
photo"), `remove` that requirement as described above.
Also return a top-level boolean `delegates_all_choices`. Set it to true ONLY when the CURRENT
USER MESSAGE states no requirement at all and hands every creative choice to you ("Make
something great from my Troya trip", "Suggest an edit", "you decide", "Troya gezimden harika bir
şey yap", "Bana bir düzenleme öner"); `brief_updates` is then empty. Set it to false whenever the
message asks for anything specific (a title or other text, a length, an order, clips to use or
skip, music or sound, a look), even when it also says "suggest an edit" or "you decide the
rest", and whenever you are unsure. It never withdraws a requirement already in the contract.
When the contract is "Creative brief v0 (nothing recorded yet)" and lists earlier creator
messages, those messages were never read into the brief: treat them as part of the CURRENT USER
MESSAGE. Record every requirement any of them states as an `add`, and set
`delegates_all_choices` to true only when neither they nor the current message state any
requirement.
Also return a top-level boolean `starts_fresh`. Set it to true ONLY when the CURRENT USER
MESSAGE asks to start over and drop what it asked for before ("Start fresh", "start over from
scratch", "forget my earlier instructions", "Baştan başla", "sıfırdan yap"). The server then
withdraws every requirement in the contract, so record as an `add` each requirement the current
message itself states, even one the contract already had. Otherwise set it to false.
""".strip("\n")
)


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
    # KRI-520: the chat's language (app.kria.reply_language). None/"en" => the
    # prompt is byte-identical and the field is left out of input dumps.
    reply_language: str | None = Field(default=None, max_length=8, exclude_if=lambda v: v is None)
    # KRI-815: footage attached, no voiceover, script flag on. False => the prompt is
    # byte-identical, the field is left out of input dumps, and the action is refused.
    voiceover_script_available: bool = Field(default=False, exclude_if=lambda v: not v)


class MainCreatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: CreatorAgentOutput
    # KRI-188: requirements newly stated/changed by the current message. Empty
    # (and omitted from dumps) unless the brief is enabled for this creator.
    brief_updates: list[BriefUpdate] = Field(default_factory=list, exclude_if=lambda v: not v)
    # KRI-814: the extraction step's typed declaration that the current message states no
    # requirement and delegates every choice. Only ever True with no `brief_updates`; the
    # server, never the strategy or summary prose, turns it into an empty brief version.
    delegates_all_choices: bool = Field(default=False, exclude_if=lambda v: not v)
    # KRI-816: typed "start fresh": the planner withdraws every saved requirement.
    starts_fresh: bool = Field(default=False, exclude_if=lambda v: not v)
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
        model=settings.main_creator_model,
        fallback_models=("gemini-3.6-flash",)
        if settings.main_creator_model.startswith("gemini")
        else (),
        # KRI-118 item 4: bumped from 2 -> 3 alongside `schema_retry_limit`
        # below -- `_run_on_model`'s loop bounds EVERY retry path (transient/
        # refusal/schema) by `max_attempts`, so a `schema_retry_limit` above
        # `max_attempts - 1` is otherwise unreachable dead configuration.
        max_attempts=3,
        backoff_s=(2.0,),
        # Generation time grows with thinking + answer tokens: ~5.2 s + 7.3 ms
        # per token on an 18.8k-token manifest at thinking "high" (KRI-542 fit,
        # 2026-10-08; the latency model in tests/agents/test_thinking_budget.py).
        # The full `max_output_tokens` budget runs out (~124 s) before this
        # deadline, so a runaway call truncates (retried once at "low") instead
        # of ending outcome-unknown. A typical "high" call there takes 57-106 s.
        timeout_s=130.0,
        # KRI-1037: prod thread 729b3686 (27 clips) ended twice at the provider deadline
        # (91.5 s and 27.6 s left of a 180 s turn, 2k output tokens, so not a token limit)
        # and "Try again" repeated the same call. A deadline now steps thinking down to
        # "medium", then runs the fallback model at "low", inside the turn budget.
        degrade_on_deadline=settings.main_creator_deadline_ladder_enabled,
        # Reserve output capacity for the full source manifest.
        thinking_level=settings.main_creator_thinking_level,
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
    # thought 3,047 and failed at MAX_TOKENS on 2026-09-24). At thinking "high"
    # (PR 1485) an 18.8k-token manifest thinks 5.9k-13k tokens (KRI-542, prod
    # thread 74dfc456: 8,192 left 315 for the plan and the turn dead-ended; five
    # 16k-budget replays thought 5,878 / 7,603 / 8,540 / 12,956 / ~7.7k). 16,384
    # fits the heaviest observed call with ~2.5k to spare; past it the runtime
    # retries once at "low".
    max_output_tokens = 16_384

    def __init__(self, model_client: ModelClient) -> None:
        super().__init__(model_client)
        if not settings.planner_boundary_honest_enabled:
            # KRI-1097 wave 2: off, the rendered prompt is v108's byte for byte; runs say so.
            self.spec = replace(type(self).spec, prompt_version=_PRE_BOUNDARY_PROMPT_VERSION)

    def required_fields(self) -> list[str]:
        return ["action"]

    def render_prompt(self, input: MainCreatorInput) -> str:  # noqa: A002
        # Storage identity is needed for the confirmation/execution fence but
        # is not useful creative context and must not be shown to the model.
        prompt_manifest = input.capability_manifest.model_dump_json(
            exclude_none=True,
            exclude={"narration": True},
        )
        # Cache-friendly order: fixed rules and flag/manifest sections first, per-turn
        # data last, so Gemini's implicit prefix cache can reuse the long shared head.
        prompt = load_prompt(
            "main_creator",
            # Renders to "" (flag off) on the one blank template line it
            # occupies, so the rest of the prompt is untouched byte-for-byte.
            clip_intents_section=(
                _CLIP_INTENTS_PROMPT_SECTION if settings.clip_intents_enabled else ""
            ),
            described_text_exception=(
                _DESCRIBED_TEXT_EXCEPTION if settings.clip_intents_enabled else ""
            ),
            boundary_evidence_keys=(
                _BOUNDARY_EVIDENCE_KEYS if settings.planner_boundary_honest_enabled else ""
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
            # there is real content to show. KRI-519: a Voiceover manifest gets
            # the Voiceover wording of the same contract.
            reaction_beats_section=(
                (
                    "\n" + _reaction_beats_section(input.capability_manifest)
                    if _reaction_beats_available(input.capability_manifest)
                    else ""
                )
                # KRI-841: shares the slot, so a manifest with no video to end on is unchanged.
                + (
                    "\n" + _ENDING_CLIP_PROMPT_SECTION
                    if ending_clip_candidates(input.capability_manifest)
                    else ""
                )
                # Exact-timed small cards: same slot, "" unless the manifest advertises them.
                + (
                    "\n" + _TIMED_CARDS_PROMPT_SECTION
                    if _timed_cards_available(input.capability_manifest)
                    else ""
                )
                # KRI-1117: a photo / looping video behind the speaker; "" unless advertised.
                + (
                    "\n" + _SPEAKER_BACKGROUND_PROMPT_SECTION
                    if speaker_background_open(input.capability_manifest)
                    else ""
                )
            ),
            # KRI-188: "" (flag off) adds no bytes; same line-suffix trick.
            brief_section=("\n" + _BRIEF_PROMPT_SECTION if input.brief_enabled else ""),
            # KRI-374: "" (no usable song) adds no bytes; same line-suffix trick.
            user_song_section=(
                "\n"
                + _USER_SONG_PROMPT_SECTION
                + (
                    "\n" + _USER_SONG_LIPSYNC_CARD_SECTION
                    if input.capability_manifest.chosen_format == "lipsync"
                    else ""
                )
                if input.capability_manifest.has_user_song
                else ""
            ),
            # KRI-479: "" (no `phone_source_audio`) adds no bytes; same line-suffix trick.
            voice_mode_section=_camera_audio_mode_section(input.capability_manifest),
            # KRI-815: "" (no script possible) adds no bytes; same line-suffix trick.
            voiceover_script_section=(
                "\n" + _VOICEOVER_SCRIPT_PROMPT_SECTION if input.voiceover_script_available else ""
            ),
        )
        turn = load_prompt(
            "main_creator_turn",
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
        )
        prompt = (
            load_skill("edit-story")
            + "\n\n"
            + load_skill("grounded-hook")
            + "\n\n"
            + load_skill("meaningful-moment")
            + "\n\n"
            + prompt.rstrip("\n")
            + "\n\n"
            + turn
        )
        if input.reply_language == "tr":
            prompt += (
                "\n\nFor newly authored audience-facing hook/title copy only "
                "(not the chat reply): " + turkish_audience_instruction("tr")
            )
        # KRI-520: the reply-language instruction goes last, where it wins over the
        # English examples above. "" for English/unknown: byte-identical prompt.
        language_line = prompt_language_line(input.reply_language)
        return f"{prompt.rstrip(chr(10))}\n\n{language_line}\n" if language_line else prompt

    def parse(self, raw_text: str, input: MainCreatorInput) -> MainCreatorOutput:  # noqa: A002
        self._schema_feedback = ""
        try:
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            data = _hoist_misplaced_declarations(data)
            data = _hoist_misplaced_brief_updates(data)
            data = _repair_creative_decision(data)
            raw_action = _repair_action_envelope(data.get("action"))
            raw_action = _repair_story_structure(raw_action)
            if isinstance(raw_action, dict) and raw_action.get("kind") == "propose_strategy":
                raw_strategy = raw_action.get("strategy")
                if not isinstance(raw_strategy, dict) or "target_duration_s" not in raw_strategy:
                    raise ValueError("propose_strategy must explicitly choose target_duration_s")
                rationale = raw_strategy.get("rationale")
                if not isinstance(rationale, str) or not rationale.strip():
                    # KRI-586: the model sometimes writes it beside `strategy`.
                    sibling = raw_action.get("rationale")
                    if isinstance(sibling, str) and sibling.strip():
                        raw_strategy = {**raw_strategy, "rationale": sibling}
                        raw_action = {
                            **{k: v for k, v in raw_action.items() if k != "rationale"},
                            "strategy": raw_strategy,
                        }
                    else:
                        raise ValueError("propose_strategy must explain its duration in rationale")
            honest = bool(settings.planner_boundary_honest_enabled)
            raw_action, boundary_quotes = (
                _take_boundary_quotes(raw_action) if honest else (raw_action, {})
            )
            unknown_looks = _unknown_text_looks(raw_action) if honest else []
            action = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
                raw_action, context={PLANNER_BOUNDARY_DECLINES_CONTEXT: honest}
            )
            if isinstance(action, DraftVoiceoverScript) and not input.voiceover_script_available:
                # KRI-815: only offered with footage and no voiceover; elsewhere it is a miss.
                raise ValueError("draft_voiceover_script is not available for this project")
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
                requested_duration_s = creator_duration_s(
                    creator_texts=[*user_messages, request_contract]
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
                reuse_policy = recognize_explicit_cadence_reuse_policy(
                    input.user_message
                ) or recognize_cadence_reuse_policy(combined_request)
                if cadence_cut_s is not None and len(videos) == 2:
                    cadence = MontageCadenceConstraint(
                        source_media_ids=[media.media_id for media in videos],
                        cut_duration_s=cadence_cut_s,
                        reuse_policy=reuse_policy,
                    )
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
                boundary_kwargs = {
                    "timing": timing,
                    "regex_cadence": cadence,
                    "reuse_messages": [request_contract, *user_messages, input.user_message],
                    "latest_message": input.user_message,
                    "cadence_reuse": reuse_policy,
                }
                server_reads = _timing_cadence_reuse(action.strategy, **boundary_kwargs)
                reads = (
                    _timing_cadence_reuse(
                        action.strategy,
                        **boundary_kwargs,
                        quoted=_creator_quote(
                            boundary_quotes,
                            [*user_messages, request_contract, input.user_message],
                            action.strategy,
                        ),
                    )
                    if honest
                    else server_reads
                )

                def finish(reads: dict[str, Any]) -> CreativeStrategy:
                    strategy = action.strategy.model_copy(
                        update={
                            # Server-owned approval provenance.  Read this from
                            # creator text, never from the model's always-present
                            # planning field or its marker.
                            "target_duration_requested": (
                                True if requested_duration_s is not None else None
                            ),
                            "creator_duration_s": requested_duration_s,
                            # Pace provenance is server-owned too; the route re-stamps it from
                            # the creator's words (`_apply_explicit_render_intent`).
                            "pacing_requested": None,
                            "selected_music_section": None,
                            # Preserve the actual requested target rather than a
                            # conflicting model-selected duration.
                            "target_duration_s": (
                                requested_duration_s
                                if requested_duration_s is not None
                                else action.strategy.target_duration_s
                            ),
                            # KRI-374: server-owned, like `resolved_clip_intents`: a
                            # model-authored per-take song placement is never trusted.
                            "resolved_song_takes": None,
                            "selection_reconciliation": None,
                            "confirmed_order_replacement": None,
                            # KRI-578: the caption pin is the planner's call, never the model's.
                            "song_captions": None,
                            # KRI-476: server-owned conflict answers, never model-authored.
                            "choice_answers": None,
                            **reads,
                            "media_scope": resolved_scope,
                        }
                    )
                    if strategy.execution_contract is not None and native_voiceover_only(
                        input.capability_manifest
                    ):
                        # KRI-598: this manifest (runtime v2) never offers the guided contract.
                        # Repair, like KRI-586's envelope repairs, instead of refusing: every
                        # refusal spends a retry and three end the turn. The planner's policy
                        # check then resolves "all" to every own clip and says what the native
                        # voiceover edit leaves out.
                        log.info(
                            "main_creator_contract_dropped_native_only",
                            media_scope=strategy.media_scope,
                            reaction_beats=len(strategy.reaction_beats or ()),
                        )
                        strategy = strategy.model_copy(update={"execution_contract": None})
                    strategy = _repair_unrecorded_voiceover(
                        input.capability_manifest,
                        strategy,
                        source_audio_stated=input.brief_enabled
                        and _states_source_audio(
                            data.get("brief_updates"),
                            input.creator_request,
                            starts_fresh=data.get("starts_fresh") is True,
                        ),
                    )
                    strategy = normalize_creator_strategy_media(
                        input.capability_manifest,
                        strategy,
                        repair_model_output=True,
                    )
                    if strategy.editorial_plan is not None:
                        misfit = _editorial_plan_misfit(input.capability_manifest, strategy)
                        if misfit:
                            # KRI-806: the story plan is optional and only runs on a music-led
                            # phone montage of the selected clips. Repair, like KRI-598, instead
                            # of refusing: each refusal spends one of three attempts (Don Julio
                            # M1 lost its last one here). Counts only: beats carry creator
                            # wording.
                            log.info(
                                "main_creator_editorial_plan_dropped",
                                reasons=misfit,
                                beats=len(strategy.editorial_plan.beats),
                                text_goals=sum(
                                    1 for beat in strategy.editorial_plan.beats if beat.text_goal
                                ),
                            )
                            strategy = strategy.model_copy(update={"editorial_plan": None})
                    return strategy

                unrenderable: list[str] = []
                try:
                    strategy = finish(reads)
                except ValueError:
                    if reads == server_reads:
                        raise
                    # KRI-1097 wave 2: a quoted ask this manifest cannot render falls back to
                    # the server's own reading (what the parse kept before) instead of
                    # spending a retry, and the card names it (`planner_declines`).
                    unrenderable = [f for f in reads if reads[f] != server_reads[f]]
                    log.info("main_creator_quoted_ask_unrenderable", fields=unrenderable)
                    strategy = finish(server_reads)
                declines = [*unknown_looks, *(PlannerDecline(field=f) for f in unrenderable)]
                strategy = strategy.model_copy(update={"planner_declines": declines or None})
                action = action.model_copy(update={"strategy": strategy})
            brief_updates = (
                _refile_text_sequences(
                    _refile_take_trims(parse_brief_updates(data.get("brief_updates")), input),
                    input,
                )
                if input.brief_enabled
                else []
            )
            return MainCreatorOutput(
                action=action,
                creative_decision=(
                    CreativeCopyDecision.model_validate(data["creative_decision"])
                    if data.get("creative_decision") is not None
                    else None
                ),
                brief_updates=brief_updates,
                # KRI-814: a declaration beside stated requirements contradicts itself; fail
                # closed (the requirements stand, nothing is recorded as delegated).
                delegates_all_choices=input.brief_enabled
                and data.get("delegates_all_choices") is True
                and not brief_updates,
                starts_fresh=input.brief_enabled and data.get("starts_fresh") is True,
            )
        except ValidationError as exc:
            # Tell the retry which contract fields failed, without echoing
            # private field values or the model's full response into logs.
            self._schema_feedback = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                for error in exc.errors(include_input=False, include_context=False)[:8]
            )[:1000]
            if exc.title == "CreativeCopyDecision":
                # KRI-586: its errors carry no parent path, so the retry could not tell which
                # object was wrong. Name it and the exact shape.
                self._schema_feedback = (
                    "creative_decision (a top-level key beside action, never inside it): "
                    + self._schema_feedback
                    + "; allowed keys only: target (opening_title|closing_title), status "
                    "(unresolved|delegated|candidate|creator_supplied|cancelled), "
                    "proposed_text, source_evidence, language"
                )
            error_class = _schema_error_class(exc)
            if any(
                "opening_title_duration_s" in map(str, error["loc"])
                for error in exc.errors(include_input=False, include_context=False)
            ):
                # KRI-523: a title hold is capped; text that must stay the whole video
                # is a different field, so the retry names it instead of looping.
                self._schema_feedback += (
                    "; opening_title_duration_s is a short hold (max "
                    f"{MAX_OPENING_TITLE_DURATION_S:g}s): text that must stay on screen the "
                    "whole video belongs in pinned_texts, not in a longer title hold"
                )
            if any(
                "pinned_texts" in map(str, error["loc"])
                for error in exc.errors(include_input=False, include_context=False)
            ):
                # KRI-525/526: name the pin rules the retry broke (a range is seconds OR a
                # clip, start before end, and the line limits) instead of a bare error type.
                self._schema_feedback += (
                    "; pinned_texts: at most 4 lines of at most 120 characters, each with "
                    "either start_s/end_s (start before end) or clip, never both"
                )
            raise _classed_schema_error(
                f"main_creator: invalid output: {exc}", error_class
            ) from exc
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
                if _non_pydantic_error_class(exc) in _FORMAT_REFUSAL_CLASSES:
                    # KRI-816 T06: name the formats that CAN render, so the retry picks
                    # one instead of spending every attempt on the same refusal.
                    renderable = _renderable_edit_formats(input.capability_manifest)
                    if renderable:
                        self._schema_feedback += (
                            "; edit_format values that can render here: " + ", ".join(renderable)
                        )
            raise _classed_schema_error(
                f"main_creator: invalid output: {exc}", _non_pydantic_error_class(exc)
            ) from exc

    def schema_clarification(self) -> str:
        feedback = getattr(self, "_schema_feedback", "")
        return (
            "\nReturn only the documented JSON envelope with one valid action object."
            + (f"\nCorrect these schema errors: {feedback}." if feedback else "")
            + " Use only documented fields, exact enum values, and #RRGGBB colors."
        )


def _timing_cadence_reuse(
    strategy: CreativeStrategy,
    *,
    timing: MixedMediaTimingProfile | None,
    regex_cadence: MontageCadenceConstraint | None,
    reuse_messages: list[str],
    latest_message: str,
    cadence_reuse: Literal["no_repeat", "allow_repeat"],
    quoted: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """`mixed_media_timing`, `montage_cadence` and `video_reuse_policy` at the parse boundary.

    The recognizers' explicit reading of the creator's words always wins. ``quoted`` None is
    the server's reading alone: a model value they do not read is dropped. With ``quoted``
    (``PLANNER_BOUNDARY_HONEST_ENABLED``, KRI-1097 wave 2) the model's own value stays where the
    recognizers are silent and the model's quote of the creator's words grounds it
    (`_creator_quote`), the rule `media_scope` follows (KRI-129 part C). An ungrounded value is
    no creator ask, so the model still cannot grant itself repeats or a timing nobody asked for.
    A quote grounds only its own field: a timing quote keeps the photo and video holds, never
    grouping the model read elsewhere, and a cadence quote keeps the cut, never the model's
    permission to repeat footage (``cadence_reuse``, the server's reading, stands unless the
    repeat ask is grounded by its own quote). A latest-turn "stop alternating" keeps any cadence
    out.
    """
    keep = quoted or (lambda _field: False)
    if timing is None and strategy.mixed_media_timing is not None and keep("mixed_media_timing"):
        model_timing = strategy.mixed_media_timing
        timing = MixedMediaTimingProfile(
            image_hold=model_timing.image_hold,
            image_hold_s=model_timing.image_hold_s,
            video_hold=model_timing.video_hold,
            boundary_style=model_timing.boundary_style,
        )
    cadence = regex_cadence
    if (
        cadence is None
        and strategy.montage_cadence is not None
        and not rejects_round_robin_cadence(latest_message)
        and keep("montage_cadence")
    ):
        cadence = strategy.montage_cadence
        if not keep("video_reuse_policy"):
            cadence = cadence.model_copy(update={"reuse_policy": cadence_reuse})
    reuse = (strategy.video_reuse_policy if keep("video_reuse_policy") else None) or "once"
    for message in reuse_messages:
        reuse = resolve_video_reuse_policy(message, reuse)
    reuse = resolve_video_reuse_policy(latest_message, reuse, cadence)
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
    return {"mixed_media_timing": timing, "montage_cadence": cadence, "video_reuse_policy": reuse}


# KRI-1097 wave 2: the `render_intent_evidence` quotes for the three boundary fields. With the
# switch on they leave the raw action before validation and only `_creator_quote` reads them;
# off they stay, and the evidence model refuses them as unknown keys, as the foundation did.
_BOUNDARY_QUOTE_FIELDS = ("mixed_media_timing", "montage_cadence", "video_reuse_policy")

# Characters that end a sentence in any script. A boundary quote starts and ends at one of them
# (or at the edge of a message), so it never leaves out words of its own sentence: a negation
# before it reaches the quoted words across commas, colons and asides ("No quiero que, como la
# última vez, las fotos ..."), and a later clause can turn them around. Punctuation, not words:
# what the sentence means is the model's reading (the language policy, KRI-773).
_SENTENCE_MARKS = frozenset(".;!?\n…。；！？؛؟।")
# Quotation marks, brackets and opening marks stepped over on the way to a sentence edge.
_SENTENCE_WRAPPERS = frozenset("\"'«»“”„‘’‚()[]¿¡ ")
# How a seconds unit is written right after the digits ("2 s", "1.5-second", "2 segundos",
# "2 saniyede"): notation read like the digits themselves, never what a sentence asks for.
_SECOND_UNITS = frozenset(
    {
        "s",
        "sec",
        "secs",
        "second",
        "seconds",
        "seg",
        "segs",
        "segundo",
        "segundos",
        "seconde",
        "secondes",
        "secondo",
        "secondi",
        "sek",
        "sekunde",
        "sekunden",
        "sn",
        "sny",
        "saniye",
        "saniyede",
        "saniyelik",
        "saniyeden",
        "saniyeye",
        "saniyeyi",
        "saniyesi",
    }
)


def _take_boundary_quotes(raw_action: object) -> tuple[object, dict[str, str]]:
    """``raw_action`` without the boundary quotes in its `render_intent_evidence`, and the
    string quotes it carried."""
    if not isinstance(raw_action, dict):
        return raw_action, {}
    evidence = raw_action.get("render_intent_evidence")
    if not isinstance(evidence, dict) or not any(f in evidence for f in _BOUNDARY_QUOTE_FIELDS):
        return raw_action, {}
    quotes = {
        field: value
        for field, value in evidence.items()
        if field in _BOUNDARY_QUOTE_FIELDS and isinstance(value, str)
    }
    rest = {key: value for key, value in evidence.items() if key not in _BOUNDARY_QUOTE_FIELDS}
    return {**raw_action, "render_intent_evidence": rest}, quotes


def _sentence_edge(text: str, index: int, step: int) -> bool:
    """Whether the slice boundary ``index`` of ``text`` is a sentence edge, looking left
    (``step`` -1) or right (+1): the edge of the text, or a sentence mark past only wrappers. A
    point between two digits is a decimal point ("0.5"), not an edge."""
    at = index - 1 if step < 0 else index
    while 0 <= at < len(text) and text[at] in _SENTENCE_WRAPPERS:
        at += step
    if not 0 <= at < len(text):
        return True
    mark = text[at]
    if mark not in _SENTENCE_MARKS:
        return False
    decimal = mark == "." and 0 < at < len(text) - 1
    return not (decimal and text[at - 1].isdecimal() and text[at + 1].isdecimal())


def _whole_sentences_in(source: str, quote: str) -> bool:
    """Whether ``quote`` is found in ``source`` (spacing aside) as one or more whole
    sentences. A quote that carries its own closing mark ("... 2 segundos.") ends one."""
    lines = [" ".join(line.split()) for line in source.splitlines()]
    text = "\n".join(line for line in lines if line)
    flat = text.replace("\n", " ")
    start = flat.find(quote)
    while start >= 0:
        end = start + len(quote)
        closed = _sentence_edge(text, end, 1) or _sentence_edge(text, end, -1)
        if closed and _sentence_edge(text, start, -1):
            return True
        start = flat.find(quote, start + 1)
    return False


def _unit_after(text: str, index: int) -> str:
    """The word starting at ``index`` past spaces and hyphens, casefolded ("" for none)."""
    while index < len(text) and text[index] in " -\u00a0":
        index += 1
    end = index
    while end < len(text) and text[end].isalpha():
        end += 1
    return text[index:end].casefold()


def _seconds_written(text: str) -> list[float]:
    """The numbers written with digits and a seconds unit right after them in ``text`` ("2
    segundos", "0,5 s", "1.5-second"); the 2 of "Somos 2 amigos" is none."""
    out: list[float] = []
    run = ""
    for index, char in enumerate(f"{text} "):
        if char.isdecimal():
            run += char
        elif char in ".," and run and text[index + 1 : index + 2].isdecimal() and "." not in run:
            run += "."
        elif run:
            if _unit_after(text, index) in _SECOND_UNITS:
                out.append(float(run))
            run = ""
    return out


def _value_seconds(field: str, strategy: CreativeStrategy) -> list[float]:
    """The seconds the model wrote for ``field``: a cadence's cut, a photo hold."""
    if field == "montage_cadence" and strategy.montage_cadence is not None:
        return [strategy.montage_cadence.cut_duration_s]
    timing = strategy.mixed_media_timing
    if field == "mixed_media_timing" and timing is not None and timing.image_hold_s is not None:
        return [timing.image_hold_s]
    return []


def _creator_quote(
    quotes: Mapping[str, str], creator_texts: list[str], strategy: CreativeStrategy
) -> Callable[[str], bool]:
    """Whether a field's boundary quote grounds the model's value (KRI-1097 wave 2).

    Language-neutral checks only (the language policy, KRI-773/786: the model that reads the
    message records its meaning, the server checks the record and never guesses meaning from
    words):
    - the quote is the creator's own words, two words or more found verbatim (spacing aside)
      in what they wrote, the rule the title and pinned-text quotes follow;
    - it is whole sentences of that text, never a part, so it cannot leave out words of its own
      sentence, even across commas and asides ("las fotos pasen muy rápido" out of "No quiero
      que, como la última vez, las fotos pasen muy rápido", "montaje lento" out of "Crea un
      montaje lento del verano en Madrid");
    - each number of seconds the model wrote (a cadence's cut, a photo hold) is written in it
      with digits and a seconds unit right after them: "cada 2 segundos" grounds no 1 s cut,
      and "Somos 2 amigos" no 2 s photo hold.
    Each check fails toward the server's own reading, the foundation's behaviour.
    """

    def quoted(field: str) -> bool:
        quote = " ".join(str(quotes.get(field) or "").split())
        if len(quote.split()) < 2:
            return False
        written = _seconds_written(quote)
        if any(
            all(abs(value - number) > 0.02 for number in written)
            for value in _value_seconds(field, strategy)
        ):
            return False
        return any(_whole_sentences_in(str(text or ""), quote) for text in creator_texts)

    return quoted


# A title animation / label position value that asks for no look at all.
_LOOK_OFF_WORDS = frozenset({"none", "null", "no", "off", "default", "static"})


def _unknown_text_looks(raw_action: object) -> list[PlannerDecline]:
    """Title animation / label position words the renderer has no name for (KRI-1097 wave 2).

    The strategy's validators read such a word as unset, the default look. The model fills
    these fields only when the creator named a look, so the word is kept for the card to say
    it is not used instead of vanishing. A known word, an empty value or an "off" word is none.
    """
    strategy = raw_action.get("strategy") if isinstance(raw_action, dict) else None
    if not isinstance(strategy, dict):
        return []
    out: list[PlannerDecline] = []
    for field, known in (
        ("title_animation", normalize_title_animation),
        ("label_position", normalize_label_position),
    ):
        value = strategy.get(field)
        word = " ".join(value.split()) if isinstance(value, str) else ""
        if not word or word.casefold() in _LOOK_OFF_WORDS or known(word) is not None:
            continue
        out.append(PlannerDecline(field=field, value=word[:80]))
    return out


def _editorial_plan_misfit(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> list[str]:
    """KRI-806: why `strategy.editorial_plan` cannot run (empty = it can).

    Only a music-led phone montage of video clips orders footage by the plan, and the
    plan must bind exactly the clips the edit uses: the creator's selection (after media
    normalisation), else every clip -- the set the worker checks it against again.
    """

    from app.services.editorial_plan import editorial_order

    montage = manifest.capabilities.get("phone_format:montage")
    reasons = [
        reason
        for reason, misfit in (
            ("edit_format", strategy.edit_format != "montage"),
            ("phone_montage_unavailable", montage is None or not montage.available),
            ("voiceover", manifest.has_voiceover),
            ("non_video_media", any(media.kind != "video" for media in manifest.media)),
            ("audio_strategy", strategy.audio_strategy != "licensed_music"),
            ("montage_cadence", strategy.montage_cadence is not None),
            ("montage_audio", strategy.montage_audio is not None),
        )
        if misfit
    ]
    if reasons:
        return reasons
    try:
        editorial_order(
            strategy.editorial_plan,
            strategy.selected_media_ids
            or [media.media_id for media in manifest.media if media.kind in {"video", "image"}],
        )
    except ValueError:
        return ["coverage"]
    return []


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


def _camera_audio_mode_section(manifest: ResolvedCreatorManifest) -> str:
    capability = manifest.capabilities.get("phone_source_audio")
    if capability is None or not capability.available:
        return ""
    sections = []
    # Per-clip sound uses the ordinary montage renderer, not the speech lane.
    if settings.kria_plan_authority_enabled:
        sections.append(_PER_CLIP_AUDIO_PROMPT_SECTION)
    if _voice_mode_available(manifest):
        sections.append(_VOICE_MODE_PROMPT_SECTION)
    return "\n" + "\n\n".join(sections) if sections else ""


def _voice_mode_available(manifest: ResolvedCreatorManifest) -> bool:
    """KRI-479: teach spoken modes only while the phone voice route is available.

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


def _timed_cards_available(manifest: ResolvedCreatorManifest) -> bool:
    """Whether the SMALL CARDS AT AN EXACT TIME guidance is shown: a read of the manifest's
    own `timed_cards` capability (`creator_capabilities.resolve_creator_manifest` owns the
    flag / format / device gate; `repair_creator_timed_cards` never trusts the model alone)."""

    capability = manifest.capabilities.get(CAPABILITY_TIMED_CARDS)
    return bool(capability is not None and capability.available)


def _reaction_beats_section(manifest: ResolvedCreatorManifest) -> str:
    """KRI-519: the beats guidance worded for the edit that advertises them --
    the capability resolver only opens it on a phone Talking or a phone
    Voiceover (recorded voiceover) manifest."""

    if manifest.has_voiceover and manifest.edit_format in NARRATED_EDIT_FORMATS:
        return _VOICEOVER_REACTION_BEATS_PROMPT_SECTION
    return _REACTION_BEATS_PROMPT_SECTION


def _repair_action_envelope(action: object) -> object:
    """Repair only the known harmless nested-field envelope typos.

    Some model responses put the proposal `summary` (a string) or
    `render_intent_evidence` (an object) inside ``strategy`` although the
    documented envelope puts both beside ``strategy`` (the evidence one was
    seen on a phone Talking "Add captions" turn, KRI-238). Move each only when
    its destination is absent; all other malformed or unknown fields remain
    subject to the strict adapter and fail closed.

    KRI-687 (prod 2026-10-15 logged ``strategy.summary`` as ``extra_forbidden``): a
    `summary` the move rule above leaves in ``strategy`` -- a copy beside the documented
    one, or a value that is not a string -- failed the strict adapter. `summary` is prose
    only the proposal envelope reads --
    `CreativeStrategy` has no such field -- so a nested copy that cannot be moved
    (the documented one already holds text, or the value is not a string) carries
    nothing and is dropped. Nothing else is dropped: a misspelt real field must
    still stop the turn, and two diverging `render_intent_evidence` objects are an
    ambiguity the retry should resolve, not one to settle by guessing.
    """

    if not isinstance(action, dict) or action.get("kind") != "propose_strategy":
        return action
    strategy = action.get("strategy")
    if not isinstance(strategy, dict):
        return action
    outer_summary = action.get("summary")
    outer_summary_blank = outer_summary is None or (
        isinstance(outer_summary, str) and not outer_summary.strip()
    )
    moved: dict[str, object] = {}
    nested_summary = strategy.get("summary")
    if outer_summary_blank and isinstance(nested_summary, str) and nested_summary.strip():
        moved["summary"] = nested_summary
    if "render_intent_evidence" not in action and isinstance(
        strategy.get("render_intent_evidence"), dict
    ):
        moved["render_intent_evidence"] = strategy["render_intent_evidence"]
    removed = set(moved) | ({"summary"} if "summary" in strategy else set())
    if not removed:
        return action
    repaired_strategy = {key: value for key, value in strategy.items() if key not in removed}
    return {**action, "strategy": repaired_strategy, **moved}


# Punctuation only -- no regex, so this cannot become a guess at what the model meant
# (tests/kria/test_kri667_no_new_intent_regexes.py ratchets the `re.*` calls per module).
_STORY_SENTENCE_END = frozenset(".!?…")
_STORY_CLAUSE_END = frozenset(",;:")
_STORY_DASHES = frozenset("-–—")


def _trim_story_line(line: str) -> str:
    """Shorten one over-long story line to ``STORY_LINE_MAX_CHARS``, cutting at a boundary.

    Prefers the end of the last full sentence, then the last clause break, then the last
    word -- each only if it keeps at least half the allowance -- and falls back to a plain
    cut for a line with no usable break. Anything cut mid-sentence ends in an ellipsis so
    a reader can see it was shortened. A line that fits once its padding is stripped is
    only stripped (the schema counts the padding), never shortened.
    """

    text = line.strip()
    if len(text) <= STORY_LINE_MAX_CHARS:
        return text
    text = " ".join(text.split())
    limit = STORY_LINE_MAX_CHARS
    if len(text) <= limit:
        return text
    floor = limit // 2
    room = limit - 1  # leaves one character for the ellipsis
    # Slices (not indexes) look one or two characters ahead, so the end of the text is safe.
    sentence_end = max(
        (
            i + 1
            for i in range(limit)
            if text[i] in _STORY_SENTENCE_END and text[i + 1 : i + 2] == " "
        ),
        default=0,
    )
    if sentence_end >= floor:
        return text[:sentence_end]
    clause = max(
        (
            i
            for i in range(room + 1)
            if (text[i] in _STORY_CLAUSE_END and text[i + 1 : i + 2] == " ")
            or (
                text[i] == " "
                and text[i + 1 : i + 2] in _STORY_DASHES
                and text[i + 2 : i + 3] == " "
            )
        ),
        default=0,
    )
    if clause >= floor:
        cut = clause
    else:
        word = text.rfind(" ", 0, room + 1)
        cut = word if word >= floor else room
    return text[:cut].rstrip(" ,;:-–—") + "…"


def _repair_story_structure(action: object) -> object:
    """Trim an over-long ``strategy.story_structure`` line instead of failing the turn.

    KRI-687 (prod 2026-10-15, thread 1390E686): on a rich multi-part prompt the model wrote
    one story line past 120 characters on all three attempts, so the creator got "I couldn't
    turn that into a plan" for a request that was fine. The field is taste only
    (``preference_only`` in the render contract): readers join the lines into a goal
    sentence, show them as numbered beats and keep them in the receipt, and no renderer is
    held to their wording. Trimming keeps the count and order of the lines (splitting could
    pass the eight-line cap and shows the creator beats nobody wrote); a blank line, a
    non-text entry or more than eight lines still fail the strict schema.
    """

    if not isinstance(action, dict) or action.get("kind") != "propose_strategy":
        return action
    strategy = action.get("strategy")
    if not isinstance(strategy, dict):
        return action
    lines = strategy.get("story_structure")
    if not isinstance(lines, list):
        return action
    repaired = [
        _trim_story_line(line)
        if isinstance(line, str) and len(line) > STORY_LINE_MAX_CHARS
        else line
        for line in lines
    ]
    if repaired == lines:
        return action
    log.info(
        "main_creator_story_line_trimmed",
        trimmed=sum(1 for before, after in zip(lines, repaired, strict=True) if before != after),
        lines=len(lines),
    )
    return {**action, "strategy": {**strategy, "story_structure": repaired}}


_DECISION_KEYS = ("target", "status", "proposed_text", "source_evidence", "language")
_DECISION_KEY_ALIASES = {
    "status": ("resolution", "state"),
    "proposed_text": ("text", "candidate", "suggestion", "wording"),
}
# The server's own `creative_copy_state` vocabulary, echoed back by the model (KRI-586).
_DECISION_STATUS_ALIASES = {
    "generate": "candidate",
    "revise": "candidate",
    "authorship": "unresolved",
    "write_my_own": "unresolved",
}
# Server-owned states: an echo is dropped, never converted (it must not read as consent).
_DECISION_SERVER_STATES = frozenset({"approved", "stale"})
_INERT_DECISION_STATUSES = frozenset({"unresolved", "delegated", "candidate"})


def _repair_creative_decision(data: dict) -> dict:
    """Repair the known harmless shape mistakes in a model-authored `creative_decision`.

    KRI-586 (prod 2026-10-11): asked to write a hook, the model had no documented
    shape for `creative_decision` and returned ``{"state": ...}``, ``{"resolution":
    ..., "text": ...}`` or ``{"target": "r1", "candidate": ...}``, which the strict
    model rejected on all three attempts. Hoist it out of `action`, rename the known
    aliases, and map the server's state vocabulary back to decision statuses.

    Never produces ``creator_supplied`` or ``cancelled`` and never guesses the target
    of a decision that could grant consent: only the inert suggestion statuses
    (``unresolved``/``delegated``/``candidate``) get a defaulted target, and those
    always go through the server's own wording approval before reaching pixels.
    """

    action = data.get("action")
    decision = data.get("creative_decision")
    if decision is None and isinstance(action, dict) and "creative_decision" in action:
        decision = action["creative_decision"]
        data = {**data, "action": {k: v for k, v in action.items() if k != "creative_decision"}}
    elif decision is not None and isinstance(action, dict) and "creative_decision" in action:
        data = {**data, "action": {k: v for k, v in action.items() if k != "creative_decision"}}
    if not isinstance(decision, dict):
        return data if decision is None else {**data, "creative_decision": decision}
    repaired = dict(decision)
    for canonical, aliases in _DECISION_KEY_ALIASES.items():
        if canonical not in repaired:
            for alias in aliases:
                if alias in repaired:
                    repaired[canonical] = repaired[alias]
                    break
    repaired = {key: repaired[key] for key in _DECISION_KEYS if key in repaired}
    status = repaired.get("status")
    status = status.strip().lower() if isinstance(status, str) else status
    if status in _DECISION_SERVER_STATES:
        return {k: v for k, v in data.items() if k != "creative_decision"}
    if status in _DECISION_STATUS_ALIASES:
        status = _DECISION_STATUS_ALIASES[status]
        if status == "candidate" and not repaired.get("proposed_text"):
            status = "delegated"
    if status is not None:
        repaired["status"] = status
    target = repaired.get("target")
    if isinstance(target, str) and target not in {"opening_title", "closing_title"}:
        lowered = target.strip().lower()
        if lowered.startswith(("closing", "outro", "ending")):
            repaired["target"] = "closing_title"
        elif lowered.startswith(("opening", "intro", "hook", "title")):
            repaired["target"] = "opening_title"
        elif status in _INERT_DECISION_STATUSES:
            repaired["target"] = "opening_title"
    return {**data, "creative_decision": repaired}


def _schema_error_class(exc: ValidationError) -> str:
    """A closed-vocabulary, value-free tag for ``sensitive_agent_error:<class>`` (<= 80 chars).

    Built only from Pydantic locations and error types, never from input values.
    """

    prefix = "creative_decision:" if exc.title == "CreativeCopyDecision" else ""
    parts: list[str] = []
    for error in exc.errors(include_input=False, include_context=False)[:6]:
        loc = ":".join(str(p) for p in error["loc"] if not isinstance(p, int))
        parts.append(f"{prefix}{loc}:{error['type']}" if loc else f"{prefix}{error['type']}")
    return _clean_error_class(",".join(parts) or "validation_error")


# A saved audio requirement as the server renders it (`brief._render_requirement`):
# `- [rN] [audio/<scope>] <text> facts={...}`, facts last with sorted keys.
_SAVED_AUDIO_PATTERN = r"^- \[(?P<id>r\d+)\] \[audio/[^\]]+\] .* facts=\{(?P<facts>[^{}]*)\}$"
_SAVED_AUDIO_LINE = re.compile(_SAVED_AUDIO_PATTERN)  # kri667: server-rendered typed facts
_AUDIO_INTENT_FACT = re.compile(r"(?:^|; )audio_intent: (?P<intent>[^;]+)")  # kri667: typed facts


def _states_source_audio(
    brief_updates: object, creator_request: str = "", *, starts_fresh: bool = False
) -> bool:
    """The creator's brief records "keep the original camera sound" (a typed fact).

    This turn's own audio updates win, including a change or removal that targets a
    saved audio requirement by id (kind may be omitted, KRI-805). With none, the saved
    brief's live audio requirements still hold (KRI-816 T06 already had it as r4), unless
    this turn starts fresh or a saved audio requirement asks for another audio intent.
    """
    try:
        updates = parse_brief_updates(brief_updates)
    except Exception:  # noqa: BLE001 - an unreadable batch is refused by the parse below
        return False
    saved: dict[str, str] = {
        match.group("id"): match.group("facts")
        for line in creator_request.splitlines()
        if (match := _SAVED_AUDIO_LINE.match(line.strip()))
    }
    audio = [
        update
        for update in updates
        if update.kind == "audio"
        or (update.operation != "add" and update.target_requirement_id in saved)
    ]
    if audio:
        return any(
            update.operation != "remove" and update.facts.get("audio_intent") == "source_unchanged"
            for update in audio
        )
    if starts_fresh:
        return False
    intents = {
        match.group("intent").strip()
        for facts in saved.values()
        if (match := _AUDIO_INTENT_FACT.search(facts))
    }
    return intents == {"source_unchanged"}


def _repair_unrecorded_voiceover(manifest, strategy, *, source_audio_stated: bool):
    """Drop a model-invented voiceover when nothing is recorded (KRI-816, T06).

    Only when the creator's brief records that they asked for their original camera
    sound (a typed fact, never a word match). A format the model chose is never
    swapped here: an unavailable format stays a named policy refusal (KRI-129, no
    silent overrides).
    """
    from app.agents._schemas.edit_format import coerce_edit_format  # noqa: PLC0415

    if (
        source_audio_stated
        and strategy.audio_strategy == "voiceover"
        and not manifest.has_voiceover
        and coerce_edit_format(strategy.edit_format) not in NARRATED_EDIT_FORMATS
    ):
        log.info("main_creator_unrecorded_voiceover_repaired")
        strategy = strategy.model_copy(update={"audio_strategy": "original_audio"})
    return strategy


_FORMAT_REFUSAL_CLASSES = frozenset(
    {"policy_edit_format_unavailable", "policy_phone_format_unavailable"}
)


def _renderable_edit_formats(manifest: ResolvedCreatorManifest) -> list[str]:
    """Edit formats this manifest can render now (the iPhone's when it is a phone edit)."""
    capabilities = manifest.capabilities
    prefix = (
        "phone_format:"
        if any(name.startswith("phone_format:") for name in capabilities)
        else "edit_format:"
    )
    return sorted(
        name.removeprefix(prefix)
        for name, capability in capabilities.items()
        if name.startswith(prefix)
        and capability.available
        and (
            capabilities.get(f"edit_format:{name.removeprefix(prefix)}") is None
            or capabilities[f"edit_format:{name.removeprefix(prefix)}"].available
        )
    )


# Closed, value-free tags for server policy refusals (KRI-816): the old catch-all
# `policy_refusal` hid which rule ended the T06 turn.
_POLICY_REFUSAL_TAGS: tuple[tuple[str, str], ...] = (
    ("is unavailable", "policy_edit_format_unavailable"),
    ("does not support voiceover", "policy_phone_voiceover_unavailable"),
    ("phone rendering requires bound video", "policy_phone_media_unavailable"),
    ("montage audio sources", "policy_montage_audio_sources"),
    ("montage cadence sources", "policy_montage_cadence_sources"),
    ("requires at least one attached clip", "policy_native_no_clip"),
    ("selected_media_ids must reference", "policy_unknown_media"),
)


def _non_pydantic_error_class(exc: Exception) -> str:
    from app.agents._schemas.creator_policy import (  # noqa: PLC0415
        PhoneFormatUnavailableError,
        PhoneMediaUnavailableError,
    )

    if isinstance(exc, UserSongUnavailableError):
        return "user_song_unavailable"
    if isinstance(exc, json.JSONDecodeError):
        return "json_decode"
    message = str(exc)
    if "rationale" in message:
        return "strategy_rationale_missing"
    if "target_duration_s" in message:
        return "strategy_duration_missing"
    if "brief_updates" in message:
        return "brief_updates_invalid"
    if isinstance(exc, PhoneFormatUnavailableError):
        return (
            "policy_phone_voiceover_unavailable"
            if getattr(exc, "voiceover", False)
            else "policy_phone_format_unavailable"
        )
    if isinstance(exc, PhoneMediaUnavailableError):
        return "policy_phone_media_unavailable"
    if isinstance(exc, ValueError):
        return next((tag for key, tag in _POLICY_REFUSAL_TAGS if key in message), "policy_refusal")
    return type(exc).__name__.lower()


def _clean_error_class(value: str) -> str:
    cleaned = re.sub(r"[^a-z_:,]", "_", value.lower())
    return cleaned[:80]


def _classed_schema_error(message: str, error_class: str) -> SchemaError:
    error = SchemaError(message)
    error.error_class = _clean_error_class(error_class)  # type: ignore[attr-defined]
    return error


def _refile_take_trims(
    updates: list[BriefUpdate], agent_input: MainCreatorInput
) -> list[BriefUpdate]:
    """KRI-789: quoted start / end lines of a one-video edit are a trim, not a clip order.

    v64 filed "Start at “Hi, I'm Karolina…” and end right after “I'm gonna be here all week”"
    as a key-less `order` requirement, which asked the order_basis question on a one-video
    Talking edit. The prompt now teaches the `timing` shape; this keeps a reply in the old
    shape from reaching the brief when the edit is one video (no clip order to mean instead).
    """
    media = agent_input.capability_manifest.media
    visual = [m for m in media if m.kind in {"video", "image"}]
    if not updates or len(visual) != 1 or visual[0].kind != "video":
        return updates
    refiled = [refile_take_trim(u, creator_message=agent_input.user_message) for u in updates]
    moved = sum(new is not old for new, old in zip(refiled, updates, strict=True))
    if moved:
        log.info("main_creator_take_trim_refiled", count=moved)
    return refiled


def _refile_text_sequences(
    updates: list[BriefUpdate], agent_input: MainCreatorInput
) -> list[BriefUpdate]:
    """29 Ekim: words the creator wants SHOWN one at a time are a `text_sequence`, not captions.

    The prompt teaches the shape; this keeps a reply that still files the ask as
    `captions: word_by_word` from reaching the brief (see `kria.text_sequence`).
    """
    if not updates:
        return updates
    earlier = [
        str(row.get("content") or "")
        for row in agent_input.conversation
        if isinstance(row, dict) and row.get("role") == "user"
    ]
    refiled = refile_text_sequences(
        updates,
        creator_message=agent_input.user_message,
        known_literal=last_quoted_literal(earlier),
        has_voiceover=agent_input.capability_manifest.has_voiceover,
    )
    moved = sum(new is not old for new, old in zip(refiled, updates, strict=True))
    if moved:
        log.info("main_creator_text_sequence_refiled", count=moved)
    return refiled


_TOP_LEVEL_DECLARATIONS = ("delegates_all_choices", "starts_fresh")


def _hoist_misplaced_declarations(data: dict) -> dict:
    """Move the two typed brief declarations the model wrote inside `action` to the top level.

    KRI-992 (prod 2026-10-27, 7 of 32 planner calls): the prompt asks for top-level booleans
    `delegates_all_choices` / `starts_fresh`, but the model sometimes writes them inside the
    `propose_strategy` object. The strict adapter rejects them as ``extra_forbidden`` and the
    runtime spends a second full planner call. Both describe the creator's message, not the
    action, so where they sit carries no meaning.

    Only these two keys, only real booleans, from the action itself (not `strategy`). A key
    already present at the top level wins, even when false, and the nested copy is dropped.
    A nested non-boolean is left in place, so it still fails closed through the retry. Every
    other unknown key still reaches the strict adapter.
    """

    action = data.get("action")
    if not isinstance(action, dict):
        return data
    lifted: dict[str, bool] = {}
    dropped: list[str] = []
    for key in _TOP_LEVEL_DECLARATIONS:
        if not isinstance(action.get(key), bool):
            continue
        dropped.append(key)
        if key not in data:
            lifted[key] = action[key]
    if not dropped:
        return data
    log.info("main_creator_declarations_hoisted", keys=dropped, lifted=sorted(lifted))
    repaired_action = {key: value for key, value in action.items() if key not in dropped}
    return {**data, **lifted, "action": repaired_action}


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
    # KRI-520: "tüm klipleri kullan", "hepsini kullan", "sadece en iyileri".
    return turkish_media_scope(request)
