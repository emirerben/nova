"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { BeamLoader } from "@/components/progress";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import {
  createTikTokPublication,
  getTikTokPublishOptions,
  startTikTokOAuth,
  type TikTokPublication,
  type TikTokPublishOptions,
} from "@/lib/tiktok-api";

type PublishStep = "details" | "confirm";
type DeliveryMode = "direct_post" | "draft_upload";

export type TikTokPublishSimulation = {
  creatorNickname: string;
  previewUrl: string;
  durationSeconds: number | null;
};

export function TikTokPublishDialog({
  open,
  jobId,
  variantId,
  videoTitle = "Your video",
  variantLabel = "Original",
  accountAvatarUrl = null,
  simulation = null,
  onClose,
  onPublished,
}: {
  open: boolean;
  jobId: string;
  variantId?: string | null;
  videoTitle?: string;
  variantLabel?: string;
  accountAvatarUrl?: string | null;
  simulation?: TikTokPublishSimulation | null;
  onClose: () => void;
  onPublished?: (publication: TikTokPublication) => void;
}) {
  const simulationEnabled = simulation !== null;
  const simulationCreatorNickname = simulation?.creatorNickname ?? "";
  const simulationPreviewUrl = simulation?.previewUrl ?? "";
  const simulationDurationSeconds = simulation?.durationSeconds ?? null;
  const [options, setOptions] = useState<TikTokPublishOptions | null>(null);
  const [step, setStep] = useState<PublishStep>("details");
  const [deliveryMode, setDeliveryMode] = useState<DeliveryMode>("direct_post");
  const [title, setTitle] = useState("");
  const [privacy, setPrivacy] = useState("");
  const [allowComment, setAllowComment] = useState(false);
  const [allowDuet, setAllowDuet] = useState(false);
  const [allowStitch, setAllowStitch] = useState(false);
  const [commercialContent, setCommercialContent] = useState(false);
  const [brandContent, setBrandContent] = useState(false);
  const [brandOrganic, setBrandOrganic] = useState(false);
  const [isAigc, setIsAigc] = useState(false);
  const [musicConfirmed, setMusicConfirmed] = useState(false);
  const [draftHandoffConfirmed, setDraftHandoffConfirmed] = useState(false);
  const [state, setState] = useState<"loading" | "ready" | "submitting" | "error">(
    "loading",
  );
  const [error, setError] = useState<string | null>(null);
  const [optionsAttempt, setOptionsAttempt] = useState(0);
  const idempotencyKey = useRef(crypto.randomUUID());
  const submissionInFlight = useRef(false);
  const sheetRef = useRef<HTMLElement>(null);
  const initialFocusRef = useRef<HTMLButtonElement>(null);
  const bodyScrollRef = useRef<HTMLDivElement>(null);
  const stepHeadingRef = useRef<HTMLHeadingElement>(null);
  const errorSummaryRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  useFocusTrap(sheetRef, open);

  const storageKeyFor = useCallback((mode: DeliveryMode) => {
    return `tiktok:publish-key:${jobId}:${variantId ?? "default"}:${mode}`;
  }, [jobId, variantId]);

  const restoreIdempotencyKey = useCallback((mode: DeliveryMode) => {
    try {
      const storageKey = storageKeyFor(mode);
      const storedKey = window.sessionStorage.getItem(storageKey);
      idempotencyKey.current = storedKey || crypto.randomUUID();
      if (!storedKey) window.sessionStorage.setItem(storageKey, idempotencyKey.current);
    } catch {
      idempotencyKey.current = crypto.randomUUID();
    }
  }, [storageKeyFor]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setStep("details");
    setDeliveryMode("direct_post");
    setState("loading");
    setError(null);
    setOptions(null);
    setPrivacy("");
    setAllowComment(false);
    setAllowDuet(false);
    setAllowStitch(false);
    setCommercialContent(false);
    setBrandContent(false);
    setBrandOrganic(false);
    setIsAigc(false);
    setMusicConfirmed(false);
    setDraftHandoffConfirmed(false);
    restoreIdempotencyKey("direct_post");
    const optionsPromise = simulationEnabled
      ? Promise.resolve<TikTokPublishOptions>({
          preview_url: simulationPreviewUrl,
          source_revision: "local-preview-source-revision-0001",
          variant_id: variantId ?? null,
          duration_s: simulationDurationSeconds,
          creator_nickname: simulationCreatorNickname,
          privacy_options: [
            "PUBLIC_TO_EVERYONE",
            "MUTUAL_FOLLOW_FRIENDS",
            "FOLLOWER_OF_CREATOR",
            "SELF_ONLY",
          ],
          comment_disabled: false,
          duet_disabled: false,
          stitch_disabled: false,
          max_duration_s: 600,
          suggested_title: videoTitle,
          audited: true,
          consent_version: "local-preview",
          can_direct_post: true,
          can_upload_draft: true,
        })
      : getTikTokPublishOptions(jobId, variantId);
    void optionsPromise
      .then((value) => {
        if (cancelled) return;
        const normalizedValue = {
          ...value,
          // Vercel can briefly run ahead of Fly during the rollout. The old
          // response shape was Direct Post only, so preserve that behavior.
          can_direct_post: value.can_direct_post ?? true,
          can_upload_draft: value.can_upload_draft ?? false,
        };
        setOptions(normalizedValue);
        const availableMode = normalizedValue.can_direct_post ? "direct_post" : "draft_upload";
        setDeliveryMode(availableMode);
        restoreIdempotencyKey(availableMode);
        setTitle(value.suggested_title);
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(
          reason instanceof Error ? reason.message : "TikTok publishing is unavailable",
        );
        setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [
    open,
    jobId,
    variantId,
    simulationEnabled,
    simulationCreatorNickname,
    simulationDurationSeconds,
    simulationPreviewUrl,
    videoTitle,
    optionsAttempt,
    restoreIdempotencyKey,
  ]);

  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement as HTMLElement | null;
    initialFocusRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submissionInFlight.current) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      openerRef.current?.focus();
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open || state !== "ready") return;
    if (bodyScrollRef.current) bodyScrollRef.current.scrollTop = 0;
    stepHeadingRef.current?.focus();
  }, [open, state, step]);

  useEffect(() => {
    if (error) errorSummaryRef.current?.focus();
  }, [error]);

  if (!open || typeof document === "undefined") return null;

  const invalidCommercial = commercialContent && !brandContent && !brandOrganic;
  const invalidPrivateBrand = brandContent && privacy === "SELF_ONLY";
  const canReview = deliveryMode === "draft_upload"
    ? musicConfirmed && draftHandoffConfirmed
    : Boolean(privacy && musicConfirmed && !invalidCommercial && !invalidPrivateBrand);
  const reviewBlocker = deliveryMode === "draft_upload"
    ? !musicConfirmed
      ? "Confirm TikTok's music usage terms."
      : !draftHandoffConfirmed
        ? "Confirm that you'll finish this in the TikTok app."
        : null
    : !privacy
      ? "Choose who can watch this video."
      : invalidCommercial
        ? "Choose what kind of commercial content this is."
        : invalidPrivateBrand
          ? "Branded content cannot use Only you privacy."
          : !musicConfirmed
            ? "Confirm TikTok's music usage terms."
            : null;

  function changeDeliveryMode(value: DeliveryMode) {
    setDeliveryMode(value);
    setStep("details");
    setError(null);
    restoreIdempotencyKey(value);
  }

  async function publish() {
    if (!options || !canReview || submissionInFlight.current) return;
    submissionInFlight.current = true;
    setState("submitting");
    setError(null);
    try {
      const publication = simulationEnabled
        ? simulatedPublication({
            jobId,
            variantId: options.variant_id,
            title: deliveryMode === "draft_upload" ? "" : title,
            privacy: deliveryMode === "draft_upload" ? "TIKTOK_DRAFT" : privacy,
            allowComment,
            allowDuet,
            allowStitch,
            creatorNickname: simulationCreatorNickname,
            deliveryMode,
          })
        : await createTikTokPublication({
            job_id: jobId,
            variant_id: options.variant_id,
            source_revision: options.source_revision,
            idempotency_key: idempotencyKey.current,
            delivery_mode: deliveryMode,
            title: deliveryMode === "draft_upload" ? "" : title,
            privacy_level: deliveryMode === "draft_upload" ? "TIKTOK_DRAFT" : privacy,
            allow_comment: deliveryMode === "direct_post" && allowComment,
            allow_duet: deliveryMode === "direct_post" && allowDuet,
            allow_stitch: deliveryMode === "direct_post" && allowStitch,
            brand_content_toggle: deliveryMode === "direct_post" && brandContent,
            brand_organic_toggle: deliveryMode === "direct_post" && brandOrganic,
            is_aigc: deliveryMode === "direct_post" && isAigc,
            music_usage_confirmed: musicConfirmed,
            draft_handoff_confirmed: deliveryMode === "draft_upload" && draftHandoffConfirmed,
            consent_version: options.consent_version,
          });
      try {
        window.sessionStorage.removeItem(storageKeyFor(deliveryMode));
      } catch {
        // Storage is a resilience aid; the backend idempotency contract remains authoritative.
      }
      submissionInFlight.current = false;
      onPublished?.(publication);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not send this video to TikTok");
      setState("ready");
      submissionInFlight.current = false;
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[100] bg-[#ffffff]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tiktok-publish-title"
    >
      <section
        ref={sheetRef}
        data-testid="tiktok-publish-workspace"
        className="fixed inset-0 flex w-full flex-col bg-[#ffffff]"
      >
        <header className="border-b border-zinc-200 bg-[#ffffff]">
          <div className="mx-auto grid min-h-[72px] w-full max-w-[1280px] grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-4 px-4 md:px-8">
            <button
              ref={initialFocusRef}
              type="button"
              onClick={onClose}
              disabled={state === "submitting"}
              className="inline-flex min-h-11 items-center gap-2 rounded-md px-1 text-sm font-medium text-[#3f3f46] transition-colors hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:opacity-40"
            >
              <span aria-hidden>←</span>
              Exit
            </button>
            <div className="min-w-0 text-center">
              <h2 id="tiktok-publish-title" className="truncate font-display text-xl text-[#0c0c0e] md:text-2xl">
                {simulationEnabled ? "Preview TikTok delivery" : "Send to TikTok"}
              </h2>
              <p className="mt-0.5 text-[11px] font-medium uppercase tracking-[0.16em] text-[#71717a] md:hidden">
                {step === "details" ? "1 of 2 · Details" : "2 of 2 · Confirm"}
              </p>
            </div>
            <StepProgress step={step} />
          </div>
        </header>

        <div
          ref={bodyScrollRef}
          className="min-h-0 flex-1 overflow-y-auto"
          data-testid="tiktok-publish-scroll"
        >
          <div className="mx-auto w-full max-w-[1280px] px-5 py-6 md:px-8 md:py-8">
          {state === "loading" && <PublishLoading />}

          {state === "error" && (
            <div
              ref={errorSummaryRef}
              tabIndex={-1}
              role="alert"
              className="mx-auto max-w-xl py-12 outline-none md:py-20"
            >
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">TikTok settings</p>
              <p className="mt-3 font-display text-3xl text-[#0c0c0e]">TikTok needs your attention</p>
              <p className="mt-3 text-base leading-relaxed text-[#3f3f46]">
                {publishOptionsErrorMessage(error)}
              </p>
              <div className="mt-7 flex flex-wrap gap-3">
                <button
                  type="button"
                  onClick={() => setOptionsAttempt((value) => value + 1)}
                  className="min-h-11 rounded-full bg-[#0c0c0e] px-5 text-sm font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
                >
                  Retry settings
                </button>
                {isTikTokReconnectError(error) && (
                  <button
                    type="button"
                    onClick={() => void startTikTokOAuth(currentReturnTo())}
                    className="min-h-11 rounded-full border border-zinc-300 bg-white px-5 text-sm font-semibold text-[#0c0c0e] transition-colors hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
                  >
                    Reconnect TikTok
                  </button>
                )}
                <button
                  type="button"
                  onClick={onClose}
                  className="min-h-11 px-2 text-sm font-medium text-[#3f3f46] underline decoration-zinc-300 underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
                >
                  Return to item
                </button>
              </div>
            </div>
          )}

          {options && state !== "loading" && state !== "error" && step === "details" && (
            <>
              {simulationEnabled && (
                <p className="mb-5 text-sm text-[#3f3f46]" role="note">
                  <span className="font-semibold text-[#0c0c0e]">Local preview.</span>{" "}
                  No TikTok API request will be made.
                </p>
              )}
              <h3 ref={stepHeadingRef} tabIndex={-1} className="sr-only">TikTok delivery details</h3>
              <DeliveryModePicker
                value={deliveryMode}
                canDirectPost={options.can_direct_post}
                canUploadDraft={options.can_upload_draft}
                onChange={changeDeliveryMode}
              />
              {deliveryMode === "direct_post" ? <DetailsStep
                options={options}
              simulation={simulationEnabled}
              title={title}
              privacy={privacy}
              allowComment={allowComment}
              allowDuet={allowDuet}
              allowStitch={allowStitch}
              commercialContent={commercialContent}
              brandContent={brandContent}
              brandOrganic={brandOrganic}
              isAigc={isAigc}
              musicConfirmed={musicConfirmed}
              invalidCommercial={invalidCommercial}
              invalidPrivateBrand={invalidPrivateBrand}
              accountAvatarUrl={accountAvatarUrl}
              videoTitle={videoTitle}
              variantLabel={variantLabel}
              onTitle={setTitle}
              onPrivacy={setPrivacy}
              onAllowComment={setAllowComment}
              onAllowDuet={setAllowDuet}
              onAllowStitch={setAllowStitch}
              onCommercialContent={(checked) => {
                setCommercialContent(checked);
                if (!checked) {
                  setBrandOrganic(false);
                  setBrandContent(false);
                }
              }}
              onBrandContent={setBrandContent}
              onBrandOrganic={setBrandOrganic}
              onIsAigc={setIsAigc}
                onMusicConfirmed={setMusicConfirmed}
              /> : <DraftDetailsStep
                options={options}
                simulation={simulationEnabled}
                accountAvatarUrl={accountAvatarUrl}
                videoTitle={videoTitle}
                variantLabel={variantLabel}
                musicConfirmed={musicConfirmed}
                handoffConfirmed={draftHandoffConfirmed}
                onMusicConfirmed={setMusicConfirmed}
                onHandoffConfirmed={setDraftHandoffConfirmed}
              />}
            </>
          )}

          {options && state !== "loading" && state !== "error" && step === "confirm" && (
            <>
              <h3 ref={stepHeadingRef} tabIndex={-1} className="sr-only">Confirm TikTok delivery</h3>
              {deliveryMode === "direct_post" ? <ConfirmStep
                options={options}
                simulation={simulationEnabled}
                title={title}
                privacy={privacy}
                allowComment={allowComment}
                allowDuet={allowDuet}
                allowStitch={allowStitch}
                commercialContent={commercialContent}
                brandContent={brandContent}
                brandOrganic={brandOrganic}
                isAigc={isAigc}
                accountAvatarUrl={accountAvatarUrl}
                onEdit={() => setStep("details")}
              /> : <DraftConfirmStep
                options={options}
                simulation={simulationEnabled}
                accountAvatarUrl={accountAvatarUrl}
                onEdit={() => setStep("details")}
              />}
            </>
          )}
          </div>
        </div>

        {options && state !== "loading" && state !== "error" && (
          <footer className="border-t border-zinc-200 bg-[#ffffff]">
            <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-3 px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3 md:px-8 lg:flex-row lg:items-center lg:justify-between lg:gap-8">
              <div className="min-w-0" aria-live="polite">
                {error && (
                  <div ref={errorSummaryRef} tabIndex={-1} role="alert" className="outline-none">
                    <p className="text-sm font-medium text-red-700">{publicationErrorMessage(error)}</p>
                    <p className="mt-0.5 text-xs text-[#71717a]">Your details are still here. Review them and try again.</p>
                  </div>
                )}
                {!error && step === "details" && reviewBlocker && (
                  <p id="tiktok-review-blocker" className="text-sm font-medium text-[#3f3f46]">
                    {reviewBlocker}
                  </p>
                )}
                {!error && (!reviewBlocker || step === "confirm") && (
                  <p className="text-xs leading-relaxed text-[#71717a]">
                    {simulationEnabled
                      ? "Preview only — no post will be sent."
                      : step === "confirm"
                        ? deliveryMode === "draft_upload"
                          ? "TikTok will notify you in the app inbox to finish editing and post."
                          : "Publishing creates a TikTok post. Changes may need to be made in TikTok."
                        : "You will review everything before publishing."}
                  </p>
                )}
              </div>
              <div className="flex w-full gap-3 lg:max-w-[460px] lg:justify-end">
              {step === "details" ? (
                <button
                  type="button"
                  onClick={() => setStep("confirm")}
                  disabled={!canReview}
                  aria-describedby={!canReview ? "tiktok-review-blocker" : undefined}
                  className="min-h-12 w-full rounded-full bg-[#0c0c0e] px-5 text-sm font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-35 lg:max-w-[300px]"
                >
                  Review {deliveryMode === "draft_upload" ? "handoff" : "post"}
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => setStep("details")}
                    disabled={state === "submitting"}
                    className="min-h-12 flex-1 rounded-full border border-zinc-300 bg-white px-5 text-sm font-semibold text-[#0c0c0e] transition-colors hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:opacity-40"
                  >
                    Back to details
                  </button>
                  <button
                    type="button"
                    onClick={() => void publish()}
                    disabled={!canReview || state === "submitting"}
                    className="min-h-12 flex-[1.4] rounded-full bg-[#0c0c0e] px-5 text-sm font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-35"
                  >
                    {state === "submitting"
                      ? simulationEnabled ? "Simulating…" : "Sending to TikTok…"
                      : simulationEnabled ? "Simulate delivery" : deliveryMode === "draft_upload" ? "Send to TikTok inbox" : "Publish now"}
                  </button>
                </>
              )}
              </div>
            </div>
          </footer>
        )}
      </section>
    </div>,
    document.body,
  );
}

