"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { StableVideo } from "@/components/StableVideo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  adminGetEditFeedback,
  adminListEditFeedback,
  adminSaveEditFeedbackAnnotation,
  adminSaveEditFeedbackAnnotationsBulk,
  type AnnotationRating,
  type EditFeedbackAnnotation,
  type EditFeedbackArtifact,
  type EditFeedbackDetailResponse,
  type EditFeedbackListParams,
} from "@/lib/admin-edit-feedback-api";
import { EditFeedbackTimeline } from "./components/EditFeedbackTimeline";

const DEFAULT_LIMIT = 25;
const RATING_DIMENSIONS = [
  ["overall_quality", "Overall quality"],
  ["ai_guidance_and_response", "Nova guidance and response"],
  ["instruction_fit", "Instruction fit"],
  ["hook", "Hook"],
  ["pacing", "Pacing"],
  ["cuts", "Cuts"],
  ["clip_selection", "Clip selection"],
  ["clip_ordering", "Clip ordering"],
  ["captions", "Captions"],
  ["text", "Text"],
  ["transitions", "Transitions"],
  ["music", "Music"],
  ["audio", "Audio"],
  ["effects", "Effects"],
  ["overlays", "Overlays"],
] as const;

type Filters = Omit<EditFeedbackListParams, "cursor" | "limit">;
const EMPTY_FILTERS: Filters = {
  format: "",
  language: "",
  media_mix: "",
  date_from: "",
  date_to: "",
  prompt_version: "",
  model_version: "",
  review_state: "all",
  quality_signal: "all",
  edit_signal: "all",
  sampling: "stratified",
};

function filtersFromSearch(search: URLSearchParams): Filters {
  const result = { ...EMPTY_FILTERS };
  for (const key of Object.keys(result) as (keyof Filters)[]) {
    const value = search.get(key);
    if (value) result[key] = value as never;
  }
  return result;
}

function display(value: string | null | undefined, fallback = "—") {
  return value || fallback;
}

function mergeFeedbackAnnotations(
  existing: EditFeedbackAnnotation[],
  saved: EditFeedbackAnnotation[],
) {
  return saved.reduce<EditFeedbackAnnotation[]>((annotations, annotation) => [
    ...annotations.map((current) =>
      current.dimension === annotation.dimension
      && current.is_current !== false
      && current.current !== false
        ? { ...current, is_current: false, current: false, superseded_by: annotation.id }
        : current,
    ),
    annotation,
  ], existing);
}

function currentAnnotationDimensions(annotations: EditFeedbackAnnotation[]) {
  return new Set(
    annotations
      .filter((annotation) => annotation.is_current !== false && annotation.current !== false)
      .map((annotation) => annotation.dimension),
  );
}

function evidenceText(record: Record<string, unknown> | null | undefined, ...keys: string[]) {
  if (!record) return null;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
  }
  return null;
}

function evidenceJson(record: Record<string, unknown> | null | undefined, ...keys: string[]) {
  if (!record) return null;
  for (const key of keys) {
    const value = record[key];
    if (value == null) continue;
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value);
    } catch {
      return null;
    }
  }
  return null;
}

