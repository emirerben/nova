# Initial request-following inventory — 2026-10-16

Base inspected: `552bdbee63409599478eed49557e932a2a96efd5`.
Linear context: KRI-215, Evals & AI quality, KRI-598 and its review comments.
No Linear records were changed.

The generated inventory found **36 incident records, 29 reliability gates and
60 ticket references in JSON fixtures**. Counts describe available references,
not passing end-to-end coverage.

A read-only production capture of the documented KRI-598 Tigre incident collected
29 events and 3 runtime turns (two completed, one failed), with all four core
endpoints and linked plan-item/job debug metadata available. The session is development material: it already appears in
regression fixtures and has been inspected. No holdout has been acquired or run.
The collaborator's identity was not inferred; broader account coverage remains
uncollected until explicit identities/session ids are supplied.

| Initial case | References found | Missing evidence |
| --- | --- | --- |
| KRI-590/593/594/598 cue placement, all-clips correction, photo beat | Seven related JSON fixtures; journeys `kri-590-voiceover-cues`, `kri-593-voiceover-clips`, `kri-598-voiceover-planner`; PR references 1547, 1549, 1550, 1556, 1568 | No matching incident-corpus record or reliability-ledger row; no inspected final-output artifact; complete manual-editor history unavailable |

The private proposal contains source pointers for initial and follow-up requests,
four proposed expectations, four authored variation proposals, and fixing PR
links. It remains **unreviewed**, with no claimed understanding, persistence or
render pass. The creator explicitly clarified the initial wording on 2026-10-16:
keep the title and English subtitles; exclude extra labels/editorial text. This
resolves intent, but is not approval of the entire regression case. Presence of saved-operation events is not persisted-output proof.

KRI-598's Linear review explicitly distinguishes hand-authored goldens from live
model checks. Existing tests must be inspected before describing the planner as
fixed. This implementation changes no prompts and makes no improvement claim.

Private captures, request excerpts, variants and full report live outside git.
Only this redacted inventory and tooling are proposed for review. The synthetic
acceptance journey proves that an approved case can enter and execute in the real
incident corpus; it does not substitute for human approval of the Tigre case.

Validation: 154 passed and 2 existing expected failures in the incident/CLI suites;
34 CI selector tests and 6 journey-policy tests passed. Focused acceptance tests
were rerun after adding linked debug capture. Ruff and preship checks passed.
No paid/provider calls, device runs, production writes or deployments occurred.
