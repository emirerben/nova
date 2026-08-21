"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { useCallback, useEffect, useRef, useState } from "react";

import SignInPrompt from "@/app/plan/_components/SignInPrompt";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { LightShell } from "@/components/ui/LightShell";
import {
  attachClips,
  createOrResumeManualDraft,
  getPlanItemFresh,
  initializeManualDraft,
  requestUploadUrls,
  uploadContentTypeForFile,
  uploadToGcs,
  type ManualDraftMedia,
  type ManualDraftResponse,
} from "@/lib/plan-api";

const MANUAL_VARIANT_ID = "original_text";
const MAX_MANUAL_MEDIA = 20;
const IMAGE_EXTENSIONS = [".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"];
const PHOTO_DRAFT_MESSAGE =
  "Photo timelines are not available in the manual editor yet. Use Make a video with Kria for photos, or choose videos only.";

type DraftUpload = ManualDraftMedia & {
  name: string;
  order: number;
};

type PendingUpload = {
  file: File;
  order: number;
};

function isImageName(name: string): boolean {
  const clean = name.split("?", 1)[0].toLowerCase();
  return IMAGE_EXTENSIONS.some((extension) => clean.endsWith(extension));
}

function filenameFromPath(path: string): string {
  const encoded = path.split("?", 1)[0].split("/").pop() || "Uploaded media";
  try {
    return decodeURIComponent(encoded);
  } catch {
    return encoded;
  }
}

function fallbackMedia(path: string, order: number): DraftUpload {
  const kind = isImageName(path) ? "image" : "video";
  return {
    gcs_path: path,
    kind,
    duration_s: kind === "image" ? 3 : 5,
    name: filenameFromPath(path),
    order,
  };
}

function inspectMedia(file: File): Promise<Pick<ManualDraftMedia, "duration_s" | "kind">> {
  const kind = file.type.startsWith("image/") || isImageName(file.name) ? "image" : "video";
  if (kind === "image") return Promise.resolve({ kind, duration_s: 3 });
  if (typeof document === "undefined" || typeof URL === "undefined" || !URL.createObjectURL) {
    return Promise.resolve({ kind, duration_s: 5 });
  }

  return new Promise((resolve) => {
    const video = document.createElement("video");
    const objectUrl = URL.createObjectURL(file);
    let settled = false;
    const finish = (duration: number) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeoutId);
      URL.revokeObjectURL(objectUrl);
      resolve({
        kind,
        duration_s: Math.min(60, Math.max(0.1, Number.isFinite(duration) ? duration : 5)),
      });
    };
    const timeoutId = window.setTimeout(() => finish(5), 3_000);
    video.preload = "metadata";
    video.onloadedmetadata = () => finish(video.duration);
    video.onerror = () => finish(5);
    video.src = objectUrl;
  });
}

function editorPath(draft: ManualDraftResponse): string {
  return `/plan/items/${encodeURIComponent(draft.plan_item_id)}/edit?variant=${encodeURIComponent(
    draft.variant_id || MANUAL_VARIANT_ID,
  )}`;
}

