import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PersonalizationPage from "@/app/plan/profile/page";
import * as memoryApi from "@/lib/memory-api";

jest.mock("next-auth/react", () => ({ useSession: () => ({ status: "authenticated" }) }));
jest.mock("@/lib/memory-api", () => ({
  CREATOR_MEMORY_ENABLED: true,
  getCreatorMemory: jest.fn(),
  toggleCreatorMemory: jest.fn(),
  createCreatorMemoryItem: jest.fn(),
  clearCreatorMemory: jest.fn(),
  updateCreatorMemoryItem: jest.fn(),
  forgetCreatorMemoryItem: jest.fn(),
  acceptCreatorMemorySuggestion: jest.fn(),
  dismissCreatorMemorySuggestion: jest.fn(),
  undoCreatorMemoryOperation: jest.fn(),
  MemoryApiError: class MemoryApiError extends Error {},
}));

const baseMemory = () => ({
  enabled: true,
  revision: 2,
  items: [{ id: "memory-1", section: "video_style", instruction: "Use Playfair Display", normalized_key: "font_family", enforcement: "constraint", enforcement_status: "enforced", scope_label: "Applies to all future videos", source_label: "Added in Personalization", state: "active", user_locked: true }],
  suggestions: [{ id: "suggestion-1", section: "stories_pacing", instruction: "Quiet openings", enforcement: "advisory", state: "suggested", user_locked: false }],
});

beforeEach(() => {
  jest.clearAllMocks();
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValue(baseMemory());
  (memoryApi.toggleCreatorMemory as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.createCreatorMemoryItem as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.clearCreatorMemory as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.updateCreatorMemoryItem as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.forgetCreatorMemoryItem as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.acceptCreatorMemorySuggestion as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.dismissCreatorMemorySuggestion as jest.Mock).mockResolvedValue({ revision: 3 });
  (memoryApi.undoCreatorMemoryOperation as jest.Mock).mockResolvedValue({ revision: 4 });
});

async function renderPage() {
  render(<PersonalizationPage />);
  await screen.findByRole("heading", { name: "Personalization" });
}

it("renders remembered preferences as one combined list", async () => {
  await renderPage();
  expect(screen.getByText("Use Playfair Display")).toBeTruthy();
  expect(screen.getByText("Quiet openings")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "What Kria remembers" })).toBeTruthy();
  expect(screen.getByRole("list", { name: "Remembered preferences" })).toBeTruthy();
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "About your videos" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Visual style" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Storytelling and tone" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Things to avoid" })).toBeNull();
});

it("provides a way back to the content plan", async () => {
  await renderPage();
  const back = screen.queryByRole("button", { name: /back/i }) ?? screen.queryByRole("link", { name: /back/i });
  expect(back).not.toBeNull();
  if (back?.tagName === "A") expect(back?.getAttribute("href")).toBe("/plan");
});

it("toggles personalization", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("switch", { name: "Use personalization in new projects" }));
  await waitFor(() => expect(memoryApi.toggleCreatorMemory).toHaveBeenCalledWith(false, 2));
});

it("rolls the personalization switch back when the update fails", async () => {
  (memoryApi.toggleCreatorMemory as jest.Mock).mockRejectedValueOnce(new Error("offline"));
  await renderPage();
  const toggle = screen.getByRole("switch", { name: "Use personalization in new projects" });
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute("data-state")).toBe("checked"));
  expect(screen.getByText("Personalization couldn’t be updated. Try again.")).toBeTruthy();
  (memoryApi.toggleCreatorMemory as jest.Mock).mockResolvedValueOnce({ memory: { ...baseMemory(), enabled: false, revision: 3 } });
  fireEvent.click(toggle);
  await waitFor(() => expect(screen.queryByText("Personalization couldn’t be updated. Try again.")).toBeNull());
});

it("does not discard a concurrent preference save when a toggle fails", async () => {
  let rejectToggle!: (reason?: unknown) => void;
  (memoryApi.toggleCreatorMemory as jest.Mock).mockReturnValueOnce(new Promise((_, reject) => { rejectToggle = reject; }));
  (memoryApi.createCreatorMemoryItem as jest.Mock).mockResolvedValueOnce({
    memory: { ...baseMemory(), enabled: false, revision: 3, items: [{ ...baseMemory().items[0], id: "memory-2", instruction: "Keep pacing calm" }] },
  });
  await renderPage();
  fireEvent.click(screen.getByRole("switch", { name: "Use personalization in new projects" }));
  const input = screen.getByRole("textbox", { name: "Tell Kria what to remember" });
  fireEvent.change(input, { target: { value: "Keep pacing calm" } });
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByText("Keep pacing calm")).toBeTruthy();
  await act(async () => rejectToggle(new Error("offline")));
  expect(screen.getByText("Keep pacing calm")).toBeTruthy();
  await waitFor(() => expect(screen.getByRole("switch", { name: "Use personalization in new projects" }).getAttribute("data-state")).toBe("checked"));
});

