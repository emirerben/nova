/**
 * PlanThreadPanel — the "Plan with Kria" guided-edit conversation, lifted out
 * of the item setup zone into a Sheet (Lane D of the Kria Design System
 * migration; see plans "P5 Post-draft review" / "C4 Overlays"). Covers:
 *   - closed by default
 *   - opens from a triggering button (mirrors the setup-page status row)
 *   - body is the EditProposalCard conversation surface, opened by default
 *   - Esc closes it
 */
import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import PlanThreadPanel from "@/app/plan/items/[id]/components/PlanThreadPanel";
import type { PlanItem } from "@/lib/plan-api";

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
    PlanApiError,
  };
});

function makeItem(overrides: Partial<PlanItem> = {}): PlanItem {
  return {
    id: "item-1",
    day_index: null,
    theme: "Corfu",
    idea: "Corfu trip",
    position: 0,
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: ["users/u/plan/item-1/coast.mp4"],
    clip_assignments: [],
    status: "awaiting_clips",
    current_job_id: null,
    user_edited: false,
    landscape_fit: "fit",
    edit_proposal: null,
    guided_edit_available: true,
    guided_edit_conversation_available: true,
    ...overrides,
  } as PlanItem;
}

function Harness({ item }: { item: PlanItem }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Plan with Kria
      </button>
      <PlanThreadPanel
        open={open}
        onOpenChange={setOpen}
        item={item}
        onChanged={jest.fn()}
      />
    </div>
  );
}

describe("PlanThreadPanel", () => {
  it("is closed by default", () => {
    render(<Harness item={makeItem()} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens from the triggering button and shows the conversation surface", async () => {
    const user = userEvent.setup();
    render(<Harness item={makeItem()} />);

    await user.click(screen.getByRole("button", { name: "Plan with Kria" }));

    const dialog = screen.getByRole("dialog", { name: "Plan with Kria" });
    expect(dialog).toBeInTheDocument();
    // Body = EditProposalCard, seeded straight onto the conversation surface
    // (defaultConversationOpen) — no "Plan edit" button morph inside the panel.
    expect(
      screen.getByRole("textbox", { name: "Tell Kria what you want in the edit" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Plan edit" })).toBeNull();
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    render(<Harness item={makeItem()} />);

    await user.click(screen.getByRole("button", { name: "Plan with Kria" }));
    expect(screen.getByRole("dialog", { name: "Plan with Kria" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
