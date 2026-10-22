# Saved original Visuals Add: named native coverage

CI run39757553970 on source62de3e98 was green, but diagnostics artifact12425465272 selected only four smoke UI tests. Neither newly declared SavedUploads UI test ran. The workflow now explicitly executes the device-only original Add and relink-then-Add tests on the compiled checkout, retains their xcresult/JSON, and uses the existing strict result verifier. Both exact names must pass; missing, skipped or failed cases reject the job. The smoke and named unit stages stay intact. New current-head CI is required before merge.

This verifies reconstructed UI fixtures and local API admission controls. It does not establish the original horse clip's physical Add/Save/relaunch/render journey (KRI-833), successful downstream Visuals readiness/render (KRI-835), or background-upload recovery. The source asset remains device_only while a separately verified original source can be available to Visuals; proxy bytes are never promoted.
