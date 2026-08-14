import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import EditProposalCard from "@/app/plan/items/[id]/components/EditProposalCard";
import {
  approveEditProposal,
  draftEditProposal,
  updateEditProposal,
  type EditProposal,
  type PlanItem,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  draftEditProposal: jest.fn(),
  updateEditProposal: jest.fn(),
  approveEditProposal: jest.fn(),
}));

const mockDraft = draftEditProposal as jest.MockedFunction<typeof draftEditProposal>;
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
  };
}

describe("EditProposalCard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("collects direction before starting and prevents a duplicate start", async () => {
    let resolve!: (value: PlanItem) => void;
    mockDraft.mockReturnValue(new Promise((done) => { resolve = done; }));
    const onChanged = jest.fn();
    render(<EditProposalCard item={item(null)} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "Plan edit" }));
    fireEvent.click(screen.getByRole("radio", { name: /Fast montage/ }));
    fireEvent.change(screen.getByLabelText("Goal or context"), {
      target: { value: "Show the town, food, and water" },
    });
    const start = screen.getByRole("button", { name: "Build edit plan" });
    fireEvent.click(start);
    fireEvent.click(start);

    expect(mockDraft).toHaveBeenCalledTimes(1);
    expect(mockDraft).toHaveBeenCalledWith("item-1", {
      direction: "fast_montage",
      goal: "Show the town, food, and water",
      pace: "balanced",
      duration_s: 24,
    });
    await act(async () => resolve(item(proposal("analyzing"))));
    expect(onChanged).toHaveBeenCalledTimes(1);
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
    expect(screen.getByText("Your media changed")).toBeInTheDocument();
    expect(screen.getByText(/last approved plan.*What I noticed in Corfu/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan again" })).toBeVisible();
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
    fireEvent.click(screen.getByRole("button", { name: "Build edit plan" }));

    await waitFor(() => {
      expect(mockDraft).toHaveBeenCalledWith("item-1", failed.brief);
    });
    expect(onChanged).toHaveBeenCalledWith(analyzing);
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
