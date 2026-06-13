/**
 * Shared variant-editor types — the surface both the generative page and the
 * plan flow lean on for the 0-latency instant text/style editor.
 *
 * `EditableVariant` is the structural subset of `GenerativeVariant` that the
 * shared hook + preview components actually read. Both `GenerativeVariant`
 * (lib/generative-api.ts) and `PlanItemVariant` (lib/plan-api.ts) are
 * assignable to it, so the shared machinery never imports a surface-specific
 * type. Field names/types mirror `GenerativeVariant` exactly — keep them in
 * lockstep (adding a field the hook reads means widening this interface, not
 * narrowing the call site).
 */

export interface EditableVariant {
  variant_id: string;
  /** Persisted intro hook text. null/absent on non-text variants. */
  intro_text?: string | null;
  /** Agent-decided (or user-pinned) intro size. null for non-text variants. */
  intro_text_size_px: number | null;
  /** Selected text style set. */
  style_set_id: string | null;
  /** User-pinned font override (independent of style_set_id). */
  intro_font_family?: string | null;
  /** User-pinned animation/effect override. */
  intro_effect?: string | null;
  /** User-pinned text color override. */
  intro_text_color?: string | null;
  /** Editorial cluster: hero-word font override. */
  intro_cluster_hero_font?: string | null;
  /** Editorial cluster: body/connector font override. */
  intro_cluster_body_font?: string | null;
  /** Effective intro layout — "cluster" intros are not locally preview-able. */
  intro_layout?: "linear" | "cluster" | null;
  /** Intro rendering mode. "sequence" → text is server-locked (transcript/rhythm sync). */
  intro_mode?: "sequence" | "cluster" | "linear" | null;
  /** Fresh-signed playback URL of the text-free fast-reburn base. */
  base_video_url?: string | null;
  base_video_path?: string | null;
  /** "none" means the variant's text was removed. */
  text_mode: "lyrics" | "agent_text" | "none";
  render_status: "ready" | "rendering" | "failed" | null;
  /** Render-completion fingerprint — the commit watcher keys off this. */
  render_finished_at?: string | null;
  output_url: string | null;
}
