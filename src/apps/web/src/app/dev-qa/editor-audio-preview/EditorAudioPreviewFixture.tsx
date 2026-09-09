"use client";

import { useMemo, useRef, useState } from "react";

import type { DraftSlot } from "@/app/generative/timeline-math";
import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import { resolveVirtualPreviewAudio } from "@/app/plan/items/[id]/_editor/preview-audio";
import { useVirtualPreview } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { LookPreset, TimelineClip } from "@/lib/generative-api";
import type { PlanItemVariant } from "@/lib/plan-api";

type AudioScenario = "uploaded" | "music" | "narration" | "native";

const CLIP_URL = "/landing/raw-story/istanbul.mp4";
const EXTERNAL_AUDIO_URL = "/landing/raw-story/travel-reference-audio.m4a";
const RENDERED_AUDIO_URL = "/landing/raw-story/travel-render.mp4";

const variant = {
  variant_id: "dev-qa-editor-audio-preview",
  output_url: null,
  base_video_url: null,
  render_status: "draft",
  text_mode: "none",
  resolved_archetype: null,
  captions_enabled: false,
} as unknown as PlanItemVariant;

const slots: DraftSlot[] = [
  {
    key: "audio-preview-slot",
    slotId: "audio-preview-slot",
    clipIndex: 0,
    inS: 0,
    durationBeats: null,
    durationS: 5.5,
    removed: false,
    layout: "fullscreen",
    momentDescription: null,
  },
];

const clips: Pick<TimelineClip, "clip_index" | "signed_url" | "kind">[] = [
  { clip_index: 0, signed_url: CLIP_URL, kind: "video" },
];

function scenarioInputs(scenario: AudioScenario) {
  return {
    virtualPreviewRequested: true,
    clipDirty: false,
    musicDirty: false,
    backgroundMusicDirty: false,
    musicTrackActive: scenario === "music",
    musicAudioUrl: scenario === "music" ? EXTERNAL_AUDIO_URL : null,
    musicStartS: 0,
    sourceAudioMix: scenario === "uploaded" ? "uploaded" : "interleaved",
    sourceAudioOptions: [
      { mix: "uploaded", audio_url: EXTERNAL_AUDIO_URL },
      { mix: "interleaved", audio_url: RENDERED_AUDIO_URL },
    ],
    baseVideoUrl: RENDERED_AUDIO_URL,
    narrationApplied: scenario === "narration",
    videoMuted: false,
    soundMuted: false,
  };
}

export default function EditorAudioPreviewFixture() {
  const [scenario, setScenario] = useState<AudioScenario>("uploaded");
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(5.5);
  const [playing, setPlaying] = useState(false);
  const [visualEditCount, setVisualEditCount] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const authority = useMemo(
    () => resolveVirtualPreviewAudio(scenarioInputs(scenario)),
    [scenario],
  );
  const lookPreset: LookPreset = visualEditCount % 2 === 0 ? "none" : "golden_hour";
  const deckLooks = useMemo(
    () => ({ a: lookPreset, b: lookPreset }),
    [lookPreset],
  );
  const preview = useVirtualPreview({
    enabled: true,
    slots,
    baselineSlots: slots,
    clips,
    grid: [],
    currentTime,
    muted: authority.muted,
    musicAudioUrl: authority.url,
    musicStartS: authority.startS,
    soundMuted: authority.muted,
    musicTrackActive: authority.active,
    onTimeUpdate: setCurrentTime,
    onDuration: setDuration,
    onPlayingChange: setPlaying,
    onSourceError: () => setPlaying(false),
    onMusicError: () => setPlaying(false),
  });

  const chooseScenario = (next: AudioScenario) => {
    preview.pause();
    setCurrentTime(0);
    setScenario(next);
  };

  return (
    <main className="min-h-screen bg-zinc-100 p-6 text-zinc-950">
      <div className="mx-auto flex max-w-5xl flex-col gap-4 lg:flex-row">
        <section className="flex min-h-[700px] min-w-0 flex-1 items-center justify-center overflow-hidden rounded-2xl border border-zinc-200 bg-white p-6">
          <EditorCanvas
            variant={variant}
            elements={[]}
            bars={[]}
            selectedTextId={null}
            currentTime={currentTime}
            lookPreset={lookPreset}
            virtualDeckLookPresets={deckLooks}
            playing={playing}
            masonryDurationS={duration}
            zoomPct={100}
            tool="select"
            videoRef={videoRef}
            onSelectText={() => undefined}
            onClearSelection={() => undefined}
            onPatchBar={() => undefined}
            onFocusContent={() => undefined}
            onTimeUpdate={setCurrentTime}
            onDuration={setDuration}
            onPlayingChange={setPlaying}
            virtualPreview={preview}
          />
        </section>

        <aside className="w-full rounded-2xl border border-zinc-200 bg-white p-5 lg:w-80">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-zinc-500">
            Local audio parity QA
          </p>
          <h1 className="mt-2 text-xl font-semibold">Audio survives visual edits</h1>
          <p className="mt-2 text-sm leading-6 text-zinc-600">
            Start playback, then apply visual edits. The external audio should continue while the source clip stays muted.
          </p>

          <div className="mt-5 grid grid-cols-2 gap-2">
            {(["uploaded", "music", "narration", "native"] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => chooseScenario(item)}
                className={`rounded-lg border px-3 py-2 text-sm font-medium capitalize ${
                  scenario === item
                    ? "border-lime-500 bg-lime-50"
                    : "border-zinc-200 bg-white"
                }`}
              >
                {item}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={preview.toggle}
            className="mt-4 w-full rounded-lg bg-zinc-950 px-4 py-3 text-sm font-semibold text-white"
          >
            {playing ? "Pause" : "Play"}
          </button>
          <button
            type="button"
            onClick={() => setVisualEditCount((count) => count + 1)}
            className="mt-2 w-full rounded-lg border border-zinc-300 px-4 py-3 text-sm font-semibold"
          >
            Apply visual edit
          </button>

          <dl className="mt-5 grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-zinc-500">Authority</dt>
            <dd data-testid="audio-authority" className="text-right font-medium">{authority.kind}</dd>
            <dt className="text-zinc-500">Playing</dt>
            <dd data-testid="audio-playing" className="text-right font-medium">{playing ? "yes" : "no"}</dd>
            <dt className="text-zinc-500">Time</dt>
            <dd data-testid="audio-time" className="text-right font-mono">{currentTime.toFixed(1)}s</dd>
            <dt className="text-zinc-500">Visual edits</dt>
            <dd data-testid="visual-edit-count" className="text-right font-medium">{visualEditCount}</dd>
          </dl>
          <p className="mt-4 rounded-lg bg-zinc-100 p-3 text-xs leading-5 text-zinc-600">
            “Native” is the comparison case and intentionally uses the clip’s original audio. The other modes must use their selected external bed.
          </p>
        </aside>
      </div>
    </main>
  );
}
