# Main Creator Agent rubric

Score whether the response makes one decisive, footage-grounded editorial choice; obeys the
capability manifest; uses only owned media IDs for native strategies; keeps audio-led formats native; explains a coherent
hook, story, pacing, captions, and audio strategy; and avoids implementation details or invented
transcript/lyrics. A passing response must be specific enough for a creator to confirm or reject.

For a guided strategy, `selected_media_ids: []` is the required privacy and response-size contract:
the bounded guided specialist receives the complete accepted media universe and selects the sources.
Do not penalize an empty guided selection. For a native strategy, score ownership and usefulness of
the selected IDs normally. Licensed music is valid only when the manifest catalog contains music.

`clip_intents` are supported requests, not invented copy or manual `media_overlays`.
Clip-sourced requests describe an attribute for the visual resolver; they must not
contain invented label values. Transcript-sourced labels require the recorded
`guided_voiceover_v1` path: participant placeholders, spoken scores, and spoken
topics are grounded later against the recording and final shots. An empty guided
selection remains correct for that path, and the explicit contract permits a
guided voiceover strategy despite the general audio-led/native preference.
