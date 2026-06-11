"use client";

// Light-editorial confirm dialog: centered white card on a soft scrim,
// Playfair question line, ink confirm pill + quiet zinc text cancel.
// Focus-trapped; Escape cancels; focus returns to the opener on close.

import { useEffect, useRef } from "react";
import { useFocusTrap } from "./useFocusTrap";

interface ConfirmDialogProps {
  open: boolean;
  /** The Playfair question line ("Discard your clip edits?"). */
  question: string;
  /** Optional quieter supporting line under the question. */
  detail?: string;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  question,
  detail,
  confirmLabel,
  cancelLabel = "Cancel",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  useFocusTrap(cardRef, open);

  useEffect(() => {
    if (!open) return;
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 px-6"
      onClick={onCancel}
    >
      <div
        ref={cardRef}
        role="alertdialog"
        aria-modal="true"
        aria-label={question}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[400px] rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm"
      >
        <p className="font-display text-xl text-[#0c0c0e]">{question}</p>
        {detail && <p className="mt-2 text-sm text-[#71717a]">{detail}</p>}
        <div className="mt-6 flex items-center justify-end gap-4">
          <button
            onClick={onCancel}
            className="px-2 py-2 text-sm text-[#71717a] hover:underline underline-offset-4"
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            onClick={onConfirm}
            className="rounded-full bg-[#0c0c0e] px-6 py-2.5 text-sm font-semibold text-white hover:opacity-80 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
