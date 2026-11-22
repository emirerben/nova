# KRI-1585 informative text: decision trail

Each PR appends its rows. Newest phase last.

## Phase 0: Kyoto fixture (2026-11-22)

| # | Decision | Why | Evidence |
|---|---|---|---|
| 0.1 | The fixture is production's own record of job `3930c822-2d44-4251-bcf6-d45ef865c5ef`, not the replay's output. | The replay re-runs live models, so its words and layout differ from what shipped. Every PR proves itself against what the creator actually got. | `admin.py --prod GET jobs/<id>/debug`, `plan-items/28c65c59-…/proposal-trace` (two pages), `creation-threads/<thread>/events`. GET only. |
| 0.2 | Freeze the job debug pulled at 11:48 UTC, before the creator's later editor save. | The creator edited this job in the iPhone editor at 11:53 UTC, after it rendered. Row 0 moved to 0.676 s and a hook row was added ("Kyoto’nun en guzel 25 dakikasi", `#FFF0A6`, 0–2 s, Montserrat Bold 72). The fixture pins the render as produced. The edited rows are kept in `text_rows_after_editor_save.json`. | `freeze --job-debug <saved debug>`. |
| 0.3 | For PR1: the 11:53 editor save left `creator_style_fields` as `[]` on the moved row and absent (`None`) on the added row. | Today an editor save does not record which fields the creator changed, so a regeneration would treat those edits as automatic values. This is the first thing PR1 must prove or fix. | `text_rows_after_editor_save.json`. |
| 0.4 | Research comes from the 10:15 UTC pass. The 08:01 pass is not used. | The rows' `retrieved_at` (10:15:05) names the pass that wrote them. The 08:01 pass belongs to the earlier draft, before "Bunlar olmadan hazırla". | `research.json` `public_facts.created_at`. |
| 0.5 | Facts are stored in three tiers: all 16 raw facts, the 6 given to the writer, and the 1 cited per row. Both 10:15 `caption_binding` attempts are kept with their `correction` and `snapshot` windows. `claim_review` is kept too. | PR2 needs the uncited facts to show the label-grounding bug, and the windows to feed the writer offline. | `research.json`. |
| 0.6 | "Kyoto Demiryolu" occurs only in fact-1. fact-1 was given to the writer but is not cited by row 1, which cites fact-11 ("Tren"). "Hozukyo Geçidi" (row 2, cites fact-8) occurs only in fact-6 and fact-7. Row 0 keeps "7.3" from fact-6 and drops its "25 dakika". "Sagano Romantik Treni" is in no fact. | This confirms the teardown's inferred label-check gap: the check searches every fact, not the cited one. Recorded as `research.label_grounding` assertions. | String search over `research.json` `all_facts`. |
| 0.7 | The repository is public, so the fixture is built by allowlist. Owner and thread ids become `<owner>`/`<thread>`. Signed URLs, `output_url`, `video_path` and `poster_path` are dropped. Proposal media keep only kind, lane, aspect, id, duration and generation, plus analysis width, height, subject and exposure. Clip transcripts, descriptions, people notes and `user_context` are dropped. `freeze` fails if an id, `X-Goog-`, `Signature=` or a `"transcript"` key survives. | Creator data and footage never enter git. Recorded digests (`media_digest`, `base_digest`, `contract_digest`) were computed over the unscrubbed paths and will not recompute from the fixture. | `scripts/kri1585_kyoto_fixture.py freeze`. |
| 0.8 | Footage and frames live in the gitignored `.cache/kri1585-kyoto/`. `scripts/kri1585_kyoto_fixture.py cache` fetches them through `replay_job.py --fetch-only` (GET only, guarded by owner prefix) and checks each proxy against the sha256 in `media_manifest.json`. It then writes the pipeline's own `sample_background` grid (108×192 gray) plus a PNG at 2/25/50/75/98 % of every text window and every clip window. That gives 50 anchors. | These are the anchor fractions `_ensure_generation_environment` uses. Clip windows are included because B1 places one line per clip. | `.cache/kri1585-kyoto/frames.json`. |
| 0.9 | `make kria-replay-job` accepts a successful (`variants_ready`) job. The run with `MAX_COST_USD=2.00` ended `diverged_from_production`, with no device plan. On "Bunlar olmadan hazırla" the replay's turn replied with a partial-fit note instead of the approval production reached. Spend was $0.511. | Divergence is a replay note, not a fixture blocker, since 0.1 uses production's record. The run was not repeated, so this is the single $2 pull. | `/private/tmp/nova-replay-job/3930c822-…/runs/20261122T115125Z/replay-receipt.json`. |
| 0.10 | Production's row colours are a regression anchor in `expected_rows.baseline`, not a test. | A crude box (the row's `max_width_frac`, ±0.07 height) through `automatic_fill` on the cached frames reproduced 2 of 3 colours. Row 1 came out white where prod had black. Prod sampled at most 12 frames in 18 s across all rows, and the real box comes from the measured text. PR5 owns the contrast proof with the real box. | Ad hoc check, not committed. |
| 0.11 | Default design for this ask is B1. | The request asks for "bilgilendirici alt yazı" (informative subtitles). The plan sets B1 for subtitle asks and A2 for other asks. | `plan.json` `creator_messages[0]`. |

### Teardown measurements (1080×1920)

The stills are 540×960. The mock-ups were drawn at 1080×1920 with Pillow and scaled down by 2. Each size is the Pillow font size whose ink width, rendered with the repo's own font file, matches the still's ink width ×2.

Calibration: V0, the real prod render recorded at 62 px Schibsted Grotesk Bold, fits at 62–63. Tolerance is about ±2 px.

| Design | Element | Font file | Size px | Ink notes |
|---|---|---|---|---|
| A1 | hook | Literata-SemiBold | 91 | 2 lines. The widest line is 852 px. Ink y is 704–914. |
| A1 | heading "7,3 KM" | Inter, tracked caps | ≈32 by cap height | `#FFE7A0`. Tracking makes a width fit invalid. |
| A1 | detail | Literata-SemiBold | 53 | left, x = 80 |
| A2 | hook | SchibstedGrotesk-Bold | 98–99 | 2 lines. The widest line is 828 px. |
| A2 | heading "7,3 km" | SchibstedGrotesk-Bold | 64 | `#FFE7A0`, x = 78 |
| A2 | detail | Inter-Regular | 36 | Regular fits within 1 px. Medium misses by 9. |
| A3 | hook | InstrumentSerif-Regular | 128–130 | 2 lines. The widest line is 834 px. |
| A3 | heading | InstrumentSerif-Regular | 81–83 | centred |
| A3 | detail | Inter | 36–38 | centred |
| B1 | hook | n/a | n/a | `b1_still0.jpg` is byte-identical to `a3_still0.jpg` (same sha256). B1's hook still is A3's image, so it is not an independent measurement. |
| B1 | line | Inter-Medium | 51 | It fits 51 on three lines (`b1_still3/5/6`). Bold fits 49–50 and is less consistent. White, centred. Ink y is about 1198–1312. |

The hook size depends on the design, so there is no single number. The widest hook line is 830–850 px in all three A designs. That suggests the mock-ups fit to about 0.78 of the width, not a fixed size. The registry owner chooses between a fixed size and fit-to-width.

For PR3: `assets/fonts/Inter-Medium.ttf` and `Inter-Regular.ttf` already exist in the bundle, which the plan did not expect. Check whether they are in `font-registry.json` and the iOS bundle before adding new files.
