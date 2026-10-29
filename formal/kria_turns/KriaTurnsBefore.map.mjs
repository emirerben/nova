// Chart labels for the Kria chat-turn model: what the creator sees in the chat.
// TLC functions over 1..n arrive as arrays indexed from 0: turn t is at [t - 1].
const TURNS = [1, 2, 3];
const at = (f, t) => f[t - 1];
// A set arrives as an array, or as "a..b" when TLC kept the range unevaluated.
function members(set) {
  if (Array.isArray(set)) return set;
  const range = /^(\d+)\.\.(\d+)$/.exec(String(set));
  if (!range) return Object.values(set ?? {});
  const out = [];
  for (let m = Number(range[1]); m <= Number(range[2]); m++) out.push(m);
  return out;
}
const sent = (v) => TURNS.filter((t) => t <= v.sent);
const running = (v) => sent(v).some((t) => ["pending", "planning"].includes(at(v.status, t)));
const promoting = (v) =>
  v.api === "promote" || Object.values(v.w).some((run) => run.pc === "promote");

const sweepWillStart = (facts) =>
  [...(facts?.enabled ?? [])].some((a) => String(a).startsWith("ReleaseStranded"));

function message(v, t, facts) {
  const s = at(v.status, t);
  if (s === "pending" || s === "planning") {
    const again = at(v.epoch, t) > 0 && !Object.values(v.w).some((run) => run.turn === t && run.pc !== "idle");
    return `${t} thinking${again && s === "pending" ? " again" : ""}`;
  }
  if (s === "queued") {
    if (running(v)) return `${t} waiting`;
    if (promoting(v)) return `${t} about to start`;
    return sweepWillStart(facts) ? `${t} waiting for the sweep (up to a minute)` : `${t} stuck waiting`;
  }
  if (s === "completed") {
    const ahead = members(at(v.answered, t)).filter((m) => m > t);
    return ahead.length ? `${t} answered using ${ahead.join(", ")}` : `${t} answered`;
  }
  if (s === "cancelled") return `${t} stopped`;
  return `${t} ${s}`;
}

export const readerActions = ["Submit", "Cancel"];

export function state(v, facts) {
  if (v.sent === 0) return "Empty chat: nothing sent";
  const context = v.sent === 1 ? "One message" : `${v.sent} messages`;
  return `${context}: ${sent(v).map((t) => message(v, t, facts)).join(", ")}`;
}

export function group(v, facts) {
  if (sent(v).some((t) => at(v.status, t) === "queued") && !running(v) && !promoting(v))
    return sweepWillStart(facts) ? "Kria is working" : "Stuck";
  if (running(v) || promoting(v)) return "Kria is working";
  return "Your turn";
}
export const groupColors = {
  "Kria is working": "#e8f0fe",
  "Your turn": "#e6f4ea",
  Stuck: "#fde7e9",
};

const changed = (before, after) =>
  TURNS.find((t) => t <= after.status.length && at(before.status, t) !== at(after.status, t)) ?? 0;

export function step(action, before, after) {
  const t = changed(before, after);
  switch (action.replace(/\(.*$/, "")) {
    case "Submit":
      return `you send message ${after.sent}`;
    case "Cancel":
      return `you remove message ${t}`;
    case "Claim":
      return t ? `Kria starts on message ${t}` : "Kria picks the message up again";
    case "ReadHistory":
      return "Kria reads the chat";
    case "Complete":
      if (t && at(after.status, t) === "completed") return `Kria replies to message ${t}`;
      if (t && at(after.status, t) === "pending") return `Kria sees the chat changed and starts message ${t} over`;
      return "";
    case "WorkerPromote":
    case "CancelPromote":
      return t ? `message ${t} starts` : "";
    case "ReleaseStranded":
      return `the sweep starts message ${t}`;
    case "WorkerCrash":
    case "ApiCrash":
      return "the server restarts";
    case "Reconcile":
      return "the sweep retries";
    case "LeaseLapses":
      return "";
    default:
      return action;
  }
}

export function cause(v, facts, invariant) {
  return {
    NoStuckFollowUp:
      "the server restarted after Kria finished or stopped one message and before it started the waiting one, and nothing ever starts it, so every new message is refused",
    AnswersOnlyUpToItsMessage:
      "Kria planned its answer to the first message with the follow-up in view, and the follow-up then gets its own answer too, so it is acted on twice",
    OneAtATime: "a late start step began a waiting message while another was running",
    NoStaleCommit: "a run that lost its turn saved a reply",
  }[invariant];
}

export const legend = {
  works: "Works on main",
  wrong: "Still wrong with the fix",
  fixed: "Fixed in codex/kri-chat-turn-tla",
};
