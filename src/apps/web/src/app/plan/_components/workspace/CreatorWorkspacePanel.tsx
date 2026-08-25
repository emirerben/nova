"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  decideCreatorWorkspaceRelevanceProposal,
  pollLatestCreatorWorkspaceReceipt,
  recordCreatorWorkspacePreferenceSignal,
  type CreatorWorkspaceRelevanceProposal,
  type CreatorWorkspaceReceipt,
} from "@/lib/plan-api";
import { PlanApiError } from "@/lib/plan-api";

interface CreatorWorkspacePanelProps {
  planId: string;
  /** Optional proposal supplied by an upload flow; absent on V1 workspaces. */
  proposal?: CreatorWorkspaceRelevanceProposal | null;
  onProposalChange?: (proposal: CreatorWorkspaceRelevanceProposal) => void;
}

export function CreatorWorkspacePanel({
  planId,
  proposal,
  onProposalChange,
}: CreatorWorkspacePanelProps) {
  const [receipt, setReceipt] = useState<CreatorWorkspaceReceipt | null>(null);
  const [available, setAvailable] = useState(false);
  const [note, setNote] = useState("");
  const [pendingNote, setPendingNote] = useState<string | null>(null);
  const [savingNote, setSavingNote] = useState(false);
  const [pendingDecision, setPendingDecision] = useState<"accept_existing" | "accept_new_topic" | "reject" | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
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

  async function confirmPreference() {
    if (!pendingNote || savingNote) return;
    setSavingNote(true);
    setError(null);
    try {
      await recordCreatorWorkspacePreferenceSignal(planId, pendingNote, eventId(), undefined, receipt?.receipt_id);
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
    if (!proposal || !pendingDecision || !proposal.proposal_hash || deciding) return;
    setDeciding(true);
    setError(null);
    try {
      const next = await decideCreatorWorkspaceRelevanceProposal(planId, proposal.proposal_id, {
        expected_proposal_hash: proposal.proposal_hash,
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

  if (!available && !proposal) return null;
  const deliverables = receipt?.deliverables ?? [];
  const done = deliverables.filter((item) => item.status === "ready").length;

  return (
    <section aria-label="Creator workspace" className="rounded-xl border border-white/15 bg-[#151515] p-4 text-white">
      {available && receipt && (
        <>
          <div className="flex items-baseline justify-between gap-3">
            <div>
              <p className="text-sm font-semibold">Workspace progress</p>
              <p className="mt-1 text-xs text-white/55">{done} of {deliverables.length} deliverables ready · {statusLabel(receipt.status)}</p>
            </div>
            <span className="text-xs text-lime-200">{deliverables.length ? Math.round((done / deliverables.length) * 100) : 0}%</span>
          </div>
          {deliverables.length > 0 && (
            <ul className="mt-3 grid gap-2 sm:grid-cols-2">
              {deliverables.map((item) => (
                <li key={item.deliverable_id} className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-white/70">
                  <span className="font-medium text-white">Deliverable</span> · {statusLabel(item.status)}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {proposal && <RelevanceDecision proposal={proposal} pendingDecision={pendingDecision} setPendingDecision={setPendingDecision} onConfirm={() => void confirmDecision()} deciding={deciding} />}

      {available && (
        <div className="mt-4 border-t border-white/10 pt-4">
          <p className="text-sm font-medium">Teach Kria one thing about your taste</p>
          <p className="mt-1 text-xs text-white/50">Only a note you explicitly confirm becomes a preference signal.</p>
          {pendingNote ? (
            <div className="mt-3 rounded-md border border-lime-300/30 bg-lime-300/10 p-3">
              <p className="text-sm text-white">“{pendingNote}”</p>
              <div className="mt-2 flex gap-2">
                <Button size="sm" disabled={savingNote} onClick={() => void confirmPreference()}>{savingNote ? "Saving…" : "Confirm preference"}</Button>
                <Button size="sm" variant="outline" disabled={savingNote} onClick={() => setPendingNote(null)}>Keep editing</Button>
              </div>
            </div>
          ) : (
            <form className="mt-3 flex gap-2" onSubmit={(event) => { event.preventDefault(); if (note.trim()) setPendingNote(note.trim()); }}>
              <Input aria-label="Preference note" value={note} onChange={(event) => setNote(event.target.value)} className="min-w-0 flex-1 border-white/15 bg-white/[0.06] text-white placeholder:text-white/35" placeholder="More quiet openings, please…" />
              <Button type="submit" size="sm" disabled={!note.trim()}>Review</Button>
            </form>
          )}
        </div>
      )}
      {error && <p className="mt-3 text-xs text-amber-200" role="status">{error}</p>}
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
  if (proposal.status === "pending") return <p className="mt-3 text-sm text-white/60" role="status">Checking where this footage belongs…</p>;
  if (proposal.status === "failed") return <p className="mt-3 text-sm text-amber-200" role="status">We couldn’t classify that footage yet.</p>;
  if (proposal.status !== "ready") return null;
  const title = proposal.relevance === "existing_item" ? "This looks useful for an existing plan item." : proposal.relevance === "new_topic" ? `This could be a new topic: ${proposal.topic ?? "Untitled"}` : "This doesn’t clearly match the current plan.";
  const decision = proposal.relevance === "existing_item" ? "accept_existing" : proposal.relevance === "new_topic" ? "accept_new_topic" : "reject";
  return (
    <div className="mt-4 rounded-lg border border-white/15 bg-white/[0.04] p-3">
      <p className="text-sm font-medium">{title}</p>
      {proposal.rationale && <p className="mt-1 text-xs text-white/55">{proposal.rationale}</p>}
      {!pendingDecision ? (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" onClick={() => setPendingDecision(decision)}>Review suggested choice</Button>
          <Button size="sm" variant="outline" onClick={() => setPendingDecision("reject")}>Keep out of plan</Button>
        </div>
      ) : (
        <div className="mt-3 rounded-md border border-lime-300/30 bg-lime-300/10 p-3">
          <p className="text-xs text-white/75">Confirm: {pendingDecision === "reject" ? "keep this footage out of the plan" : "use this footage in the workspace"}?</p>
          <div className="mt-2 flex gap-2"><Button size="sm" disabled={deciding} onClick={onConfirm}>{deciding ? "Saving…" : "Confirm decision"}</Button><Button size="sm" variant="outline" disabled={deciding} onClick={() => setPendingDecision(null)}>Cancel</Button></div>
        </div>
      )}
    </div>
  );
}

function statusLabel(status: string): string {
  return status.replace(/_/g, " ");
}

function eventId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}
