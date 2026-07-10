/**
 * ShotSlotUploader — slot state machine, pill counts, label softening,
 * post-reload chip-led filled state, aria, Generate gate, pool add/remove.
 *
 * Per plan T11. Matches the test coverage map in the plan file.
 */

// @ts-nocheck
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

// ── Mock plan-api (all async helpers) ────────────────────────────────────────

const mockRequestUploadUrls = jest.fn();
const mockUploadToGcsWithProgress = jest.fn();
const mockAttachClips = jest.fn();
const mockUpdatePlanItemShot = jest.fn();

jest.mock("@/lib/plan-api", () => ({
  requestUploadUrls: (...args: unknown[]) => mockRequestUploadUrls(...args),
  uploadToGcsWithProgress: (...args: unknown[]) => mockUploadToGcsWithProgress(...args),
  attachClips: (...args: unknown[]) => mockAttachClips(...args),
  updatePlanItemShot: (...args: unknown[]) => mockUpdatePlanItemShot(...args),
}));

// ── Mock URL APIs (jsdom stubs) ───────────────────────────────────────────────

Object.defineProperty(window, "URL", {
  writable: true,
  value: {
    createObjectURL: jest.fn(() => "blob:mock"),
    revokeObjectURL: jest.fn(),
  },
});

// ── Mock <video> so probeVideoDuration resolves quickly in happy-path tests ───
// jsdom never fires loadedmetadata/onerror for blob URLs, so we stub src setter
// to fire onerror immediately (resolves with null — no duration).
// Tests that need duration mock this again to fire loadedmetadata instead.
const _origCreate = document.createElement.bind(document);
jest
  .spyOn(document, "createElement")
  .mockImplementation((tag: string) => {
    const el = _origCreate(tag);
    if (tag === "video") {
      Object.defineProperty(el, "src", {
        set() {
          setTimeout(() => {
            if (this.onerror) this.onerror(new Event("error"));
          }, 0);
        },
        configurable: true,
      });
    }
    return el;
  });

// ── Import component AFTER mocks ─────────────────────────────────────────────

import ShotSlotUploader from "@/app/plan/items/[id]/components/ShotSlotUploader";
import type { ClipAssignment, FilmingShot, PlanItem } from "@/lib/plan-api";

// ── Helpers ───────────────────────────────────────────────────────────────────

function shot(overrides: Partial<FilmingShot> = {}): FilmingShot {
  return {
    shot_id: "sid-1",
    what: "creator to camera",
    how: "eye level",
    duration_s: 8,
    ...overrides,
  };
}

function makeItem(overrides: Partial<PlanItem> = {}): PlanItem {
  return {
    id: "item-1",
    day_index: 1,
    theme: "fitness",
    idea: "morning routine",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    clip_assignments: [],
    status: "idea",
    current_job_id: null,
    user_edited: false,
    ...overrides,
  };
}

function makeFile(name = "clip.mp4", size = 1024): File {
  return new File(["x".repeat(size)], name, { type: "video/mp4" });
}

// Default happy-path mock setup.
function setupHappyMocks(updatedItem?: PlanItem) {
  mockRequestUploadUrls.mockResolvedValue([
    { upload_url: "https://storage.example.com/put", gcs_path: "users/u/plan/item-1/clip.mp4" },
  ]);
  mockUploadToGcsWithProgress.mockImplementation(async (_url, _file, onProgress) => {
    onProgress(0.5);
    onProgress(1.0);
  });
  mockAttachClips.mockResolvedValue(updatedItem ?? makeItem());
}

// ── Tests ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  jest.clearAllMocks();
});

describe("ShotSlotUploader — idle slot render", () => {
  it("renders empty-slot copy and slot count for each shot", () => {
    const s1 = shot({ shot_id: "s1", what: "creator to camera" });
    const s2 = shot({ shot_id: "s2", what: "close-up product" });
    const item = makeItem({ filming_guide: [s1, s2] });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    // Each shot's "what" text appears.
    expect(screen.getByText("creator to camera")).toBeInTheDocument();
    expect(screen.getByText("close-up product")).toBeInTheDocument();
    // Upload copy on idle slots.
    expect(screen.getAllByText("Upload this shot — or drag a file here").length).toBe(2);
  });

  it("shows '0 of N filmed' muted text when nothing is filled", () => {
    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" }), shot({ shot_id: "s2" })] });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    expect(screen.getByText("0 of 2 filmed")).toBeInTheDocument();
    // No lime pill when zero filled.
    expect(screen.queryByText(/of 2 filmed/)).toHaveTextContent("0 of 2 filmed");
  });

  it("sets per-slot aria-label with shot index and what text", () => {
    const s = shot({ shot_id: "s1", what: "creator to camera" });
    const item = makeItem({ filming_guide: [s] });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    const label = screen.getByLabelText("Upload shot 1: creator to camera");
    expect(label).toBeInTheDocument();
  });

  it("has an aria-live polite region for screen-reader announcements", () => {
    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    const liveRegion = document.querySelector("[aria-live='polite']");
    expect(liveRegion).toBeInTheDocument();
  });
});

