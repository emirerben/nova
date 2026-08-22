"use client";

import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";

/**
 * ToolRail — the left icon-over-label rail (plan §2).
 *
 * Active tool = ink-bordered card (approved mockup Variant A); inactive =
 * borderless ghost icon-over-label buttons. Clicking the active tool toggles
 * its drawer closed.
 *
 * Tool availability is driven by server capabilities so kill switches surface
 * as disabled buttons with honest tooltips.
 *
 * Disabled tools use the focusable-disabled pattern (review fix round on plan
 * 010): `aria-disabled="true"` instead of the `disabled` attribute, a no-op
 * onClick, and `aria-describedby` → a visually-hidden per-tool reason element,
 * so keyboard / screen-reader / touch users can reach the WHY (a native
 * `title` on a `disabled` button is mouse-hover-only). The title stays as a
 * pointer bonus.
 */

export type EditorTool =
  | "nova"
  | "text"
  | "captions"
  | "visuals"
  | "sounds"
  | "overlays"
  | "styles";

export const NOVA_TOOL_SEEN_KEY = "nova-tool-seen";

// Captions sit next to Text, not at the end: both are typography, and on a
// subtitled/narrated edit captions are the most-edited object on screen. The
// glyph stays in the rail's text-glyph family (`T`, `Aa` in InspectorRail) —
// "CC" reads as captions everywhere without importing an icon set.
const TOOLS: Array<{ id: EditorTool; icon: string; label: string }> = [
  { id: "nova", icon: "✧", label: "Nova" },
  { id: "text", icon: "T", label: "Text" },
  { id: "captions", icon: "CC", label: "Captions" },
  { id: "visuals", icon: "▦", label: "Visuals" },
  { id: "sounds", icon: "♫", label: "Sounds" },
  { id: "overlays", icon: "▤", label: "Overlays" },
  { id: "styles", icon: "✦", label: "Styles" },
];

export default function ToolRail({
  activeTool,
  disabledTools = {},
  onToggleTool,
}: {
  /** null = drawer closed, no active tool. */
  activeTool: EditorTool | null;
  disabledTools?: Partial<Record<EditorTool, string | null>>;
  onToggleTool: (tool: EditorTool) => void;
}) {
  const [novaSeen, setNovaSeen] = useState(true);
  const copilotEnabled = process.env.NEXT_PUBLIC_EDIT_COPILOT_ENABLED === "true";
  const visualBlocksEnabled =
    process.env.NEXT_PUBLIC_VISUAL_BLOCKS_ENABLED === "true";
  const motionScenesEnabled =
    process.env.NEXT_PUBLIC_MOTION_SCENES_ENABLED === "true";
  const tools = useMemo(
    () =>
      TOOLS.filter(
        (tool) =>
          (copilotEnabled || tool.id !== "nova") &&
          (visualBlocksEnabled || motionScenesEnabled || tool.id !== "visuals"),
      ),
    [copilotEnabled, motionScenesEnabled, visualBlocksEnabled],
  );

  useEffect(() => {
    if (!copilotEnabled) return;
    try {
      setNovaSeen(window.localStorage.getItem(NOVA_TOOL_SEEN_KEY) === "true");
    } catch {
      setNovaSeen(true);
    }
  }, [copilotEnabled]);

  useEffect(() => {
    if (activeTool !== "nova") return;
    try {
      window.localStorage.setItem(NOVA_TOOL_SEEN_KEY, "true");
    } catch {
      /* localStorage unavailable — discovery degrades silently */
    }
    setNovaSeen(true);
  }, [activeTool]);

  return (
    <div
      data-region="tool-rail"
      className="flex w-16 flex-col items-center gap-1 border-r border-border bg-background pt-3"
    >
      {tools.map((tool) => {
        const active = activeTool === tool.id;
        const disabledReason = disabledTools[tool.id];
        const enabled = !disabledReason;
        const reasonId = `tool-rail-reason-${tool.id}`;
        const showNovaPing = tool.id === "nova" && !active && !novaSeen && enabled;
        return (
          <Button
            key={tool.id}
            type="button"
            variant="ghost"
            aria-disabled={enabled ? undefined : true}
            aria-describedby={enabled ? undefined : reasonId}
            aria-pressed={active}
            aria-label={`${tool.label} tool`}
            title={enabled ? tool.label : `${tool.label} — ${disabledReason}`}
            onClick={() => {
              if (!enabled) return; // focusable-disabled: reachable, inert
              onToggleTool(tool.id);
            }}
            className={`relative flex h-16 w-16 flex-col items-center justify-center gap-1 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
              active
                ? "bg-muted text-foreground hover:bg-muted"
                : enabled
                  ? "text-muted-foreground hover:bg-muted"
                  : "cursor-not-allowed text-muted-foreground/40 hover:bg-transparent"
            }`}
          >
            {showNovaPing && (
              <span className="pointer-events-none absolute ml-8 mt-[-42px] flex h-2.5 w-2.5">
                <span className="absolute inline-flex h-full w-full rounded-full bg-lime-500 opacity-75 motion-safe:animate-ping" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-lime-600" />
              </span>
            )}
            <span
              aria-hidden
              className={`flex h-5 w-5 items-center justify-center text-[17px] leading-none ${
                active ? "font-bold" : ""
              }`}
            >
              {tool.icon}
            </span>
            <span className={`text-[11px] ${active ? "font-semibold" : ""}`}>
              {tool.label}
            </span>
            {!enabled && (
              <span id={reasonId} className="sr-only">
                {disabledReason}
              </span>
            )}
          </Button>
        );
      })}
    </div>
  );
}
