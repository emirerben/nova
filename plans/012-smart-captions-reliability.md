# Plan 012 — Smart Captions reliability: make the captions do what the creator wants, every render

Status: IN PROGRESS — P0-1, P0-2, P1-3, and the P1-4 transcript cache IMPLEMENTED + tested
(2026-07-22). Remaining: P1-4 whisper biasing prompt and insertion-capable correction (the
transcript QUALITY levers), and the P1-3 keyword placement / suppress-claimed-span refinements
(need prod-image `make verify-overlays`). See "Implementation status" at the bottom.

Original status: DRAFT (fix plan from real-render diagnosis, 2026-07-22)
Motivation: PR-1 (plan 011, emphasis cue sizing + layout) shipped the intelligence, but two
real renders of the same talking-head clip (`ecfeca7f` @ prompt 2026-07-22.1, `ef92001a` @
2026-07-22.2) exposed that the OUTPUT is unreliable: a salient name ("Lionel Messi") sometimes
renders isolated and sometimes as "Lionel Messi he is" or bare "Messi", "number" fragments into
lone one-word captions, and a "number" overlay clutters the caption band.

This plan is diagnosis-driven and adversarially verified (each proposed fix was checked against
the code by a second agent; the checks killed the tempting-but-wrong fixes). The intelligence
(scene matcher + chunker) is not the problem — four specific mechanisms are.

## What's actually broken (root causes, each code-verified)

```
audio ──► whisper (openai-1) ──► cues ──► scene matcher ──► chunker ──► section overlays ──► burn
           [1 transcript                    [2 emphasis      [3 marker      [4 section-heading
            variance]                        LLM-only]        fragments]     overlay clutter]
```

1. **Transcript variance (separate-ticket).** `_transcribe_openai` (transcribe.py:249) calls
   whisper-1 with no determinism control, no biasing prompt, and NO cache — it re-transcribes
   from scratch every render (generative_build.py:8333/8501/8582). whisper-1 is non-deterministic,
   so the same audio yields different words run-to-run (drops "Lionel", splits "number one").
   The v2 correction pass is substitution-only + closed-vocab (caption_correct.py:299 enforces
   equal token count; :322 no-ops with no visual-asset aliases), so it can neither re-insert a
   dropped "Lionel" nor re-merge a split "number one". **Trap avoided:** `temperature=0` is a
   no-op — whisper-1 defaults to 0, and 0 is exactly the value that ENABLES whisper's
   non-deterministic temperature-fallback. whisper-1 exposes no seed. Caching is the real lever.

2. **Emphasis is 100% LLM-driven, no deterministic floor (pr1-emphasis).** A cue gets
   `smart_emphasis=True` only when the scene-matcher LLM emitted a standalone span whose words
   exactly equal that cue (captions.py:277). So when the LLM misses a name (render 2 tagged
   Mbappé/Elliot Anderson but not Messi), a lone "Messi" cue carries no emphasis. Also
   `_validate_emphasis_spans` de-conflicts standalone spans by pure start-time FIFO
   (planner.py:1091), so an adjacent marker span can evict a name span within the 1.5s gate.

3. **"number" fragments into lone cues (pr1-emphasis).** `_should_close` is hard-gated by
   `min_words` (=3 for cigdem/v2) at captions.py:63-64, so a 1-word cue can NEVER close on
   pause/char-cap. The only closes that bypass the floor are semantic (role change / boundary /
   standalone-span edge). The chapter/list-marker path stamps a role boundary right after
   "number", isolating it as its own cue — worsened when whisper splits "number one" into
   separate tokens.

4. **The "nu"/"number" overlay is a Smart Captions BUG, not the editorial sequence
   (separate-ticket, high-visibility).** On subtitled, every list-item the scene matcher tags
   becomes a `section_heading` TextLane → two `role="generative_sequence"` elements:
   `section_number` (the digit, y_frac 0.085) and `section_keyword` (y_frac 0.62). The keyword
   picker's stop-list is Turkish-only, so English "number" is NOT filtered → the overlay renders
   the literal word **"number"** (the wrong word — it should be the salient entity "MESSI") and
   sits at y_frac 0.62, colliding with the caption band at 0.705. `suppress_claimed_span` also
   failed to strip it from the caption, so the words duplicate.

## The fix plan (prioritized by impact on "captions do what I want")

