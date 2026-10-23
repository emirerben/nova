# KRI-852 acceptance and failure modes, before implementation

Initial base 49edd1c3473f00282b541aa07a618620514359af. Single implementation lane.
Landed main through a16e51751 was integrated normally; no unmerged PR was stacked.
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


## Result and limits

A cleaned, source-backed Talking segment can be deleted, then trimmed, slid or
reordered without the former 409 window guard. The immutable source catalogue,
creator ownership, source generation, authored slot identity, source bounds and
concurrent Save checks remain authoritative. Cloud composite tracks remain
unsupported for authored source edits.

The API and iOS project captions/words and source-attached text, visuals, motion,
camera, drawings, overlays and SFX through the same ordered source occurrences.
Crossing intervals split; overlapping point events have one right-biased owner.
Continuous music and independent narration keep their own clocks. Existing
sub-100ms speech slots round-trip down to 1ms; the editing handle's existing 100ms
minimum remains. Stable source anchors preserve temporarily trimmed text across
Save/reopen and extension; caption mirrors are excluded from authored text.
Only omitted lanes are server-projected. Submitted lanes already use their
submitted output clock and are never remapped a second time.

Full transcript timing is reused only from the existing preflight snapshot after
matching the original source path, generation and complete window. One source
caption pool serves all occurrences, with sparse per-record source anchors.
The 300s / 120-slot fixture retains 1500 unpunctuated words below 300KB of serialized
basis; this is a representation assertion, not an added word/admission cap.
The first run exposed a >30s general sentence-balancer timeout; new source cue
planning now uses bounded batches of the existing eight-word phone budget.

Without that transcript, newly exposed speech can still Save as an exact durable
draft under existing editor protocol 2. Preview/export reports a caption timing
prerequisite. Restore the prior range, author replacement captions, or disable
captions to recover. Sending iOS's automatically projected caption lane, or
repeatedly saving a now-empty visible caption lane, cannot clear that requirement.
An explicit deletion remains lane-specific. Protocol 1 keeps its existing
unsupported-phone-edit response for that missing prerequisite. No new client
protocol, capability, feature flag, prompt, provider call or migration is required.

## Executed evidence

Implementation revision: `d682bcab41afe4285527048ba7be714380ef2627`.
Subsequent evidence-only documentation commits preserve the recorded API/iOS
source-tree identities in `kri-852-native-output.json`.

- Initial red controls: real authored trim/slide/combined Save returned
  `TIMELINE_STALE`; reorder retained the wrong source words. Additional red
  controls cover restore-after-reopen, fractional overlap points, exact source
  endpoints, caption mirrors, relative karaoke word offsets, empty caption
  recovery and native auto-projected caption recovery. Retained logs are in the
  artifact directory's `logs/` subdirectory.
- API adjacent checks: **338 passed** (Save matrix, speech state, authored/phone
  compiler, phone commit, empty drafts, code catalogue, reliability schema).
  Command: `src/apps/api/.venv/bin/python -m pytest
  src/apps/api/tests/routes/test_editor_save_contract_matrix.py
  src/apps/api/tests/pipeline/test_speech_cut_state.py
  src/apps/api/tests/routes/test_editor_save_code_catalog.py
  src/apps/api/tests/pipeline/test_authored_timeline.py
  src/apps/api/tests/pipeline/test_phone_authored_timeline.py
  src/apps/api/tests/routes/test_phone_editor_commit.py
  src/apps/api/tests/services/test_editor_empty_drafts.py
  src/apps/api/tests/gates/test_reliability_gates.py -q`.
- Final named incident gate: **159 passed, zero environment skips/errors**,
  including **26** durable scratch-Postgres checks with DB reload, immutable
  recipe inspection, simultaneous Save, owner/generation/source/bounds controls
  and both caption recovery bypasses. Command:
  `DATABASE_URL=postgresql://emirerben@localhost:5432/nova_kri_852_speech_editor_test
  python3 scripts/reliability-gates.py run kri-852-speech-editor`.
  This `open_pr` gate is run explicitly here; normal changed-gate selection only
  selects merged gates. Its named journey owns the new CI case.
