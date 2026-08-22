"use client";

/**
 * Dropzone — stock shadcn-style drag/drop + click-to-browse file picker
 * (Lane G, DESIGN.md §15). Wraps a visually-hidden `<input type="file">`
 * behind a large dashed drop target; click, Enter/Space, and a real
 * dataTransfer drop all route through the same `onFiles` callback so
 * callers can share one handler across every entry point.
 *
 * `ariaLabel` sets the drop target's accessible name independently of its
 * visible copy (title/subline) — callers that need a stable a11y name across
 * copy changes (e.g. per-format helper text) should pass one explicitly.
 */

import * as React from "react";
import { Upload } from "lucide-react";

import { cn } from "@/lib/cn";

export interface DropzoneProps {
  /** Always called with a FileList — both input.files and
   *  event.dataTransfer.files are FileList, never a bare File[]. */
  onFiles: (files: FileList | null) => void;
  accept?: string;
  multiple?: boolean;
  disabled?: boolean;
  /** Compact footprint (min-h-20) for "add more" states once content exists. */
  compact?: boolean;
  title: React.ReactNode;
  subline?: React.ReactNode;
  /** Accessible name for the drop target itself. */
  ariaLabel: string;
  /** Accessible name for the underlying sr-only file input (existing contract
   *  some call sites already query by, e.g. "Upload video clips for this idea"). */
  inputAriaLabel: string;
  icon?: React.ReactNode;
  className?: string;
}

export const Dropzone = React.forwardRef<HTMLInputElement, DropzoneProps>(
  function Dropzone(
    {
      onFiles,
      accept,
      multiple = true,
      disabled = false,
      compact = false,
      title,
      subline,
      ariaLabel,
      inputAriaLabel,
      icon,
      className,
    },
    ref,
  ) {
    const inputRef = React.useRef<HTMLInputElement>(null);
    const [dragging, setDragging] = React.useState(false);

    React.useImperativeHandle(ref, () => inputRef.current as HTMLInputElement);

    const open = React.useCallback(() => {
      if (disabled) return;
      inputRef.current?.click();
    }, [disabled]);

    return (
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled || undefined}
        aria-label={ariaLabel}
        data-dragging={dragging || undefined}
        onClick={open}
        onKeyDown={(event) => {
          if (disabled) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            open();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (disabled) return;
          onFiles(event.dataTransfer.files);
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/25 bg-muted/30 px-6 text-center transition-colors hover:bg-muted/50 data-[dragging]:border-primary data-[dragging]:bg-muted",
          compact ? "min-h-20 gap-1 px-4 py-4" : "min-h-48 py-10",
          disabled && "pointer-events-none cursor-not-allowed opacity-50",
          className,
        )}
      >
        {!compact && (icon ?? <Upload aria-hidden className="h-8 w-8 text-muted-foreground" />)}
        <p className="text-sm font-medium text-foreground">{title}</p>
        {subline && <p className="text-xs text-muted-foreground">{subline}</p>}
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          multiple={multiple}
          aria-label={inputAriaLabel}
          className="sr-only"
          tabIndex={-1}
          disabled={disabled}
          onChange={(event) => {
            onFiles(event.target.files);
            // Reset so re-selecting the same file fires change again (Safari
            // otherwise keeps rendering the chosen filename after a delete).
            event.target.value = "";
          }}
        />
      </div>
    );
  },
);
