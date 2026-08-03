/**
 * Tests for the PoolUploadCard upload pipeline on plan/items/[id]/page.tsx
 * (mobile Safari upload/delete fix).
 *
 * Covers:
 *   - sr-only input + styled trigger replace the raw native file input.
 *   - input.value reset after selection (Safari same-file re-pick fix).
 *   - Per-file pending cards with real progress; cancel aborts + excludes from
 *     attach; failure → error card → Retry mints a FRESH signed URL.
 *   - Per-file JIT URL minting (one single-file requestUploadUrls per clip).
 *   - ≤3 concurrent uploads (module semaphore).
 *   - Re-entrant batches: trigger stays enabled mid-upload.
 *   - Delete-race regressions: (A) delete while an upload's attach settles must
 *     not resurrect/wipe; (B) a stale poll during an attach must not clobber
 *     the assignments ref (gated sync).
 *   - 44px mobile tap targets on delete/cancel (class assertions with a
 *     positive control — see collapse-wrapper-tests-need-class-assertions).
 *   - maxClips=1: trigger hidden while a pending upload exists, no cap copy.
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
window.HTMLMediaElement.prototype.load = jest.fn();

// detectLandscapeClip probes each video File via URL.createObjectURL + a
// detached <video>. jsdom has neither — stub the URL API and make the probe
// fail fast (onerror on src set) so its 5s timeout never keeps tests alive.
(URL as { createObjectURL?: unknown }).createObjectURL = jest.fn(() => "blob:mock");
(URL as { revokeObjectURL?: unknown }).revokeObjectURL = jest.fn();
const realCreateElement = document.createElement.bind(document);
jest.spyOn(document, "createElement").mockImplementation((tag: string, options?: unknown) => {
  const el = realCreateElement(tag, options as ElementCreationOptions | undefined);
  if (tag === "video") {
    Object.defineProperty(el, "src", {
      configurable: true,
      set() {
        queueMicrotask(() => (el as HTMLVideoElement).onerror?.(new Event("error")));
      },
      get: () => "",
    });
  }
  return el;
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

process.env.NEXT_PUBLIC_SUBTITLED_ENABLED = "true";

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useRouter: jest.fn(() => ({ push: jest.fn() })),
  useSearchParams: jest.fn(() => new URLSearchParams()),
}));

const mockRefetch = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: jest.fn(),
}));
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<typeof usePolledJobStatus>;

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  requestUploadUrls: jest.fn(),
  attachClips: jest.fn(),
  generatePlanItem: jest.fn(),
  updatePlanItem: jest.fn(),
  setItemVoiceover: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn(),
  changePlanItemStyle: jest.fn(),
  setPlanItemIntroSize: jest.fn(),
  uploadToGcs: jest.fn(),
  uploadToGcsWithProgress: jest.fn(),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {
    constructor() {
      super("Not authenticated");
      this.name = "NotAuthenticatedError";
    }
  },
}));

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  uploadVoiceover: jest.fn(),
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {
    status = 0;
    code: string | null = null;
  },
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
jest.mock("@/app/plan/_components/PlanVariantEditor", () => ({
  __esModule: true,
  default: () => <div data-testid="plan-variant-editor" />,
}));
jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));
jest.mock("@/app/library/_components/FeedbackButtons", () => ({
  __esModule: true,
  default: () => <div data-testid="feedback-buttons" />,
}));
jest.mock("@/app/plan/_components/AssetPool", () => ({
  __esModule: true,
  default: () => <div data-testid="asset-pool" />,
}));
jest.mock("@/app/plan/_components/SuggestionRail", () => ({
  __esModule: true,
  default: () => <div data-testid="suggestion-rail" />,
}));
jest.mock("@/app/plan/items/[id]/components/ShotSlotUploader", () => ({
  __esModule: true,
  default: () => <div data-testid="shot-slot-uploader" />,
  ClipNoteControl: () => <div data-testid="clip-note-control" />,
}));

import { attachClips, requestUploadUrls, uploadToGcsWithProgress } from "@/lib/plan-api";
const PlanItemPage = require("@/app/plan/items/[id]/page").default;
const mockAttachClips = attachClips as jest.MockedFunction<typeof attachClips>;
const mockRequestUploadUrls = requestUploadUrls as jest.MockedFunction<typeof requestUploadUrls>;
const mockUploadWithProgress = uploadToGcsWithProgress as jest.MockedFunction<
  typeof uploadToGcsWithProgress
>;

// ===== Harness =====

type CapturedUpload = {
  url: string;
  file: File;
  onProgress: (fraction: number) => void;
  signal?: AbortSignal;
  resolve: () => void;
  reject: (err: unknown) => void;
  settled: boolean;
};

const capturedUploads: CapturedUpload[] = [];

function installUploadCapture() {
  mockUploadWithProgress.mockImplementation(
    (url, file, onProgress, signal) =>
      new Promise<void>((resolve, reject) => {
        const entry: CapturedUpload = {
          url,
          file,
          onProgress,
          signal,
          settled: false,
          resolve: () => {
            entry.settled = true;
            resolve();
          },
          reject: (err) => {
            entry.settled = true;
            reject(err);
          },
        };
        signal?.addEventListener("abort", () =>
          entry.reject(new DOMException("Upload cancelled", "AbortError")),
        );
        capturedUploads.push(entry);
      }),
  );
}

function makeItem(overrides = {}) {
  return {
    id: "test-item-id",
    day_index: 3,
    theme: "Morning Routine",
    idea: "Film your morning from 6am",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    clip_assignments: [],
    status: "draft",
    current_job_id: null,
    user_edited: false,
    content_mode: "existing_footage",
    instruction_level: "full" as const,
    conformance: null,
    smart_captions_enabled: false,
    smart_sound_design_enabled: true,
    smart_captions_available: false,
    smart_captions_unavailable_reason: "feature_disabled",
    landscape_fit: "fit",
    ...overrides,
  };
}

function setData(item: Record<string, unknown>) {
  mockUsePolledJobStatus.mockReturnValue({
    data: { item, job: null },
    error: null,
    refetch: mockRefetch,
  });
}

function pickFiles(files: File[]) {
  const input = screen.getByLabelText("Upload video clips for this idea") as HTMLInputElement;
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  capturedUploads.length = 0;
  installUploadCapture();
  mockAttachClips.mockResolvedValue({});
  // JIT minting: one single-file call per clip; URL derived from the filename.
  mockRequestUploadUrls.mockImplementation(async (_id, files) =>
    files.map((f) => ({
      upload_url: `https://gcs.example/${f.filename}`,
      gcs_path: `users/u1/plan/test-item-id/${f.filename}`,
    })),
  );
});

afterEach(async () => {
  // The upload semaphore is module-scoped — settle every outstanding upload so
  // slots can't leak into the next test.
  capturedUploads.filter((c) => !c.settled).forEach((c) => c.resolve());
  await flush();
});

// ===== Tests =====

describe("PoolUploadCard — input markup (mobile Safari fix)", () => {
  it("renders an sr-only input behind a styled trigger, not the raw native control", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    const input = screen.getByLabelText("Upload video clips for this idea");
    expect(input).toHaveClass("sr-only");
    // Old raw-input styling must be gone (this is what rendered Safari's
    // native button + filename + thumbnail).
    expect(input.className).not.toContain("file:mr-3");

    const trigger = screen.getByRole("button", { name: "Add clips" });
    expect(trigger).toHaveClass("min-h-11");
    expect(trigger).toHaveClass("sm:min-h-0");
  });

  it("resets input.value after selection so re-picking the same file fires change", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    const input = screen.getByLabelText("Upload video clips for this idea") as HTMLInputElement;
    const valueSets: string[] = [];
    Object.defineProperty(input, "value", {
      configurable: true,
      get: () => "",
      set: (v: string) => {
        valueSets.push(v);
      },
    });

    await act(async () => {
      pickFiles([new File(["x"], "a.mp4", { type: "video/mp4" })]);
    });
    await flush();

    expect(valueSets).toContain("");
  });
});

describe("PoolUploadCard — pending cards, cancel, retry", () => {
  it("shows a per-file card with real progress while uploading", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      pickFiles([
        new File(["a"], "a.mp4", { type: "video/mp4" }),
        new File(["b"], "b.mp4", { type: "video/mp4" }),
      ]);
    });
    await flush();

    const cards = screen.getAllByTestId("pending-clip-card");
    expect(cards).toHaveLength(2);
    expect(screen.getByText("a.mp4")).toBeInTheDocument();
    expect(screen.getByText("b.mp4")).toBeInTheDocument();

    await act(async () => {
      capturedUploads[0].onProgress(0.5);
    });
    expect(screen.getByText(/Uploading… 50%/)).toBeInTheDocument();
  });

  it("cancel aborts the transfer and excludes the file from attach", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      pickFiles([
        new File(["a"], "a.mp4", { type: "video/mp4" }),
        new File(["b"], "b.mp4", { type: "video/mp4" }),
      ]);
    });
    await flush();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel upload of a.mp4" }));
    });
    expect(screen.queryByText("a.mp4")).toBeNull();
    expect(capturedUploads[0].signal?.aborted).toBe(true);

    await act(async () => {
      capturedUploads[1].resolve();
    });
    await waitFor(() => expect(mockAttachClips).toHaveBeenCalledTimes(1));
    const assignments = mockAttachClips.mock.calls[0][2];
    expect(assignments.map((a) => a.gcs_path)).toEqual(["users/u1/plan/test-item-id/b.mp4"]);
  });

  it("cancel during URL minting never starts the transfer", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    let releaseMint: (v: unknown) => void = () => {};
    mockRequestUploadUrls.mockImplementationOnce(
      () => new Promise((resolve) => (releaseMint = resolve)),
    );

    await act(async () => {
      pickFiles([new File(["a"], "a.mp4", { type: "video/mp4" })]);
    });
    await flush();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel upload of a.mp4" }));
    });
    await act(async () => {
      releaseMint([
        { upload_url: "https://gcs.example/a.mp4", gcs_path: "users/u1/plan/test-item-id/a.mp4" },
      ]);
    });
    await flush();

    expect(mockUploadWithProgress).not.toHaveBeenCalled();
    expect(mockAttachClips).not.toHaveBeenCalled();
  });

  it("failure flips the card to an error with Retry, and Retry mints a FRESH signed URL", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      pickFiles([new File(["a"], "a.mp4", { type: "video/mp4" })]);
    });
    await flush();
    expect(mockRequestUploadUrls).toHaveBeenCalledTimes(1);

    await act(async () => {
      capturedUploads[0].reject(new Error("network hiccup"));
    });
    expect(screen.getByText("network hiccup")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });
    await flush();
    expect(mockRequestUploadUrls).toHaveBeenCalledTimes(2);

    await act(async () => {
      capturedUploads[1].resolve();
    });
    await waitFor(() => expect(mockAttachClips).toHaveBeenCalledTimes(1));
    expect(mockAttachClips.mock.calls[0][1]).toEqual(["users/u1/plan/test-item-id/a.mp4"]);
    await waitFor(() => expect(screen.queryByTestId("pending-clip-card")).toBeNull());
  });

  it("surfaces an attach failure in the page banner (upload succeeded, save didn't)", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });
    mockAttachClips.mockRejectedValueOnce(new Error("boom from attach"));

    await act(async () => {
      pickFiles([new File(["a"], "a.mp4", { type: "video/mp4" })]);
    });
    await flush();
    await act(async () => {
      capturedUploads[0].resolve();
    });

    await waitFor(() => expect(screen.getByText(/boom from attach/)).toBeInTheDocument());
    expect(screen.queryByTestId("pending-clip-card")).toBeNull();
  });
});

describe("PoolUploadCard — concurrency + minting", () => {
  it("mints one signed URL per file, just-in-time (single-file batches)", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      pickFiles([
        new File(["a"], "a.mp4", { type: "video/mp4" }),
        new File(["b"], "b.mov", { type: "video/quicktime" }),
      ]);
    });
    await flush();

    expect(mockRequestUploadUrls).toHaveBeenCalledTimes(2);
    for (const call of mockRequestUploadUrls.mock.calls) {
      expect(call[1]).toHaveLength(1);
    }
    const minted = mockRequestUploadUrls.mock.calls.map((c) => c[1][0]);
    expect(minted.map((m) => m.filename).sort()).toEqual(["a.mp4", "b.mov"]);
    expect(minted.find((m) => m.filename === "b.mov")?.content_type).toBe("video/quicktime");
  });

  it("runs at most 3 uploads concurrently; a queued file starts when a slot frees", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      pickFiles(
        [1, 2, 3, 4, 5].map((n) => new File(["x"], `c${n}.mp4`, { type: "video/mp4" })),
      );
    });
    await flush();

    expect(capturedUploads).toHaveLength(3);

    await act(async () => {
      capturedUploads[0].resolve();
    });
    await waitFor(() => expect(capturedUploads).toHaveLength(4));
  });

  it("is re-entrant: adding clips while a batch uploads appends more cards, trigger stays enabled", async () => {
    setData(makeItem());
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      pickFiles([new File(["a"], "a.mp4", { type: "video/mp4" })]);
    });
    await flush();

    expect(screen.getByRole("button", { name: "Add clips" })).toBeEnabled();

    await act(async () => {
      pickFiles([new File(["b"], "b.mp4", { type: "video/mp4" })]);
    });
    await flush();

    expect(screen.getAllByTestId("pending-clip-card")).toHaveLength(2);
  });
});

describe("PoolUploadCard — delete-race regressions", () => {
  const c1 = {
    gcs_path: "users/u1/plan/test-item-id/c1.mp4",
    shot_id: null,
    user_note: "",
    machine_matched: false,
  };

  it("regression A: deleting while another upload's attach settles composes, never wipes or resurrects", async () => {
    setData(makeItem({ clip_assignments: [c1] }));
    await act(async () => {
      render(<PlanItemPage />);
    });

    // Hold the FIRST attach (f2's) so the delete queues behind it.
    let releaseAttach: (v: unknown) => void = () => {};
    mockAttachClips.mockImplementationOnce(
      () => new Promise((resolve) => (releaseAttach = resolve)),
    );

    await act(async () => {
      pickFiles([new File(["f2"], "f2.mp4", { type: "video/mp4" })]);
    });
    await flush();
    await act(async () => {
      capturedUploads[0].resolve();
    });
    await waitFor(() => expect(mockAttachClips).toHaveBeenCalledTimes(1));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove c1.mp4" }));
    });
    await act(async () => {
      releaseAttach({});
    });

    await waitFor(() => expect(mockAttachClips).toHaveBeenCalledTimes(2));
    const finalAssignments = mockAttachClips.mock.calls[1][2].map((a) => a.gcs_path);
    // Old closure-based code computed `remaining` from stale item data: the
    // delete would wipe f2 (payload []) — and an upload settling after a
    // delete would resurrect c1.
    expect(finalAssignments).toEqual(["users/u1/plan/test-item-id/f2.mp4"]);
  });

  it("regression B: a stale poll landing during an attach cannot clobber the ref (gated sync)", async () => {
    const staleItem = makeItem({ clip_assignments: [c1] });
    setData(staleItem);
    let view: ReturnType<typeof render>;
    await act(async () => {
      view = render(<PlanItemPage />);
    });

    // Hold the delete's attach POST.
    let releaseAttach: (v: unknown) => void = () => {};
    mockAttachClips.mockImplementationOnce(
      () => new Promise((resolve) => (releaseAttach = resolve)),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove c1.mp4" }));
    });

    // Stale poll response (still contains c1) arrives while the attach is in
    // flight — a NEW array identity, so the sync effect re-runs.
    await act(async () => {
      setData({ ...staleItem, clip_assignments: [{ ...c1 }] });
      view!.rerender(<PlanItemPage />);
    });
    await act(async () => {
      releaseAttach({});
    });
    await waitFor(() => expect(mockAttachClips).toHaveBeenCalledTimes(1));

    // Next writer must see the POST-DELETE list — an ungated sync would have
    // restored c1 into the ref and resurrected it here.
    await act(async () => {
      pickFiles([new File(["f3"], "f3.mp4", { type: "video/mp4" })]);
    });
    await flush();
    await act(async () => {
      capturedUploads[0].resolve();
    });
    await waitFor(() => expect(mockAttachClips).toHaveBeenCalledTimes(2));
    const finalAssignments = mockAttachClips.mock.calls[1][2].map((a) => a.gcs_path);
    expect(finalAssignments).toEqual(["users/u1/plan/test-item-id/f3.mp4"]);
  });
});

describe("PoolUploadCard — touch targets + maxClips", () => {
  it("delete and cancel controls meet the 44px mobile floor (with positive control)", async () => {
    setData(
      makeItem({
        clip_assignments: [
          {
            gcs_path: "users/u1/plan/test-item-id/c1.mp4",
            shot_id: null,
            user_note: "",
            machine_matched: false,
          },
        ],
      }),
    );
    await act(async () => {
      render(<PlanItemPage />);
    });

    const remove = screen.getByRole("button", { name: "Remove c1.mp4" });
    expect(remove).toHaveClass("h-11");
    expect(remove).toHaveClass("w-11");
    expect(remove).toHaveClass("sm:h-5");
    expect(remove).toHaveClass("sm:w-5");

    await act(async () => {
      pickFiles([new File(["a"], "a.mp4", { type: "video/mp4" })]);
    });
    await flush();
    const cancel = screen.getByRole("button", { name: "Cancel upload of a.mp4" });
    expect(cancel).toHaveClass("h-11");
    expect(cancel).toHaveClass("w-11");

    // Positive control: the class assertions CAN fail — a sibling element does
    // not carry the 44px classes.
    expect(screen.getByText("c1.mp4")).not.toHaveClass("h-11");
  });

  it("maxClips=1 (subtitled): pending upload counts toward the cap, no lying cap copy", async () => {
    setData(makeItem({ edit_format: "subtitled", content_mode: "create_new" }));
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      const input = screen.getByLabelText("Upload video clips for this idea") as HTMLInputElement;
      Object.defineProperty(input, "files", {
        value: [new File(["a"], "a.mp4", { type: "video/mp4" })],
        configurable: true,
      });
      fireEvent.change(input);
    });
    await flush();

    // Trigger + input are gone while the single slot is pending…
    expect(screen.queryByRole("button", { name: "Add your clip" })).toBeNull();
    // …and the "One clip added" copy must NOT show for a still-uploading clip.
    expect(screen.queryByText(/One clip added/)).toBeNull();
    expect(screen.getByTestId("pending-clip-card")).toBeInTheDocument();
  });
});
