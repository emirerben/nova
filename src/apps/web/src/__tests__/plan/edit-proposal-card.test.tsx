import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import EditProposalCard from "@/app/plan/items/[id]/components/EditProposalCard";
import {
  approveEditProposal,
  draftEditProposal,
  editProposalConversationTurn,
  updateEditProposal,
  type EditProposal,
  type PlanItem,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  draftEditProposal: jest.fn(),
  editProposalConversationTurn: jest.fn(),
  updateEditProposal: jest.fn(),
  approveEditProposal: jest.fn(),
}));

const mockDraft = draftEditProposal as jest.MockedFunction<typeof draftEditProposal>;
const mockConversation = editProposalConversationTurn as jest.MockedFunction<
  typeof editProposalConversationTurn
>;
const mockUpdate = updateEditProposal as jest.MockedFunction<typeof updateEditProposal>;
const mockApprove = approveEditProposal as jest.MockedFunction<typeof approveEditProposal>;

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

    expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
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

  it("resumes a reloaded in-flight conversation without allowing a generic plan", () => {
    const briefing = proposal("briefing");
    briefing.conversation_in_progress = true;
    render(<EditProposalCard item={item(briefing)} onChanged={jest.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent("Thinking it through");
    expect(screen.getByLabelText("Tell Kria what you want in the edit")).toBeDisabled();
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
    fireEvent.change(screen.getByLabelText("Direction"), { target: { value: "text_explainer" } });
    fireEvent.change(screen.getByLabelText("Pace"), { target: { value: "relaxed" } });
    fireEvent.change(screen.getByLabelText("Target length"), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("Moment 1 topic"), {
      target: { value: "Sea and beaches" },
    });
    fireEvent.change(screen.getAllByLabelText("Layout")[0], {
      target: { value: "supporting_card" },
    });
    fireEvent.change(screen.getAllByRole("textbox", { name: /Thought/ })[0], {
      target: { value: "I kept coming back to the water." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    const sent = mockUpdate.mock.calls[0][2];
    expect(sent.direction).toBe("text_explainer");
    expect(sent.pace).toBe("relaxed");
    expect(sent.duration_s).toBe(30);
    expect(sent.story_beats[0].topic).toBe("Sea and beaches");
    expect(sent.story_beats[0].layout).toBe("supporting_card");
    expect(sent.story_beats[0].thought_source).toBe("user");
    expect(sent.media[0]).not.toHaveProperty("preview_url");
    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith("item-1", 3));
    expect(onChanged).toHaveBeenLastCalledWith(approved);
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

  it("lets the creator request a draft change in plain language", async () => {
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
    expect(screen.getByText(/What would you change about this draft/)).toBeInTheDocument();
    expect(screen.queryByText("I’m ready to draft a reflective plan.")).toBeNull();
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
    expect(onChanged).toHaveBeenCalledWith(item(savedProposal));
    expect(mockConversation).toHaveBeenCalledWith("item-1", 3, "Put food first");
    expect(onChanged).toHaveBeenLastCalledWith(item(revisedProposal));
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
    expect(await screen.findByRole("alert")).toHaveTextContent("Approval service unavailable");

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
});
