/**
 * Live preview edit mode on the plan-item hero (plan/items/[id]/page.tsx).
 *
 * When a variant carries a signed overlay-clean base (`pre_overlay_video_url`,
 * captured by the backend before the first card burn) AND overlay cards exist,
 * the hero plays that CLEAN base and ALL cards render as a live CSS layer
 * (LiveOverlayCardsLayer) — so timeline lane edits (scale / position / window /
 * trim / remove) preview in real time. The FFmpeg bake still fires only on
 * Download (render:false autosaves untouched).
 *
 * Covers:
 *  - live mode ON: hero src === pre_overlay_video_url; cards render with
 *    overlayCardStyle positioning; [start_s, end_s] time-gating follows the
 *    hero playhead.
 *  - live mode OFF (no pre_overlay_video_url): hero keeps output_url and only
 *    blob-URL (freshly uploaded) cards render — regression guard against
 *    doubling baked pixels.
 *  - re-burn in-flight (render_status "rendering"): the source is NOT switched
 *    mid-burn.
 *  - the layer itself fires no network (no overlay save / render calls).
 *  - LiveOverlayCardsLayer prop changes (scale/position/window) re-style the
 *    cards immediately.
 *  - video cards seek to trim offset, follow the hero clock within the drift
 *    tolerance, and freeze at the trim end (tpad-style).
 */

// @ts-nocheck

import React from "react";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

// jsdom doesn't implement media playback — stub the methods the sync code calls.
beforeAll(() => {
  window.HTMLMediaElement.prototype.load = jest.fn();
  window.HTMLMediaElement.prototype.pause = jest.fn();
  window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
});

import { act, fireEvent, render } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useSearchParams: jest.fn(() => new URLSearchParams()),
}));

const mockRefetch = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: jest.fn(),
}));
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<
  typeof usePolledJobStatus
>;

// Network spies: the live layer must be pure CSS — rendering / seeking it must
// never save overlays or dispatch a render.
const mockSetVariantMediaOverlays = jest.fn().mockResolvedValue({});
const mockRenderVariantSfx = jest.fn().mockResolvedValue({});
const mockSetVariantSoundEffects = jest.fn().mockResolvedValue({});
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  requestUploadUrls: jest.fn(),
  attachClips: jest.fn(),
  generatePlanItem: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn(),
  uploadToGcs: jest.fn(),
  listPoolAssets: jest.fn().mockResolvedValue([]),
  getSfxAudioUrl: jest.fn().mockResolvedValue("https://signed/sfx.mp3"),
  setVariantMediaOverlays: (...a: unknown[]) => mockSetVariantMediaOverlays(...a),
  renderVariantSfx: (...a: unknown[]) => mockRenderVariantSfx(...a),
  setVariantSoundEffects: (...a: unknown[]) => mockSetVariantSoundEffects(...a),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
}));

jest.mock("@/lib/sfx-api", () => ({
  getSoundEffects: jest.fn().mockResolvedValue([]),
}));

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {
    status = 0;
    code: string | null = null;
  },
  GENERATIVE_TERMINAL_STATUSES: [
    "variants_ready",
    "variants_ready_partial",
    "variants_failed",
    "processing_failed",
  ],
}));

jest.mock("@/lib/music-api", () => ({
  getMusicTracks: jest.fn().mockResolvedValue({ tracks: [] }),
}));

jest.mock("@/lib/font-faces", () => ({ FONT_FACES: "" }));
jest.mock("@/lib/download-video", () => ({ downloadVideo: jest.fn() }));
jest.mock("@/lib/plan-text", () => ({ stripRationalePrefix: (s: string) => s }));
jest.mock("@/components/ui/LightShell", () => ({
  LightShell: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="light-shell">{children}</div>
  ),
}));
jest.mock("@/app/plan/_components/PlanFilmstrip", () => ({
  __esModule: true,
  default: () => <div data-testid="plan-filmstrip" />,
}));
jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));
jest.mock("@/app/library/_components/FeedbackButtons", () => ({
  __esModule: true,
  default: () => <div data-testid="feedback-buttons" />,
}));

import PlanItemPage from "@/app/plan/items/[id]/page";
import LiveOverlayCardsLayer from "@/app/plan/_components/LiveOverlayCardsLayer";
import { getSfxAudioUrl } from "@/lib/plan-api";
import { getSoundEffects } from "@/lib/sfx-api";
import type { MediaOverlay } from "@/lib/plan-api";

// ===== Factories =====

const PRE_OVERLAY_URL = "https://cdn/pre_overlay.mp4?sig=pre";
const OUTPUT_URL = "https://cdn/out.mp4?sig=out";

function makeItem(overrides = {}) {
  return {
    id: "test-item-id",
    day_index: 3,
    theme: "Morning Routine",
    idea: "Film your morning",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
    status: "ready",
    current_job_id: "job-1",
    user_edited: false,
    instruction_level: "full",
    conformance: null,
    ...overrides,
  };
}

