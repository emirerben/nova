// @ts-nocheck
/**
 * Pins the rank-1 precedence rule in the admin music page's AudioPlayer:
 * when `best_sections` is non-empty, the legacy 45s wash band and the
 * "Set start" / "Set end" buttons disappear. The user sees only the
 * numbered ranked bands and the playhead.
 *
 * Matches the backend invariant in app/services/music_sections.py: once
 * the song_sections agent returns a usable rank-1, that section IS the
 * canonical "best section" — manual override UX is redundant noise.
 */
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { AudioPlayer } from "@/app/admin/music/[id]/page";

// Stub the admin audio-URL fetch so the component renders past its loading
// guard. The fake URL is never played.
jest.mock("@/lib/music-api", () => {
  const actual = jest.requireActual("@/lib/music-api");
  return {
    ...actual,
    adminGetAudioUrl: jest.fn().mockResolvedValue("blob:fake-audio"),
  };
});

const baseBeats = Array.from({ length: 240 }, (_, i) => (i + 1) * 0.5);

function renderWith(sections: any) {
  return render(
    <AudioPlayer
      trackId="track-1"
      beats={baseBeats}
      duration={180}
      start={30}
      end={50}
      sections={sections}
      onStartChange={() => {}}
      onEndChange={() => {}}
    />,
  );
}

test("renders legacy wash + Set start/end buttons when no sections", async () => {
  renderWith(null);
  // Audio URL is fetched async — wait for the SVG to render.
  expect(await screen.findByRole("button", { name: /Set start/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Set end/i })).toBeInTheDocument();
  // Helper text references the green/red markers that pair with the buttons.
  expect(screen.getByText(/green/i)).toBeInTheDocument();
  expect(screen.getByText(/red/i)).toBeInTheDocument();
});

test("hides legacy wash + Set start/end buttons when rank-1 section exists", async () => {
  const sections = [
    {
      rank: 1,
      start_s: 60,
      end_s: 78,
      label: "chorus",
      energy: "high",
      suggested_use: "hook",
      rationale: "peak energy chorus.",
    },
  ];
  renderWith(sections);
  // Wait for first hover-prompt to appear (signals the section bands rendered).
  expect(
    await screen.findByText(/Hover an agent band/i),
  ).toBeInTheDocument();
  // Legacy controls gone.
  expect(screen.queryByRole("button", { name: /Set start/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Set end/i })).not.toBeInTheDocument();
  // Helper text no longer mentions green/red markers.
  expect(screen.queryByText(/green/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/red/i)).not.toBeInTheDocument();
  // Beat count footer still rendered.
  expect(screen.getByText(/240 beats/i)).toBeInTheDocument();
});