function simulatedPublication({
  jobId,
  variantId,
  title,
  privacy,
  allowComment,
  allowDuet,
  allowStitch,
  creatorNickname,
  deliveryMode,
}: {
  jobId: string;
  variantId: string | null;
  title: string;
  privacy: string;
  allowComment: boolean;
  allowDuet: boolean;
  allowStitch: boolean;
  creatorNickname: string;
  deliveryMode: DeliveryMode;
}): TikTokPublication {
  const now = new Date().toISOString();
  return {
    id: `local-preview-${crypto.randomUUID()}`,
    job_id: jobId,
    variant_id: variantId,
    delivery_mode: deliveryMode,
    title,
    privacy_level: privacy,
    allow_comment: allowComment,
    allow_duet: allowDuet,
    allow_stitch: allowStitch,
    creator_nickname: creatorNickname,
    processing_status: "processing",
    visibility_status: "unknown",
    public_at: null,
    retryable: false,
    failure_code: null,
    failure_detail: null,
    latest_metrics: null,
    metrics_synced_at: null,
    evaluation_metrics: null,
    evaluation_captured_at: null,
    created_at: now,
    updated_at: now,
  };
}

function StepProgress({ step }: { step: PublishStep }) {
  return (
    <ol aria-label="Publishing progress" className="hidden items-center gap-2 text-xs md:flex">
      <li className={`flex items-center gap-2 ${step === "details" ? "font-semibold text-[#0c0c0e]" : "text-[#71717a]"}`}>
        <span className={`flex h-7 w-7 items-center justify-center rounded-full border ${step === "details" ? "border-[#0c0c0e] bg-[#0c0c0e] text-white" : "border-zinc-300 bg-white"}`}>1</span>
        Details
      </li>
      <li aria-hidden className="h-px w-8 bg-zinc-300" />
      <li aria-current={step === "confirm" ? "step" : undefined} className={`flex items-center gap-2 ${step === "confirm" ? "font-semibold text-[#0c0c0e]" : "text-[#71717a]"}`}>
        <span className={`flex h-7 w-7 items-center justify-center rounded-full border ${step === "confirm" ? "border-[#0c0c0e] bg-[#0c0c0e] text-white" : "border-zinc-300 bg-white"}`}>2</span>
        Confirm
      </li>
    </ol>
  );
}