function makeJob(variants) {
  return {
    status: "variants_ready",
    variants,
    current_phase: null,
    phase_log: null,
    started_at: "2026-06-06T10:00:00Z",
    finished_at: "2026-06-06T10:02:00Z",
    expected_phase_durations: null,
    created_at: "2026-06-06T10:00:00Z",
  };
}

function makeCard(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "card-a",
    kind: "image",
    src_gcs_path: "users/u1/plan/item1/overlays/a.png",
    preview_url: "https://signed/a.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 0,
    end_s: 5,
    z: 0,
    ...overrides,
  };
}

// NOT instant-edit-eligible (no base_video_url) → the hero renders through
// Hero, the component that owns live-edit mode.
function makeVariant(overrides = {}) {
  return {
    variant_id: "v1",
    output_url: OUTPUT_URL,
    render_status: "ready",
    text_mode: "agent_text",
    music_track_id: null,
    track_title: null,
    style_set_id: null,
    intro_text_size_px: null,
    intro_size_source: null,
    render_finished_at: "2026-06-06T10:02:00Z",
    error_class: null,
    ...overrides,
  };
}

function setData(variants) {
  mockUsePolledJobStatus.mockReturnValue({
    data: { item: makeItem(), job: makeJob(variants) },
    error: null,
    refetch: mockRefetch,
  });
}

function heroVideo(): HTMLVideoElement {
  const el = document.querySelector<HTMLVideoElement>(
    '[data-variant-preview] video[controls]',
  );
  if (!el) throw new Error("Hero video not found");
  return el;
}

/** Drive the hero playhead: set currentTime and fire timeupdate. */
async function seekHero(video: HTMLVideoElement, t: number) {
  Object.defineProperty(video, "currentTime", {
    value: t,
    configurable: true,
    writable: true,
  });
  await act(async () => {
    fireEvent(video, new Event("timeupdate"));
  });
}

beforeEach(() => {
  mockSetVariantMediaOverlays.mockClear();
  mockRenderVariantSfx.mockClear();
  mockSetVariantSoundEffects.mockClear();
  (getSfxAudioUrl as jest.Mock).mockClear();
  (getSoundEffects as jest.Mock).mockClear();
});

// ===== Page-level: hero source switching + time-gated live layer =====

