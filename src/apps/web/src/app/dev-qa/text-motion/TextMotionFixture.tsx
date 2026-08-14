"use client";

import { useState } from "react";
import TextElementOverlayLayer from "@/app/plan/items/[id]/components/TextElementOverlayLayer";
import TextMotionControls from "@/components/text-motion/TextMotionControls";
import type { TextElement } from "@/lib/plan-api";
import {
  defaultTextMotion,
  type TextMotionConfigV2,
} from "@/lib/text-motion-v2";

const ELEMENT_BASE: Omit<TextElement, "motion"> = {
  id: "smooth-type-qa",
  text: "CREATE WITH\nCONFIDENCE",
  start_s: 0,
  end_s: 5.3,
  role: "generative_intro",
  position: "middle",
  x_frac: 0.5,
  y_frac: 0.48,
  font_family: "Inter",
  size_px: 112,
  color: "#FFFFFF",
  stroke_width: 0,
  shadow_enabled: true,
  alignment: "center",
  effect: "smooth-type",
  max_width_frac: 0.86,
};

export default function TextMotionFixture() {
  const [motion, setMotion] = useState<TextMotionConfigV2>(
    defaultTextMotion("smooth-type"),
  );
  const [currentTime, setCurrentTime] = useState(0.4);
  const element: TextElement = { ...ELEMENT_BASE, motion };

  return (
    <main className="min-h-screen bg-[#f5f5f2] px-5 py-8 text-[#0c0c0e]">
      <div className="mx-auto grid max-w-[920px] gap-7 md:grid-cols-[minmax(0,1fr)_320px]">
        <section>
          <div className="mx-auto aspect-[9/16] w-full max-w-[360px] overflow-hidden rounded-2xl bg-[#101114] shadow-xl">
            <div className="relative h-full w-full">
              <TextElementOverlayLayer elements={[element]} currentTime={currentTime} />
            </div>
          </div>
          <label className="mx-auto mt-4 block max-w-[360px] text-sm font-semibold">
            <span className="flex justify-between">
              <span>Preview time</span>
              <span className="font-normal tabular-nums text-zinc-500">
                {currentTime.toFixed(2)}s
              </span>
            </span>
            <input
              aria-label="Preview time"
              type="range"
              min={0}
              max={5.3}
              step={1 / 30}
              value={currentTime}
              onChange={(event) => setCurrentTime(Number(event.target.value))}
              className="mt-2 w-full accent-[#0c0c0e]"
            />
          </label>
        </section>

        <aside className="h-fit rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-zinc-500">
            Text animation
          </p>
          <h1 className="mt-1 text-xl font-semibold">Smooth Type</h1>
          <TextMotionControls
            effect="smooth-type"
            motion={motion}
            onChange={(patch) => setMotion((current) => ({ ...current, ...patch, version: 2 }))}
          />
        </aside>
      </div>
      <div
        id="qa-state"
        data-time={currentTime}
        data-motion={JSON.stringify(motion)}
        aria-hidden="true"
      />
    </main>
  );
}
