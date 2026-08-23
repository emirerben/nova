"use client";

/**
 * Guided-edit planning conversation.
 *
 * DESIGN.md's editorial-interview stance (see AskKriaPanel.tsx: "Editorial
 * interview, not a chat app — NO bubbles, NO avatar") is deliberately
 * overridden for THIS surface per explicit founder direction: the durable
 * multi-turn server thread (`edit_proposal.conversation`, up to 20 turns)
 * reads better as a real chat — every turn visible, an optimistic echo while
 * Kria thinks, honest errors. Visual language matches the edit copilot's
 * bubbles (`_editor/CopilotDrawer.tsx`) via the shared `components/chat/`
 * primitives; CopilotDrawer itself is untouched.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  approveEditProposal,
  draftEditProposal,
  editProposalConversationTurn,
  updateEditProposal,
  PlanApiError,
  type EditProposalDirection,
  type EditProposalPace,
  type EditProposalSnapshot,
  type PlanItem,
} from "@/lib/plan-api";
import { InfoDot } from "@/components/ui/InfoDot";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ArrowUp, ChevronDown, ChevronUp } from "lucide-react";
import { ChatBubble } from "@/components/chat/ChatBubble";
import { ChatThinking } from "@/components/chat/ChatThinking";
import { useAutoScrollToEnd } from "@/components/chat/useAutoScrollToEnd";
import { chatErrorMessage } from "@/lib/chat-errors";

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

// Starter chips shown before Kria has said anything at all.
const BRIEFING_STARTER_SUGGESTIONS = [
  "A personal travel diary",
  "Fast highlights, little text",
  "Explain what stood out",
];
const REVIEW_SUGGESTIONS = ["Make it more personal", "Use less text", "Put food first"];
// Fallback chips when the last agent turn came back with none of its own
// (briefing only shows these while the brief still isn't ready — once it is,
// the "Build this edit plan" CTA is the next step, not more chat prompting).
const BRIEFING_FALLBACK_SUGGESTIONS = [
  "Make it more personal",
  "Keep it short and punchy",
  "You decide — build the plan",
];

const EMPTY_MEDIA_MESSAGE = "Add a photo or video first — Kria plans from your real footage";

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
        className="flex h-14 w-10 shrink-0 items-center justify-center rounded-md bg-muted px-1 text-center text-xs text-muted-foreground"
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
  hasPoolMedia = false,
  defaultConversationOpen = false,
}: {
  item: PlanItem;
  onChanged: (item: PlanItem) => void;
  onRefresh?: () => void;
  /** Mirrors the backend's own media gate for a conversation turn: a pool
   *  asset still finishing analysis (queued/analyzing) or ready also counts,
   *  not just clip_assignments/clip_gcs_paths — a pool-only item (nothing
   *  attached to a shot yet) must not get locked out of chat. */
  hasPoolMedia?: boolean;
  /** PlanThreadPanel mounts this fresh every open (unmounted while closed),
   *  so it seeds straight onto the conversation surface — skipping the
   *  "Plan edit" button morph, which only matters when the card lives
   *  inline on the setup page. */
  defaultConversationOpen?: boolean;
}) {
  const proposal = item.edit_proposal ?? null;
  const conversationEnabled = item.guided_edit_conversation_available === true;
  const [conversationOpen, setConversationOpen] = useState(
    defaultConversationOpen || (conversationEnabled && proposal?.status === "briefing"),
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
  // Local optimistic echo of the in-flight message: shown as a pending bubble
  // until the server's durable turn (onChanged) or a failure clears it.
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const working = workingAction !== null;
  const conversationInProgress = proposal?.conversation_in_progress === true;
  const conversationRetryRequired = proposal?.conversation_retry_required === true;
  const conversationBlocked = conversationInProgress || conversationRetryRequired;
  const hasMedia =
    (item.clip_gcs_paths?.length ?? 0) > 0 ||
    (item.clip_assignments?.length ?? 0) > 0 ||
    hasPoolMedia;
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
  const brief = proposal?.brief ?? DEFAULT_BRIEF;

  async function sendConversation(text: string, reviewing = false) {
    const trimmed = text.trim();
    // The Send button already disables on conversationInProgress, but the
    // composer stays typeable (Enter still submits) — guard here too so a
    // reload-resumed in-flight turn can't be double-submitted via keyboard.
    if (!trimmed || working || conversationInProgress || !hasMedia) return;
    setWorkingAction("conversation");
    setError(null);
    setMessage("");
    setPendingMessage(trimmed);
    // Durable save from a dirty draft, held back from onChanged until we know
    // the outcome of the turn that follows it (see catch below) — calling
    // onChanged for both the intermediate save AND the final turn made one
    // submit visibly flicker (save → draft view → turn reply).
    let savedFromDirtyDraft: PlanItem | null = null;
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
          savedFromDirtyDraft = saved;
          expectedVersion = savedVersion;
        }
      }
      const updated = await editProposalConversationTurn(
        item.id,
        expectedVersion,
        trimmed,
      );
      // The server response carries the new turn durably — the pending echo
      // above is superseded by the real thread, no separate clear needed
      // beyond the `finally` below.
      onChanged(updated);
    } catch (err) {
      // The manual-edit save above succeeded even though the turn that
      // followed it failed — surface that durable save so a retry advances
      // from its CAS version instead of resending a now-stale one.
      if (savedFromDirtyDraft) onChanged(savedFromDirtyDraft);
      setMessage(trimmed);
      const code = err instanceof PlanApiError ? err.code : undefined;
      setError(
        code === "media_required"
          ? EMPTY_MEDIA_MESSAGE
          : chatErrorMessage(err, "Kria couldn't think that through."),
      );
      // A network timeout is ambiguous: the server may have committed the
      // turn even though this response was lost. Reconcile before a retry so
      // the browser adopts that durable version instead of resending stale CAS.
      onRefresh?.();
    } finally {
      setWorkingAction(null);
      setPendingMessage(null);
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
    const lastAgentTurn = [...conversation].reverse().find((turn) => turn.role === "agent");
    // The opener greeting only earns its place once — as soon as Kria has
    // said anything real (in EITHER phase), the durable thread carries the
    // conversation and a fresh greeting would be redundant.
    const showOpener = !lastAgentTurn;
    // Suggestion chips, unlike the opener, must stay phase-scoped (P2-1): a
    // briefing turn's suggestions must never leak into the review surface
    // (or vice versa) just because it happens to be the most recent turn
    // overall. Turns predating the "phase" field are treated as briefing.
    const phaseAgentTurn = [...conversation].reverse().find(
      (turn) => turn.role === "agent" && (reviewing ? turn.phase === "review" : turn.phase !== "review"),
    );
    const suggestions = !phaseAgentTurn
      ? reviewing
        ? REVIEW_SUGGESTIONS
        : BRIEFING_STARTER_SUGGESTIONS
      : phaseAgentTurn.suggestions.length > 0
        ? phaseAgentTurn.suggestions
        : reviewing
          ? REVIEW_SUGGESTIONS
          : proposal?.brief_ready
            ? []
            : BRIEFING_FALLBACK_SUGGESTIONS;
    const showBrief = Boolean(proposal);
    const sending = workingAction === "conversation" || conversationInProgress;
    const chipsDisabled = working || conversationInProgress || !hasMedia;

    return (
      <div data-testid="edit-guide-conversation">
        <p className="text-xs font-semibold text-muted-foreground">
          {reviewing ? "Shape the draft with Kria" : "Plan with Kria"}
        </p>

        <ConversationThread
          showOpener={showOpener}
          opener={opener}
          turns={conversation}
          pendingMessage={pendingMessage}
          sending={sending}
        />

        {conversationRetryRequired ? (
          <p role="status" className="mt-3 text-sm text-muted-foreground">
            That reply took too long. Send your direction again to continue.
          </p>
        ) : !sending ? (
          <div className="mt-4 flex flex-wrap gap-2">
            {suggestions.map((suggestion) => (
              <Button
                key={suggestion}
                type="button"
                variant="outline"
                size="sm"
                disabled={chipsDisabled}
                onClick={() => void sendConversation(suggestion, reviewing)}
              >
                {suggestion}
              </Button>
            ))}
          </div>
        ) : null}

        {!hasMedia ? (
          <p role="status" className="mt-3 rounded-lg border border-border bg-background px-3 py-2 text-sm text-muted-foreground">
            {EMPTY_MEDIA_MESSAGE}
          </p>
        ) : null}

        <form
          className="mt-4 flex items-end gap-2 rounded-2xl border border-input bg-background px-3 py-2 focus-within:ring-1 focus-within:ring-ring"
          onSubmit={(event) => {
            event.preventDefault();
            void sendConversation(message, reviewing);
          }}
        >
          <label htmlFor="edit-guide-message" className="sr-only">
            Tell Kria what you want in the edit
          </label>
          <Textarea
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
            rows={1}
            maxLength={1000}
            placeholder={reviewing ? "For example: focus more on the food…" : "Tell me in your own words…"}
            className="min-h-11 flex-1 resize-none border-0 bg-transparent px-0 py-2 shadow-none outline-none focus-visible:ring-0 [field-sizing:content]"
          />
          <Button
            type="submit"
            size="icon"
            disabled={working || conversationInProgress || !message.trim() || !hasMedia}
            aria-label="Send direction"
            className="shrink-0 rounded-full"
          >
            <ArrowUp className="h-4 w-4" />
          </Button>
        </form>

        {showBrief ? (
          <div className="mt-4 border-t border-border pt-4">
            <p className="text-xs font-medium text-muted-foreground">What I heard</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Badge variant="secondary">{DIRECTION_LABELS[brief.direction]}</Badge>
              <Badge variant="outline">{PACE_LABELS[brief.pace]} pace</Badge>
              <Badge variant="outline">About {brief.duration_s}s</Badge>
            </div>
            {brief.goal ? <p className="mt-2 text-sm text-foreground">{brief.goal}</p> : null}
          </div>
        ) : null}

        {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}

        {!reviewing ? (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button
              type="button"
              disabled={working || conversationBlocked}
              onClick={() => void startDraft()}
            >
              {workingAction === "plan"
                ? "Starting…"
                : proposal?.status === "failed"
                  ? "Try planning again"
                  : "Build this edit plan"}
            </Button>
            {!proposal ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={working}
                onClick={() => setConversationOpen(false)}
              >
                Cancel
              </Button>
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
        className="mt-5 rounded-xl border border-border bg-background p-4"
        aria-labelledby="legacy-plan-edit-heading"
      >
        <h2
          id="legacy-plan-edit-heading"
          className="font-semibold tracking-tight text-lg text-foreground"
        >
          {stale ? "Your media changed" : "What should this edit do?"}
        </h2>
        {stale && proposal?.last_approved ? (
          <p className="mt-1 text-sm text-muted-foreground">
            Your last approved plan, “{proposal.last_approved.snapshot.title},” is saved for comparison.
          </p>
        ) : null}
        {proposal?.failure ? (
          <p className="mt-2 text-sm text-muted-foreground">{proposal.failure.message}</p>
        ) : null}

        <fieldset className="mt-4">
          <legend className="text-sm font-medium text-foreground">Edit direction</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            {DIRECTION_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={`min-h-16 cursor-pointer rounded-lg border p-3 outline-none transition-colors focus-within:ring-2 focus-within:ring-ring ${
                  legacyBrief.direction === option.value
                    ? "border-primary bg-accent"
                    : "border-border"
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
                <span className="block text-sm font-medium text-foreground">{option.label}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">{option.description}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <label className="mt-4 block text-sm font-medium text-foreground">
          Goal or context
          <Textarea
            value={legacyBrief.goal}
            onChange={(event) => setLegacyBrief({ ...legacyBrief, goal: event.currentTarget.value })}
            maxLength={500}
            rows={3}
            placeholder="For example: show what surprised me about the food, town, and beaches."
            className="mt-2 resize-none"
          />
        </label>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label htmlFor="edit-proposal-legacy-pace" className="text-sm font-medium text-foreground">
            Pace
            <Select
              value={legacyBrief.pace}
              onValueChange={(value) =>
                setLegacyBrief({ ...legacyBrief, pace: value as EditProposalPace })
              }
            >
              <SelectTrigger id="edit-proposal-legacy-pace" aria-label="Pace" className="mt-2">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PACE_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label htmlFor="edit-proposal-legacy-duration" className="text-sm font-medium text-foreground">
            Target length
            <Select
              value={String(legacyBrief.duration_s)}
              onValueChange={(value) =>
                setLegacyBrief({ ...legacyBrief, duration_s: Number(value) })
              }
            >
              <SelectTrigger id="edit-proposal-legacy-duration" aria-label="Target length" className="mt-2">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {durationOptions(legacyBrief.duration_s).map((seconds) => (
                  <SelectItem key={seconds} value={String(seconds)}>{seconds} seconds</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <Button
            type="button"
            disabled={working}
            onClick={() => void startDraft()}
          >
            {workingAction === "plan" ? "Starting…" : stale ? "Plan again" : "Build edit plan"}
          </Button>
          {!proposal ? (
            <Button
              type="button"
              variant="outline"
              disabled={working}
              onClick={() => setLegacyBriefOpen(false)}
              className="text-muted-foreground"
            >
              Cancel
            </Button>
          ) : null}
        </div>
        {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}
      </section>
    );
  }

  if (!conversationEnabled) {
    if (!proposal && !legacyBriefOpen) {
      return (
        <div className="mt-5 border-t border-border pt-5">
          <Button
            type="button"
            variant="outline"
            onClick={() => setLegacyBriefOpen(true)}
          >
            <span aria-hidden>✦</span>
            Plan edit
          </Button>
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
      <div className="mt-5 border-t border-border pt-5">
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            setConversationOpen(true);
            window.setTimeout(() => inputRef.current?.focus(), 0);
          }}
        >
          <span aria-hidden>✦</span>
          Plan edit
        </Button>
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
        className="mt-5 rounded-xl border border-border bg-background p-4"
        aria-labelledby="plan-edit-heading"
      >
        <h2 id="plan-edit-heading" className="sr-only">Describe your edit to Kria</h2>
        {stale && proposal?.last_approved ? (
          <p className="mb-4 text-sm text-muted-foreground">
            Your last approved plan, “{proposal.last_approved.snapshot.title},” is saved for comparison.
          </p>
        ) : null}
        {proposal?.failure ? (
          <p className="mb-4 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground">
            {proposal.failure.message}
          </p>
        ) : null}
        {conversationSurface()}
      </section>
    );
  }

  if (proposal?.status === "analyzing" || proposal?.status === "drafting") {
    return (
      <Card aria-labelledby="planning-edit-heading" className="mt-5">
        <CardHeader>
          <CardTitle id="planning-edit-heading">Planning your edit</CardTitle>
        </CardHeader>
        <CardContent>
          <div role="status" aria-live="polite" className="flex items-center gap-2 text-sm text-foreground">
            <span aria-hidden className="h-2 w-2 rounded-full bg-primary motion-safe:animate-pulse" />
            {proposal.status === "analyzing"
              ? "Understanding every photo and video…"
              : "Building the direction and story beats…"}
          </div>
        </CardContent>
      </Card>
    );
  }

  const visibleDraft = draft ?? proposal?.last_approved?.snapshot ?? null;
  if (!visibleDraft) return null;
  const readOnlyApproved = proposal?.status === "approved" && !editingApproved;

  if (readOnlyApproved) {
    return (
      <Card aria-labelledby="approved-plan-heading" className="mt-5">
        <CardHeader>
          <Badge variant="secondary" className="w-fit">Approved edit plan</Badge>
          <CardTitle id="approved-plan-heading">{visibleDraft.title}</CardTitle>
          <CardDescription>
            {visibleDraft.story_beats.length} moments · {selectedSourceCount(visibleDraft)} sources · about {visibleDraft.duration_s}s
          </CardDescription>
        </CardHeader>
        {(proposal?.failure || proposal?.render_failure) && (
          <CardContent>
            <p className="rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground">
              {proposal?.failure?.message ?? proposal?.render_failure?.message}
            </p>
          </CardContent>
        )}
        <CardFooter className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => setEditingApproved(true)}
          >
            Edit plan
          </Button>
          {conversationEnabled ? (
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setEditingApproved(true);
                setConversationOpen(true);
              }}
            >
              Tell Kria a change
            </Button>
          ) : null}
        </CardFooter>
      </Card>
    );
  }

  return (
    <Card aria-labelledby="draft-plan-heading" className="mt-5">
      <CardHeader>
        <Badge variant="secondary" className="w-fit">Kria’s draft</Badge>
        <CardTitle id="draft-plan-heading" className="sr-only">Review edit plan</CardTitle>
        {conversationEnabled && conversationOpen ? (
          <div className="rounded-xl border border-border bg-background p-4">
            {conversationSurface({ reviewing: true })}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setConversationOpen(false)}
              className="mt-4"
            >
              Close conversation
            </Button>
          </div>
        ) : conversationEnabled ? (
          <Button
            type="button"
            variant="outline"
            className="w-fit"
            onClick={() => {
              setConversationOpen(true);
              window.setTimeout(() => inputRef.current?.focus(), 0);
            }}
          >
            Tell Kria what to change
          </Button>
        ) : null}
        <label className="block text-sm font-medium text-foreground">
          Title
          <Input
            value={visibleDraft.title}
            onChange={(event) => setDraft({ ...visibleDraft, title: event.currentTarget.value })}
            maxLength={100}
            className="mt-1"
          />
        </label>
        <label className="block text-sm font-medium text-foreground">
          Goal
          <Textarea
            value={visibleDraft.goal}
            onChange={(event) => setDraft({ ...visibleDraft, goal: event.currentTarget.value })}
            rows={2}
            maxLength={500}
            className="mt-1 resize-none"
          />
        </label>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <label htmlFor="edit-proposal-direction" className="text-sm font-medium text-foreground">
            Direction
            <Select
              value={visibleDraft.direction}
              onValueChange={(value) =>
                setDraft({ ...visibleDraft, direction: value as EditProposalDirection })
              }
            >
              <SelectTrigger id="edit-proposal-direction" aria-label="Direction" className="mt-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DIRECTION_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label htmlFor="edit-proposal-pace" className="text-sm font-medium text-foreground">
            Pace
            <Select
              value={visibleDraft.pace}
              onValueChange={(value) =>
                setDraft({ ...visibleDraft, pace: value as EditProposalPace })
              }
            >
              <SelectTrigger id="edit-proposal-pace" aria-label="Pace" className="mt-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PACE_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label htmlFor="edit-proposal-duration" className="text-sm font-medium text-foreground">
            Target length
            <Select
              value={String(visibleDraft.duration_s)}
              onValueChange={(value) =>
                setDraft({ ...visibleDraft, duration_s: Number(value) })
              }
            >
              <SelectTrigger id="edit-proposal-duration" aria-label="Target length" className="mt-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {durationOptions(visibleDraft.duration_s).map((seconds) => (
                  <SelectItem key={seconds} value={String(seconds)}>{seconds} seconds</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>

        <ol className="space-y-2">
          {visibleDraft.story_beats.map((beat, index) => (
            <li key={beat.beat_id} className="rounded-lg border border-border bg-background p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <label className="flex items-baseline gap-1 text-sm font-semibold text-foreground">
                    <span aria-hidden className="text-muted-foreground tabular-nums">{index + 1}.</span>
                    <Input
                      aria-label={`Moment ${index + 1} topic`}
                      value={beat.topic}
                      onChange={(event) =>
                        patchBeat(beat.beat_id, { topic: event.currentTarget.value })
                      }
                      maxLength={80}
                      className="h-auto flex-1 border-transparent bg-transparent px-1 py-1 text-base font-semibold shadow-none hover:border-input focus-visible:border-input focus-visible:bg-background focus-visible:ring-1 focus-visible:ring-ring"
                    />
                    <Badge variant="outline" className="shrink-0 tabular-nums">{beat.duration_s}s</Badge>
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
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    disabled={index === 0}
                    onClick={() => moveBeat(index, -1)}
                    aria-label={`Move ${beat.topic} earlier`}
                  >
                    <ChevronUp className="h-4 w-4" />
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    disabled={index === visibleDraft.story_beats.length - 1}
                    onClick={() => moveBeat(index, 1)}
                    aria-label={`Move ${beat.topic} later`}
                  >
                    <ChevronDown className="h-4 w-4" />
                  </Button>
                </div>
              </div>
              <label
                htmlFor={`edit-proposal-beat-layout-${beat.beat_id}`}
                className="mt-3 block text-sm text-foreground"
              >
                Layout
                <Select
                  value={beat.layout}
                  onValueChange={(value) =>
                    patchBeat(beat.beat_id, {
                      layout: value as "fullscreen" | "supporting_card",
                    })
                  }
                >
                  <SelectTrigger
                    id={`edit-proposal-beat-layout-${beat.beat_id}`}
                    aria-label="Layout"
                    className="mt-1"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="fullscreen">Full screen</SelectItem>
                    <SelectItem value="supporting_card">Supporting card</SelectItem>
                  </SelectContent>
                </Select>
              </label>
              <label className="mt-3 block text-sm text-foreground">
                Thought
                {beat.thought_source === "ai_draft" && (
                  <Badge variant="outline" className="ml-2">AI draft</Badge>
                )}
                <Textarea
                  value={beat.thought}
                  onChange={(event) =>
                    patchBeat(beat.beat_id, {
                      thought: event.currentTarget.value,
                      thought_source: "user",
                    })
                  }
                  maxLength={280}
                  rows={2}
                  className="mt-1 resize-none"
                />
              </label>
            </li>
          ))}
        </ol>
      </CardContent>

      <CardFooter className="items-center gap-2">
        <Button
          type="button"
          className="flex-1"
          disabled={working || !visibleDraft.title.trim()}
          onClick={approve}
        >
          {workingAction === "approve" ? "Approving…" : "Approve plan"}
        </Button>
        <InfoDot label="Plan approval">
          AI thoughts stay drafts until you approve this plan.
        </InfoDot>
      </CardFooter>
      {error && <p role="alert" className="px-6 pb-6 text-sm text-destructive">{error}</p>}
    </Card>
  );
}

/**
 * The scrolling bubble thread: every persisted turn (both "briefing" and
 * "review" phases, oldest first) plus an optional leading opener bubble and
 * a trailing optimistic/pending bubble. The server keeps only the last 20
 * turns (older ones drop off the front), so a turn's position in the array
 * — and therefore its index — shifts over the item's lifetime; keys are
 * derived from the turn's own content instead of a bare index.
 */
function ConversationThread({
  showOpener,
  opener,
  turns,
  pendingMessage,
  sending,
}: {
  showOpener: boolean;
  opener: string;
  turns: Array<{ role: "user" | "agent"; phase?: "briefing" | "review"; content: string }>;
  pendingMessage: string | null;
  sending: boolean;
}) {
  const threadRef = useAutoScrollToEnd<HTMLDivElement>([
    turns.length,
    pendingMessage,
    sending,
  ]);
  return (
    <ScrollArea ref={threadRef} className="mt-3 max-h-[320px] rounded-md border border-border">
      {/* role="log": an append-only running transcript, keyboard-scrollable
          (tabIndex) for creators who can't drag the scrollbar. No aria-live
          here — ChatThinking already owns its own role="status"/aria-live, and
          a live region on the whole thread would announce the creator's own
          pending echo back at them as if Kria had said it. */}
      <div role="log" tabIndex={0} className="space-y-3 p-4">
        {showOpener && <ChatBubble role="assistant">{opener}</ChatBubble>}
        {turns.map((turn, index) => (
          <ChatBubble
            key={`${turn.role}-${turn.phase ?? "briefing"}-${index}-${turn.content.slice(0, 24)}`}
            role={turn.role === "user" ? "user" : "assistant"}
          >
            {turn.content}
          </ChatBubble>
        ))}
        {pendingMessage !== null && (
          <ChatBubble role="user" pending>
            {pendingMessage}
          </ChatBubble>
        )}
        {sending && <ChatThinking />}
      </div>
    </ScrollArea>
  );
}
