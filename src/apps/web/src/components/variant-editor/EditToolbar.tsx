"use client";

import {
  INTRO_ANIMATIONS,
  INTRO_FONTS,
} from "@/lib/overlay-constants";
import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
import type { IntroOverlayParams } from "@/lib/overlay-layout";
import type { VariantEditSession } from "@/lib/variant-editor/useVariantEditSession";

export function EditToolbar({
  session,
  styleSets: _styleSets,
  fallbackSizePx,
  resolvedParams,
}: {
  session: VariantEditSession;
  /** Unused — style presets are replaced by independent pickers. Kept for back-compat. */
  styleSets: GenerativeStyleSet[];
  fallbackSizePx: number | null;
  resolvedParams?: IntroOverlayParams;
}) {
  const { draft } = session;
  const sliderPx = draft.sizePx ?? fallbackSizePx ?? 60;

  return (
    <div className="space-y-4 rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
      {!draft.removed && (
        <>
          {draft.layout === "cluster" ? (
            /* Editorial cluster: Hero + Body + Accent font pickers + per-role sizes */
            <>
              {(["hero", "body", "accent"] as const).map((role) => {
                const current =
                  role === "hero"
                    ? (draft.clusterHeroFont ?? resolvedParams?.clusterHeroFont ?? null)
                    : role === "body"
                      ? (draft.clusterBodyFont ?? resolvedParams?.clusterBodyFont ?? null)
                      : (draft.clusterAccentFont ?? resolvedParams?.clusterAccentFont ?? null);
                const setter =
                  role === "hero"
                    ? session.setClusterHeroFont
                    : role === "body"
                      ? session.setClusterBodyFont
                      : session.setClusterAccentFont;
                const label =
                  role === "hero" ? "Hero font" : role === "body" ? "Body font" : "Accent font";
                return (
                  <div key={role}>
                    <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-[#71717a]">
                      {label}
                    </div>
                    <div className="flex gap-1.5 overflow-x-auto pb-1">
                      {INTRO_FONTS.map((f) => {
                        const selected = current === f.name;
                        return (
                          <button
                            key={f.name}
                            onClick={() => setter(f.name)}
                            aria-pressed={selected}
                            style={{ fontFamily: f.cssFamily, fontWeight: f.weight }}
                            className={`shrink-0 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm transition-colors ${
                              selected
                                ? "border-lime-600 bg-lime-50 text-lime-800"
                                : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                            }`}
                          >
                            {f.name}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
              {/* Per-role size sliders for cluster */}
              {(
                [
                  { role: "Hero", sizePx: draft.clusterHeroSizePx, fallback: resolvedParams?.clusterHeroSizePx ?? sliderPx, setter: session.setClusterHeroSizePx },
                  { role: "Body", sizePx: draft.clusterBodySizePx, fallback: resolvedParams?.clusterBodySizePx ?? Math.round(sliderPx * 0.6), setter: session.setClusterBodySizePx },
                  { role: "Accent", sizePx: draft.clusterAccentSizePx, fallback: resolvedParams?.clusterAccentSizePx ?? Math.round(sliderPx * 0.74), setter: session.setClusterAccentSizePx },
                ] satisfies { role: string; sizePx: number | null; fallback: number; setter: (px: number) => void }[]
              ).map(({ role, sizePx, fallback, setter }) => {
                const val = sizePx ?? fallback;
                return (
                  <div key={role}>
                    <div className="mb-1.5 flex items-center justify-between">
                      <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#71717a]">
                        {role} size
                      </span>
                      <span className="tabular-nums text-xs text-[#71717a]">{val}px</span>
                    </div>
                    <input
                      type="range"
                      min={INTRO_SIZE_MIN}
                      max={INTRO_SIZE_MAX}
                      step={1}
                      value={val}
                      aria-label={`${role} text size`}
                      onChange={(e) => setter(Number(e.target.value))}
                      className="w-full accent-lime-600"
                    />
                  </div>
                );
              })}
            </>
          ) : (
            /* Linear: single font picker */
            <div>
              <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-[#71717a]">
                Font
              </div>
              <div className="flex gap-1.5 overflow-x-auto pb-1">
                {INTRO_FONTS.map((f) => {
                  const current = draft.fontFamily ?? resolvedParams?.fontFamily ?? null;
                  const selected = current === f.name;
                  return (
                    <button
                      key={f.name}
                      onClick={() => session.setFont(f.name)}
                      aria-pressed={selected}
                      style={{ fontFamily: f.cssFamily, fontWeight: f.weight }}
                      className={`shrink-0 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm transition-colors ${
                        selected
                          ? "border-lime-600 bg-lime-50 text-lime-800"
                          : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                      }`}
                    >
                      {f.name}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Animation picker — linear layout only */}
          {draft.layout !== "cluster" && (
            <div>
              <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-[#71717a]">
                Animation
              </div>
              <div className="flex flex-wrap gap-1.5">
                {INTRO_ANIMATIONS.map((a) => {
                  const current = draft.animation ?? resolvedParams?.effect ?? null;
                  const selected = current === a.value;
                  return (
                    <button
                      key={a.value}
                      onClick={() => session.setAnimation(a.value)}
                      aria-pressed={selected}
                      className={`rounded-lg border px-3 py-1 text-xs transition-colors ${
                        selected
                          ? "border-lime-600 bg-lime-50 text-lime-800"
                          : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                      }`}
                    >
                      {a.label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Color picker */}
          {(() => {
            const currentColor = (draft.textColor ?? resolvedParams?.textColor ?? "#FFFFFF").toUpperCase();
            return (
              <div>
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-[#71717a]">
                  Color
                </div>
                <label className="flex cursor-pointer items-center gap-3">
                  <div className="relative h-10 w-10 shrink-0">
                    <div
                      className="h-10 w-10 rounded-full shadow-sm ring-1 ring-zinc-200 ring-offset-2"
                      style={{ backgroundColor: currentColor }}
                    />
                    <input
                      type="color"
                      value={currentColor}
                      onChange={(e) => session.setColor(e.target.value)}
                      className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
                      aria-label="Pick text color"
                    />
                  </div>
                  <span className="font-mono text-xs text-[#71717a]">{currentColor}</span>
                </label>
              </div>
            );
          })()}

          {/* Size slider — linear layout only (cluster has per-role sliders above) */}
          {draft.layout !== "cluster" && (
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#71717a]">
                  Text size
                </span>
                <span className="tabular-nums text-xs text-[#71717a]">{sliderPx}px</span>
              </div>
              <input
                type="range"
                min={INTRO_SIZE_MIN}
                max={INTRO_SIZE_MAX}
                step={1}
                value={sliderPx}
                aria-label="Intro text size"
                onChange={(e) => session.setSize(Number(e.target.value))}
                className="w-full accent-lime-600"
              />
            </div>
          )}
        </>
      )}

      {session.commitError && (
        <p className="text-xs text-red-600" role="alert">
          {session.commitError}
        </p>
      )}

      {/* Actions */}
      <div className="flex items-center justify-between pt-0.5">
        <button
          onClick={() => session.setRemoved(!draft.removed)}
          className="text-xs text-[#71717a] underline-offset-2 hover:text-[#3f3f46] hover:underline"
        >
          {draft.removed ? "Add text back" : "Remove text"}
        </button>
        <div className="flex items-center gap-2">
          {!draft.removed && (
            <button
              onClick={session.replay}
              title="Replay entrance animation"
              className="rounded-lg border border-zinc-200 px-2.5 py-1 text-xs text-[#3f3f46] hover:border-zinc-300 hover:bg-zinc-50"
            >
              ↺ Replay
            </button>
          )}
          <button
            onClick={session.cancel}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs text-[#3f3f46] hover:border-zinc-300 hover:bg-zinc-50"
          >
            Cancel
          </button>
          <button
            onClick={() => void session.commit()}
            disabled={!session.isDirty}
            className="rounded-lg bg-[#0c0c0e] px-4 py-1.5 text-xs font-semibold text-white hover:opacity-80 disabled:opacity-40"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
