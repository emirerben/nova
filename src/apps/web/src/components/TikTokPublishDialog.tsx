"use client";

import { useEffect, useRef, useState } from "react";
import {
  createTikTokPublication,
  getTikTokPublishOptions,
  type TikTokPublication,
  type TikTokPublishOptions,
} from "@/lib/tiktok-api";

export function TikTokPublishDialog({
  open,
  jobId,
  variantId,
  onClose,
  onPublished,
}: {
  open: boolean;
  jobId: string;
  variantId?: string | null;
  onClose: () => void;
  onPublished?: (publication: TikTokPublication) => void;
}) {
  const [options, setOptions] = useState<TikTokPublishOptions | null>(null);
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
  const [state, setState] = useState<"loading" | "ready" | "submitting" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const idempotencyKey = useRef(crypto.randomUUID());

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
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
    idempotencyKey.current = crypto.randomUUID();
    void getTikTokPublishOptions(jobId, variantId)
      .then((value) => {
        if (cancelled) return;
        setOptions(value);
        setTitle(value.suggested_title);
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "TikTok publishing is unavailable");
        setState("error");
      });
    return () => { cancelled = true; };
  }, [open, jobId, variantId]);

  if (!open) return null;

  async function publish() {
    if (!options || !privacy || !musicConfirmed) return;
    setState("submitting");
    setError(null);
    try {
      const publication = await createTikTokPublication({
        job_id: jobId,
        variant_id: options.variant_id,
        source_revision: options.source_revision,
        idempotency_key: idempotencyKey.current,
        title,
        privacy_level: privacy,
        allow_comment: allowComment,
        allow_duet: allowDuet,
        allow_stitch: allowStitch,
        brand_content_toggle: brandContent,
        brand_organic_toggle: brandOrganic,
        is_aigc: isAigc,
        music_usage_confirmed: musicConfirmed,
        consent_version: options.consent_version,
      });
      onPublished?.(publication);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not send this video to TikTok");
      setState("ready");
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-labelledby="tiktok-publish-title">
      <div className="max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-2xl bg-white p-5 shadow-2xl sm:p-7">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#71717a]">Direct Post</p>
            <h2 id="tiktok-publish-title" className="mt-1 font-display text-2xl text-[#0c0c0e]">Publish to TikTok</h2>
          </div>
          <button type="button" onClick={onClose} className="min-h-11 rounded-full px-3 text-sm text-[#3f3f46] hover:bg-zinc-100">Close</button>
        </div>

        {state === "loading" && <p className="py-16 text-center text-sm text-[#71717a]">Checking your TikTok posting options…</p>}
        {state === "error" && <p className="my-8 rounded-xl bg-red-50 p-4 text-sm text-red-700">{error}</p>}

        {options && state !== "loading" && state !== "error" && (
          <div className="mt-5 grid gap-6 md:grid-cols-[220px_1fr]">
            <video src={options.preview_url} controls playsInline preload="metadata" className="aspect-[9/16] w-full rounded-xl bg-black object-cover" aria-label="Exact video TikTok will receive" />
            <div className="space-y-5">
              <p className="text-sm text-[#3f3f46]">Posting as <strong>{options.creator_nickname}</strong>. TikTok will receive exactly the preview shown here.</p>
              {!options.audited && <p className="rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-sm text-[#3f3f46]">Private beta posts are limited to <strong>Only you</strong> until TikTok completes the app audit.</p>}

              <label className="block text-sm font-medium text-[#18181b]">Caption and hashtags
                <textarea value={title} onChange={(event) => setTitle(event.target.value)} maxLength={2200} rows={4} className="mt-1 w-full rounded-xl border border-zinc-300 px-3 py-2 font-normal" />
              </label>

              <label className="block text-sm font-medium text-[#18181b]">Who can view this video?
                <select value={privacy} onChange={(event) => setPrivacy(event.target.value)} className="mt-1 min-h-11 w-full rounded-xl border border-zinc-300 bg-white px-3 font-normal">
                  <option value="" disabled>Select privacy…</option>
                  {options.privacy_options.map((value) => <option key={value} value={value}>{privacyLabel(value)}</option>)}
                </select>
              </label>

              <fieldset className="space-y-2">
                <legend className="text-sm font-medium text-[#18181b]">Allow people to</legend>
                <Check label="Comment" checked={allowComment} disabled={options.comment_disabled} onChange={setAllowComment} />
                <Check label="Duet" checked={allowDuet} disabled={options.duet_disabled} onChange={setAllowDuet} />
                <Check label="Stitch" checked={allowStitch} disabled={options.stitch_disabled} onChange={setAllowStitch} />
              </fieldset>

              <fieldset className="space-y-2 rounded-xl border border-zinc-200 p-3">
                <legend className="px-1 text-sm font-medium text-[#18181b]">Content disclosure</legend>
                <Check
                  label="This video promotes a brand, product, or service"
                  checked={commercialContent}
                  onChange={(checked) => {
                    setCommercialContent(checked);
                    if (!checked) {
                      setBrandOrganic(false);
                      setBrandContent(false);
                    }
                  }}
                />
                {commercialContent && (
                  <div className="ml-6 space-y-2 border-l border-zinc-200 pl-3">
                    <Check label="Your Brand" checked={brandOrganic} onChange={setBrandOrganic} />
                    <Check label="Branded Content" checked={brandContent} onChange={setBrandContent} />
                    {!brandOrganic && !brandContent && (
                      <p className="text-xs text-red-700">Choose at least one commercial-content type.</p>
                    )}
                  </div>
                )}
                <Check label="This includes realistic AI-generated or significantly AI-edited media" checked={isAigc} onChange={setIsAigc} />
              </fieldset>

              <label className="flex gap-3 rounded-xl bg-zinc-50 p-3 text-sm text-[#3f3f46]">
                <input type="checkbox" checked={musicConfirmed} onChange={(event) => setMusicConfirmed(event.target.checked)} className="mt-1" />
                <span>
                  By posting, you agree to TikTok&apos;s {brandContent ? "Branded Content Policy and " : ""}Music Usage Confirmation.
                </span>
              </label>

              {error && <p className="text-sm text-red-700">{error}</p>}
              <button type="button" onClick={() => void publish()} disabled={!privacy || !musicConfirmed || state === "submitting" || (commercialContent && !brandContent && !brandOrganic) || (brandContent && privacy === "SELF_ONLY")} className="min-h-11 w-full rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40">
                {state === "submitting" ? "Sending to TikTok…" : "Publish now"}
              </button>
              {brandContent && privacy === "SELF_ONLY" && <p className="text-xs text-red-700">TikTok does not allow branded content with Only you privacy.</p>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Check({ label, checked, disabled = false, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return <label className={`flex min-h-11 items-center gap-2 text-sm ${disabled ? "text-[#a1a1aa]" : "text-[#3f3f46]"}`}><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />{label}{disabled && " (unavailable)"}</label>;
}

function privacyLabel(value: string) {
  if (value === "SELF_ONLY") return "Only you";
  if (value === "MUTUAL_FOLLOW_FRIENDS") return "Friends";
  if (value === "FOLLOWER_OF_CREATOR") return "Followers";
  if (value === "PUBLIC_TO_EVERYONE") return "Everyone";
  return value.replaceAll("_", " ").toLowerCase();
}
