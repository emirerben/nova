const EDITOR_RETURN_PARAM_NAMES = [
  "editor_saved",
  "editor_variant",
  "editor_generation",
  "editor_prior_finished_at",
  "editor_render",
] as const;

type EditorReturnParamName = (typeof EDITOR_RETURN_PARAM_NAMES)[number];
type SearchParamReader = Pick<URLSearchParams, "get">;

export interface EditorReturnSignal {
  variantId: string;
  generation: string;
  priorFinishedAt: string | null;
  renderStarted: boolean;
  key: string;
}

export interface EditorReturnHrefInput {
  variantId: string;
  generation: string;
  priorFinishedAt: string | null;
  renderStarted: boolean;
}

export interface EditorCommitSectionsLike {
  text_elements?: boolean;
  caption_cues?: boolean;
  timeline?: boolean;
  mix?: boolean;
  sound_effects?: boolean;
  media_overlays?: boolean;
  title?: boolean;
}

export function editorCommitStartedRender(sections: EditorCommitSectionsLike): boolean {
  return Boolean(
    sections.text_elements ||
      sections.caption_cues ||
      sections.timeline ||
      sections.mix ||
      sections.sound_effects ||
      sections.media_overlays,
  );
}

export function buildPlanItemEditorReturnHref(
  itemId: string,
  input: EditorReturnHrefInput,
): string {
  const params = new URLSearchParams();
  params.set("editor_saved", "1");
  params.set("editor_variant", input.variantId);
  params.set("editor_generation", input.generation);
  params.set("editor_render", input.renderStarted ? "1" : "0");
  if (input.priorFinishedAt !== null) {
    params.set("editor_prior_finished_at", input.priorFinishedAt);
  }
  return `/plan/items/${encodeURIComponent(itemId)}?${params.toString()}`;
}

export function parsePlanItemEditorReturnSignal(
  params: SearchParamReader,
): EditorReturnSignal | null {
  if (params.get("editor_saved") !== "1") return null;

  const variantId = params.get("editor_variant");
  const generation = params.get("editor_generation");
  if (!variantId || generation === null) return null;

  const priorFinishedAt = params.get("editor_prior_finished_at");
  const renderStarted = params.get("editor_render") === "1";
  return {
    variantId,
    generation,
    priorFinishedAt: priorFinishedAt || null,
    renderStarted,
    key: [variantId, generation, priorFinishedAt ?? "", renderStarted ? "1" : "0"].join(":"),
  };
}

export function stripPlanItemEditorReturnParams(search: string): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  for (const name of EDITOR_RETURN_PARAM_NAMES satisfies readonly EditorReturnParamName[]) {
    params.delete(name);
  }
  const next = params.toString();
  return next ? `?${next}` : "";
}
