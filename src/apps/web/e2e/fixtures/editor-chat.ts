/** Deterministic HTTP fixture around the real workspace and embedded EditorShell.
 * No model, storage or render service is contacted. Also used by /browse. */
export function installEditorChatFixture() {
  const original = window.fetch.bind(window);
  const root = window.parent === window ? window : window.parent;
  const fixture = root as Window & { editorChatFixture?: { requests: Array<{path: string; body: any}>; events: any[]; text: string } };
  const data = fixture.editorChatFixture ??= { requests: [], events: [], text: "Original hook" };
  const variant = () => ({
    variant_id: "original_text", render_status: "ready", render_generation_id: "generation-1",
    output_url: "/landing/raw-story/alberobello.mp4", base_video_url: "/landing/raw-story/alberobello.mp4",
    duration_s: 12, text_mode: "agent_text", resolved_archetype: "montage",
    text_elements: [{ id: "hook", text: data.text, role: "generative_intro", start_s: 0, end_s: 10,
      font_family: "Montserrat", size_px: 72, position: "middle", color: "#FFFFFF" }],
    editor_capabilities: { text_elements: true, timeline: false, mix: false, suggestions: true },
  });
  const thread = () => ({ id: "editor-chat-fixture", title: "Morning in Italy", status: "active", runtime_version: 1,
    revision: data.events.length + 1, state: { edit_format: "montage", media: [], selected_variant_id: "original_text" },
    active_plan_item_id: "fixture-item", active_job_id: "fixture-job", content_plan_id: "fixture-plan",
    active_creator_agent_session_id: null, events: data.events,
    job: { id: "fixture-job", status: "variants_ready", variants: [variant()] },
    created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z" });
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.origin);
    if (!url.pathname.startsWith("/api/") && !["/music-tracks", "/sound-effects"].includes(url.pathname)) return original(input, init);
    const path = url.pathname;
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    data.requests.push({ path, body });
    let result: unknown = {};
    if (path === "/api/auth/session") result = { user: { name: "Editor fixture", email: "fixture@example.test" }, expires: "2099-01-01T00:00:00Z" };
    else if (path.endsWith("/capabilities")) result = { formats: [{ id: "montage", edit_format: "montage" }] };
    else if (path.endsWith("/editor-events")) {
      for (const message of body.messages) if (!data.events.some((event) => event.payload.editor_message_id === message.id)) {
        data.events.push({ id: message.id, sequence: data.events.length + 1, revision: data.events.length + 2,
          role: message.role, content: message.text, event_type: `editor_${message.role}_message`,
          payload: { editor_message_id: message.id, applied: message.applied, rejected: message.rejected }, created_at: "2026-09-10T00:00:00Z" });
      }
      result = thread();
    } else if (path === "/api/plan/creation-threads") result = [thread()];
    else if (path.startsWith("/api/plan/creation-threads/")) result = thread();
    else if (path.endsWith("/copilot/turn")) {
      const text = body.message.toLowerCase().includes("second") ? "Second hook" : "Fresh morning in Italy";
      const ops = body.message.includes("?") ? [] : [{ op: "edit_text", bar_index: 0, text }];
      result = { ops, confidence: 0.99, reply: ops.length ? "Updated the opening text." : "The opening text is visible for ten seconds.",
        outcome: ops.length ? "applied" : "answer", suggestions: [], needs_clarification: false };
    } else if (path === "/api/plan/plan-items/fixture-item") result = { id: "fixture-item", theme: "Morning in Italy", idea: "Morning in Italy", current_job_id: "fixture-job", clip_gcs_paths: [] };
    else if (path.endsWith("/timeline")) result = { slots: [], beat_grid: [], clips: [], baseline_slots: [] };
    else if (path.endsWith("/status")) result = { id: "fixture-job", status: "variants_ready", variants: [variant()] };
    else if (path.endsWith("/editor-commit")) { data.text = body.text_elements?.[0]?.text ?? data.text; result = { status: "rendering", render_generation_id: "generation-2" }; }
    else if (path.includes("music")) result = { tracks: [] };
    else if (path.includes("sound-effects")) result = { effects: [] };
    else if (path.includes("visual")) result = [];
    else if (path.includes("overlay-suggestions")) result = { suggestions: [] };
    return new Response(JSON.stringify(result), { status: 200, headers: { "Content-Type": "application/json" } });
  };
}
