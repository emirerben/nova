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
import type { VariantEditSession } from "@/lib/variant-editor/useVariantEditSession";
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
    justSaved: false,
    isActive: false,
    draft: {
      text: "hello world",
      removed: false,
      styleSetId: "travel_editorial",
      sizePx: 56,
      layout: null,
      fontFamily: null,
      animation: null,
      textColor: null,
      clusterHeroFont: null,
      clusterBodyFont: null,
      clusterAccentFont: null,
      clusterHeroSizePx: null,
      clusterBodySizePx: null,
      clusterAccentSizePx: null,
    },
    isDirty: false,
    commitError: null,
    enterEdit: jest.fn(),
    cancel: jest.fn(),
    setText: jest.fn(),
    setRemoved: jest.fn(),
    setStyle: jest.fn(),
    setSize: jest.fn(),
    setLayout: jest.fn(),
    setFont: jest.fn(),
    setAnimation: jest.fn(),
    setColor: jest.fn(),
    setClusterHeroFont: jest.fn(),
    setClusterBodyFont: jest.fn(),
    setClusterAccentFont: jest.fn(),
    setClusterHeroSizePx: jest.fn(),
    setClusterBodySizePx: jest.fn(),
    setClusterAccentSizePx: jest.fn(),
    playToken: 0,
    replay: jest.fn(),
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

  it("includes cluster intros — the editorial geometry is now ported to TS", () => {
    expect(isInstantEditEligible(makeVariant({ intro_layout: "cluster" }))).toBe(true);
    expect(isInstantEditEligible(makeVariant({ intro_layout: "linear" }))).toBe(true);
    expect(isInstantEditEligible(makeVariant({ intro_layout: null }))).toBe(true);
  });

  it("excludes voiceover-synced sequence intros even when layout is cluster", () => {
    // A cluster can be sequence-synced — the sequence guard must run regardless
    // of intro_layout (text is transcript/rhythm-locked; the server 422s edits).
    expect(isInstantEditEligible(makeVariant({ intro_mode: "sequence" }))).toBe(false);
    expect(
      isInstantEditEligible(makeVariant({ intro_layout: "cluster", intro_mode: "sequence" })),
    ).toBe(false);
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
    const editorial = screen.getByRole("radio", { name: "Editorial layout" });
    expect(editorial).toBeDisabled(); // active = current layout, same as cluster
    expect(editorial).toHaveAttribute("title", "Editorial — text synced to this edit");
    expect(editorial).toHaveAttribute("aria-checked", "true");
    const classic = screen.getByRole("radio", { name: "Classic layout" });
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
    const editorial = screen.getByRole("radio", { name: "Editorial layout" });
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

  it("keeps the live preview + a quiet 'Saved' pulse after a text edit settles (W5)", () => {
    // justSaved keeps the editor card mounted past the commit so the card never
    // flashes to the burned output_url; the affordance is a brief "Saved" pulse,
    // not a blocking spinner.
    const session = makeSession({
      isEditing: false,
      isSaving: false,
      justSaved: true,
      isActive: false,
    });
    const { container } = render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ render_status: "ready" })}
        editSession={session}
      />,
    );

    // The live WYSIWYG preview (base video) is still on screen.
    expect(container.querySelector("video")!.getAttribute("src")).toBe(
      "https://x/base.mp4?sig=1",
    );
    expect(screen.getByText(/^saved$/i)).toBeInTheDocument();
    expect(screen.queryByText(/saving…/i)).not.toBeInTheDocument();
    // No blocking download/edit controls (still the editor surface, not the
    // settled card), and no "Rendering…" placeholder.
    expect(screen.queryByRole("button", { name: /download/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/^rendering…$/i)).not.toBeInTheDocument();
  });
});

describe("VariantCard layout preview cards (W3)", () => {
  it("renders Classic + Editorial preview cards in a radiogroup", () => {
    render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ intro_text: "what a view today" })}
        onChangeLayout={async () => {}}
      />,
    );
    expect(screen.getByRole("radiogroup", { name: "Intro text layout" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Classic layout" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Editorial layout" })).toBeInTheDocument();
  });

  it("gates Editorial + shows the hint for a hook outside 3-6 words", () => {
    render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ intro_text: "when they don't even listen to your feelings" })}
        onChangeLayout={async () => {}}
      />,
    );
    expect(screen.getByRole("radio", { name: "Editorial layout" })).toBeDisabled();
    expect(screen.getByText(/shorten the text to unlock it/i)).toBeInTheDocument();
  });

  it("fires onChangeLayout('cluster') when Editorial is picked for a short hook", async () => {
    const onChangeLayout = jest.fn(async () => {});
    render(
      <VariantCard
        {...baseProps}
        variant={makeVariant({ intro_text: "what a view today" })}
        onChangeLayout={onChangeLayout}
      />,
    );
    const editorial = screen.getByRole("radio", { name: "Editorial layout" });
    expect(editorial).toBeEnabled();
    await act(async () => {
      fireEvent.click(editorial);
    });
    expect(onChangeLayout).toHaveBeenCalledWith("cluster");
  });
});
