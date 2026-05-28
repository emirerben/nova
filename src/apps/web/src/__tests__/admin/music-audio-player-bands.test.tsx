// @ts-nocheck
/**
 * Tests the AudioPlayer per-band 0-slot warning marker introduced by the
 * "Admin update failed: 422" fix.
 *
 * The marker mirrors the backend PATCH validator: if clicking a band would
 * auto-fill the form into a state that 422s on Save (via
 * music_recipe.count_slots == 0), the band gets:
 *   - data-zero-slot="true" attribute (testable hook)
 *   - a <title> tooltip: "Would produce 0 slots at N=… — lower N or pick a wider band"
 *
 * The matching JS port (countSlotsClient) is pinned to the Python
 * implementation by __tests__/lib/music-slot-count.test.ts; this file
 * focuses on the visual surface (the marker rendering).
 */
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

import { AudioPlayer } from "@/app/admin/music/[id]/components/AudioPlayer";
import type { SongSection } from "@/lib/music-api";

jest.mock("@/lib/music-api", () => {
  const actual = jest.requireActual("@/lib/music-api");
  return {
    ...actual,
    adminGetAudioUrl: jest.fn().mockResolvedValue("https://example.com/audio.mp3"),
  };
});

const SPARSE_BEATS = Array.from({ length: 62 }, (_, i) => (i * 200) / 62);

const tightSection: SongSection = {
  rank: 1,
  start_s: 56.2,
  end_s: 73.4,
  label: "chorus",
  energy: "high",
  suggested_use: "hook",
  rationale: "Peak chorus.",
};

const wideSection: SongSection = {
  rank: 2,
  start_s: 0,
  end_s: 200,
  label: "intro",
  energy: "medium",
  suggested_use: "build",
  rationale: "Full track.",
};

function renderPlayer(props: Partial<React.ComponentProps<typeof AudioPlayer>> = {}) {
  return render(
    <AudioPlayer
      trackId="t1"
      beats={SPARSE_BEATS}
      duration={200}
      start={0}
      end={0}
      sections={[tightSection, wideSection]}
      slotEveryN={8}
      onStartChange={() => {}}
      onEndChange={() => {}}
      {...props}
    />,
  );
}

describe("AudioPlayer — per-band 0-slot warning", () => {
  test("0-slot band gets data-zero-slot=true + <title> tooltip at N=8", async () => {
    renderPlayer({ slotEveryN: 8 });

    const tightBand = await screen.findByTestId("section-band-1");
    expect(tightBand).toHaveAttribute("data-zero-slot", "true");
    expect(tightBand.querySelector("title")?.textContent).toMatch(
      /Would produce 0 slots at N=8/,
    );
  });

  test("compatible band has no warning at N=8", async () => {
    renderPlayer({ slotEveryN: 8 });

    const wideBand = await screen.findByTestId("section-band-2");
    expect(wideBand).toHaveAttribute("data-zero-slot", "false");
    expect(wideBand.querySelector("title")).toBeNull();
  });

  test("lowering slotEveryN clears the warning on tight band when it becomes compatible", async () => {
    const { rerender } = renderPlayer({ slotEveryN: 8 });
    const band = await screen.findByTestId("section-band-1");
    expect(band).toHaveAttribute("data-zero-slot", "true");

    // At N=2, even 5 beats in window produces ≥1 slot.
    rerender(
      <AudioPlayer
        trackId="t1"
        beats={SPARSE_BEATS}
        duration={200}
        start={0}
        end={0}
        sections={[tightSection, wideSection]}
        slotEveryN={2}
        onStartChange={() => {}}
        onEndChange={() => {}}
      />,
    );
    expect(screen.getByTestId("section-band-1")).toHaveAttribute(
      "data-zero-slot",
      "false",
    );
  });

  test("default slotEveryN=8 when prop omitted (back-compat)", async () => {
    render(
      <AudioPlayer
        trackId="t1"
        beats={SPARSE_BEATS}
        duration={200}
        start={0}
        end={0}
        sections={[tightSection]}
        onStartChange={() => {}}
        onEndChange={() => {}}
      />,
    );
    // Sparse + tight → warning fires at the default N=8.
    const tightBand = await screen.findByTestId("section-band-1");
    expect(tightBand).toHaveAttribute("data-zero-slot", "true");
  });

  // D-2: the per-band <title> tooltip is hover-only and invisible to
  // touch + screen readers. The legend area below the SVG surfaces the
  // same warning info in two places:
  //   - Idle state: a summary listing the warned ranks (e.g. "#1 would 0-slot")
  //   - Hovered state: an explicit warning row inside the rationale card
  test("legend summary lists 0-slot bands in idle state (D-2 touch/a11y)", async () => {
    renderPlayer({ slotEveryN: 8 });
    // Wait for the bands to mount so the summary can render.
    await screen.findByTestId("section-band-1");
    const summary = screen.getByTestId("band-warn-summary");
    expect(summary).toHaveTextContent(/#1/);
    expect(summary).toHaveTextContent(/0-slot at N=8/);
  });

  test("summary disappears when no band would 0-slot at current N (D-2)", async () => {
    renderPlayer({ slotEveryN: 1 });
    await screen.findByTestId("section-band-1");
    // At N=1, the tight section is recipe-compatible → no warning summary.
    expect(screen.queryByTestId("band-warn-summary")).toBeNull();
  });
});
