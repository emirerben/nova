// @ts-nocheck
/**
 * Pins the rank-1 precedence rule in the admin music page's AudioPlayer:
 * when `best_sections` is non-empty, the legacy 45s wash band and the
 * "Set start" / "Set end" buttons disappear. The user sees only the
 * numbered ranked bands and the playhead.
 *
 * Also pins the click-to-select behavior: clicking a ranked section band
 * fires both the audio preview AND the onStartChange / onEndChange
 * callbacks (so the page's form state snaps to that section's bounds).
 * The selected band gets a thicker stroke + ✓ prefix as visual feedback.
 *
 * Matches the backend invariant in app/services/music_sections.py: once
 * the song_sections agent returns a usable rank-1, that section IS the
 * canonical "best section" — manual override UX is redundant noise.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { AudioPlayer } from "@/app/admin/music/[id]/components/AudioPlayer";

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

// Three ranked sections used by the click-to-select tests below. Distinct
// start/end so we can assert the right section's bounds came through.
const threeSections = [
  {
    rank: 1,
    start_s: 60,
    end_s: 78,
    label: "chorus",
    energy: "high",
    suggested_use: "hook",
    rationale: "peak energy chorus.",
  },
  {
    rank: 2,
    start_s: 100,
    end_s: 118,
    label: "bridge",
    energy: "medium",
    suggested_use: "build",
    rationale: "mid-energy bridge.",
  },
  {
    rank: 3,
    start_s: 140,
    end_s: 156,
    label: "verse",
    energy: "medium",
    suggested_use: "transition",
    rationale: "second verse.",
  },
];

function renderWith(
  sections: any,
  overrides: { start?: number; end?: number; onStartChange?: any; onEndChange?: any } = {},
) {
  return render(
    <AudioPlayer
      trackId="track-1"
      beats={baseBeats}
      duration={180}
      start={overrides.start ?? 30}
      end={overrides.end ?? 50}
      sections={sections}
      onStartChange={overrides.onStartChange ?? (() => {})}
      onEndChange={overrides.onEndChange ?? (() => {})}
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
  renderWith(threeSections.slice(0, 1));
  // Wait for the hover prompt to appear (signals the section bands rendered).
  expect(
    await screen.findByText(/Hover for rationale/i),
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

test("clicking a section band fires onStartChange + onEndChange with that band's bounds", async () => {
  const onStartChange = jest.fn();
  const onEndChange = jest.fn();
  renderWith(threeSections, { onStartChange, onEndChange });
  // Wait for bands to render.
  await screen.findByText(/Hover for rationale/i);

  const band2 = screen.getByTestId("section-band-2");
  fireEvent.click(band2);

  expect(onStartChange).toHaveBeenCalledTimes(1);
  expect(onStartChange).toHaveBeenCalledWith(100);
  expect(onEndChange).toHaveBeenCalledTimes(1);
  expect(onEndChange).toHaveBeenCalledWith(118);
});

test("clicking a band still triggers audio preview (calls play)", async () => {
  // Spy on the HTMLMediaElement play before render so the component picks it up.
  const playSpy = jest
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockImplementation(() => Promise.resolve());
  try {
    renderWith(threeSections);
    await screen.findByText(/Hover for rationale/i);

    fireEvent.click(screen.getByTestId("section-band-3"));

    expect(playSpy).toHaveBeenCalled();
  } finally {
    playSpy.mockRestore();
  }
});

test("isSelected band gets thicker stroke when form bounds match", async () => {
  // start/end match section #2 exactly.
  renderWith(threeSections, { start: 100, end: 118 });
  await screen.findByText(/Hover for rationale/i);

  // The <rect> is the first child of the band <g>. Reading strokeWidth via
  // getAttribute returns the SVG attribute value as a string.
  const band1Rect = screen.getByTestId("section-band-1").querySelector("rect");
  const band2Rect = screen.getByTestId("section-band-2").querySelector("rect");
  const band3Rect = screen.getByTestId("section-band-3").querySelector("rect");

  expect(band1Rect?.getAttribute("stroke-width")).toBe("1");
  expect(band2Rect?.getAttribute("stroke-width")).toBe("3");
  expect(band3Rect?.getAttribute("stroke-width")).toBe("1");
});

test("isSelected label gets ✓ prefix when form bounds match", async () => {
  renderWith(threeSections, { start: 100, end: 118 });
  await screen.findByText(/Hover for rationale/i);

  // Narrow band: 18s window / 180s duration × 700px W = 70px wide. Below
  // the 110px threshold for the long label, so the short branch renders
  // "✓#2" instead of "✓ #2 · bridge · medium".
  expect(screen.getByText("✓#2")).toBeInTheDocument();
  // Unselected bands keep their plain rank label.
  expect(screen.getByText("#1")).toBeInTheDocument();
  expect(screen.getByText("#3")).toBeInTheDocument();
});

test("isSelected tolerates 0.3s drift but not 0.6s drift", async () => {
  // 0.3s drift → within the 0.5s tolerance → band #2 still selected.
  const { unmount } = renderWith(threeSections, { start: 100.3, end: 118 });
  await screen.findByText(/Hover for rationale/i);
  expect(
    screen.getByTestId("section-band-2").querySelector("rect")?.getAttribute("stroke-width"),
  ).toBe("3");
  unmount();

  // 0.6s drift → outside tolerance → band #2 falls back to default stroke.
  renderWith(threeSections, { start: 100.6, end: 118 });
  await screen.findByText(/Hover for rationale/i);
  expect(
    screen.getByTestId("section-band-2").querySelector("rect")?.getAttribute("stroke-width"),
  ).toBe("1");
});

test("rapid 'Play section' clicks (manual config flow) also clean up listeners", async () => {
  // Symmetric to the band-click cleanup test below. playSection is the
  // manual-config path used when no agent sections exist; it shares the
  // sectionEndListenerRef with playAgentSection, so a regression in one
  // path would silently break the other. This test pins the manual path.
  const playSpy = jest
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockImplementation(() => Promise.resolve());
  const removeSpy = jest.spyOn(window.HTMLMediaElement.prototype, "removeEventListener");
  try {
    renderWith(null); // no sections → manual config UI with "Play section" button
    const playButton = await screen.findByRole("button", { name: /Play section/i });

    fireEvent.click(playButton);
    fireEvent.click(playButton);

    const removes = removeSpy.mock.calls.filter(([type]) => type === "timeupdate").length;
    expect(removes).toBeGreaterThanOrEqual(1);
  } finally {
    playSpy.mockRestore();
    removeSpy.mockRestore();
  }
});

test("rapid band clicks remove the prior timeupdate listener (no stacked pauses)", async () => {
  // Pins the fix for stacked listeners in playAgentSection. Without the
  // sectionEndListenerRef cleanup, the first click's checkEnd would still
  // be attached when the second click runs; the first checkEnd would then
  // fire as soon as audio.currentTime jumps past the FIRST band's end_s
  // (which happens immediately when the second click sets currentTime to
  // a later section) and pause the audio mid-preview.
  const playSpy = jest
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockImplementation(() => Promise.resolve());
  const addSpy = jest.spyOn(window.HTMLMediaElement.prototype, "addEventListener");
  const removeSpy = jest.spyOn(window.HTMLMediaElement.prototype, "removeEventListener");
  try {
    renderWith(threeSections);
    await screen.findByText(/Hover for rationale/i);

    fireEvent.click(screen.getByTestId("section-band-1"));
    fireEvent.click(screen.getByTestId("section-band-2"));

    // The invariant: when the second click runs, it must remove the first
    // click's checkEnd listener before adding its own. Without the fix,
    // zero timeupdate removes happen during the test (the listeners only
    // self-remove when they fire, which jsdom does not simulate). With the
    // fix, the second click cleans up the first.
    const removes = removeSpy.mock.calls.filter(([type]) => type === "timeupdate").length;
    expect(removes).toBeGreaterThanOrEqual(1);
  } finally {
    playSpy.mockRestore();
    addSpy.mockRestore();
    removeSpy.mockRestore();
  }
});

test("isSelected works for sections starting at 0.0s (falsy-zero guard)", async () => {
  // Pins AudioPlayer's handling of start=0 specifically — sections starting
  // at the song's opening must be matchable, not silently rejected as falsy.
  // The page.tsx falsy-zero fix (parseFloat(bestStart) || fallback →
  // bestStart === "" ? fallback : parseFloat(bestStart)) is what feeds 0
  // through to this component; this test guards the downstream behavior.
  const earlySections = [
    {
      rank: 1,
      start_s: 0,
      end_s: 10,
      label: "intro",
      energy: "low",
      suggested_use: "ambient",
      rationale: "opening tag.",
    },
  ];
  renderWith(earlySections, { start: 0, end: 10 });
  await screen.findByText(/Hover for rationale/i);
  expect(
    screen.getByTestId("section-band-1").querySelector("rect")?.getAttribute("stroke-width"),
  ).toBe("3");
});
