const EDITOR_RETURN_PARAM_NAMES = [
  "editor_saved",
  "editor_variant",
  "editor_generation",
  "editor_prior_finished_at",
  "editor_render",
  "editor_expected_duration",
  "editor_revision_hash",
] as const;

type EditorReturnParamName = (typeof EDITOR_RETURN_PARAM_NAMES)[number];
type SearchParamReader = Pick<URLSearchParams, "get">;

export interface EditorReturnSignal {
  variantId: string;
  generation: string;
  priorFinishedAt: string | null;
  renderStarted: boolean;
  expectedDurationS?: number | null;
  revisionHash?: string | null;
  key: string;
}

export interface EditorReturnHrefInput {
  variantId: string;
  generation: string;
  priorFinishedAt: string | null;
  renderStarted: boolean;
  expectedDurationS?: number | null;
  revisionHash?: string | null;
}

export interface EditorCommitSectionsLike {
  text_elements?: boolean;
  caption_cues?: boolean;
  caption_meta?: boolean;
  timeline?: boolean;
  mix?: boolean;
  music?: boolean;
  background_music?: boolean;
  sound_effects?: boolean;
  media_overlays?: boolean;
  visual_blocks?: boolean;
  motion_scenes?: boolean;
  camera_effects?: boolean;
  title?: boolean;
  lyrics?: boolean;
  orientation?: boolean;
  carousel_moment?: boolean;
}

export function editorCommitStartedRender(sections: EditorCommitSectionsLike): boolean {
  return Boolean(
    sections.text_elements ||
      sections.caption_cues ||
      sections.caption_meta ||
      sections.timeline ||
      sections.mix ||
      sections.music ||
      sections.background_music ||
      sections.sound_effects ||
      sections.media_overlays ||
      sections.visual_blocks ||
      sections.motion_scenes ||
      sections.camera_effects ||
      sections.lyrics ||
      sections.orientation ||
      sections.carousel_moment,
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
  if (typeof input.expectedDurationS === "number" && Number.isFinite(input.expectedDurationS)) {
    params.set("editor_expected_duration", input.expectedDurationS.toFixed(3));
  }
  if (input.revisionHash) params.set("editor_revision_hash", input.revisionHash);
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
  const expectedRaw = params.get("editor_expected_duration");
  const revisionHash = params.get("editor_revision_hash");
  const expectedParsed = expectedRaw === null ? Number.NaN : Number(expectedRaw);
  const expectedDurationS = expectedRaw !== null && Number.isFinite(expectedParsed) && expectedParsed >= 0 ? expectedParsed : null;
  return {
    variantId,
    generation,
    priorFinishedAt: priorFinishedAt || null,
    renderStarted,
    ...(expectedDurationS === null ? {} : { expectedDurationS }),
    ...(revisionHash ? { revisionHash } : {}),
    key: [variantId, generation, priorFinishedAt ?? "", renderStarted ? "1" : "0", ...(expectedDurationS === null ? [] : [expectedDurationS]), ...(revisionHash ? [revisionHash] : [])].join(":"),
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