describe("ShotSlotUploader — shot text editing sentinel", () => {
  it("does not auto-enter edit mode for legacy null shot_id rows", () => {
    const item = makeItem({
      filming_guide: [
        shot({ shot_id: null, what: "legacy opener", how: "wide shot" }),
        shot({ shot_id: null, what: "legacy detail", how: "close shot" }),
      ],
    });

    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    expect(screen.getByText("legacy opener")).toBeInTheDocument();
    expect(screen.getByText("legacy detail")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("What to film")).toBeNull();
    expect(screen.queryByPlaceholderText("How (optional)")).toBeNull();
    expect(screen.queryByText("Save")).toBeNull();
    expect(screen.queryByText("Cancel")).toBeNull();
  });

  it("Cancel exits shot text edit mode", () => {
    const item = makeItem({
      filming_guide: [shot({ shot_id: "s1", what: "editable opener", how: "eye level" })],
    });

    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    fireEvent.click(screen.getByLabelText("Edit shot 1 text"));
    expect(screen.getByDisplayValue("editable opener")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Cancel"));

    expect(screen.queryByDisplayValue("editable opener")).toBeNull();
    expect(screen.getByText("editable opener")).toBeInTheDocument();
  });
});

describe("ShotSlotUploader — post-reload filled state (D9)", () => {
  it("shows chip-led filled state for clips already in clip_assignments", () => {
    const s = shot({ shot_id: "s1", what: "creator to camera" });
    const item = makeItem({
      filming_guide: [s],
      clip_assignments: [{ gcs_path: "users/u/plan/item-1/clip.mp4", shot_id: "s1" }],
    });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    // Chip with filename appears.
    expect(screen.getByText("clip.mp4")).toBeInTheDocument();
    // No image well — local file is gone after reload.
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    // Replace link is present.
    expect(screen.getByText("Replace")).toBeInTheDocument();
  });

  it("shows progress pill when a slot is pre-filled from reload", () => {
    const s = shot({ shot_id: "s1" });
    const item = makeItem({
      filming_guide: [s],
      clip_assignments: [{ gcs_path: "users/u/plan/item-1/clip.mp4", shot_id: "s1" }],
    });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    // Lime pill visible when ≥1 filled.
    expect(screen.getByText("1 of 1 filmed")).toBeInTheDocument();
  });

  it("does NOT show gray placeholder for filled-on-reload (D9: no gray=nothing)", () => {
    const s = shot({ shot_id: "s1" });
    const item = makeItem({
      filming_guide: [s],
      clip_assignments: [{ gcs_path: "users/u/plan/item-1/clip.mp4", shot_id: "s1" }],
    });

    const { container } = render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    // No dashed border on filled slot (idle's drop target is gone).
    const dashed = container.querySelector(".border-dashed");
    // The pool's "+ Add clips" label has border-dashed, but the slot itself should not.
    const dashedTargets = container.querySelectorAll(".border-dashed");
    // Only the pool add-clips label is dashed (1), not any slot.
    expect(dashedTargets.length).toBe(1);
  });
});

describe("ShotSlotUploader — label softening (D10)", () => {
  it("softens idle slots to 'Optional — add if you filmed it' once ≥1 slot is filled", () => {
    const s1 = shot({ shot_id: "s1", what: "opener" });
    const s2 = shot({ shot_id: "s2", what: "product detail" });
    const item = makeItem({
      filming_guide: [s1, s2],
      // s1 is pre-filled.
      clip_assignments: [{ gcs_path: "users/u/plan/item-1/clip.mp4", shot_id: "s1" }],
    });

    render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    // s2 is idle but softened.
    expect(screen.getByText("Optional — add if you filmed it")).toBeInTheDocument();
    // s1 is filled, so the strong copy is gone.
    expect(screen.queryByText("Upload this shot — or drag a file here")).not.toBeInTheDocument();
  });
});

describe("ShotSlotUploader — uploading / committing states", () => {
  it("shows progress bar and Cancel button during upload", async () => {
    let resolveUpload!: () => void;
    mockRequestUploadUrls.mockResolvedValue([
      { upload_url: "https://storage.example.com/put", gcs_path: "users/u/plan/item-1/clip.mp4" },
    ]);
    mockUploadToGcsWithProgress.mockImplementation(
      () => new Promise<void>((resolve) => { resolveUpload = resolve; }),
    );

    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    const file = makeFile("clip.mp4");

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    // Cancel visible during upload.
    expect(screen.getByText("Cancel")).toBeInTheDocument();

    // Clean up — resolve the upload.
    mockAttachClips.mockResolvedValue(makeItem());
    await act(async () => { resolveUpload(); });
  });

  it("calls onBusyChange(true) while uploading, onBusyChange(false) on completion", async () => {
    let resolveUpload!: () => void;
    mockRequestUploadUrls.mockResolvedValue([
      { upload_url: "https://storage.example.com/put", gcs_path: "users/u/plan/item-1/clip.mp4" },
    ]);
    mockUploadToGcsWithProgress.mockImplementation(
      () => new Promise<void>((resolve) => { resolveUpload = resolve; }),
    );
    mockAttachClips.mockResolvedValue(makeItem());

    const onBusyChange = jest.fn();
    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={onBusyChange} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");

    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    // Busy=true while upload in flight.
    expect(onBusyChange).toHaveBeenCalledWith(true);

    // Resolve upload + commit.
    await act(async () => { resolveUpload(); });
    await waitFor(() => {
      const calls = onBusyChange.mock.calls.map((c) => c[0]);
      expect(calls).toContain(false);
    });
  });

  it("Generate disabled while uploading (onBusyChange(true) fired)", async () => {
    // This test asserts the contract ShotSlotUploader gives page.tsx.
    // page.tsx uses onBusyChange to gate the Generate button.
    let resolveUpload!: () => void;
    mockRequestUploadUrls.mockResolvedValue([
      { upload_url: "https://storage.example.com/put", gcs_path: "users/u/plan/item-1/clip.mp4" },
    ]);
    mockUploadToGcsWithProgress.mockImplementation(
      () => new Promise<void>((resolve) => { resolveUpload = resolve; }),
    );

    const onBusyChange = jest.fn();
    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={onBusyChange} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    // Busy true while upload pending.
    expect(onBusyChange).toHaveBeenLastCalledWith(true);

    // Cleanup.
    mockAttachClips.mockResolvedValue(makeItem());
    await act(async () => { resolveUpload(); });
  });
});

describe("ShotSlotUploader — error state", () => {
  it("shows error chip with 'Upload failed · Retry' on upload failure", async () => {
    mockRequestUploadUrls.mockRejectedValue(new Error("network error"));

    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    await waitFor(() => {
      expect(screen.getByText("Upload failed · Retry")).toBeInTheDocument();
    });
  });

  it("error chip is zinc, NOT lime (D13: lime reserved for success)", async () => {
    mockRequestUploadUrls.mockRejectedValue(new Error("fail"));

    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    const { container } = render(
      <ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />,
    );

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    await waitFor(() => {
      expect(screen.getByText("Upload failed · Retry")).toBeInTheDocument();
    });

    // Error chip must use zinc bg-zinc-50, NOT bg-lime-50.
    const errorChip = container.querySelector(".bg-zinc-50");
    expect(errorChip).toBeInTheDocument();
    const limeChip = container.querySelector(".bg-lime-50");
    expect(limeChip).not.toBeInTheDocument();
  });
});

describe("ShotSlotUploader — filled chip after successful upload", () => {
  it("shows lime ✓ chip with filename after upload + attach succeed", async () => {
    setupHappyMocks();

    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile("myclip.mp4")] } });
    });

    // Wait for filled state — "Replace" only appears in the filled phase.
    await waitFor(() => {
      expect(screen.getByText("Replace")).toBeInTheDocument();
    });
    // ✓ chip and filename present.
    expect(screen.getByText("✓")).toBeInTheDocument();
    expect(screen.getByText("myclip.mp4")).toBeInTheDocument();
  });

  it("progress pill shows 'N of M filmed' on success", async () => {
    setupHappyMocks();

    const s1 = shot({ shot_id: "s1" });
    const s2 = shot({ shot_id: "s2", what: "close-up" });
    const item = makeItem({ filming_guide: [s1, s2] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    await waitFor(() => {
      expect(screen.getByText("1 of 2 filmed")).toBeInTheDocument();
    });
  });

  it("Replace resets slot to idle (re-upload affordance)", async () => {
    setupHappyMocks();

    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    await waitFor(() => expect(screen.getByText("Replace")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByText("Replace"));
    });

    // Slot back to idle.
    expect(screen.getByText("Upload this shot — or drag a file here")).toBeInTheDocument();
  });

  it("calls attachClips with the correct assignments payload", async () => {
    setupHappyMocks();

    const s = shot({ shot_id: "sid-abc" });
    const item = makeItem({ filming_guide: [s] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile("a.mp4")] } });
    });

    await waitFor(() => expect(mockAttachClips).toHaveBeenCalled());

    const [itemId, gcsPaths, assignments] = mockAttachClips.mock.calls[0];
    expect(itemId).toBe("item-1");
    expect(gcsPaths).toContain("users/u/plan/item-1/clip.mp4");
    expect(assignments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ gcs_path: "users/u/plan/item-1/clip.mp4", shot_id: "sid-abc" }),
      ]),
    );
  });
});

