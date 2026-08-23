// @ts-nocheck
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import EditFeedbackPage from "@/app/admin/edit-feedback/page";
import {
  adminGetEditFeedback,
  adminListEditFeedback,
  adminSaveEditFeedbackAnnotation,
} from "@/lib/admin-edit-feedback-api";

const replace = jest.fn();
let search = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/admin/edit-feedback",
  useSearchParams: () => search,
}));

jest.mock("@/lib/admin-edit-feedback-api", () => ({
  adminListEditFeedback: jest.fn(),
  adminGetEditFeedback: jest.fn(),
  adminSaveEditFeedbackAnnotation: jest.fn(),
}));

const list = adminListEditFeedback as jest.MockedFunction<typeof adminListEditFeedback>;
const get = adminGetEditFeedback as jest.MockedFunction<typeof adminGetEditFeedback>;
const save = adminSaveEditFeedbackAnnotation as jest.MockedFunction<typeof adminSaveEditFeedbackAnnotation>;

const ITEM = {
  id: "artifact-1",
  title: "Morning walk",
  format: "subtitled",
  language: "en",
  media_mix: "speech",
  prompt_version: "p3",
  model_version: "m7",
  created_at: "2026-08-20T12:00:00Z",
  duration_s: 12,
  review_state: "unreviewed",
  quality_signal: null,
  edit_signal: null,
  playback_url: "https://signed.example/final.mp4?sig=one",
  playback_identity: "render-1",
};

const DETAIL = {
  artifact: ITEM,
  timeline: [{ id: "cut-1", kind: "cut", label: "Cut", start_s: 2, end_s: 4 }],
  annotations: [],
  proposal: { direction: "guided_story", rationale: "Lead with the surprising moment." },
  execution_receipt: {
    utterance: "Make the opening faster",
    model_reply: "I will tighten the first beat.",
    intent: "tighten_hook",
    proposal_outcome: "applied",
    execution_outcome: "rejected",
  },
};

describe("EditFeedbackPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
    search = new URLSearchParams();
    list.mockResolvedValue({ items: [ITEM], next_cursor: null });
    get.mockResolvedValue(DETAIL);
    save.mockResolvedValue({
      annotation: {
        id: "annotation-1",
        dimension: "hook",
        rating: "bad",
        rationale: "Hook starts too late",
        created_at: "2026-08-20T12:01:00Z",
      },
    });
  });

  it("loads server-side filters from the URL", async () => {
    search = new URLSearchParams("format=subtitled&review_state=unreviewed");
    render(<EditFeedbackPage />);

    await screen.findByText("Morning walk");
    expect(list).toHaveBeenCalledWith(expect.objectContaining({ format: "subtitled", review_state: "unreviewed", sampling: "stratified", limit: 25 }));
  });

  it("opens the exact render detail and restores focus after Escape", async () => {
    render(<EditFeedbackPage />);
    const row = await screen.findByRole("button", { name: "Review Morning walk" });
    fireEvent.click(row);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(await screen.findByLabelText(/Final render preview/)).toHaveAttribute("src", expect.stringContaining("final.mp4"));
    expect(screen.getAllByRole("button", { name: /Review factor/ })).toHaveLength(15);
    expect(screen.getByRole("button", { name: "Review factor 2: Nova guidance and response" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Nova guidance evidence" })).toHaveTextContent("Make the opening faster");
    expect(screen.getByRole("region", { name: "Nova guidance evidence" })).toHaveTextContent("Lead with the surprising moment.");
    expect(screen.getByRole("region", { name: "Nova guidance evidence" })).toHaveTextContent("rejected");

    fireEvent.keyDown(screen.getByLabelText(/Final render preview/), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(row).toHaveFocus());
  });

  it("supports timeline keyboard seeking and play/pause", async () => {
    render(<EditFeedbackPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Review Morning walk" }));
    const timeline = await screen.findByLabelText(/Timeline keyboard controls/);
    fireEvent.keyDown(timeline, { key: "ArrowRight" });
    expect(screen.getByText("0:01 / 0:12")).toBeInTheDocument();
    fireEvent.keyDown(timeline, { key: " ", code: "Space" });
    expect(screen.getByRole("button", { name: "Pause preview" })).toBeInTheDocument();
  });

  it("appends a correction and retains the draft on failure", async () => {
    render(<EditFeedbackPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Review Morning walk" }));
    await screen.findByRole("heading", { name: "Append a review correction" });
    fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "Hook starts too late" } });
    fireEvent.click(screen.getByRole("button", { name: "Review factor 4: Hook" }));
    fireEvent.click(screen.getByRole("button", { name: "Save rating" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith("artifact-1", expect.objectContaining({
      dimension: "hook",
      rating: "good",
      rationale: "Hook starts too late",
      supersedes_annotation_id: null,
    })));
    expect(await screen.findByText("Correction appended.")).toBeInTheDocument();
  });

  it("surfaces list errors with a retry action", async () => {
    list.mockRejectedValueOnce(new Error("API unavailable"));
    render(<EditFeedbackPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
    list.mockResolvedValueOnce({ items: [ITEM], next_cursor: null });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry loading" })));
    expect(await screen.findByText("Morning walk")).toBeInTheDocument();
  });
});
