"use client";

/**
 * ToolRail — the left icon-over-label rail (plan §2).
 *
 * Active tool = ink-bordered card (approved mockup Variant A); inactive =
 * borderless ghost icon-over-label buttons. Clicking the active tool toggles
 * its drawer closed.
 *
 * Tool availability is driven by server capabilities so kill switches surface
 * as disabled buttons with honest tooltips.
 */

export type EditorTool = "text" | "sounds" | "overlays" | "styles";

const TOOLS: Array<{ id: EditorTool; icon: string; label: string }> = [
  { id: "text", icon: "T", label: "Text" },
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
  return (
    <div
      data-region="tool-rail"
      className="flex w-[92px] flex-col items-center gap-2 border-r border-zinc-200 bg-white pt-3"
    >
      {TOOLS.map((tool) => {
        const active = activeTool === tool.id;
        const disabledReason = disabledTools[tool.id];
        const enabled = !disabledReason;
        return (
          <button
            key={tool.id}
            type="button"
            disabled={!enabled}
            aria-pressed={active}
            aria-label={`${tool.label} tool`}
            title={enabled ? tool.label : `${tool.label} — ${disabledReason}`}
            onClick={() => onToggleTool(tool.id)}
            className={`flex h-16 w-16 flex-col items-center justify-center gap-1 rounded-xl border focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
              active
                ? "border-[#0c0c0e]"
                : "border-transparent hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            }`}
          >
            <span
              aria-hidden
              className={`text-[17px] leading-none ${
                active ? "font-bold text-[#0c0c0e]" : "text-[#3f3f46]"
              }`}
            >
              {tool.icon}
            </span>
            <span
              className={`text-[11px] ${
                active ? "font-semibold text-[#0c0c0e]" : "text-[#71717a]"
              }`}
            >
              {tool.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
