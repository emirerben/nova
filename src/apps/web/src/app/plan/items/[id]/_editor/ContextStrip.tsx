"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/**
 * ContextStrip — horizontally scrollable selection actions for Pocket.
 *
 * Stock shadcn Button variants keep component chrome consistent. Delete is
 * ALWAYS the word "Delete" — never an icon — so destructive intent stays
 * unmistakable. Disabled actions stay focusable and route to onDisabledTap.
 */

export type StripSelection =
  | {
      type: "text";
      onEdit: () => void;
      onStyle: () => void;
      onTiming: () => void;
      onDelete: () => void;
    }
  | {
      type: "caption";
      onEditCue: () => void;
      onAllCaptions: () => void;
    }
  | {
      type: "overlay";
      onEdit: () => void;
      onTiming: () => void;
      onDelete: () => void;
    }
  | {
      type: "motion";
      onEdit: () => void;
      onTiming: () => void;
      onDelete: () => void;
    }
  | {
      type: "carousel";
      onEdit: () => void;
      onDelete: () => void;
      deleteDisabledReason?: string | null;
    }
  | {
      type: "clip";
      onAdjust: () => void;
      onSplit: () => void;
      splitDisabledReason: string | null;
      muted: boolean;
      onToggleMute: () => void;
      onDelete: () => void;
      deleteDisabledReason?: string | null;
    };

export interface ContextStripProps {
  selection: StripSelection | null;
  onDisabledTap: (reason: string) => void;
  className?: string;
}

interface Pill {
  label: string;
  onPress: () => void;
  primary?: boolean;
  destructive?: boolean;
  disabledReason?: string | null;
}

function pillsForSelection(selection: StripSelection): Pill[] {
  switch (selection.type) {
    case "text":
      return [
        { label: "Edit", onPress: selection.onEdit, primary: true },
        { label: "Style", onPress: selection.onStyle },
        { label: "Timing", onPress: selection.onTiming },
        { label: "Delete", onPress: selection.onDelete, destructive: true },
      ];
    case "caption":
      return [
        { label: "Edit cue", onPress: selection.onEditCue, primary: true },
        { label: "All captions", onPress: selection.onAllCaptions },
      ];
    case "overlay":
      return [
        { label: "Edit", onPress: selection.onEdit, primary: true },
        { label: "Timing", onPress: selection.onTiming },
        { label: "Delete", onPress: selection.onDelete, destructive: true },
      ];
    case "motion":
      return [
        { label: "Edit", onPress: selection.onEdit, primary: true },
        { label: "Timing", onPress: selection.onTiming },
        { label: "Delete", onPress: selection.onDelete, destructive: true },
      ];
    case "carousel":
      return [
        { label: "Edit", onPress: selection.onEdit, primary: true },
        {
          label: "Delete",
          onPress: selection.onDelete,
          destructive: true,
          disabledReason: selection.deleteDisabledReason ?? undefined,
        },
      ];
    case "clip":
      return [
        { label: "Adjust", onPress: selection.onAdjust, primary: true },
        {
          label: "Split",
          onPress: selection.onSplit,
          disabledReason: selection.splitDisabledReason,
        },
        {
          label: selection.muted ? "Unmute" : "Mute",
          onPress: selection.onToggleMute,
        },
        {
          label: "Delete",
          onPress: selection.onDelete,
          destructive: true,
          disabledReason: selection.deleteDisabledReason ?? undefined,
        },
      ];
  }
}

export function ContextStrip({
  selection,
  onDisabledTap,
  className,
}: ContextStripProps): JSX.Element | null {
  if (selection === null) return null;

  const pills = pillsForSelection(selection);

  return (
    <div
      role="toolbar"
      aria-label="Selection actions"
      data-testid="pocket-context-strip"
      className={cn(
        "scrollbar-none flex min-h-12 max-w-full items-center gap-1 overflow-x-auto",
        className,
      )}
    >
      {pills.map((pill) => {
        const disabled = pill.disabledReason != null;
        const variant = pill.primary ? "secondary" : "ghost";
        return (
          <Button
            key={pill.label}
            type="button"
            variant={variant}
            aria-disabled={disabled ? true : undefined}
            onClick={() => {
              // Focusable-disabled: the tap still fires so the WHY surfaces.
              if (disabled) {
                onDisabledTap(pill.disabledReason as string);
                return;
              }
              pill.onPress();
            }}
            className={cn(
              "h-11 shrink-0 rounded-md px-3 text-[13px] shadow-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 focus-visible:ring-0",
              pill.destructive && "text-destructive hover:text-destructive",
              disabled && "opacity-50",
            )}
          >
            {pill.label}
          </Button>
        );
      })}
    </div>
  );
}
