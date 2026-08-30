import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import ChatCreationWorkspace from "@/app/plan/_components/workspace/ChatCreationWorkspace";
import {
  applyCreationAction,
  CreationThreadError,
  createCreationThread,
  getCreationCapabilities,
  listCreationThreads,
  refreshCreationThread,
  sendCreationMessage,
} from "@/lib/creation-thread-api";
import { listMyJobs } from "@/lib/me-api";

const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
  useSearchParams: () => new URLSearchParams(),
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ data: { user: { name: "Test creator", email: "test@example.com" } } }),
  signOut: jest.fn(),
}));
jest.mock("@/lib/creation-thread-api", () => {
  const actual = jest.requireActual("@/lib/creation-thread-api");
  return { ...actual, applyCreationAction: jest.fn(), createCreationThread: jest.fn(), getCreationCapabilities: jest.fn(), listCreationThreads: jest.fn(), refreshCreationThread: jest.fn(), sendCreationMessage: jest.fn() };
});
jest.mock("@/lib/me-api", () => {
  const actual = jest.requireActual("@/lib/me-api");
  return { ...actual, listMyJobs: jest.fn() };
});

const baseThread = {
  id: "thread-1", status: "active" as const, revision: 0,
  state: { format: "montage", media: [], media_count: 0 }, content_plan_id: "plan-1",
  active_plan_item_id: null, active_creator_agent_session_id: null,
  active_job_id: null, events: [{ id: "event-1", sequence: 0, revision: 0, role: "assistant" as const, event_type: "format_prompt", content: "Pick a format", payload: { kind: "select_format" }, created_at: "2026-01-01T00:00:00Z" }],
  job: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

describe("ChatCreationWorkspace", () => {
  beforeEach(() => {
    jest.mocked(listCreationThreads).mockReset();
    jest.mocked(createCreationThread).mockReset();
    jest.mocked(refreshCreationThread).mockReset();
    jest.mocked(sendCreationMessage).mockReset();
    jest.mocked(applyCreationAction).mockReset();
    jest.mocked(getCreationCapabilities).mockReset();
    jest.mocked(listMyJobs).mockReset();
    mockReplace.mockReset();
    jest.mocked(listCreationThreads).mockResolvedValue([baseThread]);
    jest.mocked(createCreationThread).mockResolvedValue(baseThread);
    jest.mocked(refreshCreationThread).mockResolvedValue(baseThread);
    jest.mocked(sendCreationMessage).mockResolvedValue(baseThread);
    jest.mocked(listMyJobs).mockResolvedValue({ jobs: [], next_cursor: null });
    jest.mocked(getCreationCapabilities).mockResolvedValue([
      { id: "montage", edit_format: "montage" },
      { id: "narrated", edit_format: "narrated_planned" },
      { id: "talking_to_camera", edit_format: "subtitled" },
    ]);
    jest.mocked(applyCreationAction).mockResolvedValue({ ...baseThread, revision: 1, state: { format: "subtitled", edit_format: "subtitled", media: [] } });
  });

  afterEach(() => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
  });

  it("renders the three Paper formats and keeps the project rail", async () => {
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("heading", { name: "Create with Kria" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Montage Music-led/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Narrated Let/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Talking to camera A clean/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New video" })).toBeInTheDocument();
  });

  it("creates only one empty project when Strict Mode replays the boot effect", async () => {
    jest.mocked(listCreationThreads).mockResolvedValue([]);
    render(<StrictMode><ChatCreationWorkspace /></StrictMode>);
    await screen.findByRole("heading", { name: "Create with Kria" });
    await waitFor(() => expect(createCreationThread).toHaveBeenCalledTimes(1));
  });

  it("sends a format action and uses the durable state for the next step", async () => {
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: /Talking to camera A clean/ }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      baseThread,
      "select_format",
      { format: "talking_to_camera" },
    ));
  });

  it("falls back only when the capability endpoint is a deliberate 404", async () => {
    jest.mocked(listCreationThreads).mockRejectedValueOnce(new CreationThreadError("off", 404));
    const fallback = jest.fn();
    render(<ChatCreationWorkspace onLegacyFallback={fallback} />);
    await waitFor(() => expect(fallback).toHaveBeenCalledTimes(1));
  });

  it("keeps a server error visible instead of silently switching experiences", async () => {
    jest.mocked(listCreationThreads).mockRejectedValueOnce(new CreationThreadError("down", 503));
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t open/i);
  });

  it("keeps the current project active when starting a new project fails", async () => {
    jest.mocked(createCreationThread).mockRejectedValueOnce(new Error("create failed"));
    const reply = {
      ...baseThread,
      events: [...baseThread.events, {
        id: "reply", sequence: 1, revision: 1, role: "user" as const,
        event_type: "user_message", content: "Keep this project", payload: null,
        created_at: "2026-01-01T00:00:01Z",
      }],
    };
    jest.mocked(sendCreationMessage).mockResolvedValueOnce(reply);
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "New video" }));
    await screen.findByRole("alert");
    const composer = screen.getByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep this project" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(await screen.findByText("Keep this project")).toBeInTheDocument();
  });

  it("shows refreshed durable media counts in the chat header", async () => {
    const hydrated = { ...baseThread, state: { format: "montage", edit_format: "montage", media: [], media_count: 3 } };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([hydrated]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(hydrated);
    render(<ChatCreationWorkspace />);
    expect(await screen.findByText("Montage · 3 files")).toBeInTheDocument();
  });

  it("preserves typed direction when sending fails", async () => {
    jest.mocked(sendCreationMessage).mockRejectedValueOnce(new Error("offline"));
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep the opening intimate" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(composer).toHaveValue("Keep the opening intimate"));
  });

  it("submits an offline message once the same project reconnects", async () => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Hold the harbor shot" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent(/saved here/i);

    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    fireEvent(window, new Event("online"));
    await waitFor(() => expect(sendCreationMessage).toHaveBeenCalledWith(baseThread, "Hold the harbor shot"));
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
  });

  it("does not overwrite a newer composer draft while reconnecting", async () => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Older queued direction" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await screen.findByRole("alert");

    fireEvent.change(composer, { target: { value: "Newer direction" } });
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    fireEvent(window, new Event("online"));
    await waitFor(() => expect(composer).toHaveValue("Newer direction"));
    expect(sendCreationMessage).not.toHaveBeenCalled();
  });

  it("does not carry an offline message into a different project", async () => {
    const second = { ...baseThread, id: "thread-2" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([baseThread, second]);
    jest.mocked(refreshCreationThread).mockImplementation((id) => Promise.resolve(id === "thread-2" ? second : baseThread));
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Only for the first project" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await screen.findByRole("alert");
    fireEvent.click(screen.getAllByRole("button", { name: /Untitled video/ })[1]);
    await waitFor(() => expect(composer).toHaveValue(""));
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    fireEvent(window, new Event("online"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sendCreationMessage).not.toHaveBeenCalled();
  });

  it("loads existing completed jobs into Gallery", async () => {
    jest.mocked(listMyJobs).mockResolvedValueOnce({ jobs: [{
      id: "job-1", mode: "generative", status: "ready", raw_status: "ready",
      output_url: "/cut.mp4", poster_url: null, output_variant_id: "original_text",
      tiktok_publishable: false, tiktok_publication: null, created_at: "2026-01-01T00:00:00Z",
      content_plan_item_id: null, feedback_signal: null,
    }], next_cursor: null });
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    expect(await screen.findByRole("button", { name: "Play preview" })).toBeInTheDocument();
    expect(screen.getByText("Ready to post")).toBeInTheDocument();
  });

  it("shows a partial ready cut and offers retry without hiding playable actions", async () => {
    const partial = {
      ...baseThread,
      revision: 5,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1" }], media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
        { variant_id: "song_text", render_status: "failed", output_url: null },
      ] },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([partial]);
    jest.mocked(refreshCreationThread).mockResolvedValue(partial);
    render(<ChatCreationWorkspace />);
    expect(await screen.findByText("Partially ready", { selector: "div" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Retry failed variant/i }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      partial,
      "retry",
      { variant_id: "song_text" },
    ));
  });

  it("selects a ready variant through the typed action before playing it", async () => {
    const ready = {
      ...baseThread,
      revision: 5,
      state: { format: "montage", edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/original.mp4" },
        { variant_id: "song_text", render_status: "ready", output_url: "/song.mp4" },
      ] },
      events: [],
    };
    const selected = { ...ready, state: { ...ready.state, selected_variant_id: "song_text" } };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValue(ready);
    jest.mocked(applyCreationAction).mockResolvedValueOnce(selected);
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Song Text" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(ready, "select_variant", { variant_id: "song_text" }));
  });

  it("uses generate for a ready Creator Agent revision proposal", async () => {
    const ready = {
      ...baseThread,
      revision: 5,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1" }], media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [{ variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" }] },
      events: [
        { id: "initial-strategy", sequence: 0, revision: 1, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "Your first direction", payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "confirmation", sequence: 1, revision: 2, role: "assistant" as const, event_type: "agent_user_confirmation", content: "Confirmed", payload: null, created_at: "2026-01-01T00:00:01Z" },
        { id: "execution-started", sequence: 2, revision: 3, role: "assistant" as const, event_type: "agent_assistant_execution", content: "Render started", payload: { status: "started" }, created_at: "2026-01-01T00:00:02Z" },
        { id: "execution-ready", sequence: 3, revision: 4, role: "assistant" as const, event_type: "agent_assistant_execution", content: "Render ready", payload: { status: "ready" }, created_at: "2026-01-01T00:00:03Z" },
        { id: "revision-request", sequence: 4, revision: 5, role: "user" as const, event_type: "user_message", content: "Make the opening slower", payload: null, created_at: "2026-01-01T00:00:04Z" },
        { id: "strategy", sequence: 5, revision: 6, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "A tighter cut is ready.", payload: null, created_at: "2026-01-01T00:00:05Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValue(ready);
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Create revision" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(ready, "generate", { base_generation: "job-1" }));
  });

  it("does not show a revision card for the initial strategy on the first ready cut", async () => {
    const ready = {
      ...baseThread,
      revision: 5,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1" }], media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [{ variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" }] },
      events: [
        { id: "strategy", sequence: 0, revision: 4, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "Your first direction is ready.", payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "generation", sequence: 1, revision: 5, role: "system" as const, event_type: "action_generate", content: null, payload: { action: "generate" }, created_at: "2026-01-01T00:00:01Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValue(ready);
    render(<ChatCreationWorkspace />);
    await screen.findByText("Your first direction is ready.");
    expect(screen.queryByRole("button", { name: "Create revision" })).not.toBeInTheDocument();
  });

  it("prepares a queued revision once when its exact job becomes ready", async () => {
    const ready = {
      ...baseThread,
      revision: 7,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1" }], media_count: 1, pending_revision_intent: "Open on the laugh" },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [{ variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" }] },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValue(ready);
    render(<ChatCreationWorkspace />);
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      ready,
      "revise",
      { intent: "Open on the laugh" },
      "revision-thread-1-job-1",
    ));
  });

  it("reloads a stale chat revision while keeping the composer draft", async () => {
    jest.mocked(sendCreationMessage).mockRejectedValueOnce(new CreationThreadError("stale", 409));
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep this intimate" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/changed in another window/i));
    expect(composer).toHaveValue("Keep this intimate");
    expect(refreshCreationThread).toHaveBeenCalledWith("thread-1");
  });

  it("offers the shared voice recorder for Narrated projects", async () => {
    jest.mocked(applyCreationAction).mockResolvedValueOnce({
      ...baseThread, revision: 1,
      state: { format: "narrated", edit_format: "narrated_planned", media: [], media_count: 0 },
    });
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: /Narrated Let/ }));
    expect(await screen.findByText("Upload audio")).toBeInTheDocument();
  });

  it("keeps narration capture available after footage is attached", async () => {
    jest.mocked(applyCreationAction).mockResolvedValueOnce({
      ...baseThread, revision: 1,
      state: { format: "narrated", edit_format: "narrated_planned", media: [{ kind: "video", content_type: "video/mp4" }], media_count: 1 },
    });
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: /Narrated Let/ }));
    expect(await screen.findByText("Upload audio")).toBeInTheDocument();
  });

  it("keeps Change format available after footage is attached", async () => {
    const selected = {
      ...baseThread,
      state: { format: "montage", edit_format: "montage", media: [{ kind: "video" }], media_count: 1 },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([selected]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(selected);
    render(<ChatCreationWorkspace />);
    const changeFormat = await screen.findByRole("button", { name: "Change format" });
    await waitFor(() => expect(changeFormat).toBeEnabled());
    fireEvent.click(changeFormat);
    expect(await screen.findByRole("button", { name: /Montage Music-led/ })).toBeInTheDocument();
  });

  it("removes an attached server media item through a revision-fenced action", async () => {
    const withMedia = {
      ...baseThread,
      revision: 3,
      state: {
        format: "montage",
        edit_format: "montage",
        media: [{ media_id: "media-1.mp4", kind: "video", filename: "arrival.mp4" }],
        media_count: 1,
      },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([withMedia]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(withMedia);
    jest.mocked(applyCreationAction).mockResolvedValueOnce({
      ...withMedia,
      revision: 4,
      state: { ...withMedia.state, media: [], media_count: 0 },
    });
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove attached arrival.mp4" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      withMedia,
      "remove_media",
      { media_id: "media-1.mp4" },
    ));
  });

  it("keeps Gallery navigation and the URL projection in sync", async () => {
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    expect(mockReplace).toHaveBeenCalledWith("/plan?view=gallery", { scroll: false });
    fireEvent.click(await screen.findByRole("button", { name: "Back to chat" }));
    expect(mockReplace).toHaveBeenLastCalledWith("/plan", { scroll: false });
  });

  it("prevents project switches while a chat mutation is in flight", async () => {
    let resolveMessage: ((value: typeof baseThread) => void) | undefined;
    jest.mocked(sendCreationMessage).mockImplementationOnce(() => new Promise((resolve) => {
      resolveMessage = resolve;
    }));
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep this warm" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getAllByRole("button", { name: /Untitled video/ })[0]).toBeDisabled());
    resolveMessage?.(baseThread);
  });
});