- Native: **320 passed**, comprising Session (227), RenderCompiler (61), Sources
  (19), SaveMatrix (4), SaveCodeCatalog (5), SpeechTimeline (4). Built after main
  integration using the task-owned iPhone simulator
  `8F9AAA8A-67F5-4A0C-A9D1-8F03041F0116`, generated project, signing off and
  parallel testing off. Exact selected classes and command are in the receipt.
  `.dev/kri852/DerivedData/Logs/Test/Test-Kria-2026.10.23_09-38-39-+0300.xcresult`
  retains the result bundle. API-only recovery hardening did not alter that iOS
  source tree.
- CI mapping: all changed paths mapped; `kri-852-speech-editor` requires native
  evidence and the four API regression files. Existing broad shared-path journeys
  remain selected normally; their full remote CI result is pending.

## Inspected native output

Local durable artifact root:
`/Users/emirerben/.codex/visualizations/2026/10/23/01a16750-30f9-7cd1-b2e8-7560519bbd1f/kri852`.
Final recipes, DB-reloaded sections, immutable request identities, exported MP4s,
caption/title contact sheet and independent verification report are in `verified/`.
Scripts and the minimal macOS runner are preserved in `reproduce/`; no media is
committed to Git. The tracked JSON receipt records byte hashes and source trees.

This is constructed QA using retained synthetic English speech over Troya footage,
not the original failing phone draft. The historical take was continuous with
cleanup off; QA creates a three-segment cleaned baseline from source intervals
0–7s, 9–16.3s and 18–25.788s, then performs these actual Save/reopen steps:

| Stage | Ordered source start + duration | Output duration |
|---|---|---|
| Delete middle | 0 + 7; 18 + 7.788 | 14.788333s |
| Trim last | 0 + 7; 20.8 + 4.5 | 11.5s |
| Slide last | 0 + 7; 20.5 + 4.5 | 11.5s |
| Reorder combined | 20.5 + 4.5; 0 + 7 | 11.5s |

The uninstrumented current KriaMediaEngine exports these recipes on macOS via
AVFoundation. Diagnostic branding is disabled. Word-highlight captions and the
source-attached “QA Museum” title were inspected in the contact sheet. The title
follows source 21.2–22.5s: output 10.2–11.5, 7.4–8.7, 7.7–9.0 and 0.7–2.0s.
An earlier QA fixture had a noncanonical font/text-mode setting and omitted the
title; that attempt is retained in `word-title/` and is not title evidence.

Reproduction (from the task worktree, with the retained artifacts available):

```sh
# Scratch DB only; this injects the retained transcript, never calls a model.
cd src/apps/api
DATABASE_URL=postgresql://emirerben@localhost:5432/nova_kri_852_speech_editor_test \
KRI852_OUTPUT=/private/tmp/kri852-replay OPENAI_API_KEY=sk-test GEMINI_API_KEY='' \
.venv/bin/python /private/tmp/nova-kri-852-speech-editor/.dev/kri852/build-qa.py
cd /private/tmp/nova-kri-852-speech-editor
swift build --package-path .dev/kri852/export-runner --scratch-path .dev/kri852/export-build
.dev/kri852/export-build/debug/ModelExport /private/tmp/kri852-replay/requests.json
src/apps/api/.venv/bin/python .dev/kri852/verify-exports.py /private/tmp/kri852-replay
```

The oracle uses the independently declared intervals above, not the mapper's
result. It checks each saved word and title time, recipe source windows, full MP4
decode, output duration, source picture regions and original audio. Wrong-segment
captions retain the correct count/timing but rotate their words and fail the
source oracle. A wrong-source audio reference retains the same output clock and
fails the same correlation threshold.

