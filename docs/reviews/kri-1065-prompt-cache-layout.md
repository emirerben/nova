# KRI-1065 prompt-cache layout (2026-10-30)

Offline and live-model evidence for moving per-turn data to the end of the `edit_copilot` and `main_creator` prompts. Scripts sit in `docs/reviews/kri-1065/`; run from `src/apps/api` with `PYTHONPATH=.:tests`.

## Same content, new order (offline)
`prove_same_content.py` re-substitutes the origin/main templates with the new code's inputs and compares line multisets.
- main_creator: nothing removed. Added: two orientation lines, `INPUT (this turn)`, one closing line.
- edit_copilot: nothing removed except two reworded lines (the "Assistant entries ..." pointer and the "up to $max_ops ops" bullet). Added: trust-boundary pointer, `## Turn block` header and sentence, `MAX OPS: 48`.

## Live model, gemini-3.1-pro-preview (approved, about $0.40)
`tokens_cached` as reported by the provider, four different user turns 10 s apart:

| Layout | Agent | Cached tokens per call (input about 14k) |
| --- | --- | --- |
| Old | edit_copilot | 0, 0, 0, 0 |
| New | edit_copilot | 0, 0, 0, 12087 |
| New | main_creator | 12825, 0, 0, 0 |

Control, identical prompt three times: gemini-3.1-pro-preview 0, 12075, 12075; gemini-2.5-flash 0, 13291, 13291.

Reading: implicit caching is best-effort, so hits are uneven at this traffic. Every hit covered about 85% of the prompt, which matches the size of the shared head, and the old layout never hit. Production hit rate and savings are unmeasured until after deploy.
