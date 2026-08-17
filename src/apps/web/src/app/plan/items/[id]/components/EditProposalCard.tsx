"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  approveEditProposal,
  draftEditProposal,
  editProposalConversationTurn,
  updateEditProposal,
  type EditProposalDirection,
  type EditProposalPace,
  type EditProposalSnapshot,
  type PlanItem,
} from "@/lib/plan-api";

const DIRECTION_OPTIONS: Array<{
  value: EditProposalDirection;
  label: string;
  description: string;
}> = [
  { value: "guided_story", label: "Story with thoughts", description: "Topics, context, and text" },
  { value: "fast_montage", label: "Fast montage", description: "Quick, music-led cuts" },
  { value: "text_explainer", label: "Text explainer", description: "More written detail" },
];

const PACE_OPTIONS: Array<{ value: EditProposalPace; label: string }> = [
  { value: "relaxed", label: "Relaxed" },
  { value: "balanced", label: "Balanced" },
  { value: "fast", label: "Fast" },
];

const DEFAULT_BRIEF = {
  direction: "guided_story" as EditProposalDirection,
  goal: "",
  pace: "balanced" as EditProposalPace,
  duration_s: 24,
};

const DIRECTION_LABELS: Record<EditProposalDirection, string> = {
  guided_story: "Story with thoughts",
  fast_montage: "Fast montage",
  text_explainer: "Text explainer",
};

const PACE_LABELS: Record<EditProposalPace, string> = {
  relaxed: "Relaxed",
  balanced: "Balanced",
  fast: "Fast",
};

const DURATION_OPTIONS = [15, 20, 24, 30, 45, 60];

function durationOptions(current: number): number[] {
  return Array.from(new Set([current, ...DURATION_OPTIONS])).sort((a, b) => a - b);
}

function selectedSourceCount(snapshot: EditProposalSnapshot): number {
  return new Set(snapshot.story_beats.flatMap((beat) => beat.media_ids)).size;
}

function withoutPreviewUrls(snapshot: EditProposalSnapshot): EditProposalSnapshot {
  return {
    ...snapshot,
    media: snapshot.media.map(({ preview_url: _preview, ...ref }) => ref),
  };
}

function MediaThumb({
  src,
  kind,
  label,
}: {
  src?: string | null;
  kind: "image" | "video";
  label: string;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  if (!src || failed) {
    return (
      <div
        role="img"
        aria-label={`${label}: preview unavailable`}
        title={`${label}: preview unavailable`}
        className="flex h-14 w-10 shrink-0 items-center justify-center rounded-md bg-zinc-100 px-1 text-center text-[9px] text-zinc-500"
      >
        {failed ? "File" : kind === "image" ? "Photo" : "Video"}
      </div>
    );
  }
  if (kind === "image") {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- signed GCS URL has no stable loader host
      <img
        src={src}
        alt={label}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
        className="h-14 w-10 shrink-0 rounded-md object-cover"
      />
    );
  }
  return (
    <video
      src={src}
      aria-label={label}
      muted
      playsInline
      preload="none"
      onError={() => setFailed(true)}
      className="h-14 w-10 shrink-0 rounded-md object-cover"
    />
  );
}