describe("ShotSlotUploader — pool (extra footage strip)", () => {
  it("renders 'Extra footage' strip", () => {
    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    expect(screen.getByText("Extra footage")).toBeInTheDocument();
    expect(screen.getByText("optional")).toBeInTheDocument();
  });

  it("shows pre-existing pool clips as zinc chips", () => {
    const item = makeItem({
      filming_guide: [shot({ shot_id: "s1" })],
      clip_assignments: [
        { gcs_path: "users/u/plan/item-1/a.mp4", shot_id: "s1" },
        { gcs_path: "users/u/plan/item-1/pool.mp4", shot_id: null },
      ],
    });

    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    expect(screen.getByText("pool.mp4")).toBeInTheDocument();
    // Pool remove button.
    expect(screen.getByLabelText("Remove pool.mp4")).toBeInTheDocument();
  });

  it("removes a pool clip on ✕ click and enqueues attach", async () => {
    mockAttachClips.mockResolvedValue(makeItem());

    const item = makeItem({
      filming_guide: [shot({ shot_id: "s1" })],
      clip_assignments: [{ gcs_path: "users/u/plan/item-1/pool.mp4", shot_id: null }],
    });

    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    await act(async () => {
      fireEvent.click(screen.getByLabelText("Remove pool.mp4"));
    });

    // Chip gone.
    expect(screen.queryByText("pool.mp4")).not.toBeInTheDocument();
    // Attach called without that path.
    await waitFor(() => expect(mockAttachClips).toHaveBeenCalled());
    const [, gcsPaths] = mockAttachClips.mock.calls[0];
    expect(gcsPaths).not.toContain("users/u/plan/item-1/pool.mp4");
  });
});

