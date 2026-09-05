import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import EditProposalCard from "@/app/plan/items/[id]/components/EditProposalCard";
import {
  approveEditProposal,
  confirmEditDirection,
  draftEditProposal,
  editProposalConversationTurn,
  updateEditProposal,
  PlanApiError,
  type EditProposal,
  type PlanItem,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => {
  class PlanApiError extends Error {
    status: number;
    code: string;
    retryable: boolean;
    constructor({
      message,
      status = 500,
      code = "request_failed",
      retryable = false,
    }: {
      message: string;
      status?: number;
      code?: string;
      retryable?: boolean;
    }) {
      super(message);
      this.name = "PlanApiError";
      this.status = status;
      this.code = code;
      this.retryable = retryable;
    }
  }
  return {
    draftEditProposal: jest.fn(),
    editProposalConversationTurn: jest.fn(),
    updateEditProposal: jest.fn(),
    approveEditProposal: jest.fn(),
    confirmEditDirection: jest.fn(),
    PlanApiError,
  };
});

const mockDraft = draftEditProposal as jest.MockedFunction<typeof draftEditProposal>;
const mockConversation = editProposalConversationTurn as jest.MockedFunction<
  typeof editProposalConversationTurn
>;
const mockUpdate = updateEditProposal as jest.MockedFunction<typeof updateEditProposal>;
const mockApprove = approveEditProposal as jest.MockedFunction<typeof approveEditProposal>;
const mockConfirmDirection = confirmEditDirection as jest.MockedFunction<
  typeof confirmEditDirection
>;

function openSelect(trigger: HTMLElement) {
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
}

function snapshot() {
  return {
    direction: "guided_story" as const,
    goal: "Share what stood out",
    pace: "balanced" as const,
    duration_s: 24,
    title: "What I noticed in Corfu",
    media: [
      {
        lane: "asset" as const,
        media_id: "photo-1",
        gcs_path: "users/u/plan/i/pool/food.jpg",
        generation: "1",
        kind: "image" as const,
        source_filename: "food.jpg",
        user_context: "",
        analysis: {},
        preview_url: "https://cdn.example/food.jpg",
      },
      {
        lane: "clip" as const,
        media_id: "video-1",
        gcs_path: "users/u/plan/i/coast.mp4",
        generation: "2",
        kind: "video" as const,
        source_filename: "coast.mp4",
        user_context: "",
        analysis: {},
      },
    ],
    story_beats: [
      {
        beat_id: "beat-food",
        topic: "Food",
        thought: "The colors made every stop feel inviting.",
        thought_source: "ai_draft" as const,
        media_ids: ["photo-1"],
        layout: "fullscreen" as const,
        duration_s: 4,
      },
      {
        beat_id: "beat-coast",
        topic: "Coast",
        thought: "The coastline changed the rhythm.",
        thought_source: "ai_draft" as const,
        media_ids: ["video-1"],
        layout: "fullscreen" as const,
        duration_s: 4,
      },
    ],
  };
}

function proposal(status: EditProposal["status"] = "draft"): EditProposal {
  const draft = snapshot();
  return {
    schema_version: 1,
    proposal_version: 2,
    generation_attempt_id: "attempt-1",
    media_digest: "a".repeat(64),
    status,
    brief: { direction: "guided_story", goal: draft.goal, pace: "balanced", duration_s: 24 },
    conversation: [],
    brief_ready: false,
    draft,
    last_approved: null,
    failure: null,
  };
}

function item(editProposal: EditProposal | null): PlanItem {
  return {
    id: "item-1",
    day_index: null,
    theme: "Corfu",
    idea: "Corfu trip",
    position: 0,
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: ["users/u/plan/i/coast.mp4"],
    clip_assignments: [],
    status: "awaiting_clips",
    current_job_id: null,
    user_edited: false,
    landscape_fit: "fit",
    edit_proposal: editProposal,
    guided_edit_available: true,
    guided_edit_conversation_available: true,
  };
}

