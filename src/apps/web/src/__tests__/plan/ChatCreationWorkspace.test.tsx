import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import ChatCreationWorkspace, { renderPhaseLabel } from "@/app/plan/_components/workspace/ChatCreationWorkspace";
import {
  applyCreationAction,
  CreationThreadError,
  createCreationThread,
  deleteCreationThread,
  getCreationCapabilities,
  listCreationThreads,
  refreshCreationThread,
  renameCreationThread,
  sendCreationMessage,
} from "@/lib/creation-thread-api";
import { listMyJobs } from "@/lib/me-api";
import { getPlanItemFresh } from "@/lib/plan-api";

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
  return { ...actual, applyCreationAction: jest.fn(), createCreationThread: jest.fn(), deleteCreationThread: jest.fn(), getCreationCapabilities: jest.fn(), listCreationThreads: jest.fn(), refreshCreationThread: jest.fn(), renameCreationThread: jest.fn(), sendCreationMessage: jest.fn() };
});
jest.mock("@/lib/me-api", () => {
  const actual = jest.requireActual("@/lib/me-api");
  return { ...actual, listMyJobs: jest.fn() };
});
jest.mock("@/lib/plan-api", () => {
  const actual = jest.requireActual("@/lib/plan-api");
  return { ...actual, getPlanItemFresh: jest.fn() };
});
jest.mock("@/app/plan/_components/AssetPool", () => ({
  __esModule: true,
  default: ({ itemId, concise }: { itemId: string; concise?: boolean }) => <div data-testid="mock-asset-pool" data-concise={concise || undefined}>Visuals pool for {itemId}</div>,
}));

