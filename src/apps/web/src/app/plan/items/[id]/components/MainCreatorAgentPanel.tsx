"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Sparkles, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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

function eventOptions(event: CreatorAgentEvent): string[] {
  const options = event.payload.options;
  return Array.isArray(options)
    ? options.filter((value): value is string => typeof value === "string" && value.trim().length > 0)
    : [];
}

function eventRecommendedOption(event: CreatorAgentEvent): string | null {
  const value = event.payload.recommended_option;
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function eventId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

const ACTIVE_STATES = new Set(["executing", "rendering", "reviewing"]);
const ACTIVE_REVIEW_STATES = new Set(["pending", "queued", "running"]);

function mixedMediaTimingLabel(
  profile: NonNullable<CreatorAgentSession["pending_plan"]>["mixed_media_timing"],
): string | null {
  if (
    profile?.image_hold !== "very_fast" ||
    profile.video_hold !== "longer" ||
    profile.boundary_style !== "cut"
  ) {
    return null;
  }
  return "Photos 0.5–0.8s · Videos 1.5–3s · hard cuts";
}

function shouldPoll(session: CreatorAgentSession): boolean {
  if (ACTIVE_STATES.has(session.status)) return true;
  return session.status === "awaiting_feedback" && ACTIVE_REVIEW_STATES.has(session.last_review?.status ?? "");
}

export default function MainCreatorAgentPanel({
  itemId,
  onActiveChange,
  onAvailabilityChange,
}: {
  itemId: string;
  onActiveChange?: (active: boolean) => void;
  onAvailabilityChange?: (
    availability: "loading" | "available" | "unavailable" | "error",
  ) => void;
}) {
  const [session, setSession] = useState<CreatorAgentSession | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoOptIn, setAutoOptIn] = useState(false);
  const [autoSending, setAutoSending] = useState(false);
  const reportedAvailability = useRef<string | null>(null);

  const reportAvailability = useCallback(
    (next: "loading" | "available" | "unavailable" | "error") => {
      if (reportedAvailability.current === next) return;
      reportedAvailability.current = next;
      onAvailabilityChange?.(next);
    },
    [onAvailabilityChange],
  );

  const acceptSession = useCallback(
    (next: CreatorAgentSession | null) => {
      setAvailable(true);
      setSession(next);
      setError(null);
      reportAvailability("available");
    },
    [reportAvailability],
  );

  const refresh = useCallback(async () => {
    try {
      const next = await getCreatorAgentSession(itemId);
      acceptSession(next);
      return next;
    } catch (reason) {
      // The public web flag can cover more users than a percentage-based API
      // rollout. Hide the entry point for users outside the server cohort.
      if (reason instanceof PlanApiError && reason.status === 404) {
        setAvailable(false);
        setSession(null);
        reportAvailability("unavailable");
        return null;
      }
      setError("Kria couldn't refresh this plan. Your work is safe.");
      reportAvailability("error");
      throw reason;
    }
  }, [acceptSession, itemId, reportAvailability]);

  useEffect(() => {
    let cancelled = false;
    reportAvailability("loading");
    refresh()
      .catch(() => null)
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refresh, reportAvailability]);

  const sessionActive =
    session != null && !["completed", "failed", "cancelled"].includes(session.status);
  useEffect(() => {
    onActiveChange?.(sessionActive);
  }, [onActiveChange, sessionActive]);

  useEffect(() => {
    if (!session || !shouldPoll(session)) return;
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

  async function send(option?: string): Promise<void> {
    const trimmed = (option ?? message).trim();
    if (!trimmed || sending) return;
    setSending(true);
    setError(null);
    try {
      const next = session
        ? await turnCreatorAgentSession(itemId, session.id, trimmed, session.revision, eventId())
        : await startCreatorAgentSession(itemId, trimmed, eventId());
      acceptSession(next);
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
      const next = await confirmCreatorAgentPlan(itemId, session, eventId());
      acceptSession(next);
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
      const next = await cancelCreatorAgentSession(itemId, session.id, session.revision);
      acceptSession(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Kria couldn't cancel this plan. Try again.");
    } finally {
      setSending(false);
    }
  }

  async function optIntoAutoIteration(): Promise<void> {
    if (!session?.auto_iteration?.available || !autoOptIn || autoSending) return;
    setAutoSending(true);
    setError(null);
    try {
      const next = await requestCreatorAutoIteration(itemId, {
          session_id: session.id,
          expected_revision: session.revision,
          opt_in: true,
          client_event_id: eventId(),
        });
      acceptSession(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Automatic revision could not be enabled.");
    } finally {
      setAutoSending(false);
    }
  }

  if (loading) {
    return (
      <section
        aria-label="Create with Kria"
        aria-busy="true"
        className="rounded-xl border border-lime-200/70 bg-lime-50/40 p-4"
      >
        <div className="flex items-center gap-2 text-sm font-semibold text-[#0c0c0e]">
          <Sparkles className="h-4 w-4 text-lime-700" aria-hidden="true" />
          Create with Kria
        </div>
        <div className="mt-3 flex items-center gap-3" role="status">
          <div className="h-9 w-9 rounded-lg bg-lime-200/70 motion-safe:animate-pulse" aria-hidden="true" />
          <div className="min-w-0 flex-1 space-y-2" aria-hidden="true">
            <div className="h-3 w-40 max-w-full rounded bg-zinc-200 motion-safe:animate-pulse" />
            <div className="h-3 w-64 max-w-full rounded bg-zinc-100 motion-safe:animate-pulse" />
          </div>
          <span className="sr-only">Checking your saved Kria plan…</span>
        </div>
      </section>
    );
  }
  if (!available) return null;

  const plan = session?.pending_plan;
  const timingLabel = plan ? mixedMediaTimingLabel(plan.mixed_media_timing) : null;
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
          {visibleEvents.map((event, eventIndex) => (
            <div key={event.id} className="text-sm text-[#27272a]">
              <p className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-[#71717a]">
                {event.role === "user" ? "You" : "Kria"}
              </p>
              <p>{eventText(event)}</p>
              {eventIndex === visibleEvents.length - 1 && event.event_type === "assistant_question" &&
                eventOptions(event).length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2" aria-label="Suggested answers">
                    {eventOptions(event).map((option) => (
                      <div key={option} className="flex flex-col items-start gap-1">
                        {option === eventRecommendedOption(event) ? (
                          <span className="text-[11px] font-medium uppercase tracking-wide text-lime-800">
                            Recommended
                          </span>
                        ) : null}
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="min-h-11 whitespace-normal border-zinc-200 bg-white text-left hover:border-lime-400 hover:bg-lime-50 focus-visible:ring-lime-500"
                          aria-label={`${option}${option === eventRecommendedOption(event) ? " (recommended)" : ""}`}
                          disabled={sending}
                          onClick={() => void send(option)}
                        >
                          {option}
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
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
          {timingLabel && (
            <p className="mt-3 rounded-lg border border-lime-200 bg-lime-50 px-2.5 py-2 text-xs font-medium text-lime-800">
              {timingLabel}
            </p>
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
          <div className="flex min-h-11 items-start gap-3 text-sm text-zinc-700">
            <Checkbox
              id="creator-auto-iteration-opt-in"
              className="mt-1 h-5 w-5"
              checked={autoOptIn}
              onCheckedChange={(checked) => setAutoOptIn(checked === true)}
            />
            <label htmlFor="creator-auto-iteration-opt-in" className="cursor-pointer leading-5">
              Allow one automatic revision if the review finds an objective issue.
              <span className="mt-1 block text-xs text-zinc-500">You can still review the result before publishing.</span>
            </label>
          </div>
          {autoOptIn && (
            <Button type="button" size="sm" className="mt-3" disabled={autoSending} onClick={() => void optIntoAutoIteration()}>
              {autoSending ? "Saving…" : "Confirm automatic revision"}
            </Button>
          )}
        </div>
      )}

      {error && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-2">
          <p className="text-sm text-zinc-700" role="alert">{error}</p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={sending}
            onClick={() => {
              setSending(true);
              void refresh().catch(() => null).finally(() => setSending(false));
            }}
          >
            Retry
          </Button>
        </div>
      )}
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
  if (review.status === "pending" || review.status === "queued" || review.status === "running") {
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
