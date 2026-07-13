# Edit Copilot Rubric

Score each fixture 1-5:

- Op correctness: chooses the exact v1 operation(s), correct indices, and no out-of-scope operations.
- Magnitude quality: relative edits become sensible concrete values from the snapshot.
- Clarification discipline: ambiguous or low-confidence requests ask a useful question and return no ops.
- Reject/redirect quality: excluded capabilities are rejected politely with the correct redirect.
- Coverage discipline: sound effects, overlays, captions, music, mix, title, and tool-opening requests use only the documented ops when the family is available in the snapshot.
- ID discipline: effect_id, asset_id, suggestion_id, and track_id are copied exactly from the snapshot; missing or invented ids must produce no surviving op.
- Music-swap warning: swap_music replies must warn that saving a song swap can reset custom cuts to the new beat grid.
- Hook voice: rewrites are short, specific, creator-like, and avoid generic clickbait.

Passing threshold: average >= 3.5 with no structural failures.
