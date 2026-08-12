"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { BeamLoader } from "@/components/progress";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import type { TikTokConnection, TikTokPublication } from "@/lib/tiktok-api";

export function TikTokReleaseRail({
  connection,
  publication,
  publications,
  comparisonPublications,
  receiptState = "ready",
  pollingStalled = false,
  videoReady = true,
  comparisonAvailable = true,
  canPublish,
  baking,
  editHref,
  durationSeconds,
  variantLabel,
  captionPreview,
  onPublish,
  onDownload,
  onConnect,
  onReceiptRetry,
  simulation = false,
}: {
  connection: TikTokConnection | null;
  publication: TikTokPublication | null;
  publications: TikTokPublication[];
  comparisonPublications?: TikTokPublication[];
  receiptState?: "loading" | "ready" | "error";
  pollingStalled?: boolean;
  videoReady?: boolean;
  comparisonAvailable?: boolean;
  canPublish: boolean;
  baking: boolean;
  editHref: string | null;
  durationSeconds: number | null;
  variantLabel: string;
  captionPreview?: string | null;
  onPublish: () => void;
  onDownload: () => void;
  onConnect?: () => void;
  onReceiptRetry?: () => void;
  simulation?: boolean;
}) {
  const [historyOpen, setHistoryOpen] = useState(false);
  const closeHistory = useCallback(() => setHistoryOpen(false), []);

  return (
    <aside className="min-w-0" aria-label="Release desk">
      <div>
        {publication ? (
          <>
            <PublicationReceipt
              publication={publication}
              connection={connection}
              onHistory={() => setHistoryOpen(true)}
              historyCount={publications.length}
              pollingStalled={pollingStalled}
              onReceiptRetry={onReceiptRetry}
              simulation={simulation}
              onDownload={onDownload}
              downloadDisabled={baking || !videoReady}
            />
            {publication.processing_status === "failed" && !publication.retryable && canPublish && (
              <button
                type="button"
                onClick={onPublish}
                disabled={baking}
                className="mt-4 min-h-12 w-full rounded-full bg-[#0c0c0e] px-5 text-sm font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {baking ? "Preparing your video…" : "Review and try again"}
              </button>
            )}
            {publication.visibility_status === "public" && (
              <div className="mt-8 border-t border-zinc-200 pt-8">
                <InsightsPane
                  publication={publication}
                  publications={comparisonPublications ?? publications}
                  comparisonAvailable={comparisonAvailable}
                />
              </div>
            )}
          </>
        ) : (
          <ReleasePreparationPane
            connection={connection}
            canPublish={canPublish}
            videoReady={videoReady}
            baking={baking}
            receiptState={receiptState}
            editHref={editHref}
            durationSeconds={durationSeconds}
            variantLabel={variantLabel}
            captionPreview={captionPreview}
            onPublish={onPublish}
            onDownload={onDownload}
            onConnect={onConnect}
            onReceiptRetry={onReceiptRetry}
            simulation={simulation}
          />
        )}
      </div>

      {historyOpen && (
        <TikTokHistory publications={publications} onClose={closeHistory} />
      )}
    </aside>
  );
}

