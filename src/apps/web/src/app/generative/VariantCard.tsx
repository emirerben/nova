"use client";

import { useEffect, useRef, useState } from "react";
import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  INTRO_SIZE_STEP,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import { downloadVideo } from "@/lib/download-video";
import { IntroTextPreview } from "./IntroTextPreview";
import { EditToolbar } from "./EditToolbar";
import { resolveIntroParams } from "./resolve-intro-params";
import type { VariantEditSession } from "./useVariantEditSession";

export const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

/** Instant edit needs the text-free base video AND an editable text mode —
 * lyrics variants have neither (no cached base; lyric typography is set-driven).
 * Cluster intros (intro_layout === "cluster") are also excluded: the local DOM
 * preview only models the linear single-block layout, so cluster text edits go
 * through the legacy server-reburn controls (still fast — reuses the base). */
export function isInstantEditEligible(variant: GenerativeVariant): boolean {
  return (
    !!variant.base_video_url &&
    (variant.text_mode === "agent_text" || variant.text_mode === "none") &&
    variant.intro_layout !== "cluster"
  );
}

/**
 * One generative-edit variant: video preview + the re-render controls (edit text,
 * remove text, swap song, change style). Shared by the public generative page and
 * the admin generative detail page — both drive the same public endpoints, so the
 * card stays presentation-only and takes the actions as callbacks.
 *
 * `editSession` (optional) switches eligible variants to the instant editor: the
 * well plays the text-free base video under a live DOM text overlay, all edits
 * are local, and "Done" commits ONE combined /edit render. Callers that omit it
 * (admin) keep the legacy per-field controls byte-for-byte.
 *
 * D20: tone="light" renders on cream canvas. Admin omits tone → default dark.
 */
