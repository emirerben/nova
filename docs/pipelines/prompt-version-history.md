# Frozen prompt-version history (Main Creator, brief extractor)

KRI-1381 moved more agents onto the same fingerprint helper: persona (`pe-`), content plan
(`cp-`), agentic style selector (`as-`), audio template (`at-`), beat aligner (`ba-`), the
research copy, layers and review agents (`rc-`, `rl-`, `rr-`) and the creation text composer
(`ct-`). Their old hand-numbered histories live in git. Still hand-numbered on purpose, because
the value is part of a persisted cache key or digest: the TikTok analyzer
(`TIKTOK_ANALYZER_PROMPT_VERSION`, in `_analysis_fingerprint`), the grounded research agent
(story-idea digest), the Layer-2 text agents and Transcript (`template_cache.py`, template
staleness), clip and media analyzers (`clip_cache.py`, `MediaAssetAnalysis` rows), the clip
router, the edit director and lyrics (stored rows compared to the live version).

Until 2026-11-06 both agents carried a hand-numbered `prompt_version` and a history comment
above it. Every prompt PR edited that one line, its history and the tests that pinned it, so
each merge conflicted with every other open prompt PR (31 of 64 conflicting merges in the
2026-11-05 batch, `docs/reviews/pr-merge-speed-2026-11-05/`). The versions are now
fingerprints of the prompt's source files (`app/agents/_prompt_fingerprint.py`): `mc-<hash>`
and `be-<hash>`. They move by themselves whenever a source file changes. Don't bump, pin or
extend them; describe a prompt change in its PR.

Stored runs, traces and replay fixtures recorded before the switch keep their old labels
(last: Main Creator `"2026-11-06-v136"`, brief extractor `"2026-11-06-v68"`).
The history below is kept verbatim for reference and is not updated any more.

## Main Creator (`MAIN_CREATOR_PROMPT_VERSION`)

