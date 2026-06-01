// @ts-nocheck
/**
 * Tests the /admin/music/[id] Timing config form behavior introduced by
 * the "Admin update failed: 422" fix:
 *
 *   - Slot-count badge color (green ≥1, amber 0)
 *   - Save button disabled when previewSlots === 0
 *   - Timing inputs declare step="any" (regression for the Turkish-locale
 *     HTML5 popup on fractional agent values like 56.25)
 *   - Lowering N in the form flips the badge from amber → green when the
 *     (window, N) combo becomes recipe-compatible
 *
 * Mocks the typed music-api client so we don't hit a real backend; the
 * test renders the page component with a Marea-style sparse-beats
 * fixture that 0-slots at N=8 but 1-slots at N=4.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";

import AdminMusicTrackPage from "@/app/admin/music/[id]/page";
import * as musicApi from "@/lib/music-api";

jest.mock("next/link", () => {
  function MockLink({
    children,
    href,
    ...rest
  }: {
    children: React.ReactNode;
    href: string;
  }) {
    return (
      <a href={href} {...rest}>
        {children}
      </a>
    );
  }
  MockLink.displayName = "MockLink";
  return { __esModule: true, default: MockLink };
});

const replaceMock = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: jest.fn() }),
  useSearchParams: () => new URLSearchParams(""),
}));

jest.mock("@/lib/music-api", () => ({
  __esModule: true,
  adminGetMusicTrack: jest.fn(),
  adminGetAudioUrl: jest.fn(),
  adminUpdateMusicTrack: jest.fn(),
  adminReanalyzeMusicTrack: jest.fn(),
  adminArchiveMusicTrack: jest.fn(),
}));

jest.mock("@/lib/admin-api", () => ({
  __esModule: true,
  adminCreateTemplateFromMusicTrack: jest.fn(),
}));

// LyricsConfigPanel hits the lyrics endpoints on mount; stub it.
jest.mock("@/app/admin/_shared/LyricsConfigPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="stub-lyrics-panel" />,
}));

const get = musicApi.adminGetMusicTrack as jest.MockedFunction<
  typeof musicApi.adminGetMusicTrack
>;
const audioUrl = musicApi.adminGetAudioUrl as jest.MockedFunction<
  typeof musicApi.adminGetAudioUrl
>;

// 5 beats in [56.2, 73.4] — the Feels Like / Marea pattern. With N=8 this
// 0-slots (PATCH would 422). With N=4 it 1-slots.
const SPARSE_BEATS = Array.from({ length: 62 }, (_, i) => (i * 200) / 62);

function makeTrack(
  overrides: Partial<musicApi.MusicTrackDetail> = {},
): musicApi.MusicTrackDetail {
  return {
    id: "t1",
    title: "Feels Like (test fixture)",
    artist: "Tame Impala",
    source_url: "upload://feels-like.mp3",
    audio_gcs_path: "music/feels-like.mp3",
    duration_s: 200,
    beat_count: SPARSE_BEATS.length,
    beat_timestamps_s: SPARSE_BEATS,
    analysis_status: "ready",
    error_detail: null,
    thumbnail_url: null,
    published_at: null,
    archived_at: null,
    track_config: {
      best_start_s: 56.2,
      best_end_s: 73.4,
      slot_every_n_beats: 8,
    },
    lyrics_status: "none",
    lyrics_source: null,
    lyrics_error_detail: null,
    lyrics_cached: null,
    lyrics_extracted_at: null,
    best_sections: [
      {
        rank: 1,
        start_s: 56.2,
        end_s: 73.4,
        label: "chorus",
        energy: "high",
        suggested_use: "hook",
        rationale: "Peak chorus.",
      },
    ],
    section_version: "2026-05-22",
    label_version: "2026-05-15",
    has_ai_labels: true,
    generative_matchable: true,
    created_at: "2026-05-01T00:00:00Z",
    ...overrides,
  } as musicApi.MusicTrackDetail;
}

beforeEach(() => {
  get.mockReset();
  audioUrl.mockReset();
  replaceMock.mockReset();
  audioUrl.mockResolvedValue("https://storage.example.com/audio.mp3");
});

async function renderPage() {
  let result: ReturnType<typeof render>;
  await act(async () => {
    result = render(<AdminMusicTrackPage params={{ id: "t1" }} />);
  });
  // Wait for the track fetch to resolve so the form renders.
  await screen.findByText(/Timing config/i);
  return result!;
}

describe("Admin Timing config — slot-count preview", () => {
  test("0-slot combo: badge amber, Save disabled", async () => {
    get.mockResolvedValue(makeTrack());
    await renderPage();

    const badge = screen.getByTestId("slot-count-badge");
    expect(badge).toHaveTextContent(/0 slots/);
    expect(badge.className).toMatch(/amber/);

    const saveBtn = screen.getByTestId("save-config-btn") as HTMLButtonElement;
    expect(saveBtn).toBeDisabled();
  });

  // Regression anchor: this is the test that would have caught the
  // original "Admin update failed: 422" bug. The fix is "Save is
  // disabled when the (window, N) combo would 0-slot," and that's only
  // meaningful if the disabled attribute actually blocks the network
  // call. Clicking a disabled button is a no-op in browsers, but if any
  // future refactor accidentally drops the `disabled` binding on the
  // button (or wires `onClick` directly without honoring it), this test
  // catches it.
  test("Save click while amber does NOT invoke adminUpdateMusicTrack (regression anchor)", async () => {
    get.mockResolvedValue(makeTrack());
    const update = musicApi.adminUpdateMusicTrack as jest.MockedFunction<
      typeof musicApi.adminUpdateMusicTrack
    >;
    update.mockReset();
    await renderPage();

    const saveBtn = screen.getByTestId("save-config-btn") as HTMLButtonElement;
    expect(saveBtn).toBeDisabled();
    fireEvent.click(saveBtn);
    expect(update).not.toHaveBeenCalled();
  });

  test("≥1-slot combo: badge green, Save enabled", async () => {
    // Wider window so 9 beats sit inside it.
    get.mockResolvedValue(
      makeTrack({
        track_config: {
          best_start_s: 0,
          best_end_s: 200,
          slot_every_n_beats: 8,
        },
      }),
    );
    await renderPage();

    const badge = screen.getByTestId("slot-count-badge");
    expect(badge).toHaveTextContent(/Would produce \d+ slots/);
    expect(badge.className).toMatch(/emerald/);

    const saveBtn = screen.getByTestId("save-config-btn") as HTMLButtonElement;
    expect(saveBtn).not.toBeDisabled();
  });

  test("lowering N from 8 → 4 flips amber → green and re-enables Save", async () => {
    get.mockResolvedValue(makeTrack());
    await renderPage();

    expect(screen.getByTestId("slot-count-badge")).toHaveTextContent(/0 slots/);
    expect(screen.getByTestId("save-config-btn")).toBeDisabled();

    // The N input renders default "8" — set it to 4 and let useMemo re-fire.
    const nInput = screen
      .getAllByRole("spinbutton")
      .find((el) => (el as HTMLInputElement).max === "32") as HTMLInputElement;
    fireEvent.change(nInput, { target: { value: "4" } });

    await waitFor(() => {
      expect(screen.getByTestId("slot-count-badge")).toHaveTextContent(/Would produce/);
    });
    expect(screen.getByTestId("save-config-btn")).not.toBeDisabled();
  });

  test('timing inputs declare step="any" (regression: locale popup on 56.25)', async () => {
    get.mockResolvedValue(makeTrack());
    await renderPage();

    const inputs = screen.getAllByRole("spinbutton") as HTMLInputElement[];
    // The two timing inputs are the ones whose value contains a dot (the N
    // input is integer-formatted). Filter by step.
    const stepAnyInputs = inputs.filter((el) => el.getAttribute("step") === "any");
    expect(stepAnyInputs).toHaveLength(2);

    // Setting 56.25 must not throw and must round-trip through React state.
    fireEvent.change(stepAnyInputs[0], { target: { value: "56.25" } });
    expect(stepAnyInputs[0].value).toBe("56.25");
  });

  // F1 regression: clearing an input used to fall back to the saved cfg
  // value, leaving the badge falsely green and Save enabled even though
  // submit would 500 on null. Submit shape now uses NaN for empty inputs
  // so previewSlots correctly reports 0.
  test("clearing best_start_s flips badge to amber + disables Save (F1 regression)", async () => {
    // Compatible saved cfg so the page loads green/enabled, then we clear
    // the start input and assert the UI flips to the failure state.
    get.mockResolvedValue(
      makeTrack({
        track_config: {
          best_start_s: 0,
          best_end_s: 200,
          slot_every_n_beats: 8,
        },
      }),
    );
    await renderPage();
    // Sanity check: starts green.
    expect(screen.getByTestId("slot-count-badge")).toHaveTextContent(/Would produce/);

    const startInput = screen.getByDisplayValue("0") as HTMLInputElement;
    fireEvent.change(startInput, { target: { value: "" } });

    await waitFor(() => {
      expect(screen.getByTestId("slot-count-badge")).toHaveTextContent(
        /Cannot save: 0 slots/,
      );
    });
    expect(screen.getByTestId("save-config-btn")).toBeDisabled();
  });

  // F4 escape hatch: when the loaded cfg 0-slots at the current N, the
  // page surfaces an inline "Try N=k" button that updates slotEveryN to
  // a compatible value. Pinned by the test below — adding new fallback
  // ranks (e.g. allowing N=3) requires updating both the suggestion
  // logic in page.tsx and the assertion here.
  test("0-slot cfg renders an inline Try N=k suggestion (F4 escape)", async () => {
    get.mockResolvedValue(makeTrack()); // 0-slot at N=8, sparse beats
    await renderPage();

    const apply = (await screen.findByTestId("apply-suggested-n")) as HTMLButtonElement;
    expect(apply).toHaveTextContent(/Try N=\d+/);

    fireEvent.click(apply);
    await waitFor(() => {
      expect(screen.getByTestId("slot-count-badge")).toHaveTextContent(/Would produce/);
    });
    expect(screen.getByTestId("save-config-btn")).not.toBeDisabled();
  });

  // D-1: 0-slot badge has a distinct chip treatment so it doesn't blur
  // into the lighter "Unsaved changes" badge. The chip carries
  // `bg-amber-500/15` + `border-amber-500/40` + the ⚠ glyph; the
  // Unsaved-changes badge is text-only at /70 opacity.
  test("0-slot badge is visually distinct from 'Unsaved changes' (D-1)", async () => {
    get.mockResolvedValue(makeTrack());
    await renderPage();

    const badge = screen.getByTestId("slot-count-badge");
    expect(badge.className).toMatch(/bg-amber-500\/15/);
    expect(badge.className).toMatch(/border-amber-500\/40/);
    expect(badge).toHaveTextContent(/Cannot save: 0 slots/);
    expect(badge).toHaveTextContent(/⚠/);
  });
});