function ReleasePreparationPane({
  connection,
  canPublish,
  baking,
  videoReady,
  receiptState,
  editHref,
  durationSeconds,
  variantLabel,
  captionPreview,
  onPublish,
  onDownload,
  onConnect,
  onReceiptRetry,
  simulation,
}: {
  connection: TikTokConnection | null;
  canPublish: boolean;
  baking: boolean;
  videoReady: boolean;
  receiptState: "loading" | "ready" | "error";
  editHref: string | null;
  durationSeconds: number | null;
  variantLabel: string;
  captionPreview?: string | null;
  onPublish: () => void;
  onDownload: () => void;
  onConnect?: () => void;
  onReceiptRetry?: () => void;
  simulation: boolean;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const hasContentPostingAccess = Boolean(
    connection?.can_publish || connection?.can_upload_draft,
  );

  return (
    <div className="space-y-4 lg:space-y-7">
      <div className="flex items-center justify-between gap-4">
        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">Release</p>
        {simulation && <p className="text-[11px] font-medium text-[#71717a]">Local preview</p>}
      </div>
      <div className="hidden gap-3 lg:flex">
        {videoReady && <StatusMark />}
        <div>
          <h2 className="text-base font-semibold text-[#0c0c0e]">
            {videoReady ? "Ready to publish" : "Video isn't ready yet"}
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-[#3f3f46]">
            {videoReady
              ? "Your final 9:16 video is rendered and ready for release."
              : "Publishing unlocks after the current render finishes successfully."}
          </p>
        </div>
      </div>

      <div className="hidden divide-y divide-zinc-200 border-y border-zinc-200 text-sm lg:block">
        <MetaRow label="Version" value={variantLabel} />
        <MetaRow label="Duration" value={formatDuration(durationSeconds)} />
        <MetaRow label="Format" value={videoReady ? "9:16 · H.264" : "Waiting for render"} />
      </div>

      {receiptState === "loading" && (
        <BeamLoader tone="light" mode="line" strength="medium" ariaLabel="Checking TikTok publishing history">
          <p role="status" className="py-3 text-sm text-[#3f3f46]">Checking TikTok publishing history…</p>
        </BeamLoader>
      )}

      {receiptState === "error" && (
        <div className="border-l-2 border-zinc-300 pl-3 text-sm text-[#3f3f46]">
          <p>Kria couldn&apos;t confirm whether this video was already sent to TikTok. Publishing stays paused to prevent a duplicate.</p>
          {onReceiptRetry && (
            <button type="button" onClick={onReceiptRetry} className="mt-2 min-h-11 font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
              Check again
            </button>
          )}
        </div>
      )}

      {receiptState === "ready" && (
        connection?.connected ? (
          <div className="space-y-4">
            <AccountBlock connection={connection} nickname={connection.account?.display_name ?? "TikTok"} />
            {captionPreview && (
              <div className="border-y border-zinc-200 py-3">
                <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[#71717a]">Caption</p>
                <p className="mt-1 line-clamp-3 text-sm leading-relaxed text-[#3f3f46]">{captionPreview}</p>
              </div>
            )}
            {simulation && (
              <p className="border-l-2 border-zinc-300 pl-3 text-xs leading-relaxed text-[#71717a]">
                Connected-state preview. Nothing will be sent to TikTok.
              </p>
            )}
            {!canPublish && !hasContentPostingAccess && (
              <div className="border-l-2 border-zinc-300 pl-3 text-sm text-[#3f3f46]">
                <p>TikTok publishing access needs to be reconnected.</p>
                {onConnect ? (
                  <button type="button" onClick={onConnect} className="mt-2 min-h-11 font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
                    Reconnect TikTok here
                  </button>
                ) : (
                  <Link href="/library" className="mt-2 inline-flex min-h-11 items-center font-medium text-lime-700 underline underline-offset-4">
                    Reconnect TikTok
                  </Link>
                )}
              </div>
            )}
            {!canPublish && hasContentPostingAccess && !videoReady && (
              <p className="border-l-2 border-zinc-300 pl-3 text-sm text-[#3f3f46]">
                Publishing unlocks after this video finishes rendering successfully.
              </p>
            )}
          </div>
        ) : (
          <div className="border-l-2 border-zinc-300 pl-3 text-sm text-[#3f3f46]">
            <p>Connect TikTok before publishing.</p>
            {onConnect ? (
              <button type="button" onClick={onConnect} className="mt-2 min-h-11 font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
                Connect TikTok here
              </button>
            ) : (
              <Link href="/library" className="mt-2 inline-flex min-h-11 items-center font-medium text-lime-700 underline underline-offset-4">
                Connect TikTok
              </Link>
            )}
          </div>
        )
      )}

      {receiptState === "ready" && canPublish && (
        <button
          type="button"
          onClick={onPublish}
          disabled={baking}
          className="min-h-[52px] w-full rounded-full bg-[#0c0c0e] px-5 py-3.5 text-sm font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {baking ? "Preparing your video…" : "Publish to TikTok"}
        </button>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
        {editHref && (
          <Link
            href={editHref}
            className="inline-flex min-h-12 items-center justify-center rounded-full border border-zinc-300 bg-white px-4 text-sm font-semibold text-[#0c0c0e] transition-colors hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
          >
            Edit video
          </Link>
        )}
        <div className="relative justify-self-end">
          <button
            type="button"
            aria-label="More video actions"
            aria-expanded={moreOpen}
            onClick={() => setMoreOpen((open) => !open)}
            disabled={baking || !videoReady}
            className="flex h-12 w-12 items-center justify-center rounded-full border border-zinc-300 bg-white text-lg font-semibold tracking-[0.12em] text-[#0c0c0e] transition-colors hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            ···
          </button>
          {moreOpen && videoReady && !baking && (
            <div className="absolute right-0 top-14 z-20 min-w-40 rounded-xl border border-zinc-200 bg-white p-1.5 shadow-[0_16px_40px_rgba(0,0,0,0.12)]">
              <button
                type="button"
                onClick={() => {
                  setMoreOpen(false);
                  onDownload();
                }}
                disabled={baking || !videoReady}
                className="min-h-11 w-full rounded-lg px-3 text-left text-sm font-medium text-[#0c0c0e] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-600 disabled:opacity-50"
              >
                {baking ? "Preparing…" : videoReady ? "Download video" : "Video not ready"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function PublicationReceipt({
  publication,
  connection,
  onHistory,
  historyCount,
  pollingStalled = false,
  onReceiptRetry,
  simulation = false,
  onDownload,
  downloadDisabled = false,
}: {
  publication: TikTokPublication;
  connection: TikTokConnection | null;
  onHistory: () => void;
  historyCount: number;
  pollingStalled?: boolean;
  onReceiptRetry?: () => void;
  simulation?: boolean;
  onDownload?: () => void;
  downloadDisabled?: boolean;
}) {
  const status = simulation
    ? {
        title: "Publish simulation complete",
        detail: "This is a local connected-state preview. Nothing was sent to TikTok.",
        short: "Preview",
      }
    : publicationStatus(publication);
  const isDraftPosted = publication.delivery_mode === "draft_upload" &&
    publication.processing_status === "complete" &&
    publication.visibility_status === "unknown";
  const isWorking = !simulation && !isDraftPosted &&
    !["draft", "public", "private", "removed"].includes(publication.visibility_status) &&
    publication.processing_status !== "failed" &&
    publication.processing_status !== "submission_unknown";

  const content = (
    <div className="space-y-5 border-y border-zinc-200 py-5">
      <div>
        <p className="font-display text-2xl text-[#0c0c0e]">{status.title}</p>
        <p className="mt-1 text-sm text-[#71717a]">{status.detail}</p>
      </div>
      <AccountBlock
        connection={connection}
        nickname={publication.creator_nickname ?? connection?.account?.display_name ?? "TikTok"}
        timestamp={publication.public_at ?? publication.created_at}
        timestampLabel={simulation ? "Simulated" : publication.public_at ? "Published" : "Submitted"}
      />
      {publication.title && (
        <div className="border-y border-zinc-200 py-4">
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-[#3f3f46]">{publication.title}</p>
        </div>
      )}
      <div className="space-y-2 text-sm">
        {publication.delivery_mode === "draft_upload" ? (
          <>
            <MetaRow label="Destination" value="TikTok app inbox" compact />
            <MetaRow label="Next step" value="Finish it in the TikTok app" compact />
          </>
        ) : (
          <>
            <MetaRow label="Privacy" value={privacyLabel(publication.privacy_level)} compact />
            <MetaRow label="Interactions" value={interactionLabel(publication)} compact />
          </>
        )}
      </div>
      {publication.visibility_status === "draft" && !simulation && (
        <div className="border-t border-zinc-200 pt-4">
          <p className="text-sm font-semibold text-[#0c0c0e]">Where to find it</p>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm leading-relaxed text-[#3f3f46]">
            <li>Open the TikTok app on your phone.</li>
            <li>Tap <span className="font-medium">Inbox</span> at the bottom.</li>
            <li>Tap the notification about your uploaded video — it opens TikTok&apos;s editor.</li>
            <li>Add anything you want, then post.</li>
          </ol>
          <p className="mt-3 text-sm leading-relaxed text-[#71717a]">
            This only works in the TikTok mobile app. It will not appear on tiktok.com in a
            desktop browser, and it will not appear under Profile → Drafts.
          </p>
          {onDownload && (
            <p className="mt-3 text-sm leading-relaxed text-[#71717a]">
              No notification?{" "}
              <button
                type="button"
                onClick={onDownload}
                disabled={downloadDisabled}
                className="font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {downloadDisabled ? "Preparing…" : "Download the video"}
              </button>{" "}
              and post it from the TikTok app yourself.
            </p>
          )}
          {publication.tiktok_publish_id && (
            <p className="mt-4 text-xs text-[#a1a1aa]">
              TikTok reference: <span className="font-mono">{publication.tiktok_publish_id}</span>
            </p>
          )}
        </div>
      )}
      {pollingStalled && !simulation && (
        <div className="border-l-2 border-zinc-300 pl-3 text-sm text-[#3f3f46]">
          <p>Kria can&apos;t refresh this receipt right now. Your last confirmed TikTok status is still shown.</p>
          {onReceiptRetry && (
            <button type="button" onClick={onReceiptRetry} className="mt-2 min-h-11 font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
              Check status again
            </button>
          )}
        </div>
      )}
      {!simulation && publication.processing_status === "submission_unknown" && (
        <div className="flex flex-wrap gap-4 border-t border-zinc-200 pt-4">
          <a
            href="https://www.tiktok.com/"
            target="_blank"
            rel="noreferrer"
            className="inline-flex min-h-11 items-center font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
          >
            Open TikTok
          </a>
          {onReceiptRetry && (
            <button type="button" onClick={onReceiptRetry} className="min-h-11 font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
              Check status again
            </button>
          )}
        </div>
      )}
      {historyCount > 0 && (
        <button
          type="button"
          onClick={onHistory}
          className="min-h-11 text-left text-sm font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
        >
          {simulation ? "Preview history" : "TikTok history"} ({historyCount})
        </button>
      )}
    </div>
  );

  if (!isWorking) return content;
  return (
    <BeamLoader tone="light" mode="frame" strength="medium" ariaLabel={status.title}>
      {content}
    </BeamLoader>
  );
}

function InsightsPane({
  publication,
  publications,
  comparisonAvailable,
}: {
  publication: TikTokPublication;
  publications: TikTokPublication[];
  comparisonAvailable: boolean;
}) {
  const metrics = publication.evaluation_metrics;
  if (!metrics) {
    return (
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">Performance</p>
        <p className="mt-3 font-display text-2xl text-[#0c0c0e]">Insights pending</p>
        <p className="mt-2 text-sm leading-relaxed text-[#3f3f46]">
          This post is live. Kria will freeze its first comparable result after 72–84 hours, then show what changed.
        </p>
        <Metrics metrics={publication.latest_metrics} />
      </div>
    );
  }

  const comparison = comparisonAvailable
    ? comparisonCopy(publication, publications)
    : "Comparison history is unavailable right now. This post's frozen result is still shown below.";
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">Performance</p>
      <p className="mt-3 font-display text-2xl text-[#0c0c0e]">What changed after 72 hours</p>
      <p className="mt-3 text-sm leading-relaxed text-[#3f3f46]">{comparison}</p>
      <Metrics metrics={metrics} />
      <div className="mt-6 divide-y divide-zinc-200 border-y border-zinc-200 text-sm">
        <MetaRow label="Time window" value={`${metrics.window_hours ?? 72} hours after publish`} />
        <MetaRow label="Source" value="TikTok" />
        <MetaRow
          label="Last synced"
          value={formatRelativeDate(publication.evaluation_captured_at ?? publication.updated_at)}
        />
      </div>
    </div>
  );
}

function Metrics({ metrics }: { metrics: Record<string, number | null> | null }) {
  if (!metrics) return null;
  return (
    <dl className="mt-6 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-zinc-200 bg-zinc-200">
      {[
        ["Views", metrics.view_count],
        ["Likes", metrics.like_count],
        ["Comments", metrics.comment_count],
        ["Shares", metrics.share_count],
      ].map(([label, value]) => (
        <div key={String(label)} className="bg-white p-3">
          <dt className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[#71717a]">{label}</dt>
          <dd className="mt-1 font-display text-2xl text-[#0c0c0e]">
            {value == null ? (
              <span aria-label="Not reported yet">—</span>
            ) : (
              formatMetric(value as number)
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function TikTokHistory({ publications, onClose }: { publications: TikTokPublication[]; onClose: () => void }) {
  const sheetRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useFocusTrap(sheetRef, true);

  useEffect(() => {
    closeRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[120] bg-black/20" role="dialog" aria-modal="true" aria-labelledby="tiktok-history-title">
      <button type="button" aria-label="Close TikTok history" onClick={onClose} tabIndex={-1} className="absolute inset-0 hidden cursor-default md:block" />
      <section ref={sheetRef} className="absolute inset-0 ml-auto flex w-full flex-col border-l border-zinc-200 bg-[#fafaf8] shadow-[-24px_0_60px_rgba(0,0,0,0.08)] md:w-[480px]">
        <header className="flex min-h-[86px] items-center justify-between border-b border-zinc-200 px-5 md:px-7">
          <h2 id="tiktok-history-title" className="font-display text-2xl text-[#0c0c0e]">TikTok history ({publications.length})</h2>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close" className="flex h-11 w-11 items-center justify-center rounded-full text-xl text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">×</button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-12 md:px-7">
          {publications.map((publication) => {
            const status = publicationStatus(publication);
            return (
              <article key={publication.id} className="border-b border-zinc-200 py-5">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="text-sm font-medium text-[#0c0c0e]">{formatDate(publication.created_at)}</p>
                    <p className="mt-1 text-xs text-[#71717a]">{privacyLabel(publication.privacy_level)}</p>
                  </div>
                  <p className={`text-sm font-medium ${publication.visibility_status === "public" ? "text-lime-700" : publication.processing_status === "failed" ? "text-red-700" : "text-[#3f3f46]"}`}>
                    {status.short}
                  </p>
                </div>
                {publication.title && <p className="mt-4 line-clamp-2 text-sm leading-relaxed text-[#3f3f46]">{publication.title}</p>}
              </article>
            );
          })}
          <p className="pt-8 text-center text-xs text-[#71717a]">All times shown in your local time.</p>
        </div>
      </section>
    </div>
  );
}

function AccountBlock({
  connection,
  nickname,
  timestamp,
  timestampLabel = "Published",
}: {
  connection: TikTokConnection | null;
  nickname: string;
  timestamp?: string;
  timestampLabel?: "Published" | "Submitted" | "Simulated";
}) {
  const avatar = connection?.account?.avatar_url ?? null;
  return (
    <div className="flex items-center gap-3">
      <span className="flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-full bg-[#ead4c6] font-semibold text-[#6b4231]">
        {avatar ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={avatar} alt="" className="h-full w-full object-cover" />
        ) : (
          nickname.trim().charAt(0).toUpperCase() || "T"
        )}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold text-[#0c0c0e]">{nickname}</span>
        <span className="block truncate text-xs text-[#71717a]">
          {timestamp ? `${timestampLabel} ${formatDate(timestamp)}` : "Connected TikTok account"}
        </span>
      </span>
    </div>
  );
}

function StatusMark() {
  return (
    <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-lime-600 text-lime-700" aria-hidden="true">
      ✓
    </span>
  );
}

function MetaRow({ label, value, compact = false }: { label: string; value: string; compact?: boolean }) {
  return (
    <div className={`flex items-center justify-between gap-4 ${compact ? "py-1" : "min-h-11 py-2"}`}>
      <span className="text-[#71717a]">{label}</span>
      <span className="text-right font-medium text-[#0c0c0e]">{value}</span>
    </div>
  );
}

function publicationStatus(publication: TikTokPublication) {
  if (publication.visibility_status === "public") return { title: "Live on TikTok", detail: "TikTok confirmed this post is public.", short: "Live" };
  if (publication.visibility_status === "draft") return { title: "Waiting in your TikTok app inbox", detail: "Open the TikTok app on your phone and tap the notification to finish and post it.", short: "In TikTok inbox" };
  if (publication.delivery_mode === "draft_upload" && publication.processing_status === "complete") return { title: "Posted from TikTok", detail: "The creator completed the post inside TikTok; its audience was chosen there.", short: "Posted" };
  if (publication.visibility_status === "private") return { title: "Published privately", detail: "This post is visible only to the audience you selected.", short: "Private" };
  if (publication.visibility_status === "removed") return { title: "No longer public", detail: "TikTok reports that this post is no longer visible.", short: "Removed" };
  if (publication.processing_status === "submission_unknown") return { title: "Check TikTok before retrying", detail: "TikTok did not confirm whether it received the delivery.", short: "Check TikTok" };
  if (publication.processing_status === "failed") return publication.retryable
    ? { title: "TikTok is retrying", detail: "Kria is retrying this post without creating a duplicate.", short: "Retrying" }
    : { title: "Publishing failed", detail: publication.failure_detail ?? "TikTok could not publish this post.", short: "Failed" };
  if (publication.processing_status === "complete") return { title: "TikTok is reviewing your post", detail: "The upload finished and TikTok moderation is pending.", short: "Reviewing" };
  return { title: "Sending to TikTok", detail: "TikTok is processing your post. This may take a few moments.", short: "Sending" };
}

function comparisonCopy(current: TikTokPublication, publications: TikTokPublication[]) {
  const currentViews = current.evaluation_metrics?.view_count;
  const comparisonViews = publications
    .filter((publication) => {
      const views = publication.evaluation_metrics?.view_count;
      return publication.id !== current.id &&
        publication.visibility_status === "public" &&
        views != null &&
        Number.isFinite(Number(views));
    })
    .slice(0, 5)
    .map((publication) => Number(publication.evaluation_metrics?.view_count ?? 0))
    .sort((a, b) => a - b);
  if (currentViews == null || comparisonViews.length === 0) {
    return "Kria captured this post's first comparable 72-hour result. More published posts will make the comparison stronger.";
  }
  const middle = Math.floor(comparisonViews.length / 2);
  const median = comparisonViews.length % 2
    ? comparisonViews[middle]
    : (comparisonViews[middle - 1] + comparisonViews[middle]) / 2;
  if (median <= 0) return "Kria captured this post's first comparable 72-hour result.";
  const ratio = Number(currentViews) / median;
  if (ratio >= 1) {
    return `This post reached ${ratio.toFixed(1)}× the views of your previous ${comparisonViews.length} public post${comparisonViews.length === 1 ? "" : "s"} in the same 72–84-hour window.`;
  }
  return `This post reached ${Math.round((1 - ratio) * 100)}% fewer views than your previous ${comparisonViews.length} public post${comparisonViews.length === 1 ? "" : "s"} in the same 72–84-hour window.`;
}

function interactionLabel(publication: TikTokPublication) {
  if (
    publication.allow_comment === undefined &&
    publication.allow_duet === undefined &&
    publication.allow_stitch === undefined
  ) {
    return "Saved in TikTok";
  }
  return [
    publication.allow_comment ? "Comments on" : "Comments off",
    publication.allow_duet ? "Duet on" : "Duet off",
    publication.allow_stitch ? "Stitch on" : "Stitch off",
  ].join(" · ");
}

function privacyLabel(value?: string) {
  if (!value) return "TikTok audience";
  if (value === "TIKTOK_DRAFT") return "TikTok app inbox";
  if (value === "SELF_ONLY") return "Only you";
  if (value === "MUTUAL_FOLLOW_FRIENDS") return "Friends";
  if (value === "FOLLOWER_OF_CREATOR") return "Followers";
  if (value === "PUBLIC_TO_EVERYONE") return "Public";
  return value.replaceAll("_", " ").toLowerCase();
}

function formatDuration(seconds: number | null) {
  if (seconds == null) return "Not available";
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60).toString().padStart(2, "0");
  return `${mins}:${secs}`;
}

function formatMetric(value: number) {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

function formatRelativeDate(value: string) {
  const elapsed = Date.now() - new Date(value).getTime();
  const days = Math.max(0, Math.round(elapsed / 86_400_000));
  if (days === 0) return "today";
  if (days === 1) return "1 day ago";
  return `${days} days ago`;
}
