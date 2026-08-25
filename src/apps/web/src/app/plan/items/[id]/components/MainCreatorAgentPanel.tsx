"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, Sparkles, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  cancelCreatorAgentSession,
  confirmCreatorAgentPlan,
  getCreatorAgentSession,
  requestCreatorAutoIteration,
  startCreatorAgentSession,
  turnCreatorAgentSession,
  type CreatorAgentEvent,
  type CreatorAgentReview,
  type CreatorAgentSession,
  PlanApiError,
} from "@/lib/plan-api";

function eventText(event: CreatorAgentEvent): string | null {
  for (const key of ["message", "reply", "summary", "text"]) {
    const value = event.payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function eventId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

const ACTIVE_STATES = new Set(["executing", "rendering", "reviewing"]);

export default function MainCreatorAgentPanel({ itemId }: { itemId: string }) {
  const [session, setSession] = useState<CreatorAgentSession | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoOptIn, setAutoOptIn] = useState(false);
  const [autoSending, setAutoSending] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = await getCreatorAgentSession(itemId);
      setAvailable(true);
      setSession(next);
      return next;
    } catch (reason) {
      // The public web flag can cover more users than a percentage-based API
      // rollout. Hide the entry point for users outside the server cohort.
      if (reason instanceof PlanApiError && reason.status === 404) {
        setAvailable(false);
        setSession(null);
        return null;
      }
      throw reason;
    }
  }, [itemId]);

  useEffect(() => {
    let cancelled = false;
    refresh()
      .catch(() => null)
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  useEffect(() => {
    if (!session || !ACTIVE_STATES.has(session.status)) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      await refresh().catch(() => null);
      if (!stopped) timer = window.setTimeout(() => void poll(), 4_000);
    };
    timer = window.setTimeout(() => void poll(), 4_000);
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refresh, session]);

  const visibleEvents = useMemo(
    () => session?.events.filter((event) => event.role !== "system" && eventText(event)) ?? [],
    [session],
  );

  async function send(): Promise<void> {
    const trimmed = message.trim();
    if (!trimmed || sending) return;
    setSending(true);
    setError(null);
    try {
      const next = session
        ? await turnCreatorAgentSession(itemId, session.id, trimmed, session.revision, eventId())
        : await startCreatorAgentSession(itemId, trimmed, eventId());
      setSession(next);
      setMessage("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Kria couldn't respond. Try again.");
    } finally {
      setSending(false);
    }
  }

  async function confirm(): Promise<void> {
    if (!session || sending) return;
    setSending(true);
    setError(null);
    try {
      setSession(await confirmCreatorAgentPlan(itemId, session, eventId()));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "That plan changed. Ask Kria to review it again.");
    } finally {
      setSending(false);
    }
  }

  async function cancel(): Promise<void> {
    if (!session || sending) return;
    setSending(true);
    try {
      setSession(await cancelCreatorAgentSession(itemId, session.id, session.revision));
    } finally {
      setSending(false);
    }
  }

  async function optIntoAutoIteration(): Promise<void> {
    if (!session?.auto_iteration?.available || !autoOptIn || autoSending) return;
    setAutoSending(true);
    setError(null);
    try {
      setSession(
        await requestCreatorAutoIteration(itemId, {
          session_id: session.id,
          expected_revision: session.revision,
          opt_in: true,
          client_event_id: eventId(),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Automatic revision could not be enabled.");
    } finally {
      setAutoSending(false);
    }
  }

  if (loading || !available) return null;

  const plan = session?.pending_plan;
  const busy = !!session && ACTIVE_STATES.has(session.status);
  const terminal = !!session && ["completed", "failed", "cancelled"].includes(session.status);
  const prompt = session
    ? "Tell Kria what to change or answer its question…"
    : "What should this video feel like? Mention the story, pace, audio, or anything you care about.";

  return (
    <section
      aria-label="Create with Kria"
      className="rounded-xl border border-lime-300/70 bg-lime-50/50 p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-[#0c0c0e]">
            <Sparkles className="h-4 w-4 text-lime-700" aria-hidden="true" />
            Create with Kria
          </div>
          <p className="mt-1 text-sm text-[#52525b]">
            Talk through the edit. Kria will propose a creative direction before rendering.
          </p>
        </div>
        {session && !["completed", "failed", "cancelled"].includes(session.status) && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Cancel creator session"
            disabled={sending}
            onClick={() => void cancel()}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        )}
      </div>

      {visibleEvents.length > 0 && (
        <div
          className="mt-4 max-h-52 space-y-3 overflow-y-auto border-l border-lime-300 pl-3"
          aria-live="polite"
        >
          {visibleEvents.map((event) => (
            <div key={event.id} className="text-sm text-[#27272a]">
              <p className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-[#71717a]">
                {event.role === "user" ? "You" : "Kria"}
              </p>
              <p>{eventText(event)}</p>
            </div>
          ))}
        </div>
      )}

      {plan && session?.status === "awaiting_confirmation" && (
        <div className="mt-4 rounded-lg border border-lime-300 bg-white p-3">
          <p className="text-sm font-semibold text-[#18181b]">{plan.summary}</p>
          {plan.creative_rationale && (
            <p className="mt-1 text-sm text-[#52525b]">{plan.creative_rationale}</p>
          )}
          {plan.intro_hook && (
            <p className="mt-3 text-sm text-[#3f3f46]">
              <span className="font-medium text-[#18181b]">Opening idea:</span> {plan.intro_hook}
            </p>
          )}
          {plan.story_structure && plan.story_structure.length > 0 && (
            <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-sm text-[#52525b]">
              {plan.story_structure.map((beat) => (
                <li key={beat}>{beat}</li>
              ))}
            </ol>
          )}
          <div className="mt-3 flex flex-wrap gap-1.5 text-xs text-[#3f3f46]">
            {plan.edit_format && <span className="rounded-full bg-zinc-100 px-2 py-1">{plan.edit_format}</span>}
            {plan.audio_strategy && <span className="rounded-full bg-zinc-100 px-2 py-1">{plan.audio_strategy}</span>}
            {plan.caption_style && <span className="rounded-full bg-zinc-100 px-2 py-1">{plan.caption_style} captions</span>}
          </div>
          <TreatmentPreview plan={plan} />
          <div className="mt-3 flex gap-2">
            {session.can_render ? (
              <Button type="button" size="sm" disabled={sending} onClick={() => void confirm()}>
                {sending ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : (
                  "Render this"
                )}
              </Button>
            ) : (
              <p className="self-center text-xs text-[#71717a]">
                Rendering is not enabled for this preview yet.
              </p>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={sending}
              onClick={() => setMessage("I'd like to change this direction: ")}
            >
              Change direction
            </Button>
          </div>
        </div>
      )}

      {busy && (
        <div className="mt-4 flex items-center gap-2 text-sm text-[#52525b]" aria-live="polite">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          {session?.status === "rendering" ? "Rendering the confirmed direction…" : "Kria is checking the edit…"}
        </div>
      )}

      {terminal && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="mt-4"
          onClick={() => {
            setSession(null);
            setMessage("");
            setError(null);
          }}
        >
          Start another direction
        </Button>
      )}

      {!busy && !terminal && (
        <div className="mt-4 space-y-2">
          <Textarea
            aria-label="Message Kria"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
            placeholder={prompt}
            rows={2}
          />
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-[#71717a]">Nothing renders until you confirm.</p>
            <Button type="button" size="sm" disabled={!message.trim() || sending} onClick={() => void send()}>
              {sending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : session ? "Send" : "Start"}
            </Button>
          </div>
        </div>
      )}

      {session?.last_review && <CreatorReviewReceipt review={session.last_review} />}

      {session?.auto_iteration?.available && session.status === "awaiting_feedback" && (
        <div className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
          <label className="flex items-start gap-2 text-sm text-zinc-700">
            <input
              type="checkbox"
              className="mt-0.5 accent-lime-600"
              checked={autoOptIn}
              onChange={(event) => setAutoOptIn(event.target.checked)}
            />
            <span>
              Allow one automatic revision if the review finds an objective issue.
              <span className="mt-1 block text-xs text-zinc-500">You can still review the result before publishing.</span>
            </span>
          </label>
          {autoOptIn && (
            <Button type="button" size="sm" className="mt-3" disabled={autoSending} onClick={() => void optIntoAutoIteration()}>
              {autoSending ? "Saving…" : "Confirm automatic revision"}
            </Button>
          )}
        </div>
      )}

      {error && <p className="mt-2 text-sm text-zinc-700" role="alert">{error}</p>}
    </section>
  );
}

function TreatmentPreview({ plan }: { plan: NonNullable<CreatorAgentSession["pending_plan"]> }) {
  const optional = plan.edit_plan?.strategy?.optional_treatments ?? [];
  const cards = [
    { key: "core", label: "Core", detail: `${plan.caption_style ?? "Auto"} captions · ${plan.audio_strategy ?? "Native"}` },
    ...(optional.includes("overlays") ? [{ key: "overlays", label: "Overlay", detail: "Visual accent layer" }] : []),
    ...(optional.includes("sfx") ? [{ key: "sfx", label: "SFX", detail: "Licensed sound accents" }] : []),
  ];
  return (
    <div className="mt-4 grid gap-2 sm:grid-cols-3" aria-label="Treatment previews">
      {cards.map((card) => (
        <div key={card.key} className="rounded-md border border-zinc-200 bg-white px-2.5 py-2">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-lime-700">{card.label}</p>
          <p className="mt-1 text-xs text-zinc-600">{card.detail}</p>
        </div>
      ))}
    </div>
  );
}

function CreatorReviewReceipt({ review }: { review: CreatorAgentReview }) {
  if (review.status === "queued" || review.status === "running") {
    return <p className="mt-4 text-sm text-zinc-600" role="status">Reviewing the exact render…</p>;
  }
  if (review.status === "failed" || review.status === "unavailable" || review.error_message) {
    return (
      <div className="mt-4 rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-700" role="status">
        <p className="font-medium">The quality review is unavailable.</p>
        <p className="mt-1 text-xs text-zinc-500">{review.error_message ?? "Your rendered video is still available. You can decide what feels right."}</p>
      </div>
    );
  }
  const evidence = review.evidence ?? [];
  return (
    <div className="mt-4 rounded-lg border border-zinc-200 bg-white p-3" aria-label="Render review">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-semibold text-zinc-900">Render review</p>
        {typeof review.quality_score === "number" && <span className="text-xs text-zinc-500">{review.quality_score.toFixed(1)}/5</span>}
      </div>
      {evidence.length > 0 ? (
        <ul className="mt-3 space-y-2">
          {evidence.map((item) => (
            <li key={item.evidence_id} className="border-l border-lime-600 pl-2 text-xs text-zinc-600">
              <span className="font-medium text-zinc-900">{formatReviewTime(item.start_s)} · {item.kind}</span>{" "}{item.observation}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-zinc-500">No evidence-linked issues were found.</p>
      )}
      {review.proposed_revision && (
        <p className="mt-3 border-t border-zinc-200 pt-3 text-xs text-zinc-600">
          Suggested next pass: <span className="text-zinc-900">{review.proposed_revision.summary}</span>
        </p>
      )}
      <p className="mt-3 text-[11px] text-zinc-500">This is a bounded recommendation. Nothing changes without your confirmation.</p>
    </div>
  );
}

function formatReviewTime(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}