export default function EditProposalCard({
  item,
  onChanged,
  onRefresh,
}: {
  item: PlanItem;
  onChanged: (item: PlanItem) => void;
  onRefresh?: () => void;
}) {
  const proposal = item.edit_proposal ?? null;
  const conversationEnabled = item.guided_edit_conversation_available === true;
  const [conversationOpen, setConversationOpen] = useState(
    conversationEnabled && proposal?.status === "briefing",
  );
  const [legacyBriefOpen, setLegacyBriefOpen] = useState(false);
  const [legacyBrief, setLegacyBrief] = useState(DEFAULT_BRIEF);
  const [message, setMessage] = useState("");
  const [workingAction, setWorkingAction] = useState<
    "conversation" | "plan" | "approve" | null
  >(null);
  const [editingApproved, setEditingApproved] = useState(false);
  const [draft, setDraft] = useState<EditProposalSnapshot | null>(proposal?.draft ?? null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const working = workingAction !== null;
  const conversationInProgress = proposal?.conversation_in_progress === true;
  const conversationRetryRequired = proposal?.conversation_retry_required === true;
  const conversationBlocked = conversationInProgress || conversationRetryRequired;
  // Poll responses recreate the draft object; only the CAS version denotes a
  // durable server revision that should replace the creator's unsaved edits.
  const appliedProposalRevision = useRef<string | null>(null);

  useEffect(() => {
    if (conversationEnabled && proposal?.status === "briefing") setConversationOpen(true);
  }, [conversationEnabled, proposal?.status]);

  useEffect(() => {
    const revision = `${item.id}:${proposal?.proposal_version ?? "none"}`;
    if (appliedProposalRevision.current === revision) return;
    appliedProposalRevision.current = revision;
    setDraft(proposal?.draft ?? null);
    const retryBrief = proposal?.status === "stale"
      ? proposal.last_approved?.snapshot
      : proposal?.brief;
    if (retryBrief) {
      setLegacyBrief({
        direction: retryBrief.direction,
        goal: retryBrief.goal,
        pace: retryBrief.pace,
        duration_s: retryBrief.duration_s,
      });
    }
    if (proposal?.status !== "approved") setEditingApproved(false);
  }, [item.id, proposal]);

  const mediaById = useMemo(
    () => new Map((draft?.media ?? []).map((ref) => [ref.media_id, ref])),
    [draft?.media],
  );

  const conversation = proposal?.conversation ?? [];
  const lastAgentTurn = [...conversation].reverse().find((turn) => turn.role === "agent");
  const lastUserTurn = [...conversation].reverse().find((turn) => turn.role === "user");
  const brief = proposal?.brief ?? DEFAULT_BRIEF;

  async function sendConversation(text: string, reviewing = false) {
    const trimmed = text.trim();
    if (!trimmed || working) return;
    setWorkingAction("conversation");
    setError(null);
    setMessage("");
    try {
      let expectedVersion = proposal?.proposal_version ?? 0;
      if (reviewing && proposal?.draft && draft) {
        const localSnapshot = withoutPreviewUrls(draft);
        const persistedSnapshot = withoutPreviewUrls(proposal.draft);
        if (JSON.stringify(localSnapshot) !== JSON.stringify(persistedSnapshot)) {
          const saved = await updateEditProposal(item.id, expectedVersion, localSnapshot);
          const savedVersion = saved.edit_proposal?.proposal_version;
          if (!savedVersion) throw new Error("The saved plan could not be verified.");
          // Make manual edits durable before Kria reads/revises the proposal.
          // If the conversation fails, retry continues from this saved version.
          onChanged(saved);
          expectedVersion = savedVersion;
        }
      }
      const updated = await editProposalConversationTurn(
        item.id,
        expectedVersion,
        trimmed,
      );
      onChanged(updated);
    } catch (err) {
      setMessage(trimmed);
      setError(err instanceof Error ? err.message : "Kria couldn't think that through.");
      // A network timeout is ambiguous: the server may have committed the
      // turn even though this response was lost. Reconcile before a retry so
      // the browser adopts that durable version instead of resending stale CAS.
      onRefresh?.();
    } finally {
      setWorkingAction(null);
    }
  }

  async function startDraft() {
    if (working) return;
    setWorkingAction("plan");
    setError(null);
    try {
      const updated = await draftEditProposal(
        item.id,
        conversationEnabled ? brief : legacyBrief,
      );
      setConversationOpen(false);
      setLegacyBriefOpen(false);
      onChanged(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kria couldn't start planning this edit.");
    } finally {
      setWorkingAction(null);
    }
  }

  function moveBeat(index: number, offset: -1 | 1) {
    if (!draft) return;
    const target = index + offset;
    if (target < 0 || target >= draft.story_beats.length) return;
    const beats = [...draft.story_beats];
    [beats[index], beats[target]] = [beats[target], beats[index]];
    setDraft({ ...draft, story_beats: beats });
  }

  function patchBeat(
    beatId: string,
    patch: Partial<EditProposalSnapshot["story_beats"][number]>,
  ) {
    if (!draft) return;
    setDraft({
      ...draft,
      story_beats: draft.story_beats.map((beat) =>
        beat.beat_id === beatId ? { ...beat, ...patch } : beat,
      ),
    });
  }

  async function approve() {
    if (working || !draft || !proposal) return;
    setWorkingAction("approve");
    setError(null);
    try {
      const saved = await updateEditProposal(
        item.id,
        proposal.proposal_version,
        withoutPreviewUrls(draft),
      );
      const savedVersion = saved.edit_proposal?.proposal_version;
      if (!savedVersion) throw new Error("The saved plan could not be verified.");
      // Surface the durable PATCH before approval. If approval fails, the
      // creator retries from the saved version instead of an unrecoverably
      // stale expected_proposal_version.
      onChanged(saved);
      const approved = await approveEditProposal(item.id, savedVersion);
      setEditingApproved(false);
      onChanged(approved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kria couldn't approve this plan.");
    } finally {
      setWorkingAction(null);
    }
  }

  function conversationSurface({ reviewing = false }: { reviewing?: boolean } = {}) {
    const opener = proposal?.status === "stale"
      ? "Your uploads changed. What should the new edit keep or emphasize?"
      : proposal?.status === "failed"
        ? "Your direction is saved. Tell me what to change, or I can try planning it again."
        : reviewing
          ? "What would you change about this draft? You can be as specific or as rough as you like."
          : "What do you want this video to feel like—and what should people remember after watching it?";
    const contextualAgentTurn = reviewing
      ? [...conversation].reverse().find(
          (turn) => turn.role === "agent" && turn.phase === "review",
        )
      : lastAgentTurn;
    const contextualUserTurn = reviewing
      ? [...conversation].reverse().find(
          (turn) => turn.role === "user" && turn.phase === "review",
        )
      : lastUserTurn;
    const reply = contextualAgentTurn?.content ?? opener;
    const suggestions = contextualAgentTurn
      ? contextualAgentTurn.suggestions
      : reviewing
        ? ["Make it more personal", "Use less text", "Put food first"]
        : ["A personal travel diary", "Fast highlights, little text", "Explain what stood out"];
    const showBrief = Boolean(proposal);

    return (
      <div data-testid="edit-guide-conversation">
        <p className="text-[11px] font-semibold uppercase tracking-[.18em] text-lime-700">
          {reviewing ? "Shape the draft with Kria" : "Plan with Kria"}
        </p>
        {contextualUserTurn ? (
          <blockquote className="mt-3 border-l-2 border-lime-600 pl-3">
            <p className="line-clamp-3 text-sm text-[#71717a]">{contextualUserTurn.content}</p>
          </blockquote>
        ) : null}
        <p
          className="mt-3 max-w-prose font-display text-xl leading-snug text-[#0c0c0e]"
          aria-live="polite"
        >
          {reply}
        </p>

        {workingAction === "conversation" || conversationInProgress ? (
          <p role="status" className="mt-3 flex items-center gap-2 text-sm text-[#71717a]">
            <span className="h-1.5 w-1.5 rounded-full bg-lime-600 motion-safe:animate-ping" />
            Thinking it through…
          </p>
        ) : conversationRetryRequired ? (
          <p role="status" className="mt-3 text-sm text-[#71717a]">
            That reply took too long. Send your direction again to continue.
          </p>
        ) : (
          <div className="mt-4 flex flex-wrap gap-2">
            {suggestions.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => void sendConversation(suggestion, reviewing)}
                className="min-h-11 rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] outline-none transition-colors hover:border-lime-600 hover:text-[#0c0c0e] focus-visible:ring-2 focus-visible:ring-lime-600"
              >
                {suggestion}
              </button>
            ))}
          </div>
        )}

        <form
          className="mt-4 flex items-end gap-2 rounded-2xl border border-zinc-200 bg-white px-3 py-2 focus-within:border-lime-500 focus-within:ring-2 focus-within:ring-lime-500/20"
          onSubmit={(event) => {
            event.preventDefault();
            void sendConversation(message, reviewing);
          }}
        >
          <label htmlFor="edit-guide-message" className="sr-only">
            Tell Kria what you want in the edit
          </label>
          <textarea
            ref={inputRef}
            id="edit-guide-message"
            value={message}
            onChange={(event) => setMessage(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void sendConversation(message, reviewing);
              }
            }}
            disabled={working || conversationInProgress}
            rows={1}
            maxLength={1000}
            placeholder={reviewing ? "For example: focus more on the food…" : "Tell me in your own words…"}
            className="min-h-11 flex-1 resize-none bg-transparent py-2 text-base text-[#0c0c0e] outline-none placeholder:text-[#a1a1aa] [field-sizing:content]"
          />
          <button
            type="submit"
            disabled={working || conversationInProgress || !message.trim()}
            aria-label="Send direction"
            className="flex min-h-11 min-w-11 items-center justify-center rounded-full bg-[#0c0c0e] text-white outline-none transition-opacity hover:opacity-80 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2 disabled:opacity-25"
          >
            →
          </button>
        </form>

        {showBrief ? (
          <div className="mt-4 border-t border-zinc-200 pt-4">
            <p className="text-xs font-medium text-[#71717a]">What I heard</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <span className="rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-xs text-lime-800">
                {DIRECTION_LABELS[brief.direction]}
              </span>
              <span className="rounded-full border border-zinc-200 bg-white px-3 py-1 text-xs text-[#3f3f46]">
                {PACE_LABELS[brief.pace]} pace
              </span>
              <span className="rounded-full border border-zinc-200 bg-white px-3 py-1 text-xs text-[#3f3f46]">
                About {brief.duration_s}s
              </span>
            </div>
            {brief.goal ? <p className="mt-2 text-sm text-[#3f3f46]">{brief.goal}</p> : null}
          </div>
        ) : null}

        {error ? <p role="alert" className="mt-3 text-sm text-red-700">{error}</p> : null}

        {!reviewing ? (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={working || conversationBlocked}
              onClick={() => void startDraft()}
              className="min-h-11 rounded-lg bg-lime-600 px-4 py-2 text-sm font-semibold text-white outline-none hover:bg-lime-700 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2 disabled:opacity-50"
            >
              {workingAction === "plan"
                ? "Starting…"
                : proposal?.status === "failed"
                  ? "Try planning again"
                  : "Build this edit plan"}
            </button>
            {!proposal ? (
              <button
                type="button"
                disabled={working}
                onClick={() => setConversationOpen(false)}
                className="min-h-11 text-sm text-[#71717a] underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
              >
                Cancel
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  function legacyBriefSurface() {
    const stale = proposal?.status === "stale";
    return (
      <section
        className="mt-5 rounded-xl border border-zinc-200 bg-white p-4"
        aria-labelledby="legacy-plan-edit-heading"
      >
        <h2
          id="legacy-plan-edit-heading"
          className="font-display text-lg font-medium text-[#0c0c0e]"
        >
          {stale ? "Your media changed" : "What should this edit do?"}
        </h2>
        {stale && proposal?.last_approved ? (
          <p className="mt-1 text-sm text-[#71717a]">
            Your last approved plan, “{proposal.last_approved.snapshot.title},” is saved for comparison.
          </p>
        ) : null}
        {proposal?.failure ? (
          <p className="mt-2 text-sm text-[#71717a]">{proposal.failure.message}</p>
        ) : null}

        <fieldset className="mt-4">
          <legend className="text-sm font-medium text-[#0c0c0e]">Edit direction</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            {DIRECTION_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={`min-h-16 cursor-pointer rounded-lg border p-3 outline-none transition-colors focus-within:ring-2 focus-within:ring-lime-600 ${
                  legacyBrief.direction === option.value
                    ? "border-lime-500 bg-lime-50"
                    : "border-zinc-200"
                }`}
              >
                <input
                  type="radio"
                  name="edit-direction"
                  value={option.value}
                  checked={legacyBrief.direction === option.value}
                  onChange={() => setLegacyBrief({ ...legacyBrief, direction: option.value })}
                  className="sr-only"
                />
                <span className="block text-sm font-medium text-[#0c0c0e]">{option.label}</span>
                <span className="mt-0.5 block text-xs text-[#71717a]">{option.description}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <label className="mt-4 block text-sm font-medium text-[#0c0c0e]">
          Goal or context
          <textarea
            value={legacyBrief.goal}
            onChange={(event) => setLegacyBrief({ ...legacyBrief, goal: event.currentTarget.value })}
            maxLength={500}
            rows={3}
            placeholder="For example: show what surprised me about the food, town, and beaches."
            className="mt-2 w-full resize-none rounded-lg border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-base font-normal outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
          />
        </label>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="text-sm font-medium text-[#0c0c0e]">
            Pace
            <select
              value={legacyBrief.pace}
              onChange={(event) =>
                setLegacyBrief({
                  ...legacyBrief,
                  pace: event.currentTarget.value as EditProposalPace,
                })
              }
              className="mt-2 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
            >
              {PACE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="text-sm font-medium text-[#0c0c0e]">
            Target length
            <select
              value={legacyBrief.duration_s}
              onChange={(event) =>
                setLegacyBrief({ ...legacyBrief, duration_s: Number(event.currentTarget.value) })
              }
              className="mt-2 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
            >
              {durationOptions(legacyBrief.duration_s).map((seconds) => (
                <option key={seconds} value={seconds}>{seconds} seconds</option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={working}
            onClick={() => void startDraft()}
            className="min-h-11 rounded-lg bg-lime-600 px-4 py-2 text-sm font-semibold text-white outline-none hover:bg-lime-700 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2 disabled:opacity-50"
          >
            {workingAction === "plan" ? "Starting…" : stale ? "Plan again" : "Build edit plan"}
          </button>
          {!proposal ? (
            <button
              type="button"
              disabled={working}
              onClick={() => setLegacyBriefOpen(false)}
              className="min-h-11 rounded-lg border border-zinc-200 px-4 py-2 text-sm text-[#3f3f46] outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
            >
              Cancel
            </button>
          ) : null}
        </div>
        {error ? <p role="alert" className="mt-3 text-sm text-red-700">{error}</p> : null}
      </section>
    );
  }

  if (!conversationEnabled) {
    if (!proposal && !legacyBriefOpen) {
      return (
        <div className="mt-5 border-t border-zinc-200 pt-5">
          <button
            type="button"
            onClick={() => setLegacyBriefOpen(true)}
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-[#0c0c0e] outline-none transition-colors hover:border-lime-500 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
          >
            <span aria-hidden>✦</span>
            Plan edit
          </button>
          <p className="mt-2 text-sm text-[#71717a]">
            Choose the direction, pace, and goal before Kria plans every upload.
          </p>
        </div>
      );
    }
    if (
      legacyBriefOpen ||
      proposal?.status === "briefing" ||
      proposal?.status === "failed" ||
      proposal?.status === "stale"
    ) {
      return legacyBriefSurface();
    }
  }

  if (!proposal && !conversationOpen) {
    return (
      <div className="mt-5 border-t border-zinc-200 pt-5">
        <button
          type="button"
          onClick={() => {
            setConversationOpen(true);
            window.setTimeout(() => inputRef.current?.focus(), 0);
          }}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-[#0c0c0e] outline-none transition-colors hover:border-lime-500 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
        >
          <span aria-hidden>✦</span>
          Plan edit
        </button>
        <p className="mt-2 text-sm text-[#71717a]">
          Tell Kria what you want. It will understand all your uploads and propose the edit before rendering.
        </p>
      </div>
    );
  }

  if (
    (conversationOpen &&
      (!proposal || ["briefing", "failed", "stale"].includes(proposal.status))) ||
    proposal?.status === "briefing" ||
    proposal?.status === "failed" ||
    proposal?.status === "stale"
  ) {
    const stale = proposal?.status === "stale";
    return (
      <section
        className="mt-5 rounded-xl border border-zinc-200 bg-white p-4"
        aria-labelledby="plan-edit-heading"
      >
        <h2 id="plan-edit-heading" className="sr-only">Describe your edit to Kria</h2>
        {stale && proposal?.last_approved ? (
          <p className="mb-4 text-sm text-[#71717a]">
            Your last approved plan, “{proposal.last_approved.snapshot.title},” is saved for comparison.
          </p>
        ) : null}
        {proposal?.failure ? (
          <p className="mb-4 rounded-lg border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-sm text-[#3f3f46]">
            {proposal.failure.message}
          </p>
        ) : null}
        {conversationSurface()}
      </section>
    );
  }

  if (proposal?.status === "analyzing" || proposal?.status === "drafting") {
    return (
      <section className="mt-5 rounded-xl border border-lime-200 bg-lime-50 p-4" aria-labelledby="planning-edit-heading">
        <h2 id="planning-edit-heading" className="font-display text-lg font-medium text-[#0c0c0e]">
          Planning your edit
        </h2>
        <div role="status" aria-live="polite" className="mt-2 flex items-center gap-2 text-sm text-[#3f3f46]">
          <span aria-hidden className="h-2 w-2 rounded-full bg-lime-600 motion-safe:animate-pulse" />
          {proposal.status === "analyzing"
            ? "Understanding every photo and video…"
            : "Building the direction and story beats…"}
        </div>
      </section>
    );
  }

  const visibleDraft = draft ?? proposal?.last_approved?.snapshot ?? null;
  if (!visibleDraft) return null;
  const readOnlyApproved = proposal?.status === "approved" && !editingApproved;

  if (readOnlyApproved) {
    return (
      <section className="mt-5 rounded-xl border border-lime-300 bg-lime-50 p-4" aria-labelledby="approved-plan-heading">
        <p className="text-[11px] font-semibold uppercase tracking-[.15em] text-lime-700">Approved edit plan</p>
        <h2 id="approved-plan-heading" className="mt-1 font-display text-xl font-medium text-[#0c0c0e]">
          {visibleDraft.title}
        </h2>
        <p className="mt-1 text-sm text-[#3f3f46]">
          {visibleDraft.story_beats.length} moments · {selectedSourceCount(visibleDraft)} sources · about {visibleDraft.duration_s}s
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setEditingApproved(true)}
            className="min-h-11 rounded-lg border border-lime-700 px-4 py-2 text-sm font-medium text-lime-700 outline-none focus-visible:ring-2 focus-visible:ring-lime-700"
          >
            Edit plan
          </button>
          {conversationEnabled ? (
            <button
              type="button"
              onClick={() => {
                setEditingApproved(true);
                setConversationOpen(true);
              }}
              className="min-h-11 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-[#3f3f46] outline-none hover:border-lime-500 focus-visible:ring-2 focus-visible:ring-lime-600"
            >
              Tell Kria a change
            </button>
          ) : null}
        </div>
      </section>
    );
  }

  return (
    <section className="mt-5 rounded-xl border border-lime-200 bg-lime-50 p-4" aria-labelledby="draft-plan-heading">
      <p className="text-[11px] font-semibold uppercase tracking-[.15em] text-lime-700">Kria’s draft</p>
      <h2 id="draft-plan-heading" className="sr-only">Review edit plan</h2>
      {conversationEnabled && conversationOpen ? (
        <div className="mt-3 rounded-xl border border-zinc-200 bg-white p-4">
          {conversationSurface({ reviewing: true })}
          <button
            type="button"
            onClick={() => setConversationOpen(false)}
            className="mt-4 min-h-11 text-sm text-[#71717a] underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
          >
            Close conversation
          </button>
        </div>
      ) : conversationEnabled ? (
        <button
          type="button"
          onClick={() => {
            setConversationOpen(true);
            window.setTimeout(() => inputRef.current?.focus(), 0);
          }}
          className="mt-3 min-h-11 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-[#3f3f46] outline-none hover:border-lime-500 focus-visible:ring-2 focus-visible:ring-lime-600"
        >
          Tell Kria what to change
        </button>
      ) : null}
      <label className="mt-2 block text-sm font-medium text-[#0c0c0e]">
        Title
        <input
          value={visibleDraft.title}
          onChange={(event) => setDraft({ ...visibleDraft, title: event.currentTarget.value })}
          maxLength={100}
          className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base font-normal outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
        />
      </label>
      <label className="mt-3 block text-sm font-medium text-[#0c0c0e]">
        Goal
        <textarea
          value={visibleDraft.goal}
          onChange={(event) => setDraft({ ...visibleDraft, goal: event.currentTarget.value })}
          rows={2}
          maxLength={500}
          className="mt-1 w-full resize-none rounded-lg border border-zinc-200 bg-white px-3 py-2 text-base font-normal outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
        />
      </label>

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <label className="text-sm font-medium text-[#0c0c0e]">
          Direction
          <select
            value={visibleDraft.direction}
            onChange={(event) =>
              setDraft({
                ...visibleDraft,
                direction: event.currentTarget.value as EditProposalDirection,
              })
            }
            className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base font-normal outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
          >
            {DIRECTION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <label className="text-sm font-medium text-[#0c0c0e]">
          Pace
          <select
            value={visibleDraft.pace}
            onChange={(event) =>
              setDraft({
                ...visibleDraft,
                pace: event.currentTarget.value as EditProposalPace,
              })
            }
            className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base font-normal outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
          >
            {PACE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <label className="text-sm font-medium text-[#0c0c0e]">
          Target length
          <select
            value={visibleDraft.duration_s}
            onChange={(event) =>
              setDraft({ ...visibleDraft, duration_s: Number(event.currentTarget.value) })
            }
            className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base font-normal outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
          >
            {durationOptions(visibleDraft.duration_s).map((seconds) => (
              <option key={seconds} value={seconds}>{seconds} seconds</option>
            ))}
          </select>
        </label>
      </div>

      <ol className="mt-5 space-y-3">
        {visibleDraft.story_beats.map((beat, index) => (
          <li key={beat.beat_id} className="rounded-lg border border-lime-200 bg-white p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <label className="block text-sm font-semibold text-[#0c0c0e]">
                  <span aria-hidden>{index + 1}. </span>
                  <input
                    aria-label={`Moment ${index + 1} topic`}
                    value={beat.topic}
                    onChange={(event) =>
                      patchBeat(beat.beat_id, { topic: event.currentTarget.value })
                    }
                    maxLength={80}
                    className="min-h-11 w-[calc(100%-1.5rem)] rounded-md border border-transparent bg-transparent px-1 text-base outline-none hover:border-zinc-200 focus:border-lime-500 focus:bg-white focus:ring-2 focus:ring-lime-500/30"
                  />
                </label>
                <div className="mt-2 flex gap-1.5 overflow-x-auto pb-1">
                  {beat.media_ids.map((mediaId) => {
                    const media = mediaById.get(mediaId);
                    return media ? (
                      <MediaThumb
                        key={mediaId}
                        src={media.preview_url}
                        kind={media.kind}
                        label={`${beat.topic}: ${media.source_filename || media.kind}`}
                      />
                    ) : null;
                  })}
                </div>
              </div>
              <div className="flex shrink-0 gap-1">
                <button
                  type="button"
                  disabled={index === 0}
                  onClick={() => moveBeat(index, -1)}
                  aria-label={`Move ${beat.topic} earlier`}
                  className="min-h-11 min-w-11 rounded-md border border-zinc-200 text-zinc-600 outline-none focus-visible:ring-2 focus-visible:ring-lime-600 disabled:opacity-30"
                >↑</button>
                <button
                  type="button"
                  disabled={index === visibleDraft.story_beats.length - 1}
                  onClick={() => moveBeat(index, 1)}
                  aria-label={`Move ${beat.topic} later`}
                  className="min-h-11 min-w-11 rounded-md border border-zinc-200 text-zinc-600 outline-none focus-visible:ring-2 focus-visible:ring-lime-600 disabled:opacity-30"
                >↓</button>
              </div>
            </div>
            <label className="mt-3 block text-sm text-[#3f3f46]">
              Layout
              <select
                value={beat.layout}
                onChange={(event) =>
                  patchBeat(beat.beat_id, {
                    layout: event.currentTarget.value as "fullscreen" | "supporting_card",
                  })
                }
                className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-[#fafaf8] px-3 text-base outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
              >
                <option value="fullscreen">Full screen</option>
                <option value="supporting_card">Supporting card</option>
              </select>
            </label>
            <label className="mt-3 block text-sm text-[#3f3f46]">
              Thought
              {beat.thought_source === "ai_draft" && (
                <span className="ml-2 rounded border border-lime-200 bg-lime-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-lime-800">
                  AI draft
                </span>
              )}
              <textarea
                value={beat.thought}
                onChange={(event) =>
                  patchBeat(beat.beat_id, {
                    thought: event.currentTarget.value,
                    thought_source: "user",
                  })
                }
                maxLength={280}
                rows={2}
                className="mt-1 w-full resize-none rounded-lg border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-base outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
              />
            </label>
          </li>
        ))}
      </ol>

      <button
        type="button"
        disabled={working || !visibleDraft.title.trim()}
        onClick={approve}
        className="mt-4 min-h-11 w-full rounded-lg bg-lime-600 px-4 py-2 text-sm font-semibold text-white outline-none hover:bg-lime-700 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {workingAction === "approve" ? "Approving…" : "Approve plan"}
      </button>
      <p className="mt-2 text-center text-xs text-[#71717a]">
        AI thoughts stay drafts until you approve this plan.
      </p>
      {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
    </section>
  );
}