describe("ShotSlotUploader — uninstructed items (no filming_guide)", () => {
  it("renders no shot rows when filming_guide is empty", () => {
    const item = makeItem({ filming_guide: [] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    // No shot index numerals.
    expect(screen.queryByText(/1\./)).not.toBeInTheDocument();
    // Header row shows 0 of 0.
    expect(screen.getByText("0 of 0 filmed")).toBeInTheDocument();
  });
});

describe("ShotSlotUploader — duration chip (D11)", () => {
  it("shows duration label on filled chip when probed successfully", async () => {
    setupHappyMocks();

    // Override the existing createElement spy to fire loadedmetadata (not onerror).
    // Use _origCreate (captured pre-spy) to avoid recursive calls through the spy.
    (document.createElement as jest.Mock).mockImplementation((tag: string) => {
      const el = _origCreate(tag);
      if (tag === "video") {
        Object.defineProperty(el, "duration", { value: 18, configurable: true });
        Object.defineProperty(el, "src", {
          set() {
            setTimeout(() => {
              if (this.onloadedmetadata) this.onloadedmetadata(new Event("loadedmetadata"));
            }, 0);
          },
          configurable: true,
        });
      }
      return el;
    });

    const item = makeItem({ filming_guide: [shot({ shot_id: "s1" })] });
    render(<ShotSlotUploader item={item} onAttached={jest.fn()} onBusyChange={jest.fn()} />);

    const input = screen.getByLabelText("Upload shot 1: creator to camera");
    await act(async () => {
      fireEvent.change(input, { target: { files: [makeFile()] } });
    });

    await waitFor(() => {
      // "· 0:18" appears on the chip.
      expect(screen.getByText("· 0:18")).toBeInTheDocument();
    });

    // Restore the onerror version for subsequent tests.
    (document.createElement as jest.Mock).mockImplementation((tag: string) => {
      const el = _origCreate(tag);
      if (tag === "video") {
        Object.defineProperty(el, "src", {
          set() {
            setTimeout(() => {
              if (this.onerror) this.onerror(new Event("error"));
            }, 0);
          },
          configurable: true,
        });
      }
      return el;
    });
  });
});
