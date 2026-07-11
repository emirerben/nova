"use client";

import { useState } from "react";
import OverlayLane from "@/app/plan/_components/OverlayLane";
import type { MediaOverlay } from "@/lib/plan-api";

type PatchLogEntry = {
  id: string;
  patch: Partial<MediaOverlay>;
  record: boolean | null;
};

const INITIAL_CARDS: MediaOverlay[] = [
  {
    id: "big-card",
    kind: "image",
    src_gcs_path: "fixtures/big.png",
    preview_url: null,
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    display_mode: "pip",
    start_s: 0.5,
    end_s: 2.5,
    z: 0,
  },
  {
    id: "tiny-card",
    kind: "image",
    src_gcs_path: "fixtures/tiny.png",
    preview_url: null,
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.2,
    display_mode: "fullscreen",
    start_s: 3.2,
    end_s: 3.7,
    z: 1,
  },
  {
    id: "video-card",
    kind: "video",
    src_gcs_path: "fixtures/video.mp4",
    preview_url: null,
    position: "bottom",
    x_frac: 0.72,
    y_frac: 0.7,
    scale: 0.3,
    display_mode: "pip",
    start_s: 5,
    end_s: 7,
    clip_duration_s: 5,
    clip_trim_start_s: 0.5,
    clip_trim_end_s: 2.5,
    z: 2,
  },
];

export default function OverlaysFixture() {
  const [cards, setCards] = useState<MediaOverlay[]>(INITIAL_CARDS);
  const [patchLog, setPatchLog] = useState<PatchLogEntry[]>([]);

  function updateCard(
    id: string,
    patch: Partial<MediaOverlay>,
    options?: { record?: boolean },
  ) {
    setPatchLog((log) => [
      ...log,
      { id, patch, record: options?.record ?? null },
    ]);
    setCards((current) =>
      current.map((card) => (card.id === id ? { ...card, ...patch } : card)),
    );
  }

  return (
    <main className="min-h-screen bg-[#fafaf8] px-4 py-6 text-[#0c0c0e]">
      <div className="mx-auto max-w-[760px] rounded bg-[#18181b] py-4">
        <OverlayLane
          totalDurationS={10}
          currentTimeS={0}
          overlayCards={cards}
          overlaysEnabled={true}
          overlayUploading={false}
          localPreviewUrls={{}}
          onOverlayUploadRequest={() => undefined}
          onUpdateCard={updateCard}
          onRemoveCard={(id) => {
            setCards((current) => current.filter((card) => card.id !== id));
          }}
          onClearOverlays={() => setCards([])}
        />
        <div
          id="qa-state"
          data-cards={JSON.stringify(cards)}
          data-patch-log={JSON.stringify(patchLog)}
          aria-hidden="true"
        />
      </div>
      <div className="min-h-[200vh]" aria-hidden="true" />
    </main>
  );
}
