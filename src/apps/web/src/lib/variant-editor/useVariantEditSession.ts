"use client";

/**
 * Instant-edit session state for one editable variant.
 *
 * The Instagram-style editor: every text/style/size change lands in a local
 * `draft` (0 network), previewed live over the base video. "Done" commits the
 * WHOLE session as ONE `POST .../edit` → one re-render, instead of the legacy
 * one-render-per-field endpoints.
 *
 * Surface-agnostic: the hook takes `(variant, onCommit)` and depends only on the
 * shared `EditableVariant` shape, so the generative page and the plan flow share
 * one copy. `onCommit` is the only network call (the caller wires the right
 * endpoint).
 *
 * Designed to live in a component that stays mounted across status polls
 * (VariantTile keys by variant_id), so:
 * - polls never clobber the draft (server values are only adopted on enterEdit)
 * - a commit's render_status flip to "rendering" keeps the preview on screen
 *   ("Saving…") until the fresh output arrives
 * - edits made while a commit renders coalesce into ONE follow-up commit,
 *   fired when the in-flight render completes (also absorbs 409s).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EditVariantPayload } from "@/lib/generative-api";
import type { EditableVariant } from "@/lib/variant-editor/types";

export interface EditDraft {
  text: string;
  removed: boolean;
  styleSetId: string | null;
  sizePx: number | null;
  /** Intro layout pick — "linear" classic block vs "cluster" editorial word-
   * cluster. Additive: the generative flow never calls setLayout, so its draft
   * keeps this at the seeded value and buildEditPayload never emits intro_layout
   * for it (no diff). The plan deferred-burn flow drives it via the Layout pill. */
  layout: "linear" | "cluster" | null;
  /** User-pinned font override — null = inherit from resolved style set. */
  fontFamily: string | null;
  /** User-pinned animation override — null = inherit from resolved style set. */
  animation: string | null;
  /** User-pinned text color override — null = inherit from resolved style set. */
  textColor: string | null;
  /** Cluster editorial: hero-word font override. */
  clusterHeroFont: string | null;
  /** Cluster editorial: body/connector font override. */
  clusterBodyFont: string | null;
  /** Cluster editorial: accent/closer font override. */
  clusterAccentFont: string | null;
  /** Cluster editorial: per-role size overrides (absolute px). */
  clusterHeroSizePx: number | null;
  clusterBodySizePx: number | null;
  clusterAccentSizePx: number | null;
  /** Occlude the AI-intro overlay behind the moving subject. Unlike the
   * font/animation/color overrides, this has no "inherit" state — always a
   * concrete boolean. */
  behindSubject: boolean;
}

export interface VariantEditSession {
  /** Edit mode is open (toolbar + preview visible). */
  isEditing: boolean;
  /** A commit is in flight or its render is still running — show "Saving…". */
  isSaving: boolean;
  /** Just settled a text-only commit — drives a brief "Saved" pulse. */
  justSaved: boolean;
  /** Editor or saving preview should be on screen (drives VariantTile layout). */
  isActive: boolean;
  draft: EditDraft;
  isDirty: boolean;
  /** Last commit failure, shown in the toolbar; cleared on the next commit. */
  commitError: string | null;
  enterEdit: () => void;
  cancel: () => void;
  setText: (text: string) => void;
  setRemoved: (removed: boolean) => void;
  setStyle: (styleSetId: string) => void;
  setSize: (sizePx: number) => void;
  setLayout: (layout: "linear" | "cluster") => void;
  setFont: (fontFamily: string) => void;
  setAnimation: (animation: string) => void;
  setColor: (textColor: string) => void;
  setClusterHeroFont: (fontFamily: string) => void;
  setClusterBodyFont: (fontFamily: string) => void;
  setClusterAccentFont: (fontFamily: string) => void;
  setClusterHeroSizePx: (px: number) => void;
  setClusterBodySizePx: (px: number) => void;
  setClusterAccentSizePx: (px: number) => void;
  setBehindSubject: (behindSubject: boolean) => void;
  /** Increments each time the entrance animation should replay in the preview. */
  playToken: number;
  /** Replay the entrance animation in the preview now. */
  replay: () => void;
  commit: () => Promise<void>;
}

