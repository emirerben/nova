# KRI-769 suggestion-chip integration

Original source: `adf95feb8865e1bc8db2f1e6ee51d8df07a5f55a` (#1736).
Fresh fetched main: `36031e054c1ffd2d6a3fdf3f5e492a4979154124`.
The original native scroll/reveal and empty-prompt changes are unchanged.
No unmerged dependency is imported. This integration also repairs bounded
truthfulness errors discovered in the new deterministic evidence subtitles.

## Failure controls written before the copy fix

Speech-presence flags do not identify the creator. A first-prompt upload is not
an already assembled edit. A music track plus speech metadata does not prove
perceptual masking, and default creation must not promise an added music track.
Do not change chip actions, ranks, selection, flags or model-authored subtitles.

Ten static EN/TR expectations run the real suggestion builder, independent of
its evidence formatter and duplicated hygiene oracle. Third-party speech with
no on-camera creator exercises partial/all-speech counts. First-prompt and
ready fixtures exercise output state and music/speech presence.

Before the relevant copy edits:
- `bash scripts/check-api.sh test tests/evals/test_suggestion_chips_evals.py -k background_speech_evidence -q --tb=short`: **4 failed**, all on the original inferred speaker-identity claim.
- `bash scripts/check-api.sh test tests/evals/test_suggestion_chips_evals.py -k evidence_stays_with_available_state -q --tb=short`: **6 failed**, all on the original completed-edit/music claims.

The fallback copy now says Speech in N clips, N clips ready to combine before
creation, Speech clips and a music track, and Kria picks the cuts and pace.
Completed-edit hard-cut counts retain their existing wording. Existing contract
examples and the hygiene oracle follow the deliberate copy change; the ten
independent static expectations remain separate. Model-written grounded why,
chip labels/messages, IDs, anchors, action execution and UI scrolling are unchanged.
No model prompt changed and no prompt-version bump is needed.

## Executed verification

`bash scripts/check-api.sh test tests/evals/test_suggestion_chips_evals.py tests/evals/test_chip_suggester_evals.py tests/services/test_suggestion_chips_guards.py -q --tb=short`: **909 passed, 7 expected failures**. Known gaps remain explicit.
Scoped Ruff checks/format and whitespace checks passed.

The isolated preship database is `nova_pr_1736_integration_test`.
Committed preship and current-head hosted native UI verification are still
required before merge. The original source includes the after-render visibility
assertion; no new screenshot/device result is claimed by this refresh. Broader
KRI-769 live-quality, attribution and physical-device work remain open.
No paid call, phone operation, rollout setting or manual release was performed.
