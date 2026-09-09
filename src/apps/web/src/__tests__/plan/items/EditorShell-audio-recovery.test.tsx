import "@testing-library/jest-dom";
import { act, render, waitFor } from "@testing-library/react";
import type { PlanItem, PlanItemVariant } from "@/lib/plan-api";

const mockUseVirtualPreview = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
}));

jest.mock("@/lib/music-api", () => ({
  ...jest.requireActual("@/lib/music-api"),
  getMusicTracks: jest.fn().mockResolvedValue({ tracks: [] }),
}));

jest.mock("@/lib/sfx-api", () => ({
  ...jest.requireActual("@/lib/sfx-api"),
  getSoundEffects: jest.fn().mockResolvedValue([]),
}));

jest.mock("@/app/plan/_components/useClipTimeline", () => ({
  useClipTimeline: () => ({
    state: {
      grid: [],
      clipDurations: { 0: 5 },
      baseline: [
        {
          key: "slot-1",
          slotId: "slot-1",
          clipIndex: 0,
          inS: 0,
          durationS: 4,
          durationBeats: null,
          removed: false,
          layout: "fullscreen",
          momentDescription: null,
        },
      ],
      slots: [
        {
          key: "slot-1",
          slotId: "slot-1",
          clipIndex: 0,
          inS: 0,
          durationS: 5,
          durationBeats: null,
          removed: false,
          layout: "fullscreen",
          momentDescription: null,
        },
      ],
      past: [],
      future: [],
      clampNonce: 0,
      clampedKey: null,
    },
    dispatch: jest.fn(),
    clips: [
      {
        clip_index: 0,
        signed_url: "https://cdn.example.test/source.mp4",
        kind: "video",
      },
    ],
    windows: [],
    totalS: 5,
    loadState: "ready",
    reload: jest.fn(),
  }),
}));

jest.mock("@/app/plan/items/[id]/_editor/useVirtualPreview", () => {
  const { buildVirtualTimeline } = jest.requireActual(
    "@/app/plan/items/[id]/_editor/virtual-timeline",
  ) as typeof import("@/app/plan/items/[id]/_editor/virtual-timeline");
  return {
    useVirtualPreview: (options: Record<string, unknown>) => {
      mockUseVirtualPreview(options);
      const slots = options.slots as Parameters<typeof buildVirtualTimeline>[0];
      const clips = options.clips as Parameters<typeof buildVirtualTimeline>[1];
      const grid = options.grid as Parameters<typeof buildVirtualTimeline>[2];
      const baseline = options.baselineSlots as Parameters<typeof buildVirtualTimeline>[4];
      return {
        timeline: buildVirtualTimeline(slots, clips, grid, null, baseline),
        activeDeck: "a",
        buffering: false,
        transitionPreview: null,
        play: jest.fn(),
        pause: jest.fn(),
        toggle: jest.fn(),
        seekTo: jest.fn(),
      };
    },
  };
});

jest.mock("@/app/plan/items/[id]/_editor/EditorCanvas", () => ({
  __esModule: true,
  default: () => null,
}));

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
const { getMusicTracks } = require("@/lib/music-api") as {
  getMusicTracks: typeof import("@/lib/music-api").getMusicTracks;
};
const mockGetMusicTracks = getMusicTracks as jest.MockedFunction<typeof getMusicTracks>;

const ITEM = {
  id: "item-1",
  theme: "Narrated edit",
  current_job_id: "job-1",
} as unknown as PlanItem;

const VARIANT = {
  variant_id: "narrated",
  output_url: "https://cdn.example.test/output.mp4",
  base_video_url: "https://cdn.example.test/base.mp4",
  render_status: "ready",
  text_mode: "agent_text",
  style_set_id: null,
  intro_text_size_px: null,
  text_elements: [],
  resolved_archetype: "guided_story",
  render_generation_id: "gen-current",
  render_receipt: { narration_applied: true },
  editor_capabilities: {
    text_elements: true,
    timeline: true,
    split_clips: true,
    mix: true,
    sfx: true,
    overlays: true,
    visual_blocks: true,
    suggestions: false,
    reason: null,
  },
} as unknown as PlanItemVariant;

