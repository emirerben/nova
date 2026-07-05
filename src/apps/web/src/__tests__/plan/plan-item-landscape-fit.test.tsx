/**
 * T-LANDSCAPE-2: Jest coverage for the Fit/Fill landscape-clip toggle.
 *
 * The interactive toggle appears pre-render (variants.length === 0) and
 * disappears post-render, replaced by a read-only applied-fit display
 * (T-LANDSCAPE-1). This suite covers both states plus active-state
 * styling, click dispatch, and the hidden-while-generating guard.
 *
 * Note on button accessible names: each Fit/Fill button renders two child
 * spans ("Fit" + desc). The ARIA accessible name concatenates them without
 * a separator, yielding e.g. "FitKeep horizontal, black bars top & bottom".
 * Queries use /^Fit/i (prefix match) — not /^Fit$/i — to accommodate this.
 */

// @ts-nocheck

// jsdom lacks ResizeObserver (used internally by some child components).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

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

import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "item-lf-1" })),
}));

const mockRefetch = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: jest.fn(),
}));
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<
  typeof usePolledJobStatus
>;

const mockUpdatePlanItem = jest.fn().mockResolvedValue({});
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  requestUploadUrls: jest.fn(),
  attachClips: jest.fn(),
  generatePlanItem: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn().mockResolvedValue({}),
  changePlanItemStyle: jest.fn().mockResolvedValue({}),
  setPlanItemIntroSize: jest.fn().mockResolvedValue({}),
  editPlanItemVariant: jest.fn().mockResolvedValue({}),
  uploadToGcs: jest.fn(),
  updatePlanItem: (...args: unknown[]) => mockUpdatePlanItem(...args),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
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

jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));

jest.mock("@/app/library/_components/FeedbackButtons", () => ({
  __esModule: true,
  default: () => <div data-testid="feedback-buttons" />,
}));

jest.mock("@/components/variant-editor/IntroTextPreview", () => ({
  IntroTextPreview: () => <div data-testid="intro-text-preview" />,
}));

import PlanItemPage from "@/app/plan/items/[id]/page";

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeItem(overrides: Record<string, unknown> = {}) {
  return {
    id: "item-lf-1",
    day_index: 1,
    theme: "Sunrise shoot",
    idea: "Film the sunrise from the hill",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status: "draft",
    current_job_id: null,
    user_edited: false,
    instruction_level: "full",
    conformance: null,
    landscape_fit: "fit" as const,
    notes: null,
    source_idea_seed_text: null,
    clip_assignments: [],
    ...overrides,
  };
}

const sampleVariant = {
  variant_id: "original_text",
  output_url: "https://cdn/out.mp4",
  render_status: "ready" as const,
  text_mode: "agent_text",
  music_track_id: null,
  track_title: null,
  style_set_id: null,
  intro_text_size_px: 56,
  intro_size_source: "computed",
  intro_text: "A new dawn",
  intro_layout: "linear",
  base_video_url: "https://cdn/base.mp4",
  base_video_path: "generative-jobs/j/base.mp4",
};

/**
 * Wire up the mock. Pass `variants` to simulate a post-render job.
 * `job: null` is used for pre-render tests to keep the setup minimal.
 */
function setData(
  item: ReturnType<typeof makeItem>,
  variants: unknown[] = [],
) {
  const job =
    variants.length > 0
      ? {
          status: "variants_ready",
          variants,
          current_phase: null,
          phase_log: null,
          started_at: "2026-06-26T09:00:00Z",
          finished_at: "2026-06-26T09:02:00Z",
          expected_phase_durations: null,
          created_at: "2026-06-26T09:00:00Z",
        }
      : null;

  mockUsePolledJobStatus.mockReturnValue({
    data: { item, job },
    error: null,
    refetch: mockRefetch,
  });
}

beforeEach(() => {
  mockUpdatePlanItem.mockClear();
  mockRefetch.mockClear();
});

// ── Landscape-clip detection helpers ────────────────────────────────────────
// detectLandscapeClip() (page.tsx) creates a detached <video>, sets .src, and
// resolves off .onloadedmetadata/.onerror. jsdom doesn't decode real video, so
// we stub document.createElement("video") to synchronously fire the callback
// the component assigned, using a queued width/height per created element.

let nextVideoDims: Array<{ width: number; height: number } | "error"> = [];

function mockNextUpload(dims: Array<{ width: number; height: number } | "error">) {
  nextVideoDims = [...dims];
}

beforeAll(() => {
  if (!("createObjectURL" in URL)) {
    Object.defineProperty(URL, "createObjectURL", {
      value: jest.fn(() => "blob:mock"),
      writable: true,
    });
  }
  if (!("revokeObjectURL" in URL)) {
    Object.defineProperty(URL, "revokeObjectURL", { value: jest.fn(), writable: true });
  }
  const realCreateElement = document.createElement.bind(document);
  jest.spyOn(document, "createElement").mockImplementation((tag: string, ...rest: unknown[]) => {
    if (tag !== "video") return realCreateElement(tag, ...(rest as []));
    const dims = nextVideoDims.shift();
    const el = realCreateElement("video") as HTMLVideoElement;
    Object.defineProperty(el, "src", {
      set() {
        // Fire on next tick, mirroring real async metadata loading.
        setTimeout(() => {
          if (dims === "error") {
            el.onerror?.(new Event("error"));
          } else if (dims) {
            Object.defineProperty(el, "videoWidth", { value: dims.width, configurable: true });
            Object.defineProperty(el, "videoHeight", { value: dims.height, configurable: true });
            el.onloadedmetadata?.(new Event("loadedmetadata"));
          }
        }, 0);
      },
      get() {
        return "";
      },
    });
    return el;
  });
});