Audio comparison decodes both media onto their presentation clock with
`aresample=async=1:min_hard_comp=0.000001:first_pts=0`. Packed raw PCM ignores source
AAC timestamp discontinuities and initially reported a misleading 42.7ms shift;
that failed attempt and the clock probe are retained. The final measurement keeps
a strict 1ms maximum lag and >0.95 correlation at the measured lag. Worst observed
lag is 62.5 microseconds, minimum aligned correlation exceeds 0.982, maximum
picture-region mean absolute error is below 2.1/255. Zero-delay correlations are
also recorded; they are not silently substituted for aligned values.

## Regression checks brought over from #1788 (2026-10-23)

#1788 was an iOS-only stopgap for the same ticket that greyed Range / slide / move out on a
speech segment. Its fixture and rows were brought into this branch and inverted so they prove
the edit works; #1788 stays unmerged and is closed once this lands. Details and the row table:
`docs/reviews/kri-750-save-failure-modes.md` §12.

- Save matrix (`tests/routes/test_editor_save_contract_matrix.py`, rows `talking-segment-*`,
  fixture `talking_cut_job` / `talking_segment_save` in `_editor_save_matrix.py`): a 10 s take cut
  at two pauses with one caption per segment, saved with editor protocol 2 like the iPhone.
  Deleting the middle segment drops its caption and moves the later caption with its clip
  (5-7.5 s -> 2-4.5 s); Range saves with the caption clipped to the window; reorder moves captions
  with their clips; a slide into speech that has no transcript saves exactly and sets
  `speech_timing_unresolved`. With protocol 1 the same slide is refused as `unsupported_phone_edit`
  (the pre-existing protocol-1 response, unchanged).
- Native (`NativeEditorSessionTests`, `testTalkingSegment*`): the same four edits on a session whose
  slots and cues come from the server snapshot (a renamed slot would mint a new clip id and bypass
  the guard under test). Captions follow in the same undo step; `source_start_locked` is written on
  a moved source start; `requiresCaptionTiming` fires only for untranscribed speech.
- Registered in `scripts/ci/journey-manifest.json` (`kri-852-speech-editor`) and the gate's
  covered neighbours.

Results after merging `origin/main` (1cfb929d5):
`python3 scripts/reliability-gates.py run kri-852-speech-editor` with a scratch Postgres:
**163 passed, 0 environment skips/errors** (159 before, plus the four rows). Neighbouring API
suites (matrix, speech state, code catalogue, authored/phone compilers, phone and subtitled commit,
empty drafts, gate schema): 382 passed. Simulator (iPhone Air, iOS 26.5, arm64):
`NativeEditorSessionTests/testTalkingSegment*`, `testTalkingSourceClipHasOneUndoableTrimGesture`
and `NativeSpeechTimelineTests`: 9 passed.

## Remaining acceptance and mandatory-check limits

- **KRI-771:** original physical iPhone Save/reopen/export is still open. The
  original draft/media were untouched; simulator tests and macOS native exports
  do not establish physical-device or original-incident acceptance.
- **#1761:** optional exact `source_end_s` projection compatibility is tested by
  the paired held-tail fixture without inferring an effective rate. Its
  unmerged authored resolver/compiler was not stacked; combined final endpoint /
  speed/export proof remains pending after it lands. The draft does not wait on
  that separately held work.
- **KRI-724 / #1760:** occurrence-specific phrase planning, processed audio and
  noise work remain separate. #1765's old speech-window refusal assertion is
  superseded by positive trim/slide/reorder checks, while source identity guards
  remain covered.
- Mandatory `make verify-editor-timeline` was run before the standing no-web
  direction: 209 Jest checks passed; desktop Playwright had 21 passed / 7 failed
  (timeouts and aborted/detached navigation). The same seven passed unchanged in
  a sequential retry. The full Make invocation remained **nonzero** and did not
  reach its mobile section. No proven root cause is claimed and no web source
  was changed. There was no further browser investigation/repair after the
  iPhone/native scope direction. This historical web-only check is not presented
  as a green full gate.
- Deployment, rollout, physical request fulfillment and remote CI acceptance
  remain pending. No release metadata, secrets, production media or flags changed.
