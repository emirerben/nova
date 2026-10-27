# Native feature map

This map names eight fixture journeys and two missing native product flows.
Fixture checks, real-service checks and missing product flows are separate.

| Feature | Proof |
| --- | --- |
| [Creation chat](chat.md) | Attached footage reaches confirmation and ready. |
| [Media clarification](media.md) | A Visuals photo has a thumbnail and photo name. |
| [Native editor](editor.md) | Approving a caption changes the document and enables Save. |
| [Delivery](delivery.md) | Device-render progress reaches playback and delivery controls. |
| [Slides](slides.md) | Drawing survives Save and switching slides. |
| [Account](account.md) | Consent, account controls and deletion confirmation with a fixture account. |
| [Camera](camera.md) | Script and take recovery when capture or intake fails. |
| [Retention](retention.md) | A warned cloud copy remains reusable; saved posts stay visible. |
| [Billing](billing.md) | Missing native purchase flow. The harness records a gap and exits 3. |
| [Publication](publication.md) | Missing native platform-publish completion. The harness records a gap and exits 3. |

The runtime's test names are in `scripts/ios/ui-test-groups.json`. Keep this index
and its sibling files in sync. Do not substitute these fixture checks for an
original-request replay and inspected native export.
