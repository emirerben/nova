"use client";

import Link from "next/link";
import { useState } from "react";

type DemoStage = "details" | "confirm" | "receipt";
type DeliveryMode = "direct_post" | "draft_upload";

export default function TikTokProductWorkspace({ videoSrc }: { videoSrc: string | null }) {
  const [stage, setStage] = useState<DemoStage>("details");
  const [deliveryMode, setDeliveryMode] = useState<DeliveryMode>("direct_post");
  const [caption, setCaption] = useState("A quiet morning in the studio #creatorlife");
  const [privacy, setPrivacy] = useState("");
  const [comments, setComments] = useState(false);
  const [commercialContent, setCommercialContent] = useState(false);
  const [brandOrganic, setBrandOrganic] = useState(false);
  const [aigc, setAigc] = useState(false);
  const [musicConfirmed, setMusicConfirmed] = useState(false);
  const [draftHandoffConfirmed, setDraftHandoffConfirmed] = useState(false);
  const hasValidCommercialDisclosure = !commercialContent || brandOrganic;
  const canContinue = deliveryMode === "draft_upload"
    ? draftHandoffConfirmed && musicConfirmed
    : privacy === "SELF_ONLY" && musicConfirmed && hasValidCommercialDisclosure;

  function chooseDeliveryMode(mode: DeliveryMode) {
    setDeliveryMode(mode);
    setStage("details");
  }

  function resetDemo() {
    setStage("details");
    setPrivacy("");
    setComments(false);
    setCommercialContent(false);
    setBrandOrganic(false);
    setAigc(false);
    setMusicConfirmed(false);
    setDraftHandoffConfirmed(false);
  }

  return (
    <main className="min-h-screen bg-[#ffffff] text-[#0c0c0e]">
      <div className="mx-auto max-w-[1180px] px-6 pb-24 pt-14">
        <header className="border-b border-zinc-200 pb-10">
          <p className="mb-3 text-xs font-semibold uppercase text-lime-700">Product workspace</p>
          <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <div>
              <h1 className="max-w-3xl text-balance font-display text-4xl font-medium leading-tight sm:text-5xl">
                TikTok delivery, from approved edit to creator control
              </h1>
              <p className="mt-4 max-w-2xl text-pretty text-base leading-relaxed text-[#52525b]">
                This public workspace demonstrates every TikTok permission Kria requests: account
                connection, publishing the exact video you approved, and an inbox handoff that you
                finish inside TikTok.
              </p>
            </div>
            <Link
              href="/plan"
              className="inline-flex min-h-11 items-center justify-center rounded-full bg-[#0c0c0e] px-6 py-2 text-sm font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
            >
              Open live product
            </Link>
          </div>
        </header>

        <section className="grid gap-3 border-b border-zinc-200 py-6 sm:grid-cols-3" aria-label="Integration status">
          <StatusCard label="Connection" value="TikTok connected" detail="Login Kit + granted scopes" />
          <StatusCard label="Publishing" value="Publish now or finish in TikTok" detail="The exact video you approved" />
          <StatusCard label="Sandbox review" value="Private testing" detail="Target user only" />
        </section>

        <section className="py-12" aria-labelledby="workflow-heading">
          <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="text-xs font-semibold uppercase text-[#71717a]">Interactive product preview</p>
              <h2 id="workflow-heading" className="mt-1 text-balance font-display text-3xl font-medium">
                Review the release flow
              </h2>
            </div>
            <p className="max-w-sm text-pretty text-sm text-[#71717a]">
              Demo data only. Completing this walkthrough never submits a post or changes an account.
            </p>
          </div>

          <div className="grid overflow-hidden rounded-2xl border border-zinc-200 bg-white lg:grid-cols-[minmax(300px,0.82fr)_minmax(0,1.18fr)]">
            <div className="border-b border-zinc-200 bg-[#111113] p-7 lg:border-b-0 lg:border-r">
              <div className="mx-auto aspect-[9/16] max-h-[610px] overflow-hidden rounded-2xl bg-zinc-900 shadow-lg">
                {videoSrc ? (
                  <video
                    src={videoSrc}
                    controls
                    playsInline
                    preload="metadata"
                    aria-label="The exact video you approved"
                    className="size-full object-cover"
                  />
                ) : (
                  <div className="flex size-full items-end bg-zinc-800 p-6 text-white">
                    <div>
                      <p className="text-xs uppercase text-white/60">The exact video you approved</p>
                      <p className="mt-2 text-pretty font-display text-2xl">A quiet morning in the studio</p>
                    </div>
                  </div>
                )}
              </div>
              <div className="mx-auto mt-4 flex max-w-[343px] items-center justify-between text-xs text-zinc-400">
                <span>Finalized MP4</span>
                <span className="tabular-nums">00:08</span>
              </div>
            </div>

            <div className="p-6 sm:p-9">
              <fieldset className="mb-8">
                <legend className="text-xs font-semibold uppercase text-[#71717a]">Choose delivery path</legend>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <DeliveryModeCard
                    checked={deliveryMode === "direct_post"}
                    title="Publish now"
                    detail="Kria sends the exact video you approved directly to TikTok after final confirmation."
                    onChange={() => chooseDeliveryMode("direct_post")}
                  />
                  <DeliveryModeCard
                    checked={deliveryMode === "draft_upload"}
                    title="Finish in TikTok"
                    detail="Kria sends the exact video you approved to your TikTok inbox. Open the TikTok app to finish editing and post it."
                    onChange={() => chooseDeliveryMode("draft_upload")}
                  />
                </div>
              </fieldset>

              <ol className="mb-8 grid grid-cols-3 gap-2 text-xs" aria-label="Publishing steps">
                <Step label="Details" number="1" active={stage === "details"} complete={stage !== "details"} />
                <Step label="Confirm" number="2" active={stage === "confirm"} complete={stage === "receipt"} />
                <Step label="Receipt" number="3" active={stage === "receipt"} complete={false} />
              </ol>

              {stage === "details" && (
                <div>
                  <h3 className="text-balance font-display text-2xl">
                    {deliveryMode === "direct_post" ? "Creator-controlled details" : "TikTok app inbox handoff"}
                  </h3>
                  <p className="mt-2 text-pretty text-sm leading-relaxed text-[#71717a]">
                    {deliveryMode === "direct_post"
                      ? "Kria suggests copy, but you choose privacy and interactions before publishing."
                      : "TikTok will send an inbox notification to the TikTok app on your phone. Open it there to edit and finish the post; sending it does not publish anything."}
                  </p>

                  {deliveryMode === "draft_upload" ? (
                    <div className="mt-6 space-y-4">
                      <ol className="space-y-3 rounded-xl border border-zinc-200 bg-[#f4f4f1] p-4 text-sm" aria-label="TikTok app inbox handoff steps">
                        <LifecycleRow label="Kria sends the approved video" detail="TikTok creates an inbox notification for this account." />
                        <LifecycleRow label="Open the TikTok app to continue" detail="Tap the notification in Inbox and finish editing there." />
                        <LifecycleRow label="You decide whether to post" detail="No TikTok post exists until you complete TikTok's own composer." />
                      </ol>
                      <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-zinc-200 p-4 text-sm focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
                        <input
                          type="checkbox"
                          checked={draftHandoffConfirmed}
                          onChange={(event) => setDraftHandoffConfirmed(event.target.checked)}
                          className="mt-1 accent-lime-600"
                        />
                        <span>I understand I must open the TikTok app on my phone, tap the notification in my Inbox, and finish it there.</span>
                      </label>
                    </div>
                  ) : (
                    <>
                  <label className="mt-6 block text-sm font-medium text-[#3f3f46]">
                    Caption and hashtags
                    <textarea
                      value={caption}
                      onChange={(event) => setCaption(event.target.value)}
                      rows={3}
                      className="mt-2 w-full resize-y rounded-xl border border-zinc-300 bg-white px-3 py-3 text-[#0c0c0e] outline-none focus:border-zinc-500 focus:ring-2 focus:ring-lime-500/30"
                    />
                  </label>

                  <fieldset className="mt-6">
                    <legend className="text-sm font-medium text-[#3f3f46]">Who can watch this video?</legend>
                    <label className="mt-2 flex cursor-pointer items-start gap-3 rounded-xl border border-zinc-200 p-4 focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
                      <input
                        type="radio"
                        name="demo-privacy"
                        value="SELF_ONLY"
                        checked={privacy === "SELF_ONLY"}
                        onChange={(event) => setPrivacy(event.target.value)}
                        className="mt-1 accent-lime-600"
                      />
                      <span>
                        <span className="block text-sm font-medium">Only you</span>
                        <span className="mt-0.5 block text-xs text-[#71717a]">
                          Required during TikTok&apos;s unaudited review period.
                        </span>
                      </span>
                    </label>
                  </fieldset>

                  <fieldset className="mt-6 space-y-3">
                    <legend className="text-sm font-medium text-[#3f3f46]">Interactions and disclosures</legend>
                    <CheckRow checked={comments} onChange={setComments} label="Allow comments" />
                    <CheckRow checked={false} onChange={() => undefined} label="Allow Duet" disabled />
                    <CheckRow checked={false} onChange={() => undefined} label="Allow Stitch" disabled />
                    <CheckRow
                      checked={commercialContent}
                      onChange={(checked) => {
                        setCommercialContent(checked);
                        if (!checked) setBrandOrganic(false);
                      }}
                      label="This video promotes a brand, product, or service"
                    />
                    {commercialContent && (
                      <div className="ml-4 space-y-3 border-l border-zinc-200 pl-4">
                        <CheckRow
                          checked={brandOrganic}
                          onChange={setBrandOrganic}
                          label="Your brand"
                        />
                        <CheckRow
                          checked={false}
                          onChange={() => undefined}
                          label="Branded content — unavailable with Only you"
                          disabled
                        />
                        {!brandOrganic && (
                          <p className="text-xs text-red-700">
                            Choose the available commercial-content type to continue.
                          </p>
                        )}
                      </div>
                    )}
                    <CheckRow checked={aigc} onChange={setAigc} label="This content includes AI-generated material" />
                  </fieldset>
                    </>
                  )}

                  <label className="mt-6 flex cursor-pointer items-start gap-3 rounded-xl bg-[#f4f4f1] p-4 text-sm focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600">
                    <input
                      type="checkbox"
                      checked={musicConfirmed}
                      onChange={(event) => setMusicConfirmed(event.target.checked)}
                      className="mt-1 accent-lime-600"
                    />
                    <span className="text-pretty text-[#3f3f46]">
                      I confirm that I have the right to use the music in this video.
                    </span>
                  </label>

                  <button
                    type="button"
                    disabled={!canContinue}
                    onClick={() => setStage("confirm")}
                    className="mt-7 min-h-11 w-full rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {deliveryMode === "direct_post" ? "Review submission" : "Review handoff"}
                  </button>
                </div>
              )}

              {stage === "confirm" && (
                <div>
                  <h3 className="text-balance font-display text-2xl">
                    {deliveryMode === "direct_post" ? "Confirm the exact submission" : "Confirm the TikTok handoff"}
                  </h3>
                  <p className="mt-2 text-pretty text-sm text-[#71717a]">
                    The final object fingerprint is rechecked before Kria snapshots and sends it to TikTok.
                  </p>
                  <dl className="mt-7 divide-y divide-zinc-200 border-y border-zinc-200">
                    <SummaryRow label="Account" value="@review_sandbox" />
                    <SummaryRow label="Delivery" value={deliveryMode === "direct_post" ? "Publish now" : "Finish in TikTok"} />
                    {deliveryMode === "direct_post" ? (
                      <>
                        <SummaryRow label="Audience" value="Only you" />
                        <SummaryRow label="Comments" value={comments ? "Allowed" : "Off"} />
                        <SummaryRow label="Commercial content" value={brandOrganic ? "Your brand" : "None"} />
                        <SummaryRow label="AI-generated content" value={aigc ? "Declared" : "No declaration"} />
                        <SummaryRow label="Caption" value={caption || "No caption"} />
                      </>
                    ) : (
                      <SummaryRow label="Next step" value="Open the notification in the TikTok app" />
                    )}
                  </dl>
                  <div className="mt-7 flex flex-col-reverse gap-3 sm:flex-row">
                    <button
                      type="button"
                      onClick={() => setStage("details")}
                      className="min-h-11 flex-1 rounded-full border border-zinc-300 px-5 py-2 text-sm font-semibold hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
                    >
                      Back
                    </button>
                    <button
                      type="button"
                      onClick={() => setStage("receipt")}
                      className="min-h-11 flex-1 rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
                    >
                      {deliveryMode === "direct_post" ? "Complete preview" : "Preview inbox handoff"}
                    </button>
                  </div>
                </div>
              )}

              {stage === "receipt" && (
                <div>
                  <div className="inline-flex rounded-full bg-lime-100 px-3 py-1 text-xs font-semibold text-lime-800">
                    Demo receipt
                  </div>
                  <h3 className="mt-4 text-balance font-display text-2xl">
                    {deliveryMode === "direct_post" ? "Published privately on TikTok" : "Waiting in your TikTok inbox"}
                  </h3>
                  <p className="mt-2 text-pretty text-sm leading-relaxed text-[#71717a]">
                    {deliveryMode === "direct_post"
                      ? "Processing is complete and visibility is private. Completion never implies public visibility; Kria tracks both states independently."
                      : "TikTok accepted the video for handoff and sent an inbox notification to the TikTok app on your phone. Open it there to continue — it does not appear on tiktok.com or in Profile drafts, and Kria has not created a post."}
                  </p>
                  <ol className="mt-7 space-y-4" aria-label="Demo publication lifecycle">
                    <LifecycleRow label="Immutable snapshot created" detail="Approved generation preserved" />
                    {deliveryMode === "direct_post" ? (
                      <>
                        <LifecycleRow label="TikTok processed the post" detail="Webhook reconciled" />
                        <LifecycleRow label="Visibility: Only you" detail="Private sandbox result confirmed" />
                      </>
                    ) : (
                      <>
                        <LifecycleRow label="TikTok accepted the handoff" detail="Inbox notification created" />
                        <LifecycleRow label="No post created" detail="Creator must finish inside TikTok" />
                      </>
                    )}
                  </ol>
                  <button
                    type="button"
                    onClick={resetDemo}
                    className="mt-8 min-h-11 w-full rounded-full border border-zinc-300 px-5 py-2 text-sm font-semibold hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600"
                  >
                    Restart walkthrough
                  </button>
                </div>
              )}
            </div>
          </div>
        </section>

        <section className="grid gap-5 border-t border-zinc-200 py-12 md:grid-cols-3" aria-label="Integration safeguards">
          <InfoCard title="Exact video" body="Kria sends the exact approved video to TikTok with a short-lived media token." />
          <InfoCard title="Creator consent" body="Privacy is always selected manually. Interactions start off, and commercial, AIGC, and music declarations are explicit." />
          <InfoCard title="Account control" body="Disconnect revokes access and erases stored TikTok credentials. Kria requests no profile, statistics, or video-list scopes in this review." />
        </section>

        <footer className="flex flex-col gap-4 border-t border-zinc-200 pt-8 text-sm text-[#71717a] sm:flex-row sm:items-center sm:justify-between">
          <p className="text-pretty">Kria is a creator workspace for planning, editing, and delivering short-form video.</p>
          <nav className="flex gap-5" aria-label="Legal and product links">
            <Link href="/plan" className="inline-flex min-h-11 items-center hover:text-[#0c0c0e] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-600">Live product</Link>
            <Link href="/terms" className="inline-flex min-h-11 items-center hover:text-[#0c0c0e] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-600">Terms</Link>
            <Link href="/privacy" className="inline-flex min-h-11 items-center hover:text-[#0c0c0e] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-600">Privacy</Link>
          </nav>
        </footer>
      </div>
    </main>
  );
}

function StatusCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4">
      <p className="text-xs text-[#71717a]">{label}</p>
      <p className="mt-1 font-semibold">{value}</p>
      <p className="mt-1 text-xs text-[#71717a]">{detail}</p>
    </div>
  );
}

function DeliveryModeCard({
  checked,
  title,
  detail,
  onChange,
}: {
  checked: boolean;
  title: string;
  detail: string;
  onChange: () => void;
}) {
  return (
    <label
      className={`cursor-pointer rounded-xl border p-4 focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600 ${
        checked ? "border-lime-600 bg-lime-50" : "border-zinc-200 bg-white"
      }`}
    >
      <span className="flex items-center gap-2 text-sm font-semibold">
        <input
          type="radio"
          name="demo-delivery-mode"
          checked={checked}
          onChange={onChange}
          className="accent-lime-600"
        />
        {title}
      </span>
      <span className="mt-2 block text-xs leading-relaxed text-[#71717a]">{detail}</span>
    </label>
  );
}

function Step({ label, number, active, complete }: { label: string; number: string; active: boolean; complete: boolean }) {
  return (
    <li className={`border-t-2 pt-2 ${active || complete ? "border-lime-600 text-[#0c0c0e]" : "border-zinc-200 text-[#a1a1aa]"}`}>
      <span className="mr-1 tabular-nums">{complete ? "✓" : number}.</span> {label}
    </li>
  );
}