```text
Named sound effects are model-read with verbatim licensed_sfx evidence (v35).
KRI-178: reaction beats (name/word-triggered photo/sticker + sound pop-ins,
plus a held closing shot) on a phone `subtitled` (Talking) edit (v36).
KRI-188: Creative Brief requirement extraction (`brief_updates`, taught only
when the brief is on for the creator) -- v37.
KRI-189: when/where clip facts with provenance (v38).
KRI-190: brief `facts` (distance/activity/start/end) and an `order` requirement
are always captured when the creator asks the edit to follow a route or sequence (v39).
KRI-244: descriptive footage chronology remains creative context unless the
creator actually asks the edit to order, group, label, include, or caption it (v40).
KRI-374: a creator-uploaded song as the music (`audio_strategy="user_song"` +
`song_sync`), taught only when the manifest carries a usable song (v42).
KRI-422: dictated per-shot texts are one per_clip brief entry per shot (they all
stay in force); `brief_updates` cap 8 -> 16 (v43).
KRI-459: stable IDs make changes and removals unambiguous (v44).
KRI-470: a stated video length ALWAYS becomes a `timing` requirement; the clarification
gate cannot compare a number the brief lost (v45; brief_extractor v2 shares the section).
KRI-506: delegated creative copy is proposed for a separate server approval (v46).
KRI-479: `voice_mode` (continuous | excerpts) for a montage that keeps one clip's camera
audio, taught only when the manifest advertises `phone_source_audio` AND the route can render
(`Settings.voice_behind_footage_enabled`) (v47).
KRI-522: brief facts for first_clip/last_clip, animation, position, placeholder (v48).
KRI-523: `pinned_texts` (whole-video corner text) + choice-answer brief rule (v49).
KRI-520: a Turkish chat gets a reply-language line at the end of the prompt; English is
byte-identical (v50).
KRI-519: reaction beats on an iPhone Voiceover edit, worded for the voiceover (v51).
KRI-525/526: pins may carry `start_s`/`end_s` or `clip`; the 4-line / 120-character limits are
taught so an over-long ask becomes a question, not a schema failure (v52).
KRI-598: no `execution_contract` where `guided_voiceover` is unavailable (runtime v2); on a
Voiceover edit a cue for an own clip is `include`, never `order`, and beats persist (v55).
KRI-614: a clip's `filename` (the name the app shows) identifies it when the creator writes it
(v56).
KRI-667: brief `facts` carry what an ask means in English keys and values (`triggers`,
`keep_whole_take`, `closing`, `speech_cleanup`, `captions`, `caption_language`) whatever the
creator's language, so the server checks them without re-reading the words (v58).
v59 combines these facts with KRI-669 heading groups and relative sizes.
KRI-700: a brighten / darken ask carries `footage_light`, so the server can say no draft
changes the footage's light instead of the summary promising it (v60).
v61 (KRI-674 live verification): per-clip camera sound is not spoken excerpts.
KRI-793: a captions ask that lists words to spell records them in `spellings`, which the
phone Talking writer applies to its captions and the render-ready review checks (v65).
KRI-791: an on-screen text timed to a spoken word carries `triggers` too (it was taught only
for photos, videos, stickers and sounds, so "Show MEROPE ... the first time she says Merope"
arrived with no trigger), plus `speaker` when someone other than the creator says it.
KRI-806: optional editorial plans pair only with licensed music and no kept clip sound.
v67 retains caption spellings, spoken triggers/speaker and the editorial audio pairing rule.
v66 combines both authored changes without losing either prompt instruction.
v66 (KRI-792): named spoken cuts carry cut_lines; caption spellings remain intact.
KRI-789: quoted start / end LINES inside a take are a `timing` trim (`start_line` /
`end_line`), never an `order` requirement; route places keep `order` (v67).
v68 combines caption spelling, spoken-text triggers/speaker, quoted trims and named cuts.
v69 retains all shared brief instructions plus editorial audio pairing.
v70 (KRI-851): the Talking reaction-beats section no longer forbids `opening_title`; a requested
title / closing text still goes in `opening_title` / `closing_title` alongside the beats.
KRI-814: a typed `delegates_all_choices` declaration for a message that states no requirement
and hands every choice to Kria, so readiness need not match the stock sentence (v71).
v72 (KRI-814): earlier creator lines never read into the brief are read with the current one.
v74 combines delegation v72 with subtitle-semantics v73, retaining all instructions.
v75 adds N2 fullscreen photo-then-original sequences to the complete v74 union.
v76 (KRI-816): a typed `starts_fresh` declaration withdraws the saved brief ("Start fresh").
v76 (KRI-815): `$voiceover_script_section` teaches `draft_voiceover_script` when footage is
attached and no voiceover is (a script request returns a script, not a doomed narrated draft).
v77 combines both v76 branches (KRI-816 starts_fresh + KRI-815 voiceover script section).
v77 (KRI-827): "no text on her face or on the plate" => `keep_text_off` facts.
v78 combines main's v77 (KRI-815 + KRI-816) with KRI-827's v77 (keep_text_off).
v77 (KRI-821): "mute the camera / crowd sound" is a typed `source_muted` audio intent.
v78 (KRI-821): a named cut of a repeated line records which take (`cut_line_occurrences`).
v79 combines main's v77 (KRI-815 + KRI-816) with KRI-821's v78.
v80 combines main's v79 (KRI-815/816/821) with KRI-827's keep_text_off (branch v78).
KRI-724: the Nth time a quoted phrase is said carries `speech_remove` {quote, occurrence}
beside its cut_lines, and an engine/motor noise ask carries `noise_reduction: "engine"`, so
the phone Talking worker cuts / filters from facts, never from the words (KRI-667) (v76,
on top of v75).
v76 (KRI-840): Talking reaction beats can ask for a photo full screen at a word
(`display_mode` "fullscreen"), and the shared brief records `display_mode`.
v76 (KRI-841): `ending_clip` (a video added after the talk, with its own sound, Talking only) and
the brief fact that records it next to `last_clip` (brief_extractor v21 shares the section).
v76 (KRI-842) adds the `clip_sound` fact: one clip's own sound under, or after, a voiceover.
KRI-847: a ceiling ("at most 15 seconds") carries `duration_bound: "at_most"` beside
`duration_s`, so a shorter draft meets it instead of missing 15 s +/- 10 % (v76).
v76 (KRI-849): "if you can't tell what he says, ask me instead of guessing" is a captions fact
(`ask_if_unclear`), and the creator's later answer is `unclear_answer` ("omit" or their own
words); the server compares two hearings of the speech and asks, it never reads the words.
v92 (KRI-881): main's v91 plus a research / sources ask as a typed `text` requirement with
`facts.research` ({topic, request_quote}); the server researches it before approval.
v93 combines main v92 research-before-approval with runtime creative skills.
v95 (KRI-689): the shared brief section records a clip-speed ask as `facts.playback_rate`
(0.25-4; "slow motion" = 0.5, "speed up" = 2) on a `style` requirement, never for pacing /
text / music or a speed ramp; the phone montage plans the rate onto the named clips.
v96: exact-timed small cards (`timed_cards`, flag PHONE_TIMED_CARDS_ENABLED) on phone
Montage / Voiceover edits, and the shared brief records a `timed_card` fact for the ask.
v97 (KRI-945): the described-text exception covers text on particular clips only; an
intro, explanation or facts for the whole video is never a clip intent.
v98 (KRI-942): the shared brief records clip sound on / off (`clip_sound` "on"/"off" with
`sound_clip`, `sound_from_s`/`sound_to_s`, `sound_line`, `sound_except`) for montages.
v99 (KRI-957): an after-voice clip ask's `triggers` is the line said in that clip, never the
creator's own last voiceover words ("after my last words \"before we eat\"").
v100 (KRI-994): a select ask records the media kind it restricts to / includes
(`only_media_kind` "video", `include_media_kind` "photo"), so the server can judge "clips
only" on the finished timeline and withdraw an earlier photo ask a later clips-only ask replaces.
v102 (29 Ekim lane D, KRI-1038): words shown a piece at a time are a `text_sequence`
requirement, not `captions: word_by_word`; "say" plus a quote with no voiceover is on-screen
text. (v101 is lane B's order-key edit; they merge in that order.)
v103 (KRI-1027): the shared brief records how sideways clips sit in a vertical video
(`landscape_fit` "fill" / "fit"), which preselects crop or black bars on the confirm screen.
v104 (30 Ekim): prompt-cache layout only, no rule change. The per-turn INPUT block
(creator context, footage, manifest, conversation, message) moved to
prompts/main_creator_turn.txt at the end so the rules above it form a shared prefix.
v105 (KRI-1077): title animation names come from the vocab registry (all designed edges).
v106 (prompt fidelity, animation): an UNNAMED animation ask ("with animations", "animated
text") is a `text` requirement carrying `animation: "any"`, not prose only; a named one
keeps its id. One fact for both: the disclosure unit's interim `animated: true` fact is gone.
v107 (KRI-1117): `background_media` (a photo / looping video behind the Talking speaker), taught
only where the manifest advertises `speaker_background`; every other prompt is byte-identical.
v108 (KRI-1093): the Lip-sync picker card (`manifest.chosen_format`) defaults an attached song
to `lipsync`; the song section gains one paragraph only on that manifest, so every other
render is byte-identical.
v109 (KRI-1151): a multi-clip Talking edit can show its other clips full screen where the creator
talks about them, optionally playing their own sound with the creator's voice muted under it.
Taught (with the voice-muted honesty) only where `talking_footage_cutaways(manifest)` is open,
so every other render is byte-identical; `MainCreatorInput.server_note` is the planner's one
corrective re-run after a refusal the open gates contradict (`app.kria.ask_refusals`).
v110 (parent KRI-1151): `insert_clips` (a footage clip played while the Talking take pauses),
taught only where `insert_clip_candidates(manifest)` is non-empty, and the shared brief records
it as an `insert_clip` fact (brief_extractor v46 shares the section). The cutaway section
says an insert exists only as `insert_clips` while that lane is open.
A clip shown while the talk keeps running is recorded as `display_mode` / `clip_sound`, never
`triggers`.
v109 (KRI-1162): commentary-style green screen. `background_media` gains `sequence` /
`spoken_phrase` / `cadence_s` / `speaker_frame`, taught in one extra paragraph only where the
manifest advertises `speaker_background_windows`; every other prompt is byte-identical.
v111: v109 (KRI-1162, 11-01), v109 (KRI-1151, 11-02) and v110 (insert clips) merged;
the prompt carries all three.
v112 (parent KRI-1151, thread b61b7673): "place them in between my speech" for clips that have
speech or sound of their own is an INSERT (one `insert_clips` row per clip, no phrase: the
worker pauses the talk at the end of the sentence about the clip, chosen by meaning), and
"over my speech / while I keep talking / keep my voice" stays the overlay. The creator's
position words decide the route, never the sound words ("use their audio" is listed once, on the
overlay side), and a clip with no sound of its own said "in between my speech" is the same
insert played silent (`keep_audio` false). Only the texts shown while the insert lane is open
change; every closed-gate prompt keeps its bytes. No live eval of this routing exists; the
fixtures under tests/fixtures/agent_evals/main_creator/kri1151_* are hand-authored.
v114 (KRI-1175, prod "Green 3"): "whichever photo or video I'm talking about" gives every
background_media.sequence entry a spoken_phrase from the take's own words (spoken order; one
phrase for two visuals of one thing; a visual never talked about is left out and named in
summary), so the worker lays them out on the words. Only the windows section changes (shown only
with the speaker_background_windows capability); every other prompt keeps its bytes. v113 is used
by unmerged branches (PR #2089, KRI-1173).
v116 (KRI-1177 / KRI-1164, Green 3, PR #2089; was v113 on its branch, v115 is #2088's): the
shared brief section records the creator's caption look (`caption_size`, `caption_position`,
`caption_color`, `caption_case`) and "no music" as `audio_intent: no_music`; the speaker's own
corner is never a caption position. On top of v114.
v117 (KRI-1189, Green 3; v115 is #2088's): the shared brief section records an unquoted "cut the
sentence I restarted" / "my retakes" / "tekrar aldığım cümle" as `cut_restarts: true` and never
copies the take's words into `cut_lines` (Green 3 r2 did); a named restart stays a cut line and
"start over" about the edit or a clip is no restart. On top of v116.
v118 (KRI-1173; v117 is held by #2092): a plain colour behind the speaker, on top of v116.
`background_media` takes `color` ("#RRGGBB") instead of `media_id`, taught in one extra
paragraph only where the manifest advertises `speaker_background_color`; the shared brief
section records `background_color` beside `speaker_background` (brief_extractor v53 shares
it). Every other creator prompt is byte-identical.
v119 (KRI-1189 lands after #2088): v117's `cut_restarts` teaching on top of v118. The
KRI-1189 replay fixtures keep the `2026-11-03-v117` label they were recorded under.
v120 (Qendresa item 12): the shared brief section records the caption shape as typed facts,
`caption_words` (1-3 words at a time) and `caption_box` (a #RRGGBB box behind the captions).
On top of v119; brief_extractor v55 shares it.
v121 (Qendresa 13, tight pauses; on top of v120, Qendresa 12 caption shape): the shared brief
section records an "all my pauses / tight pace" ask as `pauses: "tight"` beside
`speech_cleanup`. On top of v119.
v123 (Qendresa item 17, follow-up of KRI-1175; v121-v122 reserved by items 13-14): one
paragraph after the windows section teaching how to write a sequence entry's `spoken_phrase`
(as said in the take, with the words only said where that visual is introduced). Shown only
with BACKGROUND_FOLLOWS_TALK_ENABLED and the windows capability; every other prompt keeps its
bytes.
v125 (Qendresa item 16; v124 is reserved for item 15): two teachings, shown only with
SPEAKER_FULL_OPENING_ENABLED and the `speaker_full_opening` capability. Strategy: "start with me
full screen ... after that my recordings and me small in the corner" sets
`background_media.full_screen_opening` (+ `full_screen_opening_s` for a named length). Brief: the
shared section records `speaker_full_opening` / `speaker_full_opening_s` on the requirement it
belongs to (brief_extractor v60 shares it). Every other prompt keeps its bytes.
v126 (Qendresa item 18; v124 is reserved for item 15; shipped as v136, below): one paragraph
after the full-opening section, shown only with BACKGROUND_FOCUS_ENABLED and the
`background_focus` capability. The recordings behind the speaker are aimed at their readable
text by the compile; the planner sets `background_media.focus` (a named side) / `focus_zoom`
(a named amount) only when the creator names one. Every other prompt keeps its bytes.
v128 (Qendresa item 21), on top of v120 (Qendresa 12 caption shape): behind
`settings.clip_text_tiktok_look_enabled`, `_CLIP_TEXT_LOOK_PROMPT_SECTION` teaches one line
pinned in the MIDDLE for the whole video (`corner: "middle_center"`) and TikTok's own text look
(`look: "tiktok"`). Flag off => the rendered prompt is byte-identical to v120.
v131 (item 23, Related KRI-1129), on top of v120 (Qendresa 12 caption shape): with
BRIEF_LONG_REQUEST_ENABLED the brief section says "at most 32 objects" instead of 16.
Flag off: every prompt byte-identical.
v133 (Qendresa item 16; v130 until main reached v131 with item 23; v132 is held by item 24):
v125's two full-screen-opening teachings on top of v128 and v131. Flag off, every prompt keeps
its bytes.
v135 (Qendresa item 20, TITLE_LOOK_ENABLED): only while `title_look_enabled` is on, the Talking
reaction-beats section gains one rule: a logo/sticker she wants with or above the opening title
is a beat with `anchor: "title"`; the title stays in `opening_title`, " / " included. Flag off:
every prompt keeps its bytes. On top of v133 (#2111). Reserved as v129, renumbered once main
passed it; v132 and v134 are held by items 24 and 18.
v136 (Qendresa item 18; was v126, then v132, then v134 until item 20 merged as v135):
v126's background-focus paragraph on top of v133 (item 16) and v135 (item 20). Every other
prompt keeps its bytes.
```

