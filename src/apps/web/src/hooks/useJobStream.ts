/**
 * Live template-job status stream: SSE primary, polling fallback.
 *
 * Why: the result page used to poll every 4s, leaving the user staring at an
 * unchanging spinner for up to 4 seconds after every phase transition. SSE
 * delivers phase changes within ~750ms (the worker writes → API tick reads).
 *
 * Fallback rules:
 *   - If `EventSource` is unavailable (rare, old browsers / restricted runtimes)
 *     → use polling at 1500ms.
 *   - If the SSE connection errors out → switch to polling.
 *   - We always do one immediate fetch of /status to render the page without
 *     waiting for the first SSE frame.
 */

import { useEffect, useRef, useState } from "react";
import {
  getTemplateJobStatus,
  getTemplateJobEventsUrl,
  type TemplateJobStatusResponse,
} from "@/lib/api";

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 30 * 60 * 1000; // hard cap: 30 min total

const TERMINAL_STATUSES = new Set([
  "template_ready",
  "music_ready",
  "processing_failed",
  "done",
]);

interface UseJobStreamResult {
  data: TemplateJobStatusResponse | null;
  error: string | null;
  /** True while connected via SSE; false on polling fallback. Surfaced for
   *  debug-only display, not for branching user copy. */
  streaming: boolean;
}

export function useJobStream(jobId: string | null): UseJobStreamResult {
  const [data, setData] = useState<TemplateJobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);

  // Stable refs across renders so cleanup is idempotent.
  const esRef = useRef<EventSource | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const startedAtRef = useRef<number>(0);

  useEffect(() => {
    if (!jobId) return;

    stoppedRef.current = false;
    startedAtRef.current = Date.now();
    let usedSse = false;

    function stop() {
      stoppedRef.current = true;
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      setStreaming(false);
    }

    function applyUpdate(next: TemplateJobStatusResponse) {
      setData(next);
      if (TERMINAL_STATUSES.has(next.status)) {
        stop();
      }
    }

    // Initial snapshot — don't wait for the first SSE frame to paint
    // something. We use the same /status endpoint the legacy poll used, so
    // this is a single HTTP round-trip before the stream takes over.
    getTemplateJobStatus(jobId)
      .then((d) => {
        if (!stoppedRef.current) applyUpdate(d);
      })
      .catch((e: unknown) => {
        if (!stoppedRef.current) {
          setError(e instanceof Error ? e.message : "Failed to fetch status");
        }
      });

    async function pollOnce() {
      if (stoppedRef.current) return;
      if (Date.now() - startedAtRef.current > POLL_TIMEOUT_MS) {
        setError("Processing is taking unusually long. The worker may be down.");
        stop();
        return;
      }
      try {
        const next = await getTemplateJobStatus(jobId!);
        if (stoppedRef.current) return;
        applyUpdate(next);
        if (!TERMINAL_STATUSES.has(next.status)) {
          pollTimerRef.current = setTimeout(pollOnce, POLL_INTERVAL_MS);
        }
      } catch (e) {
        if (stoppedRef.current) return;
        setError(e instanceof Error ? e.message : "Failed to fetch status");
        stop();
      }
    }

    function startPollingFallback() {
      if (stoppedRef.current) return;
      setStreaming(false);
      // Slight delay so we don't immediately re-fetch after the initial
      // snapshot above. The next tick is the next chance for new state.
      pollTimerRef.current = setTimeout(pollOnce, POLL_INTERVAL_MS);
    }

    if (typeof EventSource === "undefined") {
      startPollingFallback();
      return stop;
    }

    try {
      const es = new EventSource(getTemplateJobEventsUrl(jobId));
      esRef.current = es;
      setStreaming(true);

      const handleSnapshot = (evt: MessageEvent) => {
        try {
          const payload = JSON.parse(evt.data) as Partial<TemplateJobStatusResponse>;
          // SSE payload is lean — it doesn't ship the full assembly_plan.
          // Merge over the last known full state to keep the fully-rendered
          // sections (timeline, etc.) intact on intermediate frames.
          setData((prev) => {
            const merged = (prev ? { ...prev, ...payload } : payload) as TemplateJobStatusResponse;
            if (TERMINAL_STATUSES.has(merged.status)) {
              // On `complete`, fetch the full /status once to get the full
              // assembly_plan (output_url, platform_copy, etc.). Without
              // this the result view would render with phase data but no
              // video URL.
              getTemplateJobStatus(jobId!)
                .then((full) => {
                  if (!stoppedRef.current) {
                    setData(full);
                  }
                })
                .catch(() => {})
                .finally(() => stop());
            }
            return merged;
          });
        } catch {
          // Malformed frame — ignore, next frame will catch up.
        }
      };

      es.addEventListener("phase_change", handleSnapshot);
      es.addEventListener("complete", handleSnapshot);

      es.addEventListener("timeout", () => {
        // Server-side max-duration safety. Fall back to polling so the user
        // doesn't get stranded — the job may still finish.
        usedSse = true;
        es.close();
        esRef.current = null;
        startPollingFallback();
      });
      es.addEventListener("error", () => {
        // EventSource auto-reconnects on transient errors, but if the
        // connection is permanently broken (404, CORS, server down) we
        // want to fall back instead of looping silently.
        if (es.readyState === EventSource.CLOSED) {
          if (!usedSse) {
            // Connection never opened — almost certainly a CORS or 404. Go
            // straight to polling instead of giving up.
            esRef.current = null;
            startPollingFallback();
          }
        }
      });
      es.addEventListener("open", () => {
        usedSse = true;
      });
    } catch {
      // Browser threw constructing EventSource — fall back.
      startPollingFallback();
    }

    return stop;
  }, [jobId]);

  return { data, error, streaming };
}
