/**
 * Tests for _editor/editor-reseed.ts — the conflict-tile Reload seeding policy
 * (extracted from EditorShell's seeding effect so the branch is unit-testable
 * without mounting the full shell).
 *
 * The scenario pinned here: the user dirties ONLY overlays, another writer
 * (AI auto-apply / other tab) moves the variant, Save 409s, the user hits
 * Reload. The refetched variant must re-seed text/sfx/mix (picking up the
 * other writer's changes) while the dirty overlays keep the user's edits.
 */

import { computeReseedSections } from "@/app/plan/items/[id]/_editor/editor-reseed";

const CLEAN = {
  textDirty: false,
  sfxDirty: false,
  overlaysDirty: false,
  mixDirty: false,
};

describe("computeReseedSections", () => {
  it("seeds every section on a normal (non-conflict) seed, dirty or not", () => {
    expect(
      computeReseedSections(
        { textDirty: true, sfxDirty: true, overlaysDirty: true, mixDirty: true },
        false,
      ),
    ).toEqual({ text: true, sfx: true, overlays: true, mix: true, titleAndStyle: true });
  });

  it("conflict reseed with dirty overlays only: overlays kept, the rest re-seeded", () => {
    expect(
      computeReseedSections({ ...CLEAN, overlaysDirty: true }, true),
    ).toEqual({
      text: true, // non-dirty → replaced by the refetched server state
      sfx: true,
      mix: true,
      overlays: false, // dirty → the user's working edits survive
      titleAndStyle: false, // never reset by a conflict reseed
    });
  });

  it("conflict reseed keeps every dirty section and refreshes every clean one", () => {
    expect(
      computeReseedSections(
        { textDirty: true, sfxDirty: false, overlaysDirty: false, mixDirty: true },
        true,
      ),
    ).toEqual({ text: false, sfx: true, overlays: true, mix: false, titleAndStyle: false });
  });

  it("conflict reseed with nothing dirty re-seeds all sections (except title/style)", () => {
    expect(computeReseedSections(CLEAN, true)).toEqual({
      text: true,
      sfx: true,
      overlays: true,
      mix: true,
      titleAndStyle: false,
    });
  });
});