describe("Plan item hero — live preview edit mode", () => {
  const cardA = makeCard({ id: "card-a", start_s: 0, end_s: 5 });
  const cardB = makeCard({
    id: "card-b",
    start_s: 5,
    end_s: 8,
    preview_url: "https://signed/b.png",
    position: "top",
    x_frac: 0.3,
    y_frac: 0.2,
    scale: 0.5,
  });
  const liveVariant = makeVariant({
    media_overlays: [cardA, cardB],
    pre_media_overlay_video_path: "generative-jobs/j1/v1_pre_overlay.mp4",
    pre_overlay_video_url: PRE_OVERLAY_URL,
  });

  it("live mode ON: hero plays pre_overlay_video_url and renders applied cards with overlayCardStyle", async () => {
    setData([liveVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    // The hero <video> plays the overlay-CLEAN base, not the burned output.
    expect(heroVideo()).toHaveAttribute("src", PRE_OVERLAY_URL);

    // Card A (window [0,5]) is visible at t=0, sourced from the server-signed
    // preview_url (no blob upload happened this session).
    const wrapA = document.querySelector('[data-overlay-card="card-a"]');
    expect(wrapA).not.toBeNull();
    expect(wrapA).toHaveStyle({
      position: "absolute",
      left: "50%",
      top: "50%",
      width: "35%",
      pointerEvents: "none",
    });
    expect(wrapA!.querySelector("img")).toHaveAttribute("src", "https://signed/a.png");

    // Card B (window [5,8]) is time-gated out at t=0.
    expect(document.querySelector('[data-overlay-card="card-b"]')).toBeNull();
  });

  it("time-gate follows the hero playhead: cards show/hide on timeupdate", async () => {
    setData([liveVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    await seekHero(heroVideo(), 6);

    // t=6: card A ([0,5]) hides, card B ([5,8]) shows at ITS position/scale.
    expect(document.querySelector('[data-overlay-card="card-a"]')).toBeNull();
    const wrapB = document.querySelector('[data-overlay-card="card-b"]');
    expect(wrapB).not.toBeNull();
    expect(wrapB).toHaveStyle({ left: "30%", top: "20%", width: "50%" });
  });

  it("the live layer fires no network: no overlay save / render calls from rendering or seeking", async () => {
    setData([liveVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });
    await seekHero(heroVideo(), 6);
    await seekHero(heroVideo(), 2);

    // render:false autosave only fires on ACTUAL lane edits — never from the
    // preview layer itself.
    expect(mockSetVariantMediaOverlays).not.toHaveBeenCalled();
    expect(mockRenderVariantSfx).not.toHaveBeenCalled();
    expect(mockSetVariantSoundEffects).not.toHaveBeenCalled();
  });

  it("live mode OFF (no pre_overlay_video_url): hero keeps output_url and applied cards do NOT double over baked pixels", async () => {
    // Cards persisted but never burned — the API signs preview_url anyway, but
    // without a pre-overlay base the hero must keep the burned/original output
    // and render only blob-URL (freshly uploaded) cards. There are none here.
    setData([makeVariant({ media_overlays: [cardA, cardB] })]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(heroVideo()).toHaveAttribute("src", OUTPUT_URL);
    expect(document.querySelector("[data-overlay-card]")).toBeNull();
  });

  it("re-burn in-flight: the source is not switched mid-burn", async () => {
    // The burn just started (render_status "rendering") and the poll now
    // carries the freshly-captured pre-overlay base. Entering live mode NOW
    // would flip the hero source mid-burn — it must keep today's behavior
    // (burned output + shimmer/lock) until the burn completes.
    setData([{ ...liveVariant, render_status: "rendering" }]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(heroVideo()).toHaveAttribute("src", OUTPUT_URL);
    // Shimmer/lock overlay unchanged.
    expect(
      document.querySelector('[aria-label="Rendering new version"]'),
    ).not.toBeNull();
  });
});

// ===== Applied SFX — audible hero preview wiring (no editor tab open) =====
// Regression for the live-edit slice: the sfxAudioUrls signing effect used to
// live in FocusedVariantControls, which only mounts when an editor tab is
// open — so APPLIED placements loaded from the variant were silent on the
// hero until the user opened the Timeline tab. It now lives in FocusedResults.

describe("Plan item hero — applied SFX placements get playable URLs on mount", () => {
  it("signs user-uploaded SFX audio without opening any editor tab", async () => {
    const placement = {
      id: "p1",
      src_gcs_path: "users/u1/plan/item1/sfx/boom.mp3",
      at_s: 2,
      gain: 1,
      duration_s: 1.2,
      label: "boom",
    };
    setData([makeVariant({ sound_effects: [placement] })]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(getSfxAudioUrl).toHaveBeenCalledWith(
      "test-item-id",
      "users/u1/plan/item1/sfx/boom.mp3",
    );
  });

  it("loads the glossary on mount when an applied glossary placement needs its preview URL", async () => {
    (getSoundEffects as jest.Mock).mockResolvedValueOnce([
      { id: "fah-id", name: "Fah", preview_audio_url: "https://glossary/fah.mp3" },
    ]);
    const placement = {
      id: "p2",
      sound_effect_id: "fah-id",
      src_gcs_path: "sound-effects/fah/audio.mp3",
      at_s: 1,
      gain: 1,
      duration_s: 2.04,
      label: "Fah",
    };
    setData([makeVariant({ sound_effects: [placement] })]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(getSoundEffects).toHaveBeenCalled();
  });

  it("stays lazy: no glossary fetch when the variant has no glossary placements", async () => {
    setData([makeVariant()]);
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(getSoundEffects).not.toHaveBeenCalled();
  });
});

// ===== Component-level: LiveOverlayCardsLayer re-styling + video card sync =====

describe("LiveOverlayCardsLayer — edits re-style cards immediately", () => {
  const mainRef = { current: null };

  it("scale / position / window changes in the cards prop update the rendered layer", () => {
    const card = makeCard({ id: "c1", scale: 0.35, x_frac: 0.5, y_frac: 0.5 });
    const { rerender } = render(
      <LiveOverlayCardsLayer
        cards={[card]}
        resolveCardSrc={(c) => c.preview_url ?? undefined}
        videoTimeS={1}
        timeGate
        mainVideoRef={mainRef}
      />,
    );
    expect(document.querySelector('[data-overlay-card="c1"]')).toHaveStyle({
      left: "50%",
      top: "50%",
      width: "35%",
    });

    // Scale slider + position preset edit (same overlayCards mutation the
    // lanes produce via onUpdateCard).
    rerender(
      <LiveOverlayCardsLayer
        cards={[{ ...card, scale: 0.6, x_frac: 0.5, y_frac: 0.18 }]}
        resolveCardSrc={(c) => c.preview_url ?? undefined}
        videoTimeS={1}
        timeGate
        mainVideoRef={mainRef}
      />,
    );
    expect(document.querySelector('[data-overlay-card="c1"]')).toHaveStyle({
      left: "50%",
      top: "18%",
      width: "60%",
    });

    // Window drag: moving the card window past the playhead hides it.
    rerender(
      <LiveOverlayCardsLayer
        cards={[{ ...card, start_s: 4, end_s: 9 }]}
        resolveCardSrc={(c) => c.preview_url ?? undefined}
        videoTimeS={1}
        timeGate
        mainVideoRef={mainRef}
      />,
    );
    expect(document.querySelector('[data-overlay-card="c1"]')).toBeNull();

    // Remove (card gone from state) unmounts it.
    rerender(
      <LiveOverlayCardsLayer
        cards={[]}
        resolveCardSrc={(c) => c.preview_url ?? undefined}
        videoTimeS={1}
        timeGate
        mainVideoRef={mainRef}
      />,
    );
    expect(document.querySelector("[data-overlay-card]")).toBeNull();
  });

  it("cards without a resolvable src are skipped", () => {
    render(
      <LiveOverlayCardsLayer
        cards={[makeCard({ id: "c2", preview_url: null })]}
        resolveCardSrc={() => undefined}
        videoTimeS={1}
        timeGate
        mainVideoRef={mainRef}
      />,
    );
    expect(document.querySelector("[data-overlay-card]")).toBeNull();
  });
});

describe("LiveOverlayCardsLayer — video card follows the hero clock", () => {
  function makeMainVideo(t: number, paused = false): HTMLVideoElement {
    const main = document.createElement("video");
    Object.defineProperty(main, "currentTime", {
      value: t,
      configurable: true,
      writable: true,
    });
    Object.defineProperty(main, "paused", {
      value: paused,
      configurable: true,
      writable: true,
    });
    return main;
  }

  it("seeks to trim offset + hero delta, tolerates small drift, freezes at trim end", async () => {
    // Card visible from 3s on the timeline; its clip is trimmed to [2s, 5s].
    const card = makeCard({
      id: "vid1",
      kind: "video",
      start_s: 3,
      end_s: 12,
      clip_trim_start_s: 2,
      clip_trim_end_s: 5,
      clip_duration_s: 9,
      preview_url: "https://signed/clip.mp4",
    });
    const main = makeMainVideo(4); // hero at 4s, playing
    const mainRef = { current: main };

    render(
      <LiveOverlayCardsLayer
        cards={[card]}
        resolveCardSrc={(c) => c.preview_url ?? undefined}
        videoTimeS={4}
        timeGate
        mainVideoRef={mainRef}
      />,
    );

    const cardVideo = document.querySelector<HTMLVideoElement>(
      '[data-overlay-card="vid1"] video',
    )!;
    // Mount sync: trimStart(2) + (heroTime(4) - cardStart(3)) = 3.
    expect(cardVideo.currentTime).toBe(3);

    const play = jest.fn().mockResolvedValue(undefined);
    const pause = jest.fn();
    cardVideo.play = play;
    cardVideo.pause = pause;

    // Small drift (< 0.15s) — no corrective seek, playback keeps following.
    main.currentTime = 4.05;
    await act(async () => {
      main.dispatchEvent(new Event("timeupdate"));
    });
    expect(cardVideo.currentTime).toBe(3); // 3.05 target, drift 0.05 → no seek
    expect(play).toHaveBeenCalled(); // main playing → card plays in lockstep

    // Past the clip trim end: seek clamps to trimEnd and the card FREEZES
    // (pause) — mimicking the render's tpad hold, no seek-thrash loop.
    Object.defineProperty(cardVideo, "paused", {
      value: false,
      configurable: true,
      writable: true,
    });
    main.currentTime = 10; // cardTime = 2 + 7 = 9 → capped at trimEnd 5
    await act(async () => {
      main.dispatchEvent(new Event("timeupdate"));
    });
    expect(cardVideo.currentTime).toBe(5);
    expect(pause).toHaveBeenCalled();
  });

  it("pauses the card when the hero pauses", async () => {
    const card = makeCard({
      id: "vid2",
      kind: "video",
      start_s: 0,
      end_s: 10,
      clip_trim_start_s: 0,
      clip_trim_end_s: 8,
      clip_duration_s: 8,
      preview_url: "https://signed/clip2.mp4",
    });
    const main = makeMainVideo(2);
    const mainRef = { current: main };
    render(
      <LiveOverlayCardsLayer
        cards={[card]}
        resolveCardSrc={(c) => c.preview_url ?? undefined}
        videoTimeS={2}
        timeGate
        mainVideoRef={mainRef}
      />,
    );

    const cardVideo = document.querySelector<HTMLVideoElement>(
      '[data-overlay-card="vid2"] video',
    )!;
    const pause = jest.fn();
    cardVideo.pause = pause;
    Object.defineProperty(cardVideo, "paused", {
      value: false,
      configurable: true,
      writable: true,
    });

    main.paused = true;
    await act(async () => {
      main.dispatchEvent(new Event("pause"));
    });
    expect(pause).toHaveBeenCalled();
  });
});
