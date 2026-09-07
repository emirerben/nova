import {
  applyCreationAction,
  creationFormat,
  creationClipLimit,
  creationJobFailed,
  creationJobPartial,
  creationJobReady,
  creationJobSettled,
  creationPlanningFailed,
  creationThreadInProgress,
  creationThreadNeedsPolling,
  creationThreadPreparing,
  creationThreadProgressKey,
  creationThreadMediaCount,
  cancelKriaTurn,
  creationGenerationArtifactKey,
  CreationThreadError,
  decideKriaApproval,
  getKriaDelta,
  getKriaDraft,
  getKriaApproval,
  creationSpeechCleanupActionId,
  getCreationCapabilities,
  deleteCreationThread,
  renameCreationThread,
  isCreationSpeechCleanupStaleConflict,
  isCreationThreadRevisionConflict,
  refreshCreationThread,
  sendKriaTurn,
  undoKriaDraft,
  latestCreationDirection,
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

  it("uses the project title and revision-fenced delete contracts", async () => {
    const previousFetch = global.fetch;
    const fetchMock = jest.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ ...thread(), title: "Harbor arrival" }) })
      .mockResolvedValueOnce({ ok: true, status: 204, json: async () => { throw new Error("204 has no body"); } });
    global.fetch = fetchMock as unknown as typeof fetch;
    try {
      await expect(renameCreationThread(thread(), " Harbor arrival ")).resolves.toMatchObject({ title: "Harbor arrival" });
      await expect(deleteCreationThread(thread())).resolves.toBeUndefined();
      expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/plan/creation-threads/thread-1");
      expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "PATCH" });
      expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toMatchObject({ title: "Harbor arrival", expected_revision: 4 });
      expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/plan/creation-threads/thread-1?expected_revision=4");
      expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ method: "DELETE" });
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
        "/api/plan/creation-threads/thread-1?projection=full",
        expect.objectContaining({ cache: "no-store" }),
      );
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("reads the pinned approval and submits its exact revision and fingerprint", async () => {
    const previousFetch = global.fetch;
    const approval = {
      approval_id: "approval-1",
      turn_id: "turn-1",
      draft_id: "draft-1",
      draft_revision: 7,
      status: "pending" as const,
      consequence_summary: "Render the selected cut",
      cost_summary: null,
      expires_at: "2026-01-01T00:10:00Z",
      approval_fingerprint: "a".repeat(64),
    };
    const fetchMock = jest.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => approval })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          approval_id: "approval-1",
          turn_id: "turn-1",
          status: "approved",
          thread_revision: 9,
          render_dispatched: false,
        }),
      });
    global.fetch = fetchMock as unknown as typeof fetch;
    try {
      await expect(getKriaApproval("thread-1", "approval-1")).resolves.toEqual(approval);
      await expect(decideKriaApproval("thread-1", approval, "approve", 8)).resolves.toMatchObject({
        status: "approved",
        render_dispatched: false,
      });
      expect(fetchMock.mock.calls[0]?.[0]).toBe(
        "/api/plan/creation-threads/thread-1/approvals/approval-1",
      );
      expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ cache: "no-store" });
      expect(fetchMock.mock.calls[1]?.[0]).toBe(
        "/api/plan/creation-threads/thread-1/approvals/approval-1/approve",
      );
      expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
        expected_thread_revision: 8,
        expected_draft_revision: 7,
        expected_approval_fingerprint: "a".repeat(64),
      });
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("submits runtime-v2 turns and reads forward-only event deltas", async () => {
    const previousFetch = global.fetch;
    const fetchMock = jest.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 202,
        json: async () => ({
          turn_id: "turn-1",
          thread_revision: 5,
          status: "pending",
          replayed: false,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          thread_id: "thread-1",
          runtime_version: 2,
          status: "active",
          thread_revision: 6,
          events: [],
          after_sequence: 3,
          next_after_sequence: 3,
          has_more: false,
        }),
      });
    global.fetch = fetchMock as unknown as typeof fetch;
    try {
      await sendKriaTurn(thread({ runtime_version: 2 }), "Use the whisk", "client-turn-1");
      await getKriaDelta("thread-1", 3, 25);
      expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
        message: "Use the whisk",
        client_event_id: "client-turn-1",
        expected_thread_revision: 4,
      });
      expect(fetchMock.mock.calls[1]?.[0]).toBe(
        "/api/plan/creation-threads/thread-1?after_sequence=3&limit=25",
      );
      expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ cache: "no-store" });
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("cancels one revision-fenced queued Kria turn", async () => {
    const previousFetch = global.fetch;
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        turn_id: "turn-queued",
        thread_revision: 8,
        status: "cancelled",
        approval_ids: [],
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    try {
      await cancelKriaTurn("thread-1", "turn-queued", 7);
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/plan/creation-threads/thread-1/turns/turn-queued/cancel",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ expected_thread_revision: 7 }),
        }),
      );
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("reads and undoes the exact authoritative draft revision", async () => {
    const previousFetch = global.fetch;
    const draft = {
      draft_id: "draft-1",
      item_id: "item-1",
      variant_key: "initial",
      draft_revision: 3,
      snapshot_hash: "b".repeat(64),
      etag: `"${"b".repeat(64)}"`,
      base_job_id: null,
      base_generation_id: null,
      snapshot: { schema_version: 2, kind: "strategy", changes: ["Open on whisk"] },
      can_undo: true,
      created_at: "2026-01-01T00:00:00Z",
    };
    const fetchMock = jest.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => draft })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...draft, draft_revision: 4 }),
      });
    global.fetch = fetchMock as unknown as typeof fetch;
    try {
      await expect(getKriaDraft("thread-1")).resolves.toEqual(draft);
      await expect(undoKriaDraft("thread-1", 3)).resolves.toMatchObject({
        draft_revision: 4,
      });
      expect(fetchMock.mock.calls[0]?.[0]).toBe(
        "/api/plan/creation-threads/thread-1/draft",
      );
      expect(fetchMock.mock.calls[1]?.[0]).toBe(
        "/api/plan/creation-threads/thread-1/draft/undo",
      );
      expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
        expected_draft_revision: 3,
      });
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("preserves Kria's typed problem on client errors", async () => {
    const previousFetch = global.fetch;
    const problem = {
      code: "stale_revision",
      phase: "approval" as const,
      message: "Creation thread changed",
      retryable: true,
      recovery: "refresh_replan" as const,
      trace_id: "trace-1",
      current_revision: 12,
      target: { approval_id: "approval-1" },
    };
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ problem }),
    } as Response);
    try {
      const call = getKriaApproval("thread-1", "approval-1");
      await expect(call).rejects.toMatchObject({
        message: "Creation thread changed",
        status: 409,
        problem,
      });
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("recognizes revision conflict responses", () => {
    expect(isCreationThreadRevisionConflict(new CreationThreadError("Creation thread changed", 409))).toBe(true);
    expect(isCreationThreadRevisionConflict(new CreationThreadError("server", 500))).toBe(false);
  });

  it("does not mislabel unrelated 409s as a changed speech check", () => {
    expect(isCreationSpeechCleanupStaleConflict(
      new CreationThreadError("speech_cleanup_analysis_changed", 409),
    )).toBe(true);
    expect(isCreationSpeechCleanupStaleConflict(
      new CreationThreadError("speech_cleanup_pending", 409),
    )).toBe(false);
    expect(isCreationSpeechCleanupStaleConflict(
      new CreationThreadError("That render variant is unavailable", 409),
    )).toBe(false);
    expect(isCreationSpeechCleanupStaleConflict(
      new CreationThreadError("speech_cleanup_analysis_changed", 500),
    )).toBe(false);
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

  it("renders only semantic turns and never echoes the durable intent as assistant speech", () => {
    const intent = "Make the matcha update warm and quick";
    const result = threadMessages(thread({
      state: { edit_format: "montage", intent },
      events: [
        { id: "created", sequence: 0, revision: 1, role: "system", event_type: "thread_created", content: intent, payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "user", sequence: 1, revision: 2, role: "user", event_type: "user_message", content: intent, payload: null, created_at: "2026-01-01T00:00:01Z" },
        { id: "mirrored-user", sequence: 2, revision: 3, role: "system", event_type: "agent_user_message", content: intent, payload: null, created_at: "2026-01-01T00:00:02Z" },
        { id: "planning", sequence: 3, revision: 4, role: "assistant", event_type: "agent_assistant_auto_iteration_queued", content: "Planning another attempt", payload: null, created_at: "2026-01-01T00:00:03Z" },
        { id: "echo", sequence: 4, revision: 5, role: "assistant", event_type: "agent_assistant_strategy", content: intent, payload: null, created_at: "2026-01-01T00:00:04Z" },
        { id: "question", sequence: 5, revision: 6, role: "assistant", event_type: "agent_assistant_question", content: "Should the first sip or the packing table open the story?", payload: null, created_at: "2026-01-01T00:00:05Z" },
      ],
    }));

    expect(result.map((message) => message.id)).toEqual(["user", "echo", "question"]);
    expect(result.find((message) => message.id === "echo")).toMatchObject({
      content: "",
      artifact: "confirmation",
    });
    expect(result.filter((message) => message.content === intent)).toHaveLength(1);
  });

  it("renders a runtime-v2 assistant response as a conversation turn", () => {
    const result = threadMessages(thread({ events: [
      { id: "user", sequence: 0, revision: 1, role: "user", event_type: "user_message", content: "Use the whisk as the opening", payload: null, created_at: "2026-01-01T00:00:00Z" },
      { id: "response", sequence: 1, revision: 2, role: "assistant", event_type: "assistant_response", content: "The whisk close-up is the strongest opening; I’ve shaped the draft around it.", payload: { kind: "observed_result" }, created_at: "2026-01-01T00:00:01Z" },
    ] }));

    expect(result).toEqual([
      expect.objectContaining({ id: "user", role: "user", content: "Use the whisk as the opening" }),
      expect.objectContaining({
        id: "response",
        role: "assistant",
        content: "The whisk close-up is the strongest opening; I’ve shaped the draft around it.",
      }),
    ]);
  });

  it("projects runtime-v2 draft, approval, and render lifecycle artifacts", () => {
    const result = threadMessages(thread({
      runtime_version: 2,
      active_job_id: "job-1",
      events: [
        { id: "draft", sequence: 0, revision: 1, role: "assistant", event_type: "draft_applied", content: "Open on the whisk.", payload: { draft_id: "draft-1", draft_revision: 2, changes: ["Tighter opening"] }, created_at: "2026-01-01T00:00:00Z" },
        { id: "approval", sequence: 1, revision: 2, role: "system", event_type: "approval_requested", content: null, payload: { approval_id: "approval-1", draft_revision: 2 }, created_at: "2026-01-01T00:00:01Z" },
        { id: "queued", sequence: 2, revision: 3, role: "system", event_type: "render_queued", content: null, payload: { job_id: "job-1" }, created_at: "2026-01-01T00:00:02Z" },
      ],
    }));

    expect(result).toEqual([
      expect.objectContaining({ id: "draft", artifact: "draft", content: "Open on the whisk." }),
      expect.objectContaining({ id: "approval", artifact: "approval", payload: expect.objectContaining({ approval_id: "approval-1" }) }),
      expect.objectContaining({ artifact: "progress", content: "" }),
    ]);
  });

  it("coalesces lifecycle callbacks by exact generation identity", () => {
    const result = threadMessages(thread({
      active_job_id: "job-1",
      events: [
        { id: "queued-one", sequence: 0, revision: 1, role: "system", event_type: "generation_started", content: "Queued", payload: { job_id: "job-1", variant_id: "main", generation_id: "gen-1" }, created_at: "2026-01-01T00:00:00Z" },
        { id: "ready-one", sequence: 1, revision: 2, role: "system", event_type: "generation_ready", content: "Ready", payload: { job_id: "job-1", variant_id: "main", generation_id: "gen-1" }, created_at: "2026-01-01T00:00:01Z" },
        { id: "queued-two", sequence: 2, revision: 3, role: "system", event_type: "generation_started", content: "Queued again", payload: { job_id: "job-1", variant_id: "main", generation_id: "gen-2" }, created_at: "2026-01-01T00:00:02Z" },
      ],
    }));

    expect(result).toHaveLength(2);
    expect(result[0]).toMatchObject({
      id: "thread-1:generation:job:job-1:variant:main:generation:gen-1",
      artifact: "result",
      content: "",
    });
    expect(result[1]).toMatchObject({
      id: "thread-1:generation:job:job-1:variant:main:generation:gen-2",
      artifact: "progress",
      content: "",
    });
  });

  it("keeps the active lifecycle key stable across status changes", () => {
    const rendering = thread({ active_job_id: "job-1", job: {
      id: "job-1", status: "rendering", variants: [{
        variant_id: "main", render_generation_id: "gen-7", render_status: "rendering",
      }],
    } });
    const ready = thread({ active_job_id: "job-1", job: {
      id: "job-1", status: "ready", variants: [{
        variant_id: "main", render_generation_id: "gen-7", render_status: "ready", output_url: "/cut.mp4",
      }],
    } });
    expect(creationGenerationArtifactKey(rendering)).toBe(creationGenerationArtifactKey(ready));
  });

  it("keeps project rename receipts out of the creative conversation", () => {
    const result = threadMessages(thread({ events: [
      { id: "1", sequence: 0, revision: 1, role: "system", event_type: "thread_renamed", content: null, payload: { title: "Harbor arrival" }, created_at: "2026-01-01T00:00:00Z" },
      { id: "2", sequence: 1, revision: 2, role: "user", event_type: "user_message", content: "Open on the harbor", payload: null, created_at: "2026-01-01T00:00:01Z" },
    ] }));
    expect(result).toHaveLength(1);
    expect(result[0]?.content).toBe("Open on the harbor");
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

  it("classifies creator planning errors separately from render Jobs", () => {
    const planningFailure = thread({
      creator_agent: { status: "failed" },
      events: [{ id: "error", sequence: 1, revision: 1, role: "assistant", event_type: "agent_assistant_error", content: null, payload: { message: "Unavailable" }, created_at: "2026-01-01T00:00:00Z" }],
    });
    expect(creationPlanningFailed(planningFailure)).toBe(true);
    expect(creationPlanningFailed(thread({
      ...planningFailure,
      job: { id: "job-1", status: "processing_failed", variants: [] },
    }))).toBe(false);
    expect(creationPlanningFailed(thread({
      ...planningFailure,
      creator_agent: { status: "planning" },
      events: [
        ...planningFailure.events,
        { id: "retry", sequence: 2, revision: 2, role: "user", event_type: "user_message", content: "Try again", payload: null, created_at: "2026-01-01T00:00:02Z" },
      ],
    }))).toBe(false);
    expect(creationPlanningFailed(thread({
      ...planningFailure,
      creator_agent: null,
      events: [
        ...planningFailure.events,
        { id: "retry", sequence: 2, revision: 2, role: "user", event_type: "user_message", content: "Try again", payload: null, created_at: "2026-01-01T00:00:02Z" },
      ],
    }))).toBe(false);
    expect(creationPlanningFailed(thread({
      ...planningFailure,
      creator_agent: null,
      events: [
        ...planningFailure.events,
        { id: "retry", sequence: 2, revision: 2, role: "user", event_type: "user_message", content: "Try again", payload: null, created_at: "2026-01-01T00:00:02Z" },
        { id: "strategy-retry", sequence: 3, revision: 3, role: "assistant", event_type: "agent_assistant_strategy", content: "Here is the revised direction", payload: null, created_at: "2026-01-01T00:00:03Z" },
      ],
    }))).toBe(false);
    expect(latestCreationDirection(thread({
      ...planningFailure,
      events: [
        { id: "old", sequence: 0, revision: 1, role: "user", event_type: "user_message", content: "Old direction", payload: null, created_at: "2026-01-01T00:00:00Z" },
        { id: "new", sequence: 2, revision: 2, role: "user", event_type: "user_message", content: "New direction", payload: null, created_at: "2026-01-01T00:00:02Z" },
      ],
    }))).toBe("New direction");
  });

  it.each(["assistant_error", "agent_assistant_error"]) ("projects %s as a failure artifact", (eventType) => {
    const result = threadMessages(thread({ events: [{
      id: "error", sequence: 1, revision: 1, role: "assistant", event_type: eventType, content: null,
      payload: { message: "Planning failed" }, created_at: "2026-01-01T00:00:00Z",
    }] }));
    expect(result).toEqual([expect.objectContaining({ artifact: "failure", content: "Planning failed" })]);
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

  it("polls speech preflight without broadening render progress semantics", () => {
    const queued = thread({
      speech_cleanup: {
        applicable: true,
        analysis: { id: "analysis-1", status: "queued" },
        decision: null,
        requires_choice: false,
        render_blocker: null,
        outcome: null,
      },
    });
    const running = {
      ...queued,
      speech_cleanup: {
        ...queued.speech_cleanup!,
        analysis: { id: "analysis-1", status: "running" as const },
      },
    };
    const ready = {
      ...queued,
      speech_cleanup: {
        ...queued.speech_cleanup!,
        analysis: { id: "analysis-1", status: "ready" as const, has_findings: true },
        requires_choice: true,
      },
    };

    expect(creationThreadInProgress(queued)).toBe(false);
    expect(creationThreadNeedsPolling(queued)).toBe(true);
    expect(creationThreadNeedsPolling(running)).toBe(true);
    expect(creationThreadNeedsPolling(ready)).toBe(false);
    expect(creationThreadProgressKey(queued)).not.toBe(creationThreadProgressKey(running));
    expect(creationThreadProgressKey(running)).not.toBe(creationThreadProgressKey(ready));
  });

  it("keeps old detail/list responses compatible and builds stable cleanup action IDs", () => {
    expect(creationThreadNeedsPolling(thread())).toBe(false);
    expect(creationSpeechCleanupActionId("analysis-1", "clean", 4)).toBe("speech-cleanup:analysis-1:r4:clean");
    expect(creationSpeechCleanupActionId("analysis-1", "clean", 5)).toBe("speech-cleanup:analysis-1:r5:clean");
    expect(creationSpeechCleanupActionId("analysis-1", "no_findings", 4)).toBe("speech-cleanup:analysis-1:r4:no_findings");
    expect(creationSpeechCleanupActionId("analysis-1", "bypass", 5)).toBe("speech-cleanup:analysis-1:r5:bypass");
  });

  it("forwards AbortSignal and serializes an atomic cleanup generate action", async () => {
    const previousFetch = global.fetch;
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => thread() } as Response);
    const controller = new AbortController();
    try {
      await refreshCreationThread("thread-1", controller.signal);
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/plan/creation-threads/thread-1?projection=full",
        expect.objectContaining({ cache: "no-store", signal: controller.signal }),
      );

      await applyCreationAction(
        thread(),
        "generate",
        { speech_cleanup_analysis_id: "analysis-1", speech_cleanup_choice: "clean" },
        creationSpeechCleanupActionId("analysis-1", "clean", 4),
      );
      expect(global.fetch).toHaveBeenLastCalledWith(
        "/api/plan/creation-threads/thread-1/actions",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            action: "generate",
            payload: { speech_cleanup_analysis_id: "analysis-1", speech_cleanup_choice: "clean" },
            client_action_id: "speech-cleanup:analysis-1:r4:clean",
            expected_revision: 4,
          }),
        }),
      );
    } finally {
      global.fetch = previousFetch;
    }
  });

  it("serializes no-findings, analysis retry, render retry, and bypass as distinct actions", async () => {
    const previousFetch = global.fetch;
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => thread() } as Response);
    try {
      const source = thread();
      await applyCreationAction(
        source,
        "generate",
        { speech_cleanup_analysis_id: "analysis-1" },
        creationSpeechCleanupActionId("analysis-1", "no_findings", source.revision),
      );
      await applyCreationAction(
        source,
        "retry_speech_cleanup",
        { speech_cleanup_analysis_id: "analysis-1" },
        creationSpeechCleanupActionId("analysis-1", "retry_analysis", source.revision),
      );
      await applyCreationAction(
        source,
        "retry",
        { speech_cleanup_analysis_id: "analysis-1" },
        creationSpeechCleanupActionId("analysis-1", "retry_render", source.revision),
      );
      await applyCreationAction(
        source,
        "create_without_cleanup",
        { speech_cleanup_analysis_id: "analysis-1" },
        creationSpeechCleanupActionId("analysis-1", "bypass", source.revision),
      );

      const bodies = jest.mocked(global.fetch).mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
      expect(bodies).toEqual([
        expect.objectContaining({
          action: "generate",
          payload: { speech_cleanup_analysis_id: "analysis-1" },
          client_action_id: "speech-cleanup:analysis-1:r4:no_findings",
        }),
        expect.objectContaining({
          action: "retry_speech_cleanup",
          payload: { speech_cleanup_analysis_id: "analysis-1" },
          client_action_id: "speech-cleanup:analysis-1:r4:retry_analysis",
        }),
        expect.objectContaining({
          action: "retry",
          payload: { speech_cleanup_analysis_id: "analysis-1" },
          client_action_id: "speech-cleanup:analysis-1:r4:retry_render",
        }),
        expect.objectContaining({
          action: "create_without_cleanup",
          payload: { speech_cleanup_analysis_id: "analysis-1" },
          client_action_id: "speech-cleanup:analysis-1:r4:bypass",
        }),
      ]);
    } finally {
      global.fetch = previousFetch;
    }
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

  it("keeps media-added audit events out of the rendered transcript", () => {
    const projected = threadMessages(thread({ events: [
      { id: "direction", sequence: 0, revision: 1, role: "user", event_type: "user_message", content: "Keep the harbor opening", payload: null, created_at: "2026-01-01T00:00:00Z" },
      { id: "upload-prompt", sequence: 1, revision: 2, role: "assistant", event_type: "upload_prompt", content: null, payload: { kind: "upload_prompt" }, created_at: "2026-01-01T00:00:01Z" },
      { id: "media-one", sequence: 2, revision: 3, role: "user", event_type: "media_added", content: null, payload: { media_count: 1 }, created_at: "2026-01-01T00:00:02Z" },
      { id: "media-two", sequence: 3, revision: 4, role: "user", event_type: "media_added", content: null, payload: { media_count: 2 }, created_at: "2026-01-01T00:00:03Z" },
      { id: "media-three", sequence: 4, revision: 5, role: "user", event_type: "media_added", content: null, payload: { media_count: 3 }, created_at: "2026-01-01T00:00:04Z" },
      { id: "follow-up", sequence: 5, revision: 6, role: "user", event_type: "user_message", content: "Use a quick pace", payload: null, created_at: "2026-01-01T00:00:05Z" },
    ] }));
    expect(projected.map((item) => item.id)).toEqual(["direction", "upload-prompt", "follow-up"]);
    expect(projected.filter((item) => item.artifact === "upload")).toHaveLength(1);
  });
});
