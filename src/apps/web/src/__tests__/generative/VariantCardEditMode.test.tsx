/**
 * VariantCard instant-edit mode:
 * - eligibility: base_video_url + agent_text/none → "Edit text & style";
 *   lyrics / legacy (no base) / no session → legacy controls
 * - edit mode plays the PINNED base video (src survives re-signed polls)
 * - admin compatibility: no editSession prop → legacy controls untouched
 */

// jsdom lacks ResizeObserver (used by IntroTextPreview).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { VariantCard, isInstantEditEligible } from "@/app/generative/VariantCard";
import type { VariantEditSession } from "@/app/generative/useVariantEditSession";
import type { GenerativeVariant } from "@/lib/generative-api";

function makeVariant(over: Partial<GenerativeVariant> = {}): GenerativeVariant {
  return {
    variant_id: "song_text",
    rank: 1,
    text_mode: "agent_text",
    music_track_id: "t1",
    track_title: "Track",
    style_set_id: "travel_editorial",
    output_url: "https://x/out.mp4",
    video_path: "generative-jobs/j/v.mp4",
    render_status: "ready",
    ok: true,
    error: null,
    intro_text_size_px: 56,
    intro_size_source: "computed",
    intro_text: "hello world",
    base_video_url: "https://x/base.mp4?sig=1",
    ...over,
  } as GenerativeVariant;
}

function makeSession(over: Partial<VariantEditSession> = {}): VariantEditSession {
  return {
    isEditing: false,
    isSaving: false,
    isActive: false,
    draft: { text: "hello world", removed: false, styleSetId: "travel_editorial", sizePx: 56 },
    isDirty: false,
    commitError: null,
    enterEdit: jest.fn(),
    cancel: jest.fn(),
    setText: jest.fn(),
    setRemoved: jest.fn(),
    setStyle: jest.fn(),
    setSize: jest.fn(),
    commit: jest.fn(async () => {}),
    ...over,
  };
}

const noop = async () => {};
const baseProps = {
  tracks: [],
  styleSets: [],
  onSwap: noop,
  onRetext: noop,
  onRemoveText: noop,
  onChangeStyle: noop,
};

describe("isInstantEditEligible", () => {
  it("requires a base video and an editable text mode", () => {
    expect(isInstantEditEligible(makeVariant())).toBe(true);
    expect(isInstantEditEligible(makeVariant({ text_mode: "none" }))).toBe(true);
    expect(isInstantEditEligible(makeVariant({ text_mode: "lyrics" }))).toBe(false);
    expect(isInstantEditEligible(makeVariant({ base_video_url: null }))).toBe(false);
  });
});

describe("VariantCard instant-edit entry", () => {
  it("shows the instant editor button for eligible variants with a session", () => {
    const session = makeSession();
    render(<VariantCard {...baseProps} variant={makeVariant()} editSession={session} />);

    const btn = screen.getByRole("button", { name: /edit text & style/i });
    fireEvent.click(btn);
    expect(session.enterEdit).toHaveBeenCalledTimes(1);
    // Legacy per-field controls are superseded.
    expect(screen.queryByRole("button", { name: /^edit text$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /remove text/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/smaller intro text/i)).not.toBeInTheDocument();
  });

  it("keeps legacy controls for lyrics variants even with a session", () => {
    render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ text_mode: "lyrics", base_video_url: null })}
        editSession={makeSession()}
      />,
    );
    expect(screen.getByRole("button", { name: /^edit text$/i })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /edit text & style/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps legacy controls when no session is provided (admin page)", () => {
    render(<VariantCard {...baseProps} variant={makeVariant()} />);
    expect(screen.getByRole("button", { name: /^edit text$/i })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /edit text & style/i }),
    ).not.toBeInTheDocument();
  });
});

describe("VariantCard edit mode", () => {
  it("plays the base video with the live overlay and toolbar while editing", () => {
    const session = makeSession({ isEditing: true, isActive: true });
    const { container } = render(
      <VariantCard {...baseProps} variant={makeVariant()} editSession={session} />,
    );

    const video = container.querySelector("video");
    expect(video).not.toBeNull();
    expect(video!.getAttribute("src")).toBe("https://x/base.mp4?sig=1");
    // Toolbar present (Done/Cancel), final-output controls absent.
    expect(screen.getByRole("button", { name: /done/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /download/i })).not.toBeInTheDocument();
  });

  it("pins the base src across polls that re-sign the URL", () => {
    const session = makeSession({ isEditing: true, isActive: true });
    const { container, rerender } = render(
      <VariantCard {...baseProps} variant={makeVariant()} editSession={session} />,
    );

    rerender(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ base_video_url: "https://x/base.mp4?sig=2" })}
        editSession={session}
      />,
    );
    const video = container.querySelector("video");
    expect(video!.getAttribute("src")).toBe("https://x/base.mp4?sig=1"); // unchanged
  });

  it("shows the Saving badge (no toolbar) while a committed render runs", () => {
    const session = makeSession({ isEditing: false, isSaving: true, isActive: true });
    render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ render_status: "rendering" })}
        editSession={session}
      />,
    );

    expect(screen.getByText(/saving…/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /done/i })).not.toBeInTheDocument();
    // The legacy "Rendering…" placeholder must NOT appear — the preview is up.
    expect(screen.queryByText(/^rendering…$/i)).not.toBeInTheDocument();
  });
});
