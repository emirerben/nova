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

import { act, render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { VariantCard, isInstantEditEligible } from "@/app/generative/VariantCard";
import type { VariantEditSession } from "@/app/generative/useVariantEditSession";
import { SEQUENCE_TEXT_LOCKED_HINT, type GenerativeVariant } from "@/lib/generative-api";

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

  it("excludes cluster intros — the TS mirror only models the linear layout", () => {
    expect(isInstantEditEligible(makeVariant({ intro_layout: "cluster" }))).toBe(false);
    expect(isInstantEditEligible(makeVariant({ intro_layout: "linear" }))).toBe(true);
    expect(isInstantEditEligible(makeVariant({ intro_layout: null }))).toBe(true);
  });

  it("excludes voiceover-synced sequence intros — text edits are server-rejected", () => {
    expect(isInstantEditEligible(makeVariant({ intro_mode: "sequence" }))).toBe(false);
    expect(isInstantEditEligible(makeVariant({ intro_mode: "linear" }))).toBe(true);
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

describe("VariantCard sequence-synced gating (intro_mode === 'sequence', D6/D19)", () => {
  const sequenceVariant = makeVariant({
    intro_mode: "sequence",
    sequence_synced: true,
    intro_layout: "cluster",
    // 8 words — would word-count-block Editorial on a non-synced variant.
    intro_text: "when they don't even listen to your feelings",
  });

  it("shows the Editorial · synced badge only on sequence variants", () => {
    const { rerender } = render(<VariantCard {...baseProps} variant={sequenceVariant} />);
    expect(screen.getByText("Editorial · synced")).toBeInTheDocument();
    rerender(<VariantCard {...baseProps} variant={makeVariant()} />);
    expect(screen.queryByText("Editorial · synced")).toBeNull();
  });

  it("locks text edits with the synced tooltip; size nudge stays enabled", () => {
    render(
      <VariantCard
        {...baseProps}
        variant={sequenceVariant}
        onResize={async () => {}}
      />,
    );
    const edit = screen.getByRole("button", { name: /^edit text$/i });
    expect(edit).toBeDisabled();
    expect(edit).toHaveAttribute("title", SEQUENCE_TEXT_LOCKED_HINT);
    expect(screen.getByRole("button", { name: /remove text/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Bigger intro text" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Smaller intro text" })).toBeEnabled();
  });

  it("keeps legacy controls (no instant editor) even with a session", () => {
    render(
      <VariantCard {...baseProps} variant={sequenceVariant} editSession={makeSession()} />,
    );
    expect(screen.queryByRole("button", { name: /edit text & style/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^edit text$/i })).toBeDisabled();
  });

  it("renders Editorial active without the word-count gate; Classic opts out", async () => {
    const onChangeLayout = jest.fn(async () => {});
    render(
      <VariantCard {...baseProps} variant={sequenceVariant} onChangeLayout={onChangeLayout} />,
    );
    const editorial = screen.getByRole("button", { name: "Editorial" });
    expect(editorial).toBeDisabled(); // active = current layout, same as cluster
    expect(editorial).toHaveAttribute("title", "Editorial — text synced to this edit");
    const classic = screen.getByRole("button", { name: "Classic" });
    expect(classic).toBeEnabled();
    await act(async () => {
      fireEvent.click(classic);
    });
    expect(onChangeLayout).toHaveBeenCalledWith("linear");
  });

  it("legacy variants without intro_mode keep the word-count gate and free text edits", () => {
    const onChangeLayout = jest.fn(async () => {});
    render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({
          intro_text: "when they don't even listen to your feelings",
        })}
        onChangeLayout={onChangeLayout}
      />,
    );
    expect(screen.queryByText("Editorial · synced")).toBeNull();
    expect(screen.getByRole("button", { name: /^edit text$/i })).toBeEnabled();
    const editorial = screen.getByRole("button", { name: "Editorial" });
    expect(editorial).toBeDisabled(); // 8-word hook → gate intact
    expect(editorial).toHaveAttribute(
      "title",
      "Editorial layout needs a 3-6 word hook — shorten the text first",
    );
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
