// Lives outside page.tsx because Next's generated page-type check forbids
// extra named exports from a page module.

export function isCoarsePointerDevice(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: coarse)").matches
  );
}

export function shouldAutoOpenPlanItemEditor({
  editorEnabled,
  itemReady,
  hasEditorReturnSignal,
  readyVariantCount,
  canOpenVariant,
  isCoarsePointer,
}: {
  editorEnabled: boolean;
  itemReady: boolean;
  hasEditorReturnSignal: boolean;
  readyVariantCount: number;
  canOpenVariant: boolean;
  isCoarsePointer: boolean;
}): boolean {
  return (
    editorEnabled &&
    itemReady &&
    !hasEditorReturnSignal &&
    readyVariantCount === 1 &&
    canOpenVariant &&
    !isCoarsePointer
  );
}