export default function ManualCreatePage() {
  const router = useRouter();
  const { status: authStatus } = useSession();
  const manualEditorEnabled = process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED === "true";
  const [draft, setDraft] = useState<ManualDraftResponse | null>(null);
  const [uploads, setUploads] = useState<DraftUpload[]>([]);
  const uploadsRef = useRef<DraftUpload[]>([]);
  const [failedUploads, setFailedUploads] = useState<PendingUpload[]>([]);
  const [loadingDraft, setLoadingDraft] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [openingEditor, setOpeningEditor] = useState(false);
  const [removingOrder, setRemovingOrder] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const nextOrderRef = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const replaceWithEditor = useCallback(
    (response: ManualDraftResponse) => {
      setOpeningEditor(true);
      router.replace(editorPath(response));
    },
    [router],
  );

  useEffect(() => {
    if (!manualEditorEnabled) {
      router.replace("/plan");
      return;
    }
    if (authStatus !== "authenticated") {
      setLoadingDraft(authStatus === "loading");
      return;
    }

    let active = true;
    setLoadingDraft(true);
    setError(null);
    void createOrResumeManualDraft()
      .then(async (response) => {
        if (!active) return;
        setDraft(response);
        if (response.variant_id) {
          replaceWithEditor(response);
          return;
        }
        const item = await getPlanItemFresh(response.plan_item_id);
        if (!active) return;
        const resumed = (item.clip_gcs_paths || []).map(fallbackMedia);
        uploadsRef.current = resumed;
        setUploads(resumed);
        nextOrderRef.current = resumed.length;
      })
      .catch((cause) => {
        if (!active) return;
        setError(
          cause instanceof Error
            ? cause.message
            : "Kria couldn’t open your manual draft.",
        );
      })
      .finally(() => {
        if (active) setLoadingDraft(false);
      });
    return () => {
      active = false;
    };
  }, [authStatus, manualEditorEnabled, replaceWithEditor, router]);

  const persistUploads = useCallback(
    async (next: DraftUpload[]) => {
      if (!draft) throw new Error("Your draft is still loading.");
      const ordered = [...next].sort((a, b) => a.order - b.order);
      await attachClips(
        draft.plan_item_id,
        ordered.map((upload) => upload.gcs_path),
      );
      uploadsRef.current = ordered;
      setUploads(ordered);
    },
    [draft],
  );

  const handleFiles = useCallback(
    async (input: FileList | readonly File[] | readonly PendingUpload[] | null) => {
      if (!draft || !input || input.length === 0 || uploading || openingEditor) return;
      const raw = Array.from(input as ArrayLike<File | PendingUpload>);
      if (raw.some((entry) => isImageName(entry instanceof File ? entry.name : entry.file.name))) {
        setError(PHOTO_DRAFT_MESSAGE);
        return;
      }
      if (uploadsRef.current.length + raw.length > MAX_MANUAL_MEDIA) {
        setError(`A manual draft can use up to ${MAX_MANUAL_MEDIA} videos.`);
        return;
      }
      const selected = raw.map((entry) =>
        entry instanceof File ? { file: entry, order: nextOrderRef.current++ } : entry,
      );
      setUploading(true);
      setError(null);
      setFailedUploads([]);
      try {
        const targets = await requestUploadUrls(
          draft.plan_item_id,
          selected.map(({ file }) => ({
            filename: file.name,
            content_type: uploadContentTypeForFile(file),
            file_size_bytes: file.size,
          })),
        );
        const settled = await Promise.allSettled(
          selected.map(async ({ file, order }, index) => {
            const target = targets[index];
            if (!target) throw new Error("Kria did not reserve an upload slot.");
            const [, media] = await Promise.all([
              uploadToGcs(target.upload_url, file),
              inspectMedia(file),
            ]);
            return {
              ...media,
              gcs_path: target.gcs_path,
              name: file.name,
              order,
            } satisfies DraftUpload;
          }),
        );
        const completed = settled.flatMap((result) =>
          result.status === "fulfilled" ? [result.value] : [],
        );
        const failed = settled.flatMap((result, index) =>
          result.status === "rejected" ? [selected[index]] : [],
        );
        if (completed.length > 0) {
          const combined = [...uploadsRef.current, ...completed].sort(
            (a, b) => a.order - b.order,
          );
          // Keep successful PUTs in the browser even if the small metadata
          // attach request is interrupted. Continue retries only that request;
          // it never uploads the same large media bytes again.
          uploadsRef.current = combined;
          setUploads(combined);
          // Persist after every completed batch. The next visit can resume from
          // PlanItem.clip_gcs_paths even if the browser refreshes before init.
          try {
            await attachClips(
              draft.plan_item_id,
              combined.map((upload) => upload.gcs_path),
            );
          } catch (cause) {
            setFailedUploads(failed);
            setError(
              cause instanceof Error
                ? `Your files uploaded, but Kria couldn’t save their order. ${cause.message}`
                : "Your files uploaded, but Kria couldn’t save their order. Continue to retry.",
            );
            return;
          }
        }
        setFailedUploads(failed);
        if (failed.length > 0) {
          setError(
            `${completed.length} uploaded · ${failed.length} didn’t upload. Retry only the failed files.`,
          );
        }
      } catch (cause) {
        setFailedUploads(selected);
        setError(cause instanceof Error ? cause.message : "Your media couldn’t be uploaded.");
      } finally {
        setUploading(false);
      }
    },
    [draft, openingEditor, uploading],
  );

  const removeUpload = useCallback(
    async (upload: DraftUpload) => {
      if (!draft || uploading || openingEditor) return;
      setRemovingOrder(upload.order);
      setError(null);
      try {
        await persistUploads(
          uploadsRef.current.filter((candidate) => candidate.order !== upload.order),
        );
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Kria couldn’t remove that file.");
      } finally {
        setRemovingOrder(null);
      }
    },
    [draft, openingEditor, persistUploads, uploading],
  );

  const openEditor = useCallback(async () => {
    if (!draft || uploadsRef.current.length === 0 || uploading || openingEditor) return;
    if (uploadsRef.current.some((upload) => upload.kind === "image")) {
      setError(PHOTO_DRAFT_MESSAGE);
      return;
    }
    setOpeningEditor(true);
    setError(null);
    try {
      const ordered = [...uploadsRef.current].sort((a, b) => a.order - b.order);
      // Re-attach on Continue as a recovery path for a prior upload whose final
      // persistence request was interrupted. No media bytes are uploaded again.
      await attachClips(
        draft.plan_item_id,
        ordered.map((upload) => upload.gcs_path),
      );
      const response = await initializeManualDraft(
        draft.plan_item_id,
        ordered.map(({ gcs_path, duration_s, kind }) => ({ gcs_path, duration_s, kind })),
      );
      replaceWithEditor({ ...response, variant_id: response.variant_id || MANUAL_VARIANT_ID });
    } catch (cause) {
      setOpeningEditor(false);
      setError(cause instanceof Error ? cause.message : "Kria couldn’t open the editor.");
    }
  }, [draft, openingEditor, replaceWithEditor, uploading]);

  if (!manualEditorEnabled) return null;

  if (authStatus === "loading" || loadingDraft) {
    return (
      <LightShell size="narrow">
        <div className="motion-safe:animate-pulse space-y-5 py-10" aria-label="Loading manual draft">
          <div className="h-3 w-28 rounded bg-zinc-200" />
          <div className="h-12 w-3/4 rounded bg-zinc-200" />
          <div className="h-44 rounded-xl bg-zinc-100" />
        </div>
      </LightShell>
    );
  }

  if (authStatus === "unauthenticated") {
    return (
      <LightShell size="narrow">
        <SignInPrompt
          callbackUrl="/create/manual"
          title="Sign in to edit your footage"
          subtitle="Your manual draft stays connected to your Kria account."
        />
      </LightShell>
    );
  }

  return (
    <LightShell size="narrow" className="text-[#0c0c0e]">
      <div className="mb-9">
        <Eyebrow tone="lime" className="mb-3">
          Manual draft
        </Eyebrow>
        <h1 className="font-display text-4xl leading-tight tracking-[-0.02em] sm:text-5xl">
          Build from your footage.
        </h1>
        <p className="mt-4 max-w-xl leading-7 text-[#71717a]">
          Add videos in the order you want them to start. You can trim,
          rearrange, add text, and choose audio in the editor.
        </p>
      </div>

      {error && (
        <div
          role="alert"
          className="mb-5 rounded-lg border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]"
        >
          {error}
        </div>
      )}

      <section aria-labelledby="manual-footage-heading">
        <div className="mb-3 flex items-baseline justify-between gap-4">
          <h2 id="manual-footage-heading" className="text-sm font-semibold">
            Timeline footage
          </h2>
          <span className="text-xs text-[#a1a1aa]">Videos · up to 20</span>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept="video/*"
          multiple
          disabled={!draft || uploading || openingEditor}
          aria-label="Upload timeline footage"
          className="sr-only"
          onChange={(event) => {
            void handleFiles(event.target.files);
            event.target.value = "";
          }}
        />
        <button
          type="button"
          disabled={!draft || uploading || openingEditor}
          onClick={() => inputRef.current?.click()}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            void handleFiles(event.dataTransfer.files);
          }}
          className="flex min-h-[172px] w-full flex-col items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white px-6 py-8 text-center transition-colors hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:cursor-wait disabled:opacity-60"
        >
          <span
            aria-hidden
            className="mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-[#0c0c0e] text-white"
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" strokeLinecap="round" />
            </svg>
          </span>
          <span className="text-sm font-semibold">
            {uploading ? "Uploading and saving…" : "Choose videos"}
          </span>
          <span className="mt-1 text-sm text-[#71717a]">or drop files here</span>
        </button>

        {uploads.length > 0 && (
          <ol className="mt-4 divide-y divide-zinc-100 rounded-xl border border-zinc-200 bg-white px-4">
            {uploads.map((upload, index) => (
              <li key={`${upload.gcs_path}-${upload.order}`} className="flex min-h-[60px] items-center gap-3 py-2.5">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-zinc-100 text-[11px] font-semibold text-[#71717a]">
                  {index + 1}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-[#3f3f46]">{upload.name}</span>
                  <span className="text-[11px] uppercase tracking-wide text-[#a1a1aa]">
                    {upload.kind}
                  </span>
                </span>
                <button
                  type="button"
                  aria-label={`Remove ${upload.name}`}
                  disabled={removingOrder !== null || uploading || openingEditor}
                  onClick={() => void removeUpload(upload)}
                  className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full text-lg text-[#a1a1aa] transition-colors hover:bg-zinc-100 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:opacity-40"
                >
                  {removingOrder === upload.order ? "…" : "×"}
                </button>
              </li>
            ))}
          </ol>
        )}

        {failedUploads.length > 0 && (
          <div className="mt-4 flex flex-col items-start gap-2 text-sm text-[#71717a] sm:flex-row sm:items-center sm:justify-between">
            <span>
              Couldn&apos;t upload {failedUploads.map(({ file }) => file.name).join(", ")}
            </span>
            <button
              type="button"
              disabled={uploading || openingEditor}
              onClick={() => void handleFiles(failedUploads)}
              className="min-h-[44px] rounded-full border border-zinc-300 px-4 py-2 font-medium text-[#3f3f46] hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:opacity-40"
            >
              Retry failed files
            </button>
          </div>
        )}
      </section>

      <div className="sticky bottom-0 -mx-6 mt-8 border-t border-zinc-200 bg-[#fafaf8]/95 px-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] pt-4 backdrop-blur">
        <button
          type="button"
          onClick={() => void openEditor()}
          disabled={uploads.length === 0 || uploading || openingEditor || !draft}
          className="inline-flex min-h-[48px] w-full items-center justify-center rounded-full bg-[#0c0c0e] px-7 py-3 text-[15px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {openingEditor ? "Opening editor…" : "Continue to editor"}
        </button>
        <p className="mt-2 text-center text-xs text-[#a1a1aa]">
          Your order is saved. The first export creates the finished video.
        </p>
      </div>

      <Link
        href="/plan"
        className="mt-6 inline-flex min-h-[44px] items-center text-sm text-[#71717a] underline-offset-4 hover:text-[#0c0c0e] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e]"
      >
        Back to Create
      </Link>
    </LightShell>
  );
}
