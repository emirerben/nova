# Creator sound-effect library (KRI-173)

~116 creator-ready effects in nine categories: rejection, approval, suspense,
transition, impact, comedy, ui, sports and money. They are published to the
`sound_effects` table and served by `GET /sound-effects`. Every file plays on
the iPhone renderer and in the iOS editor.

## Where things live

| Piece | Path |
| -- | -- |
| What ships (names, categories, search terms, voice flag, sources) | `src/apps/api/scripts/sfx_library/catalog.py` |
| Procedural recipes (numpy, seeded per slug) | `scripts/sfx_library/synth.py` |
| Pinned CC0 recordings (Kenney.nl packs, Freesound previews) | `scripts/sfx_library/sources.py` |
| Mastering + iPhone QA contract | `scripts/sfx_library/master.py` |
| Build / upload / rollback CLI | `scripts/seed_sfx_library.py` |
| Selection used by the AI placers | `app/services/sfx_catalog.py` |
| Guards | `tests/scripts/test_sfx_library.py`, `tests/services/test_sfx_catalog.py`, `tests/test_overlay_sfx_intent.py` |

Audio never enters git. `build` fetches the pinned sources into
`~/.cache/nova-sfx-library` (override with `NOVA_SFX_CACHE`) and writes to the
git-ignored `src/apps/api/.sfx-library/`.

## The file contract (`master.py`)

The device mixer has no per-clip fade and plays each file at its source level,
so every file must be correct on its own:

- 48 kHz stereo AAC in `.m4a`. `PlayableAudioFile` rejects Ogg, Opus and WebM, and
  the admin upload allowlist now rejects them too.
- The first audible sample is within 1 ms of the file start, after a 0.5 ms
  raised-cosine fade-in, so a hit lands on `at_s`. AVFoundation honours the
  AAC priming edit list, so this measures the same on the phone (checked with
  `AVAudioFile`).
- The tail fades out: the last 1 ms of what the player actually plays peaks below
  −45 dBFS. QA trims ffmpeg's AAC end padding (`decode_playable`) before it measures.
  Layers cut from a longer recording get 5 ms / 15 ms edge ramps, so they can't click
  mid-effect.
- One level rule for everything: the loudest 100 ms (K-weighted) is at −14
  LUFS, unless that would push the true peak above −1 dBTP. Very short clicks
  therefore land quieter; they are peak-limited.
- One-shots are at most 3 s; beds (crowds, loops, builds) at most 10 s.
- `deliver()` re-measures the DECODED `.m4a` and re-encodes until both limits
  hold, because AAC smears short transients. `build` fails on any QA problem.

The seven `smart-*` sound-design effects (`seed_smart_sfx.py`) are deliberately
quieter UI accents and are left unchanged.

## Build, check, publish

Run from `src/apps/api` with `PYTHONPATH=$PWD`:

```bash
python scripts/seed_sfx_library.py build        # ~40 s; writes manifest.json + index.html
open .sfx-library/index.html                     # listen before publishing
python scripts/seed_sfx_library.py upload --dry-run
python scripts/seed_sfx_library.py upload        # local API (ADMIN_API_KEY)
python scripts/seed_sfx_library.py upload --prod # prod, asks y/N (ADMIN_PROD_API_KEY)
```

Uploads go through the admin API (`upload-init-file` → signed PUT →
`upload-confirm` → PATCH). Each effect is PATCHed with `publish`,
`manual_audit_status: approved`, `category`, `search_terms`, `contains_voice`,
`provenance` and `license`. `category` and `search_terms` need migration 0109
deployed first; an older API silently ignores them, and a re-run fills them in.

The upload is idempotent, and it refuses a manifest with any QA problem. It only
touches rows the library owns: a row tagged with the entry's provenance
(`kria-sfx-library-v1:<slug> …`), or an untagged row with byte-identical audio (a run
interrupted between confirm and PATCH). A foreign row that happens to share
`<slug>.m4a` or an effect name, such as an admin page upload, is reported and never
modified.

- Same SHA-256: only the metadata is refreshed.
- Changed audio: the row is reported and left alone, unless you pass `--replace-audio`.
  That uploads and publishes the new row first, then archives the old one;
  existing placements keep their stored audio path.

**Rollback:** `python scripts/seed_sfx_library.py retract --prod` unpublishes
every row whose provenance starts with `kria-sfx-library-v1`. The rows and
their audio remain.

## Adding or changing an effect

1. Add an `Effect(...)` to `catalog.LIBRARY`. Choose a unique, plain name; a
   category; and search terms with the primary keyword first. Set
   `contains_voice=True` for speech, singing, laughter or crowds.
2. The source is one of:
   - a new `@recipe` in `synth.py`;
   - a Kenney pack member;
   - a new `FreesoundPin`. Its sound page must show
     `creativecommons.org/publicdomain/zero/1.0`, and you pin the preview's
     SHA-256.
3. `pytest tests/scripts/test_sfx_library.py`, then `build` and listen.
4. Upload. Order within a category matters: each effect's position in
   `catalog.LIBRARY` is stored as `catalog_rank` (migration 0109), and it is the
   curated tie-break the placers use, so put the headline sound first. It
   survives re-uploads; `created_at` only breaks ties among unranked rows.

Licensing: every current effect is either procedurally generated or CC0. The
issue waived KRI-143's CC0-only fence, but CC0 was available for every sound we needed.

## How the AI reaches 116 effects

- **Chat planner (`creator_sessions.py`):** the manifest holds a
  request-independent, category-round-robin slice (~20; `planner_catalog`). It
  has to be request-independent because confirmation re-derives the manifest
  hash without the chat. Effects outside the slice are still reachable. When a
  creator names or describes a sound ("add a buzzer sound effect"), the
  planning turn first tries exact names (`_resolve_explicit_sfx_outside_manifest`),
  then `_resolve_described_sfx` → `resolve_described_effect`. Every content word
  must be covered by one effect's name or search terms, and at least one must be
  specific to that effect, not its whole category. So "the right sound effect"
  or "a text sound effect" picks nothing and fails visibly. A refusal picks
  nothing either: "no", "not", "without", or "don't / do not / never" directly
  before the verb ("don't use the whoosh sound effect"; "…too much" is
  moderation, not refusal). The effect joins the planning view without changing
  the manifest hash, and execution revalidates it by id.
- **Placement agent (`autoplace.py`, 30-effect cap):** `placement_catalog` sends
  voice-free one-shots, role-tagged `smart-*` first, then one headline effect per
  category.
- **Overlay auto-SFX (`map_sfx_intent`):** the intent's purpose-built role wins,
  then a whole-word name match ("tap" never selects "Tape rewind"). It never
  picks a voice clip. The glossary is ordered by `created_at, id`.
- **Not covered yet** (KRI-143 P2): a sound effect the model proposes on its
  own, without the creator asking for one, is still dropped. The web edit
  copilot still sends only the first 20 effects of `GET /sound-effects`.