function draftFromVariant(variant: EditableVariant): EditDraft {
  return {
    text: variant.intro_text ?? "",
    removed: variant.text_mode === "none",
    styleSetId: variant.style_set_id ?? null,
    sizePx: variant.intro_text_size_px ?? null,
    layout: variant.intro_layout ?? null,
    fontFamily: variant.intro_font_family ?? null,
    animation: variant.intro_effect ?? null,
    textColor: variant.intro_text_color ?? null,
    clusterHeroFont: variant.intro_cluster_hero_font ?? null,
    clusterBodyFont: variant.intro_cluster_body_font ?? null,
    clusterAccentFont: variant.intro_cluster_accent_font ?? null,
    clusterHeroSizePx: variant.intro_cluster_hero_size_px ?? null,
    clusterBodySizePx: variant.intro_cluster_body_size_px ?? null,
    clusterAccentSizePx: variant.intro_cluster_accent_size_px ?? null,
    behindSubject: variant.intro_behind_subject ?? false,
  };
}

function draftsEqual(a: EditDraft, b: EditDraft): boolean {
  return (
    a.text.trim() === b.text.trim() &&
    a.removed === b.removed &&
    a.styleSetId === b.styleSetId &&
    a.sizePx === b.sizePx &&
    a.layout === b.layout &&
    a.fontFamily === b.fontFamily &&
    a.animation === b.animation &&
    a.textColor === b.textColor &&
    a.clusterHeroFont === b.clusterHeroFont &&
    a.clusterBodyFont === b.clusterBodyFont &&
    a.clusterAccentFont === b.clusterAccentFont &&
    a.clusterHeroSizePx === b.clusterHeroSizePx &&
    a.clusterBodySizePx === b.clusterBodySizePx &&
    a.clusterAccentSizePx === b.clusterAccentSizePx &&
    a.behindSubject === b.behindSubject
  );
}

/** Minimal payload: only fields that differ from the baseline go to the server. */
export function buildEditPayload(draft: EditDraft, baseline: EditDraft): EditVariantPayload {
  const payload: EditVariantPayload = {};
  // Deleting every character means "remove the text" — without this, an empty
  // draft would produce no `text` field, the payload would be empty, and Done
  // would silently bring the old burned text back.
  const removed = draft.removed || !draft.text.trim();
  if (removed) {
    if (!baseline.removed) payload.remove_text = true;
  } else {
    const text = draft.text.trim();
    if (text && (baseline.removed || text !== baseline.text.trim())) payload.text = text;
    if (draft.sizePx !== null && draft.sizePx !== baseline.sizePx) {
      payload.text_size_px = draft.sizePx;
    }
  }
  if (draft.styleSetId && draft.styleSetId !== baseline.styleSetId) {
    payload.style_set_id = draft.styleSetId;
  }
  // Layout pick rides the same batched /edit. Only emit when it actually moved
  // from the baseline (so the generative draft — which never calls setLayout —
  // contributes nothing here).
  if (
    (draft.layout === "linear" || draft.layout === "cluster") &&
    draft.layout !== baseline.layout
  ) {
    payload.intro_layout = draft.layout;
  }
  if (draft.fontFamily !== null && draft.fontFamily !== baseline.fontFamily) {
    payload.font_family = draft.fontFamily;
  }
  if (draft.animation !== null && draft.animation !== baseline.animation) {
    payload.effect = draft.animation;
  }
  if (draft.textColor !== null && draft.textColor !== baseline.textColor) {
    payload.text_color = draft.textColor;
  }
  if (draft.clusterHeroFont !== null && draft.clusterHeroFont !== baseline.clusterHeroFont) {
    payload.cluster_hero_font = draft.clusterHeroFont;
  }
  if (draft.clusterBodyFont !== null && draft.clusterBodyFont !== baseline.clusterBodyFont) {
    payload.cluster_body_font = draft.clusterBodyFont;
  }
  if (draft.clusterAccentFont !== null && draft.clusterAccentFont !== baseline.clusterAccentFont) {
    payload.cluster_accent_font = draft.clusterAccentFont;
  }
  if (draft.clusterHeroSizePx !== null && draft.clusterHeroSizePx !== baseline.clusterHeroSizePx) {
    payload.cluster_hero_size_px = draft.clusterHeroSizePx;
  }
  if (draft.clusterBodySizePx !== null && draft.clusterBodySizePx !== baseline.clusterBodySizePx) {
    payload.cluster_body_size_px = draft.clusterBodySizePx;
  }
  if (draft.clusterAccentSizePx !== null && draft.clusterAccentSizePx !== baseline.clusterAccentSizePx) {
    payload.cluster_accent_size_px = draft.clusterAccentSizePx;
  }
  if (draft.behindSubject !== baseline.behindSubject) {
    payload.text_behind_subject = draft.behindSubject;
  }
  return payload;
}

