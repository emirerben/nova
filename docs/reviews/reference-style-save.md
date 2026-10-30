# Reference-style edits: durable Save and truthful feedback

KRI-1060. A creator manually styled one text block, asked Kria to match the other
blocks, then tried Save before and after Undo. The editor showed the requested
change, but the reply said it could not do it, and Save rejected the document.

## Causes and change

Save converts a silent guided project into a creator-owned authored timeline.
That changes caption eligibility. The native document carried caption
presentation defaults even though it contained ordinary text and no caption
consumers. The new baseline rejected those settings with the caption-tab error.

The authored device boundary now accepts and persists validated presentation
metadata only when server-owned state proves there are no existing or proposed
caption consumers, speech timing, narration or pinned voiceover. Actual caption
cue writes keep their guard. Font, geometry, protocol, source and generation
validation remain in force. This preserves metadata and acknowledges it; it does
not strip user changes to force a successful Save. Both supported Save wire
versions are covered. The existing caption reader-floor guard also runs at the
authored entry point, before mutation.

Separately, receipt binding treated the quoted style reference as the destination
despite a global quantity. When that scope lacks typed evidence, the reply now
reports the actual applied changes as unchecked, rather than falsely declaring
failure or success. Concrete wrong targets/values and explicit declines retain
their outcomes. Native leads count only checked, met requests as completed.

## Verification

- Before the change, four valid Save cases failed with the exact caption error;
  eight rejection controls passed. The expanded matrix covers rollout on/off,
  both Save wire versions, repeated Save, stale retry, invalid fonts, real caption
  consumers, incoming caption text, narration/speech and unsupported destinations.
- 572 focused Save, caption, source, speed and receipt cases passed. This includes
  the protocol-24 refusal/protocol-25 acceptance control and EN/TR reply leads.
- Two disposable-PostgreSQL journeys passed: manual reference style, fixture
  planner answer, actual runtime/receipts, Save transaction and durable reopen,
  with and without local Undo. The live provider is replaced; source generation
  validation runs against a path-scoped fake object store.
- Two retained 27-word payloads compile locally with exact editable identities,
  copy, timings, fonts and sizes. They use immutable copies and the read-only
  qualified-capability list. No original project or media was changed.
- Thirteen simulator tests passed: one test renders both API-produced saved
  recipes, and twelve existing Save/Undo/dirty-state/conflict controls. All twelve
  preview/export OCR samples matched. Each synthetic export is 1080x1920,
  30fps, 90 frames and 3.000 seconds.

Native input is the production snake-case recipe decoded by
`RecipeJSON.decoder()`. The harness substitutes only synthetic video bytes and
their matching fingerprints, retaining API text and bundled-font fingerprints.
The final API recipes match the native-tested canonical device recipe digests:

| Case | Digest |
| --- | --- |
| Styled all | `de809ce041472fd9d44ead7a7bbf5ddcd21962e53bea9e5bfd27abe87fb49ec8` |
| Local Undo | `674c5d72a99a5eadfd5a4f3930051b7654fecf894090e9bd3ce9908d7ed72abd` |

Raw JSON differs only in serialization order of the schema-declared
`required_capabilities` set; every other field compares exactly. This is semantic
recipe identity, not a byte-identical JSON claim.

Run the three API files registered in journey `reference-style-save`, then:

```sh
xcodebuild test -project Kria.xcodeproj -scheme Kria \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -only-testing:KriaTests/ReferenceStyleSaveTests CODE_SIGNING_ALLOWED=NO
```

The API journey can emit synthetic fixtures with
`KRIA_REFERENCE_STYLE_FIXTURE_DIR=/tmp/reference-style-save`.

## Limits

No live model replay, physical-phone acceptance, historical response rewrite,
production project repair, flag activation or new feature rollout is claimed.
The user's successful text-timing test is separate evidence. Hosted checks and
release verification must complete for this repair. Unchecked scope is reported
honestly; this does not establish that every possible reference-style request is
understood. The original opening failure's historical cause remains unproven;
PR1946 is separate dormant reader groundwork.
