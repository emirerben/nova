# KRI-852 acceptance and failure modes, before implementation

Base 49edd1c3473f00282b541aa07a618620514359af. Single implementation lane.
Original intent: whole cleaned Talking segment delete, then trim/slide/reorder,
Save, reopen and export retain requested speech and its matching captions/text.

Six contract decisions:
1. Canonical ordered authored slots own source windows and duration; source-to-output
   projection belongs to speech_cut_state, alongside existing cleanup mapping.
2. Speech captions/words and source-attached text/visual/SFX/drawings follow source
   occurrences; crossing windows split and removed portions disappear. Continuous
   music stays on output time. Independent voiceover remains independent.
3. NativeEditorTimeline projection duration bounds scrubbing, playback and geometry;
   authored recipe duration must agree, including capped overlaps.
4. Source edges trim, never stretch content; explicit playback rate converts source
   to output time. Existing geometry's 0.1s minimum and 50ms bound slack apply;
   recipe source reads stay inside immutable asset bounds. Source-start is locked.
5. Clip and all affected lanes share one document undo snapshot. Drag previews
   project from immutable gesture baseline; Undo restores everything.
6. Paired API/native fixtures cover source order, bounds, captions/words, lanes,
   undo and recipe duration. Native renderer export is separate from API proof.

Failure modes: valid window rejected as stale; changed source admitted; missing/
foreign slot; stale generation; concurrent Save overwrite; source overflow; removed
word retained in a clipped caption; reorder attaches words to wrong occurrence;
repeated edits accumulate rounding; restored ranges lose text; double remap on Save;
client/server parity drift; continuous bed shifts; preview differs from export;
caption mirrors render twice; original audio is absent/mismatched; reopening resets
source windows; unsupported original media access mistaken for successful proof.
No new model calls, flag changes, original draft/media mutation or deployment.

Additional boundaries checked before final repair: an empty projected caption lane
must not erase the original need for transcript coverage; source-attached karaoke
word offsets are relative to their text element, while caption words are absolute;
a 300s source and 120 occurrences must retain one source transcript pool rather
than a full transcript copy for every occurrence. No word-count cap is assumed.
An automatically projected caption lane sent by iOS must also retain missing
transcript recovery; merely supplying that lane does not confirm new caption text.
