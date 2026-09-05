import {
  creationFormat,
  creationClipLimit,
  creationJobFailed,
  creationJobPartial,
  creationJobReady,
  creationJobSettled,
  creationThreadInProgress,
  creationThreadPreparing,
  creationThreadProgressKey,
  creationThreadMediaCount,
  CreationThreadError,
  getCreationCapabilities,
  isCreationThreadRevisionConflict,
  refreshCreationThread,
  threadMessages,
  type CreationThread,
} from "@/lib/creation-thread-api";

function thread(overrides: Partial<CreationThread> = {}): CreationThread {
  return {
    id: "thread-1",
    status: "active",
    revision: 4,
    state: { edit_format: "montage", media: [{ media_id: "m1" }] },
    content_plan_id: "plan-1",
    active_plan_item_id: "item-1",
    active_creator_agent_session_id: "session-1",
    active_job_id: null,
    events: [],
    job: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("creation thread projection", () => {
  it("supports only the three Paper formats", () => {
    expect(creationFormat("montage")).toBe("montage");
    expect(creationFormat("narrated_planned")).toBe("narrated_planned");
    expect(creationFormat("subtitled")).toBe("subtitled");
    expect(creationFormat("day_vlog")).toBeNull();
  });

  it("uses the server capability when it exposes the PlanItem clip limit", () => {
    const capabilities = [
      { id: "montage", edit_format: "montage" as const, max_clips: 37 },
      { id: "talking-to-camera", edit_format: "subtitled" as const, limits: { max_clips: 1 } },
    ];
    expect(creationClipLimit(capabilities, "montage")).toBe(37);
    expect(creationClipLimit(capabilities, "subtitled")).toBe(1);
    expect(creationClipLimit([], "narrated_planned")).toBe(50);
  });

  it("preserves the server media policy in the capability response", async () => {
    const previousFetch = global.fetch;
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        formats: [{ id: "montage", edit_format: "montage", max_clips: 50 }],
        media: {
          clips: {
            max: 50,
            max_file_bytes: 4096,
            content_types: ["video/mp4"],
          },
        },
      }),
    } as Response);
    try {
      await expect(getCreationCapabilities()).resolves.toEqual({
        formats: [{ id: "montage", edit_format: "montage", max_clips: 50 }],
        media: {
          clips: {
            max: 50,
            max_file_bytes: 4096,
            content_types: ["video/mp4"],
          },
        },
      });
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("bypasses caches when refreshing mutable render state", async () => {
    const previousFetch = global.fetch;
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => thread(),
    } as Response);
    try {
      await refreshCreationThread("thread-1");
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/plan/creation-threads/thread-1",
        expect.objectContaining({ cache: "no-store" }),
      );
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("hydrates media count from durable state", () => {
    expect(creationThreadMediaCount(thread())).toBe(1);
    expect(creationThreadMediaCount(thread({ state: { media_count: 8 } }))).toBe(8);
  });

  it("projects structured events without dropping assistant artifacts", () => {
    const result = threadMessages(thread({ events: [
      { id: "1", sequence: 0, revision: 1, role: "assistant", event_type: "format_prompt", content: "Choose a format", payload: { kind: "select_format" }, created_at: "2026-01-01T00:00:00Z" },
      { id: "2", sequence: 1, revision: 2, role: "user", event_type: "user_message", content: "Make it intimate", payload: null, created_at: "2026-01-01T00:00:01Z" },
      { id: "3", sequence: 2, revision: 3, role: "assistant", event_type: "confirm_generation", content: "Here’s the direction", payload: { kind: "confirm_generation" }, created_at: "2026-01-01T00:00:02Z" },
    ] }));
    expect(result.map((item) => item.artifact)).toEqual(["format", undefined, "confirmation"]);
  });

  it("recognizes partial-ready and terminal failure states from authoritative job data", () => {
    expect(creationJobReady(thread({ job: { id: "j", status: "processing", variants: [{ render_status: "ready", output_url: "/cut.mp4" }] } }))).toBe(true);
    expect(creationJobFailed(thread({ job: { id: "j", status: "processing_failed", variants: [] } }))).toBe(true);
    expect(creationJobPartial(thread({ job: { id: "j", status: "ready", variants: [
      { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      { variant_id: "song_text", render_status: "failed", output_url: null },
    ] } }))).toBe(true);
    expect(creationJobReady(thread({ job: { id: "j", status: "variants_ready", variants: [
      {
        variant_id: "guided_story",
        render_status: "failed",
        render_generation_id: "generation-2",
        output_url: "/last-good-cut.mp4",
      },
    ] } }))).toBe(true);
    expect(creationJobPartial(thread({ job: { id: "j", status: "variants_ready", variants: [
      {
        variant_id: "guided_story",
        render_status: "failed",
        render_generation_id: "generation-2",
        output_url: "/last-good-cut.mp4",
      },
    ] } }))).toBe(true);
  });

  it("keeps polling while one variant is ready and another is still rendering", () => {
    expect(creationJobSettled(thread({ job: { id: "j", status: "variants_rendering", variants: [
      { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      { variant_id: "song_text", render_status: "rendering", output_url: null },
    ] } }))).toBe(false);
    expect(creationJobSettled(thread({ job: { id: "j", status: "variants_ready_partial", variants: [
      { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      { variant_id: "song_text", render_status: "failed", output_url: null },
    ] } }))).toBe(true);
    expect(creationJobSettled(thread({ job: { id: "j", status: "processing", variants: [
      { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
    ] } }))).toBe(false);
    expect(creationThreadInProgress(thread({ active_job_id: "j", job: {
      id: "j", status: "processing", variants: [
        { variant_id: "original_text", render_status: "ready", output_url: "/cut.mp4" },
      ],
    } }))).toBe(true);
  });

  it("stops polling when a terminal parent failure has stale rendering metadata", () => {
    const failed = thread({
      active_job_id: "j",
      job: {
        id: "j",
        status: "processing_failed",
        variants: [{ variant_id: "original_text", render_status: "rendering", output_url: null }],
      },
      state: { generation: { status: "rendering" } },
    });
    expect(creationJobSettled(failed)).toBe(true);
    expect(creationThreadInProgress(failed)).toBe(false);
  });

  it("falls back to projected progress and distinguishes exact revision conflicts", () => {
    const preparing = thread({
      creator_agent: null,
      state: { creator_agent: { status: "reviewing" }, generation: { status: "queued" } },
    });
    expect(creationThreadPreparing(preparing)).toBe(true);
    expect(creationThreadProgressKey(preparing)).toBe("::reviewing:queued:");
    expect(isCreationThreadRevisionConflict(new CreationThreadError("Creation thread changed", 409))).toBe(true);
    expect(isCreationThreadRevisionConflict(new CreationThreadError("Creator session is busy", 409))).toBe(false);
    expect(isCreationThreadRevisionConflict(new Error("Creation thread changed"))).toBe(false);
  });

  it("keeps a settled parent Job polling for an editor replacement generation", () => {
    const rendering = thread({
      active_job_id: "j",
      job: {
        id: "j",
        status: "variants_ready",
        variants: [{
          variant_id: "guided_story",
          render_generation_id: "generation-2",
          render_status: "rendering",
          render_finished_at: "2026-01-01T00:00:00Z",
          output_url: "/old-cut.mp4",
        }],
      },
    });
    const ready = {
      ...rendering,
      job: {
        ...rendering.job!,
        variants: [{
          ...rendering.job!.variants[0],
          render_status: "ready",
          render_finished_at: "2026-01-01T00:02:00Z",
          output_url: "/new-cut.mp4",
        }],
      },
    };

    expect(creationThreadInProgress(rendering)).toBe(true);
    expect(creationThreadInProgress(ready)).toBe(false);
    expect(creationThreadProgressKey(rendering)).not.toBe(creationThreadProgressKey(ready));
  });

  it("does not replay the initial strategy as a revision after the first cut is ready", () => {
    const event = { id: "strategy", sequence: 0, revision: 5, role: "assistant" as const,
      event_type: "agent_assistant_strategy", content: "Here is the direction", payload: null,
      created_at: "2026-01-01T00:00:00Z" };
    const generation = { id: "generation", sequence: 1, revision: 6, role: "system" as const,
      event_type: "action_generate", content: null, payload: { action: "generate" },
      created_at: "2026-01-01T00:00:01Z" };
    expect(threadMessages(thread({ events: [event] }))[0]?.artifact).toBe("confirmation");
    expect(threadMessages(thread({ events: [event, generation], active_job_id: "j", job: {
      id: "j", status: "ready", variants: [{ render_status: "ready", output_url: "/cut.mp4" }],
    } }))[0]?.artifact).not.toBe("revision");
  });

  it("marks a strategy after generation as a revision proposal", () => {
    const generation = { id: "generation", sequence: 1, revision: 6, role: "system" as const,
      event_type: "action_generate", content: null, payload: { action: "generate" },
      created_at: "2026-01-01T00:00:01Z" };
    const revision = { id: "revision", sequence: 2, revision: 7, role: "assistant" as const,
      event_type: "agent_assistant_strategy", content: "A tighter direction", payload: null,
      created_at: "2026-01-01T00:00:02Z" };
    expect(threadMessages(thread({ events: [generation, revision], active_job_id: "j", job: {
      id: "j", status: "ready", variants: [{ render_status: "ready", output_url: "/cut.mp4" }],
    } }))[0]?.artifact).toBe("revision");
  });

  it("keeps only the latest retry strategy actionable", () => {
    const events = [
      { id: "initial", sequence: 0, revision: 1, role: "assistant" as const,
        event_type: "agent_assistant_strategy", content: "First direction", payload: null,
        created_at: "2026-01-01T00:00:00Z" },
      { id: "generate", sequence: 1, revision: 2, role: "system" as const,
        event_type: "action_generate", content: null, payload: { action: "generate" },
        created_at: "2026-01-01T00:00:01Z" },
      { id: "retry-one-strategy", sequence: 2, revision: 3, role: "assistant" as const,
        event_type: "agent_assistant_strategy", content: "Try the first retry direction", payload: null,
        created_at: "2026-01-01T00:00:02Z" },
      { id: "failed", sequence: 3, revision: 4, role: "system" as const,
        event_type: "generation_failed", content: "That render failed", payload: null,
        created_at: "2026-01-01T00:00:03Z" },
      { id: "retry", sequence: 4, revision: 5, role: "system" as const,
        event_type: "action_retry", content: null, payload: { action: "retry" },
        created_at: "2026-01-01T00:00:04Z" },
      { id: "retry-two-strategy", sequence: 5, revision: 6, role: "assistant" as const,
        event_type: "agent_assistant_strategy", content: "Try the latest retry direction", payload: null,
        created_at: "2026-01-01T00:00:05Z" },
    ];
    const projected = threadMessages(thread({ events }));
    expect(projected.find((item) => item.id === "initial")?.artifact).toBeUndefined();
    expect(projected.find((item) => item.id === "retry-one-strategy")?.artifact).toBeUndefined();
    expect(projected.find((item) => item.id === "retry-two-strategy")?.artifact).toBe("revision");
  });

  it("recognizes a revision in a recovered Creator Agent event sequence", () => {
    const events = [
      { id: "initial-strategy", sequence: 0, revision: 1, role: "assistant" as const,
        event_type: "agent_assistant_strategy", content: "Your first direction", payload: null,
        created_at: "2026-01-01T00:00:00Z" },
      { id: "confirmation", sequence: 1, revision: 2, role: "assistant" as const,
        event_type: "agent_user_confirmation", content: "Confirmed", payload: null,
        created_at: "2026-01-01T00:00:01Z" },
      { id: "execution-started", sequence: 2, revision: 3, role: "assistant" as const,
        event_type: "agent_assistant_execution", content: "Render started", payload: { status: "started" },
        created_at: "2026-01-01T00:00:02Z" },
      { id: "execution-ready", sequence: 3, revision: 4, role: "assistant" as const,
        event_type: "agent_assistant_execution", content: "Render ready", payload: { status: "ready" },
        created_at: "2026-01-01T00:00:03Z" },
      { id: "revision-request", sequence: 4, revision: 5, role: "user" as const,
        event_type: "user_message", content: "Make the opening slower", payload: null,
        created_at: "2026-01-01T00:00:04Z" },
      { id: "revision-strategy", sequence: 5, revision: 6, role: "assistant" as const,
        event_type: "agent_assistant_strategy", content: "A slower opening", payload: null,
        created_at: "2026-01-01T00:00:05Z" },
    ];
    const projected = threadMessages(thread({ events }));
    expect(projected.find((item) => item.id === "initial-strategy")?.artifact).toBeUndefined();
    expect(projected.find((item) => item.id === "revision-strategy")?.artifact).toBe("revision");
  });
});
