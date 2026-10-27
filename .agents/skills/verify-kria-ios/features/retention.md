# Retention warning and reuse

Source under `src/apps/ios/`: `Kria/Core/MediaLibrary.swift`,
`Kria/Features/SavedUploadsView.swift`, `Kria/Features/SavedMediaGrid.swift`,
`Kria/Features/SavedUploadsUITestFixture.swift` and
`Tests/KriaUITests/SavedUploadsUITests.swift`.

Run `drive retention`. Open Saved uploads. Read the tile's accessible cloud-expiry
warning. Add the warned upload to one post, then reuse it in another. Both posts remain visible,
and the fixture rejects another upload request.

The transport supplies the real `storage_warnings` shape. This checks native
presentation and reuse, not the server's 83/90/365-day policy or a destructive
sweep. Use `docs/runbooks/gcp-cost-controls.md` for protected-reference checks.
KRI-331 and KRI-652 remain separate retention work. No production media is deleted.
