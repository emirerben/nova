"use client";

// Light-editorial confirm dialog. Thin wrapper over the shadcn `AlertDialog`
// primitive (DESIGN.md §15) — same props/behavior as before the migration:
// role="alertdialog", accessible name = `question`, confirm button focused
// on open, Escape cancels. Every existing `<ConfirmDialog>` call site keeps
// working unchanged (PlanVariantEditor.tsx, EditorShell.tsx).

import { useRef } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from "./alert-dialog";

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
  const confirmRef = useRef<HTMLButtonElement>(null);
  // Radix fires onOpenChange(false) for every dismissal path — Escape,
  // overlay click, Cancel, AND Confirm (Action closes too). Route it to
  // onCancel except right after a Confirm click, so callers never see both
  // onConfirm and onCancel fire for the same interaction.
  const confirmedRef = useRef(false);

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (next) return;
        if (confirmedRef.current) {
          confirmedRef.current = false;
          return;
        }
        onCancel();
      }}
    >
      <AlertDialogContent
        aria-label={question}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          confirmRef.current?.focus();
        }}
      >
        <AlertDialogTitle>{question}</AlertDialogTitle>
        {detail ? <AlertDialogDescription>{detail}</AlertDialogDescription> : null}
        <AlertDialogFooter>
          <AlertDialogCancel>{cancelLabel}</AlertDialogCancel>
          <AlertDialogAction
            ref={confirmRef}
            onClick={() => {
              confirmedRef.current = true;
              onConfirm();
            }}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