function PublishLoading() {
  return (
    <BeamLoader tone="light" mode="line" strength="medium" ariaLabel="Checking TikTok settings">
      <div className="flex min-h-[420px] items-center justify-center py-12 text-center">
        <div role="status" aria-live="polite">
          <p className="font-display text-3xl text-[#0c0c0e]">Checking TikTok settings</p>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-[#71717a]">
            Preparing the privacy, interaction, and disclosure choices for this account.
          </p>
        </div>
      </div>
    </BeamLoader>
  );
}

function DeliveryModePicker({
  value,
  canDirectPost,
  canUploadDraft,
  onChange,
}: {
  value: DeliveryMode;
  canDirectPost: boolean;
  canUploadDraft: boolean;
  onChange: (value: DeliveryMode) => void;
}) {
  return (
    <fieldset className="mb-7">
      <legend className="text-sm font-semibold text-[#18181b]">How do you want to continue?</legend>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className={`rounded-xl border p-4 ${canDirectPost ? "cursor-pointer" : "cursor-not-allowed opacity-50"} ${value === "direct_post" ? "border-lime-600 bg-lime-50/40" : "border-zinc-200 bg-white"}`}>
          <span className="flex items-start gap-3">
            <input
              type="radio"
              name="tiktok-delivery-mode"
              value="direct_post"
              checked={value === "direct_post"}
              disabled={!canDirectPost}
              onChange={() => onChange("direct_post")}
              className="mt-0.5 h-5 w-5 accent-lime-600"
            />
            <span>
              <span className="block text-sm font-semibold text-[#0c0c0e]">Post now</span>
              <span className="mt-1 block text-xs leading-relaxed text-[#71717a]">Choose the audience and publish the approved video directly.</span>
            </span>
          </span>
        </label>
        <label className={`rounded-xl border p-4 ${canUploadDraft ? "cursor-pointer" : "cursor-not-allowed opacity-50"} ${value === "draft_upload" ? "border-lime-600 bg-lime-50/40" : "border-zinc-200 bg-white"}`}>
          <span className="flex items-start gap-3">
            <input
              type="radio"
              name="tiktok-delivery-mode"
              value="draft_upload"
              checked={value === "draft_upload"}
              disabled={!canUploadDraft}
              onChange={() => onChange("draft_upload")}
              className="mt-0.5 h-5 w-5 accent-lime-600"
            />
            <span>
              <span className="block text-sm font-semibold text-[#0c0c0e]">Finish in TikTok</span>
              <span className="mt-1 block text-xs leading-relaxed text-[#71717a]">Send it to your TikTok inbox, then finish and post it in the TikTok phone app.</span>
            </span>
          </span>
        </label>
      </div>
    </fieldset>
  );
}

function DraftDetailsStep({
  options,
  simulation,
  accountAvatarUrl,
  videoTitle,
  variantLabel,
  musicConfirmed,
  handoffConfirmed,
  onMusicConfirmed,
  onHandoffConfirmed,
}: {
  options: TikTokPublishOptions;
  simulation: boolean;
  accountAvatarUrl: string | null;
  videoTitle: string;
  variantLabel: string;
  musicConfirmed: boolean;
  handoffConfirmed: boolean;
  onMusicConfirmed: (value: boolean) => void;
  onHandoffConfirmed: (value: boolean) => void;
}) {
  return (
    <div className="grid gap-7 lg:grid-cols-[minmax(280px,0.72fr)_minmax(500px,1.28fr)] lg:items-start xl:gap-12">
      <div className="border-y border-zinc-200 py-4 lg:sticky lg:top-0">
        <div className="grid grid-cols-[88px_minmax(0,1fr)] items-start gap-4 sm:grid-cols-[112px_minmax(0,1fr)] lg:grid-cols-[132px_minmax(0,1fr)]">
          <video src={options.preview_url} controls playsInline preload="metadata" className="aspect-[9/16] w-full rounded-lg bg-black object-cover" aria-label="Exact video TikTok will receive" />
          <div className="min-w-0">
            <AccountRow nickname={options.creator_nickname} avatarUrl={accountAvatarUrl} subline="Connected TikTok account" compact />
            <p className="mt-4 line-clamp-4 text-sm leading-relaxed text-[#3f3f46]">{videoTitle}</p>
            <p className="mt-3 text-xs leading-relaxed text-[#71717a]">{variantLabel} · {formatDuration(options.duration_s)}</p>
          </div>
        </div>
      </div>
      <div className="space-y-5">
        <div className="border-l-2 border-lime-600 pl-4">
          <p className="font-semibold text-[#0c0c0e]">Kria sends the video, TikTok finishes the post</p>
          <p className="mt-1 text-sm leading-relaxed text-[#3f3f46]">TikTok will send an inbox notification. Open it in the TikTok app to add a sound or effects, choose privacy and disclosures, and publish. Kria cannot complete those steps for you.</p>
        </div>
        <label className="flex min-h-14 items-start gap-3 border-y border-zinc-200 px-1 py-4 text-sm text-[#3f3f46] focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
          <input type="checkbox" checked={musicConfirmed} onChange={(event) => onMusicConfirmed(event.target.checked)} className="mt-0.5 h-5 w-5 accent-lime-600" />
          <span>{simulation ? "For this local preview, confirm the video follows " : "By sending this draft, you agree to "}TikTok&apos;s Music Usage Confirmation.</span>
        </label>
        <label className="flex min-h-14 items-start gap-3 border-y border-zinc-200 px-1 py-4 text-sm text-[#3f3f46] focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
          <input type="checkbox" checked={handoffConfirmed} onChange={(event) => onHandoffConfirmed(event.target.checked)} className="mt-0.5 h-5 w-5 accent-lime-600" />
          <span>I understand I must open the TikTok app on my phone, tap the notification in my Inbox, and finish and post it there. It will not appear in Drafts or on tiktok.com.</span>
        </label>
      </div>
    </div>
  );
}

function DraftConfirmStep({
  options,
  simulation,
  accountAvatarUrl,
  onEdit,
}: {
  options: TikTokPublishOptions;
  simulation: boolean;
  accountAvatarUrl: string | null;
  onEdit: () => void;
}) {
  return (
    <div className="mx-auto w-full max-w-[900px]">
      <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-4 border-b border-zinc-200 pb-6 sm:grid-cols-[112px_minmax(0,1fr)]">
        <video src={options.preview_url} controls muted playsInline preload="metadata" className="aspect-[9/16] w-full rounded-lg bg-black object-cover" aria-label={simulation ? "Video being previewed" : "Video being sent to your TikTok inbox"} />
        <AccountRow nickname={options.creator_nickname} avatarUrl={accountAvatarUrl} subline="Sending to your TikTok inbox" compact />
      </div>
      <div className="mt-7 flex items-center justify-between gap-4">
        <p className="font-display text-3xl text-[#0c0c0e]">Inbox handoff summary</p>
        <button type="button" onClick={onEdit} className="min-h-11 text-sm font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">Edit choice</button>
      </div>
      <dl className="mt-2 divide-y divide-zinc-200 border-y border-zinc-200">
        <ReviewSummaryRow label="Destination" value="TikTok inbox (phone app)" />
        <ReviewSummaryRow label="Next step" value="Open TikTok's inbox notification to edit and post" />
        <ReviewSummaryRow label="Music" value="Music usage confirmed" />
      </dl>
      <div className="mt-7 border border-lime-600 px-4 py-4">
        <p className="font-semibold text-[#0c0c0e]">Ready to send</p>
        <p className="mt-1 text-sm text-[#3f3f46]">This does not create a TikTok post. After you send it, open the TikTok app on your phone, tap Inbox, then tap the notification to finish and post. It will not appear on tiktok.com or under Profile → Drafts.</p>
      </div>
    </div>
  );
}

function DetailsStep({
  options,
  simulation,
  title,
  privacy,
  allowComment,
  allowDuet,
  allowStitch,
  commercialContent,
  brandContent,
  brandOrganic,
  isAigc,
  musicConfirmed,
  invalidCommercial,
  invalidPrivateBrand,
  accountAvatarUrl,
  videoTitle,
  variantLabel,
  onTitle,
  onPrivacy,
  onAllowComment,
  onAllowDuet,
  onAllowStitch,
  onCommercialContent,
  onBrandContent,
  onBrandOrganic,
  onIsAigc,
  onMusicConfirmed,
}: {
  options: TikTokPublishOptions;
  simulation: boolean;
  title: string;
  privacy: string;
  allowComment: boolean;
  allowDuet: boolean;
  allowStitch: boolean;
  commercialContent: boolean;
  brandContent: boolean;
  brandOrganic: boolean;
  isAigc: boolean;
  musicConfirmed: boolean;
  invalidCommercial: boolean;
  invalidPrivateBrand: boolean;
  accountAvatarUrl: string | null;
  videoTitle: string;
  variantLabel: string;
  onTitle: (value: string) => void;
  onPrivacy: (value: string) => void;
  onAllowComment: (value: boolean) => void;
  onAllowDuet: (value: boolean) => void;
  onAllowStitch: (value: boolean) => void;
  onCommercialContent: (value: boolean) => void;
  onBrandContent: (value: boolean) => void;
  onBrandOrganic: (value: boolean) => void;
  onIsAigc: (value: boolean) => void;
  onMusicConfirmed: (value: boolean) => void;
}) {
  return (
    <div className="grid gap-7 lg:grid-cols-[minmax(280px,0.72fr)_minmax(500px,1.28fr)] lg:items-start xl:gap-12">
      <div className="space-y-5 lg:sticky lg:top-0">
        <div className="border-y border-zinc-200 py-4">
          <div className="grid grid-cols-[88px_minmax(0,1fr)] items-start gap-4 sm:grid-cols-[112px_minmax(0,1fr)] lg:grid-cols-[132px_minmax(0,1fr)]">
            <video
              src={options.preview_url}
              controls
              playsInline
              preload="metadata"
              className="aspect-[9/16] w-full rounded-lg bg-black object-cover"
              aria-label="Exact video TikTok will receive"
            />
            <div className="min-w-0">
              <AccountRow
                nickname={options.creator_nickname}
                avatarUrl={accountAvatarUrl}
                subline="Connected TikTok account"
                compact
              />
              <p className="mt-4 line-clamp-4 text-sm leading-relaxed text-[#3f3f46]">
                {title || videoTitle}
              </p>
              <p className="mt-3 text-xs leading-relaxed text-[#71717a]">
                {variantLabel} · {formatDuration(options.duration_s)}
              </p>
            </div>
          </div>
        </div>

        {!options.audited && (
          <p className="border-l-2 border-lime-600 pl-3 text-sm text-[#3f3f46]">
            Private beta posts are limited to <strong>Only you</strong> until TikTok completes the app audit.
          </p>
        )}
      </div>

      <div className="space-y-6">
        <label className="block text-sm font-semibold text-[#18181b]">
        Caption &amp; hashtags
        <textarea
          value={title}
          onChange={(event) => onTitle(event.target.value)}
          maxLength={2200}
          rows={5}
          className="mt-2 w-full resize-none rounded-lg border border-zinc-300 bg-white px-4 py-3 font-normal leading-relaxed text-[#0c0c0e] focus:border-lime-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
        />
        <span aria-live="polite" className="mt-1 block text-right text-xs font-normal text-[#71717a]">
          {title.length} / 2200
        </span>
        </label>

      <fieldset>
        <legend className="text-sm font-semibold text-[#18181b]">Who can watch this video?</legend>
        <div className="mt-2 divide-y divide-zinc-200 border-y border-zinc-200">
          {options.privacy_options.map((value) => (
            <label key={value} className="flex min-h-14 cursor-pointer items-center gap-3 py-2 text-sm text-[#3f3f46] focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
              <input
                type="radio"
                name="tiktok-privacy"
                value={value}
                checked={privacy === value}
                onChange={(event) => onPrivacy(event.target.value)}
                className="h-5 w-5 accent-lime-600"
              />
              <span>
                <span className="block font-medium text-[#0c0c0e]">{privacyLabel(value)}</span>
                <span className="block text-xs text-[#71717a]">{privacyDescription(value)}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset>
        <legend className="text-sm font-semibold text-[#18181b]">Interactions</legend>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <Toggle label="Comments" checked={allowComment} disabled={options.comment_disabled} onChange={onAllowComment} />
          <Toggle label="Duet" checked={allowDuet} disabled={options.duet_disabled} onChange={onAllowDuet} />
          <Toggle label="Stitch" checked={allowStitch} disabled={options.stitch_disabled} onChange={onAllowStitch} />
        </div>
      </fieldset>

      <fieldset className="border-y border-zinc-200 py-4">
        <legend className="px-1 text-sm font-semibold text-[#18181b]">Content disclosures</legend>
        <Check
          label="This video promotes a brand, product, or service"
          checked={commercialContent}
          onChange={onCommercialContent}
        />
        {commercialContent && (
          <div className="ml-6 space-y-1 border-l border-zinc-200 pl-3">
            <Check label="Your Brand" checked={brandOrganic} onChange={onBrandOrganic} />
            <Check label="Branded Content" checked={brandContent} onChange={onBrandContent} />
            {invalidCommercial && (
              <p className="text-xs text-red-700">Choose at least one commercial-content type.</p>
            )}
          </div>
        )}
        <Check
          label="This includes realistic AI-generated or significantly AI-edited media"
          checked={isAigc}
          onChange={onIsAigc}
        />
      </fieldset>

      <label className="flex min-h-14 items-start gap-3 border-l-2 border-lime-600 px-4 py-3 text-sm text-[#3f3f46] focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
        <input
          type="checkbox"
          checked={musicConfirmed}
          onChange={(event) => onMusicConfirmed(event.target.checked)}
          className="mt-0.5 h-5 w-5 accent-lime-600"
        />
        <span>
          {simulation ? "For this local preview, confirm the post follows " : "By posting, you agree to "}
          TikTok&apos;s {brandContent ? "Branded Content Policy and " : ""}Music Usage Confirmation.
        </span>
      </label>

      {invalidPrivateBrand && (
        <p className="text-sm text-red-700">TikTok does not allow branded content with Only you privacy.</p>
      )}
      </div>
    </div>
  );
}

function ConfirmStep({
  options,
  simulation,
  title,
  privacy,
  allowComment,
  allowDuet,
  allowStitch,
  commercialContent,
  brandContent,
  brandOrganic,
  isAigc,
  accountAvatarUrl,
  onEdit,
}: {
  options: TikTokPublishOptions;
  simulation: boolean;
  title: string;
  privacy: string;
  allowComment: boolean;
  allowDuet: boolean;
  allowStitch: boolean;
  commercialContent: boolean;
  brandContent: boolean;
  brandOrganic: boolean;
  isAigc: boolean;
  accountAvatarUrl: string | null;
  onEdit: () => void;
}) {
  const interactions = [allowComment && "Comments on", allowDuet && "Duet on", allowStitch && "Stitch on"]
    .filter(Boolean)
    .join(" · ") || "Comments off · Duet off · Stitch off";
  const disclosures = [
    commercialContent && (brandContent ? "Branded content" : brandOrganic ? "Your brand" : "Commercial"),
    isAigc && "AI-edited media",
  ].filter(Boolean).join(" · ") || "None";

  return (
    <div className="mx-auto w-full max-w-[900px]">
      <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-4 border-b border-zinc-200 pb-6 sm:grid-cols-[112px_minmax(0,1fr)]">
        <video
          src={options.preview_url}
          controls
          muted
          playsInline
          preload="metadata"
          className="aspect-[9/16] w-full rounded-lg bg-black object-cover"
          aria-label={simulation ? "Video being previewed" : "Video being published"}
        />
        <div className="min-w-0">
          <AccountRow
            nickname={options.creator_nickname}
            avatarUrl={accountAvatarUrl}
            subline="Posting to TikTok"
            compact
          />
          <p className="mt-4 line-clamp-5 whitespace-pre-wrap text-sm leading-relaxed text-[#3f3f46]">
            {title || "No caption"}
          </p>
        </div>
      </div>

      <div className="mt-7 flex items-center justify-between gap-4">
        <p className="font-display text-3xl text-[#0c0c0e]">Post summary</p>
        <button
          type="button"
          onClick={onEdit}
          className="min-h-11 text-sm font-medium text-lime-700 underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
        >
          Edit details
        </button>
      </div>
      <dl className="mt-2 divide-y divide-zinc-200 border-y border-zinc-200">
        <ReviewSummaryRow label="Audience" value={privacyLabel(privacy)} />
        <ReviewSummaryRow label="Interactions" value={interactions} />
        <ReviewSummaryRow label="Disclosures" value={disclosures} />
        <ReviewSummaryRow label="Music" value="Music usage confirmed" />
      </dl>

      <div className="mt-7 border border-lime-600 px-4 py-4">
        <p className="font-semibold text-[#0c0c0e]">All set to publish</p>
        <p className="mt-1 text-sm text-[#3f3f46]">Your post choices are complete and ready for final confirmation.</p>
      </div>

      <p className="mt-8 text-xs leading-relaxed text-[#71717a]">
        {simulation
          ? "Completing this preview creates a local receipt only. Nothing will be sent to TikTok."
          : "Publishing creates a TikTok post. Changes or removal may need to be made in TikTok."}
      </p>
    </div>
  );
}

function ReviewSummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-h-[68px] grid-cols-[minmax(110px,0.45fr)_minmax(0,1fr)] items-center gap-4 py-3">
      <dt className="text-sm font-semibold text-[#0c0c0e]">{label}</dt>
      <dd className="text-sm text-[#3f3f46]">{value}</dd>
    </div>
  );
}

function AccountRow({
  nickname,
  avatarUrl,
  subline,
  compact = false,
}: {
  nickname: string;
  avatarUrl: string | null;
  subline?: string;
  compact?: boolean;
}) {
  return (
    <div className={`flex items-center gap-3 ${compact ? "mt-1" : ""}`}>
      <span className={`${compact ? "h-9 w-9" : "h-11 w-11"} flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-[#ead4c6] font-semibold text-[#6b4231]`}>
        {avatarUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={avatarUrl} alt="" className="h-full w-full object-cover" />
        ) : (
          nickname.trim().charAt(0).toUpperCase() || "T"
        )}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold text-[#0c0c0e]">{nickname}</span>
        {subline && <span className="block truncate text-xs text-[#71717a]">{subline}</span>}
      </span>
    </div>
  );
}

function Toggle({
  label,
  checked,
  disabled = false,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className={`flex min-h-12 cursor-pointer items-center justify-center rounded-lg border px-2 text-center text-xs font-medium focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-500 ${
      disabled
        ? "cursor-not-allowed border-zinc-200 bg-zinc-50 text-[#a1a1aa]"
        : checked
          ? "border-lime-600 bg-lime-50 text-lime-800"
          : "border-zinc-200 bg-white text-[#3f3f46]"
    }`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="sr-only"
      />
      {label}{disabled ? " unavailable" : checked ? " on" : " off"}
    </label>
  );
}

function Check({
  label,
  checked,
  disabled = false,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className={`flex min-h-11 items-center gap-2 text-sm ${disabled ? "text-[#a1a1aa]" : "text-[#3f3f46]"}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="h-4 w-4 accent-lime-600"
      />
      {label}
      {disabled && " (unavailable)"}
    </label>
  );
}

function privacyLabel(value: string) {
  if (value === "SELF_ONLY") return "Only you";
  if (value === "MUTUAL_FOLLOW_FRIENDS") return "Friends";
  if (value === "FOLLOWER_OF_CREATOR") return "Followers";
  if (value === "PUBLIC_TO_EVERYONE") return "Public";
  return value.replaceAll("_", " ").toLowerCase();
}

function privacyDescription(value: string) {
  if (value === "SELF_ONLY") return "Visible only from your TikTok account";
  if (value === "MUTUAL_FOLLOW_FRIENDS") return "People you follow who follow you back";
  if (value === "FOLLOWER_OF_CREATOR") return "People who follow your TikTok account";
  if (value === "PUBLIC_TO_EVERYONE") return "Anyone on or off TikTok can watch";
  return "TikTok account privacy setting";
}

function formatDuration(seconds: number | null) {
  if (seconds == null) return "Exact duration";
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60).toString().padStart(2, "0");
  return `${mins}:${secs}`;
}

function isTikTokReconnectError(error: string | null) {
  return /connect|reconnect|authorization|expired|permission/i.test(error ?? "");
}

function publishOptionsErrorMessage(error: string | null) {
  if (isTikTokReconnectError(error)) {
    return "Reconnect the TikTok account for this item, then Kria will restore the publishing flow here.";
  }
  return "Kria couldn't load the latest choices for this TikTok account. Retry without losing your place.";
}

function publicationErrorMessage(error: string | null) {
  if (/changed|revision|render/i.test(error ?? "")) {
    return "The video changed before TikTok received it.";
  }
  if (isTikTokReconnectError(error)) {
    return "TikTok authorization changed before the post was sent.";
  }
  return "TikTok did not accept the post yet.";
}

function currentReturnTo() {
  if (typeof window === "undefined") return "/plan";
  return `${window.location.pathname}${window.location.search}`;
}
