const $ = id => document.getElementById(id);
const state = { config: null, queue: null, detail: null, cursor: null, filter: "all", selected: null, pointers: new Set(), dirty: false, busy: false, load: 0 };
const labels = { unreviewed: "Needs review", approved: "Intent approved", needs_clarification: "Needs clarification", rejected: "Rejected", superseded: "New capture available" };
const messages = {
  source_changed_wait_for_collection: "The conversation changed. Refresh after the next collection, then review the new capture.",
  source_superseded: "A newer capture replaced this one. Refresh and open the current session.",
  review_changed_reload: "Someone changed this review. Refresh the case before saving again.",
  collector_busy_retry: "Collection is running. Try again shortly.",
  case_unavailable: "This case is unavailable. It may have expired or lost consent.",
  intent_review_required: "Approve the expected outcome before exporting.",
  approval_requires_expectations_and_complete_capture: "Approval needs a complete capture and at least one expected outcome.",
};
function show(id, text) { $(id).textContent = text; $(id).hidden = !text; }
function element(tag, text, className) { const node = document.createElement(tag); if (text) node.textContent = text; if (className) node.className = className; return node; }
function date(value) { return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
async function api(path = "", init = {}) {
  const response = await fetch(`/api/cases${path}`, { ...init, cache: "no-store", headers: { "Content-Type": "application/json", "X-Review-Session": state.config.session } });
  const data = await response.json();
  if (!response.ok) throw new Error(messages[data.detail] || (typeof data.detail === "string" ? data.detail : "Check the review fields and try again."));
  return data;
}
function mayLeave() { return !state.dirty || confirm("Discard the changes to this unsaved review?"); }
function setBusy(value) {
  state.busy = value;
  $("refresh").disabled = value; $("filter").disabled = value;
  $("save").disabled = value; $("export").disabled = value;
  $("review-fields").disabled = value || state.detail?.review_state === "superseded";
  document.querySelectorAll(".case-button, .pager button, .message input").forEach(node => { node.disabled = value; });
  $("review").setAttribute("aria-busy", String(value));
}
async function loadQueue() {
  show("error", ""); $("queue-loading").hidden = false; $("queue-empty").hidden = true;
  const params = new URLSearchParams({ review_state: state.filter });
  if (state.cursor) params.set("cursor", state.cursor);
  try {
    state.queue = await api(`?${params}`);
    const queue = state.queue, collector = queue.collector;
    const configured = collector.scoped_users + collector.scoped_threads > 0;
    $("collection-summary").textContent = `${collector.enabled && configured ? "Automatic collection · every 15 minutes" : "Collection is paused or needs a scope"} · ${collector.last_run_at ? "Last run " + date(collector.last_run_at) : "Not run yet"}`;
    $("collection-detail").replaceChildren(element("p", `${collector.scoped_users} scoped accounts · ${collector.scoped_threads} scoped sessions. Current training consent is checked on capture and review.`));
    if (!collector.enabled || !configured) $("collection-detail").append(element("p", "To start, enable REQUEST_FOLLOWING_COLLECTION_ENABLED and set an explicit account or session scope on the API and worker. The local app does not change these settings."));
    if (collector.last_run) $("collection-detail").append(element("pre", JSON.stringify(collector.last_run, null, 2)));
    $("collection-detail").append(element("p", queue.coverage_note));
    $("cases").replaceChildren();
    for (const item of queue.items) {
      const button = element("button", "", "case-button"); button.type = "button";
      button.setAttribute("aria-pressed", String(item.id === state.selected));
      button.append(element("span", labels[item.review_state] || item.review_state, `pill ${item.review_state}`));
      button.append(element("span", item.request_preview || `Session ${item.thread_id.slice(0, 8)}`, "case-preview"));
      button.append(element("span", date(item.created_at), "case-meta"));
      const failures = item.diagnostics.failed_turns.length, broken = item.diagnostics.broken_edges.length;
      if (failures || broken) button.append(element("span", `${failures} failed turns · ${broken} broken links`, "signal"));
      button.addEventListener("click", () => { if (!state.busy && mayLeave()) void openCase(item.id); });
      const li = element("li"); li.append(button); $("cases").append(li);
    }
    $("page-count").textContent = `${queue.items.length} on this page`;
    $("queue-empty").hidden = queue.items.length > 0;
    $("queue-empty").textContent = state.filter === "all" ? "No eligible sessions on this page. Collection needs a selected scope and current consent. Held-out sessions stay private." : "No sessions with this review status on this page.";
    show("holdouts", queue.holdout_count ? `${queue.holdout_count} held-out sessions on this page. Their content is withheld.` : "");
    $("next-page").hidden = !queue.next_cursor; $("first-page").hidden = !state.cursor;
  } catch (error) { show("error", error.message); }
  finally { $("queue-loading").hidden = true; }
}
async function openCase(id) {
  const request = ++state.load;
  state.selected = id; state.dirty = false;
  $("choose").hidden = true; $("case-detail").hidden = true; $("detail-loading").hidden = false;
  show("error", ""); show("review-error", ""); show("notice", "");
  setBusy(true);
  try {
    const detail = await api(`/${id}`);
    if (request !== state.load) return;
    state.detail = detail;
    renderCase(detail);
    $("case-detail").hidden = false;
    $("case-title").focus({ preventScroll: true });
    if (matchMedia("(max-width:700px)").matches) $("review").scrollIntoView({ behavior: "auto", block: "start" });
    document.querySelectorAll(".case-button").forEach((node, index) => node.setAttribute("aria-pressed", String(state.queue.items[index].id === id)));
  } catch (error) { show("error", error.message); }
  finally { if (request === state.load) { $("detail-loading").hidden = true; setBusy(false); updateSaveHelp(); } }
}
function renderCase(detail) {
  const events = detail.capture.sections.events || [], review = detail.review;
  const first = events.find(e => e.kind === "user_message" && e.content);
  $("case-title").textContent = first?.content?.slice(0, 130) || "Review this session";
  $("case-date").textContent = `Captured ${date(detail.created_at)} · session ${detail.thread_id.slice(0, 8)}`;
  $("review-state").textContent = labels[detail.review_state] || detail.review_state;
  $("review-state").className = `pill ${detail.review_state}`;
  const missing = Object.keys(detail.capture.missing);
  show("case-diagnostics", detail.review_state === "superseded" ? "A newer capture replaced this one. Open the current session before reviewing." : missing.length ? `Capture incomplete: ${missing.join(", ")}. Approval is unavailable until the missing evidence is collected.` : "");
  state.pointers = new Set((review?.expectations || []).flatMap(e => e.source_pointers));
  $("conversation").replaceChildren();
  events.forEach((event, index) => {
    if (!event.content) return;
    const creator = event.kind === "user_message" && event.actor === "user";
    const li = element("li", "", `message ${creator ? "creator" : "assistant"}`);
    if (creator) {
      const pointer = `/sections/events/${index}/content`;
      const label = element("label"); const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.checked = state.pointers.has(pointer);
      checkbox.addEventListener("change", () => { checkbox.checked ? state.pointers.add(pointer) : state.pointers.delete(pointer); state.dirty = true; updateSaveHelp(); });
      label.append(checkbox, document.createTextNode(`Creator message ${event.sequence}`)); li.append(label);
    } else li.append(element("div", `${event.actor} · message ${event.sequence}`, "role"));
    li.append(element("p", event.content)); $("conversation").append(li);
  });
  if (!$("conversation").children.length) $("conversation").append(element("li", "No message text was available in this capture.", "muted small"));
  $("turns").replaceChildren();
  for (const [index, turn] of (detail.capture.sections.turns || []).entries()) {
    const card = element("article", "", "turn"); card.append(element("h4", `Turn ${index + 1} · ${turn.status}`));
    const plan = turn.plan;
    if (plan) {
      const humanSummary = plan.summary || plan.reply || plan.explanation;
      if (typeof humanSummary === "string") card.append(element("p", humanSummary));
      const raw = element("details", "", "technical"); raw.append(element("summary", "Recorded plan"), element("pre", JSON.stringify(plan, null, 2))); card.append(raw);
    }
    for (const execution of turn.executions || []) card.append(element("p", `${execution.tool_name || "Operation"}: ${execution.status}`));
    if (turn.error) card.append(element("pre", JSON.stringify(turn.error, null, 2)));
    const receipts = element("details", "", "technical"); receipts.append(element("summary", "Tool receipts"), element("pre", JSON.stringify(turn.executions || [], null, 2))); card.append(receipts);
    $("turns").append(card);
  }
  if (!$("turns").children.length) $("turns").append(element("p", "No runtime-v2 turn records were available. Read the assistant messages above; they are not execution proof.", "muted small"));
  const { events: _events, turns: _turns, ...saved } = detail.capture.sections;
  $("saved-evidence").textContent = JSON.stringify({ ...saved, missing: detail.capture.missing }, null, 2);
  $("expected").value = (review?.expectations || []).map(e => e.text).join("\n");
  $("reviewer").value = review?.reviewer || ""; $("rationale").value = review?.rationale || "";
  $("decision").value = review?.decision || "needs_clarification"; $("stage").value = review?.loss_stage || "unknown"; $("tickets").value = (review?.tickets || []).join(", ");
  $("decision").querySelector('[value="approved"]').disabled = missing.length > 0;
  $("export").hidden = review?.decision !== "approved";
  state.dirty = false;
  updateSaveHelp();
}
function updateSaveHelp() {
  if (!state.detail) return;
  const approve = $("decision").value === "approved";
  const incomplete = Object.keys(state.detail.capture.missing).length > 0;
  $("expected").required = approve;
  const unsupported = approve && (!state.pointers.size || incomplete);
  $("save").disabled = state.busy || unsupported || state.detail.review_state === "superseded";
  $("save-help").textContent = unsupported ? "Select at least one creator message and use a complete capture to approve intent." : `Private capture expires ${date(state.detail.expires_at)}.`;
}
$("review-form").addEventListener("input", () => { state.dirty = true; });
$("decision").addEventListener("change", () => { state.dirty = true; updateSaveHelp(); });
$("review-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.busy) return;
  show("review-error", ""); show("notice", "");
  const detail = state.detail;
  const body = { source_sha256: detail.source_sha256, review_version: detail.review_version, decision: $("decision").value, reviewer: $("reviewer").value, rationale: $("rationale").value, loss_stage: $("stage").value, tickets: $("tickets").value.split(/[,\s]+/).filter(Boolean), expectations: $("expected").value.trim() ? [{ text: $("expected").value, source_pointers: [...state.pointers] }] : [] };
  if (body.expectations.length && !state.pointers.size) { show("review-error", "Select the creator messages that support this expected outcome."); return; }
  setBusy(true);
  try {
    await api(`/${detail.id}/review`, { method: "POST", body: JSON.stringify(body) });
    state.dirty = false;
    await loadQueue(); await openCase(detail.id);
    show("notice", "Review saved. The expected outcome is recorded; execution and video evidence still need their own checks.");
  } catch (error) { show("review-error", error.message); }
  finally { setBusy(false); updateSaveHelp(); }
});
$("export").addEventListener("click", async () => {
  if (state.dirty) { show("review-error", "Save your changes before exporting the reviewed packet."); return; }
  setBusy(true); show("review-error", "");
  try {
    const packet = await api(`/${state.detail.id}/export`);
    const url = URL.createObjectURL(new Blob([JSON.stringify(packet, null, 2)], { type: "application/json" }));
    const link = element("a"); link.href = url; link.download = `request-following-${state.detail.id}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    show("notice", "Private packet downloaded. Use request-following-loop.py import-queue to add a runnable regression and review it for promotion. Keep the packet outside git.");
  } catch (error) { show("review-error", error.message); }
  finally { setBusy(false); updateSaveHelp(); }
});
$("refresh").addEventListener("click", async () => { if (!mayLeave()) return; await loadQueue(); if (state.selected) await openCase(state.selected); });
$("filter").addEventListener("change", async () => { if (!mayLeave()) { $("filter").value = state.filter; return; } state.filter = $("filter").value; state.cursor = null; await loadQueue(); });
$("first-page").addEventListener("click", async () => { if (mayLeave()) { state.cursor = null; await loadQueue(); } });
$("next-page").addEventListener("click", async () => { if (mayLeave()) { state.cursor = state.queue.next_cursor; await loadQueue(); } });
window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
try { state.config = await (await fetch("/config", { cache: "no-store" })).json(); $("source").textContent = state.config.source; await loadQueue(); }
catch (error) { show("error", `Cannot connect to the local review app. ${error.message}`); $("queue-loading").hidden = true; }
