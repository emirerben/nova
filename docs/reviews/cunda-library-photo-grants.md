# Cunda library-original device grants (KRI-835)

Cunda's newly approved device-test render passed planning but stopped before
export. Five of its ten ready photos used their linked original-library objects.
The worker validated those owned, available originals at their exact generations.
The device-download route still required every visual to use the current
project's upload-folder prefix and returned409 for those five library objects.
The phone reported a generic changed-elsewhere conflict. Photos was never reached.

## Acceptance and repair

A photo or video accepted by `bind_phone_visual_assets` must remain downloadable
through the job owner's current device recipe. Library-linked rows use the exact
owned available original source, path, generation, SHA-256 and byte count.
Unlinked uploads retain the existing project-folder checks. A linked object still
needs its source checks when its path happens to use that folder.

The HTTP route now applies those source rules without changing the wire schema,
approval, source generation, asset identity or recipe. It re-reads the original
source before granting access. Foreign callers/items, unavailable originals,
analysis proxies and repointed sources remain refused.

## Evidence

The new PostgreSQL replay runs the real worker binder, hashes actual fixture bytes,
persists the resulting recipe, and calls the real authenticated HTTP route. Only
GCS transport and the test caller's authentication are substituted. Photos and
videos are covered, including library storage, the current upload folder and a
linked original in another folder. PostgreSQL keeps its immutable-source guards;
negative cases use new source versions and repointed project references.

Before the fix, the positive case failed with409 `Visual changed; refresh the
recipe`. After the fix,31 database-backed grant cases and the existing route and
binding neighbors pass:263 checks, zero skips. Scoped Ruff and formatting pass.

```bash
bash scripts/dev/test-db.sh
bash scripts/check-api.sh test tests/kria/test_library_visual_device_grants_postgres.py tests/routes/test_device_render.py tests/services/test_phone_visuals.py -q
```

The negative control keeps source shape/order valid but changes the referenced
original's role, availability, path, generation, hash or byte count. No signed
URL is returned. A foreign caller receives404. The source-binding contract is
independent of the grant implementation; it admitted the incident's originals
before this change.

## Remaining delivery evidence

The live failure and source checks were read-only; only counts and comparison flags
returned from production. Private media, paths and source identities are absent
from this report. Physical iPhone UI results and local red/green logs are retained
in the launch task's private evidence folders.

This is a source-authorization repair. Deployment, resuming the saved device
recipe, inspecting the finished video and a Photos write remain pending. The
original Cunda timing-preservation request also remains failed: the creator
explicitly authorized the later37.84-second draft only for device testing. It
cannot establish fulfillment of the original28.804-second unchanged edit.
No further planning submission is needed to resume the already pinned recipe.