it("does not show an Undo affordance or changed banner after a profile mutation", async () => {
  (memoryApi.toggleCreatorMemory as jest.Mock).mockResolvedValue({
    revision: 3,
    operation_id: "operation-profile-1",
    undo_expires_at: "2099-01-01T00:00:00Z",
  });
  await renderPage();
  fireEvent.click(screen.getByRole("switch", { name: "Use personalization in new projects" }));
  await waitFor(() => expect(memoryApi.toggleCreatorMemory).toHaveBeenCalledWith(false, 2));
  expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  expect(screen.queryByText("Personalization changed. Undo is available for 10 minutes.")).toBeNull();
});

it("saves composer text directly without a review or approval step", async () => {
  await renderPage();
  const input = screen.getByRole("textbox", { name: "Tell Kria what to remember" });
  fireEvent.change(input, { target: { value: "Always use Playfair" } });
  fireEvent.submit(input.closest("form")!);
  await waitFor(() => expect(memoryApi.createCreatorMemoryItem).toHaveBeenCalledWith(expect.objectContaining({ instruction: "Always use Playfair", enforcement: "constraint" })));
  expect(screen.queryByRole("region", { name: "Memory update review" })).toBeNull();
  expect(screen.queryByText("Remember this for future videos?")).toBeNull();
  expect(screen.queryByText(/Personalization changed/)).toBeNull();
});

it("uses an inline memory response without refetching after a save", async () => {
  (memoryApi.createCreatorMemoryItem as jest.Mock).mockResolvedValueOnce({
    memory: { ...baseMemory(), revision: 3, items: [{ ...baseMemory().items[0], instruction: "Never use shadows", normalized_key: "shadow_enabled" }] },
  });
  await renderPage();
  const input = screen.getByRole("textbox", { name: "Tell Kria what to remember" });
  fireEvent.change(input, { target: { value: "Never use shadows" } });
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByText("Never use shadows")).toBeTruthy();
  expect(memoryApi.getCreatorMemory).toHaveBeenCalledTimes(1);
});

it("refetches memory when a mutation returns the production operation shape", async () => {
  (memoryApi.createCreatorMemoryItem as jest.Mock).mockResolvedValueOnce({ revision: 3, operation_id: "operation-create-1" });
  (memoryApi.getCreatorMemory as jest.Mock)
    .mockResolvedValueOnce(baseMemory())
    .mockResolvedValueOnce({ ...baseMemory(), revision: 3, items: [{ ...baseMemory().items[0], id: "memory-2", instruction: "Keep pacing calm" }] });
  await renderPage();
  const input = screen.getByRole("textbox", { name: "Tell Kria what to remember" });
  fireEvent.change(input, { target: { value: "Keep pacing calm" } });
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByText("Keep pacing calm")).toBeTruthy();
  expect(memoryApi.getCreatorMemory).toHaveBeenCalledTimes(2);
});

it("keeps composer text and shows a recoverable error when saving fails", async () => {
  (memoryApi.createCreatorMemoryItem as jest.Mock).mockRejectedValueOnce(new Error("offline"));
  await renderPage();
  const input = screen.getByRole("textbox", { name: "Tell Kria what to remember" });
  fireEvent.change(input, { target: { value: "Keep the pacing calm" } });
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByText("That preference couldn’t be saved. Try again.")).toBeTruthy();
  expect((input as HTMLInputElement).value).toBe("Keep the pacing calm");
});

it("keeps statement metadata out of the document", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValueOnce({
    enabled: true,
    revision: 2,
    items: [{
      id: "memory-advisory",
      section: "video_style",
      instruction: "Keep compositions airy",
      enforcement: "advisory",
      enforcement_status: "advisory",
      scope_label: "Applies to all future videos",
      source_label: "Added in Personalization",
      state: "active",
      user_locked: false,
    }],
    suggestions: [],
  });
  await renderPage();
  expect(screen.getByText("Keep compositions airy")).toBeTruthy();
  expect(screen.queryByText("Advisory · Kria uses this as guidance")).toBeNull();
  expect(screen.queryByText(/Applies to all future videos/)).toBeNull();
  expect(screen.queryByText(/Added in Personalization/)).toBeNull();
});

