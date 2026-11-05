# Planner speech budget — failure modes (written before the fix)

**Incident:** S4 run "fb 1" (prod, 2026-11-04, thread `6A624CDB`, job `c39a3210`). An 83 s Turkish talking take
with ten Visuals and the green-screen ask "behind me whichever photo or video I'm talking about". The Main Creator
put all 8 `background_media.sequence` phrases in the first 29 s; after that the backgrounds cycled with no relation
to the speech (Maltepe photo on "taraftar", stadium on "Maltepe'ye", "müteahhit").

**Root cause:** `resolve_item_creator_context` gives the Main Creator each footage clip's evidence through
`_chat_evidence`, which keeps timed speech `segments` only up to `CHAT_EVIDENCE_SEGMENT_CHARS = 480` characters
(whole sentences; KRI-282). The take's first five sentences are 445 characters, the sixth crosses 480, so the planner
never saw sentences 6–14. Green 1–4 used a 31 s take, under the cap. The media-overlay matcher reads the full take
and did place the park photo on "park", which isolates the cap to the planner's input.

**Change:** footage clips (the creator's own clips, not Visuals) share one speech budget instead of 480 each:
`max(480, 6000 // clips_with_segments)` characters per clip. Visuals keep 480.

| # | Failure | Guard |
|---|---|---|
| F1 | A single talking take longer than ~480 characters of speech loses its later sentences in the planner input (the incident). | Offline replay: an 83 s take, 14 sentences; the anchor sentence after 60 s must reach the Main Creator prompt with its time. Red on the old cap. |
| F2 | A very long take blows up the prompt or the 130 s Main Creator timeout. | Budget is bounded: 6000 characters per clip at most (stored segments are already capped at 60); whole sentences only. |
| F3 | Many talking clips (montage) multiply the budget. | Per-clip share shrinks with the number of clips that have segments; floor stays 480, so the total never exceeds today's worst case `max(6000, 480·n)`. |
| F4 | Clips without speech take a share and starve the speaker clip. | Only clips whose analysis has segments count toward the split. |
| F5 | Visuals (pool assets) start carrying long speech and drown the narration. | Visuals keep the old 480 cap and the old `_chat_evidence` defaults (byte-identical; `test_the_main_creator_chat_evidence_is_unchanged`). |
| F6 | `SPEECH_EXCERPT_MONTAGE_ENABLED=false` starts sending segments. | Segments still follow `include_segments=settings.speech_excerpt_montage_enabled`. |
| F7 | A sentence is cut mid-way and the planner quotes a phrase that is not said. | `prompt_view` keeps whole sentences only; unchanged. |
| F8 | Duplicate media ids or clips past `MAX_CREATOR_MEDIA_REFS` skew the split. | The count uses the same de-duplicated, sliced list that builds the entries. |
| F9 | The change shifts the manifest hash and every open approval hits the "Footage or capabilities changed" fence. | Analysis evidence is not part of `CreatorMediaRef` / the manifest hash; test asserts the manifest is identical with short and long speech. |
| F10 | Text beyond the budget is silently dropped again for 8+ minute takes. | Remaining gap: no marker tells the planner where its view of the speech ends. Out of scope; noted in the PR. |