## Brief extractor (`BRIEF_EXTRACTOR_PROMPT_VERSION`)

```text
v9 (KRI-667/KRI-669): shared brief preserves heading and language-neutral ask facts.
v10 (KRI-700): shared brief records `footage_light` for brighten / darken asks.
v11 (KRI-674 follow-up): timing examples satisfy the non-empty content contract.
v12 (KRI-721 live check): shared brief carries unprocessed source-audio intent.
v13 (KRI-793): shared brief records the words to spell in the captions (`spellings`).
v14 (KRI-791 + KRI-793): retain spellings and record text `triggers` + `speaker`.
v14 (KRI-792): named spoken cuts carry cut_lines as well as caption spellings.
v15 (KRI-789): shared brief records quoted start / end lines as a `timing` trim.
v16 shares the complete caption/spoken-trigger/quoted-trim/named-cut instruction union.
v17 (KRI-814): shared brief section teaches the typed `delegates_all_choices` declaration.
v18 (KRI-814): earlier creator lines never read into the brief are read with the current one.
v20 combines delegation v18 with subtitle-semantics v19 in the shared brief.
v21 (KRI-816): shared brief section teaches the typed `starts_fresh` declaration.
v22 (KRI-827): the shared brief section also records `keep_text_off` facts.
v21 (KRI-724): shared brief records `speech_remove` {quote, occurrence} and
`noise_reduction: "engine"`, on top of v20.
v21 (KRI-840): shared brief records `display_mode` ("fullscreen" / "pip") for a timed photo ask.
v21 (KRI-841): shared brief records a video added after the talk as `ending_clip` + `keep_audio`.
v21 (KRI-842): the shared brief records `clip_sound` (one clip's sound under / after a voice).
v21 (KRI-847): shared brief records a ceiling length as `duration_bound: "at_most"`.
v21 (KRI-849): shared brief records `ask_if_unclear` and the creator's `unclear_answer`.
v34 (KRI-881): main's v33 plus the shared brief section typing a research ask
(`facts.research`).
v35 (KRI-689): the shared brief section records a clip-speed ask as `facts.playback_rate`.
v36: the shared brief section records a `timed_card` fact (a photo as a small card at an
exact second / clip) for phone Montage and Voiceover edits.
v37 (KRI-942): the shared brief section records clip sound on / off (`clip_sound` "on"/"off").
v38 (KRI-957): an after-voice clip ask's `triggers` is the line said in that clip, never the
creator's own last voiceover words.
v39 (KRI-994): the shared brief section records `only_media_kind` / `include_media_kind` on a
select ask.
v41 (29 Ekim lane D, KRI-1038): the shared brief section records words shown a piece at a
time as a `text_sequence` requirement, not `captions: word_by_word`. (v40 is lane B's.)
v42 (KRI-1027): the shared brief section records `landscape_fit` ("fill" / "fit") on a style
ask about how sideways clips sit in a vertical video.
v45 (prompt fidelity, animation): the shared brief section records an unnamed animation ask
as a `text` requirement with `animation: "any"`.
v46 (parent KRI-1151): the shared brief section records `insert_clip` / `insert_at_phrase`.
v47 (KRI-1162 / KRI-1161): the shared brief section records a photo or video behind the
speaker as a `style` fact `speaker_background` (+ `background_changes`, `speaker_small`).
v48 (parent KRI-1151): "place them in between my speech" for clips with their own sound is an
`insert_clip` fact with no position (the server picks the sentence); "over my speech" stays
display_mode / clip_sound, and the sound words never decide the route ("use their audio" is
listed on the overlay side only; a clip with no sound said "in between my speech" is the same
insert). Only the insert paragraph (shown while inserts are open) changes.
v49 (generated-image grounding): the extractor records "the <generated image> when I say W" as a
typed `triggers` ask, and says which videos that does NOT cover (the creator's own footage
clips). Extractor-only section; the shared brief section is untouched.
v50 (KRI-1177 / KRI-1164, Green 3): the shared brief section records the creator's caption look
(`caption_size`, `caption_position`, `caption_color`, `caption_case`) and "no music" as
`audio_intent: no_music`; the speaker's own corner is never a caption position.
v52 (KRI-1189, Green 3; v51 is #2088's): the shared brief section records an unquoted restart ask
as `cut_restarts: true`, with no transcript words in `cut_lines`.
v53 (KRI-1173; v51-v52 are held by #2092 and others): the brief section also records
`background_color` on a plain-colour background ask.
v54 (KRI-1189 lands after #2088): v52's `cut_restarts` teaching on top of v53. The
KRI-1189 replay fixtures keep the `2026-11-03-v52` label they were recorded under.
v55 (Qendresa item 12): the shared brief section records `caption_words` and `caption_box`.
v56 (Qendresa 13, tight pauses; v55 is reserved by another open item): the shared brief section
records an "all my pauses / tight pace" ask as `pauses: "tight"` beside `speech_cleanup`.
v60 (Qendresa item 16; v56 is Qendresa 13; v57-v59 are reserved for items 14, 15 and 17): with
SPEAKER_FULL_OPENING_ENABLED the shared brief section records `speaker_full_opening` /
`speaker_full_opening_s` ("start with me full screen"). The extractor has no manifest, so the
flag alone gates it; the main planner also needs the capability, and a fact recorded where the
phone cannot start full screen is answered honestly by the receipt. Flag off: unchanged bytes.
v66 (item 23, Related KRI-1129), on top of v55 (Qendresa 12 caption shape): with
BRIEF_LONG_REQUEST_ENABLED the shared brief section says "at most 32 objects" instead of 16.
Flag off: byte-identical.
v68 (Qendresa item 16 after main reached v66; v67 is held by item 24): v60's full-screen-opening
fact on top of v66. Flag off: unchanged bytes.
```
