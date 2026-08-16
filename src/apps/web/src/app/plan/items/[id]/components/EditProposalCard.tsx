"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  approveEditProposal,
  draftEditProposal,
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
  if (!src) {
    return (
      <div className="flex h-14 w-10 shrink-0 items-center justify-center rounded-md bg-zinc-100 px-1 text-center text-[9px] text-zinc-500">
        {kind === "image" ? "Photo" : "Video"}
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
      className="h-14 w-10 shrink-0 rounded-md object-cover"
    />
  );
}

export default function EditProposalCard({
  item,
  onChanged,
}: {
  item: PlanItem;
  onChanged: (item: PlanItem) => void;
}) {
  const proposal = item.edit_proposal ?? null;
  const [briefOpen, setBriefOpen] = useState(false);
  const [direction, setDirection] = useState<EditProposalDirection>("guided_story");
  const [goal, setGoal] = useState("");
  const [pace, setPace] = useState<EditProposalPace>("balanced");
  const [duration, setDuration] = useState(24);
  const [working, setWorking] = useState(false);
  const [editingApproved, setEditingApproved] = useState(false);
  const [draft, setDraft] = useState<EditProposalSnapshot | null>(proposal?.draft ?? null);
  const [error, setError] = useState<string | null>(null);
  // Poll responses recreate the draft object; only the CAS version denotes a
  // durable server revision that should replace the creator's unsaved edits.
  const appliedProposalRevision = useRef<string | null>(null);

  useEffect(() => {
    const revision = `${item.id}:${proposal?.proposal_version ?? "none"}`;
    if (appliedProposalRevision.current === revision) return;
    appliedProposalRevision.current = revision;
    setDraft(proposal?.draft ?? null);
    const retryDirection =
      proposal?.status === "stale" ? proposal.last_approved?.snapshot : null;
    if (retryDirection) {
      setDirection(retryDirection.direction);
      setGoal(retryDirection.goal);
      setPace(retryDirection.pace);
      setDuration(retryDirection.duration_s);
    } else if (proposal?.brief) {
      setDirection(proposal.brief.direction);
      setGoal(proposal.brief.goal);
      setPace(proposal.brief.pace);
      setDuration(proposal.brief.duration_s);
    }
    if (proposal?.status !== "approved") setEditingApproved(false);
  }, [item.id, proposal]);

  const mediaById = useMemo(
    () => new Map((draft?.media ?? []).map((ref) => [ref.media_id, ref])),
    [draft?.media],
  );

  async function startDraft() {
    if (working) return;
    setWorking(true);
    setError(null);
    try {
      const updated = await draftEditProposal(item.id, {
        direction,
        goal: goal.trim(),
        pace,
        duration_s: duration,
      });
      setBriefOpen(false);
      onChanged(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kria couldn't start planning this edit.");
    } finally {
      setWorking(false);
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
    setWorking(true);
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
      setWorking(false);
    }
  }

  if (!proposal && !briefOpen) {
    return (
      <div className="mt-5 border-t border-zinc-200 pt-5">
        <button
          type="button"
          onClick={() => setBriefOpen(true)}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-[#0c0c0e] outline-none transition-colors hover:border-lime-500 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
        >
          <span aria-hidden>✦</span>
          Plan edit
        </button>
        <p className="mt-2 text-sm text-[#71717a]">
          Ask Nova to understand all your uploads and propose a sequence before rendering.
        </p>
      </div>
    );
  }

  if (briefOpen || proposal?.status === "failed" || proposal?.status === "stale") {
    const stale = proposal?.status === "stale";
    return (
      <section className="mt-5 rounded-xl border border-zinc-200 bg-white p-4" aria-labelledby="plan-edit-heading">
        <h2 id="plan-edit-heading" className="font-display text-lg font-medium text-[#0c0c0e]">
          {stale ? "Your media changed" : "What should this edit do?"}
        </h2>
        {stale && proposal?.last_approved ? (
          <p className="mt-1 text-sm text-[#71717a]">
            Your last approved plan, “{proposal.last_approved.snapshot.title},” is saved for comparison.
          </p>
        ) : null}
        {proposal?.failure ? <p className="mt-2 text-sm text-[#71717a]">{proposal.failure.message}</p> : null}

        <fieldset className="mt-4">
          <legend className="text-sm font-medium text-[#0c0c0e]">Edit direction</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            {DIRECTION_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={`min-h-16 cursor-pointer rounded-lg border p-3 outline-none transition-colors focus-within:ring-2 focus-within:ring-lime-600 ${
                  direction === option.value ? "border-lime-500 bg-lime-50" : "border-zinc-200"
                }`}
              >
                <input
                  type="radio"
                  name="edit-direction"
                  value={option.value}
                  checked={direction === option.value}
                  onChange={() => setDirection(option.value)}
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
            value={goal}
            onChange={(event) => setGoal(event.currentTarget.value)}
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
              value={pace}
              onChange={(event) => setPace(event.currentTarget.value as EditProposalPace)}
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
              value={duration}
              onChange={(event) => setDuration(Number(event.currentTarget.value))}
              className="mt-2 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/30"
            >
              {[15, 20, 24, 30, 45, 60].map((seconds) => (
                <option key={seconds} value={seconds}>{seconds} seconds</option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={working}
            onClick={startDraft}
            className="min-h-11 rounded-lg bg-lime-600 px-4 py-2 text-sm font-semibold text-white outline-none hover:bg-lime-700 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {working ? "Starting…" : stale ? "Plan again" : "Build edit plan"}
          </button>
          {!proposal && (
            <button
              type="button"
              disabled={working}
              onClick={() => setBriefOpen(false)}
              className="min-h-11 rounded-lg border border-zinc-200 px-4 py-2 text-sm text-[#3f3f46] outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
            >
              Cancel
            </button>
          )}
        </div>
        {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
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
          {visibleDraft.story_beats.length} moments · {visibleDraft.media.length} sources · about {visibleDraft.duration_s}s
        </p>
        <button
          type="button"
          onClick={() => setEditingApproved(true)}
          className="mt-3 min-h-11 rounded-lg border border-lime-700 px-4 py-2 text-sm font-medium text-lime-700 outline-none focus-visible:ring-2 focus-visible:ring-lime-700"
        >
          Edit plan
        </button>
      </section>
    );
  }

  return (
    <section className="mt-5 rounded-xl border border-lime-200 bg-lime-50 p-4" aria-labelledby="draft-plan-heading">
      <p className="text-[11px] font-semibold uppercase tracking-[.15em] text-lime-700">Nova’s draft</p>
      <h2 id="draft-plan-heading" className="sr-only">Review edit plan</h2>
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
            {[15, 20, 24, 30, 45, 60].map((seconds) => (
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
        {working ? "Approving…" : "Approve plan"}
      </button>
      <p className="mt-2 text-center text-xs text-[#71717a]">
        AI thoughts stay drafts until you approve this plan.
      </p>
      {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
    </section>
  );
}
