import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import ChatCreationWorkspace, { workspaceEditorVariant, renderPhaseLabel } from "@/app/plan/_components/workspace/ChatCreationWorkspace";
import { POSTER_RECOVERY_DELAYS_MS } from "@/hooks/useLibraryPosterRecovery";
import {
  applyCreationAction,
  cancelKriaTurn,
  CreationThreadError,
  createCreationThread,
  decideKriaApproval,
  deleteCreationThread,
  getCreationCapabilities,
  getKriaApproval,
  getKriaDelta,
  getKriaDraft,
  listCreationThreads,
  refreshCreationThread,
  renameCreationThread,
  sendCreationMessage,
  sendKriaTurn,
  undoKriaDraft,
  uploadCreationMedia,
  type CreationSpeechCleanupProjection,
  type CreationThread,
} from "@/lib/creation-thread-api";
import { deleteMyJob, listMyJobs, refreshMyJobPosters, type LibraryJob } from "@/lib/me-api";
import { undoCreatorMemoryOperation } from "@/lib/memory-api";
import { getPlanItemFresh } from "@/lib/plan-api";

const mockReplace = jest.fn();
let mockSearchParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
  useSearchParams: () => mockSearchParams,
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ data: { user: { name: "Test creator", email: "test@example.com" } } }),
  signOut: jest.fn(),
}));
jest.mock("@/lib/creation-thread-api", () => {
  const actual = jest.requireActual("@/lib/creation-thread-api");
  return { ...actual, applyCreationAction: jest.fn(), cancelKriaTurn: jest.fn(), createCreationThread: jest.fn(), decideKriaApproval: jest.fn(), deleteCreationThread: jest.fn(), getCreationCapabilities: jest.fn(), getKriaApproval: jest.fn(), getKriaDelta: jest.fn(), getKriaDraft: jest.fn(), listCreationThreads: jest.fn(), refreshCreationThread: jest.fn(), renameCreationThread: jest.fn(), sendCreationMessage: jest.fn(), sendKriaTurn: jest.fn(), undoKriaDraft: jest.fn(), uploadCreationMedia: jest.fn() };
});
jest.mock("@/lib/me-api", () => {
  const actual = jest.requireActual("@/lib/me-api");
  return { ...actual, deleteMyJob: jest.fn(), listMyJobs: jest.fn(), refreshMyJobPosters: jest.fn() };
});
jest.mock("@/lib/memory-api", () => {
  const actual = jest.requireActual("@/lib/memory-api");
  return {
    ...actual,
    CREATOR_MEMORY_ENABLED: true,
    undoCreatorMemoryOperation: jest.fn(),
  };
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

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const confirmationEvent = {
  id: "strategy",
  sequence: 1,
  revision: 1,
  role: "assistant" as const,
  event_type: "agent_assistant_strategy",
  content: "This direction is ready.",
  payload: null,
  created_at: "2026-01-01T00:00:01Z",
};

function cleanupThread(
  speechCleanup: CreationSpeechCleanupProjection,
  overrides: Partial<CreationThread> = {},
): CreationThread {
  return {
    ...baseThread,
    revision: 3,
    state: {
      edit_format: "subtitled",
      media: [{ media_id: "video-1", filename: "talk.mp4", kind: "video" }],
      media_count: 1,
    },
    active_plan_item_id: "item-1",
    events: [confirmationEvent],
    speech_cleanup: speechCleanup,
    ...overrides,
  };
}

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
    jest.mocked(cancelKriaTurn).mockReset();
    jest.mocked(createCreationThread).mockReset();
    jest.mocked(refreshCreationThread).mockReset();
    jest.mocked(sendCreationMessage).mockReset();
    jest.mocked(sendKriaTurn).mockReset();
    jest.mocked(getKriaDelta).mockReset();
    jest.mocked(getKriaApproval).mockReset();
    jest.mocked(decideKriaApproval).mockReset();
    jest.mocked(getKriaDraft).mockReset();
    jest.mocked(undoKriaDraft).mockReset();
    jest.mocked(deleteCreationThread).mockReset();
    jest.mocked(renameCreationThread).mockReset();
    jest.mocked(uploadCreationMedia).mockReset();
    jest.mocked(applyCreationAction).mockReset();
    jest.mocked(getCreationCapabilities).mockReset();
    jest.mocked(listMyJobs).mockReset();
    jest.mocked(deleteMyJob).mockReset();
    jest.mocked(refreshMyJobPosters).mockReset();
    jest.mocked(getPlanItemFresh).mockReset();
    jest.mocked(undoCreatorMemoryOperation).mockReset();
    mockReplace.mockReset();
    mockSearchParams = new URLSearchParams();
    jest.mocked(listCreationThreads).mockResolvedValue([baseThread]);
    jest.mocked(createCreationThread).mockResolvedValue(baseThread);
    jest.mocked(refreshCreationThread).mockResolvedValue(baseThread);
    jest.mocked(sendCreationMessage).mockResolvedValue(baseThread);
    jest.mocked(sendKriaTurn).mockResolvedValue({
      turn_id: "turn-1",
      thread_revision: 1,
      status: "pending",
      replayed: false,
    });
    jest.mocked(cancelKriaTurn).mockResolvedValue({
      turn_id: "turn-queued",
      thread_revision: 2,
      status: "cancelled",
      approval_ids: [],
    });
    jest.mocked(getKriaDelta).mockResolvedValue({
      thread_id: "thread-1",
      runtime_version: 2,
      status: "active",
      thread_revision: 1,
      events: [],
      after_sequence: 0,
      next_after_sequence: 0,
      has_more: false,
    });
    jest.mocked(deleteCreationThread).mockResolvedValue();
    jest.mocked(renameCreationThread).mockImplementation(async (thread, title) => ({ ...thread, title }));
    jest.mocked(uploadCreationMedia).mockResolvedValue(baseThread);
    jest.mocked(listMyJobs).mockResolvedValue({ jobs: [], next_cursor: null });
    jest.mocked(deleteMyJob).mockResolvedValue();
    jest.mocked(refreshMyJobPosters).mockResolvedValue({ jobs: [] });
    jest.mocked(getPlanItemFresh).mockRejectedValue(new Error("No linked plan item"));
    jest.mocked(undoCreatorMemoryOperation).mockResolvedValue({ revision: 2 });
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
    expect((await screen.findAllByText("Untitled video"))[0]).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Montage Music-led/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Narrated Let/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Talking to camera A clean/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();
  });

  it("opens profile and memory from the sidebar account icon", async () => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);

    (await screen.findAllByText("Untitled video"))[0];
    expect(screen.queryByText("Test creator")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Account menu" }));

    expect(screen.getByText("Test creator")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Personalization" })).toHaveAttribute(
      "href",
      "/plan/profile",
    );
  });

  it("uses runtime-v2 turn transport and hydrates the semantic reply", async () => {
    const user = userEvent.setup();
    const runtimeThread = {
      ...baseThread,
      runtime_version: 2 as const,
      revision: 2,
      events: [{
        id: "created",
        sequence: 0,
        revision: 1,
        role: "assistant" as const,
        event_type: "format_prompt",
        content: "Pick a format",
        payload: { kind: "select_format" },
        created_at: "2026-01-01T00:00:00Z",
      }],
    };
    const reply = {
      ...runtimeThread,
      revision: 4,
      events: [
        ...runtimeThread.events,
        { id: "user", sequence: 1, revision: 3, role: "user" as const, event_type: "user_message", content: "Open on the whisk", payload: { turn_id: "turn-1" }, created_at: "2026-01-01T00:00:01Z" },
        { id: "assistant", sequence: 2, revision: 4, role: "assistant" as const, event_type: "assistant_response", content: "The whisk is the strongest opening.", payload: { turn_id: "turn-1" }, created_at: "2026-01-01T00:00:02Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([runtimeThread]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(runtimeThread)
      .mockResolvedValueOnce(reply);
    jest.mocked(getKriaDelta).mockResolvedValueOnce({
      thread_id: runtimeThread.id,
      runtime_version: 2,
      status: "active",
      thread_revision: 4,
      events: [reply.events[2]],
      after_sequence: 0,
      next_after_sequence: 2,
      has_more: false,
    });

    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    await user.type(composer, "Open on the whisk");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(sendKriaTurn).toHaveBeenCalledWith(
      runtimeThread,
      "Open on the whisk",
    ));
    expect(sendCreationMessage).not.toHaveBeenCalled();
    expect(await screen.findByText("The whisk is the strongest opening.")).toBeInTheDocument();
  });

  it("renders a reversible draft and consumes only the pinned approval", async () => {
    const user = userEvent.setup();
    const approvalId = "approval-1";
    const runtimeThread = {
      ...baseThread,
      runtime_version: 2 as const,
      revision: 5,
      active_plan_item_id: "item-1",
      events: [
        { id: "draft", sequence: 0, revision: 4, role: "assistant" as const, event_type: "draft_applied", content: "Open on the whisk and finish on the order.", payload: { turn_id: "turn-1", draft_id: "draft-1", draft_revision: 2, changes: ["Tighter opening", "Product reveal last"] }, created_at: "2026-01-01T00:00:01Z" },
        { id: "approval", sequence: 1, revision: 5, role: "system" as const, event_type: "approval_requested", content: null, payload: { turn_id: "turn-1", approval_id: approvalId, consequence_summary: "Render the saved matcha draft.", cost_summary: "One render" }, created_at: "2026-01-01T00:00:02Z" },
      ],
    };
    const approval = {
      approval_id: approvalId,
      turn_id: "turn-1",
      draft_id: "draft-1",
      draft_revision: 2,
      status: "pending" as const,
      consequence_summary: "Render the saved matcha draft.",
      cost_summary: "One render",
      expires_at: "2026-01-01T00:15:00Z",
      approval_fingerprint: "a".repeat(64),
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([runtimeThread]);
    jest.mocked(refreshCreationThread).mockResolvedValue(runtimeThread);
    jest.mocked(getKriaApproval).mockResolvedValueOnce(approval);
    jest.mocked(decideKriaApproval).mockResolvedValueOnce({
      approval_id: approvalId,
      turn_id: "turn-1",
      status: "approved",
      thread_revision: 6,
      render_dispatched: false,
    });

    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    expect(await screen.findByText("Tighter opening · Product reveal last")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo draft" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve and render" }));

    await waitFor(() => expect(decideKriaApproval).toHaveBeenCalledWith(
      runtimeThread.id,
      approval,
      "approve",
      runtimeThread.revision,
    ));
    expect(applyCreationAction).not.toHaveBeenCalledWith(
      expect.anything(),
      "generate",
      expect.anything(),
    );
    await waitFor(() => expect(composer).toHaveFocus());
  });

  it("undoes the current runtime draft through its revision fence", async () => {
    const user = userEvent.setup();
    const runtimeThread = {
      ...baseThread,
      runtime_version: 2 as const,
      revision: 4,
      active_plan_item_id: "item-1",
      events: [{ id: "draft", sequence: 0, revision: 4, role: "assistant" as const, event_type: "draft_applied", content: "Use a faster opening.", payload: { turn_id: "turn-1", changes: ["Faster opening"] }, created_at: "2026-01-01T00:00:01Z" }],
    };
    const draft = {
      draft_id: "draft-1",
      item_id: "item-1",
      variant_key: "initial",
      draft_revision: 3,
      snapshot_hash: "b".repeat(64),
      etag: `"${"b".repeat(64)}"`,
      base_job_id: null,
      base_generation_id: null,
      snapshot: { schema_version: 2, kind: "strategy", changes: ["Faster opening"] },
      can_undo: true,
      created_at: "2026-01-01T00:00:00Z",
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([runtimeThread]);
    jest.mocked(refreshCreationThread).mockResolvedValue(runtimeThread);
    jest.mocked(getKriaDraft).mockResolvedValueOnce(draft);
    jest.mocked(undoKriaDraft).mockResolvedValueOnce({ ...draft, draft_revision: 4 });

    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    await user.click(await screen.findByRole("button", { name: "Undo draft" }));

    await waitFor(() => expect(undoKriaDraft).toHaveBeenCalledWith(runtimeThread.id, 3));
    await waitFor(() => expect(composer).toHaveFocus());
  });

  it("shows one queued runtime request with change and cancel controls", async () => {
    const user = userEvent.setup();
    const queuedThread = {
      ...baseThread,
      runtime_version: 2 as const,
      revision: 6,
      events: [{
        id: "queued-user",
        sequence: 0,
        revision: 6,
        role: "user" as const,
        event_type: "user_message",
        content: "Make the ending quieter",
        payload: { turn_id: "turn-queued", turn_status: "queued" },
        created_at: "2026-01-01T00:00:01Z",
      }],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([queuedThread]);
    jest.mocked(refreshCreationThread).mockResolvedValue(queuedThread);

    render(<ChatCreationWorkspace />);
    expect(await screen.findByText("After this render")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Change request" }));

    await waitFor(() => expect(cancelKriaTurn).toHaveBeenCalledWith(
      queuedThread.id,
      "turn-queued",
      queuedThread.revision,
    ));
    const composer = screen.getByRole("textbox", { name: "Message Kria" });
    expect(composer).toHaveValue(
      "Make the ending quieter",
    );
    await waitFor(() => expect(composer).toHaveFocus());
  });

  it("keeps project navigation available without the chat header", async () => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);

    await screen.findByRole("textbox", { name: "Message Kria" });
    expect(screen.queryByTestId("project-title")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open projects" })).toBeInTheDocument();
    const sidebarShell = screen.getByTestId("project-sidebar-shell");
    const sidebarPanel = screen.getByTestId("project-sidebar-panel");

    expect(sidebarShell).toHaveAttribute("data-state", "open");
    expect(sidebarShell).toHaveClass("md:w-[260px]", "motion-safe:transition-[width]");
    await user.click(screen.getByRole("button", { name: "Hide project sidebar" }));

    const showSidebar = await screen.findByRole("button", { name: "Show project sidebar" });
    expect(screen.getByRole("navigation", { name: "Project navigation" })).toContainElement(showSidebar);
    expect(sidebarShell).toHaveAttribute("data-state", "closed");
    expect(sidebarShell).toHaveAttribute("inert", "");
    expect(sidebarShell).toHaveClass("md:w-0", "motion-safe:duration-[var(--t-accordion-dur)]");
    expect(sidebarPanel).toHaveClass("md:-translate-x-full", "md:opacity-0");

    await user.click(showSidebar);
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
    expect((await screen.findAllByText("A real weekend in Corfu"))[0]).toBeInTheDocument();
    expect(screen.getByTestId("production-video-player")).toHaveAttribute(
      "src",
      "https://storage.example/real-video.mp4",
    );
    expect(screen.getByRole("textbox", { name: "Message Kria" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Project actions for A real weekend in Corfu" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project (preview)" }));
    const nameInput = screen.getByRole("textbox", { name: "Project name" });
    await user.clear(nameInput);
    await user.type(nameInput, "Corfu preview name");
    await user.keyboard("{Enter}");
    expect((await screen.findAllByText("Corfu preview name"))[0]).toBeInTheDocument();

    expect(createCreationThread).not.toHaveBeenCalled();
    expect(renameCreationThread).not.toHaveBeenCalled();
    expect(deleteCreationThread).not.toHaveBeenCalled();
  });

  it("does not refresh posters while the production preview Gallery is open", async () => {
    jest.useFakeTimers();
    const posterlessProductionJob: LibraryJob = {
      id: "prod-job-posterless",
      mode: "generative",
      status: "ready",
      raw_status: "done",
      output_url: "https://storage.example/real-video.mp4",
      poster_url: null,
      poster_identity: "generative-jobs/prod-job-posterless/video.mp4",
      poster_status: "repairing",
      output_variant_id: "original_text",
      tiktok_publishable: false,
      tiktok_publication: null,
      created_at: "2026-08-30T10:00:00Z",
      content_plan_item_id: null,
      feedback_signal: null,
    };
    jest.mocked(listCreationThreads).mockRejectedValueOnce(new CreationThreadError("Unavailable", 404));
    jest.mocked(getCreationCapabilities).mockRejectedValueOnce(new CreationThreadError("Unavailable", 404));
    jest.mocked(listMyJobs).mockResolvedValueOnce({
      next_cursor: null,
      jobs: [posterlessProductionJob],
    });

    mockSearchParams = new URLSearchParams("view=gallery");
    try {
      render(<ChatCreationWorkspace productionPreview />);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByRole("heading", { name: "Gallery" })).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(POSTER_RECOVERY_DELAYS_MS[0] + 1);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(refreshMyJobPosters).not.toHaveBeenCalled();
    } finally {
      mockSearchParams = new URLSearchParams();
      jest.useRealTimers();
    }
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
    await screen.findByRole("textbox", { name: "Message Kria" });

    const picker = document.getElementById("creation-file-picker") as HTMLInputElement;
    await waitFor(() => expect(picker).not.toBeDisabled());
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

  it("preserves reading position and offers a new-update jump", async () => {
    const updated = {
      ...baseThread,
      revision: 1,
      events: [
        ...baseThread.events,
        { id: "user-2", sequence: 1, revision: 1, role: "user" as const, event_type: "user_message", content: "Keep the close-up longer", payload: null, created_at: "2026-01-01T00:00:01Z" },
      ],
    };
    jest.mocked(sendCreationMessage).mockResolvedValueOnce(updated);
    render(<ChatCreationWorkspace />);
    await screen.findByText("Pick a format");
    const transcript = await screen.findByRole("log", { name: "Conversation history" });
    Object.defineProperties(transcript, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    });
    fireEvent.scroll(transcript);
    const composer = screen.getByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep the close-up longer" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    const jump = await screen.findByRole("button", { name: "New update" });
    expect(transcript.scrollTop).toBe(100);
    fireEvent.click(jump);
    expect(transcript.scrollTop).toBe(1000);
  });

  it("creates only one empty project when Strict Mode replays the boot effect", async () => {
    jest.mocked(listCreationThreads).mockResolvedValue([]);
    render(<StrictMode><ChatCreationWorkspace /></StrictMode>);
    (await screen.findAllByText("Untitled video"))[0];
    await waitFor(() => expect(createCreationThread).toHaveBeenCalledTimes(1));
  });

  it("does not create a second project when New chat is clicked during initial loading", async () => {
    const listed = deferred<typeof baseThread[]>();
    const capabilities = deferred<Awaited<ReturnType<typeof getCreationCapabilities>>>();
    jest.mocked(listCreationThreads).mockReturnValueOnce(listed.promise);
    jest.mocked(getCreationCapabilities).mockReturnValueOnce(capabilities.promise);

    render(<ChatCreationWorkspace />);
    const newChat = screen.getByRole("button", { name: "New chat" });
    expect(newChat).toBeDisabled();
    fireEvent.click(newChat);
    expect(createCreationThread).not.toHaveBeenCalled();

    listed.resolve([baseThread]);
    capabilities.resolve({ formats: [], media: {} });
    (await screen.findAllByText("Untitled video"))[0];
    expect(createCreationThread).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalledWith("/plan/undefined", expect.anything());
  });

  it("coalesces repeated Retry clicks into one initial load", async () => {
    jest.mocked(listCreationThreads).mockRejectedValueOnce(new CreationThreadError("down", 503));
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load/i);

    const retry = deferred<typeof baseThread[]>();
    jest.mocked(listCreationThreads).mockReturnValueOnce(retry.promise);
    const retryButton = screen.getByRole("button", { name: "Retry" });
    fireEvent.click(retryButton);
    fireEvent.click(retryButton);
    expect(listCreationThreads).toHaveBeenCalledTimes(2);

    retry.resolve([baseThread]);
    (await screen.findAllByText("Untitled video"))[0];
    expect(createCreationThread).not.toHaveBeenCalled();
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

  it("keeps an unavailable API visible instead of switching experiences", async () => {
    jest.mocked(getCreationCapabilities).mockRejectedValueOnce(new CreationThreadError("off", 404));
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load/i);
    expect(getCreationCapabilities).toHaveBeenCalledTimes(1);
    expect(listCreationThreads).toHaveBeenCalledTimes(1);
    expect(createCreationThread).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(getCreationCapabilities).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: /Montage Music-led/ })).toBeInTheDocument();
  });

  it("keeps a server error visible instead of silently switching experiences", async () => {
    jest.mocked(listCreationThreads).mockRejectedValueOnce(new CreationThreadError("down", 503));
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load/i);
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
    (await screen.findAllByText("Untitled video"))[0];
    fireEvent.click(await screen.findByRole("button", { name: "New chat" }));
    await screen.findByRole("alert");
    const composer = screen.getByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep this project" } });
    await act(async () => {
      fireEvent.keyDown(composer, { key: "Enter" });
    });
    expect(await screen.findByText("Keep this project")).toBeInTheDocument();
  });

  it("shows refreshed durable media counts in the chat header", async () => {
    const hydrated = { ...baseThread, state: { format: "montage", edit_format: "montage", media: [], media_count: 3 } };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([hydrated]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(hydrated);
    render(<ChatCreationWorkspace />);
    expect(await screen.findByRole("textbox", { name: "Message Kria" })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "New video" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Play preview" })).toBeInTheDocument();
    expect(screen.getByText("Ready to post")).toBeInTheDocument();
  });

  it("shows the day-83 source retention notice in Gallery", async () => {
    jest.mocked(listMyJobs).mockResolvedValueOnce({
      jobs: [],
      next_cursor: null,
      retention_warnings: [],
      retention_summary: {
        affected_video_count: 7,
        earliest_delete_at: "2026-09-15T12:00:00Z",
        source_count: 21,
        final_retention_days: 365,
      },
    });

    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));

    const notice = (await screen.findByText("Source-file retention notice.")).parentElement;
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent("Source-file retention notice");
    expect(notice).toHaveTextContent("7 inactive videos");
    expect(notice).toHaveTextContent("365-day retention policy");
  });

  it("refreshes and clears the owner-wide retention notice after deleting its job", async () => {
    const warnedJob: LibraryJob = {
      id: "job-warned", mode: "generative", status: "ready", raw_status: "ready",
      output_url: "/warned.mp4", poster_url: null, output_variant_id: "original_text",
      tiktok_publishable: false, tiktok_publication: null, created_at: "2026-01-01T00:00:00Z",
      content_plan_item_id: null, feedback_signal: null,
    };
    jest.mocked(listMyJobs)
      .mockResolvedValueOnce({
        jobs: [warnedJob],
        next_cursor: null,
        retention_warnings: [{
          job_id: warnedJob.id,
          delete_at: "2026-09-15T12:00:00Z",
          source_count: 2,
          final_retention_days: 365,
        }],
        retention_summary: {
          affected_video_count: 1,
          earliest_delete_at: "2026-09-15T12:00:00Z",
          source_count: 2,
          final_retention_days: 365,
        },
      })
      .mockResolvedValueOnce({
        jobs: [],
        next_cursor: null,
        retention_warnings: [],
        retention_summary: null,
      });

    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    await screen.findByText("Source-file retention notice.");
    fireEvent.keyDown(screen.getByRole("button", { name: "More video actions" }), { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete video" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete video" }));

    await waitFor(() => expect(deleteMyJob).toHaveBeenCalledWith(warnedJob.id));
    await waitFor(() => expect(listMyJobs).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Source-file retention notice.")).not.toBeInTheDocument();
  });

  it("loads older Gallery pages from next_cursor and deduplicates jobs", async () => {
    const job = (id: string): LibraryJob => ({
      id, mode: "generative", status: "ready", raw_status: "ready",
      output_url: `/${id}.mp4`, poster_url: null, output_variant_id: "original_text",
      tiktok_publishable: false, tiktok_publication: null, created_at: "2026-01-01T00:00:00Z",
      content_plan_item_id: null, feedback_signal: null,
    });
    jest.mocked(listMyJobs)
      .mockResolvedValueOnce({ jobs: [job("job-1")], next_cursor: "cursor-1" })
      .mockResolvedValueOnce({ jobs: [job("job-1"), job("job-2")], next_cursor: null });

    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    expect(await screen.findAllByRole("button", { name: "Play preview" })).toHaveLength(1);
    const loadMore = await screen.findByRole("button", { name: "Load more videos" });
    expect(loadMore).toHaveClass("min-h-11");
    fireEvent.click(loadMore);
    await waitFor(() => expect(listMyJobs).toHaveBeenLastCalledWith({ cursor: "cursor-1" }));
    expect(await screen.findAllByRole("button", { name: "Play preview" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Load more videos" })).not.toBeInTheDocument();
  });

  it("shows a non-destructive Gallery error with retry", async () => {
    jest.mocked(listMyJobs).mockRejectedValueOnce(new Error("network down"));
    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/saved videos are still safe/i);
    expect(screen.getByRole("button", { name: "Retry" })).toHaveClass("min-h-11");
  });

  it("retries the failed Gallery page without dropping videos already loaded", async () => {
    const job = (id: string): LibraryJob => ({
      id, mode: "generative", status: "ready", raw_status: "ready",
      output_url: `/${id}.mp4`, poster_url: null, output_variant_id: "original_text",
      tiktok_publishable: false, tiktok_publication: null, created_at: "2026-01-01T00:00:00Z",
      content_plan_item_id: null, feedback_signal: null,
    });
    jest.mocked(listMyJobs)
      .mockResolvedValueOnce({ jobs: [job("job-1")], next_cursor: "cursor-1" })
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({ jobs: [job("job-2")], next_cursor: null });

    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    fireEvent.click(await screen.findByRole("button", { name: "Load more videos" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/load more videos/i);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(listMyJobs).toHaveBeenLastCalledWith({ cursor: "cursor-1" }));
    expect(await screen.findAllByRole("button", { name: "Play preview" })).toHaveLength(2);
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

  it("does not repeat state intent in the render approval card", async () => {
    const intent = "Make the matcha restock update warm and quick";
    const awaitingApproval = {
      ...baseThread,
      state: {
        edit_format: "montage",
        intent,
        media: [{ media_id: "m1", kind: "video" }],
        media_count: 1,
      },
      active_plan_item_id: "item-1",
      events: [
        { id: "user", sequence: 0, revision: 1, role: "user" as const, event_type: "user_message", content: intent, payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "strategy", sequence: 1, revision: 2, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "Open on the whisk, then move from restock to packing orders.", payload: null, created_at: "2026-01-01T00:00:01Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([awaitingApproval]);
    jest.mocked(refreshCreationThread).mockResolvedValue(awaitingApproval);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Open on the whisk, then move from restock to packing orders.")).toBeInTheDocument();
    expect(within(screen.getByRole("log", { name: "Conversation history" })).getAllByText(intent)).toHaveLength(1);
    expect(screen.getByText("Kria will use the proposed direction to make a new cut. Rendering starts only after you approve.")).toBeInTheDocument();
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

  it("keeps the composer usable while a render is running", async () => {
    const rendering = {
      ...baseThread,
      revision: 5,
      state: { edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "variants_rendering", current_phase: "render_variants", variants: [
        { variant_id: "original_text", render_status: "rendering" },
      ] },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([rendering]);
    jest.mocked(refreshCreationThread).mockResolvedValue(rendering);
    jest.mocked(sendCreationMessage).mockResolvedValue(rendering);
    render(<ChatCreationWorkspace />);

    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    expect(composer).toBeEnabled();
    fireEvent.change(composer, { target: { value: "Use a quieter ending after this render" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(sendCreationMessage).toHaveBeenCalledWith(
      rendering,
      "Use a quieter ending after this render",
    ));
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
    expect(screen.getByRole("button", { name: "Edit direction and try again" })).toBeInTheDocument();
  });

  it("surfaces creator planning failures without a Job and restores the last direction", async () => {
    const direction = "Match the content with the videos, add intro texts, and show the scores from my voiceover.";
    const failed = {
      ...baseThread,
      state: { format: "narrated_planned", edit_format: "narrated_planned", media: [], media_count: 1 },
      creator_agent: { status: "failed" },
      events: [
        { id: "direction", sequence: 0, revision: 1, role: "user" as const, event_type: "user_message", content: direction, payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "strategy", sequence: 1, revision: 2, role: "assistant" as const, event_type: "agent_assistant_strategy", content: "I’ll shape the story around your voiceover.", payload: null, created_at: "2026-01-01T00:00:01Z" },
        { id: "planning-error", sequence: 2, revision: 3, role: "assistant" as const, event_type: "agent_assistant_error", content: null, payload: { message: "This direction needs another pass." }, created_at: "2026-01-01T00:00:02Z" },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([failed]);
    jest.mocked(refreshCreationThread).mockResolvedValue(failed);

    render(<ChatCreationWorkspace />);

    const editDirection = await screen.findByRole("button", { name: "Edit direction and try again" });
    expect(screen.queryByRole("button", { name: "Retry render" })).not.toBeInTheDocument();
    fireEvent.click(editDirection);
    const composer = screen.getByRole("textbox", { name: "Message Kria" });
    expect(composer).toHaveValue(direction);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(sendCreationMessage).toHaveBeenCalledWith(failed, direction));
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
      creator_agent: { status: "awaiting_feedback" },
      state: { ...baseThread.state, render_status: "rendering" },
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
    const deleteItem = screen.getByRole("menuitem", { name: "Delete when ready" });
    expect(deleteItem).toHaveAttribute("aria-disabled", "true");
    expect(deleteItem).toHaveAttribute("title", "Wait for the active render before deleting this project.");
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
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn’t rename that project");
  });

  it("refreshes a deletion revision conflict and requires another deliberate confirmation", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    const refreshed = { ...titled, revision: titled.revision + 1 };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    jest.mocked(deleteCreationThread).mockRejectedValueOnce(new CreationThreadError("Creation thread changed", 409));
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete project" }));
    jest.mocked(refreshCreationThread).mockResolvedValue(refreshed);
    await user.click(screen.getByRole("button", { name: "Delete project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("confirm deletion again");
    expect(deleteCreationThread).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Delete project" }));
    await waitFor(() => expect(deleteCreationThread).toHaveBeenLastCalledWith(refreshed));
  });

  it("shows the server deletion blocker", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    jest.mocked(deleteCreationThread).mockRejectedValueOnce(new CreationThreadError("Project has an active upload", 409));
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete project" }));
    await user.click(screen.getByRole("button", { name: "Delete project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Project has an active upload");
    expect(deleteCreationThread).toHaveBeenCalledTimes(1);
  });

  it("renames a project inline with Enter without sending creative direction", async () => {
    const user = userEvent.setup();
    const titled = { ...baseThread, title: "Old name" };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([titled]);
    jest.mocked(refreshCreationThread).mockResolvedValue(titled);
    render(<ChatCreationWorkspace />);

    await user.click(await screen.findByRole("button", { name: "Project actions for Old name" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(input.closest("nav")).toHaveAttribute("aria-label", "Recent projects");
    await waitFor(() => expect(input).toHaveFocus());
    await user.clear(input);
    await user.type(input, "Harbor arrival");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(renameCreationThread).toHaveBeenCalledWith(titled, "Harbor arrival"));
    expect(sendCreationMessage).not.toHaveBeenCalled();
  });

  it("cancels an inline rename with Escape without saving", async () => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Untitled video" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const name = screen.getByRole("textbox", { name: "Project name" });
    await user.clear(name);
    await user.type(name, "Discard this name{Escape}");
    expect(screen.queryByRole("textbox", { name: "Project name" })).not.toBeInTheDocument();
    expect(renameCreationThread).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Project actions for Untitled video" })).toBeInTheDocument();
  });

  it.each(["", "   ", "Untitled video"])("dismisses unchanged or empty inline name %j without saving", async (value) => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Untitled video" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    fireEvent.change(input, { target: { value } });
    fireEvent.blur(input);
    expect(screen.queryByRole("textbox", { name: "Project name" })).not.toBeInTheDocument();
    expect(renameCreationThread).not.toHaveBeenCalled();
  });

  it("retries a failed inline rename while retaining the entered name", async () => {
    const user = userEvent.setup();
    jest.mocked(renameCreationThread).mockRejectedValueOnce(new Error("offline"));
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Untitled video" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    fireEvent.change(input, { target: { value: "Harbor arrival" } });
    fireEvent.submit(input.closest("form")!);
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn’t rename that project");
    expect(input).toHaveValue("Harbor arrival");
    expect(input).toHaveAttribute("aria-invalid", "true");
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Project name" })).not.toBeInTheDocument());
    expect(renameCreationThread).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not submit Enter while composing a project name", async () => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Untitled video" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    fireEvent.change(input, { target: { value: "港の到着" } });
    expect(fireEvent.keyDown(input, { key: "Enter", isComposing: true })).toBe(false);
    expect(renameCreationThread).not.toHaveBeenCalled();
    expect(input).toHaveValue("港の到着");
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(renameCreationThread).toHaveBeenCalledWith(baseThread, "港の到着"));
  });

  it("deduplicates Enter and blur while an inline rename is pending", async () => {
    const user = userEvent.setup();
    const pending = deferred<CreationThread>();
    jest.mocked(renameCreationThread).mockReturnValueOnce(pending.promise);
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Project actions for Untitled video" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename project" }));
    const input = screen.getByRole("textbox", { name: "Project name" });
    fireEvent.change(input, { target: { value: "  Harbor arrival  " } });
    fireEvent.submit(input.closest("form")!);
    fireEvent.blur(input);
    fireEvent.submit(input.closest("form")!);
    expect(renameCreationThread).toHaveBeenCalledTimes(1);
    expect(renameCreationThread).toHaveBeenCalledWith(baseThread, "Harbor arrival");
    expect(input).toHaveAttribute("readonly");
    await act(async () => pending.resolve({ ...baseThread, title: "Harbor arrival" }));
    expect(screen.queryByRole("textbox", { name: "Project name" })).not.toBeInTheDocument();
  });

  it("opens and cancels project deletion from the Gallery sidebar", async () => {
    const user = userEvent.setup();
    render(<ChatCreationWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Gallery" }));
    await user.click(screen.getByRole("button", { name: "Project actions for Untitled video" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete project" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Delete project?")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(deleteCreationThread).not.toHaveBeenCalled();
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
    expect(screen.queryByRole("button", { name: "Save name" })).not.toBeInTheDocument();
    await user.clear(input);
    expect(renameCreationThread).not.toHaveBeenCalled();
    await user.type(input, "x".repeat(121));
    expect(input).toHaveValue("x".repeat(120));
    await user.tab();
    await waitFor(() => expect(renameCreationThread).toHaveBeenCalledWith(titled, "x".repeat(120)));
  });

  it("keeps the last good cut playable when an editor replacement fails", async () => {
    const failedReplacement = {
      ...baseThread,
      state: { format: "montage", edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: {
        id: "job-1",
        status: "variants_ready",
        variants: [{
          variant_id: "guided_story",
          render_status: "failed",
          render_generation_id: "generation-2",
          output_url: "/last-good-cut.mp4",
        }],
      },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([failedReplacement]);
    jest.mocked(refreshCreationThread).mockResolvedValue(failedReplacement);

    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Partially ready", { selector: "div" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "/last-good-cut.mp4",
    );
    expect(screen.getByRole("button", { name: /Retry failed variant/i })).toBeInTheDocument();
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

  it("polls a confirmed Creator turn before its Job exists and discovers the exact Job", async () => {
    const preparing = {
      ...baseThread,
      revision: 4,
      state: {
        format: "montage",
        edit_format: "montage",
        media: [{ media_id: "m1" }],
        media_count: 1,
        creator_agent: { status: "executing" },
        generation: { status: "queued" },
      },
      active_plan_item_id: "item-1",
      active_creator_agent_session_id: "creator-1",
      events: [{
        id: "confirmed",
        sequence: 1,
        revision: 4,
        role: "assistant" as const,
        event_type: "agent_assistant_strategy",
        content: "Use every clip once.",
        payload: null,
        created_at: "2026-01-01T00:00:00Z",
      }],
    };
    const ready = {
      ...preparing,
      creator_agent: { status: "awaiting_feedback" },
      state: { ...preparing.state, creator_agent: { status: "awaiting_feedback" }, generation: { status: "ready", job_id: "job-1" } },
      active_job_id: "job-1",
      job: { id: "job-1", status: "variants_ready", variants: [{ variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" }] },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([preparing]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(ready);

    render(<ChatCreationWorkspace />);

    expect(await screen.findByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(refreshCreationThread).toHaveBeenCalledWith("thread-1");
  });

  it("does not let a delayed send overwrite a newer status poll", async () => {
    jest.useFakeTimers();
    const rendering = {
      ...baseThread,
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "processing", variants: [] },
    };
    const freshPoll = {
      ...rendering,
      job: { ...rendering.job, current_phase: "finalize" },
      events: [...rendering.events, {
        id: "fresh-poll",
        sequence: 1,
        revision: 1,
        role: "assistant" as const,
        event_type: "agent_assistant_execution",
        content: "Fresh poll state",
        payload: null,
        created_at: "2026-01-01T00:00:01Z",
      }],
    };
    const staleSend = {
      ...rendering,
      job: { ...rendering.job, current_phase: "analyze_clips" },
      events: [...rendering.events, {
        id: "stale-send",
        sequence: 1,
        revision: 1,
        role: "assistant" as const,
        event_type: "agent_assistant_execution",
        content: "Stale send state",
        payload: null,
        created_at: "2026-01-01T00:00:01Z",
      }],
    };
    let resolvePoll!: (value: typeof freshPoll) => void;
    let resolveSend!: (value: typeof staleSend) => void;
    jest.mocked(listCreationThreads).mockResolvedValueOnce([rendering]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(rendering)
      .mockResolvedValueOnce(rendering)
      .mockImplementationOnce(() => new Promise((resolve) => { resolvePoll = resolve; }));
    jest.mocked(sendCreationMessage).mockImplementationOnce(() => new Promise((resolve) => { resolveSend = resolve; }));

    try {
      render(<ChatCreationWorkspace />);
      await waitFor(() => expect(refreshCreationThread).toHaveBeenCalledTimes(2));
      const composer = await screen.findByRole("textbox", { name: "Message Kria" });
      fireEvent.change(composer, { target: { value: "Keep the opening warm" } });
      fireEvent.keyDown(composer, { key: "Enter" });

      await act(async () => { jest.advanceTimersByTime(2500); });
      await waitFor(() => expect(refreshCreationThread).toHaveBeenCalledTimes(3));
      await act(async () => { resolvePoll(freshPoll); });
      expect(await screen.findByText("Polishing the final cut…", { selector: "span" })).toBeInTheDocument();
      expect(screen.queryByText("Fresh poll state")).not.toBeInTheDocument();

      await act(async () => { resolveSend(staleSend); });
      expect(screen.getByText("Polishing the final cut…", { selector: "span" })).toBeInTheDocument();
      expect(screen.queryByText("Stale send state")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("does not poll a stale rendering variant after the parent Job fails", async () => {
    const failed = {
      ...baseThread,
      state: { ...baseThread.state, edit_format: "montage", media_count: 1 },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: {
        id: "job-1",
        status: "processing_failed",
        variants: [{ variant_id: "original_text", render_status: "rendering", output_url: null }],
      },
      events: [],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([failed]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(failed);

    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Render needs attention")).toBeInTheDocument();
    expect(screen.queryByText("Kria is building your first cut…")).not.toBeInTheDocument();
    expect(refreshCreationThread).toHaveBeenCalledTimes(1);
  });

  it("hides duplicate confirmation while a pre-Job Creator turn is executing", async () => {
    const preparing = {
      ...baseThread,
      state: {
        format: "montage",
        edit_format: "montage",
        media: [{ media_id: "m1" }],
        media_count: 1,
        creator_agent: { status: "executing" },
      },
      active_plan_item_id: "item-1",
      active_creator_agent_session_id: "creator-1",
      events: [{
        id: "direction",
        sequence: 1,
        revision: 2,
        role: "assistant" as const,
        event_type: "agent_assistant_strategy",
        content: "Ready to render.",
        payload: null,
        created_at: "2026-01-01T00:00:00Z",
      }],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([preparing]);
    jest.mocked(refreshCreationThread).mockResolvedValue(preparing);

    render(<ChatCreationWorkspace />);

    expect((await screen.findAllByText("Preparing")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Create this video" })).not.toBeInTheDocument();
  });

  it("shows reconnecting copy when an in-flight status poll fails", async () => {
    const rendering = {
      ...baseThread,
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "processing", variants: [] },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([rendering]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(rendering)
      .mockRejectedValueOnce(new Error("network down"));

    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Reconnecting…")).toBeInTheDocument();
    expect(refreshCreationThread).toHaveBeenCalledWith("thread-1", expect.any(AbortSignal));
  });

  it("cancels a failed poll retry when the workspace unmounts", async () => {
    jest.useFakeTimers();
    const rendering = {
      ...baseThread,
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: { id: "job-1", status: "processing", variants: [] },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([rendering]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(rendering)
      .mockRejectedValueOnce(new Error("network down"));

    try {
      const view = render(<ChatCreationWorkspace />);
      expect(await screen.findByText("Reconnecting…")).toBeInTheDocument();
      expect(refreshCreationThread).toHaveBeenCalledTimes(2);

      view.unmount();
      act(() => jest.advanceTimersByTime(5000));
      expect(refreshCreationThread).toHaveBeenCalledTimes(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it("does not start a status poll after an authoritative ready response", async () => {
    const ready = {
      ...baseThread,
      state: { ...baseThread.state, edit_format: "montage" },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: {
        id: "job-1",
        status: "variants_ready",
        variants: [{ variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" }],
      },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(ready);

    render(<ChatCreationWorkspace />);

    expect(await screen.findByRole("button", { name: "Play" })).toBeInTheDocument();
    await act(async () => { await Promise.resolve(); });
    expect(refreshCreationThread).toHaveBeenCalledTimes(1);
  });

  it("tracks the exact embedded-editor generation and rejects a late stale refresh", async () => {
    const oldReady = {
      ...baseThread,
      state: { ...baseThread.state, edit_format: "montage" },
      active_plan_item_id: "item-1",
      active_job_id: "job-1",
      job: {
        id: "job-1",
        status: "variants_ready",
        variants: [{
          variant_id: "guided_story",
          render_status: "ready",
          render_generation_id: "generation-1",
          output_url: "/old-cut.mp4",
        }],
      },
    };
    const rendering = {
      ...oldReady,
      job: {
        ...oldReady.job,
        variants: [{
          ...oldReady.job.variants[0],
          render_status: "rendering",
          render_generation_id: "generation-2",
        }],
      },
    };
    const newReady = {
      ...oldReady,
      job: {
        ...oldReady.job,
        variants: [{
          ...oldReady.job.variants[0],
          render_status: "ready",
          render_generation_id: "generation-2",
          render_finished_at: "2026-01-01T00:02:00Z",
          output_url: "/new-cut.mp4",
        }],
      },
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([oldReady]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(oldReady);

    render(<ChatCreationWorkspace />);
    const frame = await screen.findByTitle("Full video editor");
    jest.useFakeTimers();

    try {
      let resolveStale!: (value: typeof oldReady) => void;
      let resolveFresh!: (value: typeof rendering) => void;
      jest.mocked(refreshCreationThread).mockReset();
      jest.mocked(refreshCreationThread)
        .mockImplementationOnce(() => new Promise((resolve) => { resolveStale = resolve; }))
        .mockImplementationOnce(() => new Promise((resolve) => { resolveFresh = resolve; }))
        .mockResolvedValue(newReady);

      act(() => {
        window.dispatchEvent(new MessageEvent("message", {
          origin: window.location.origin,
          source: (frame as HTMLIFrameElement).contentWindow,
          data: {
            type: "nova:embedded-editor-leave",
            refresh: true,
            variant_id: "guided_story",
            render_generation_id: "generation-2",
          },
        }));
      });

      expect(await screen.findByText("Kria is building your cut…")).toBeInTheDocument();
      await waitFor(() => expect(refreshCreationThread).toHaveBeenCalledTimes(2));

      await act(async () => { resolveFresh(rendering); });
      await act(async () => {
        jest.advanceTimersByTime(2500);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(refreshCreationThread).toHaveBeenCalledTimes(3);
      expect(await screen.findByRole("link", { name: "Download" })).toHaveAttribute(
        "href",
        "/new-cut.mp4",
      );

      // The first post-Save request returns last with the pre-Save generation.
      // It must not overwrite the already-observed replacement generation.
      await act(async () => { resolveStale(oldReady); });
      expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
        "href",
        "/new-cut.mp4",
      );
    } finally {
      jest.useRealTimers();
    }
  });

  it("reloads a stale chat revision while keeping the composer draft", async () => {
    jest.mocked(sendCreationMessage).mockRejectedValueOnce(new CreationThreadError("Creation thread changed", 409));
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Keep this intimate" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/changed in another window/i));
    expect(composer).toHaveValue("Keep this intimate");
    expect(refreshCreationThread).toHaveBeenCalledWith("thread-1", expect.any(AbortSignal));
  });

  it("shows a specific busy conflict without stale-window copy", async () => {
    jest.mocked(sendCreationMessage).mockRejectedValueOnce(new CreationThreadError("Creator session is busy", 409));
    render(<ChatCreationWorkspace />);
    const composer = await screen.findByRole("textbox", { name: "Message Kria" });
    fireEvent.change(composer, { target: { value: "Make it warmer" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent("Creator session is busy");
    expect(screen.getByRole("alert")).not.toHaveTextContent(/another window/i);
    expect(composer).toHaveValue("Make it warmer");
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
    expect(changeFormat).toHaveClass("min-h-11");
    fireEvent.click(changeFormat);
    expect(await screen.findByRole("button", { name: /Montage Music-led/ })).toBeInTheDocument();
  });

  it("hydrates several media-added events into one uploader with one copy of each file", async () => {
    const filenames = ["arrival.mp4", "harbor.mp4", "sunset.mp4"];
    const hydrated = {
      ...baseThread,
      revision: 4,
      state: {
        format: "narrated",
        edit_format: "narrated_planned",
        media: filenames.map((filename, index) => ({
          media_id: `media-${index + 1}.mp4`,
          kind: "video",
          filename,
        })),
        media_count: filenames.length,
      },
      events: [
        ...baseThread.events,
        ...filenames.map((filename, index) => ({
          id: `media-added-${index + 1}`,
          sequence: index + 1,
          revision: index + 2,
          role: "user" as const,
          event_type: "media_added",
          content: null,
          payload: { filename, media_count: index + 1 },
          created_at: `2026-01-01T00:00:0${index + 1}Z`,
        })),
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([hydrated]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(hydrated);

    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Add clips and voiceover")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Add primary video clips" })).toHaveLength(1);
    for (const filename of filenames) {
      expect(screen.getAllByText(filename)).toHaveLength(1);
      expect(screen.getAllByRole("button", { name: `Remove attached ${filename}` })).toHaveLength(1);
    }
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
    const remove = await screen.findByRole("button", { name: "Remove attached arrival.mp4" });
    expect(remove).toHaveClass("size-11");
    fireEvent.click(remove);
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      withMedia,
      "remove_media",
      { media_id: "media-1.mp4" },
    ));
  });

  it("keeps Gallery navigation and the URL projection in sync", async () => {
    render(<ChatCreationWorkspace />);
    (await screen.findAllByText("Untitled video"))[0];
    fireEvent.click(await screen.findByRole("button", { name: "Gallery" }));
    expect(mockReplace).toHaveBeenCalledWith("/plan/thread-1?view=gallery", { scroll: false });
    fireEvent.click(await screen.findByRole("button", { name: "Back to chat" }));
    expect(mockReplace).toHaveBeenLastCalledWith("/plan/thread-1", { scroll: false });
  });

  it("preserves an initial Gallery deep link when hydrating the project URL", async () => {
    mockSearchParams = new URLSearchParams("view=gallery");
    render(<ChatCreationWorkspace />);

    expect(await screen.findByRole("heading", { name: "Gallery" })).toBeInTheDocument();
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith(
      "/plan/thread-1?view=gallery",
      { scroll: false },
    ));
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
    jest.mocked(refreshCreationThread).mockRejectedValueOnce(new CreationThreadError(
      "Creation thread not found",
      404,
      { code: "thread_not_found", phase: "accept", message: "Creation thread not found", trace_id: "t1" },
    ));
    render(<ChatCreationWorkspace initialThreadId="deleted-project" />);
    expect(await screen.findByRole("heading", { name: "Project unavailable" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to projects" })).toHaveAttribute("href", "/plan");
  });

  it("shows a retryable state (never a deletion verdict) for a transient service failure", async () => {
    jest.mocked(refreshCreationThread).mockRejectedValueOnce(
      new CreationThreadError("Kria couldn’t complete that request. Retry in a moment.", 502, undefined, {
        code: "upstream_error",
        retryable: true,
      }),
    );
    render(<ChatCreationWorkspace initialThreadId="flaky-project" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load/i);
    expect(screen.queryByRole("heading", { name: "Project unavailable" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows a retryable state for a service-shaped 404 (e.g. a runtime-flag rollback), not a deletion verdict", async () => {
    jest.mocked(refreshCreationThread).mockRejectedValueOnce(new CreationThreadError(
      "Creation chat unavailable",
      404,
      { code: "kria_runtime_unavailable", phase: "accept", message: "Creation chat unavailable", trace_id: "t2" },
    ));
    render(<ChatCreationWorkspace initialThreadId="rollback-project" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load/i);
    expect(screen.queryByRole("heading", { name: "Project unavailable" })).not.toBeInTheDocument();
  });

  it("offers sign-in, not retry, when the session has expired", async () => {
    jest.mocked(refreshCreationThread).mockRejectedValueOnce(
      new CreationThreadError("Authentication required", 401),
    );
    render(<ChatCreationWorkspace initialThreadId="expired-session-project" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/session expired/i);
    expect(screen.queryByRole("heading", { name: "Project unavailable" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
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

  it("renders server-driven findings and sends clean as one atomic generate action", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: {
        id: "analysis-1",
        status: "ready",
        has_findings: true,
        candidate_count: 2,
        category_counts: { filler_sound: 2 },
        estimated_removed_ms: 900,
      },
      decision: null,
      requires_choice: true,
      render_blocker: null,
      outcome: null,
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    jest.mocked(applyCreationAction).mockResolvedValueOnce({
      ...setup,
      active_job_id: "job-1",
      state: { ...setup.state, generation: { status: "queued" } },
    });

    render(<ChatCreationWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Clean up and create" }));

    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      setup,
      "generate",
      { speech_cleanup_analysis_id: "analysis-1", speech_cleanup_choice: "clean" },
      "speech-cleanup:analysis-1:r3:clean",
    ));
  });

  it("dispatches only one atomic choice when both decision controls fire in the same tick", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-double", status: "ready", has_findings: true, candidate_count: 2 },
      decision: null,
      requires_choice: true,
      render_blocker: null,
      outcome: null,
    });
    let resolveAction!: (value: CreationThread) => void;
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    jest.mocked(applyCreationAction).mockImplementationOnce(() => new Promise((resolve) => {
      resolveAction = resolve;
    }));
    render(<ChatCreationWorkspace />);

    const clean = await screen.findByRole("button", { name: "Clean up and create" });
    const keep = screen.getByRole("button", { name: "Keep speech and create" });
    act(() => {
      clean.click();
      keep.click();
    });

    expect(applyCreationAction).toHaveBeenCalledTimes(1);
    expect(applyCreationAction).toHaveBeenCalledWith(
      setup,
      "generate",
      { speech_cleanup_analysis_id: "analysis-double", speech_cleanup_choice: "clean" },
      "speech-cleanup:analysis-double:r3:clean",
    );
    await act(async () => { resolveAction(setup); });
  });

  it("does not infer a cleanup decision card from chat prose", async () => {
    const setup: CreationThread = {
      ...cleanupThread({ applicable: false, analysis: null, decision: null }),
      speech_cleanup: undefined,
      events: [
        {
          id: "user-cleanup-request",
          sequence: 0,
          revision: 0,
          role: "user",
          event_type: "user_message",
          content: "Remove every filler sound and awkward pause.",
          payload: null,
          created_at: "2026-01-01T00:00:00Z",
        },
        confirmationEvent,
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Remove every filler sound and awkward pause.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Speech cleanup" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create this video" })).toBeInTheDocument();
  });

  it("uses the identical decision box for Narrated embedded or uploaded narration", async () => {
    const projection: CreationSpeechCleanupProjection = {
      applicable: true,
      analysis: { id: "analysis-2", status: "ready", has_findings: true, candidate_count: 1 },
      decision: null,
      requires_choice: true,
      render_blocker: null,
      outcome: null,
    };
    const narrated = cleanupThread(projection, {
      state: {
        edit_format: "narrated_planned",
        media: [
          { media_id: "video-1", filename: "story.mp4", kind: "video" },
          { media_id: "audio-1", filename: "narration.mp3", kind: "audio" },
        ],
        media_count: 2,
      },
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([narrated]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(narrated);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByRole("heading", { name: "Speech cleanup" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clean up and create" })).toBeInTheDocument();
    expect(screen.getByText("narration.mp3")).toBeInTheDocument();
  });

  it("keeps audio-only findings pending until video attaches without changing analysis", async () => {
    const projection: CreationSpeechCleanupProjection = {
      applicable: true,
      analysis: { id: "analysis-audio", status: "ready", has_findings: true, candidate_count: 3 },
      decision: null,
      requires_choice: true,
      render_blocker: "video_required",
      outcome: null,
    };
    const audioOnly = cleanupThread(projection, {
      state: {
        edit_format: "narrated_planned",
        media: [{ media_id: "audio-1", filename: "voice.mp3", kind: "audio" }],
        media_count: 1,
      },
    });
    const withVideo = cleanupThread({ ...projection, render_blocker: null }, {
      revision: 4,
      state: {
        edit_format: "narrated_planned",
        media: [
          { media_id: "audio-1", filename: "voice.mp3", kind: "audio" },
          { media_id: "video-1", filename: "visual.mp4", kind: "video" },
        ],
        media_count: 2,
      },
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([audioOnly]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(audioOnly);
    jest.mocked(uploadCreationMedia).mockResolvedValueOnce(withVideo);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Add at least one video clip to make your video.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clean up and create" })).not.toBeInTheDocument();
    expect(applyCreationAction).not.toHaveBeenCalled();

    const picker = document.getElementById("creation-file-picker") as HTMLInputElement;
    fireEvent.change(picker, {
      target: { files: [new File(["video"], "visual.mp4", { type: "video/mp4" })] },
    });
    expect(await screen.findByRole("button", { name: "Clean up and create" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Clean up 3 speech moments?" })).toBeInTheDocument();
    expect(applyCreationAction).not.toHaveBeenCalled();
  });

  it("generates a checked no-findings video with analysis ID and no choice", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-none", status: "no_findings", has_findings: false },
      decision: null,
      requires_choice: false,
      render_blocker: null,
      outcome: null,
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    jest.mocked(applyCreationAction).mockResolvedValueOnce(setup);
    render(<ChatCreationWorkspace />);

    fireEvent.click(await screen.findByRole("button", { name: "Create this video" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      setup,
      "generate",
      { speech_cleanup_analysis_id: "analysis-none" },
      "speech-cleanup:analysis-none:r3:no_findings",
    ));
  });

  it("polls analysis-only state without showing render progress", async () => {
    const queued = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-poll", status: "queued" },
      decision: null,
      requires_choice: false,
      render_blocker: null,
      outcome: null,
    });
    const ready = cleanupThread({
      ...queued.speech_cleanup!,
      analysis: { id: "analysis-poll", status: "ready", has_findings: true, candidate_count: 1 },
      requires_choice: true,
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([queued]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(queued)
      .mockResolvedValueOnce(ready);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByRole("button", { name: "Clean up and create" })).toBeInTheDocument();
    expect(screen.queryByText("Kria is building your first cut…")).not.toBeInTheDocument();
    expect(screen.queryByText("Rendering", { selector: "div" })).not.toBeInTheDocument();
    expect(refreshCreationThread).toHaveBeenLastCalledWith("thread-1", expect.any(AbortSignal));
  });

  it("uses neutral reconnecting copy when an analysis-only poll fails", async () => {
    const queued = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-offline", status: "running" },
      decision: null,
      requires_choice: false,
      render_blocker: null,
      outcome: null,
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([queued]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(queued)
      .mockRejectedValueOnce(new Error("offline"));
    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Reconnecting…")).toBeInTheDocument();
    expect(screen.getByText("Checking for filler sounds…")).toBeInTheDocument();
    expect(screen.queryByText(/building your first cut/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Rendering$/)).not.toBeInTheDocument();
  });

  it("offers explicit failed-check bypass and keeps one workspace announcer", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: {
        id: "analysis-failed",
        status: "failed",
        error: { code: "analysis_timeout", retryable: true },
      },
      decision: null,
      requires_choice: false,
      render_blocker: null,
      outcome: null,
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    jest.mocked(applyCreationAction).mockResolvedValueOnce(setup);
    const { container } = render(<ChatCreationWorkspace />);

    fireEvent.click(await screen.findByRole("button", { name: "Create without cleanup" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      setup,
      "create_without_cleanup",
      { speech_cleanup_analysis_id: "analysis-failed" },
      "speech-cleanup:analysis-failed:r3:bypass",
    ));
    expect(container.querySelectorAll('[aria-live="polite"]')).toHaveLength(1);
    expect(screen.getByTestId("speech-cleanup-live-announcer")).toHaveTextContent(/could not check the speech/i);
    expect(screen.getByRole("log", { name: "Conversation history" })).toHaveAttribute("aria-live", "off");
  });

  it("retries an audio-only failed check without allowing a premature render", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: {
        id: "analysis-audio-failed",
        status: "failed",
        error: { code: "analysis_timeout", retryable: true },
      },
      decision: null,
      requires_choice: false,
      render_blocker: "video_required",
      outcome: null,
    }, {
      state: {
        edit_format: "narrated_planned",
        media: [{ media_id: "audio-1", filename: "voice.mp3", kind: "audio" }],
        media_count: 1,
      },
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    jest.mocked(applyCreationAction).mockResolvedValueOnce(setup);
    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Add at least one video clip to make your video.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create without cleanup" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry speech check" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      setup,
      "retry_speech_cleanup",
      { speech_cleanup_analysis_id: "analysis-audio-failed" },
      "speech-cleanup:analysis-audio-failed:r3:retry_analysis",
    ));
  });

  it("retries cleanup application with the existing retry payload and stable snapshot key", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-apply", status: "ready", has_findings: true, candidate_count: 2 },
      decision: "clean",
      requires_choice: false,
      render_blocker: null,
      outcome: {
        job_id: "job-failed",
        render_generation_id: "generation-failed",
        status: "failed",
        error: { code: "audio_apply_failed", retryable: true },
      },
    }, {
      active_job_id: "job-failed",
      job: { id: "job-failed", status: "processing_failed", variants: [] },
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(setup);
    jest.mocked(applyCreationAction).mockResolvedValueOnce(setup);
    render(<ChatCreationWorkspace />);

    fireEvent.click(await screen.findByRole("button", { name: "Retry cleanup" }));
    await waitFor(() => expect(applyCreationAction).toHaveBeenCalledWith(
      setup,
      "retry",
      { speech_cleanup_analysis_id: "analysis-apply" },
      "speech-cleanup:analysis-apply:r3:retry_render",
    ));
    expect(screen.queryByRole("button", { name: "Play" })).not.toBeInTheDocument();
  });

  it("refreshes a stale cleanup choice and clears it before accepting another choice", async () => {
    const setup = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-old", status: "ready", has_findings: true, candidate_count: 2 },
      decision: null,
      requires_choice: true,
      render_blocker: null,
      outcome: null,
    });
    const current = cleanupThread({
      ...setup.speech_cleanup!,
      analysis: { id: "analysis-new", status: "ready", has_findings: true, candidate_count: 1 },
    }, { revision: 4 });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([setup]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(setup)
      .mockResolvedValueOnce(current);
    jest.mocked(applyCreationAction).mockRejectedValueOnce(
      new CreationThreadError("speech_cleanup_analysis_changed", 409),
    );
    render(<ChatCreationWorkspace />);

    fireEvent.click(await screen.findByRole("button", { name: "Clean up and create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/speech check changed/i);
    expect(screen.getByRole("group", { name: "Clean up 1 speech moment?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clean up and create" })).toBeEnabled();
  });

  it("aborts the one in-flight cleanup detail request on unmount", async () => {
    const queued = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-abort", status: "queued" },
      decision: null,
      requires_choice: false,
      render_blocker: null,
      outcome: null,
    });
    let pollSignal: AbortSignal | undefined;
    jest.mocked(listCreationThreads).mockResolvedValueOnce([queued]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(queued)
      .mockImplementationOnce((_threadId, signal) => new Promise<CreationThread>(() => { pollSignal = signal; }));

    const view = render(<ChatCreationWorkspace />);
    await waitFor(() => expect(pollSignal).toBeDefined());
    expect(pollSignal?.aborted).toBe(false);
    view.unmount();
    expect(pollSignal?.aborted).toBe(true);
  });

  it("aborts the prior cleanup fetch before opening another project", async () => {
    const first = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-switch", status: "running" },
      decision: null,
      requires_choice: false,
      render_blocker: null,
      outcome: null,
    }, { state: { edit_format: "subtitled", intent: "First project", media: [{ kind: "video" }], media_count: 1 } });
    const second: CreationThread = {
      ...baseThread,
      id: "thread-2",
      state: { edit_format: "montage", intent: "Second project", media: [], media_count: 0 },
    };
    let firstPollSignal: AbortSignal | undefined;
    jest.mocked(listCreationThreads).mockResolvedValueOnce([first, second]);
    jest.mocked(refreshCreationThread)
      .mockResolvedValueOnce(first)
      .mockImplementationOnce((_threadId, signal) => new Promise<CreationThread>(() => { firstPollSignal = signal; }))
      .mockResolvedValueOnce(second);
    render(<ChatCreationWorkspace />);
    await waitFor(() => expect(firstPollSignal).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: /Second project/ }));
    await waitFor(() => expect(refreshCreationThread).toHaveBeenLastCalledWith("thread-2", expect.any(AbortSignal)));
    expect(firstPollSignal?.aborted).toBe(true);
    expect(await screen.findByText("Pick a format")).toBeInTheDocument();
  });

  it("shows and applies the ten-minute Undo receipt for automatic learning", async () => {
    const learnedThread = {
      ...baseThread,
      revision: 1,
      events: [
        ...baseThread.events,
        {
          id: "memory-event-1",
          sequence: 1,
          revision: 1,
          role: "assistant" as const,
          event_type: "memory_updated",
          content: "Remembered for future videos.",
          payload: {
            kind: "creator_memory_receipt",
            operation_id: "operation-1",
            memory_revision: 1,
            undo_expires_at: "2099-01-01T00:00:00Z",
          },
          created_at: "2026-01-01T00:00:01Z",
        },
      ],
    };
    jest.mocked(listCreationThreads).mockResolvedValueOnce([learnedThread]);
    jest.mocked(refreshCreationThread).mockResolvedValue(learnedThread);

    render(<ChatCreationWorkspace />);

    expect(await screen.findByText("Preference updated")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Undo (10 minutes)" }));
    await waitFor(() =>
      expect(undoCreatorMemoryOperation).toHaveBeenCalledWith("operation-1", 1),
    );
    expect(await screen.findByText("Automatic update undone")).toBeInTheDocument();
  });

  it.each([
    ["applied", "Speech cleanup applied · 2 moments removed"],
    ["checked_no_change", "Speech cleanup checked · no safe cuts applied"],
    ["declined", "Speech kept as recorded"],
    ["bypassed_unchecked", "Created without checking speech cleanup"],
  ] as const)("shows the generation-bound %s Ready receipt", async (status, copy) => {
    const ready = cleanupThread({
      applicable: true,
      analysis: { id: "analysis-ready", status: "ready", has_findings: true, candidate_count: 2 },
      decision: status === "declined" ? "keep_original" : status === "bypassed_unchecked" ? "create_without_cleanup" : "clean",
      requires_choice: false,
      render_blocker: null,
      outcome: { job_id: "job-1", render_generation_id: "generation-1", status, removal_count: status === "applied" ? 2 : null },
    }, {
      active_job_id: "job-1",
      job: { id: "job-1", status: "ready", variants: [{ variant_id: "cut", render_status: "ready", render_generation_id: "generation-1", output_url: "/cut.mp4" }] },
      events: [],
    });
    jest.mocked(listCreationThreads).mockResolvedValueOnce([ready]);
    jest.mocked(refreshCreationThread).mockResolvedValueOnce(ready);
    render(<ChatCreationWorkspace />);
    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
  });
});

test("a selected rendering variant remains the chat target instead of falling back to another cut", () => {
  const selected = { variant_id: "original_text", render_status: "rendering" };
  const current = { ...baseThread, state: { selected_variant_id: "original_text" }, job: { id: "job", status: "rendering", variants: [{ variant_id: "song_text", render_status: "ready", output_url: "video.mp4" }, selected] } } as CreationThread;
  expect(workspaceEditorVariant(current)).toBe(selected);
});
