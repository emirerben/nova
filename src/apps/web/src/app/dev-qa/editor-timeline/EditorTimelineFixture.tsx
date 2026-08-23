"use client";

import { useEffect, useMemo, useState } from "react";
import EditorTimelineBody, {
  type EditorVisualBlockBar,
} from "@/app/plan/items/[id]/_editor/EditorTimelineBody";
import { buildVirtualTimeline } from "@/app/plan/items/[id]/_editor/virtual-timeline";
import { placeAfterSelected } from "@/app/plan/items/[id]/_editor/editor-bar-drag";
import type { EditorSelection } from "@/app/plan/items/[id]/_editor/useEditorSelection";
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

const initialMediaBlocks: EditorVisualBlockBar[] = [
  {
    id: "media-a",
    kind: "media",
    start_s: 1,
    end_s: 3,
    media_kind: "video",
    source_duration_s: 2,
    trim_start_s: 0,
    z: 1,
  },
  {
    id: "media-b",
    kind: "media",
    start_s: 2,
    end_s: 4,
    media_kind: "image",
    z: 2,
  },
];

interface FixtureSnapshot {
  carouselDurationS: number;
  mediaBlocks: EditorVisualBlockBar[];
}

interface ScenarioStyle {
  uploadsReady: boolean;
  displayMode: "fullscreen" | "overlay";
  fitMode: "contain" | "cover";
  zoom: number;
  focalX: number;
  focalY: number;
}

const DEFAULT_SCENARIO_STYLE: ScenarioStyle = {
  uploadsReady: false,
  displayMode: "overlay",
  fitMode: "contain",
  zoom: 1,
  focalX: 0.5,
  focalY: 0.5,
};

const SAVED_FIXTURE_KEY = "nova-editor-timeline-media-scenario";

