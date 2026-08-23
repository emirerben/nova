# Nova edit-feedback contract

Use exactly these dimensions and ratings. The API rejects unknown values.

## Dimensions

| Dimension | Apply to |
| --- | --- |
| `overall_quality` | The holistic quality and publish-readiness of the resulting edit. |
| `ai_guidance_and_response` | Nova's conversational response: understanding, questions, explanation, and honesty about what it changed. |
| `instruction_fit` | Whether the rendered/staged edit follows the creator's stated request. |
| `hook` | The opening moment and its ability to create immediate interest. |
| `pacing` | Overall rhythm, energy, density, and time spent on moments. |
| `cuts` | Individual cut points, cut duration, trimming, and cut style. |
| `clip_selection` | Which source moments Nova chose or omitted. |
| `clip_ordering` | The sequence of otherwise selected clips and the resulting build/payoff. |
| `captions` | Speech subtitles: wording, timing, readability, and styling. |
| `text` | Titles, thoughts, labels, and other non-caption on-screen copy. |
| `transitions` | Hard cuts, dissolves, wipes, and other between-shot transitions. |
| `music` | Track choice, musical fit, and beat relationship. |
| `audio` | Speech/original sound clarity, levels, mixing, muting, and sync. |
| `effects` | Speed changes, filters, motion treatments, and other visual effects. |
| `overlays` | Images, video cards, stickers, graphical layers, and their placement. |

### Disambiguation

- Slow energy across a section is `pacing`; an individual cut landing late is `cuts`.
- The wrong source moment is `clip_selection`; the right moments in the wrong sequence are `clip_ordering`.
- Subtitle feedback is `captions`; title or editorial copy feedback is `text`.
- Track choice/beat fit is `music`; dialogue, ambience, volume, or mix is `audio`.
- Nova saying it applied an unsupported or failed operation is `ai_guidance_and_response`; the final output not matching the request is `instruction_fit`. A comment may legitimately rate both when it describes both failures.

## Ratings

- `good`: approved with no identified issue in that dimension.
- `bad`: substantively wrong or undesirable.
- `mixed`: meaningful positive and negative evidence in the same dimension.
- `not_applicable`: the dimension is absent or irrelevant to this artifact.

Every `good`, `bad`, or `mixed` annotation requires a non-empty rationale. Use `No other issue noted in this review pass.` only after Emir confirms the grouped remaining-good interpretation.

## Payload constraints

- Send 1–15 unique dimensions in one bulk request.
- `rationale`: at most 4,000 characters.
- `frame_start_s` and `frame_end_s`: both present or both absent; start must be non-negative, end must be greater than start and no later than the artifact duration.
- `supersedes_annotation_id`: omit or set `null` for a new dimension; use the exact current annotation ID for a correction.
- A bulk request is atomic: any invalid or stale entry prevents all entries from being appended.

The detail response includes annotation history. Treat an annotation as current when `is_current` or `current` is true and `superseded_by` is null.
