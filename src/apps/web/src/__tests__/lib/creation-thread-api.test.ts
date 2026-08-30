import {
  creationFormat,
  creationJobFailed,
  creationJobPartial,
  creationJobReady,
  creationJobSettled,
  creationThreadMediaCount,
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
    expect(projected.find((item) => item.id === "initial-strategy")?.artifact).toBe("confirmation");
    expect(projected.find((item) => item.id === "revision-strategy")?.artifact).toBe("revision");
  });
});
