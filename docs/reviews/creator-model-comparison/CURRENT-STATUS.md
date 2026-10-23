# Current decision and delivery status — 21 October 2026

The development pilot is inconclusive for provider selection. Keep Gemini as the production default. Stop low-materiality preference votes; prioritize request fulfillment, usable output, whole-video quality and first-attempt reliability. No provider rollout has occurred. The focused title/placeholder product fix is deployed independently of this experiment.

## Product delivery

The demonstrated title/placeholder fixes have been extracted onto current main in [the focused product PR](https://github.com/emirerben/nova/pull/1706), **fix(ai): honor word-by-word titles and preserve placeholders**. It has no dependency on merging the broad comparison infrastructure.

The fix prevents an entrance-effect match from skipping requested word segmentation, preserves protected placeholder copy, orders styles before splitting invalidates selectors, targets universal placeholders without requiring actual location recognition, and keeps an opening hook out of the chapter-caption lane. Current-main copy-move handling is preserved. Local replay, browser timeline, native proxy export and preship checks pass. All PR CI checks, including the captured creation/export journey on the iPhone simulator, passed.

PR #1706 was squash-merged on 21 October at 13:46 UTC as `7036e6c7e85c776976cc1895a1a930e2174abaae`. The user explicitly approved administrator merge after GitHub's review policy blocked self-approval by the sole code owner, who was also the PR author; repository rules were not changed. [Fly Deploy succeeded](https://github.com/emirerben/nova/actions/runs/39610907857) at 13:49 UTC. All six managed machines expose the same immutable image and exact merge revision. Read-only file fingerprints match the approved fix on the live API and active task workers. The general render worker is intentionally idle-stopped with that same image. The production health page returned 200 with no browser console errors in 0.564 seconds. These are deployment/code/health checks, not a fresh real-request quality verdict. A physical-phone resend and final export remain unverified; the paired phone was disconnected. New compositions use the fix; saved frozen compositions are unchanged.

The provider-comparison PR remains a draft and has main conflicts. It is infrastructure/experimental work, not an approved provider-quality improvement or a production release.

## Human review now recorded

- Main Creator hook-tone case: one blinded hook wording preference recorded. It is evidence for that development case only, not a complete-edit or provider verdict.
- Constrained travel montage: excluded from preference ranking because explicit source order/copy plus shared recovery made outputs converge. Original failures remain retained.
- Copilot alignment: creator confirmed shared left edge, stacked vertical spacing and unchanged separate location label. Two usable compiled proposals match those position obligations; the third returned no accepted edit.
- Copilot opener: creator confirmed each word should disappear before the next. Native transition frames show one proposal overlaps words and another does not. The creator judged the visual difference minimal and low importance; no preference is assigned and the timing requirement remains.
- Compound labels: creator confirmed the global fade-in instruction applies to all text, including location labels. Keeping static labels misses it. One usable proposal adds the fades; another does not. This is an instruction-following distinction, not a complete-edit quality score.

## Render evidence and limits

There are local native proxy videos for the three recovered Main montage outputs, the two usable opener edits and the two usable compound-label edits. Before-edit videos and sampled transition frames accompany the Copilot cases. Three hook-copy visualizations use the exact proposed lines in a fixed layout; those models asked for wording approval and did not author executable edits.

Copilot previews reconstruct recorded text/timeline lanes using the owned job and eligible analysis proxies. They use the app's text compiler and native engine, but do not exercise the full production save/render dispatch or physical-phone export. Main recovery artifacts retain original failures and shared finishing steps separately. These evidence classes must not be combined into a first-attempt success rate.

Twenty exact recorded inputs are prepared across five development threads from one creator. Zero independent real holdouts are enrolled. Repeated sources, related turns, reconstructed execution bases and recovered outputs cannot establish population reliability or a production quality winner. Full Sonnet native grammar is still blocked, and the tested JSON guard did not eliminate format failures.

## Current paid envelope

- Approved total: **$5.66**.
- Confirmed metered usage: **$3.158952**.
- Original protected unknown liability: **$1.716306**.
- Accounted total: **$4.875258**.
- Remaining: **$0.784742**.
- No active submissions; the original unknown is neither retried nor released.
- Review previews and the product-fix extraction added **zero** model calls.

The prior morning report and final-development-results JSON remain historical phase snapshots. Their older counts, pending human-review statements, no-export claims and lower accounting totals are superseded by this document. Original journals, returned bytes and accounting history are unchanged. Raw source bodies, media, per-case feedback and blind provider mappings remain outside Git under existing expiry/revocation controls.

## Remaining experiment work

No automatic provider switch is justified. A meaningful next comparison needs independently eligible creators/media and materially different end-to-end requests, assessed on actual saved/exported results and first-attempt usability. Register that cohort and price it before another paid phase. The current pilot does not supply those holdouts or fund a larger unpriced evaluation.