### P0 — Deterministic emphasis floor: a salient name ALWAYS gets its own caption
Extends PR-1. Directly fixes render-2's "bare Messi, no emphasis".
- **In `app/smart_edit/captions.py` `build_semantic_caption_cues`**, at cue assembly: when a
  cue turns out to be a LONE cue whose words are a single proper-noun / named-entity span and it
  is not already `smart_emphasis`, promote it to `smart_emphasis=True` (apply the fix at the
  point of harm, not by flooding the span list). Guarantees the styling + min-hold on any
  isolated name even when the LLM misses it.
- **In `app/smart_edit/planner.py` `_validate_emphasis_spans`**, replace pure start-time FIFO
  (planner.py:1091) with a priority key: proper-noun/entity standalone > marker standalone, so a
  name is never evicted by an adjacent marker inside the 1.5s gate.
- Keep the LLM as the primary signal; add 2-3 few-shot examples to `_EMPHASIS_RULES_BLOCK` and
  bump `prompt_version` (cheap, improves the common case). Do NOT loosen the 1.5s gap / 10-span
  budget (they stop standalone strobing).
- Effort: ~half day. Guards: keep the spans-empty output byte-identical
  (`test_no_spans_matches_baseline_and_adds_no_keys`); wrap any deterministic detector in
  try/except returning [] so it can never drop matches/tags; skip sentence-initial capitals when
  ASR punctuation is absent; exclude non-entity capitals (brands/days).

### P0 — Kill lone "number" cues: markers lead into the name, never strand
Extends PR-1. Fixes the choppy one-word "number" captions.
- **In `app/smart_edit/captions.py` `build_semantic_caption_cues`**, after the chunk loop, add a
  merge-back pass: a sub-`min_words` marker chunk stranded specifically because a standalone-span
  close fired right after it (the `[marker]` chunk directly preceding a standalone cue) merges
  into its neighbor / leads into the name — never stands alone. Keep the "never merge into a
  standalone_range" and "don't cross a `boundary_after_word_ids`" guards.
- Gate the pass on the emphasis feature being active (spans non-empty) so the spans-empty
  byte-identity guarantee holds.
- Add a regression test for the whisper-split `"number one" → "number"` scenario; re-run
  `tests/smart_edit/test_captions.py` to prove spans-empty output is unchanged.
- Effort: ~half day.

### P1 — Fix the section-heading overlay (the "nu"/"number" clutter)
Separate concern but the most visible defect. Three coordinated changes:
- **Word:** source `section_keyword` from the scene-matcher's salient entity (via
  `emphasis_spans` / `matches` anchor) so the overlay shows **"MESSI"**, not "number"
  (`app/smart_edit/compiler.py` `_section_keyword`). Fallback: add English coverage to the
  keyword stop-list (`_KEYWORD_STOP` / `_marker_keyword_index`), scoped compiler-local so
  planner asset-matching stays byte-identical.
- **Placement:** raise `section_keyword` clear of the caption band (y_frac ≤ 0.5) so it can't
  crowd captions at 0.705 (preset coordinate; run `make verify-overlays`).
- **Duplication:** make `suppress_claimed_span` reliably strip the claimed list-item words from
  the caption so the overlay and caption never render the same line.
- Kill switch: `smart_caption_section_heading_enabled` (rollback lever, default-preserving).
- Effort: ~1 day.

### P1 — Transcript stability: same clip → same (and better) transcript every render
Separate-ticket, the hardest and highest-leverage for consistency. Sequence matters:
1. **Bias the transcription:** wire a short (<=224-token) `prompt` on the ordinary subtitled/
   smart-v2 path at generative_build.py:8333 (currently only the silence-cut verbatim case sets
   one, transcribe.py:243). Seed with a neutral enumeration/name primer plus proper nouns from
   the clip's Gemini analysis / plan entities so whisper keeps "Lionel Messi" whole. Flag-gated.
2. **Insertion-capable correction:** relax caption_correct's equal-token constraint
   (caption_correct.py:299) in a conservative mode that may prepend/append ONE adjacent
   proper-noun token or re-merge a split enumeration, synthesizing the inserted word's time by
   interpolating neighbor word times (preserve the per-word timing contract). Source vocabulary
   from clip/plan analysis, not just uploaded filenames.
3. **Content-addressed cache:** persist the FINAL (post-correction) whisper words keyed by audio
   content sha256 + language; reuse on re-renders/reburns (kills "two renders differ"). Run
   AFTER 1+2 so the cached transcript is the corrected one, not a frozen bad roll. Cache-bust on
   language override.