let statusVariant = VARIANT;

beforeEach(() => {
  statusVariant = VARIANT;
  mockUseVirtualPreview.mockClear();
  global.fetch = jest.fn().mockRejectedValue(new Error("fixture skips blob cache"));
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockImplementation(async () => ({
    variants: [statusVariant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>));
});

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell virtual audio recovery", () => {
  it("refreshes a narration URL once, then falls back from virtual preview", async () => {
    await act(async () => {
      render(<EditorShell itemId="item-1" variantParam="narrated" />);
    });

    await waitFor(() =>
      expect(mockUseVirtualPreview).toHaveBeenCalledWith(
        expect.objectContaining({
          enabled: true,
          musicAudioUrl: "https://cdn.example.test/base.mp4",
          musicTrackActive: true,
        }),
      ),
    );

    const firstOptions = mockUseVirtualPreview.mock.calls.at(-1)?.[0] as {
      onMusicError: () => void;
    };
    act(() => firstOptions.onMusicError());

    await waitFor(() => expect(mockGetPlanItem).toHaveBeenCalledTimes(2));

    const refreshedOptions = mockUseVirtualPreview.mock.calls.at(-1)?.[0] as {
      onMusicError: () => void;
    };
    act(() => refreshedOptions.onMusicError());

    await waitFor(() =>
      expect(mockUseVirtualPreview).toHaveBeenLastCalledWith(
        expect.objectContaining({ enabled: false }),
      ),
    );
    expect(mockGetPlanItem).toHaveBeenCalledTimes(2);
  });

  it("refreshes owner-scoped status when matched music falls outside the gallery", async () => {
    statusVariant = {
      ...VARIANT,
      render_receipt: { narration_applied: false },
      music_track_id: "unpublished-track",
      music_preview_url: "https://cdn.example.test/matched-music.m4a",
    } as unknown as PlanItemVariant;

    await act(async () => {
      render(<EditorShell itemId="item-1" variantParam="narrated" />);
    });

    await waitFor(() =>
      expect(mockUseVirtualPreview).toHaveBeenCalledWith(
        expect.objectContaining({
          enabled: true,
          musicAudioUrl: "https://cdn.example.test/matched-music.m4a",
          musicTrackActive: true,
        }),
      ),
    );

    const options = mockUseVirtualPreview.mock.calls.at(-1)?.[0] as {
      onMusicError: () => void;
    };
    act(() => options.onMusicError());

    await waitFor(() => expect(mockGetPlanItem).toHaveBeenCalledTimes(2));
    expect(mockGetMusicTracks).toHaveBeenCalled();
  });

  it("refreshes an explicit source bed once, then falls back safely", async () => {
    statusVariant = {
      ...VARIANT,
      render_receipt: { narration_applied: false },
      source_audio_mix: "source_a",
      source_audio_options: [
        {
          mix: "source_a",
          audio_path: "source-a.m4a",
          audio_url: "https://cdn.example.test/source-a.m4a",
          duration_s: 5,
        },
      ],
    } as unknown as PlanItemVariant;

    await act(async () => {
      render(<EditorShell itemId="item-1" variantParam="narrated" />);
    });

    await waitFor(() =>
      expect(mockUseVirtualPreview).toHaveBeenCalledWith(
        expect.objectContaining({
          enabled: true,
          musicAudioUrl: "https://cdn.example.test/source-a.m4a",
          musicTrackActive: true,
        }),
      ),
    );

    const firstOptions = mockUseVirtualPreview.mock.calls.at(-1)?.[0] as {
      onMusicError: () => void;
    };
    act(() => firstOptions.onMusicError());
    await waitFor(() => expect(mockGetPlanItem).toHaveBeenCalledTimes(2));

    const refreshedOptions = mockUseVirtualPreview.mock.calls.at(-1)?.[0] as {
      onMusicError: () => void;
    };
    act(() => refreshedOptions.onMusicError());

    await waitFor(() =>
      expect(mockUseVirtualPreview).toHaveBeenLastCalledWith(
        expect.objectContaining({ enabled: false }),
      ),
    );
    expect(mockGetPlanItem).toHaveBeenCalledTimes(2);
  });
});
