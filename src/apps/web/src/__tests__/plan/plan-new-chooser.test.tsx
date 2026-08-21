// @ts-nocheck
/**
 * /plan/new chooser — step 1 of the New-video flow.
 * Guards: item created only on Continue; type persists via the shared
 * persistedEditFormatFor rule (narrated_planned → narrated_ready); failure
 * paths never dead-end; double-tap creates exactly one item.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import NewVideoPage from "@/app/plan/new/page";
import { addIdea, getContentPlan, updatePlanItem } from "@/lib/plan-api";

const push = jest.fn();
const replace = jest.fn();

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated" }),
}));

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
}));

jest.mock("@/lib/plan-api", () => ({
  getContentPlan: jest.fn(),
  addIdea: jest.fn(),
  updatePlanItem: jest.fn(),
}));

jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));

const mockGetContentPlan = getContentPlan as jest.MockedFunction<typeof getContentPlan>;
const mockAddIdea = addIdea as jest.MockedFunction<typeof addIdea>;
const mockUpdatePlanItem = updatePlanItem as jest.MockedFunction<typeof updatePlanItem>;

describe("/plan/new chooser", () => {
  beforeEach(() => {
    push.mockReset();
    replace.mockReset();
    mockGetContentPlan.mockReset().mockResolvedValue({ id: "plan-1", items: [] });
    mockAddIdea.mockReset().mockResolvedValue({ id: "item-1" });
    mockUpdatePlanItem.mockReset().mockResolvedValue({ id: "item-1" });
  });

  async function ready() {
    render(<NewVideoPage />);
    // Continue enables once the plan loads.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled(),
    );
  }

  it("renders the type cards with Montage selected by default", async () => {
    await ready();
    const montage = screen.getByRole("radio", { name: /Montage/ });
    expect(montage).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Voiceover/ })).toBeInTheDocument();
    expect(screen.getByText("What kind of video?")).toBeInTheDocument();
  });

  it("Continue creates the item, stamps montage + existing_footage, lands with ?setup=done", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/plan/items/item-1?setup=done"),
    );
    expect(mockAddIdea).toHaveBeenCalledWith("plan-1", "Montage");
    expect(mockUpdatePlanItem).toHaveBeenCalledWith("item-1", {
      edit_format: "montage",
      content_mode: "existing_footage",
    });
  });

  it("Voiceover persists as narrated_ready (shared legacy-upgrade rule)", async () => {
    await ready();
    fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/plan/items/item-1?setup=done"),
    );
    expect(mockAddIdea).toHaveBeenCalledWith("plan-1", "Voiceover");
    expect(mockUpdatePlanItem).toHaveBeenCalledWith("item-1", {
      edit_format: "narrated_ready",
    });
  });

  it("addIdea failure stays on the chooser with a retryable error — nothing created", async () => {
    mockAddIdea.mockRejectedValueOnce(new Error("nope"));
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/try again/i);
    expect(push).not.toHaveBeenCalled();
    expect(mockUpdatePlanItem).not.toHaveBeenCalled();
    // Retry works.
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/plan/items/item-1?setup=done"),
    );
  });

  it("updatePlanItem failure still lands on the item page WITHOUT setup=done", async () => {
    mockUpdatePlanItem.mockRejectedValueOnce(new Error("patch failed"));
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
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
    const btn = screen.getByRole("button", { name: "Continue" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    resolveAdd();
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(mockAddIdea).toHaveBeenCalledTimes(1);
  });

  it("no plan yet → back to /plan (onboarding owns that path)", async () => {
    mockGetContentPlan.mockResolvedValue(null);
    render(<NewVideoPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/plan"));
  });
});