export function VariantCard({
  variant,
  tracks,
  styleSets,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onSetMix,
  onChangeLayout,
  tone = "dark",
  editSession,
}: {
  variant: GenerativeVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize?: (textSizePx: number) => Promise<void>;
  onSetMix?: (mix: number) => Promise<void>;
  onChangeLayout?: (layout: "linear" | "cluster") => Promise<void>;
  tone?: "dark" | "light";
  editSession?: VariantEditSession;
}) {
  const [busy, setBusy] = useState(false);
  const rendering = variant.render_status === "rendering" || busy;
  const failed = variant.render_status === "failed";

  const instantEligible = !!editSession && isInstantEditEligible(variant);
  const editActive = !!editSession && editSession.isActive;

  // Pin the base-video src for the whole session: every poll re-signs the URL
  // (new query string), and swapping <video src> restarts playback. On a media
  // error (expired signature in a very long session) fall forward to the
  // freshest signed URL from the latest poll.
  const baseSrcRef = useRef<string | null>(null);
  const [baseSrcNonce, setBaseSrcNonce] = useState(0);
  if (editActive && baseSrcRef.current === null && variant.base_video_url) {
    baseSrcRef.current = variant.base_video_url;
  }
  if (!editActive && baseSrcRef.current !== null) {
    baseSrcRef.current = null;
  }
  void baseSrcNonce; // re-render trigger only

  // Voice/footage mix for voiceover variants.
  const isVoiceover = variant.variant_id.startsWith("voiceover");
  const [mix, setMix] = useState<number>(variant.mix ?? 1);
  const mixTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    setMix(variant.mix ?? 1);
  }, [variant.mix]);
  useEffect(() => {
    return () => {
      if (mixTimer.current) clearTimeout(mixTimer.current);
    };
  }, []);
  const bedLabel = variant.music_track_id !== null ? "Music" : "Footage";
  const curPx =
    variant.text_mode === "agent_text" ? variant.intro_text_size_px : null;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  // Palette
  const cardClass = tone === "light"
    ? "rounded-lg border border-zinc-200 bg-white p-3"
    : "rounded-lg border border-zinc-800 bg-zinc-950 p-3";
  const badgeClass = tone === "light"
    ? "rounded bg-zinc-100 px-2 py-0.5 text-xs text-[#3f3f46]"
    : "rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300";
  const videoWellClass = tone === "light"
    ? "aspect-[9/16] w-full overflow-hidden rounded bg-zinc-100"
    : "aspect-[9/16] w-full overflow-hidden rounded bg-black";
  const renderingTextClass = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
  const failedTextClass = tone === "light" ? "text-red-600" : "text-red-300";
  const emptyTextClass = tone === "light" ? "text-[#71717a]" : "text-zinc-600";
  const btnClass = tone === "light"
    ? "rounded border border-zinc-200 px-2 py-1 text-xs text-[#3f3f46] disabled:opacity-40"
    : "rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40";
  const sizeControlClass = tone === "light"
    ? "flex items-center overflow-hidden rounded border border-zinc-200"
    : "flex items-center overflow-hidden rounded border border-zinc-700";
  const sizeBtnClass = tone === "light"
    ? "px-2.5 py-1 text-xs text-[#3f3f46] hover:bg-zinc-100 disabled:opacity-40"
    : "px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40";
  const sizeDivClass = tone === "light"
    ? "select-none border-x border-zinc-200 px-2 py-1 text-xs tabular-nums text-[#71717a]"
    : "select-none border-x border-zinc-700 px-2 py-1 text-xs tabular-nums text-zinc-500";
  const selectClass = tone === "light"
    ? "rounded border border-zinc-200 bg-white px-2 py-1 text-xs text-[#3f3f46] disabled:opacity-40"
    : "rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40";
  const mixLabelClass = tone === "light"
    ? "mb-1 flex items-center justify-between text-xs text-[#71717a]"
    : "mb-1 flex items-center justify-between text-xs text-zinc-400";
  const mixPctClass = tone === "light" ? "tabular-nums text-[#71717a]" : "tabular-nums text-zinc-500";
  const sliderAccent = tone === "light" ? "accent-lime-600" : "accent-white";

  // ── Instant edit mode ──────────────────────────────────────────────────────
  // The well plays the text-free base (final audio mix) under the live DOM
  // overlay; every toolbar change updates the preview at 0 latency. While a
  // committed render runs, the preview stays up with a "Saving…" badge — the
  // user never stares at a "Rendering…" placeholder for a text tweak.
  if (editActive && editSession) {
    const introParams = resolveIntroParams(variant, styleSets, editSession.draft);
    return (
      <div className={cardClass}>
        <div className="mb-2 flex items-center justify-between">
          <span className={badgeClass}>
            {TEXT_MODE_LABEL[variant.text_mode] ?? variant.text_mode}
            {variant.track_title ? ` · ${variant.track_title}` : " · Original audio"}
          </span>
          {editSession.isSaving && (
            <span className="rounded bg-lime-100 px-2 py-0.5 text-xs text-lime-700">
              Saving…
            </span>
          )}
        </div>

        <div className={`relative ${videoWellClass}`}>
          {baseSrcRef.current ? (
            <video
              src={baseSrcRef.current}
              controls
              loop
              autoPlay
              muted
              playsInline
              className="h-full w-full object-contain"
              onError={() => {
                // Expired signature mid-session → fall forward to the freshest
                // signed URL the poll delivered.
                if (
                  variant.base_video_url &&
                  baseSrcRef.current !== variant.base_video_url
                ) {
                  baseSrcRef.current = variant.base_video_url;
                  setBaseSrcNonce((n) => n + 1);
                }
              }}
            />
          ) : (
            <div className={`flex h-full items-center justify-center text-sm ${emptyTextClass}`}>
              No preview
            </div>
          )}
          <IntroTextPreview
            params={introParams}
            editable={editSession.isEditing}
            onTextChange={editSession.setText}
          />
        </div>

        {editSession.isEditing ? (
          <EditToolbar
            session={editSession}
            styleSets={styleSets}
            fallbackSizePx={variant.intro_text_size_px ?? null}
          />
        ) : (
          <p className="mt-3 text-xs text-[#71717a]">
            Saving your edits — this preview already shows the final look.
          </p>
        )}
      </div>
    );
  }

  return (
    <div className={cardClass}>
      <div className="mb-2 flex items-center justify-between">
        <span className={badgeClass}>
          {TEXT_MODE_LABEL[variant.text_mode] ?? variant.text_mode}
          {variant.track_title ? ` · ${variant.track_title}` : " · Original audio"}
        </span>
      </div>

      <div className={videoWellClass}>
        {rendering ? (
          <div className={`flex h-full items-center justify-center text-sm ${renderingTextClass}`}>
            Rendering…
          </div>
        ) : failed ? (
          <div className={`flex h-full items-center justify-center px-3 text-center text-sm ${failedTextClass}`}>
            {variant.error ?? "Render failed"}
          </div>
        ) : variant.output_url ? (
          <video src={variant.output_url} controls className="h-full w-full object-contain" />
        ) : (
          <div className={`flex h-full items-center justify-center text-sm ${emptyTextClass}`}>
            No preview
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {!rendering && !failed && variant.output_url && (
          <button
            onClick={() =>
              downloadVideo(variant.output_url!, `nova-${variant.variant_id}.mp4`)
            }
            className={btnClass}
          >
            Download
          </button>
        )}
        {instantEligible && editSession ? (
          // Instant editor entry point — supersedes the prompt()-based text
          // controls below for eligible variants (one batched render on Done).
          <button
            disabled={rendering}
            onClick={editSession.enterEdit}
            className={btnClass}
          >
            Edit text &amp; style
          </button>
        ) : (
          <>
            <button
              disabled={rendering}
              onClick={() => {
                const next = prompt("New intro text:", variant.intro_text ?? "");
                if (next && next.trim()) run(() => onRetext(next.trim()));
              }}
              className={btnClass}
            >
              Edit text
            </button>
            <button
              disabled={rendering}
              onClick={() => run(onRemoveText)}
              className={btnClass}
            >
              Remove text
            </button>
          </>
        )}
        {!instantEligible && onResize && curPx != null && (
          <div className={sizeControlClass}>
            <button
              disabled={rendering || curPx <= INTRO_SIZE_MIN}
              onClick={() =>
                run(() => onResize(Math.max(INTRO_SIZE_MIN, curPx - INTRO_SIZE_STEP)))
              }
              aria-label="Smaller intro text"
              className={sizeBtnClass}
            >
              A−
            </button>
            <span
              title={
                variant.intro_size_source === "user"
                  ? `Your size · ${curPx}px`
                  : `Auto-sized · ${curPx}px`
              }
              className={sizeDivClass}
            >
              {variant.intro_size_source === "user" ? `${curPx}` : `${curPx} auto`}
            </span>
            <button
              disabled={rendering || curPx >= INTRO_SIZE_MAX}
              onClick={() =>
                run(() => onResize(Math.min(INTRO_SIZE_MAX, curPx + INTRO_SIZE_STEP)))
              }
              aria-label="Bigger intro text"
              className={sizeBtnClass}
            >
              A+
            </button>
          </div>
        )}
        {!instantEligible && styleSets.length > 0 && (
          <select
            disabled={rendering}
            value={variant.style_set_id ?? ""}
            onChange={(e) => {
              if (e.target.value && e.target.value !== variant.style_set_id) {
                run(() => onChangeStyle(e.target.value));
              }
            }}
            className={selectClass}
          >
            <option value="" disabled>
              Style…
            </option>
            {styleSets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        )}
        {onChangeLayout && variant.text_mode === "agent_text" && (() => {
          // Post-render layout pick. The editorial word-cluster only works on
          // short hooks (server enforces 3-6 words; the chip pre-disables with
          // a hint so the user isn't bounced by a 422).
          const layout = variant.intro_layout === "cluster" ? "cluster" : "linear";
          const words = (variant.intro_text ?? "").trim().split(/\s+/).filter(Boolean).length;
          const clusterBlocked = words < 3 || words > 6;
          return (
            <div className={sizeControlClass} role="group" aria-label="Intro text layout">
              <button
                disabled={rendering || layout === "linear"}
                onClick={() => run(() => onChangeLayout("linear"))}
                title="Classic centered text"
                className={`${sizeBtnClass} ${layout === "linear" ? "font-semibold underline" : ""}`}
              >
                Classic
              </button>
              <button
                disabled={rendering || layout === "cluster" || clusterBlocked}
                onClick={() => run(() => onChangeLayout("cluster"))}
                title={
                  clusterBlocked
                    ? "Editorial layout needs a 3-6 word hook — shorten the text first"
                    : "Editorial word-cluster — mixed sizes, magazine-style"
                }
                className={`${sizeBtnClass} ${layout === "cluster" ? "font-semibold underline" : ""}`}
              >
                Editorial
              </button>
            </div>
          );
        })()}
        {tracks.length > 0 && variant.music_track_id !== null && (
          <select
            disabled={rendering}
            value=""
            onChange={(e) => {
              if (e.target.value) run(() => onSwap(e.target.value));
            }}
            className={selectClass}
          >
            <option value="">Swap song…</option>
            {tracks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title}
              </option>
            ))}
          </select>
        )}
      </div>

      {isVoiceover && onSetMix && (
        <div className="mt-3">
          <div className={mixLabelClass}>
            <label htmlFor={`mix-${variant.variant_id}`}>Voice / {bedLabel}</label>
            <span className={mixPctClass}>{Math.round(mix * 100)}% voice</span>
          </div>
          <input
            id={`mix-${variant.variant_id}`}
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={mix}
            disabled={rendering}
            aria-label={`Voice versus ${bedLabel.toLowerCase()} mix`}
            onChange={(e) => {
              const next = Number(e.target.value);
              setMix(next);
              if (mixTimer.current) clearTimeout(mixTimer.current);
              mixTimer.current = setTimeout(() => {
                run(() => onSetMix(next));
              }, 600);
            }}
            className={`w-full ${sliderAccent} disabled:opacity-40`}
          />
        </div>
      )}
    </div>
  );
}