it("combines every compatibility profile field into the same list", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValueOnce({
    enabled: true,
    revision: 2,
    suggestions: [],
    compatibility: {
      about_your_videos: "Founder-led explainers",
      goal: "Build trust",
      audience: "Independent creators",
      content_pillars: ["Craft", "Business"],
      posting_cadence: "Twice weekly",
      tone: "Direct and warm",
    },
  });
  await renderPage();
  const list = screen.getByRole("list", { name: "Remembered preferences" });
  expect(list.textContent).toContain("Founder-led explainers");
  expect(list.textContent).toContain("Goal: Build trust");
  expect(list.textContent).toContain("Audience: Independent creators");
  expect(list.textContent).toContain("Content pillars: Craft, Business");
  expect(list.textContent).toContain("Posting cadence: Twice weekly");
  expect(list.textContent).toContain("Tone: Direct and warm");
});

it("does not offer an unsupported edit for the legacy style compatibility row", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValueOnce({
    enabled: true,
    revision: 2,
    items: [{ id: "compatibility-style", section: "video_style", instruction: "Existing style: editorial", enforcement: "advisory", state: "active", user_locked: false }],
    suggestions: [],
  });
  await renderPage();
  expect(screen.getByText("Existing style: editorial")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Edit Existing style: editorial" })).toBeNull();
});

it("does not render an automatic-memory Undo banner", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValueOnce({
    ...baseMemory(),
    recent_undo: {
      operation_id: "operation-automatic-1",
      item_id: "memory-1",
      revision: 2,
      undo_expires_at: "2099-01-01T00:00:00Z",
    },
  });
  await renderPage();
  expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  expect(screen.queryByText(/recent project/)).toBeNull();
});

it("uses one divider between the page header and the document content", async () => {
  await renderPage();
  const titleHeader = screen.getByRole("heading", { name: "Personalization" }).closest("header");
  const settings = screen.getByRole("region", { name: "Personalization settings" });
  expect(titleHeader?.className).not.toContain("border-b");
  expect(settings.className).toContain("border-b");
});

it("dismisses suggestions explicitly", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  await waitFor(() => expect(memoryApi.dismissCreatorMemorySuggestion).toHaveBeenCalledWith("suggestion-1", 2));
});

it("accepts suggestions explicitly", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Remember" }));
  await waitFor(() => expect(memoryApi.acceptCreatorMemorySuggestion).toHaveBeenCalledWith("suggestion-1", 2));
});

it("uses conflict-specific suggestion actions", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValueOnce({
    ...baseMemory(),
    suggestions: [{ ...baseMemory().suggestions[0], conflict: { message: "This replaces your current opening preference." } }],
  });
  await renderPage();
  expect(screen.getByText("This replaces your current opening preference.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Use this instead" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Keep current" })).toBeTruthy();
});

it("explains where a suggestion came from", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Why this suggestion" }));
  expect(screen.getByRole("status").textContent).toContain("Kria noticed this pattern");
});

it("removes a preference from its row without showing Undo", async () => {
  const user = userEvent.setup();
  (memoryApi.forgetCreatorMemoryItem as jest.Mock).mockResolvedValue({
    revision: 3,
    operation_id: "operation-forget-1",
    undo_expires_at: "2099-01-01T00:00:00Z",
    memory: { enabled: true, revision: 3, items: [], suggestions: [] },
  });
  await renderPage();
  await user.click(screen.getByRole("button", { name: "Actions for Use Playfair Display" }));
  await user.click(await screen.findByRole("menuitem", { name: "Remove preference" }));
  await waitFor(() =>
    expect(memoryApi.forgetCreatorMemoryItem).toHaveBeenCalledWith("memory-1", 2),
  );
  expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  expect(screen.queryByText("Personalization changed. Undo is available for 10 minutes.")).toBeNull();
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Tell Kria what to remember" })));
});

it("keeps a preference visible when removal fails", async () => {
  const user = userEvent.setup();
  (memoryApi.forgetCreatorMemoryItem as jest.Mock).mockRejectedValueOnce(new Error("offline"));
  await renderPage();
  await user.click(screen.getByRole("button", { name: "Actions for Use Playfair Display" }));
  await user.click(await screen.findByRole("menuitem", { name: "Remove preference" }));
  expect(await screen.findByText("That preference couldn’t be removed. Try again.")).toBeTruthy();
  expect(screen.getByText("Use Playfair Display")).toBeTruthy();
});