export function useVariantEditSession(
  variant: EditableVariant,
  onCommit: (payload: EditVariantPayload) => Promise<void>,
): VariantEditSession {
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState<EditDraft>(() => draftFromVariant(variant));
  const [baseline, setBaseline] = useState<EditDraft>(() => draftFromVariant(variant));
  const [committing, setCommitting] = useState(false);
  const [awaitingRender, setAwaitingRender] = useState(false);
  const [pendingDraft, setPendingDraft] = useState<EditDraft | null>(null);
  const [sawRendering, setSawRendering] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  // Brief "Saved" affordance after a text-only commit settles — a quiet lime
  // pulse that recedes, never a blocking spinner. Auto-clears after a beat.
  const [justSaved, setJustSaved] = useState(false);
  const [playToken, setPlayToken] = useState(0);

  const onCommitRef = useRef(onCommit);
  onCommitRef.current = onCommit;
  // render_finished_at fingerprint at commit time: right after a commit the poll
  // can still report the PRE-commit "ready" state — settling on that would drop
  // the preview and flash "Rendering…". Only a ready whose fingerprint moved
  // (or that follows an observed "rendering") is the commit's own completion.
  const commitMarkerRef = useRef<string | null>(null);
  const variantFinishedAt = variant.render_finished_at ?? null;

  const isDirty = useMemo(() => !draftsEqual(draft, baseline), [draft, baseline]);
  const isSaving = committing || awaitingRender;
  const isActive = isEditing || isSaving;

  const enterEdit = useCallback(() => {
    const seed = draftFromVariant(variant);
    setBaseline(seed);
    setDraft(seed);
    setCommitError(null);
    setJustSaved(false);
    setIsEditing(true);
    setPlayToken((t) => t + 1); // auto-play on editor open
  }, [variant]);

  const cancel = useCallback(() => {
    setIsEditing(false);
    setDraft(baseline);
  }, [baseline]);

  const setText = useCallback(
    (text: string) => setDraft((d) => ({ ...d, text, removed: false })),
    [],
  );
  const setRemoved = useCallback(
    (removed: boolean) => setDraft((d) => ({ ...d, removed })),
    [],
  );
  const setStyle = useCallback(
    (styleSetId: string) => setDraft((d) => ({ ...d, styleSetId })),
    [],
  );
  const setSize = useCallback(
    (sizePx: number) => setDraft((d) => ({ ...d, sizePx, removed: false })),
    [],
  );
  const setLayout = useCallback(
    (layout: "linear" | "cluster") => setDraft((d) => ({ ...d, layout })),
    [],
  );
  const setFont = useCallback(
    (fontFamily: string) => {
      setDraft((d) => ({ ...d, fontFamily }));
      setPlayToken((t) => t + 1);
    },
    [],
  );
  const setAnimation = useCallback(
    (animation: string) => {
      setDraft((d) => ({ ...d, animation }));
      setPlayToken((t) => t + 1);
    },
    [],
  );
  const setColor = useCallback(
    (textColor: string) => setDraft((d) => ({ ...d, textColor })),
    [],
  );
  const setClusterHeroFont = useCallback(
    (clusterHeroFont: string) => setDraft((d) => ({ ...d, clusterHeroFont })),
    [],
  );
  const setClusterBodyFont = useCallback(
    (clusterBodyFont: string) => setDraft((d) => ({ ...d, clusterBodyFont })),
    [],
  );
  const setClusterAccentFont = useCallback(
    (clusterAccentFont: string) => setDraft((d) => ({ ...d, clusterAccentFont })),
    [],
  );
  const setClusterHeroSizePx = useCallback(
    (clusterHeroSizePx: number) => setDraft((d) => ({ ...d, clusterHeroSizePx })),
    [],
  );
  const setClusterBodySizePx = useCallback(
    (clusterBodySizePx: number) => setDraft((d) => ({ ...d, clusterBodySizePx })),
    [],
  );
  const setClusterAccentSizePx = useCallback(
    (clusterAccentSizePx: number) => setDraft((d) => ({ ...d, clusterAccentSizePx })),
    [],
  );
  const setBehindSubject = useCallback(
    (behindSubject: boolean) => setDraft((d) => ({ ...d, behindSubject })),
    [],
  );
  const replay = useCallback(() => setPlayToken((t) => t + 1), []);

  const fireCommit = useCallback(
    async (toCommit: EditDraft, base: EditDraft, preCommitFinishedAt: string | null) => {
      const payload = buildEditPayload(toCommit, base);
      if (Object.keys(payload).length === 0) return; // nothing changed → no render
      setCommitting(true);
      try {
        await onCommitRef.current(payload);
        commitMarkerRef.current = preCommitFinishedAt;
        setSawRendering(false);
        setAwaitingRender(true);
        // The just-committed draft becomes the new baseline: a follow-up edit
        // diffs against what the server is now rendering.
        setBaseline(toCommit);
      } finally {
        setCommitting(false);
      }
    },
    [],
  );

  const commit = useCallback(async () => {
    setCommitError(null);
    setJustSaved(false);
    setIsEditing(false);
    if (variant.render_status === "rendering" || committing || awaitingRender) {
      // A render is already in flight (ours, or one started from another tab /
      // the admin page) — coalesce; the watcher fires it on the next ready
      // poll. awaitingRender must engage the watcher even for an EXTERNAL
      // render, or the queued draft would strand silently.
      commitMarkerRef.current = variantFinishedAt;
      setPendingDraft(draft);
      setAwaitingRender(true);
      return;
    }
    try {
      await fireCommit(draft, baseline, variantFinishedAt);
    } catch (e) {
      // Commit failed at the HTTP layer — NO silent retry (a refire loop would
      // hammer the API during an outage). Reopen the editor with the draft
      // intact and let the user hit Done again.
      setCommitError(e instanceof Error ? e.message : "Couldn't save — try again.");
      setIsEditing(true);
    }
  }, [
    variant.render_status,
    variantFinishedAt,
    committing,
    awaitingRender,
    draft,
    baseline,
    fireCommit,
  ]);

  // Render-completion watcher: when the in-flight render finishes, either fire
  // the coalesced follow-up commit or settle the session (show the new output).
  useEffect(() => {
    if (!awaitingRender || committing) return;
    if (variant.render_status === "rendering") {
      setSawRendering(true);
    } else if (variant.render_status === "ready") {
      // Guard against the pre-commit "ready" still being polled: this is OUR
      // render only if we watched it run or its completion fingerprint moved.
      const isFreshRender = sawRendering || variantFinishedAt !== commitMarkerRef.current;
      if (!isFreshRender) return;
      if (pendingDraft && !draftsEqual(pendingDraft, baseline)) {
        const toCommit = pendingDraft;
        setPendingDraft(null);
        void fireCommit(toCommit, baseline, variantFinishedAt).catch((e) => {
          // Same no-silent-retry policy as commit(): surface + reopen the
          // editor with the queued draft so nothing is lost.
          setDraft(toCommit);
          setCommitError(e instanceof Error ? e.message : "Couldn't save — try again.");
          setAwaitingRender(false);
          setIsEditing(true);
        });
      } else {
        setPendingDraft(null);
        setAwaitingRender(false);
        // Quiet "Saved" pulse: the live WYSIWYG preview is already on screen
        // (the caller keeps it up rather than flashing to output_url), so the
        // only affordance is this brief lime pulse that then recedes.
        setJustSaved(true);
      }
    } else if (variant.render_status === "failed") {
      // The committed render failed — drop out of the preview so the standard
      // failure card (with retry) takes over. The draft is preserved for a
      // fresh enterEdit only insofar as the variant still carries it.
      setPendingDraft(null);
      setAwaitingRender(false);
    }
  }, [
    awaitingRender,
    committing,
    variant.render_status,
    variantFinishedAt,
    sawRendering,
    pendingDraft,
    baseline,
    fireCommit,
  ]);

  // Recede the "Saved" pulse after a short beat (no blocking state).
  useEffect(() => {
    if (!justSaved) return;
    const t = setTimeout(() => setJustSaved(false), 1600);
    return () => clearTimeout(t);
  }, [justSaved]);

  // Unsaved-edits guard: warn before the tab closes mid-edit.
  useEffect(() => {
    if (!isEditing || !isDirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isEditing, isDirty]);

  return {
    isEditing,
    isSaving,
    justSaved,
    isActive,
    draft,
    isDirty,
    commitError,
    enterEdit,
    cancel,
    setText,
    setRemoved,
    setStyle,
    setSize,
    setLayout,
    setFont,
    setAnimation,
    setColor,
    setClusterHeroFont,
    setClusterBodyFont,
    setClusterAccentFont,
    setClusterHeroSizePx,
    setClusterBodySizePx,
    setClusterAccentSizePx,
    setBehindSubject,
    playToken,
    replay,
    commit,
  };
}
