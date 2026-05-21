"use client";

import {
  ACTIVE_FONT_NAMES,
  FONT_REGISTRY,
  FONT_VIBE_LABELS,
  FONT_VIBES,
  fontPreviewSlug,
  type FontVibe,
} from "./overlay-constants";

interface FontLibraryBrowserProps {
  currentFamily?: string | null;
  onPickFont: (family: string) => void;
}

export function fontsByVibe(): Record<FontVibe, string[]> {
  const grouped = Object.fromEntries(
    FONT_VIBES.map((vibe) => [vibe, [] as string[]]),
  ) as Record<FontVibe, string[]>;

  for (const family of ACTIVE_FONT_NAMES) {
    const vibe = FONT_REGISTRY[family]?.vibe;
    if (vibe && vibe in grouped) {
      grouped[vibe].push(family);
    }
  }

  return grouped;
}

export function FontLibraryBrowser({
  currentFamily,
  onPickFont,
}: FontLibraryBrowserProps) {
  const grouped = fontsByVibe();

  return (
    <div className="space-y-4 pt-2">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-medium uppercase tracking-wide text-zinc-400">
          Browse library
        </h4>
        <span className="text-[10px] text-zinc-600">
          {ACTIVE_FONT_NAMES.length} active fonts
        </span>
      </div>

      {FONT_VIBES.map((vibe) => {
        const families = grouped[vibe];
        if (families.length === 0) return null;

        return (
          <section key={vibe} className="space-y-2">
            <div className="flex items-center gap-2">
              <h5 className="text-[11px] font-medium text-zinc-300">
                {FONT_VIBE_LABELS[vibe]}
              </h5>
              <span className="text-[10px] text-zinc-600">
                {families.length}
              </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
              {families.map((family) => {
                const isCurrent = family === currentFamily;
                return (
                  <button
                    key={family}
                    type="button"
                    onClick={() => onPickFont(family)}
                    className={[
                      "group rounded border p-1.5 text-left transition",
                      isCurrent
                        ? "border-emerald-500 bg-emerald-950/30"
                        : "border-zinc-800 bg-zinc-950 hover:border-zinc-600",
                    ].join(" ")}
                    title={`Use ${family}`}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={`/font-previews/${fontPreviewSlug(family)}.png`}
                      alt={family}
                      width={180}
                      height={36}
                      loading="lazy"
                      className="h-9 w-full rounded bg-zinc-900 object-contain"
                    />
                    <span className="mt-1 block truncate text-[10px] text-zinc-300">
                      {family}
                    </span>
                  </button>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}