function CheckRow({ checked, onChange, label, disabled = false }: { checked: boolean; onChange: (checked: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <label className={`flex items-center justify-between gap-3 rounded-xl border border-zinc-200 px-4 py-3 text-sm focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-600 ${disabled ? "cursor-not-allowed text-[#a1a1aa]" : "cursor-pointer"}`}>
      <span>{label}</span>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} className="accent-lime-600" />
    </label>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 py-4 sm:grid-cols-[150px_1fr] sm:gap-4">
      <dt className="text-sm text-[#71717a]">{label}</dt>
      <dd className="break-words text-sm font-medium sm:text-right">{value}</dd>
    </div>
  );
}

function LifecycleRow({ label, detail }: { label: string; detail: string }) {
  return (
    <li className="flex gap-3">
      <span className="mt-1 flex size-5 shrink-0 items-center justify-center rounded-full bg-lime-600 text-xs font-bold text-white">✓</span>
      <div>
        <p className="text-sm font-medium">{label}</p>
        <p className="mt-0.5 text-xs text-[#71717a]">{detail}</p>
      </div>
    </li>
  );
}

function InfoCard({ title, body }: { title: string; body: string }) {
  return (
    <article className="rounded-2xl border border-zinc-200 bg-white p-5">
      <h3 className="font-display text-xl">{title}</h3>
      <p className="mt-3 text-pretty text-sm leading-relaxed text-[#71717a]">{body}</p>
    </article>
  );
}
