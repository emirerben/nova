"use client";

/**
 * InspectorRail — the right edge rail (plan §4, decision D6).
 *
 * Vertical tabs switching the inspector column between properties and preset
 * browsing:
 *  - `Basic` is DISABLED (zinc, no hover) until a selection exists; selecting
 *    anything activates it and switches to it (the shell handles the switch).
 *  - `Presets` is always browsable.
 * Active tab = ink-bordered card, same treatment as the left tool rail.
 */

export type InspectorTab = "basic" | "presets";

export default function InspectorRail({
  tab,
  hasSelection,
  onTab,
}: {
  tab: InspectorTab;
  hasSelection: boolean;
  onTab: (tab: InspectorTab) => void;
}) {
  const tabs: Array<{ id: InspectorTab; icon: string; label: string; enabled: boolean }> = [
    { id: "basic", icon: "Aa", label: "Basic", enabled: hasSelection },
    { id: "presets", icon: "❏", label: "Presets", enabled: true },
  ];
  return (
    <div
      data-region="inspector-rail"
      className="flex w-[72px] flex-col items-center gap-2 border-l border-zinc-200 bg-white pt-3"
    >
      {tabs.map((t) => {
        const active = tab === t.id;
        return (
          <button
            key={t.id}
            type="button"
            disabled={!t.enabled}
            aria-pressed={active}
            aria-label={`${t.label} inspector tab`}
            title={t.enabled ? t.label : "Select something to edit its properties"}
            onClick={() => onTab(t.id)}
            className={`flex h-14 w-14 flex-col items-center justify-center gap-0.5 rounded-xl border focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
              active
                ? "border-[#0c0c0e]"
                : "border-transparent hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            }`}
          >
            <span
              aria-hidden
              className={`text-[13px] leading-none ${
                active ? "font-bold text-[#0c0c0e]" : "text-[#3f3f46]"
              }`}
            >
              {t.icon}
            </span>
            <span
              className={`text-[10px] ${
                active ? "font-semibold text-[#0c0c0e]" : "text-[#71717a]"
              }`}
            >
              {t.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
