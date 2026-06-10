"use client";

/**
 * Instant-edit session state for one generative variant.
 *
 * The Instagram-style editor: every text/style/size change lands in a local
 * `draft` (0 network), previewed live over the base video. "Done" commits the
 * WHOLE session as ONE `POST .../edit` → one re-render, instead of the legacy
 * one-render-per-field endpoints.
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
import type { EditVariantPayload, GenerativeVariant } from "@/lib/generative-api";

export interface EditDraft {
  text: string;
  removed: boolean;
  styleSetId: string | null;
  sizePx: number | null;
}

export interface VariantEditSession {
  /** Edit mode is open (toolbar + preview visible). */
  isEditing: boolean;
  /** A commit is in flight or its render is still running — show "Saving…". */
  isSaving: boolean;
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
  commit: () => Promise<void>;
}

function draftFromVariant(variant: GenerativeVariant): EditDraft {
  return {
    text: variant.intro_text ?? "",
    removed: variant.text_mode === "none",
    styleSetId: variant.style_set_id ?? null,
    sizePx: variant.intro_text_size_px ?? null,
  };
}

function draftsEqual(a: EditDraft, b: EditDraft): boolean {
  return (
    a.text.trim() === b.text.trim() &&
    a.removed === b.removed &&
    a.styleSetId === b.styleSetId &&
    a.sizePx === b.sizePx
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
  return payload;
}

export function useVariantEditSession(
  variant: GenerativeVariant,
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
    setIsEditing(true);
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
    commit,
  };
}
