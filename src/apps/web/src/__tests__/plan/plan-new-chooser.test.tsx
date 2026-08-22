// @ts-nocheck
/**
 * /plan/new chooser — step 1 of the New-video flow.
 * Guards: item created only on a final card tap (kind cards advance/create,
 * style cards create — no Continue button); type persists via the shared
 * persistedEditFormatFor rule (narrated_planned → narrated_ready); failure
 * paths never dead-end; double-tap creates exactly one item.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

import NewVideoPage from "@/app/plan/new/page";
import { addIdea, getContentPlan, getPlanItem, updatePlanItem } from "@/lib/plan-api";

const push = jest.fn();
const replace = jest.fn();
// Mutable so individual tests can simulate `?item=<id>&step=...` — reset to
// empty (new-item mode) in beforeEach.
let mockSearchParams = new URLSearchParams();

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated" }),
}));

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => mockSearchParams,
}));

jest.mock("@/lib/plan-api", () => ({
  getContentPlan: jest.fn(),
  getPlanItem: jest.fn(),
  addIdea: jest.fn(),
  updatePlanItem: jest.fn(),
}));

jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));

const mockGetContentPlan = getContentPlan as jest.MockedFunction<typeof getContentPlan>;
const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockAddIdea = addIdea as jest.MockedFunction<typeof addIdea>;
const mockUpdatePlanItem = updatePlanItem as jest.MockedFunction<typeof updatePlanItem>;

describe("/plan/new chooser", () => {
  beforeEach(() => {
    push.mockReset();
    replace.mockReset();
    mockSearchParams = new URLSearchParams();
    mockGetContentPlan.mockReset().mockResolvedValue({ id: "plan-1", items: [] });
    mockGetPlanItem
      .mockReset()
      .mockResolvedValue({ id: "existing-1", edit_format: "montage", montage_preset: "classic" });
    mockAddIdea.mockReset().mockResolvedValue({ id: "item-1" });
    mockUpdatePlanItem.mockReset().mockResolvedValue({ id: "item-1" });
  });

  async function ready() {
    render(<NewVideoPage />);
    // Cards become tappable once the plan loads (aria-disabled clears).
    await waitFor(() =>
      expect(screen.getByRole("radio", { name: /Montage/ })).not.toHaveAttribute(
        "aria-disabled",
      ),
    );
  }

  it("renders the type cards with Montage selected by default", async () => {
    await ready();
    const montage = screen.getByRole("radio", { name: /Montage/ });
    expect(montage).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Voiceover/ })).toBeInTheDocument();
    expect(screen.getByText("What kind of video?")).toBeInTheDocument();
  });

  it("has no Continue button", async () => {
    await ready();
    expect(screen.queryByRole("button", { name: /Continue/i })).not.toBeInTheDocument();
  });

  async function throughStyleStep() {
    // Montage inserts Step 2 "Pick a style." — tapping the (already
    // selected) Montage card advances instead of creating anything.
    fireEvent.click(screen.getByRole("radio", { name: /Montage/ }));
    expect(await screen.findByText("Pick a style.")).toBeInTheDocument();
    expect(mockAddIdea).not.toHaveBeenCalled();
  }

  it("Montage: kind → style (classic default) → item minted with montage_preset, lands with ?setup=done", async () => {
    await ready();
    await throughStyleStep();
    expect(screen.getByRole("radio", { name: /Classic/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    fireEvent.click(screen.getByRole("radio", { name: /Classic/ }));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/plan/items/item-1?setup=done"),
    );
    expect(mockAddIdea).toHaveBeenCalledWith("plan-1", "Montage");
    expect(mockUpdatePlanItem).toHaveBeenCalledWith("item-1", {
      edit_format: "montage",
      content_mode: "existing_footage",
      montage_preset: "classic",
    });
  });

  it("Montage: picking Masonry persists montage_preset masonry", async () => {
    await ready();
    await throughStyleStep();
    fireEvent.click(screen.getByRole("radio", { name: /Masonry/ }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(mockUpdatePlanItem).toHaveBeenCalledWith("item-1", {
      edit_format: "montage",
      content_mode: "existing_footage",
      montage_preset: "masonry",
    });
  });

  it("Back from the style step returns to kind with nothing created", async () => {
    await ready();
    await throughStyleStep();
    fireEvent.click(screen.getByRole("button", { name: "Back to video kind" }));
    expect(await screen.findByText("What kind of video?")).toBeInTheDocument();
    expect(mockAddIdea).not.toHaveBeenCalled();
  });

  it("Voiceover skips the style step and persists as narrated_ready", async () => {
    await ready();
    fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
    expect(screen.queryByText("Pick a style.")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/plan/items/item-1?setup=done"),
    );
    expect(mockAddIdea).toHaveBeenCalledWith("plan-1", "Voiceover");
    expect(mockUpdatePlanItem).toHaveBeenCalledWith("item-1", {
      edit_format: "narrated_ready",
    });
  });

  it("Enter on a focused card advances", async () => {
    const user = userEvent.setup();
    await ready();
    const montage = screen.getByRole("radio", { name: /Montage/ });
    montage.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Pick a style.")).toBeInTheDocument();
    expect(mockAddIdea).not.toHaveBeenCalled();
  });

  it("addIdea failure stays on the chooser with a retryable error — nothing created", async () => {
    mockAddIdea.mockRejectedValueOnce(new Error("nope"));
    await ready();
    await throughStyleStep();
    fireEvent.click(screen.getByRole("radio", { name: /Classic/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/try again/i);
    expect(push).not.toHaveBeenCalled();
    expect(mockUpdatePlanItem).not.toHaveBeenCalled();
    // Retry works — tap the card again.
    fireEvent.click(screen.getByRole("radio", { name: /Classic/ }));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/plan/items/item-1?setup=done"),
    );
  });

  it("updatePlanItem failure still lands on the item page WITHOUT setup=done", async () => {
    mockUpdatePlanItem.mockRejectedValueOnce(new Error("patch failed"));
    await ready();
    await throughStyleStep();
    fireEvent.click(screen.getByRole("radio", { name: /Classic/ }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/plan/items/item-1"));
  });

  it("double-click creates exactly one item", async () => {
    let resolveAdd;
    mockAddIdea.mockReturnValueOnce(
      new Promise((done) => {
        resolveAdd = () => done({ id: "item-1" });
      }),
    );
    await ready();
    await throughStyleStep();
    const classic = screen.getByRole("radio", { name: /Classic/ });
    fireEvent.click(classic);
    fireEvent.click(classic);
    resolveAdd();
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(mockAddIdea).toHaveBeenCalledTimes(1);
  });

  it("no plan yet → back to /plan (onboarding owns that path)", async () => {
    mockGetContentPlan.mockResolvedValue(null);
    render(<NewVideoPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/plan"));
  });

  describe("Lane J — ?item=<id> edits an existing item instead of creating one", () => {
    async function readyForItem() {
      render(<NewVideoPage />);
      await waitFor(() =>
        expect(screen.getByRole("radio", { name: /Montage/ })).not.toHaveAttribute(
          "aria-disabled",
        ),
      );
    }

    it("kind step: pre-selects the item's current kind and never calls getContentPlan/addIdea", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "narrated_ready",
        montage_preset: "classic",
      });
      await readyForItem();
      expect(mockGetPlanItem).toHaveBeenCalledWith("existing-1");
      expect(screen.getByRole("radio", { name: /Voiceover/ })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      expect(mockGetContentPlan).not.toHaveBeenCalled();
    });

    it("step=style opens directly on the style step, pre-selecting the item's montage_preset", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1", step: "style" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "montage",
        montage_preset: "masonry",
      });
      render(<NewVideoPage />);
      expect(await screen.findByText("Pick a style.")).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /Masonry/ })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    });

    it("tapping a kind card updates the existing item via updatePlanItem, not addIdea", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "montage",
        montage_preset: "classic",
      });
      await readyForItem();
      fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
      await waitFor(() =>
        expect(push).toHaveBeenCalledWith("/plan/items/existing-1?setup=done"),
      );
      expect(mockAddIdea).not.toHaveBeenCalled();
      expect(mockUpdatePlanItem).toHaveBeenCalledWith("existing-1", {
        edit_format: "narrated_ready",
      });
    });

    it("tapping a style card updates the existing item's montage_preset", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1", step: "style" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "montage",
        montage_preset: "classic",
      });
      render(<NewVideoPage />);
      await screen.findByText("Pick a style.");
      fireEvent.click(screen.getByRole("radio", { name: /Masonry/ }));
      await waitFor(() =>
        expect(push).toHaveBeenCalledWith("/plan/items/existing-1?setup=done"),
      );
      expect(mockAddIdea).not.toHaveBeenCalled();
      expect(mockUpdatePlanItem).toHaveBeenCalledWith("existing-1", {
        edit_format: "montage",
        content_mode: "existing_footage",
        montage_preset: "masonry",
      });
    });

    it("× on the kind step returns to the item instead of /plan", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "montage",
        montage_preset: "classic",
      });
      await readyForItem();
      // No loop: the kind step exits to /plan even in item mode (the item
      // page's Back leads here, so linking back to the item would cycle).
      const exit = screen.getByRole("link", { name: "Back to your videos" });
      expect(exit).toHaveAttribute("href", "/plan");
    });

    it("‹ on the style step still returns to the kind step (local, no navigation)", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1", step: "style" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "montage",
        montage_preset: "classic",
      });
      render(<NewVideoPage />);
      await screen.findByText("Pick a style.");
      fireEvent.click(screen.getByRole("button", { name: "Back to video kind" }));
      expect(await screen.findByText("What kind of video?")).toBeInTheDocument();
      expect(mockAddIdea).not.toHaveBeenCalled();
      expect(mockUpdatePlanItem).not.toHaveBeenCalled();
    });

    it("updatePlanItem failure on the existing item shows the retryable error, no navigation", async () => {
      mockSearchParams = new URLSearchParams({ item: "existing-1" });
      mockGetPlanItem.mockResolvedValue({
        id: "existing-1",
        edit_format: "montage",
        montage_preset: "classic",
      });
      mockUpdatePlanItem.mockRejectedValueOnce(new Error("patch failed"));
      await readyForItem();
      fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/try again/i);
      expect(push).not.toHaveBeenCalled();
    });
  });
});
