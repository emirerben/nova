"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  decideCreatorWorkspaceRelevanceProposal,
  getCreatorWorkspaceRelevanceProposal,
  pollLatestCreatorWorkspaceReceipt,
  recordCreatorWorkspacePreferenceSignal,
  type CreatorWorkspaceRelevanceProposal,
  type CreatorWorkspaceReceipt,
} from "@/lib/plan-api";
import { PlanApiError } from "@/lib/plan-api";

const CREATOR_WORKSPACE_PREFERENCES_ENABLED =
  process.env.NEXT_PUBLIC_MAIN_CREATOR_AGENT_WORKSPACE_ENABLED === "true";

interface CreatorWorkspacePanelProps {
  planId: string;
  /** Optional proposal supplied by an upload flow; absent on V1 workspaces. */
  proposal?: CreatorWorkspaceRelevanceProposal | null;
  onProposalChange?: (proposal: CreatorWorkspaceRelevanceProposal) => void;
  /** Test seam; production defaults to the build-time workspace UI flag. */
  preferencesEnabled?: boolean;
}

export function CreatorWorkspacePanel({
  planId,
  proposal,
  onProposalChange,
  preferencesEnabled = CREATOR_WORKSPACE_PREFERENCES_ENABLED,
}: CreatorWorkspacePanelProps) {
  const [receipt, setReceipt] = useState<CreatorWorkspaceReceipt | null>(null);
  const [available, setAvailable] = useState(false);
  const [note, setNote] = useState("");
  const [pendingNote, setPendingNote] = useState<string | null>(null);
  const [savingNote, setSavingNote] = useState(false);
  const [pendingDecision, setPendingDecision] = useState<"accept_existing" | "accept_new_topic" | "reject" | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [proposalPollError, setProposalPollError] = useState<{
    message: string;
    terminal: boolean;
  } | null>(null);
  const [proposalPollAttempt, setProposalPollAttempt] = useState(0);
  const activeProposal = proposal?.plan_id === planId ? proposal : null;
  const activeReceipt = receipt?.plan_id === planId ? receipt : null;

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const next = await pollLatestCreatorWorkspaceReceipt(planId);
      setReceipt(next);
      setAvailable(true);
    } catch (reason) {
      // A missing/default-off coordination capability is intentionally silent.
      if (reason instanceof PlanApiError && reason.status === 404) {
        setAvailable(false);
        return;
      }
      setError("Workspace progress is temporarily unavailable.");
    }
  }, [planId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // The upload flow hands us the durable proposal identity. Poll that exact
  // row until the classifier reaches a terminal state; never infer relevance
  // in the browser and never attach footage during polling.
  useEffect(() => {
    if (!onProposalChange || !activeProposal || !["pending", "processing"].includes(activeProposal.status)) {
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await getCreatorWorkspaceRelevanceProposal(
          planId,
          activeProposal.proposal_id,
        );
        if (
          !cancelled &&
          next.plan_id === planId &&
          next.proposal_id === activeProposal.proposal_id
        ) {
          setProposalPollError(null);
          onProposalChange(next);
        }
      } catch (reason) {
        if (cancelled) return;
        const terminal =
          reason instanceof PlanApiError && [404, 409].includes(reason.status);
        setProposalPollError({
          terminal,
          message: terminal
            ? "That footage proposal is no longer available."
            : "We’re having trouble checking that footage. We’ll keep trying.",
        });
        if (terminal && timer !== undefined) window.clearInterval(timer);
      }
    };
    timer = window.setInterval(() => void poll(), 2000);
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [activeProposal, onProposalChange, planId, proposalPollAttempt]);

  async function confirmPreference() {
    if (!pendingNote || savingNote) return;
    setSavingNote(true);
    setError(null);
    try {
      await recordCreatorWorkspacePreferenceSignal(planId, pendingNote, eventId(), undefined, activeReceipt?.receipt_id);
      setPendingNote(null);
      setNote("");
      await refresh();
    } catch {
      setError("We couldn’t save that preference yet.");
    } finally {
      setSavingNote(false);
    }
  }

  async function confirmDecision() {
    if (!activeProposal || !pendingDecision || !activeProposal.proposal_hash || deciding) return;
    setDeciding(true);
    setError(null);
    try {
      const next = await decideCreatorWorkspaceRelevanceProposal(planId, activeProposal.proposal_id, {
        expected_proposal_hash: activeProposal.proposal_hash,
        decision: pendingDecision,
        client_event_id: eventId(),
      });
      onProposalChange?.(next);
      setPendingDecision(null);
    } catch {
      setError("That upload proposal changed. Refresh and review it again.");
    } finally {
      setDeciding(false);
    }
  }

  // Preserve the rollout-gated empty state for 404s, but keep operational
  // failures visible even when the capability was never advertised.
  if (!available && !activeProposal && !error && !preferencesEnabled) return null;
  const deliverables = activeReceipt?.deliverables ?? [];
  const done = deliverables.filter((item) => item.status === "ready").length;

  return (
    <section aria-label="Creator workspace" className="rounded-2xl border border-zinc-200 bg-white p-4 text-[#0c0c0e] shadow-sm">
      {available && activeReceipt && (
        <>
          <div className="flex items-baseline justify-between gap-3">
            <div>
              <p className="text-sm font-semibold">Workspace progress</p>
              <p className="mt-1 text-sm text-zinc-500">{done} of {deliverables.length} deliverables ready · {statusLabel(activeReceipt.status)}</p>
            </div>
          </div>
          {deliverables.length > 0 && (
            <ul className="mt-3 grid gap-2 sm:grid-cols-2">
              {deliverables.map((item, index) => (
                <li key={item.deliverable_id} className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-600">
                  <span className="font-medium text-zinc-900">{deliverableLabel(item, index)}</span> · {statusLabel(item.status)}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {activeProposal && !proposalPollError?.terminal && <RelevanceDecision proposal={activeProposal} pendingDecision={pendingDecision} setPendingDecision={setPendingDecision} onConfirm={() => void confirmDecision()} deciding={deciding} />}
      {activeProposal && proposalPollError && (
        <div className="mt-3 flex flex-wrap items-center gap-3 rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700" role={proposalPollError.terminal ? "alert" : "status"}>
          <span>{proposalPollError.message}</span>
          {proposalPollError.terminal && (
            <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => { setProposalPollError(null); setProposalPollAttempt((value) => value + 1); }}>
              Retry check
            </Button>
          )}
        </div>
      )}

      {(available || preferencesEnabled) && (
        <div className="mt-4 border-t border-zinc-200 pt-4">
          <p className="text-sm font-medium">Teach Kria one thing about your taste</p>
          <p className="mt-1 text-sm text-zinc-500">Only a note you explicitly confirm becomes a preference signal.</p>
          {pendingNote ? (
            <div className="mt-3 rounded-md border border-lime-200 bg-lime-50 p-3">
              <p className="text-sm text-zinc-900">“{pendingNote}”</p>
              <div className="mt-2 flex gap-2">
                <Button size="sm" variant="secondary" className="min-h-11" disabled={savingNote} onClick={() => void confirmPreference()}>{savingNote ? "Saving…" : "Confirm preference"}</Button>
                <Button size="sm" variant="outline" className="min-h-11" disabled={savingNote} onClick={() => setPendingNote(null)}>Keep editing</Button>
              </div>
            </div>
          ) : (
            <form className="mt-3 flex gap-2" onSubmit={(event) => { event.preventDefault(); if (note.trim()) setPendingNote(note.trim()); }}>
              <Input aria-label="Preference note" value={note} onChange={(event) => setNote(event.target.value)} className="min-h-11 min-w-0 flex-1" placeholder="More quiet openings, please…" />
              <Button type="submit" size="sm" variant="secondary" className="min-h-11" disabled={!note.trim()}>Review</Button>
            </form>
          )}
        </div>
      )}
      {error && (
        <div className="mt-3 flex flex-wrap items-center gap-3 rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-600" role="status">
          <span>{error}</span>
          <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => void refresh()}>
            Retry
          </Button>
        </div>
      )}
    </section>
  );
}

function RelevanceDecision({
  proposal,
  pendingDecision,
  setPendingDecision,
  onConfirm,
  deciding,
}: {
  proposal: CreatorWorkspaceRelevanceProposal;
  pendingDecision: "accept_existing" | "accept_new_topic" | "reject" | null;
  setPendingDecision: (decision: "accept_existing" | "accept_new_topic" | "reject" | null) => void;
  onConfirm: () => void;
  deciding: boolean;
}) {
  if (proposal.status === "pending" || proposal.status === "processing") return <p className="mt-3 text-sm text-zinc-600" role="status">Checking where this footage belongs…</p>;
  if (proposal.status === "failed") return <p className="mt-3 text-sm text-zinc-700" role="alert">We couldn’t classify that footage yet.</p>;
  if (proposal.status !== "ready") return null;
  const title = proposal.relevance === "existing_item" ? "This looks useful for an existing plan item." : proposal.relevance === "new_topic" ? `This could be a new topic: ${proposal.topic ?? "Untitled"}` : "This doesn’t clearly match the current plan.";
  const decision = proposal.relevance === "existing_item" ? "accept_existing" : proposal.relevance === "new_topic" ? "accept_new_topic" : "reject";
  return (
    <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50 p-3">
      <p className="text-sm font-medium">{title}</p>
      {proposal.rationale && <p className="mt-1 text-sm text-zinc-600">{proposal.rationale}</p>}
      {!pendingDecision ? (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" className="min-h-11" onClick={() => setPendingDecision(decision)}>Review suggested choice</Button>
          <Button size="sm" variant="outline" className="min-h-11" onClick={() => setPendingDecision("reject")}>Keep out of plan</Button>
        </div>
      ) : (
        <div className="mt-3 rounded-md border border-lime-200 bg-lime-50 p-3">
          <p className="text-sm text-zinc-700">Confirm: {pendingDecision === "reject" ? "keep this footage out of the plan" : "use this footage in the workspace"}?</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" className="min-h-11" disabled={deciding} onClick={onConfirm}>
              {deciding ? "Saving…" : "Confirm decision"}
            </Button>
            <Button size="sm" variant="outline" className="min-h-11" disabled={deciding} onClick={() => setPendingDecision(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function statusLabel(status: string): string {
  return {
    pending: "Waiting to start",
    processing: "In progress",
    ready: "Ready",
    failed: "Needs attention",
    stale: "Outdated",
  }[status] ?? "Status unavailable";
}

function deliverableLabel(
  _item: CreatorWorkspaceReceipt["deliverables"][number],
  index: number,
): string {
  return `Deliverable ${index + 1}`;
}

function eventId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}