function uploadFile() {
  const input = screen.getByLabelText("Upload video clips for this idea") as HTMLInputElement;
  const file = new File(["x"], "clip.mp4", { type: "video/mp4" });
  Object.defineProperty(input, "files", { value: [file] });
  fireEvent.change(input);
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("Fit/Fill landscape-clip toggle", () => {
  // Shared setup: render with an "existing_footage" item (reaches PoolUploadCard,
  // not ShotSlotUploader — see detectLandscapeClip's doc comment for the scope),
  // then upload a landscape clip so hasLandscapeClip flips true.
  async function renderWithLandscapeDetected(overrides: Record<string, unknown> = {}) {
    setData(makeItem({ status: "draft", landscape_fit: "fit", content_mode: "existing_footage", ...overrides }));
    await act(async () => {
      render(<PlanItemPage />);
    });
    mockNextUpload([{ width: 1920, height: 1080 }]);
    await act(async () => {
      uploadFile();
      await new Promise((r) => setTimeout(r, 10));
    });
  }

  it("0. pre-render, no landscape clip detected: Fit/Fill toggle is hidden", async () => {
    setData(makeItem({ status: "draft", landscape_fit: "fit" }));
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByRole("button", { name: /^Fit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Fill/i })).toBeNull();
  });

  it("0b. uploading a portrait clip does not reveal the toggle", async () => {
    setData(makeItem({ status: "draft", landscape_fit: "fit", content_mode: "existing_footage" }));
    await act(async () => {
      render(<PlanItemPage />);
    });
    mockNextUpload([{ width: 1080, height: 1920 }]);
    await act(async () => {
      uploadFile();
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(screen.queryByRole("button", { name: /^Fit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Fill/i })).toBeNull();
  });

  it("0c. metadata-load failure fails safe — toggle stays hidden, no crash", async () => {
    setData(makeItem({ status: "draft", landscape_fit: "fit", content_mode: "existing_footage" }));
    await act(async () => {
      render(<PlanItemPage />);
    });
    mockNextUpload(["error"]);
    await act(async () => {
      uploadFile();
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(screen.queryByRole("button", { name: /^Fit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Fill/i })).toBeNull();
  });

  it("1. pre-render: shows both Fit and Fill buttons once a landscape clip is detected", async () => {
    await renderWithLandscapeDetected();

    // Accessible name = label + desc concatenated: "FitKeep horizontal…"
    // Use prefix match so the description text doesn't break the query.
    expect(screen.getByRole("button", { name: /^Fit/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Fill/i })).toBeInTheDocument();
  });

  it("2a. click dispatches updatePlanItem with the new value", async () => {
    await renderWithLandscapeDetected();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Fill/i }));
    });

    expect(mockUpdatePlanItem).toHaveBeenCalledWith("item-lf-1", {
      landscape_fit: "fill",
    });
  });

  it("2b. clicking the already-active option is a no-op", async () => {
    await renderWithLandscapeDetected();

    // "Fit" is already active — clicking it should not call updatePlanItem.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Fit/i }));
    });

    expect(mockUpdatePlanItem).not.toHaveBeenCalled();
  });

  it("3. active-state: the active button carries lime classes, inactive does not", async () => {
    await renderWithLandscapeDetected();

    const fitBtn = screen.getByRole("button", { name: /^Fit/i });
    const fillBtn = screen.getByRole("button", { name: /^Fill/i });

    // Active button has the lime border; inactive has the zinc border.
    expect(fitBtn.className).toContain("border-lime-400");
    expect(fillBtn.className).not.toContain("border-lime-400");

    // Active button's label span has the lime color class.
    const fitLabelSpan = fitBtn.querySelector("span:first-child");
    expect(fitLabelSpan?.className).toContain("text-lime-800");
    const fillLabelSpan = fillBtn.querySelector("span:first-child");
    expect(fillLabelSpan?.className).not.toContain("text-lime-800");
  });

  it("4. hidden while generating — interactive toggle absent", async () => {
    setData(makeItem({ status: "generating", landscape_fit: "fit" }));
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByRole("button", { name: /^Fit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Fill/i })).toBeNull();
  });

  it("5. post-render: read-only 'Landscape clips' label visible, interactive toggle absent", async () => {
    // variants.length > 0 → read-only display; interactive buttons gone.
    setData(
      makeItem({ status: "ready", landscape_fit: "fit" }),
      [sampleVariant],
    );
    await act(async () => {
      render(<PlanItemPage />);
    });

    // Interactive buttons must not be present.
    expect(screen.queryByRole("button", { name: /^Fit/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Fill/i })).toBeNull();

    // Read-only label must show the applied fit heading and the active option.
    expect(screen.getAllByText(/Landscape clips/i).length).toBeGreaterThanOrEqual(1);
    // The <p> with applied.label renders "Fit" as a text node (not a button).
    expect(screen.getByText(/\bFit\b/)).toBeInTheDocument();
    // Description text is also shown.
    expect(
      screen.getByText(/Keep horizontal, black bars top & bottom/i),
    ).toBeInTheDocument();
  });
});