it("shows suggestion errors without removing the suggestion", async () => {
  (memoryApi.acceptCreatorMemorySuggestion as jest.Mock).mockRejectedValueOnce(new Error("offline"));
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Remember" }));
  expect(await screen.findByText("That suggestion couldn’t be remembered. Try again.")).toBeTruthy();
  expect(screen.getByText("Quiet openings")).toBeTruthy();
});

it("preserves a typed key when editing a remembered preference", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Edit Use Playfair Display" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Edit Use Playfair Display" }), { target: { value: "Use Inter" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(memoryApi.updateCreatorMemoryItem).toHaveBeenCalledWith("memory-1", expect.objectContaining({ enforcement: "constraint", normalized_key: "font_family" })));
  await waitFor(() => expect(document.activeElement?.id).toBe("edit-memory-memory-1"));
}, 15_000);

it("cancels an edit without writing the preference", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Edit Use Playfair Display" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Edit Use Playfair Display" }), { target: { value: "Use Inter" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByText("Use Playfair Display")).toBeTruthy();
  expect(memoryApi.updateCreatorMemoryItem).not.toHaveBeenCalled();
});

it("renders the empty state when nothing has been learned yet", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValueOnce({ enabled: true, revision: 0, items: [], suggestions: [] });
  await renderPage();
  expect(screen.getByRole("heading", { name: "Nothing remembered yet" })).toBeTruthy();
  expect(screen.queryByRole("list", { name: "Remembered preferences" })).toBeNull();
});

it("keeps a retry action when personalization fails to load", async () => {
  (memoryApi.getCreatorMemory as jest.Mock)
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce(baseMemory());
  render(<PersonalizationPage />);
  expect(
    await screen.findByRole("heading", { name: "Personalization couldn’t load" }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("heading", { name: "Personalization" })).toBeTruthy();
});

it("requires confirmation before clearing preferences", async () => {
  const user = userEvent.setup();
  await renderPage();
  await user.click(screen.getByRole("button", { name: "Personalization options" }));
  await user.click(await screen.findByRole("menuitem", { name: "Clear remembered preferences" }));
  expect(screen.getByRole("heading", { name: "Clear remembered preferences?" })).toBeTruthy();
  expect(screen.getByText(/creator background and goals stay here/)).toBeTruthy();
  expect(memoryApi.forgetCreatorMemoryItem).not.toHaveBeenCalled();
});

it("clears preferences after confirmation", async () => {
  const user = userEvent.setup();
  (memoryApi.clearCreatorMemory as jest.Mock).mockResolvedValueOnce({
    memory: { enabled: true, revision: 3, items: [], suggestions: [] },
  });
  await renderPage();
  await user.click(screen.getByRole("button", { name: "Personalization options" }));
  await user.click(await screen.findByRole("menuitem", { name: "Clear remembered preferences" }));
  fireEvent.click(screen.getByRole("button", { name: "Clear preferences" }));
  await waitFor(() => expect(memoryApi.clearCreatorMemory).toHaveBeenCalledWith(2));
  expect(await screen.findByRole("heading", { name: "Nothing remembered yet" })).toBeTruthy();
  await waitFor(() => expect(screen.queryByRole("heading", { name: "Clear remembered preferences?" })).toBeNull());
});

it("keeps the clear dialog open when clearing fails", async () => {
  const user = userEvent.setup();
  (memoryApi.clearCreatorMemory as jest.Mock).mockRejectedValueOnce(new Error("offline"));
  await renderPage();
  await user.click(screen.getByRole("button", { name: "Personalization options" }));
  await user.click(await screen.findByRole("menuitem", { name: "Clear remembered preferences" }));
  fireEvent.click(screen.getByRole("button", { name: "Clear preferences" }));
  expect(await screen.findByText("Remembered preferences couldn’t be cleared. Try again.")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Clear remembered preferences?" })).toBeTruthy();
});

it("turns a compatibility statement edit into a new locked memory item", async () => {
  (memoryApi.getCreatorMemory as jest.Mock).mockResolvedValue({
    enabled: true,
    revision: 2,
    items: [],
    suggestions: [],
    compatibility: { summary: "Founder stories" },
  });
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Edit Founder stories" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Edit Founder stories" }), { target: { value: "Always make founder stories" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(memoryApi.createCreatorMemoryItem).toHaveBeenCalledWith(expect.objectContaining({ category: "content", enforcement: "constraint", compatibility_key: "summary" })));
  expect(memoryApi.updateCreatorMemoryItem).not.toHaveBeenCalled();
});