export default function EditorTimelineFixture() {
  const [currentTimeS, setCurrentTimeS] = useState(0);
  const [carouselDurationS, setCarouselDurationS] = useState(8);
  const [mediaBlocks, setMediaBlocks] = useState(initialMediaBlocks);
  const [selection, setSelection] = useState<EditorSelection | null>(null);
  const [past, setPast] = useState<FixtureSnapshot[]>([]);
  const [scenarioStyle, setScenarioStyle] = useState(DEFAULT_SCENARIO_STYLE);
  const [reloadedSavedState, setReloadedSavedState] = useState(false);
  const timeline = useMemo(
    () => buildVirtualTimeline(slots, clips, [], { position: "middle", durationS: carouselDurationS }),
    [carouselDurationS],
  );

  const recordTimelineEdit = () => {
    setPast((items) => [
      ...items,
      {
        carouselDurationS,
        mediaBlocks: mediaBlocks.map((block) => ({ ...block })),
      },
    ]);
  };

  const updateMediaTiming = (
    id: string,
    patch: Pick<EditorVisualBlockBar, "start_s" | "end_s">,
  ) => {
    setMediaBlocks((blocks) =>
      blocks.map((block) => (block.id === id ? { ...block, ...patch } : block)),
    );
  };

  const placeSecondAfterFirst = () => {
    const first = mediaBlocks.find((block) => block.id === "media-a");
    const second = mediaBlocks.find((block) => block.id === "media-b");
    if (!first || !second) return;
    const next = placeAfterSelected({
      selected: first,
      durationS: second.end_s - second.start_s,
      videoDurationS: timeline.totalDurationS,
    });
    if (!next) return;
    recordTimelineEdit();
    updateMediaTiming(second.id, next);
  };

  useEffect(() => {
    const saved = window.localStorage.getItem(SAVED_FIXTURE_KEY);
    if (!saved) return;
    const parsed = JSON.parse(saved) as {
      mediaBlocks: EditorVisualBlockBar[];
      scenarioStyle: ScenarioStyle;
    };
    setMediaBlocks(parsed.mediaBlocks);
    setScenarioStyle(parsed.scenarioStyle);
    setReloadedSavedState(true);
  }, []);

  const uploadScenarioAssets = () => {
    setScenarioStyle((current) => ({ ...current, uploadsReady: true }));
  };

  const addUploadedImageFullscreen = () => {
    if (!scenarioStyle.uploadsReady) return;
    recordTimelineEdit();
    setMediaBlocks((blocks) =>
      blocks.map((block) =>
        block.id === "media-a"
          ? { ...block, media_kind: "image", start_s: 1, end_s: 3, source_duration_s: undefined }
          : block,
      ),
    );
    setScenarioStyle((current) => ({ ...current, displayMode: "fullscreen" }));
    setSelection({ kind: "visual", id: "media-a" });
  };

  const stackSecondImage = () => {
    if (mediaBlocks.some((block) => block.id === "media-c")) return;
    recordTimelineEdit();
    setMediaBlocks((blocks) => [
      ...blocks,
      { id: "media-c", kind: "media", start_s: 1.5, end_s: 2.5, media_kind: "image", z: 3 },
    ]);
  };

  const bringFirstToFront = () => {
    recordTimelineEdit();
    setMediaBlocks((blocks) => {
      const nextZ = Math.max(...blocks.map((block) => block.z ?? 0)) + 1;
      return blocks.map((block) => (block.id === "media-a" ? { ...block, z: nextZ } : block));
    });
  };

  const saveScenario = () => {
    window.localStorage.setItem(
      SAVED_FIXTURE_KEY,
      JSON.stringify({ mediaBlocks, scenarioStyle }),
    );
  };

  return (
    <main className="h-screen bg-white p-6">
      <button type="button" disabled={past.length === 0} onClick={() => {
        const previous = past.at(-1);
        if (previous == null) return;
        setCarouselDurationS(previous.carouselDurationS);
        setMediaBlocks(previous.mediaBlocks.map((block) => ({ ...block })));
        setPast((items) => items.slice(0, -1));
      }} className="mb-4 rounded border px-3 py-2">Undo</button>
      <div className="mb-4 flex items-center gap-2">
        <button
          type="button"
          onClick={placeSecondAfterFirst}
          className="rounded border px-3 py-2"
        >
          Place second media after first
        </button>
        <span data-testid="qa-media-row-order" className="text-xs text-zinc-500">
          z-order: {mediaBlocks.map((block) => block.id).join(",")}
        </span>
      </div>
      <section aria-label="Unified media scenario" className="mb-4 flex flex-wrap items-end gap-2">
        <button type="button" onClick={uploadScenarioAssets} className="rounded border px-3 py-2">
          Upload image and video
        </button>
        <button
          type="button"
          disabled={!scenarioStyle.uploadsReady}
          onClick={addUploadedImageFullscreen}
          className="rounded border px-3 py-2"
        >
          Add uploaded image full screen
        </button>
        <button
          type="button"
          onClick={() => setScenarioStyle((current) => ({ ...current, fitMode: current.fitMode === "contain" ? "cover" : "contain" }))}
          className="rounded border px-3 py-2"
        >
          Fit mode: {scenarioStyle.fitMode === "contain" ? "Fit" : "Fill"}
        </button>
        <label className="text-xs">
          Zoom
          <input
            aria-label="Media zoom"
            type="range"
            min="1"
            max="3"
            step="0.1"
            value={scenarioStyle.zoom}
            onChange={(event) => setScenarioStyle((current) => ({ ...current, zoom: Number(event.target.value) }))}
          />
        </label>
        <button
          type="button"
          onClick={() => setScenarioStyle((current) => ({ ...current, focalX: 0.8, focalY: 0.2 }))}
          className="rounded border px-3 py-2"
        >
          Reposition focal point
        </button>
        <button type="button" onClick={stackSecondImage} className="rounded border px-3 py-2">
          Stack another image
        </button>
        <button type="button" onClick={bringFirstToFront} className="rounded border px-3 py-2">
          Bring first media to front
        </button>
        <button type="button" onClick={saveScenario} className="rounded border px-3 py-2">
          Save media edit
        </button>
      </section>
      <div className="h-[320px] overflow-hidden rounded border border-zinc-200">
        <EditorTimelineBody
          durationS={13}
          timelineProjection={timeline}
          currentTimeS={currentTimeS}
          zoom={1}
          selection={selection}
          onSelect={(kind, id) => setSelection({ kind, id })}
          onClear={() => setSelection(null)}
          textBars={[]}
          visualBlocks={mediaBlocks}
          slots={slots}
          grid={[]}
          clipsLoading={false}
          filmstripClips={clips}
          carouselBlock={{ id: "qa-carousel", effectLabel: "cover flow", durationS: carouselDurationS, position: "middle" }}
          onSelectCarousel={() => undefined}
          onRecordTimelineEdit={recordTimelineEdit}
          onPreviewCarouselDuration={setCarouselDurationS}
          onPreviewVisualTiming={updateMediaTiming}
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
        data-media-past-len={past.length}
        data-media-first-start={mediaBlocks[0]?.start_s ?? ""}
        data-media-first-end={mediaBlocks[0]?.end_s ?? ""}
        data-media-second-start={mediaBlocks[1]?.start_s ?? ""}
        data-media-second-end={mediaBlocks[1]?.end_s ?? ""}
        data-uploads-ready={scenarioStyle.uploadsReady}
        data-display-mode={scenarioStyle.displayMode}
        data-fit-mode={scenarioStyle.fitMode}
        data-zoom={scenarioStyle.zoom}
        data-focal-x={scenarioStyle.focalX}
        data-focal-y={scenarioStyle.focalY}
        data-media-count={mediaBlocks.length}
        data-media-first-z={mediaBlocks.find((block) => block.id === "media-a")?.z ?? ""}
        data-reloaded-saved-state={reloadedSavedState}
        aria-hidden="true"
      />
    </main>
  );
}
