process.env.NEXT_PUBLIC_VISUAL_BLOCKS_ENABLED = "true";
process.env.NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED = "true";
process.env.NEXT_PUBLIC_SOUND_EFFECTS_ENABLED = "true";

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  EditorCapabilities,
  PlanItem,
  PlanItemVariant,
  PoolAsset,
  TextElement,
  VisualBlock,
} from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;
window.HTMLMediaElement.prototype.pause = jest.fn();
window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);

const existingCrypto = (globalThis as Record<string, unknown>).crypto ?? {};
Object.defineProperty(globalThis, "crypto", {
  value: {
    ...(existingCrypto as object),
    subtle: { digest: jest.fn(async () => new Uint8Array(32).fill(0xab).buffer) },
  },
  configurable: true,
  writable: true,
});
if (typeof File.prototype.arrayBuffer !== "function") {
  File.prototype.arrayBuffer = async () => new ArrayBuffer(8);
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width"),
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

// Toast moved to sonner (DESIGN.md §15) — assert on the toast() call.
const mockToast = jest.fn();
jest.mock("sonner", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
}));

const mockCommitEditorSession = jest.fn();
jest.mock("@/lib/editor-commit", () => ({
  ...jest.requireActual("@/lib/editor-commit"),
  commitEditorSession: (...args: unknown[]) => mockCommitEditorSession(...args),
}));

jest.mock("@/app/plan/_components/useClipTimeline", () => {
  const state = {
    grid: [],
    clipDurations: {},
    baseline: [],
    slots: [],
    past: [],
    future: [],
    clampNonce: 0,
    clampedKey: null,
  };
  const dispatch = jest.fn();
  const clips: never[] = [];
  const windows: never[] = [];
  const reload = jest.fn();
  return {
    useClipTimeline: () => ({
      state,
      dispatch,
      clips,
      windows,
      totalS: 0,
      loadState: "ready",
      reload,
    }),
  };
});

const EditorShell =
  require("@/app/plan/items/[id]/_editor/EditorShell").default as typeof import("@/app/plan/items/[id]/_editor/EditorShell").default;
const { getPlanItem, getPlanItemJobStatus } = require("@/lib/plan-api") as {
  getPlanItem: typeof import("@/lib/plan-api").getPlanItem;
  getPlanItemJobStatus: typeof import("@/lib/plan-api").getPlanItemJobStatus;
};

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;
const originalFetch = global.fetch;

const ITEM = {
  id: "item-1",
  theme: "My video",
  current_job_id: "job-1",
} as unknown as PlanItem;

const CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  visual_blocks: true,
  carousel: true,
  suggestions: true,
};

const TEXT_CARD: VisualBlock = {
  version: 1,
  id: "card-1",
  kind: "text_card",
  start_s: 0,
  end_s: 4,
  timing_mode: "manual",
  origin: "ai",
  transition_in: "fade",
  transition_out: "fade",
  audio_policy: { base: "continue", sfx: "continue" },
  background: { type: "solid", color: "#111111" },
};

function linkedText(id: string, text: string): TextElement {
  return {
    id,
    text,
    role: "generative_intro",
    visual_block_id: TEXT_CARD.id,
    start_s: 0,
    end_s: 4,
    x_frac: 0.5,
    y_frac: 0.5,
  };
}

