# KRI-689 chat-requested clip speed at creation: failure-mode list (written before the code)

Request: a creator asks in chat, while creating a phone Montage, for footage to play faster or
slower ("make the walking clip 2x", "slow motion on the pour", "Nehir çekimlerini 2 kat
hızlandır"). Today the draft says "I can't change clip speed on your iPhone yet" and the clips
play at 1x, although the phone render contract (`playback_rate` on a guided moment, `rate` on
the recipe clip, verified `variableSpeed`) and the post-creation editor chat already retime.

Acceptance (what the creator sees): the draft and approval card say which clips will play at
which rate (or why not); the finished phone video plays exactly those clips at that rate; every
other clip, label and the total length are unchanged; locked edits (song, lip-sync, voiceover,
photos, frozen frames, speed ramps) get a plain "Couldn't … because …", never a silent 1x.

Boundaries traced: creator words → brief requirement (`kind=style`, `facts.playback_rate`) →
`speed_ask.speed_spec` → `unified_montage.brief_view` → `plan_unified_montage` grounding →
`FastMontageCut.playback_rate` → `guided_story.compile_execution_plan` moment
`playback_rate` → `phone_guided_plan.compile_phone_guided_plan` clip `rate` → device render →
`brief_checks` receipt (draft preflight, render record, finished video review).

Test files: `tests/kria/test_kri689_chat_speed_spec.py` (spec parsing + receipts + notice),
`tests/tasks/test_unified_montage_dispatch.py` (end-to-end dispatch → compiled phone plan),
`tests/pipeline/test_phone_guided_speed.py` (cut → moment → recipe math),
`tests/tasks/test_kri689_speed_review.py` (finished-video review), the KRI-689 Postgres draft
test. T = covered there; DEV = device check owed (see the end).

## A. Reading the ask

| # | Failure mode | Catch |
|---|---|---|
| A1 | The rate is read from the wrong place: words win over a recorded `facts.playback_rate`, or `playback_rate: 1` is read as a change | T: a recorded fact decides first; 1.0 is no ask |
| A2 | "slow down 2x" is read as 2x (faster) instead of 0.5x; "0.5x" or "half speed" is inverted | T: direction + multiplier matrix (EN/TR) |
| A3 | "slow motion" / "ağır çekim" with no number yields no rate; "speed up" / "time-lapse" with no number yields no rate | T: defaults 0.5 and 2, receipt says the creator named no number |
| A4 | A rate outside the native range (10x, 0.1x) is passed through and the compiler refuses the whole plan | T: clamp to 0.25–4, receipt says "4x, not the 10x you asked for" (partial, never "met") |
| A5 | Decimal rates ("1.5x", "1,5x") are split into two numbers | T: 1.5 and 1,5 parse |
| A6 | A pacing / text / music ask is read as a speed ask ("fast pacing", "slow title animation") — regression of the KRI-689 guard | T: existing negative list stays green |
| A7 | A speed ramp ("speed ramp on the jump") is applied as a constant rate | T: ramp words → honest "can't ramp speed inside a clip" |
| A8 | The target words still contain ask words ("shots", "clip", "up", "2x") so moment matching fails or matches the wrong clip | T: target words for the prod wording are exactly ("river",) |
| A9 | A Turkish ask ("Son klibi yavaşlat", "Nehir görüntülerini 2x hızlandır") loses its target or position | T: TR matrix |
| A10 | A speed ask filed as `timing` carries `duration_s` that becomes the video's length | T: existing `times_one_moment` guard |
| A11 | A superseded / removed requirement is still planned | T: `live_speeds` reads `brief.live()` only |

## B. Which clip gets the rate (grounding)

| # | Failure mode | Catch |
|---|---|---|
| B1 | Rate applied to the wrong clip: target words match one clip's analysed moment but the rate lands on another cut | T: dispatch test with two described clips; the negative control asserts the other clips keep `playback_rate` None |
| B2 | Target words match no clip (or two clips): a guess is made | T: `not_grounded` → honest "couldn't tell which clip", rate on nothing |
| B3 | "the last clip" / "son klibi" is grounded by words instead of cut position | T: position → final cut only |
| B4 | A `clip:<media_id>` scope is ignored in favour of words | T: scope wins |
| B5 | Whole-edit ask ("speed up the footage", "Klipleri hızlandır") applies to photos too | T: only video cuts; a photo-only target → "a photo has no speed" |
| B6 | Rate applied to a cut that holds a freeze frame (compiler: "a freeze frame needs a moving clip at normal speed") | T: freeze-held cuts are skipped; all-held target → "Couldn't" with the freeze reason |
| B7 | Two asks name the same clip (first 2x, later 0.5x): both or the earlier one win | T: the later ask wins; the earlier receipt says so |
| B8 | A creator-pointed moment (KRI-723 `pinned`) is bypassed by analysed-moment matching | T: pinned words win |

## C. Timeline math

| # | Failure mode | Catch |
|---|---|---|
| C1 | The sped-up cut changes length so later cuts, labels and the total length drift | T: with no approved windows the OUTPUT length of every cut is preserved (source span = duration × rate); the compiled plan's total and every other moment's output window equal the no-speed plan byte for byte |
| C2 | 2x needs more footage than the clip has: the source window runs past the clip (compiler: "source window") | T: the cut keeps its length and the rate is lowered to what the footage covers (`footage_limited`); the receipt says "2.5x, not the 4x you asked for". A cut that already shows its clip whole has nothing to speed through: `no_footage`, an honest "Couldn't" (the dispatch fixture's whole-clip cuts hit exactly this) |
| C3 | An approved source window (creator-picked moment) is widened or narrowed to keep the slot length, losing the moment | T: approved windows keep their exact `source_start_s`/`source_end_s`; duration = span / rate (#1761's anchor) |
| C4 | `FastMontageCut` validator rejects `output_duration_s != source span` | T: validator honours `playback_rate` (and still rejects a wrong duration) |
| C5 | `compile_execution_plan` recomputes `source_end_s = start + duration` at 1x, undoing the rate | T: moment carries `playback_rate`; `source_end_s = start + duration × rate` within the cut's window |
| C6 | Millisecond rounding × rate 4 exceeds the compiler's 1 ms tolerance and refuses the plan | T: 0.25/0.5/2/4 all compile through `compile_phone_guided_plan`; tolerance scales with the rate |
| C7 | `_quantize_quick_mixed_timeline` (mixed photo/video timing) re-anchors `source_end_s` at 1x | T: rate honoured there too |
| C8 | Slow motion on a cut shorter than the editor floor after the span shrinks | T: span ≥ 1 frame; output unchanged |
| C9 | The recipe clip gets `rate` but the plan omits `variableSpeed` from `required_capabilities` → phone refuses the recipe | T: `required_capabilities` carries `variableSpeed` exactly when a rate ≠ 1 is present |
| C10 | The editor revision built from the plan drops the rate, so the first Save resets to 1x | T: `GuidedEditorSegment.playback_rate` is carried from the moment |

## D. Gates and honesty

| # | Failure mode | Catch |
|---|---|---|
| D1 | A rate is planned while `variableSpeed` is not verified → the phone refuses the whole video | T: planner gate (`clip_speed_planned_for`), receipt keeps the honest "can't change clip speed on your iPhone yet" |
| D2 | A rate is planned on a song montage (beat-snapped cuts, lyric captions) or a lip-sync take | T: `song_duration_s` → every speed ask "locked: the clips follow your song" |
| D3 | The voiceover-narrated montage (another writer) is promised speed | T: `clip_speed_notice` stays None for the voiceover lane; the narrated writer never reads speed specs |
| D4 | The receipt claims "2x" but the compiler clamped or the finished timeline carries no rate | T: `rate` is the clamped value; a finished variant whose moment rows lack `playback_rate` → "dropped" (not_possible) |
| D5 | The draft is approved without a word about a speed the plan refused (regression of KRI-689) | T: locked cases keep the notice; preflight returns the plan's speed receipts so the draft says "Done: … (clips 1–3 play at 2x)" or "Couldn't …" before the card |
| D6 | The honoured speed turns the first draft into the "simpler version?" question or blocks the render | T: applied → `met`; locked reasons are format limits; only `not_grounded`/`no_rate` are creator calls |
| D7 | A legacy record (written before this change, no `clip_speeds` key) is judged as if speed were supported | T: no key → KRI-689's "Couldn't" stays byte-identical (review test fixture) |
| D8 | The Turkish chat gets English receipt text | T: TR twins for every new reason |
| D9 | Prompt text changed without bumping `MAIN_CREATOR_PROMPT_VERSION` / `BRIEF_EXTRACTOR_PROMPT_VERSION` (the extractor derives its section from the Main Creator's) | T: version pins in `tests/agents/`; replay evals |

## E. Device check owed (not provable offline)

DEV: on the iPhone, a Montage from ≥3 clips with "speed clip 2 up 2x": export, then
`ffprobe` the rendered segment of clip 2 (cut boundaries from the plan's `story_timeline`):
its on-screen duration must equal the plan's `output_duration_s`, and the footage shown must
span `2 × output_duration_s` seconds of the source (compare a frame at the segment's end with
the source at `source_start_s + 2 × output_duration_s`); clips 1 and 3 keep their 1x windows.
