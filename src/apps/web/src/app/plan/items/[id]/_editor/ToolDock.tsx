"use client";

/**
 * ToolDock — the pocket editor's bottom tool bar (mobile editor Lane A).
 *
 * Mirrors the desktop ToolRail's tool set and disabled-tools contract, laid
 * out as a safe-area-aware bottom dock. Disabled tools use the
 * focusable-disabled pattern (aria-disabled, NOT the disabled attribute) so a
 * tap still fires and can surface the reason via onDisabledTap.
 *
 * At 375–430px the 7-tool set (or 6 without Nova) doesn't fit a flex-1 row —
 * label text forces each item's min-content width past its 1/7th share, and
 * the trailing tools (Overlays, Styles) clip or fall off-screen. Fixed-width
 * flex-none items in a horizontally scrollable, snap-x row keep every tool
 * reachable regardless of viewport width; the active tool auto-scrolls into
 * view (instant unless the visitor allows motion — see motion-safe:scroll-smooth
 * on the nav).
 */

import { useEffect, useRef, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
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

  const buttonRefs = useRef<Partial<Record<DockTool, HTMLButtonElement | null>>>({});

  // Auto-scroll the active tool into view when it changes (e.g. keyboard nav,
  // or a tool opened from elsewhere in the shell). `motion-safe:scroll-smooth`
  // on the nav is what makes this animate — this call stays behavior-agnostic
  // so a reduced-motion visitor gets an instant jump instead.
  useEffect(() => {
    if (!activeTool) return;
    buttonRefs.current[activeTool]?.scrollIntoView({ inline: "nearest", block: "nearest" });
  }, [activeTool]);

  return (
    <nav
      aria-label="Editor tools"
      data-testid="pocket-dock"
      className="flex flex-row overflow-x-auto snap-x scrollbar-none motion-safe:scroll-smooth border-t border-border bg-background pb-[max(8px,env(safe-area-inset-bottom))] pt-1.5"
    >
      {tools.map((tool) => {
        const active = activeTool === tool.id;
        const disabledReason = disabledTools[tool.id];
        const enabled = !disabledReason;
        return (
          <Button
            key={tool.id}
            ref={(el) => {
              buttonRefs.current[tool.id] = el;
            }}
            type="button"
            variant="ghost"
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
            className={`flex h-auto min-h-[56px] w-[72px] flex-none snap-start flex-col items-center justify-center gap-0.5 rounded-none active:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
              active ? "bg-transparent text-foreground hover:bg-transparent" : "bg-transparent text-muted-foreground hover:bg-transparent"
            }`}
          >
            <span className={enabled ? undefined : "opacity-50"}>
              {tool.icon("h-5 w-5")}
            </span>
            <span
              className={`text-[11px] font-medium ${
                enabled ? "" : "text-muted-foreground"
              }`}
            >
              {tool.label}
            </span>
            <span
              aria-hidden="true"
              className={`h-0.5 w-4 rounded-full ${
                active && enabled ? "bg-foreground" : "bg-transparent"
              }`}
            />
          </Button>
        );
      })}
    </nav>
  );
}