const baseThread = {
  id: "thread-1", status: "active" as const, revision: 0,
  state: { format: "montage", media: [], media_count: 0 }, content_plan_id: "plan-1",
  active_plan_item_id: null, active_creator_agent_session_id: null,
  active_job_id: null, events: [{ id: "event-1", sequence: 0, revision: 0, role: "assistant" as const, event_type: "format_prompt", content: "Pick a format", payload: { kind: "select_format" }, created_at: "2026-01-01T00:00:00Z" }],
  job: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

describe("ChatCreationWorkspace", () => {
  it.each([
    ["queued", "Your edit is queued…"],
    ["analyze_clips", "Finding the story in your footage…"],
    ["match_song", "Choosing the right sound…"],
    ["render_variants", "Rendering the edit variations…"],
    ["finalize", "Polishing the final cut…"],
  ])("maps %s to friendly render copy", (phase, label) => {
    expect(renderPhaseLabel(phase)).toBe(label);
    expect(renderPhaseLabel("unknown_phase")).toBe("Building your cut…");
  });

  beforeEach(() => {
    jest.mocked(listCreationThreads).mockReset();
    jest.mocked(createCreationThread).mockReset();
    jest.mocked(refreshCreationThread).mockReset();
    jest.mocked(sendCreationMessage).mockReset();
    jest.mocked(deleteCreationThread).mockReset();
    jest.mocked(renameCreationThread).mockReset();
    jest.mocked(applyCreationAction).mockReset();
    jest.mocked(getCreationCapabilities).mockReset();
    jest.mocked(listMyJobs).mockReset();
    jest.mocked(getPlanItemFresh).mockReset();
    mockReplace.mockReset();
    jest.mocked(listCreationThreads).mockResolvedValue([baseThread]);
    jest.mocked(createCreationThread).mockResolvedValue(baseThread);
    jest.mocked(refreshCreationThread).mockResolvedValue(baseThread);
    jest.mocked(sendCreationMessage).mockResolvedValue(baseThread);
    jest.mocked(deleteCreationThread).mockResolvedValue();
    jest.mocked(renameCreationThread).mockImplementation(async (thread, title) => ({ ...thread, title }));
    jest.mocked(listMyJobs).mockResolvedValue({ jobs: [], next_cursor: null });
    jest.mocked(getPlanItemFresh).mockRejectedValue(new Error("No linked plan item"));
    jest.mocked(getCreationCapabilities).mockResolvedValue({
      formats: [
        { id: "montage", edit_format: "montage" },
        { id: "narrated", edit_format: "narrated_planned" },
        { id: "talking_to_camera", edit_format: "subtitled" },
      ],
      media: {
        clips: {
          max: 50,
          max_file_bytes: 4 * 1024 * 1024 * 1024,
          content_types: ["video/mp4", "video/quicktime"],
        },
      },
    });
    jest.mocked(applyCreationAction).mockResolvedValue({ ...baseThread, revision: 1, state: { format: "subtitled", edit_format: "subtitled", media: [] } });
  });

  afterEach(() => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
  });

  it("renders the three Paper formats and keeps the project rail", async () => {
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("heading", { name: "Untitled video" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Montage Music-led/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Narrated Let/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Talking to camera A clean/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New video" })).toBeInTheDocument();
  });

  it("keeps the sidebar toggle left of the title and animates the collapsed spacing", async () => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);

    const title = await screen.findByTestId("project-title");
    const sidebarShell = screen.getByTestId("project-sidebar-shell");
    const sidebarPanel = screen.getByTestId("project-sidebar-panel");
    const toggleSlot = screen.getByTestId("sidebar-toggle-slot");

    expect(sidebarShell).toHaveAttribute("data-state", "open");
    expect(sidebarShell).toHaveClass("md:w-[260px]", "motion-safe:transition-[width]");
    await user.click(screen.getByRole("button", { name: "Hide project sidebar" }));

    const showSidebar = await screen.findByRole("button", { name: "Show project sidebar" });
    expect(screen.getByTestId("workspace-header-start").firstElementChild).toBe(toggleSlot);
    expect(toggleSlot.nextElementSibling).toContainElement(title);
    expect(showSidebar.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(toggleSlot).toHaveAttribute("data-state", "open");
    expect(toggleSlot).toHaveClass("md:mr-3", "md:grid-cols-[2.75rem]");
    expect(sidebarShell).toHaveAttribute("data-state", "closed");
    expect(sidebarShell).toHaveAttribute("inert", "");
    expect(sidebarShell).toHaveClass("md:w-0", "motion-safe:duration-[var(--t-accordion-dur)]");
    expect(sidebarPanel).toHaveClass("md:-translate-x-full", "md:opacity-0");

    await user.click(showSidebar);
    expect(toggleSlot).toHaveAttribute("data-state", "closed");
    expect(toggleSlot).toHaveClass("md:mr-0", "md:grid-cols-[0rem]");
    expect(sidebarShell).toHaveAttribute("data-state", "open");
    expect(sidebarShell).not.toHaveAttribute("inert");
    expect(sidebarShell).toHaveClass("md:w-[260px]");
    expect(sidebarPanel).toHaveClass("md:translate-x-0", "md:opacity-100");
  });

  it("hydrates real production videos in a read-only preview without creating or mutating projects", async () => {
    const user = userEvent.setup();
    jest.mocked(listCreationThreads).mockRejectedValueOnce(new CreationThreadError("Unavailable", 404));
    jest.mocked(getCreationCapabilities).mockRejectedValueOnce(new CreationThreadError("Unavailable", 404));
    jest.mocked(listMyJobs).mockResolvedValueOnce({
      next_cursor: null,
      jobs: [{
        id: "prod-job-1",
        mode: "generative",
        status: "ready",
        raw_status: "done",
        output_url: "https://storage.example/real-video.mp4",
        poster_url: "https://storage.example/real-poster.jpg",
        poster_identity: "generative-jobs/prod-job-1/video.mp4",
        poster_status: "ready",
        download_url: "https://storage.example/real-video-download.mp4",
        output_variant_id: "original_text",
        tiktok_publishable: true,
        tiktok_publication: null,
        created_at: "2026-08-30T10:00:00Z",
        content_plan_item_id: "prod-item-1",
        feedback_signal: null,
      }],
    });
    jest.mocked(getPlanItemFresh).mockResolvedValueOnce({
      id: "prod-item-1",
      idea: "A real weekend in Corfu",
      edit_format: "montage",
    } as Awaited<ReturnType<typeof getPlanItemFresh>>);

    render(<ChatCreationWorkspace productionPreview />);

    expect(await screen.findByTestId("production-preview-banner")).toHaveTextContent("Live production data");
    expect(await screen.findByRole("heading", { name: "A real weekend in Corfu" })).toBeInTheDocument();
    expect(screen.getByTestId("production-video-player")).toHaveAttribute(
      "src",
      "https://storage.example/real-video.mp4",
    );
    expect(screen.getByRole("textbox", { name: "Message Kria" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "New video" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Project actions for A real weekend in Corfu" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project (preview)" }));
    const nameInput = screen.getByRole("textbox", { name: "Project name" });
    await user.clear(nameInput);
    await user.type(nameInput, "Corfu preview name");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByRole("heading", { name: "Corfu preview name" })).toBeInTheDocument();

    expect(createCreationThread).not.toHaveBeenCalled();
    expect(renameCreationThread).not.toHaveBeenCalled();
    expect(deleteCreationThread).not.toHaveBeenCalled();
  });

  it("uses PlanItem clip guidance and keeps supporting visuals separate", async () => {
    const previousVisualFlag = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
    process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED = "true";
    const setup = {
      ...baseThread,
      state: { edit_format: "montage", media: [], media_count: 0 },
      active_plan_item_id: "item-1",
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    try {
      render(<ChatCreationWorkspace />);
      expect(await screen.findByText(/Three or more clips work best/)).toBeInTheDocument();
      expect(screen.getByText("Add visuals (optional)")).toBeInTheDocument();
      expect(screen.getByTestId("mock-asset-pool")).toHaveTextContent("item-1");
      expect(screen.getByTestId("mock-asset-pool")).toHaveAttribute("data-concise", "true");
      expect(screen.queryByText("Primary Clips")).not.toBeInTheDocument();
      expect(screen.queryByText(/PlanItem’s Visuals pool/)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Attach primary video clips" })).toHaveAttribute("aria-label", "Attach primary video clips");
    } finally {
      if (previousVisualFlag === undefined) delete process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
      else process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED = previousVisualFlag;
    }
  });

  it("does not render an empty visuals artifact when the PlanItem pool is disabled", async () => {
    const previousVisualFlag = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
    const previousGuidedFlag = process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED;
    delete process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
    delete process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED;
    const setup = {
      ...baseThread,
      state: { edit_format: "montage", media: [], media_count: 0 },
      active_plan_item_id: "item-1",
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    try {
      render(<ChatCreationWorkspace />);
      await screen.findByText(/Three or more clips work best/);
      expect(screen.queryByTestId("creation-visuals-artifact")).not.toBeInTheDocument();
    } finally {
      if (previousVisualFlag === undefined) delete process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
      else process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED = previousVisualFlag;
      if (previousGuidedFlag === undefined) delete process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED;
      else process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED = previousGuidedFlag;
    }
  });

  it("uses the server PlanItem media policy before starting an upload", async () => {
    const constrainedThread = {
      ...baseThread,
      state: { ...baseThread.state, edit_format: "montage" },
      media_capabilities: {
        clips: {
          max: 50,
          max_file_bytes: 4,
          content_types: ["video/mp4"],
        },
      },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([constrainedThread]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(constrainedThread);
    render(<ChatCreationWorkspace />);
    await screen.findByText("Montage · 0 clips");

    const picker = document.getElementById("creation-file-picker") as HTMLInputElement;
    fireEvent.change(picker, {
      target: { files: [new File(["12345"], "too-large.mp4", { type: "video/mp4" })] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/larger than the PlanItem upload limit/i);
  });

  it("constrains the chat pane so the transcript scrolls above a pinned composer", async () => {
    render(<ChatCreationWorkspace />);
    const chat = await screen.findByRole("region", { name: "Kria creation chat" });
    const transcript = screen.getByRole("log", { name: "Conversation history" });

    expect(chat.parentElement).toHaveClass("min-h-0", "flex", "flex-col", "overflow-hidden");
    expect(chat.parentElement?.parentElement).toHaveClass("min-h-0", "flex-1", "overflow-hidden");
    expect(transcript).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
      "overscroll-y-contain",
      "touch-pan-y",
      "[scrollbar-gutter:stable]",
    );
    expect(transcript).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("textbox", { name: "Message Kria" })).toBeVisible();
  });

  it("opens a long conversation at its latest message", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => 720 });
    try {
      render(<ChatCreationWorkspace />);
      const transcript = await screen.findByRole("log", { name: "Conversation history" });
      await waitFor(() => expect(transcript.scrollTop).toBe(720));
    } finally {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, "scrollHeight", descriptor);
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollHeight");
    }
  });

  it("creates only one empty project when Strict Mode replays the boot effect", async () => {
    jest.mocked(listCreationThreads).mockResolvedValue([]);
    render(<StrictMode><ChatCreationWorkspace /></StrictMode>);
    await screen.findByRole("heading", { name: "Untitled video" });
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
    jest.mocked(getCreationCapabilities).mockRejectedValueOnce(new CreationThreadError("off", 404));
    const fallback = jest.fn();
    render(<ChatCreationWorkspace onLegacyFallback={fallback} />);
    await waitFor(() => expect(fallback).toHaveBeenCalledTimes(1));
    expect(getCreationCapabilities).toHaveBeenCalledTimes(1);
    expect(listCreationThreads).toHaveBeenCalledTimes(1);
    expect(createCreationThread).not.toHaveBeenCalled();
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
    expect(await screen.findByText("Montage · 3 clips")).toBeInTheDocument();
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
    const second = { ...baseThread, id: "thread-2", title: "Second project" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([baseThread, second]);
    jest.mocked(refreshCreationThread).mockImplementation((id) => Promise.resolve(id === "thread-2" ? second : baseThread));
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Only for the first project" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await screen.findByRole("alert");
    fireEvent.click((await screen.findAllByRole("button", { name: /Second project/ }))[0]);
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

  it("keeps one lifecycle card and leaves later chat below the finished result", async () => {
    const ready = {
      ...baseThread,
      title: "Harbor arrival",
      state: { edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "variants_ready", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      ] },
      events: [
        { id: "started", sequence: 0, revision: 1, role: "system" as const, event_type: "generation_started", content: null, payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "ready", sequence: 1, revision: 2, role: "system" as const, event_type: "generation_ready", content: null, payload: null, created_at: "2026-01-01T00:00:01Z" },
        { id: "later", sequence: 2, revision: 3, role: "user" as const, event_type: "user_message", content: "Make the ending quieter", payload: null, created_at: "2026-01-01T00:00:02Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValue(ready);
    render(<ChatCreationWorkspace />);

    const result = await screen.findByText("Your cut is ready");
    const later = screen.getByText("Make the ending quieter");
    expect(screen.queryByText("Kria is building your cut…")).not.toBeInTheDocument();
    expect(result.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows partial render counts while keeping ready variants playable", async () => {
    const rendering = {
      ...baseThread,
      title: "Harbor arrival",
      state: { edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "variants_rendering", current_phase: "render_variants", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
        { variant_id: "song_text", render_status: "rendering", output_url: null },
      ] },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([rendering]);
    jest.mocked(refreshCreationThread).mockResolvedValue(rendering);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("1 of 2 ready")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play Original Text" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Rendering the edit variations.*1 of 2 ready/)).toBeInTheDocument();
  });

  it("shows retry recovery for a failed render with no newer confirmation", async () => {
    const failed = {
      ...baseThread,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1" }], media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-failed",
      job: { id: "job-failed", status: "processing_failed", failure_reason: "Render failed", variants: [] },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([failed]);
    jest.mocked(refreshCreationThread).mockResolvedValue(failed);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByRole("button", { name: "Retry render" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Adjust direction" })).toBeInTheDocument();
  });

  it("keeps state-only lifecycle cards before later user turns", async () => {
    const stateOnly = {
      ...baseThread,
      state: { format: "montage", edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-ready",
      job: { id: "job-ready", status: "variants_ready", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      ] },
      events: [{ id: "later", sequence: 7, revision: 7, role: "user" as const, event_type: "user_message", content: "Make the ending quieter", payload: null, created_at: "2026-01-01T00:00:07Z" }],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([stateOnly]);
    jest.mocked(refreshCreationThread).mockResolvedValue(stateOnly);
    render(<ChatCreationWorkspace />);

    const result = await screen.findByText("Your cut is ready");
    const later = screen.getByText("Make the ending quieter");
    expect(result.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("allows a completed project to be deleted and opens the next project", async () => {
    const user = userEvent.setup();
    const completed = {
      ...baseThread,
      title: "Finished harbor",
      active_job_id: "job-1",
      job: { id: "job-1", status: "variants_ready", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      ] },
    };
    const next = { ...baseThread, id: "thread-2", title: "Next project" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([completed, next]);
    jest.mocked(refreshCreationThread).mockImplementation((id) => Promise.resolve(id === next.id ? next : completed));
    render(<ChatCreationWorkspace />);

    await user.click(await screen.findByRole("button", { name: "Project actions for Finished harbor" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete project" }));
    expect(screen.getByText(/chat, its uploads, edit data, and completed Kria videos/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete project" }));
    await waitFor(() => expect(deleteCreationThread).toHaveBeenCalledWith(completed));
    expect(mockReplace).toHaveBeenCalledWith("/plan/thread-2", { scroll: false });
  });

  it("keeps delete disabled for a project whose summary still has an active job", async () => {
    const active = {
      ...baseThread,
      id: "active-thread",
      title: "Rendering harbor",
      active_job_id: "job-active",
      job: { id: "job-active", status: "processing", variants: [] },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([active]);
    jest.mocked(refreshCreationThread).mockResolvedValue(active);
    render(<ChatCreationWorkspace />);

    await userEvent.click(await screen.findByRole("button", { name: "Project actions for Rendering harbor" }));
    const deleteItem = screen.getByRole("menuitem", { name: "Delete after rendering" });
    expect(deleteItem).toHaveAttribute("aria-disabled", "true");
    expect(deleteItem).toHaveAttribute("title", "Finish the active render before deleting this project.");
  });

  it("shows a local error when rename loses a revision race", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    jest.mocked(renameCreationThread).mockRejectedValueOnce(new CreationThreadError("stale", 409));
    render(<ChatCreationWorkspace />);

    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    await user.clear(input);
    await user.type(input, "New name");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn’t rename that project");
  });

  it("shows a local error when delete loses a revision race", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    jest.mocked(deleteCreationThread).mockRejectedValueOnce(new CreationThreadError("stale", 409));
    render(<ChatCreationWorkspace />);

    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete project" }));
    await user.click(screen.getByRole("button", { name: "Delete project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn’t delete that project");
  });

  it("renames a project without sending the name as creative direction", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    render(<ChatCreationWorkspace />);

    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    await user.clear(input);
    await user.type(input, "Harbor arrival");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    await waitFor(() => expect(renameCreationThread).toHaveBeenCalledWith(titled, "Harbor arrival"));
    expect(sendCreationMessage).not.toHaveBeenCalled();
  });

  it("prevents empty names and caps project names at 120 characters", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    render(<ChatCreationWorkspace />);

    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    const save = screen.getByRole("button", { name: "Save name" });
    await user.clear(input);
    expect(save).toBeDisabled();
    await user.type(input, "x".repeat(121));
    expect(input).toHaveValue("x".repeat(120));
    expect(save).toBeEnabled();
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

  it("removes the pre-render visuals pool from the narrow ready chat rail", async () => {
    const previousVisualFlag = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
    process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED = "true";
    const ready = {
      ...baseThread,
      state: { edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      ] },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValue(ready);
    try {
      render(<ChatCreationWorkspace />);
      expect(await screen.findByRole("button", { name: "Play" })).toBeInTheDocument();
      expect(screen.queryByTestId("creation-visuals-artifact")).not.toBeInTheDocument();
    } finally {
      if (previousVisualFlag === undefined) delete process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED;
      else process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED = previousVisualFlag;
    }
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

  it("shows only one current confirmation after repeated failed Creator attempts", async () => {
    const retried = {
      ...baseThread,
      revision: 8,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1", kind: "video" }], media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: null,
      job: null,
      events: [
        { id: "strategy-one", sequence: 0, revision: 1, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "First attempt", payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "strategy-two", sequence: 1, revision: 2, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "Second attempt", payload: null, created_at: "2026-01-01T00:00:01Z" },
        { id: "strategy-three", sequence: 2, revision: 3, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "Latest attempt", payload: null, created_at: "2026-01-01T00:00:02Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([retried]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(retried);
    render(<ChatCreationWorkspace />);
    await screen.findByText("Latest attempt");
    expect(screen.getAllByRole("button", { name: "Create this video" })).toHaveLength(1);
  });

  it("shows the new direction instead of stale retry after a failed render", async () => {
    const adjusted = {
      ...baseThread,
      revision: 9,
      state: { format: "montage", edit_format: "montage", media: [{ media_id: "m1", kind: "video" }], media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "failed-job",
      job: { id: "failed-job", status: "processing_failed", failure_reason: "Old render failed", variants: [] },
      events: [
        { id: "old-strategy", sequence: 0, revision: 1, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "Old direction", payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "old-render", sequence: 1, revision: 2, role: "system" as const, event_type: "action_generate", content: null, payload: { action: "generate" }, created_at: "2026-01-01T00:00:01Z" },
        { id: "old-failure", sequence: 2, revision: 3, role: "assistant" as const, event_type: "agent_assistant_execution", content: "That render failed", payload: { status: "failed" }, created_at: "2026-01-01T00:00:02Z" },
        { id: "new-request", sequence: 3, revision: 4, role: "user" as const, event_type: "user_message", content: "Use the photos at 0.1 seconds", payload: null, created_at: "2026-01-01T00:00:03Z" },
        { id: "new-strategy", sequence: 4, revision: 5, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "New exact direction", payload: null, created_at: "2026-01-01T00:00:04Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([adjusted]);
    jest.mocked(refreshCreationThread).mockResolvedValue(adjusted);
    render(<ChatCreationWorkspace />);
    await screen.findByText("New exact direction");
    expect(screen.getByRole("button", { name: "Create this video" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry render" })).not.toBeInTheDocument();
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
    expect(mockReplace).toHaveBeenLastCalledWith("/plan/thread-1", { scroll: false });
  });

  it("hydrates the exact project from a canonical project URL", async () => {
    const titled = { ...baseThread, id: "thread-2", title: "Harbor arrival" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([baseThread, titled]);
    jest.mocked(refreshCreationThread).mockImplementation((id) => Promise.resolve(id === titled.id ? titled : baseThread));
    render(<ChatCreationWorkspace initialThreadId="thread-2" />);
    expect(await screen.findByRole("button", { name: "Project actions for Harbor arrival" })).toBeInTheDocument();
    expect(refreshCreationThread).toHaveBeenCalledWith("thread-2");
  });

  it("shows a deterministic unavailable state when a canonical project is missing", async () => {
    jest.mocked(refreshCreationThread).mockRejectedValueOnce(new CreationThreadError("missing", 404));
    render(<ChatCreationWorkspace initialThreadId="deleted-project" />);
    expect(await screen.findByRole("heading", { name: "Project unavailable" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to projects" })).toHaveAttribute("href", "/plan");
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