- Effort: cache ~1 day; bias ~1 day; correction mode ~1-2 days.

## Sequencing + scope

| Fix | Scope | Effort | Unblocks |
|---|---|---|---|
| P0 emphasis floor | extend PR-1 | ~0.5d | reliable name isolation regardless of LLM |
| P0 marker merge-back | extend PR-1 | ~0.5d | no lone "number" cues |
| P1 section overlay | new (compiler/preset) | ~1d | removes "nu" clutter, shows the right word |
| P1 transcript bias+correct+cache | new ticket | ~3-4d | cross-render consistency + name recovery |

- The two **P0** fixes land in the PR-1 emphasis work — they directly deliver "salient names on
  their own caption, cleanly" and are cheap + guarded. Do these first.
- The **section-overlay** fix removes the most visible clutter and is independent.
- **Transcript stability** is the deepest lever for run-to-run consistency; it's its own ticket.
  Honest caveat: caching makes the transcript STABLE, and bias+correction improve QUALITY, but
  no change makes whisper-1 perfectly transcribe every proper noun — this reduces, not
  eliminates, transcript-driven variance.

## What NOT to do (traps the verification caught)
- `temperature=0` on whisper-1 — a no-op (default is 0; 0 enables the non-deterministic
  fallback). whisper-1 has no seed.
- Flooding `standalone` spans from a detector without dedup/priority — reintroduces strobing and
  can evict real names; apply the floor at the point of harm instead.
- Loosening the 1.5s gap / 10-span budget to "catch more names" — causes standalone strobing.
- A blanket "merge every sub-min chunk" — over-broad; scope to the marker-before-standalone case
  and gate on the feature to preserve byte-identity.

## Tests each fix must carry
- P0 floor: lone proper-noun cue → emphasized even with empty LLM spans; sentence-initial
  capital NOT emphasized; spans-empty output byte-identical.
- P0 merge-back: whisper-split "number one"→"number" no longer strands; spans-empty byte-identical.
- Section overlay: keyword = entity not "number"; overlay y clear of caption band;
  no caption/overlay word duplication; kill-switch off = current behavior.
- Transcript: bias prompt threaded + flag-gated; correction inserts one adjacent proper noun
  with interpolated timing; cache hit returns identical words on re-render; cache-bust on language.

## Implementation status (2026-07-22)

| Fix | Status | Where | Tests |
|---|---|---|---|
| P0-1 emphasis floor | ✅ done | `smart_edit/captions.py` (`_is_lone_name_cue`, floor in result loop), `planner.py` (entity anchors, flag-gated) | test_captions.py floor tests + 500-trial byte-identity fuzz |
| P0-2 marker merge-back | ✅ done | `smart_edit/captions.py` (merge-back pass) | test_captions.py merge-back + boundary tests |
| P1-3 section keyword word | ✅ done | `smart_edit/compiler.py` (`_KEYWORD_STOP` English coverage) + kill-switch `smart_caption_section_heading_enabled` | verified: "number one Lionel Messi" → "Lionel"; smart_edit suite |
| P1-3 keyword placement / suppress-claimed-span | ⬜ remaining | preset y_frac + `_suppress_claimed_words` | needs prod-image `make verify-overlays` |
| P1-4 transcript cache | ✅ done | `pipeline/transcribe.py` (`transcribe_whisper_cached`, content-hash + GCS, fail-open), wired at `generative_build.py` subtitled path; flag `smart_caption_transcript_cache_enabled` | test_transcript_cache.py (hit/miss/flag-off/fail-open) |
| P1-4 whisper bias prompt | ⬜ remaining | `transcribe_whisper(verbatim_prompt=...)` hook exists; thread proper-noun primer | needs live eval to tune |
| P1-4 insertion-capable correction | ⬜ remaining | `caption_correct.py` equal-token constraint | riskiest — preserve per-word timing contract |

New config flags (all safe defaults): `smart_caption_section_heading_enabled=True`,
`smart_caption_transcript_cache_enabled=True`. Prompt bumped to `2026-07-22.2`
(named-entity standalone). All byte-identical kill switches preserved; 3408 tests pass.

Honest caveat: the transcript cache guarantees CONSISTENCY (same clip → same words on every
re-render), not first-transcription QUALITY. The bias prompt + insertion correction are the
remaining quality levers. The P0 fixes make names isolate and markers not fragment REGARDLESS of
which words whisper produced, so the render is robust even on an imperfect transcript.
