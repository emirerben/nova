/**
 * Conflict-tile Reload seeding policy (see conflictReseedRef in EditorShell).
 *
 * A normal (first / variant-switch) seed replaces EVERY section from the
 * fetched variant. A conflict reseed replaces ONLY the sections the user has
 * NOT touched — dirty sections keep the user's working edits while the
 * refetch refreshes everything another writer (AI auto-apply, other tab)
 * moved. Pure so the policy is unit-testable without mounting the shell.
 */

export interface EditorDirtyFlags {
  textDirty: boolean;
  sfxDirty: boolean;
  overlaysDirty: boolean;
  mixDirty: boolean;
}

export interface ReseedSections {
  text: boolean;
  sfx: boolean;
  overlays: boolean;
  mix: boolean;
  /** Title + applied-style chip only reset on a full (non-conflict) seed. */
  titleAndStyle: boolean;
}

export function computeReseedSections(
  dirty: EditorDirtyFlags,
  conflictReseed: boolean,
): ReseedSections {
  return {
    text: !conflictReseed || !dirty.textDirty,
    sfx: !conflictReseed || !dirty.sfxDirty,
    overlays: !conflictReseed || !dirty.overlaysDirty,
    mix: !conflictReseed || !dirty.mixDirty,
    titleAndStyle: !conflictReseed,
  };
}
