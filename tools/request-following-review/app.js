import { clearMedia, loadMedia, expectations, renderOutcomes, serializeOutcomes, outcomeDraft, restoreOutcomeDraft } from "./outcomes.js";
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
function mayLeave() { return !state.dirty || confirm("Leave this unsaved review? A draft stays in this tab."); }
function setBusy(value) {
  state.busy = value;
  $("refresh").disabled = value; $("filter").disabled = value;
  $("save-next").disabled = value; $("save").disabled = value; $("export").disabled = value;
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
    if (state.config.batch?.length) {
      const all = await Promise.all(state.config.batch.map(async proposal => {
        try { const item=await api(`/${proposal.id}`); if(proposal.title)item.request_preview=proposal.title; return item; }
        catch { return {id:proposal.id,thread_id:proposal.id,created_at:new Date().toISOString(),review_state:"unavailable",request_preview:"Session unavailable — refresh collection",diagnostics:{missing:{},failed_turns:[],broken_edges:[]}}; }
      }));
      state.queue.items = all.filter(item => state.filter === "all" || item.review_state === state.filter);
      state.queue.next_cursor = null;
    }
    const queue = state.queue, collector = queue.collector;
    const configured = collector.all_users || collector.scoped_users + collector.scoped_threads > 0;
    if (collector.mode === "prepared_copy") {
      $("collection-summary").textContent = `Prepared review batch · ${collector.scoped_threads} downloaded exports · local drafts only`;
      $("collection-detail").replaceChildren(element("p", "These copies were downloaded from eligible sessions. Drafts enter the daily report only after the server rechecks the current conversation, eligibility and export."));
    } else if (collector.mode === "private_store") {
      $("collection-summary").textContent = `Private captures · ${collector.scoped_threads} sessions ready for review`;
      $("collection-detail").replaceChildren(element("p", "This app reads downloaded copies and saves reviews locally. It does not change production or grant consent."));
    } else {
    $("collection-summary").textContent = `${collector.enabled && configured ? "Automatic collection · every 15 minutes" : "Collection is paused or needs a scope"} · ${collector.last_run_at ? "Last run " + date(collector.last_run_at) : "Not run yet"}`;
    $("collection-detail").replaceChildren(element("p", collector.all_users ? "All accounts with current recorded eligibility. Consent is checked on every capture and review; ineligible accounts are skipped." : `${collector.scoped_users} scoped accounts · ${collector.scoped_threads} scoped sessions. Current training consent is checked on capture and review.`));
    if (!collector.enabled || !configured) $("collection-detail").append(element("p", "To start, enable REQUEST_FOLLOWING_COLLECTION_ENABLED and set REQUEST_FOLLOWING_ALL_USERS or an explicit account/session scope on the API and worker. The local app does not change these settings."));
    if (collector.last_run) $("collection-detail").append(element("pre", JSON.stringify(collector.last_run, null, 2)));
    }
    $("collection-detail").append(element("p", queue.coverage_note));
    $("cases").replaceChildren();
    for (const item of queue.items) {
      const button = element("button", "", "case-button"); button.type = "button";
      button.setAttribute("aria-pressed", String(item.id === state.selected));
      button.append(element("span", labels[item.review_state] || item.review_state, `pill ${item.review_state}`));
      button.append(element("span", item.request_preview || `Session ${item.thread_id.slice(0, 8)}`, "case-preview"));
      button.append(element("span", date(item.created_at), "case-meta"));
      const missing = Object.keys(item.diagnostics.missing || {});
      if (missing.length) button.append(element("span", `Incomplete: ${missing.join(", ")}`, "signal"));
      const failures = item.diagnostics.failed_turns.length, broken = item.diagnostics.broken_edges.length;
      if (failures || broken) button.append(element("span", `${failures} failed turns · ${broken} broken links`, "signal"));
      button.addEventListener("click", () => { if (!state.busy && mayLeave()) void openCase(item.id); });
      const li = element("li"); li.append(button); $("cases").append(li);
    }
    $("page-count").textContent = collector.mode === "private_store" ? `${queue.items.length} shown · ${collector.scoped_threads} total` : `${queue.items.length} on this page`;
    $("queue-empty").hidden = queue.items.length > 0;
    $("queue-empty").textContent = state.filter === "all" ? collector.mode === "private_store" ? "No reviewable private captures on this page. Import a current eligibility receipt with each source. Expired and held-out copies stay unavailable." : "No eligible sessions on this page. Collection needs a selected scope and current consent. Held-out sessions stay private." : "No sessions with this review status on this page.";
    show("holdouts", queue.holdout_count ? `${queue.holdout_count} held-out sessions on this page. Their content is withheld.` : "");
    $("next-page").hidden = !queue.next_cursor; $("first-page").hidden = !state.cursor;
  } catch (error) { show("error", error.message); }
  finally { $("queue-loading").hidden = true; }
}
async function openCase(id) {
  const request = ++state.load;
  clearMedia(); state.selected = id; state.dirty = false;
  $("choose").hidden = true; $("case-detail").hidden = true; $("detail-loading").hidden = false;
  show("error", ""); show("review-error", ""); show("notice", "");
  setBusy(true);
  try {
    const detail = await api(`/${id}`);
    if (request !== state.load) return;
    state.detail = detail;
    renderCase(detail);
    const prepared=detail.source_mode==="prepared_copy";
    $("save").textContent=prepared ? "Save review draft" : "Save review";
    $("save-next").textContent=prepared ? "Save draft and next →" : "Save and next →";
    if(prepared) $("export").hidden=true;
    $("source").textContent=prepared ? "Prepared copies · drafts only" : state.config.source;
    const proposal = state.config.batch?.find(p => p.id === id && p.source_sha256 === detail.source_sha256);
    if (proposal?.title) $("case-title").textContent=proposal.title;
    show("inspection-hint",proposal?.observation || "");
    $("output-disclaimer").textContent=prepared ? "Drafts are saved privately on this computer. They enter the daily report only after current eligibility, conversation and export are revalidated." : "Only saved output verdicts count in the daily review report. Unsure stays unresolved. Tests and fixes still need their own verification.";
    if (!detail.review && proposal) {
      $("expected").value = proposal.expectations.map(e=>e.text).join("\n");
      state.pointers = new Set(proposal.expectations.flatMap(e=>e.source_pointers));
      document.querySelectorAll(".message.creator input").forEach(input=>{input.checked=state.pointers.has(input.dataset.pointer);});
      $("rationale").value = "Confirmed against the selected creator messages and the current export.";
      $("reviewer").value = sessionStorage.getItem("reviewer-name") || "";
      show("notice", "Proposed instructions are ready. Check them, then choose verdicts after watching. Nothing has been approved.");
    }
    await loadMedia(api, detail, proposal);
    restoreDraft();
    const batchIndex=state.config.batch?.findIndex(p=>p.id===id) ?? -1;
    $("save-next").hidden=batchIndex<0 || batchIndex===state.config.batch.length-1;
    if(batchIndex>=0) $("case-date").textContent=`Review ${batchIndex+1} of ${state.config.batch.length} · `+$("case-date").textContent;
    history.replaceState(null,"",`/?case=${encodeURIComponent(id)}`);
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
  $("case-date").textContent = `Captured ${date(detail.created_at)} · session ${detail.thread_id.slice(0, 8)}${detail.eligibility_checked_at ? " · Eligibility checked " + date(detail.eligibility_checked_at) : ""}`;
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
      const label = element("label"); const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.dataset.pointer = pointer; checkbox.checked = state.pointers.has(pointer);
      checkbox.addEventListener("change", () => { checkbox.checked ? state.pointers.add(pointer) : state.pointers.delete(pointer); state.dirty = true; saveDraft(); updateSaveHelp(); });
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
function showSelectedInstructions() {
  $("selected-instructions").replaceChildren();
  for(const pointer of [...state.pointers].sort((a,b)=>Number(a.split("/")[3])-Number(b.split("/")[3]))) {
    const index=Number(pointer.split("/")[3]);
    const event=state.detail?.capture.sections.events?.[index];
    if(event?.actor==="user" && event.content) {
      const previous=state.detail.capture.sections.events[index-1];
      if(event.content.length<50 && previous?.actor==="assistant" && previous.content) {
        const context=element("p",`Context for this reply: ${previous.content}`,"small muted");$("selected-instructions").append(context);
      }
      const quote=element("blockquote",event.content);$("selected-instructions").append(quote);
    }
  }
  if(!state.pointers.size) $("selected-instructions").append(element("p","Open the conversation above and select the relevant creator messages.","small muted"));
}
function updateSaveHelp() {
  if (!state.detail) return;
  showSelectedInstructions();
  const approve = $("decision").value === "approved";
  const incomplete = Object.keys(state.detail.capture.missing).length > 0;
  $("expected").required = approve;
  const unsupported = approve && (!state.pointers.size || incomplete);
  $("save").disabled = state.busy || unsupported || state.detail.review_state === "superseded";
  $("save-next").disabled = $("save").disabled;
  $("save-help").textContent = unsupported ? "Select at least one creator message and use a complete capture to approve intent." : `Private capture expires ${date(state.detail.expires_at)}.`;
}
function draftKey() { return `review-draft:${state.detail.id}:${state.detail.source_sha256}:${state.detail.review_version}`; }
function saveDraft() {
  if(!state.detail) return;
  const fields={}; for(const id of ["expected","decision","reviewer","rationale","stage","tickets"]) fields[id]=$(id).value;
  try { sessionStorage.setItem(draftKey(),JSON.stringify({fields,pointers:[...state.pointers],outcomes:outcomeDraft()})); } catch { show("review-error","This browser could not preserve the draft. Keep this tab open until you save."); }
}
function restoreDraft() {
  try {
    const draft=JSON.parse(sessionStorage.getItem(draftKey()) || "null");if(!draft)return;
    Object.entries(draft.fields).forEach(([id,value])=>{if($(id))$(id).value=value;});
    state.pointers=new Set(draft.pointers);renderOutcomes(null);restoreOutcomeDraft(draft.outcomes);
    document.querySelectorAll(".message.creator input").forEach(input=>{input.checked=state.pointers.has(input.dataset.pointer);});
    state.dirty=true;show("notice","Your unsaved draft was restored. It has not entered the daily report.");
  } catch { show("review-error","Could not restore this draft. Recheck the fields before saving."); }
}
$("review-form").addEventListener("input", () => { state.dirty = true; saveDraft(); });
$("review-form").addEventListener("change", () => { state.dirty = true; saveDraft(); });
$("decision").addEventListener("change", () => { state.dirty = true; updateSaveHelp(); });
$("review-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.busy) return;
  show("review-error", ""); show("notice", "");
  const detail = state.detail;
  const body = { source_sha256: detail.source_sha256, review_version: detail.review_version, decision: $("decision").value, reviewer: $("reviewer").value, rationale: $("rationale").value, loss_stage: $("stage").value, tickets: $("tickets").value.split(/[,\s]+/).filter(Boolean), expectations: expectations().map(text=>({text,source_pointers:[...state.pointers]})) };
  if (body.expectations.length && !state.pointers.size) { show("review-error", "Select the creator messages that support this expected outcome."); return; }
  try { body.outcome_review=serializeOutcomes(); }
  catch(error) { show("review-error",error.message); return; }
  if(body.outcome_review && body.decision!=="approved") { show("review-error","Choose Approve intent to confirm these instructions before recording output verdicts.");return; }
  const next = event.submitter?.id==="save-next" ? state.config.batch?.[state.config.batch.findIndex(p=>p.id===detail.id)+1]?.id : null;
  setBusy(true);
  try {
    await api(`/${detail.id}/review`, { method: "POST", body: JSON.stringify(body) });
    sessionStorage.removeItem(draftKey());sessionStorage.setItem("reviewer-name",body.reviewer);
    state.dirty = false;
    await loadQueue(); await openCase(next || detail.id);
    show("notice", detail.source_mode==="prepared_copy" ? "Review draft saved locally. It enters the daily report only after the server rechecks the conversation and export." : body.outcome_review ? "Export review saved. Current verdicts will appear in the daily report." : "Intent review saved. The output is still unverified.");
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
async function reconnect() {
  state.config = await (await fetch("/config", { cache: "no-store" })).json();
  $("source").textContent = state.config.source;
}
$("refresh").addEventListener("click", async () => {
  if (!mayLeave()) return;
  try { await reconnect(); state.cursor = null; state.filter = "all"; $("filter").value = "all"; await loadQueue(); if (state.selected) await openCase(state.selected); }
  catch (error) { show("error", error.message); }
});
$("filter").addEventListener("change", async () => { if (!mayLeave()) { $("filter").value = state.filter; return; } state.filter = $("filter").value; state.cursor = null; await loadQueue(); });
$("first-page").addEventListener("click", async () => { if (mayLeave()) { state.cursor = null; await loadQueue(); } });
$("next-page").addEventListener("click", async () => { if (mayLeave()) { state.cursor = state.queue.next_cursor; await loadQueue(); } });
window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
try { await reconnect(); await loadQueue();
  const requested=new URLSearchParams(location.search).get("case");
  const first=requested || state.config.batch?.[0]?.id;
  if(first && /^[a-f0-9-]{36}$/.test(first)) await openCase(first);
}
catch (error) { show("error", `Cannot connect to the local review app. ${error.message}`); $("queue-loading").hidden = true; }
