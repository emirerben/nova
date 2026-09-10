"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { EditorConfirmation } from "./protocol";

/** Confirmation belongs to one live draft and is never transferable to a successor. */
export function useEditorConfirmation(revision: string) {
  const [pending, setPending] = useState<EditorConfirmation | null>(null);
  const currentRevision = useRef(revision);
  currentRevision.current = revision;
  const waiter = useRef<{ value: EditorConfirmation; resolve: (approved: boolean) => void } | null>(null);

  const decide = useCallback((id: string, approved: boolean) => {
    const current = waiter.current;
    if (!current || current.value.id !== id) return;
    waiter.current = null;
    setPending(null);
    current.resolve(approved && current.value.revision === currentRevision.current);
  }, []);

  const request = useCallback((title: string) => new Promise<boolean>((resolve) => {
    if (waiter.current) { resolve(false); return; }
    const value = { id: crypto.randomUUID(), title, revision: currentRevision.current };
    waiter.current = { value, resolve };
    setPending(value);
  }), []);

  useEffect(() => {
    if (waiter.current && waiter.current.value.revision !== revision) {
      decide(waiter.current.value.id, false);
    }
  }, [decide, revision]);
  useEffect(() => () => {
    waiter.current?.resolve(false);
    waiter.current = null;
  }, []);

  return { pending, request, decide };
}
