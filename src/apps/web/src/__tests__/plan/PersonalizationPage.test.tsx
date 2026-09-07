import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  items: [{ id: "memory-1", section: "video_style", instruction: "Use Playfair Display", normalized_key: "font_family", enforcement: "constraint", state: "active", user_locked: true }],
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

it("renders the unified settings document and suggestion", async () => {
  await renderPage();
  expect(screen.getByText("Use Playfair Display")).toBeTruthy();
  expect(screen.getByText("Quiet openings")).toBeTruthy();
});

it("toggles personalization", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("switch", { name: "Use personalization in new projects" }));
  await waitFor(() => expect(memoryApi.toggleCreatorMemory).toHaveBeenCalledWith(false, 2));
});

it("shows a ten-minute Undo for a profile mutation and sends its operation revision", async () => {
  (memoryApi.toggleCreatorMemory as jest.Mock).mockResolvedValue({
    revision: 3,
    operation_id: "operation-profile-1",
    undo_expires_at: "2099-01-01T00:00:00Z",
  });
  await renderPage();
  fireEvent.click(screen.getByRole("switch", { name: "Use personalization in new projects" }));
  expect(await screen.findByText(/Undo is available for 10 minutes/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() => expect(memoryApi.undoCreatorMemoryOperation).toHaveBeenCalledWith("operation-profile-1", 3));
});

it("reviews then saves composer text as an active default or constraint", async () => {
  await renderPage();
  fireEvent.change(screen.getByRole("textbox", { name: "Tell Kria what to remember" }), { target: { value: "Always use Playfair" } });
  fireEvent.click(screen.getByRole("button", { name: "Review memory update" }));
  expect(screen.getByText("Remember this for future videos?")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(memoryApi.createCreatorMemoryItem).toHaveBeenCalledWith(expect.objectContaining({ enforcement: "constraint" })));
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

it("stops a preference and offers the shared ten-minute Undo", async () => {
  (memoryApi.forgetCreatorMemoryItem as jest.Mock).mockResolvedValue({
    revision: 3,
    operation_id: "operation-forget-1",
    undo_expires_at: "2099-01-01T00:00:00Z",
  });
  await renderPage();
  fireEvent.keyDown(screen.getByRole("button", { name: "Actions for Use Playfair Display" }), { key: "Enter" });
  fireEvent.click(
    await screen.findByRole("menuitem", { name: "Stop using for future videos" }),
  );
  await waitFor(() =>
    expect(memoryApi.forgetCreatorMemoryItem).toHaveBeenCalledWith("memory-1", 2),
  );

  expect(await screen.findByText(/Undo is available for 10 minutes/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() =>
    expect(memoryApi.undoCreatorMemoryOperation).toHaveBeenCalledWith("operation-forget-1", 3),
  );
}, 15_000);

it("preserves a typed key when editing a remembered preference", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Edit Visual style" }), { target: { value: "Use Inter" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(memoryApi.updateCreatorMemoryItem).toHaveBeenCalledWith("memory-1", expect.objectContaining({ normalized_key: "font_family" })));
}, 15_000);

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
  await renderPage();
  fireEvent.keyDown(screen.getByRole("button", { name: "Personalization options" }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Clear remembered preferences" }));
  expect(screen.getByRole("heading", { name: "Clear remembered preferences?" })).toBeTruthy();
  expect(screen.getByText(/creator background and goals stay here/)).toBeTruthy();
  expect(memoryApi.forgetCreatorMemoryItem).not.toHaveBeenCalled();
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
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Edit About your videos" }), { target: { value: "Always make founder stories" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(memoryApi.createCreatorMemoryItem).toHaveBeenCalledWith(expect.objectContaining({ category: "content", enforcement: "constraint", compatibility_key: "summary" })));
  expect(memoryApi.updateCreatorMemoryItem).not.toHaveBeenCalled();
});
