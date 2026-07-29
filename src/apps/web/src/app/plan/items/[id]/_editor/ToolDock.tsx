"use client";

/**
 * ToolDock — the pocket editor's bottom tool bar (mobile editor Lane A).
 *
 * Mirrors the desktop ToolRail's tool set and disabled-tools contract, laid
 * out as a safe-area-aware bottom dock. Disabled tools use the
 * focusable-disabled pattern (aria-disabled, NOT the disabled attribute) so a
 * tap still fires and can surface the reason via onDisabledTap.
 */

import type { ReactNode } from "react";
import type { PocketTool } from "./mobile-editor-state";
import {
  CaptionsIcon,
  NovaIcon,
  OverlaysIcon,
  SoundsIcon,
  StylesIcon,
  TextIcon,
  VisualsIcon,
} from "./editor-icons";

export type DockTool = PocketTool | "nova";

export interface ToolDockProps {
  activeTool: DockTool | null;
  disabledTools: Partial<Record<DockTool, string | null>>;
  novaEnabled: boolean;
  onToggleTool: (tool: DockTool) => void;
  onDisabledTap: (reason: string) => void;
}

// Desktop ToolRail order: nova, text, captions, visuals, sounds, overlays,
// styles. Nova is gated behind novaEnabled at render time.
const DOCK_TOOLS: Array<{
  id: DockTool;
  label: string;
  icon: (className: string) => ReactNode;
}> = [
  { id: "nova", label: "Nova", icon: (c) => <NovaIcon className={c} /> },
  { id: "text", label: "Text", icon: (c) => <TextIcon className={c} /> },
  { id: "captions", label: "Captions", icon: (c) => <CaptionsIcon className={c} /> },
  { id: "visuals", label: "Visuals", icon: (c) => <VisualsIcon className={c} /> },
  { id: "sounds", label: "Sounds", icon: (c) => <SoundsIcon className={c} /> },
  { id: "overlays", label: "Overlays", icon: (c) => <OverlaysIcon className={c} /> },
  { id: "styles", label: "Styles", icon: (c) => <StylesIcon className={c} /> },
];

export function ToolDock({
  activeTool,
  disabledTools,
  novaEnabled,
  onToggleTool,
  onDisabledTap,
}: ToolDockProps): JSX.Element {
  const tools = novaEnabled
    ? DOCK_TOOLS
    : DOCK_TOOLS.filter((tool) => tool.id !== "nova");

  return (
    <nav
      aria-label="Editor tools"
      data-testid="pocket-dock"
      className="flex flex-row border-t border-zinc-200 bg-[#fafaf8] pb-[max(8px,env(safe-area-inset-bottom))] pt-1.5"
    >
      {tools.map((tool) => {
        const active = activeTool === tool.id;
        const disabledReason = disabledTools[tool.id];
        const enabled = !disabledReason;
        return (
          <button
            key={tool.id}
            type="button"
            data-testid={`pocket-dock-${tool.id}`}
            aria-label={`${tool.label} tool`}
            aria-pressed={active}
            aria-disabled={enabled ? undefined : true}
            onClick={() => {
              // Focusable-disabled: the tap still fires so the WHY surfaces.
              if (!enabled) {
                onDisabledTap(disabledReason);
                return;
              }
              onToggleTool(tool.id);
            }}
            className={`flex min-h-[56px] flex-1 flex-col items-center justify-center gap-0.5 active:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
              active ? "text-[#0c0c0e]" : "text-zinc-600"
            }`}
          >
            <span className={enabled ? undefined : "opacity-50"}>
              {tool.icon("h-6 w-6")}
            </span>
            <span
              className={`text-[11px] font-medium ${
                enabled ? "" : "text-[#71717a]"
              }`}
            >
              {tool.label}
            </span>
            <span
              aria-hidden="true"
              className={`h-0.5 w-4 rounded-full ${
                active && enabled ? "bg-[#0c0c0e]" : "bg-transparent"
              }`}
            />
          </button>
        );
      })}
    </nav>
  );
}
