const $ = id => document.getElementById(id);
const categories = {title:"Title",labels:"Labels",captions:"Captions",order:"Clip order",selection:"Footage selection",duration:"Length",pacing:"Pacing",readability:"Readability",audio_source:"Voice / audio source",audio_mix:"Audio mix",style:"Style",preservation:"Preserve other edits",other:"Other"};
let media = null, loaded = false, rows = [], active = false;
function sameArtifact(a,b) { return a && b && ["job_id","variant_id","storage_generation","byte_count","md5"].every(key=>a[key]===b[key]); }
function node(tag, text) { const n = document.createElement(tag); if (text) n.textContent = text; return n; }
export function expectations() { return $("expected").value.split("\n").map(t=>t.trim()).filter(Boolean); }
export function renderOutcomes(saved, proposed = []) {
  rows = []; $("requirement-outcomes").replaceChildren();
  expectations().forEach((text,index)=> {
    const field = node("fieldset"), legend = node("legend", text); field.className = "requirement"; field.append(legend);
    const category = node("select"), verdict = node("select"), note = node("input");
    category.setAttribute("aria-label", `Category for instruction ${index+1}`);
    verdict.setAttribute("aria-label", `Verdict for instruction ${index+1}`);
    note.setAttribute("aria-label", `Evidence for instruction ${index+1}`); note.placeholder = "Optional: what went wrong and when"; note.maxLength = 2000;
    Object.entries(categories).forEach(([value,label])=>{ const o=node("option",label);o.value=value;category.append(o); });
    [["","Choose a verdict"],["met","Met"],["partial","Partly met"],["missed","Missed"],["unclear","Unsure"],["creator_changed","Changed by me"]].forEach(([value,label])=>{const o=node("option",label);o.value=value;verdict.append(o);});
    const prior = saved?.outcomes?.find(o=>o.expectation_index===index);
    category.value=prior?.category || proposed[index]?.category || "other"; verdict.value=prior?.verdict || ""; note.value=prior?.note || "";
    const controls=node("div");controls.className="outcome-controls";controls.append(category,verdict);field.append(controls,note);$("requirement-outcomes").append(field);
    rows.push({category,verdict,note});
  });
}
export function clearMedia() {
  media=null;loaded=false;active=false;$("export-video").pause();$("export-video").removeAttribute("src");$("export-video").load();
  $("output-review").hidden=true;$("media-preview").hidden=true;$("watched").checked=false;$("listened").checked=false;$("include-outcomes").checked=false;$("output-verdicts").disabled=true;
}
export async function loadMedia(api, detail, proposal) {
  if (detail.source_mode === "private_store") return;
  $("output-review").hidden=false;$("media-preview").hidden=false;$("media-status").textContent="Loading the current export…";
  renderOutcomes(null, proposal?.expectations);
  try {
    const result=await api(`/${detail.id}/media`);
    if(result.status!=="available") {$("media-status").textContent="No current finished export is available. You can still review intent; output stays unverified.";return;}
    media=result; active=true;
    if(proposal && !detail.review?.outcome_review) {$("include-outcomes").checked=true;$("output-verdicts").disabled=false;}
    $("export-video").src=result.url;
    $("media-status").textContent=detail.source_mode==="prepared_copy" ? "Downloaded export copy. Reviews save as local drafts until the server rechecks the current version." : "Watch with sound. This review is tied to this exact export.";
    const saved=detail.review?.outcome_review;
    if(saved && sameArtifact(saved.artifact,result.artifact)) {
      renderOutcomes(saved);$("include-outcomes").checked=true;$("output-verdicts").disabled=false;$("watched").checked=saved.watched;$("listened").checked=saved.listened;
      $("media-status").textContent=detail.source_mode==="prepared_copy" ? "Saved draft matches this downloaded copy. Current server validation is still pending." : "Saved review matches this export. Change any verdict after watching again.";
    } else if(saved) {$("media-status").textContent="The export changed since this review. Watch again and record fresh verdicts.";}
  } catch(error) {$("media-status").textContent=error.message;}
}
export function outcomeDraft() {
  return {artifact:media?.artifact,include:$("include-outcomes").checked,watched:$("watched").checked,listened:$("listened").checked,
    outcomes:rows.map((r,i)=>({expectation_index:i,category:r.category.value,verdict:r.verdict.value,note:r.note.value}))};
}
export function restoreOutcomeDraft(draft) {
  if(!draft || !media || !sameArtifact(draft.artifact,media.artifact)) return;
  renderOutcomes(draft);$("include-outcomes").checked=draft.include;$("output-verdicts").disabled=!draft.include;$("watched").checked=draft.watched;$("listened").checked=draft.listened;
}
export function serializeOutcomes() {
  if(!$("include-outcomes").checked) return null;
  const draft=outcomeDraft();
  if(!active || !loaded) throw Error("Wait for the export to load before saving an output review.");
  if(!draft.watched) throw Error("Watch the export and confirm you watched it before saving.");
  if(!rows.length || draft.outcomes.some(o=>!o.verdict)) throw Error("Choose a verdict for every instruction. Use Unsure when you cannot verify it.");
  if(!draft.listened && draft.outcomes.some(o=>["captions","audio_source","audio_mix"].includes(o.category)&&!["unclear","creator_changed"].includes(o.verdict))) throw Error("Listen with sound before judging audio or captions, or mark those instructions Unsure.");
  return {artifact:draft.artifact,watched:draft.watched,listened:draft.listened,outcomes:draft.outcomes};
}
$("export-video").addEventListener("loadeddata",()=>{loaded=true;});
$("export-video").addEventListener("error",()=>{loaded=false;if(media)$("media-status").textContent="The video could not load or its link expired. Refresh to get a fresh link; your draft stays in this tab.";});
$("include-outcomes").addEventListener("change",()=>{$("output-verdicts").disabled=!$("include-outcomes").checked;});
$("expected").addEventListener("input",()=>{renderOutcomes(null);});