export default function EditFeedbackPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [filters, setFilters] = useState<Filters>(() => filtersFromSearch(searchParams));
  const [items, setItems] = useState<EditFeedbackArtifact[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(searchParams.get("cursor"));
  const [selectedId, setSelectedId] = useState<string | null>(searchParams.get("artifact"));
  const [detail, setDetail] = useState<EditFeedbackDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const selectedTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  const filterKey = useMemo(() => JSON.stringify(filters), [filters]);
  const fetchList = useCallback(async (cursor?: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await adminListEditFeedback({ ...filters, limit: DEFAULT_LIMIT, cursor });
      setItems(response.items);
      setNextCursor(response.next_cursor ?? null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load edit feedback.");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void fetchList(searchParams.get("cursor") || undefined);
    // filterKey makes URL-driven filter changes trigger a fresh server query.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    setDetailError(null);
    void adminGetEditFeedback(selectedId)
      .then((response) => {
        if (active) setDetail(response);
      })
      .catch((cause) => {
        if (active) setDetailError(cause instanceof Error ? cause.message : "Unable to load this render.");
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedId]);

  useEffect(() => {
    if (selectedId) closeButtonRef.current?.focus();
  }, [selectedId, detailLoading]);

  const updateUrl = useCallback(
    (next: Filters, cursor?: string | null, artifact?: string | null) => {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(next)) {
        if (value && value !== "all") query.set(key, String(value));
      }
      if (cursor) query.set("cursor", cursor);
      if (artifact) query.set("artifact", artifact);
      const encoded = query.toString();
      router.replace(encoded ? `${pathname}?${encoded}` : pathname, { scroll: false });
    },
    [pathname, router],
  );

  const updateFilter = (key: keyof Filters, value: string) => {
    const next = { ...filters, [key]: value };
    setFilters(next);
    setNextCursor(null);
    updateUrl(next, null, selectedId);
  };

  const selectArtifact = (item: EditFeedbackArtifact, trigger: HTMLButtonElement) => {
    selectedTriggerRef.current = trigger;
    setSelectedId(item.id);
    updateUrl(filters, null, item.id);
  };

  const closeDetail = () => {
    setSelectedId(null);
    setDetail(null);
    updateUrl(filters, nextCursor, null);
    window.setTimeout(() => selectedTriggerRef.current?.focus(), 0);
  };

  const loadNext = () => {
    if (!nextCursor) return;
    setNextCursor(null);
    updateUrl(filters, nextCursor, selectedId);
    void fetchList(nextCursor);
  };

  return (
    <main className="min-w-0 flex-1 bg-black px-4 py-6 text-white sm:px-6 lg:px-8" aria-busy={loading}>
      <div className="mx-auto max-w-[1500px] space-y-6">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-zinc-500">Learning loop</p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">Edit feedback workbench</h1>
            <p className="mt-2 max-w-2xl text-sm text-zinc-400">
              Review exact final renders, inspect the read-only timeline, and append corrections for future evaluations.
            </p>
          </div>
          <Link href="/admin" className="text-sm text-zinc-400 underline-offset-4 hover:text-white hover:underline">
            Back to admin
          </Link>
        </header>

        <FilterBar filters={filters} onChange={updateFilter} />

        <div className="flex items-center justify-between text-sm text-zinc-500" aria-live="polite">
          <span>{loading ? "Loading feedback…" : `${items.length} render${items.length === 1 ? "" : "s"} on this page`}</span>
          {error && <span className="text-red-300">{error}</span>}
        </div>

        {error ? (
          <div role="alert" className="rounded-md border border-red-900/60 bg-red-950/20 p-5 text-sm text-red-200">
            <p>{error}</p>
            <Button className="mt-4" variant="outline" onClick={() => void fetchList()}>
              Retry loading
            </Button>
          </div>
        ) : loading ? (
          <div className="rounded-md border border-zinc-800 bg-zinc-950 p-10 text-center text-sm text-zinc-500" role="status">
            Loading feedback…
          </div>
        ) : items.length === 0 ? (
          <div className="rounded-md border border-dashed border-zinc-800 p-10 text-center text-sm text-zinc-500">
            No renders match these filters.
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border border-zinc-800 bg-zinc-950">
            <div className="hidden grid-cols-[minmax(0,1.7fr)_110px_100px_140px_130px] gap-4 border-b border-zinc-800 px-4 py-3 text-xs uppercase tracking-[0.12em] text-zinc-600 md:grid">
              <span>Render</span><span>Format</span><span>Review</span><span>Created</span><span>Signals</span>
            </div>
            <div className="divide-y divide-zinc-800">
              {items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="grid min-h-16 w-full grid-cols-1 gap-2 px-4 py-4 text-left hover:bg-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-white md:grid-cols-[minmax(0,1.7fr)_110px_100px_140px_130px] md:items-center md:gap-4"
                  onClick={(event) => selectArtifact(item, event.currentTarget)}
                  aria-label={`Review ${item.title || item.id}`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-zinc-100">{display(item.title, item.id)}</span>
                    <span className="mt-1 block truncate text-xs text-zinc-500">{item.duration_s.toFixed(1)}s · {display(item.language)}</span>
                  </span>
                  <span className="text-sm text-zinc-400">{display(item.format)}</span>
                  <span className="text-sm text-zinc-400">{item.review_state.replaceAll("_", " ")}</span>
                  <span className="text-sm text-zinc-500">{new Date(item.created_at).toLocaleDateString()}</span>
                  <span className="text-xs text-zinc-500">{display(item.quality_signal)} / {display(item.edit_signal)}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {nextCursor && !loading && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={loadNext}>Load next page</Button>
          </div>
        )}
      </div>

      {selectedId && (
        <EditFeedbackDetailPanel
          detail={detail}
          loading={detailLoading}
          error={detailError}
          closeButtonRef={closeButtonRef}
          onClose={closeDetail}
          onSaved={(annotations) => {
            const merged = mergeFeedbackAnnotations(detail?.annotations ?? [], annotations);
            const dimensions = currentAnnotationDimensions(merged);
            const reviewState = dimensions.size === 0
              ? "unreviewed"
              : dimensions.size === RATING_DIMENSIONS.length
                ? "reviewed"
                : "needs_correction";
            const currentByDimension = new Map(
              merged
                .filter((annotation) => annotation.is_current !== false && annotation.current !== false)
                .map((annotation) => [annotation.dimension, annotation]),
            );
            setDetail((current) => current ? { ...current, annotations: merged } : current);
            setItems((current) => current.map((item) => item.id === selectedId ? {
              ...item,
              review_state: reviewState,
              quality_signal: currentByDimension.get("overall_quality")?.rating ?? null,
              edit_signal: currentByDimension.get("instruction_fit")?.rating ?? null,
            } : item));
          }}
        />
      )}
    </main>
  );
}

function FilterBar({ filters, onChange }: { filters: Filters; onChange: (key: keyof Filters, value: string) => void }) {
  return (
    <section aria-label="Filter feedback" className="rounded-md border border-zinc-800 bg-zinc-950 p-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
        <label className="text-xs text-zinc-500">Format<input value={filters.format || ""} onChange={(e) => onChange("format", e.target.value)} placeholder="e.g. subtitled" className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white placeholder:text-zinc-700 focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">Language<input value={filters.language || ""} onChange={(e) => onChange("language", e.target.value)} placeholder="e.g. en" className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white placeholder:text-zinc-700 focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">Media mix<input value={filters.media_mix || ""} onChange={(e) => onChange("media_mix", e.target.value)} placeholder="e.g. speech" className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white placeholder:text-zinc-700 focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">Prompt version<input value={filters.prompt_version || ""} onChange={(e) => onChange("prompt_version", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white placeholder:text-zinc-700 focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">Model version<input value={filters.model_version || ""} onChange={(e) => onChange("model_version", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white placeholder:text-zinc-700 focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">From date<input type="date" value={filters.date_from || ""} onChange={(e) => onChange("date_from", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">To date<input type="date" value={filters.date_to || ""} onChange={(e) => onChange("date_to", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white focus:outline-none focus:ring-1 focus:ring-white sm:h-9 sm:text-sm" /></label>
        <label className="text-xs text-zinc-500">Review state<select value={filters.review_state || "all"} onChange={(e) => onChange("review_state", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white sm:h-9 sm:text-sm"><option value="all">All states</option><option value="unreviewed">Unreviewed</option><option value="reviewed">Reviewed</option><option value="needs_correction">Needs correction</option></select></label>
        <label className="text-xs text-zinc-500">Quality signal<select value={filters.quality_signal || "all"} onChange={(e) => onChange("quality_signal", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white sm:h-9 sm:text-sm"><option value="all">All quality</option><option value="good">Good</option><option value="bad">Bad</option><option value="mixed">Mixed</option><option value="not_applicable">Not applicable</option></select></label>
        <label className="text-xs text-zinc-500">Edit signal<select value={filters.edit_signal || "all"} onChange={(e) => onChange("edit_signal", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white sm:h-9 sm:text-sm"><option value="all">All edits</option><option value="good">Good</option><option value="bad">Bad</option><option value="mixed">Mixed</option><option value="not_applicable">Not applicable</option></select></label>
        <label className="text-xs text-zinc-500">Review order<select value={filters.sampling || "stratified"} onChange={(e) => onChange("sampling", e.target.value)} className="mt-1 h-10 w-full rounded-md border border-zinc-700 bg-black px-3 text-base text-white sm:h-9 sm:text-sm"><option value="stratified">Balanced across edit types</option><option value="chronological">Newest first</option></select></label>
      </div>
    </section>
  );
}

function EditFeedbackDetailPanel({
  detail,
  loading,
  error,
  closeButtonRef,
  onClose,
  onSaved,
}: {
  detail: EditFeedbackDetailResponse | null;
  loading: boolean;
  error: string | null;
  closeButtonRef: React.RefObject<HTMLButtonElement>;
  onClose: () => void;
  onSaved: (annotations: EditFeedbackAnnotation[]) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [dimension, setDimension] = useState<string>(RATING_DIMENSIONS[0][0]);
  const [rating, setRating] = useState<AnnotationRating>("good");
  const [rationale, setRationale] = useState("");
  const [frameStart, setFrameStart] = useState("");
  const [frameEnd, setFrameEnd] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [bulkSaving, setBulkSaving] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkStatus, setBulkStatus] = useState<string | null>(null);

  const currentAnnotation = detail?.annotations.find((annotation) =>
    annotation.dimension === dimension && annotation.is_current !== false && annotation.current !== false,
  );
  const ratedDimensions = currentAnnotationDimensions(detail?.annotations ?? []);
  const remainingDimensions = RATING_DIMENSIONS.filter(([key]) => !ratedDimensions.has(key));

  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      setPlaying(true);
      try {
        const playResult = video.play();
        void Promise.resolve(playResult).catch(() => setPlaying(false));
      } catch {
        setPlaying(false);
      }
    } else {
      video.pause();
      setPlaying(false);
    }
  };

  const seek = (seconds: number) => {
    const video = videoRef.current;
    if (video) video.currentTime = seconds;
    setCurrentTime(seconds);
  };

  const onVideoKeyDown = (event: KeyboardEvent<HTMLVideoElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!detail) return;
    setSaving(true);
    setSaveError(null);
    setSaveStatus(null);
    if (rating !== "not_applicable" && !rationale.trim()) {
      setSaveError("Add a rationale for this rating.");
      setSaving(false);
      return;
    }
    try {
      const response = await adminSaveEditFeedbackAnnotation(detail.artifact.id, {
        dimension,
        rating,
        rationale: rationale.trim(),
        frame_start_s: frameStart ? Number(frameStart) : null,
        frame_end_s: frameEnd ? Number(frameEnd) : null,
        supersedes_annotation_id: currentAnnotation?.id ?? null,
      });
      onSaved([response.annotation]);
      setRationale("");
      setFrameStart("");
      setFrameEnd("");
      setSaveStatus("Correction appended.");
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "Unable to save correction.");
    } finally {
      setSaving(false);
    }
  };

  const completeRemainingAsGood = async () => {
    if (!detail || remainingDimensions.length === 0) return;
    setBulkSaving(true);
    setBulkError(null);
    setBulkStatus(null);
    try {
      const response = await adminSaveEditFeedbackAnnotationsBulk(
        detail.artifact.id,
        remainingDimensions.map(([remainingDimension]) => ({
          dimension: remainingDimension,
          rating: "good",
          rationale: "No issue noted in this review pass.",
          frame_start_s: null,
          frame_end_s: null,
          supersedes_annotation_id: null,
        })),
      );
      onSaved(response.annotations);
      setBulkStatus(
        `${response.annotations.length} remaining factor${response.annotations.length === 1 ? "" : "s"} marked good. Review complete.`,
      );
    } catch (cause) {
      setBulkError(cause instanceof Error ? cause.message : "Unable to complete this review.");
    } finally {
      setBulkSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/70" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="flex h-full w-full max-w-3xl flex-col overflow-y-auto border-l border-zinc-800 bg-zinc-950 shadow-2xl" role="dialog" aria-modal="true" aria-busy={loading} aria-labelledby="edit-feedback-detail-title" onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); onClose(); } }}>
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-zinc-800 bg-zinc-950 px-5 py-4">
          <div>
            <h2 id="edit-feedback-detail-title" className="font-semibold">Render detail</h2>
            <p className="text-xs text-zinc-500">Exact final output · read-only playback</p>
          </div>
          <button ref={closeButtonRef} type="button" onClick={onClose} className="min-h-11 rounded-md border border-zinc-700 px-3 text-sm text-zinc-300 hover:bg-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white" aria-label="Close render detail">
            Close
          </button>
        </div>

        {loading && <div className="p-6 text-sm text-zinc-500" role="status">Loading render detail…</div>}
        {error && <div className="m-5 rounded-md border border-red-900/60 bg-red-950/20 p-4 text-sm text-red-200" role="alert">{error}</div>}
        {detail && (
          <div className="space-y-6 p-5">
            <div className="aspect-video overflow-hidden rounded-md border border-zinc-800 bg-black">
              {detail.artifact.playback_url ? (
                <StableVideo
                  ref={videoRef}
                  src={detail.artifact.playback_url}
                  identity={detail.artifact.playback_identity || detail.artifact.render_generation || detail.artifact.id}
                  poster={detail.artifact.poster_url || undefined}
                  controls
                  playsInline
                  className="h-full w-full object-contain"
                  aria-label={`Final render preview for ${display(detail.artifact.title, detail.artifact.id)}`}
                  onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                  onKeyDown={onVideoKeyDown}
                />
              ) : (
                <p className="flex h-full items-center justify-center px-5 text-center text-sm text-zinc-500" role="status">
                  Playback is unavailable for this render.
                </p>
              )}
            </div>
            <dl className="grid gap-2 text-sm text-zinc-400 sm:grid-cols-3">
              <Meta label="Format" value={display(detail.artifact.format)} />
              <Meta label="Language" value={display(detail.artifact.language)} />
              <Meta label="Media mix" value={display(detail.artifact.media_mix)} />
              <Meta label="Prompt" value={display(detail.artifact.prompt_version)} />
              <Meta label="Model" value={display(detail.artifact.model_version)} />
              <Meta label="Review" value={detail.artifact.review_state.replaceAll("_", " ")} />
            </dl>
            <NovaEvidenceCard proposal={detail.proposal} execution={detail.execution_receipt} />
            <EditFeedbackTimeline
              duration={detail.artifact.duration_s}
              events={detail.timeline || detail.artifact.timeline || []}
              currentTime={currentTime}
              playing={playing}
              onSeek={seek}
              onPlayPause={togglePlayback}
            />
            <section aria-labelledby="quick-review-heading" className="rounded-md border border-zinc-700 bg-zinc-900/50 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 id="quick-review-heading" className="font-medium">Fast review</h3>
                <span className="text-xs text-zinc-500">{ratedDimensions.size} of {RATING_DIMENSIONS.length} rated</span>
              </div>
              <p className="mt-1 text-sm text-zinc-400">
                Flag anything that needs work below. When you are done, explicitly mark every unrated factor as good in one step.
              </p>
              {remainingDimensions.length > 0 ? (
                <Button
                  type="button"
                  variant="outline"
                  className="mt-4"
                  disabled={bulkSaving}
                  onClick={() => void completeRemainingAsGood()}
                >
                  {bulkSaving
                    ? "Completing review…"
                    : `Mark ${remainingDimensions.length} remaining factor${remainingDimensions.length === 1 ? "" : "s"} good`}
                </Button>
              ) : (
                <p className="mt-3 text-sm text-emerald-300">All factors are rated. This edit is fully reviewed.</p>
              )}
              {bulkError && <p className="mt-3 text-sm text-red-300" role="alert">{bulkError}</p>}
              {bulkStatus && <p className="mt-3 text-sm text-emerald-300" role="status" aria-live="polite">{bulkStatus}</p>}
            </section>
            <section aria-labelledby="correction-heading" className="border-t border-zinc-800 pt-5">
              <h3 id="correction-heading" className="font-medium">Detailed feedback</h3>
              <p className="mt-1 text-xs text-zinc-500">Rate each factor separately. Corrections are append-only and supersede the current value for the selected factor.</p>
              <nav aria-label="Review factors" className="mt-4 grid gap-2 sm:grid-cols-2">
                {RATING_DIMENSIONS.map(([key, label], index) => {
                  const annotation = detail.annotations.find((item) =>
                    item.dimension === key && item.is_current !== false && item.current !== false,
                  );
                  return (
                    <button
                      key={key}
                      type="button"
                      aria-pressed={dimension === key}
                      aria-label={`Review factor ${index + 1}: ${label}`}
                      onClick={() => setDimension(key)}
                      className={`flex min-h-11 items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${dimension === key ? "border-white bg-zinc-800 text-white" : "border-zinc-800 text-zinc-400 hover:bg-zinc-900"}`}
                    >
                      <span><span className="mr-2 text-xs text-zinc-600">{String(index + 1).padStart(2, "0")}</span>{label}</span>
                      <span className="text-xs capitalize text-zinc-500">{annotation?.rating?.replaceAll("_", " ") || "Unrated"}</span>
                    </button>
                  );
                })}
              </nav>
              <form className="mt-4 space-y-4" onSubmit={save}>
                <p id="selected-factor" className="text-sm text-zinc-300">Selected factor: <span className="font-medium text-white">{RATING_DIMENSIONS.find(([key]) => key === dimension)?.[1] || dimension}</span></p>
                <fieldset>
                  <legend className="text-sm text-zinc-300">Rating</legend>
                  <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">{(["good", "bad", "mixed", "not_applicable"] as AnnotationRating[]).map((value) => <label key={value} className={`flex min-h-11 cursor-pointer items-center justify-center rounded-md border px-2 text-xs capitalize focus-within:ring-2 focus-within:ring-white ${rating === value ? "border-white bg-zinc-800 text-white" : "border-zinc-700 text-zinc-400"}`}><input type="radio" name="rating" value={value} checked={rating === value} onChange={() => setRating(value)} className="sr-only" />{value.replaceAll("_", " ")}</label>)}</div>
                </fieldset>
                <label className="block text-sm text-zinc-300">Rationale<Textarea value={rationale} onChange={(e) => setRationale(e.target.value)} rows={3} placeholder="What should the evaluator learn?" className="mt-1 bg-black" /></label>
                <div className="grid grid-cols-2 gap-3"><label className="text-sm text-zinc-300">Frame start (s)<Input type="number" min="0" step="0.1" value={frameStart} onChange={(e) => setFrameStart(e.target.value)} className="mt-1 bg-black" /></label><label className="text-sm text-zinc-300">Frame end (s)<Input type="number" min="0" step="0.1" value={frameEnd} onChange={(e) => setFrameEnd(e.target.value)} className="mt-1 bg-black" /></label></div>
                {saveError && <p className="text-sm text-red-300" role="alert">{saveError}</p>}
                {saveStatus && <p className="text-sm text-emerald-300" role="status" aria-live="polite">{saveStatus}</p>}
                <Button type="submit" disabled={saving}>{saving ? "Saving…" : currentAnnotation ? "Append correction" : "Save rating"}</Button>
              </form>
            </section>
            <AnnotationHistory annotations={detail.annotations} />
          </div>
        )}
      </aside>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-xs text-zinc-600">{label}</dt><dd>{value}</dd></div>;
}

function NovaEvidenceCard({
  proposal,
  execution,
}: {
  proposal?: Record<string, unknown> | null;
  execution?: Record<string, unknown> | null;
}) {
  const utterance = evidenceText(execution, "utterance", "user_message") || evidenceText(proposal, "utterance", "user_message");
  const modelReply = evidenceText(execution, "model_reply", "reply", "response") || evidenceText(proposal, "model_reply", "reply", "response");
  const intent = evidenceText(execution, "intent", "inferred_intent") || evidenceText(proposal, "intent", "inferred_intent");
  const outcome = evidenceText(execution, "execution_outcome", "outcome", "status", "proposal_outcome") || evidenceText(proposal, "outcome", "status");
  const rejectionReasons = evidenceJson(execution, "rejection_reasons", "rejections");
  const rationale = evidenceText(proposal, "rationale", "reasoning", "goal");
  const direction = evidenceText(proposal, "direction", "edit_direction", "pace");
  const hasEvidence = utterance || modelReply || intent || outcome || rejectionReasons || rationale || direction;
  if (!hasEvidence) {
    return <section aria-label="Nova guidance evidence" className="rounded-md border border-zinc-800 bg-zinc-900/40 p-4"><h3 className="font-medium">Nova guidance evidence</h3><p className="mt-1 text-sm text-zinc-500">No guidance or response receipt was recorded for this render.</p></section>;
  }
  return (
    <section aria-label="Nova guidance evidence" className="rounded-md border border-zinc-800 bg-zinc-900/40 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-medium">Nova guidance evidence</h3>
        <span className="text-xs uppercase tracking-[0.12em] text-zinc-600">Recorded receipt</span>
      </div>
      <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
        {utterance && <Meta label="Creator request" value={utterance} />}
        {modelReply && <Meta label="Nova response" value={modelReply} />}
        {intent && <Meta label="Inferred intent" value={intent} />}
        {outcome && <Meta label="Outcome" value={outcome.replaceAll("_", " ")} />}
        {direction && <Meta label="Direction" value={direction.replaceAll("_", " ")} />}
        {rationale && <Meta label="Proposal rationale" value={rationale} />}
      </dl>
      {rejectionReasons && <div className="mt-3 border-t border-zinc-800 pt-3"><p className="text-xs text-zinc-500">Rejection / caveat reasons</p><p className="mt-1 whitespace-pre-wrap text-sm text-zinc-400">{rejectionReasons}</p></div>}
    </section>
  );
}

function AnnotationHistory({ annotations }: { annotations: EditFeedbackAnnotation[] }) {
  if (annotations.length === 0) return <p className="text-sm text-zinc-600">No annotations yet.</p>;
  return <section aria-label="Annotation history" className="border-t border-zinc-800 pt-5"><h3 className="font-medium">Annotation history</h3><ul className="mt-3 space-y-2">{annotations.slice().reverse().map((annotation) => <li key={annotation.id} className="rounded-md border border-zinc-800 p-3 text-sm"><div className="flex justify-between gap-3"><span className="text-zinc-300">{annotation.dimension.replaceAll("_", " ")}: {annotation.rating.replaceAll("_", " ")}</span><span className="text-xs text-zinc-600">{new Date(annotation.created_at).toLocaleString()}</span></div>{annotation.rationale && <p className="mt-1 text-zinc-500">{annotation.rationale}</p>}</li>)}</ul></section>;
}
