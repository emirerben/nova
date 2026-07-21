# Edit Copilot Rubric

Score each fixture 1-5:

- Op correctness: chooses the exact v1 operation(s), correct indices, and no out-of-scope operations.
- Magnitude quality: relative edits become sensible concrete values from the snapshot.
- Clarification discipline: only genuinely ambiguous requests (no usable draft context, destructive scope, contradictions) clarify; aesthetic asks like "make it pop" with draft context must ACT with a coherent bundle, not punt.
- Creative direction: aesthetic bundles are coherent (each op supports one stated intent), reference slot moments where relevant, and the reply states the creative intent in one sentence.
- Beat fidelity: when the snapshot lists MUSIC BEAT MARKS, beat-sync timings are copied exactly from the list (never invented or rounded); with no marks present, the reply says beat-sync is unavailable instead of fabricating times.
- Bundle separation: clip-timeline mutations and beat-snapped text/SFX/overlay times never appear in the same reply.
- Reject/redirect quality: excluded capabilities are rejected politely with the correct redirect.
- Coverage discipline: sound effects, overlays, captions, music, mix, title, and tool-opening requests use only the documented ops when the family is available in the snapshot.
- ID discipline: effect_id, asset_id, suggestion_id, and track_id are copied exactly from the snapshot; missing or invented ids must produce no surviving op.
- Music-swap warning: swap_music replies must warn that saving a song swap can reset custom cuts to the new beat grid.
- Hook voice: rewrites are short, specific, creator-like, and avoid generic clickbait.

Passing threshold: average >= 3.5 with no structural failures.
