"use client";

import { useMemo, useState } from "react";
import EditorTimelineBody from "@/app/plan/items/[id]/_editor/EditorTimelineBody";
import { buildVirtualTimeline } from "@/app/plan/items/[id]/_editor/virtual-timeline";
import type { DraftSlot } from "@/app/generative/timeline-math";

const durations = [3, 3, 2, 2, 3];
const slots: DraftSlot[] = durations.map((durationS, index) => ({
  key: `slot-${index}`,
  slotId: `slot-${index}`,
  clipIndex: index,
  inS: 0,
  durationBeats: null,
  durationS,
  removed: false,
  momentDescription: null,
}));
const clips = durations.map((duration_s, clip_index) => ({
  clip_index,
  duration_s,
  signed_url: null,
}));

export default function EditorTimelineFixture() {
  const [currentTimeS, setCurrentTimeS] = useState(0);
  const [carouselDurationS, setCarouselDurationS] = useState(8);
  const [past, setPast] = useState<number[]>([]);
  const timeline = useMemo(
    () => buildVirtualTimeline(slots, clips, [], { position: "middle", durationS: carouselDurationS }),
    [carouselDurationS],
  );

  return (
    <main className="h-screen bg-white p-6">
      <button type="button" disabled={past.length === 0} onClick={() => {
        const previous = past.at(-1);
        if (previous == null) return;
        setCarouselDurationS(previous);
        setPast((items) => items.slice(0, -1));
      }} className="mb-4 rounded border px-3 py-2">Undo</button>
      <div className="h-[320px] overflow-hidden rounded border border-zinc-200">
        <EditorTimelineBody
          durationS={13}
          timelineProjection={timeline}
          currentTimeS={currentTimeS}
          zoom={1}
          selection={null}
          onSelect={() => undefined}
          onClear={() => undefined}
          textBars={[]}
          visualBlocks={[]}
          slots={slots}
          grid={[]}
          clipsLoading={false}
          filmstripClips={clips}
          carouselBlock={{ id: "qa-carousel", effectLabel: "cover flow", durationS: carouselDurationS, position: "middle" }}
          onSelectCarousel={() => undefined}
          onRecordTimelineEdit={() => {
            setPast((items) => [...items, carouselDurationS]);
          }}
          onPreviewCarouselDuration={setCarouselDurationS}
          sfx={[]}
          hasMusic
          musicLabel="Continuous music"
          videoMuted={false}
          onToggleVideoMute={() => undefined}
          soundMuted={false}
          onToggleSoundMute={() => undefined}
          overlays={[]}
          onScrub={setCurrentTimeS}
          onScrubStart={() => undefined}
        />
      </div>
      <div
        id="qa-state"
        data-current-time={currentTimeS}
        data-total-duration={timeline.totalDurationS}
        data-carousel-duration={carouselDurationS}
        data-past-len={past.length}
        aria-hidden="true"
      />
    </main>
  );
}