function makeVariant(
  textElements: TextElement[],
  visualBlocks: VisualBlock[] = [TEXT_CARD],
  extra: Partial<PlanItemVariant> = {},
) {
  return {
    variant_id: "song_text",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "agent_text",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: textElements,
    visual_blocks: visualBlocks,
    resolved_archetype: "montage",
    render_generation_id: "gen-current",
    editor_capabilities: CAPABILITIES,
    ...extra,
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: { text_elements: true, visual_blocks: true },
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="song_text" />);
    await Promise.resolve();
    await Promise.resolve();
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
  global.fetch = originalFetch;
});

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("EditorShell linked text-card deletion", () => {
  it("deletes the parent card with its final linked text, restores both with Undo, and saves both sections", async () => {
    await renderShell(makeVariant([linkedText("title-1", "Card title")]));

    fireEvent.click(screen.getByRole("button", { name: /^Text row 1, Card title,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.queryByRole("button", { name: /^Text row 1, Card title,/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Text card,/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: /^Text row 1, Card title,/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Text card,/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    expect(mockCommitEditorSession.mock.calls[0][2]).toMatchObject({
      text_elements: [],
      visual_blocks: [],
    });
  });

  it("deleting one of multiple linked texts retains the card and sibling text", async () => {
    await renderShell(
      makeVariant([
        linkedText("title-1", "Primary title"),
        linkedText("title-2", "Supporting title"),
      ]),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Text row 1, Primary title,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.queryByRole("button", { name: /Primary title/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Supporting title/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Text card,/ })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    const body = mockCommitEditorSession.mock.calls[0][2];
    expect(body.text_elements).toHaveLength(1);
    expect(body.text_elements[0].id).toBe("title-2");
    expect(body.visual_blocks).toBeUndefined();
  });

  it("deleting the parent visual removes every linked text", async () => {
    await renderShell(
      makeVariant([
        linkedText("title-1", "Primary title"),
        linkedText("title-2", "Supporting title"),
      ]),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Text card,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    expect(screen.queryByRole("button", { name: /^Text card,/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Primary title/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Supporting title/ })).toBeNull();
  });

  it("does not delete capability-locked text through the desktop shortcut", async () => {
    await renderShell(
      makeVariant([linkedText("title-1", "Locked title")], [], {
        editor_capabilities: {
          ...CAPABILITIES,
          text_elements: false,
          lanes: {
            text: { editable: false, reason: "Story text is locked." },
          },
        },
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Text row 1, Locked title,/ }));
    fireEvent.keyDown(document, { key: "Delete" });

    expect(screen.getByRole("button", { name: /^Text row 1, Locked title,/ })).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith("Story text is locked.", expect.objectContaining({ duration: 2600 }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});

describe("EditorShell Carousel deletion", () => {
  it("enables desktop Delete for a selected Carousel and Undo restores it", async () => {
    await renderShell(
      makeVariant([], [], {
        duration_s: 12,
        carousel_moment: {
          effect: "scale_sweep",
          mode: "focus",
          focus_clip_index: null,
          position: "middle",
          duration_s: 6,
          transition: "crossfade",
        },
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Carousel block/i }));
    const deleteButton = screen.getByRole("button", { name: "Delete selected" });
    expect(deleteButton).toBeEnabled();

    fireEvent.click(deleteButton);
    expect(screen.queryByRole("button", { name: /Carousel block/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: /Carousel block/i })).toBeInTheDocument();
  });

  it("disables desktop Delete for an incapable persisted Carousel", async () => {
    await renderShell(
      makeVariant([], [], {
        duration_s: 12,
        carousel_moment: {
          effect: "scale_sweep",
          mode: "focus",
          focus_clip_index: null,
          position: "middle",
          duration_s: 6,
          transition: "crossfade",
        },
        editor_capabilities: {
          ...CAPABILITIES,
          carousel: false,
          carousel_reason: "Carousel is unavailable for this video.",
        },
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Carousel block/i }));
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeDisabled();
  });
});

describe("EditorShell generated overlay bundle deletion", () => {
  it("removes the linked SFX and camera effect in one command and Undo restores all lanes", async () => {
    const overlay = {
      id: "overlay-1",
      kind: "image" as const,
      src_gcs_path: "users/u1/logo.png",
      label: "Logo",
      position: "center" as const,
      x_frac: 0.5,
      y_frac: 0.5,
      scale: 0.35,
      start_s: 1,
      end_s: 3,
      z: 0,
      source: "smart_captions" as const,
      effect_group_id: "smart-event-1",
    };
    const soundEffect = {
      id: "sfx-1",
      sound_effect_id: "whoosh",
      src_gcs_path: "sfx/whoosh.mp3",
      at_s: 1,
      gain: 1,
      source: "smart_captions" as const,
      effect_group_id: "smart-event-1",
    };
    const cameraEffect = {
      id: "camera-1",
      token: "semantic_crop_pulse" as const,
      start_s: 1,
      end_s: 3,
      intensity: 0.04,
      easing: "sine_pulse" as const,
      source: "smart_captions" as const,
      effect_group_id: "smart-event-1",
    };
    await renderShell(
      makeVariant([], [], {
        media_overlays: [overlay],
        sound_effects: [soundEffect],
        camera_effects: [cameraEffect],
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Overlay row 1, Image,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    expect(screen.queryByRole("button", { name: /^Overlay row 1, Image,/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Sound effect row 1,/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Camera focus,/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: /^Overlay row 1, Image,/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Sound effect row 1,/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Camera focus,/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Overlay row 1, Image,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalled());
    expect(mockCommitEditorSession.mock.calls[0][2]).toMatchObject({
      media_overlays: [],
      sound_effects: [],
      camera_effects: [],
    });
  });

  it("explains why a capability-locked overlay cannot be quick-deleted", async () => {
    await renderShell(
      makeVariant([], [], {
        media_overlays: [{
          id: "overlay-locked",
          kind: "image",
          src_gcs_path: "users/u1/logo.png",
          position: "center",
          x_frac: 0.5,
          y_frac: 0.5,
          scale: 0.35,
          start_s: 1,
          end_s: 3,
          z: 0,
          source: "user",
        }],
        editor_capabilities: {
          ...CAPABILITIES,
          overlays: false,
          lanes: {
            overlays: { editable: false, reason: "Overlays are locked for this story." },
          },
        },
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /^Overlay row 1, Image,/ }));
    fireEvent.keyDown(document, { key: "Delete" });

    expect(screen.getByRole("button", { name: /^Overlay row 1, Image,/ })).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith(
      "Overlays are locked for this story.",
      expect.objectContaining({ duration: 2600 }),
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});

describe("EditorShell rendered playback state", () => {
  it("stays playing when the rendered video emits time updates", async () => {
    await renderShell(makeVariant([linkedText("title-1", "Card title")]));

    const video = document.querySelector("video");
    expect(video).not.toBeNull();

    fireEvent.play(video as HTMLVideoElement);
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();

    Object.defineProperty(video, "currentTime", {
      configurable: true,
      value: 1.25,
      writable: true,
    });
    fireEvent.timeUpdate(video as HTMLVideoElement);

    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });
});

describe("EditorShell visuals upload lifecycle", () => {
  function poolAsset(overrides: Partial<PoolAsset> = {}): PoolAsset {
    return {
      id: "asset-editor",
      kind: "image",
      status: "ready",
      source_filename: "queued.png",
      duration_s: null,
      aspect: null,
      width: 1080,
      height: 1920,
      subject: "queued visual",
      user_context: "",
      nova_description: null,
      nova_on_screen_text: null,
      display_url: "https://storage.example/queued.png",
      deduped: false,
      gcs_path: "users/u/plan/item-1/pool/queued.png",
      error_code: null,
      error_detail: null,
      retryable: true,
      ...overrides,
    };
  }

  it("shows a failed transfer in the real drawer and retries it without disabling the picker", async () => {
    let presignCalls = 0;
    let putCalls = 0;
    let registerCalls = 0;
    const clientUploadIds: string[] = [];
    const correlationIds: string[] = [];
    const contentTypes: string[] = [];
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url.endsWith("/assets")) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url.endsWith("/assets/upload-urls")) {
        presignCalls += 1;
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string; content_type: string }>;
        };
        const clientUploadId = body.files[0].client_upload_id;
        clientUploadIds.push(clientUploadId);
        contentTypes.push(body.files[0].content_type);
        correlationIds.push(new Headers(init?.headers).get("X-Correlation-Id") ?? "");
        return jsonResponse({
          urls: [
            {
              reservation_id: "reservation-editor",
              client_upload_id: clientUploadId,
              upload_url: `https://storage.example/editor-${presignCalls}`,
              gcs_path: "users/u/plan/item-1/pool/broken.mov",
              expires_at: new Date(Date.now() + 60_000).toISOString(),
              upload_headers: { "x-goog-if-generation-match": "0" },
            },
          ],
        });
      }
      if (method === "PUT" && url.startsWith("https://storage.example/editor-")) {
        putCalls += 1;
        return jsonResponse({}, putCalls === 1 ? 503 : 200);
      }
      if (method === "POST" && url.endsWith("/assets")) {
        registerCalls += 1;
        return jsonResponse(
          poolAsset({
            source_filename: "broken.mov",
            kind: "video",
            subject: "recovered visual",
          }),
        );
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    await renderShell(makeVariant([]));
    fireEvent.click(screen.getByRole("button", { name: "Visuals tool" }));
    const picker = screen.getByLabelText("Upload images or videos");
    fireEvent.change(picker, {
      target: { files: [new File(["video"], "broken.mov", { type: "" })] },
    });

    expect(
      await screen.findByText("Upload interrupted. Check your connection and retry."),
    ).toBeInTheDocument();
    expect(screen.getByText("broken.mov")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry broken.mov" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove broken.mov" })).toBeInTheDocument();
    expect(screen.getByLabelText("Upload images or videos")).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Retry broken.mov" }));
    expect(await screen.findByRole("button", { name: "Select broken.mov" })).toBeInTheDocument();
    expect(presignCalls).toBe(2);
    expect(putCalls).toBe(2);
    expect(registerCalls).toBe(1);
    expect(new Set(clientUploadIds).size).toBe(1);
    expect(new Set(correlationIds).size).toBe(1);
    expect(contentTypes).toEqual(["video/quicktime", "video/quicktime"]);
  });

  it("honors an authoritative hidden reservation in the real Visuals drawer", async () => {
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url.endsWith("/assets")) {
        return jsonResponse({
          assets: [],
          max_assets: 1,
          occupied_assets: 1,
          active_reservations: [
            {
              reservation_id: "reservation-hidden",
              release_at: new Date(Date.now() + 60_000).toISOString(),
            },
          ],
        });
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    await renderShell(makeVariant([]));
    fireEvent.click(screen.getByRole("button", { name: "Visuals tool" }));
    expect(await screen.findByLabelText("Visuals pool is full")).toBeDisabled();
    expect(
      screen.getByText(
        "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.",
      ),
    ).toBeInTheDocument();
  });

  it("reopens editor upload capacity after deleting the last full-pool asset", async () => {
    const failed = poolAsset({
      id: "asset-full",
      status: "failed",
      source_filename: "full.png",
      display_url: null,
      subject: null,
      error_detail: "Kria temporarily couldn't analyze this file. Try again.",
    });
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url.endsWith("/assets")) {
        return jsonResponse({
          assets: [failed],
          max_assets: 1,
          occupied_assets: 1,
          active_reservations: [],
        });
      }
      if (method === "DELETE" && url.endsWith("/assets/asset-full")) {
        return jsonResponse({ ok: true });
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    await renderShell(makeVariant([]));
    fireEvent.click(screen.getByRole("button", { name: "Visuals tool" }));
    expect(await screen.findByLabelText("Visuals pool is full")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove full.png" }));
    expect(await screen.findByLabelText("Upload images or videos")).toBeEnabled();
  });

  it("fences stale hydration and polls a newly queued asset through to ready", async () => {
    const queued = poolAsset({
      status: "queued",
      display_url:
        "https://storage.googleapis.com/nova/users/u/plan/item-1/pool/queued.heic?signature=raw",
      subject: null,
    });
    const ready = poolAsset({
      display_url:
        "https://storage.googleapis.com/nova/users/u/plan/item-1/pool/queued.heic.preview.jpg?signature=preview",
    });
    let listCalls = 0;
    let releaseInitial: ((response: Response) => void) | null = null;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url.endsWith("/assets")) {
        listCalls += 1;
        if (listCalls === 1) {
          return await new Promise<Response>((resolve) => {
            releaseInitial = resolve;
          });
        }
        return jsonResponse({ assets: [ready], max_assets: 20 });
      }
      if (method === "POST" && url.endsWith("/assets/upload-urls")) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            {
              reservation_id: "reservation-queued",
              client_upload_id: body.files[0].client_upload_id,
              upload_url: "https://storage.example/editor-queued",
              gcs_path: queued.gcs_path,
              expires_at: new Date(Date.now() + 60_000).toISOString(),
              upload_headers: { "x-goog-if-generation-match": "0" },
            },
          ],
        });
      }
      if (method === "PUT" && url === "https://storage.example/editor-queued") {
        return jsonResponse({});
      }
      if (method === "POST" && url.endsWith("/assets")) return jsonResponse(queued);
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    await renderShell(makeVariant([]));
    fireEvent.click(screen.getByRole("button", { name: "Visuals tool" }));
    fireEvent.change(screen.getByLabelText("Upload images or videos"), {
      target: { files: [new File(["image"], "queued.png", { type: "image/png" })] },
    });

    expect(await screen.findByText("Queued for analysis…")).toBeInTheDocument();
    await act(async () => releaseInitial?.(jsonResponse({ assets: [], max_assets: 20 })));
    expect(screen.getByText("Queued for analysis…")).toBeInTheDocument();
    const readyButton = await screen.findByRole(
      "button",
      { name: "Select queued.png" },
      { timeout: 4_000 },
    );
    expect(readyButton.querySelector("img")).toHaveAttribute(
      "src",
      "https://storage.googleapis.com/nova/users/u/plan/item-1/pool/queued.heic.preview.jpg?signature=preview",
    );
    expect(listCalls).toBeGreaterThanOrEqual(2);
  });
});
