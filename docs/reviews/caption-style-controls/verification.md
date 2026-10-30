# Caption style local implementation

The patch is `caption-style-controls.patch`. The isolated checkout is
`/Users/emirerben/Documents/Codex/2026-10-29/task-3/nova`, branch
`codex/caption-style-controls`, based on `de24c7f59`. Prepared for draft publication.

## Result

- Added per-line and all-caption font-size and position controls using the existing caption metadata and TextElement paths.
- All captions replaces existing overrides for the changed property. Position-only changes preserve sizes, fonts and colors. Size-only changes preserve positions, fonts and colors. Both the inspector and post-gesture choice follow that rule.
- A drag or resize asks for scope once after release. The gesture and response use one undo transaction.
- Placement measures the displayed sentence and resolved renderer font/alignment, including individual cue fonts. Existing safe guides remain the source of placement limits. Oversized captions receive an explanation instead of an automatic size change.
- Scoped metadata survives Save and reopening. Missing legacy cue IDs become stable in the same transaction when required.
- Added protocol 25. Older clients cannot replace scoped caption state. The shared locked legacy captions PATCH also protects identities. Original unrelated phone-capability assertions are unchanged. New tests prove protocol 25 adds only this phone capability and leaves cloud maps unchanged.
- Preserved the existing per-cue font, color, size, outline and shadow fields through both compilers. These fields were previously stripped from the phone compilation copy. The native unset cue font now matches the shared TikTok Sans Regular registry default, instead of a hardcoded Bold face. Explicit saved font fields are unchanged.
- Added output-pixel labels and actual canvas dimensions to the size control.

## Zero effects policy

New phone generation already resolves unspecified outline to zero and shadow to
off in `phone_text_styling`. Explicit account choices still win. Existing authored
projects retain their appearance, including legacy omitted effect fields. No
account or project reset ran.

Forcing every existing project to zero would change authored output and cannot
distinguish a saved choice from an old omitted default. That destructive policy
is outside this implementation. Safe new-generation defaults remain zero.

Account `all_text` preferences contribute to the caption role's generation rule.
The generation code maps its position to caption placement. A bottom-left account
rule can therefore influence new captions. The exact reported lip-sync project
was not supplied or inspected, so its cause is not claimed as reproduced.

## Font size 150 diagnosis remains separate

The original tiny-caption incident is **not reproduced**. The sample matrix covers
Inter, TikTok Sans, Fraunces and Pacifico at 36, 78, 150 and 160 output pixels on
portrait, landscape and square canvases. No fitting algorithm or old authored
font size was changed.

For the sample `Hi`, every requested 150-pixel size compiles at 150. Its visible
ink height is 120 to 144 output pixels, depending on the font. Font size measures
the em, not the visible height of every letter.

For a 1080-pixel output canvas displayed at width W UIKit points, a 150-pixel em
occupies `150 * W / 1080` points. At 390 points wide, that is 54.17 points. At a
100-point preview width, it is 13.89 points. Multiply by the screen scale to
obtain physical screen pixels. Landscape and square canvas tests use their own
output widths. This explains scaling units; it does not diagnose the missing
original project.

Needed to diagnose that exact incident: the project name or link, and a screenshot
showing the 150 value with its preview. No original media is needed for intake.

## Final evidence

- `backend-reviewed-tests.log`: **680 passed** across the caption compiler/default matrix, editor commit, phone caption move, legacy plan-item PATCH, phone subtitle and song Save suites.
- `native-final-sequential.log`: **42 passed**, TEST SUCCEEDED, on task-owned iPhone 17 Pro simulator `DF626BD9-5AD3-482F-BE0C-41264B96BCEC`. This includes actual synthetic exports, Save/reopen, undo, apply-all preservation, sentence placement, protocol fixtures and neighboring text placement.
- `ui-final-sequential.log`: **1 UI journey passed**, TEST SUCCEEDED. It taps individual/all scope controls, changes size, positions the caption, drags it and chooses all captions after release.
- Three final screenshots were exported and inspected. `caption-style-line-size-position.png`, `caption-style-all-size-position.png`, and `caption-gesture-scope-choice.png` show readable controls, visible preview changes and the two scope choices.
- `caption-export/caption-scopes.mp4` uses a generated flat-gray source with two separately styled captions. Preview and MP4 PNGs were inspected. The sampled mean pixel differences are 0.02153 and 0.01512 on a 0–255 scale. Colored ink centers are x=0.2991 and x=0.7024 against requested 0.30 and 0.70. `comparison.json` records measurements. The source contains no user media.
- The existing KRI-1032 synthetic montage export journey also passed, protecting unrelated framing/audio/save behavior.
- Ruff check, Ruff format check and `git diff --check` passed.
- The independent reviewer reported no remaining proven material finding after the accepted fixes. See `independent-review.md`.

Earlier native attempts include a disk-capacity failure, Swift type-check errors,
a deliberately red scope run, and an interrupted overlapping UI wait. These
remain in their logs. They are not counted as passes. Final native and UI runs
were sequential. Only task-generated build/index caches were removed.

## Ownership and remaining proof

PRs 1866, 1909 and 1916 were merged before this checkout. Yasin's KRI-632 fitting
work and KRI-1010 crop worktrees were untouched. No fitting/crop patch was duplicated.

Before draft publication, no push, PR approval, merge, deployment, production flag/data change, user-media
mutation or external paid model eval ran. No physical phone was touched.

The local implementation and simulator evidence are complete for the checked
cases. Remaining evidence is the original 150-size project, physical-phone
behavior, every-font/every-string coverage and production rollout. The tests do
not certify those cases. The inspector's advanced scope requires the new server
capability; older servers keep it closed. Legacy cue IDs that require mutation
still respect caption-line edit permissions.

## Poteto Mode and attention

Read `/Users/emirerben/.codex/skills/poteto-mode/SKILL.md`. Applied named scope data,
one state owner, verification milestones, a local decision trail, and independent
review. Model the Domain led to `CaptionStyleScope` and typed line placement.
The skill's configured Claude/Grok families are unavailable. The repository's
explicit debugger-role fallback selected available `gpt-6-astra` for review.

Reviewed by gpt-6-astra.

No remaining proven material defect in the bounded review. Physical-device and
original-project proof remain open. The patch has not been released.

## Publication reconciliation

Rebased without conflicts onto main ad47b637e. Its four intervening commits do not alter caption source or fixtures. The runs above precede this rebase and are not claimed as rerun on the rebased SHA. Draft CI supplies fresh revision-bound checks. KRI-976 is the existing caption-positioning parent; original size-150 reproduction and physical-phone proof remain open.
