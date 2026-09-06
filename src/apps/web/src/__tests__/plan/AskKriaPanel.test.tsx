import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import AskKriaPanel from "@/app/plan/items/[id]/components/AskKriaPanel";
import {
  contestConformance,
  planItemAdvisorTurn,
  setClipNote,
  type PlanItem,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  contestConformance: jest.fn(),
  planItemAdvisorTurn: jest.fn(),
  setClipNote: jest.fn(),
}));

const advisorMock = planItemAdvisorTurn as jest.MockedFunction<typeof planItemAdvisorTurn>;
const setClipNoteMock = setClipNote as jest.MockedFunction<typeof setClipNote>;
const contestMock = contestConformance as jest.MockedFunction<typeof contestConformance>;

const item = {
  id: "item-1",
  clip_gcs_paths: ["users/u1/clip.mp4"],
  conformance: { clip_gcs_path: "users/u1/clip.mp4" },
} as unknown as PlanItem;

describe("AskKriaPanel", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    contestMock.mockResolvedValue(undefined as never);
    setClipNoteMock.mockResolvedValue(undefined as never);
  });

  it("keeps its editorial response and submits through the shared composer", async () => {
    advisorMock.mockResolvedValue({
      reply: "Use the wide arrival clip for shot one.",
      suggestions: [],
      suggested_note: "",
    });
    render(<AskKriaPanel item={item} mode="default" onClose={jest.fn()} onItemChanged={jest.fn()} />);

    fireEvent.change(screen.getByLabelText("Tell Kria about your clips"), {
      target: { value: "Which clip fits?" },
    });
    fireEvent.keyDown(screen.getByLabelText("Tell Kria about your clips"), { key: "Enter" });

    await screen.findByText("Use the wide arrival clip for shot one.");
    expect(screen.getByText("Which clip fits?")).toHaveClass("border-l-2", "italic");
    expect(advisorMock).toHaveBeenCalledTimes(1);
  });

  it("requires an explicit click before applying a suggested clip note", async () => {
    advisorMock.mockResolvedValue({
      reply: "A note would help me read that clip correctly.",
      suggestions: [],
      suggested_note: "This is the ferry arrival",
    });
    const onItemChanged = jest.fn();
    render(<AskKriaPanel item={item} mode="default" onClose={jest.fn()} onItemChanged={onItemChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "Which of my clips fits shot 1?" }));
    await screen.findByText("Re-read the clip with this context?");
    expect(setClipNoteMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Yes — re-read it" }));
    await waitFor(() => expect(setClipNoteMock).toHaveBeenCalledWith(
      "item-1",
      "users/u1/clip.mp4",
      "This is the ferry arrival",
    ));
    expect(onItemChanged).toHaveBeenCalledTimes(1);
  });

  it("restores the creator's words when the advisor request fails", async () => {
    advisorMock.mockRejectedValue(new Error("offline"));
    render(<AskKriaPanel item={item} mode="default" onClose={jest.fn()} onItemChanged={jest.fn()} />);

    const composer = screen.getByLabelText("Tell Kria about your clips");
    fireEvent.change(composer, { target: { value: "Use the candid clip" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    await screen.findByRole("alert");
    expect(composer).toHaveValue("Use the candid clip");
    expect(screen.getByText("What are you deciding? Describe your clips — I'll give you a read.")).toBeInTheDocument();
  });
});
