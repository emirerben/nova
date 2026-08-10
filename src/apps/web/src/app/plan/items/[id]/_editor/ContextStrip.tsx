"use client";

/**
 * ContextStrip — floating selection-action pills (mobile editor Lane A).
 *
 * Renders a per-selection-type pill row above the dock. The first pill of
 * each type is the primary (ink-filled) action. Delete is ALWAYS the word
 * "Delete" — never an icon — so destructive intent stays unmistakable.
 * Disabled pills stay tappable (aria-disabled) and route to onDisabledTap.
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
        { label: "Delete", onPress: selection.onDelete, destructive: true },
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
      className={`flex gap-1.5${className ? ` ${className}` : ""}`}
    >
      {pills.map((pill) => {
        const disabled = pill.disabledReason != null;
        const tone = pill.primary
          ? "bg-[#0c0c0e] text-white"
          : pill.destructive
            ? "border border-zinc-200 bg-white text-[#3f3f46]"
            : "border border-zinc-200 bg-white text-[#0c0c0e]";
        return (
          <button
            key={pill.label}
            type="button"
            aria-disabled={disabled ? true : undefined}
            onClick={() => {
              // Focusable-disabled: the tap still fires so the WHY surfaces.
              if (disabled) {
                onDisabledTap(pill.disabledReason as string);
                return;
              }
              pill.onPress();
            }}
            className={`min-h-11 rounded-full px-4 text-[13px] font-semibold shadow-[0_3px_10px_rgba(12,12,14,0.1)] active:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${tone}${
              disabled ? " opacity-50" : ""
            }`}
          >
            {pill.label}
          </button>
        );
      })}
    </div>
  );
}