describe("EditProposalCard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("keeps the existing direction form while conversation writes are dark", async () => {
    const legacyItem = {
      ...item(null),
      guided_edit_conversation_available: false,
    };
    mockDraft.mockResolvedValue(item({ ...proposal(), status: "analyzing" }));
    render(<EditProposalCard item={legacyItem} onChanged={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Plan edit" }));
    expect(screen.getByText("Edit direction")).toBeInTheDocument();
    expect(screen.queryByLabelText("Tell Kria what you want in the edit")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: /Fast montage/ }));
    fireEvent.change(screen.getByLabelText("Goal or context"), {
      target: { value: "Keep it quick and food-focused" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Build edit plan" }));

    await waitFor(() => {
      expect(mockDraft).toHaveBeenCalledWith(
        "item-1",
        expect.objectContaining({
          direction: "fast_montage",
          goal: "Keep it quick and food-focused",
        }),
      );
    });
    expect(mockConversation).not.toHaveBeenCalled();
  });

  it("offers a legacy replan path when semantic fields are read-only and chat is dark", () => {
    const legacyItem = {
      ...item(proposal()),
      guided_edit_conversation_available: false,
    };
    render(<EditProposalCard item={legacyItem} onChanged={jest.fn()} />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "Start a new plan to change direction, pace, or length",
      }),
    );

    expect(screen.getByText("Edit direction")).toBeInTheDocument();
  });

  it("shows the inferred direction and confirms it with proposal-version protection", async () => {
    const awaiting = proposal("briefing");
    awaiting.guidance = {
      state: "awaiting_direction_confirmation",
      provenance: "ai_inferred",
      fingerprint: "f".repeat(64),
      hypothesis: {
        direction: "fast_montage",
        pace: "fast",
        duration_s: 15,
        text_density: "minimal",
        audio_role: "music_led",
        rationale: "The footage is strongest as a quick visual highlight reel.",
        buildability_warnings: ["Only one source has enough motion for the hook."],
      },
    };
    const analyzing = item({
      ...awaiting,
      status: "analyzing",
      proposal_version: awaiting.proposal_version + 1,
      guidance: { ...awaiting.guidance, state: "confirmed", provenance: "creator_confirmed" },
    });
    mockConfirmDirection.mockResolvedValue(analyzing);
    const onChanged = jest.fn();

    render(<EditProposalCard item={item(awaiting)} onChanged={onChanged} />);

    expect(screen.getByText("Is this the edit you want?")).toBeInTheDocument();
    expect(screen.getByText("Fast montage")).toBeInTheDocument();
    expect(screen.getByLabelText("Buildability notes")).toHaveTextContent("enough motion");
    fireEvent.click(screen.getByRole("button", { name: "Yes, build this edit" }));

    await waitFor(() => {
      expect(mockConfirmDirection).toHaveBeenCalledWith("item-1", 2, "f".repeat(64));
    });
    expect(onChanged).toHaveBeenCalledWith(analyzing);
  });

  it("collects natural-language direction and prevents a duplicate turn", async () => {
    let resolve!: (value: PlanItem) => void;
    const briefing = proposal("briefing");
    briefing.brief = {
      direction: "fast_montage",
      goal: "Show the town, food, and water",
      pace: "fast",
      duration_s: 20,
    };
    briefing.conversation = [
      { role: "user", content: "Make it quick and fun", suggestions: [] },
      {
        role: "agent",
        content: "I’ll make a quick, music-led highlight reel.",
        suggestions: ["Focus on the food", "Keep all three topics"],
      },
    ];
    mockConversation.mockReturnValue(new Promise((done) => { resolve = done; }));
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(null)} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "Plan edit" }));
    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Make it quick and fun" },
    });
    const send = screen.getByRole("button", { name: "Send direction" });
    fireEvent.click(send);
    fireEvent.click(send);

    expect(mockConversation).toHaveBeenCalledTimes(1);
    expect(mockConversation).toHaveBeenCalledWith("item-1", 0, "Make it quick and fun");
    await act(async () => resolve(item(briefing)));
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("restores the creator's words after a failed turn and allows retry", async () => {
    const briefing = proposal("briefing");
    mockConversation
      .mockRejectedValueOnce(new Error("Kria is temporarily unavailable"))
      .mockResolvedValueOnce(item(briefing));
    const onChanged = jest.fn();
    const onRefresh = jest.fn();
    render(
      <EditProposalCard item={item(null)} onChanged={onChanged} onRefresh={onRefresh} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Plan edit" }));
    const input = screen.getByLabelText("Tell Kria what you want in the edit");
    fireEvent.change(input, { target: { value: "Keep my notes about the food" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Kria couldn’t update your edit direction. Check your connection and try again.",
    );
    expect(input).toHaveValue("Keep my notes about the food");
    expect(onRefresh).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    await waitFor(() => expect(mockConversation).toHaveBeenCalledTimes(2));
    expect(onChanged).toHaveBeenCalledWith(item(briefing));
  });

  it("submits a suggested answer as a conversation turn", async () => {
    const briefing = proposal("briefing");
    mockConversation.mockResolvedValue(item(briefing));
    render(<EditProposalCard item={item(null)} onChanged={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Plan edit" }));
    fireEvent.click(screen.getByRole("button", { name: "A personal travel diary" }));

    await waitFor(() => {
      expect(mockConversation).toHaveBeenCalledWith("item-1", 0, "A personal travel diary");
    });
  });

  it("starts analysis from the brief Kria understood", async () => {
    const briefing = proposal("briefing");
    briefing.brief = {
      direction: "text_explainer",
      goal: "Explain the food and architecture",
      pace: "relaxed",
      duration_s: 30,
      mixed_media_timing: {
        image_hold: "very_fast",
        video_hold: "longer",
        boundary_style: "cut",
      },
    };
    briefing.brief_ready = true;
    briefing.conversation = [
      { role: "user", content: "Explain the food and architecture", suggestions: [] },
      { role: "agent", content: "I’ll build a slower explained story.", suggestions: [] },
    ];
    const analyzing = item({ ...briefing, status: "analyzing", proposal_version: 3 });
    mockDraft.mockResolvedValue(analyzing);
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(briefing)} onChanged={onChanged} />);

    expect(screen.queryByRole("button", { name: "A personal travel diary" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Build this edit plan" }));

    await waitFor(() => expect(mockDraft).toHaveBeenCalledWith("item-1", briefing.brief));
    expect(onChanged).toHaveBeenCalledWith(analyzing);
  });

  it("resumes a reloaded in-flight conversation, keeping the composer typeable but Send disabled", () => {
    const briefing = proposal("briefing");
    briefing.conversation_in_progress = true;
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent("Kria is thinking");
    expect(screen.getByLabelText("Tell Kria what you want in the edit")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Send direction" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Build this edit plan" })).toBeDisabled();
  });

  it("requires the creator to resend direction after an expired conversation", () => {
    const briefing = proposal("briefing");
    briefing.conversation_retry_required = true;
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent("Send your direction again");
    expect(screen.getByLabelText("Tell Kria what you want in the edit")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Build this edit plan" })).toBeDisabled();
  });

  it("marks AI thoughts, supports keyboard-operable ordering, and approves with CAS", async () => {
    const saved = item({ ...proposal(), proposal_version: 3 });
    const approved = item({ ...proposal("approved"), proposal_version: 4 });
    mockUpdate.mockResolvedValue(saved);
    mockApprove.mockResolvedValue(approved);
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(proposal())} onChanged={onChanged} />);

    expect(screen.getAllByText("AI draft")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Move Coast earlier" }));
    expect(screen.getByLabelText("Moment 1 topic")).toHaveValue("Coast");

    expect(screen.getByText("Direction").nextElementSibling).toHaveTextContent("Story with thoughts");
    expect(screen.getByText("Pace").nextElementSibling).toHaveTextContent("Balanced");
    expect(screen.getByText("Target length").nextElementSibling).toHaveTextContent("24 seconds");
    expect(
      screen.getByRole("button", { name: "Ask Kria to change direction, pace, or length" }),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Moment 1 topic"), {
      target: { value: "Sea and beaches" },
    });
    openSelect(screen.getAllByRole("combobox", { name: "Layout" })[0]);
    fireEvent.click(await screen.findByRole("option", { name: "Supporting card" }));
    fireEvent.change(screen.getAllByRole("textbox", { name: /Thought/ })[0], {
      target: { value: "I kept coming back to the water." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    const sent = mockUpdate.mock.calls[0][2];
    expect(sent.direction).toBe("guided_story");
    expect(sent.pace).toBe("balanced");
    expect(sent.duration_s).toBe(24);
    expect(sent.story_beats[0].topic).toBe("Sea and beaches");
    expect(sent.story_beats[0].layout).toBe("supporting_card");
    expect(sent.story_beats[0].thought_source).toBe("user");
    expect(sent.media[0]).not.toHaveProperty("preview_url");
    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith("item-1", 3));
    expect(onChanged).toHaveBeenLastCalledWith(approved);
  });

  it("shows a read-only conversational custom duration instead of falling back to 15 seconds", () => {
    const custom = proposal();
    custom.draft = { ...custom.draft!, duration_s: 10 };
    custom.brief = { ...custom.brief, duration_s: 10 };

    render(<EditProposalCard item={item(custom)} onChanged={jest.fn()} />);

    expect(screen.getByText("Target length").nextElementSibling).toHaveTextContent("10 seconds");
  });

  it("shows the source-aware cut program instead of editable compatibility beats", () => {
    const fast = proposal();
    fast.draft = {
      ...fast.draft!,
      direction: "fast_montage",
      pace: "fast",
      duration_s: 2,
      fast_cuts: [
        {
          cut_id: "cut-1",
          media_id: "video-1",
          source_start_s: 0.2,
          source_end_s: 1.2,
          output_duration_s: 1,
          role: "hook",
          transition: "none",
          beat_align: true,
        },
        {
          cut_id: "cut-2",
          media_id: "photo-1",
          source_start_s: 0,
          source_end_s: 1,
          output_duration_s: 1,
          role: "payoff",
          transition: "none",
          beat_align: false,
        },
      ],
    };

    render(<EditProposalCard item={item(fast)} onChanged={jest.fn()} />);

    expect(screen.getByText("Fast cut program")).toBeInTheDocument();
    expect(screen.getByText(/coast.mp4/)).toBeInTheDocument();
    expect(screen.getByText(/hook · 0.2–1.2s · hard cut/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Moment 1 topic")).toBeNull();
    expect(screen.getByText("Direction").nextElementSibling).toHaveTextContent("Fast montage");
    expect(screen.getByText("Target length").nextElementSibling).toHaveTextContent("2 seconds");
  });

  it("turns a replan-required save failure into an actionable Kria error", async () => {
    mockUpdate.mockRejectedValueOnce(
      new PlanApiError({
        message: "proposal_replan_required",
        status: 409,
        code: "proposal_replan_required",
      }),
    );

    render(<EditProposalCard item={item(proposal())} onChanged={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This montage needs a new cut plan. Ask Kria to replan the direction before approving.",
    );
  });

  it("counts only selected sources in the approved summary", () => {
    const approved = proposal("approved");
    approved.draft!.media.push({
      lane: "clip",
      media_id: "unused-video",
      gcs_path: "users/u/plan/i/unused.mov",
      generation: "3",
      kind: "video",
      source_filename: "unused.mov",
      user_context: "",
      analysis: {},
    });

    render(<EditProposalCard item={item(approved)} onChanged={jest.fn()} />);

    expect(screen.getByText("2 moments · 2 sources · about 24s")).toBeInTheDocument();
    expect(screen.queryByText(/3 sources/)).toBeNull();
  });

  it("surfaces a render failure on an approved plan with a re-plan affordance", () => {
    const approved = proposal("approved");
    approved.render_failure = {
      proposal_version: approved.proposal_version,
      code: "guided_story_duration_impossible",
      message: "This edit's timing doesn't fit your footage. Open the planner to shorten it or add more media.",
      attempts: 1,
      failed_at: "2026-08-21T10:00:00Z",
    };

    render(<EditProposalCard item={item(approved)} onChanged={jest.fn()} />);

    expect(
      screen.getByText(/This edit's timing doesn't fit your footage/),
    ).toBeInTheDocument();
    // The re-plan affordance stays reachable — the failure notice must never
    // hide it.
    expect(screen.getByRole("button", { name: "Edit plan" })).toBeVisible();
  });

  it("shows the drafting failure instead of a stale render_failure when both are present", () => {
    const approved = proposal("approved");
    approved.failure = {
      code: "proposal_generation_timeout",
      message: "Kria took too long to plan this edit. Try again.",
      retryable: true,
    };
    approved.render_failure = {
      proposal_version: approved.proposal_version,
      code: "guided_story_duration_impossible",
      message: "This edit's timing doesn't fit your footage. Open the planner to shorten it or add more media.",
      attempts: 1,
      failed_at: "2026-08-21T10:00:00Z",
    };

    render(<EditProposalCard item={item(approved)} onChanged={jest.fn()} />);

    expect(
      screen.getByText("Kria took too long to plan this edit. Try again."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/This edit's timing doesn't fit your footage/),
    ).toBeNull();
  });

  it("replaces an unplayable source preview with an honest file fallback", () => {
    const withPreview = proposal();
    withPreview.draft!.media[1] = {
      ...withPreview.draft!.media[1],
      preview_url: "https://cdn.example/coast.mov",
    };
    render(<EditProposalCard item={item(withPreview)} onChanged={jest.fn()} />);

    const preview = screen.getByLabelText("Coast: coast.mp4");
    fireEvent.error(preview);

    expect(screen.getByRole("img", { name: "Coast: coast.mp4: preview unavailable" }))
      .toBeInTheDocument();
  });

  it("preserves unsaved edits across same-version polling and resets for a new revision", () => {
    const onChanged = jest.fn();
    const view = render(<EditProposalCard item={item(proposal())} onChanged={onChanged} />);

    fireEvent.change(screen.getAllByRole("textbox", { name: /Thought/ })[0], {
      target: { value: "Lisbon" },
    });
    view.rerender(<EditProposalCard item={item(proposal())} onChanged={onChanged} />);

    expect(screen.getAllByRole("textbox", { name: /Thought/ })[0]).toHaveValue("Lisbon");

    const serverRevision = proposal();
    serverRevision.proposal_version = 3;
    serverRevision.draft!.story_beats[0].thought = "Server revision";
    view.rerender(<EditProposalCard item={item(serverRevision)} onChanged={onChanged} />);

    expect(screen.getAllByRole("textbox", { name: /Thought/ })[0]).toHaveValue(
      "Server revision",
    );
  });

  it("keeps the last approval visible when media makes the plan stale", () => {
    const stale = proposal("stale");
    stale.last_approved = {
      proposal_version: 2,
      media_digest: stale.media_digest!,
      approved_at: "2026-08-14T10:00:00Z",
      snapshot: snapshot(),
    };
    render(<EditProposalCard item={item(stale)} onChanged={jest.fn()} />);
    expect(screen.getByText(/Your uploads changed/)).toBeInTheDocument();
    expect(screen.getByText(/last approved plan.*What I noticed in Corfu/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build this edit plan" })).toBeVisible();
  });

  it("shows a failed attempt and retries from the preserved brief", async () => {
    const failed = proposal("failed");
    failed.failure = {
      code: "proposal_generation_timeout",
      message: "Kria took too long to plan this edit. Try again.",
      retryable: true,
    };
    const analyzing = item({
      ...failed,
      proposal_version: 3,
      status: "analyzing",
      failure: null,
    });
    mockDraft.mockResolvedValue(analyzing);
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(failed)} onChanged={onChanged} />);

    expect(screen.getByText(failed.failure.message)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try planning again" }));

    await waitFor(() => {
      expect(mockDraft).toHaveBeenCalledWith("item-1", failed.brief);
    });
    expect(onChanged).toHaveBeenCalledWith(analyzing);
  });

  it("continues the same thread when moving from briefing into draft review", async () => {
    const starting = proposal();
    starting.conversation = [
      { role: "user", phase: "briefing", content: "Make it reflective", suggestions: [] },
      {
        role: "agent",
        phase: "briefing",
        content: "I’m ready to draft a reflective plan.",
        suggestions: [],
      },
    ];
    const revised = proposal();
    revised.proposal_version = 3;
    revised.conversation = [
      ...starting.conversation,
      { role: "user", content: "Put food first and make it slower", suggestions: [] },
      {
        role: "agent",
        content: "I moved food first and slowed the pace.",
        suggestions: ["Use less text"],
      },
    ];
    revised.brief = { ...revised.brief, pace: "relaxed" };
    revised.draft = {
      ...revised.draft!,
      pace: "relaxed",
      story_beats: [...revised.draft!.story_beats].reverse(),
    };
    const revisedItem = item(revised);
    mockConversation.mockResolvedValue(revisedItem);
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(starting)} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "Tell Kria what to change" }));
    expect(screen.getByText("Shape the draft with Kria")).toBeInTheDocument();
    // Real chat thread: the prior briefing-phase turn stays visible instead of
    // being replaced by a fresh review greeting — Kria has already spoken.
    expect(screen.getByText("I’m ready to draft a reflective plan.")).toBeInTheDocument();
    expect(screen.queryByText(/What would you change about this draft/)).toBeNull();
    expect(screen.getByRole("button", { name: "Put food first" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Build this edit plan" })).toBeNull();
    expect(screen.getByRole("button", { name: "Close conversation" })).toBeVisible();
    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Put food first and make it slower" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    await waitFor(() => {
      expect(mockConversation).toHaveBeenCalledWith(
        "item-1",
        2,
        "Put food first and make it slower",
      );
    });
    expect(onChanged).toHaveBeenCalledWith(revisedItem);
  });

  it("renders every persisted turn as a bubble, in conversation order", () => {
    const starting = proposal();
    starting.conversation = [
      { role: "user", phase: "briefing", content: "Make it reflective", suggestions: [] },
      { role: "agent", phase: "briefing", content: "Got it — reflective it is.", suggestions: [] },
      { role: "user", phase: "review", content: "Prioritize the food scenes", suggestions: [] },
      { role: "agent", phase: "review", content: "I moved food first.", suggestions: [] },
    ];
    render(<EditProposalCard item={item(starting)} onChanged={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Tell Kria what to change" }));

    const order = [
      "Make it reflective",
      "Got it — reflective it is.",
      "Prioritize the food scenes",
      "I moved food first.",
    ].map((text) => screen.getByText(text));
    for (let i = 1; i < order.length; i += 1) {
      // eslint-disable-next-line no-bitwise
      expect(
        order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
  });

  it("saves manual draft edits before asking Kria for a revision", async () => {
    const starting = proposal();
    const savedProposal = {
      ...proposal(),
      proposal_version: 3,
      draft: { ...snapshot(), title: "My own Corfu title" },
    };
    const revisedProposal = {
      ...savedProposal,
      proposal_version: 4,
      conversation: [
        {
          role: "agent" as const,
          phase: "review" as const,
          content: "I kept your title and moved food first.",
          suggestions: [],
        },
      ],
    };
    mockUpdate.mockResolvedValue(item(savedProposal));
    mockConversation.mockResolvedValue(item(revisedProposal));
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(starting)} onChanged={onChanged} />);

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "My own Corfu title" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Tell Kria what to change" }));
    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Put food first" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith(
        "item-1",
        2,
        expect.objectContaining({ title: "My own Corfu title" }),
      );
    });
    await waitFor(() => {
      expect(mockConversation).toHaveBeenCalledWith("item-1", 3, "Put food first");
    });
    // P3 (review double-fire): the intermediate dirty-draft save must NOT
    // surface its own onChanged — only the turn's final response does, so a
    // single submit doesn't visibly flicker save → draft view → turn reply.
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(onChanged).toHaveBeenCalledWith(item(revisedProposal));
  });

  it("surfaces the dirty-draft save (not a duplicate onChanged) when the turn that follows it fails", async () => {
    const starting = proposal();
    const savedProposal = {
      ...proposal(),
      proposal_version: 3,
      draft: { ...snapshot(), title: "My own Corfu title" },
    };
    mockUpdate.mockResolvedValue(item(savedProposal));
    mockConversation.mockRejectedValueOnce(new Error("Kria is temporarily unavailable"));
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(starting)} onChanged={onChanged} />);

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "My own Corfu title" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Tell Kria what to change" }));
    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Put food first" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    await waitFor(() => {
      expect(mockConversation).toHaveBeenCalledWith("item-1", 3, "Put food first");
    });
    // The save landed even though the turn failed — the creator's retry
    // needs that CAS version, so it's the ONE onChanged call here.
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(onChanged).toHaveBeenCalledWith(item(savedProposal));
  });

  it("surfaces a saved draft when approval fails so retry advances from the new CAS version", async () => {
    const savedV3 = item({ ...proposal(), proposal_version: 3 });
    const savedV4 = item({ ...proposal(), proposal_version: 4 });
    const approvedV5 = item({ ...proposal("approved"), proposal_version: 5 });
    mockUpdate.mockResolvedValueOnce(savedV3).mockResolvedValueOnce(savedV4);
    mockApprove
      .mockRejectedValueOnce(new Error("Approval service unavailable"))
      .mockResolvedValueOnce(approvedV5);
    const onChanged = jest.fn();
    const view = render(<EditProposalCard item={item(proposal())} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(savedV3));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Kria couldn’t approve this plan. Check your connection and try again.",
    );

    view.rerender(
      <EditProposalCard item={savedV3} onChanged={onChanged} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenLastCalledWith(
      "item-1",
      3,
      expect.any(Object),
    ));
    await waitFor(() => expect(mockApprove).toHaveBeenLastCalledWith("item-1", 4));
    expect(onChanged).toHaveBeenLastCalledWith(approvedV5);
  });

  it("replaces the pending bubble with the durable turn once the parent applies onChanged", async () => {
    let resolveTurn!: (value: PlanItem) => void;
    mockConversation.mockReturnValue(new Promise((done) => { resolveTurn = done; }));
    const briefing = proposal("briefing");
    const view = render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Make it about the coastline" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    // Exactly one bubble while pending: the optimistic echo.
    expect(await screen.findByText("Make it about the coastline")).toBeInTheDocument();
    expect(screen.getAllByText("Make it about the coastline")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("Kria is thinking");

    const revised = proposal("briefing");
    revised.proposal_version = 3;
    revised.conversation = [
      { role: "user", content: "Make it about the coastline", suggestions: [] },
      { role: "agent", content: "Coastline it is — I'll lead with the coast.", suggestions: [] },
    ];
    await act(async () => resolveTurn(item(revised)));
    // The parent (page.tsx) applies the authoritative response via
    // usePolledJobStatus's applyData; simulate that here by re-rendering
    // with the item onChanged was called with.
    view.rerender(<EditProposalCard item={item(revised)} onChanged={jest.fn()} />);

    // The durable turn REPLACED the pending bubble — still exactly one
    // match for the creator's message, not zero (P1-1: a test that only
    // asserts the pending bubble disappeared can't tell "replaced" from
    // "clobbered back to nothing").
    expect(screen.getAllByText("Make it about the coastline")).toHaveLength(1);
    expect(
      screen.getByText("Coastline it is — I'll lead with the coast."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("drops the pending bubble, restores the composer text, and shows friendly copy for a raw request-failed error", async () => {
    mockConversation.mockRejectedValueOnce(new Error("Request failed (429)"));
    const briefing = proposal("briefing");
    const onRefresh = jest.fn();
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} onRefresh={onRefresh} />);

    const input = screen.getByLabelText("Tell Kria what you want in the edit");
    fireEvent.change(input, { target: { value: "Slow it down" } });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Kria couldn’t update your edit direction. Check your connection and try again.",
    );
    expect(input).toHaveValue("Slow it down");
    // The pending echo bubble is gone — the only match left is the textarea
    // itself (React renders a controlled textarea's value as a text child,
    // so getByText legitimately matches it too).
    expect(screen.queryAllByText("Slow it down")).toHaveLength(1);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("shows the empty-media nudge copy for a 409 media_required race", async () => {
    mockConversation.mockRejectedValueOnce(
      new PlanApiError({ message: "media_required", status: 409, code: "media_required" }),
    );
    const briefing = proposal("briefing");
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Go" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add a photo or video first — Kria plans from your real footage",
    );
  });

  it("shows an empty-media nudge and disables Send when there's no footage yet", () => {
    const briefing = proposal("briefing");
    const noMedia: PlanItem = { ...item(briefing), clip_gcs_paths: [], clip_assignments: [] };
    render(<EditProposalCard item={noMedia} onChanged={jest.fn()} />);

    expect(
      screen.getByText("Add a photo or video first — Kria plans from your real footage"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Something" },
    });
    expect(screen.getByRole("button", { name: "Send direction" })).toBeDisabled();
  });

  it("shows fallback chips when the last agent turn gave none and the brief isn't ready", () => {
    const briefing = proposal("briefing");
    briefing.conversation = [
      { role: "user", content: "Not sure yet", suggestions: [] },
      { role: "agent", content: "Tell me more about what you want.", suggestions: [] },
    ];
    briefing.brief_ready = false;
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    expect(screen.getByRole("button", { name: "Make it more personal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep it short and punchy" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "You decide — build the plan" }),
    ).toBeInTheDocument();
  });

  it("never leaks a briefing turn's own suggestions into the review surface (P2-1)", () => {
    const starting = proposal();
    starting.conversation = [
      { role: "user", phase: "briefing", content: "Make it upbeat", suggestions: [] },
      {
        role: "agent",
        phase: "briefing",
        content: "Got it — upbeat it is.",
        suggestions: ["Focus on the beach", "Add friends", "Keep it short"],
      },
    ];
    render(<EditProposalCard item={item(starting)} onChanged={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Tell Kria what to change" }));

    // The briefing turn's OWN chips must never leak into the review surface.
    expect(screen.queryByRole("button", { name: "Focus on the beach" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add friends" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Keep it short" })).toBeNull();
    // No review-phase turn exists yet, so review falls back to its own trio.
    expect(screen.getByRole("button", { name: "Make it more personal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use less text" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Put food first" })).toBeInTheDocument();
  });

  it("treats a pool-only item (nothing assigned to a shot yet) as having media via hasPoolMedia (P1-2)", () => {
    const briefing = proposal("briefing");
    const poolOnly: PlanItem = { ...item(briefing), clip_gcs_paths: [], clip_assignments: [] };
    render(<EditProposalCard item={poolOnly} onChanged={jest.fn()} hasPoolMedia />);

    expect(
      screen.queryByText("Add a photo or video first — Kria plans from your real footage"),
    ).toBeNull();
    fireEvent.change(screen.getByLabelText("Tell Kria what you want in the edit"), {
      target: { value: "Something" },
    });
    expect(screen.getByRole("button", { name: "Send direction" })).toBeEnabled();
  });

  it("keeps the composer typeable while a turn is in flight", async () => {
    mockConversation.mockReturnValue(new Promise(() => {}));
    const briefing = proposal("briefing");
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    const input = screen.getByLabelText("Tell Kria what you want in the edit");
    fireEvent.change(input, { target: { value: "First message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send direction" }));

    await screen.findByText("First message");
    expect(input).toBeEnabled();
    fireEvent.change(input, { target: { value: "typing more" } });
    expect(input).toHaveValue("typing more");
    expect(screen.getByRole("button", { name: "Send direction" })).toBeDisabled();
  });
});
