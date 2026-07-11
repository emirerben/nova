"use client";

import { useEffect, useRef, useState } from "react";
import { StableVideo } from "@/components/StableVideo";
import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  INTRO_SIZE_STEP,
  SEQUENCE_TEXT_LOCKED_HINT,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import { downloadVideo } from "@/lib/download-video";
import { variantFailureCopy } from "@/lib/variant-failure-copy";
import { IntroTextPreview } from "@/components/variant-editor/IntroTextPreview";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
import { LayoutPreviewCard } from "@/components/variant-editor/LayoutPreviewCard";
import { resolveIntroParams } from "@/components/variant-editor/resolve-intro-params";
import type { VariantEditSession } from "@/lib/variant-editor/useVariantEditSession";
import { isInstantEditEligible } from "@/lib/variant-editor/eligibility";
export const RERENDER_BASELINE_MS = 120_000;

// Re-exported from the shared module so existing `@/app/generative/VariantCard`
// importers (and the eligibility test) keep working after the lift to
// lib/variant-editor/eligibility.ts. Both the generative page and the plan flow
// import the canonical copy.
export { isInstantEditEligible };

export const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

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
  clipsOpen,
  onToggleClips,
  hideVideoWell = false,
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
  /** Whether the inline clips editor is open (managed by VariantTile). */
  clipsOpen?: boolean;
  onToggleClips?: () => void;
  /**
   * When true, skip the video well in both render paths. Used by the two-column
   * onboarding payoff, which renders the hero video separately on the LEFT.
   */
  hideVideoWell?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const rendering = variant.render_status === "rendering" || busy;
  const failed = variant.render_status === "failed";

  const instantEligible = !!editSession && isInstantEditEligible(variant);
  // Keep the live WYSIWYG preview mounted through the brief post-commit "Saved"
  // pulse (justSaved) too — without it the card would flash to the burned
  // output_url for a frame, defeating the instant feel (W5).
  const editActive =
    !!editSession && (editSession.isActive || editSession.justSaved);

  // Voiceover-synced typographic sequence (D6/D19): text is derived from the
  // transcript, so intro-text / highlight-word edits are locked (server 422s
  // them). Size nudge stays enabled; Classic remains the opt-out.
  const sequenceSynced = variant.intro_mode === "sequence";

  // StableVideo handles URL stability for both the base and output videos:
  // - base_video_path is the GCS key (stable across re-signs, changes only when
  //   a clip edit produces a new base render).
  // - render_finished_at advances when a new output render completes, signalling
  //   StableVideo to adopt the new output_url (no page refresh needed).
  // No manual pin refs or nonce state are required here.

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
  // Synced-sequence chip: light tone mirrors the "Edited cut" lime pill
  // (DESIGN.md §2 soft-pill role); dark/admin stays on the zinc badge scale.
  const syncedBadgeClass = tone === "light"
    ? "rounded-full border border-lime-200 bg-lime-50 px-2 py-0.5 text-xs text-lime-800"
    : "rounded-full border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300";
  const videoWellClass = tone === "light"
    ? "aspect-[9/16] w-full overflow-hidden rounded bg-zinc-100"
    : "aspect-[9/16] w-full overflow-hidden rounded bg-black";
  const renderingTextClass = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
  // D10 failure tone: quiet zinc, never a red wall.
  const failedTextClass = tone === "light" ? "text-[#3f3f46]" : "text-zinc-400";
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
          {/* Quiet "Saved" pulse takes precedence over the saving badge: a
              text edit settles to a brief lime pulse that recedes, never a
              blocking spinner. The live preview already shows the final look. */}
          {editSession.justSaved ? (
            <span className="motion-safe:animate-fade-up rounded bg-lime-50 px-2 py-0.5 text-xs font-medium text-lime-700">
              Saved
            </span>
          ) : (
            editSession.isSaving && (
              <span className="rounded bg-lime-100 px-2 py-0.5 text-xs text-lime-700">
                Saving…
              </span>
            )
          )}
        </div>

        {!hideVideoWell && (
          <div className={`relative ${videoWellClass}`}>
            {variant.base_video_url ? (
              <StableVideo
                src={variant.base_video_url}
                identity={variant.base_video_path ?? undefined}
                controls
                loop
                autoPlay
                muted
                playsInline
                className="h-full w-full object-contain"
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
              layout={variant.intro_layout === "cluster" ? "cluster" : "linear"}
              playToken={editSession.playToken}
            />
          </div>
        )}

        {editSession.isEditing ? (
          <EditToolbar
            session={editSession}
            styleSets={styleSets}
            fallbackSizePx={variant.intro_text_size_px ?? null}
            resolvedParams={introParams}
          />
        ) : editSession.isSaving ? (
          <p className="mt-3 text-xs text-[#71717a]">
            Applying your edits — this preview already shows the final look.
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className={cardClass}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className={badgeClass}>
            {TEXT_MODE_LABEL[variant.text_mode] ?? variant.text_mode}
            {variant.track_title ? ` · ${variant.track_title}` : " · Original audio"}
          </span>
          {sequenceSynced && (
            <span className={syncedBadgeClass} title={SEQUENCE_TEXT_LOCKED_HINT}>
              Editorial · synced
            </span>
          )}
        </div>
      </div>

      {!hideVideoWell && (
        <div className={videoWellClass}>
          {rendering ? (
            <div className={`flex h-full items-center justify-center text-sm ${renderingTextClass}`}>
              Rendering…
            </div>
          ) : failed ? (
            <div className={`flex h-full items-center justify-center px-3 text-center text-sm ${failedTextClass}`}>
              {variantFailureCopy(variant.error_class)}
            </div>
          ) : variant.output_url ? (
            <StableVideo
              src={variant.output_url}
              identity={variant.render_finished_at ?? undefined}
              controls
              className="h-full w-full object-contain"
            />
          ) : (
            <div className={`flex h-full items-center justify-center text-sm ${emptyTextClass}`}>
              No preview
            </div>
          )}
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {onToggleClips && (
          <button
            onClick={onToggleClips}
            disabled={rendering}
            className={
              tone === "light"
                ? `rounded-full border px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${clipsOpen ? "border-sky-400 bg-sky-50 text-sky-700" : "border-zinc-200 text-[#0c0c0e] hover:border-zinc-400"}`
                : `rounded-full border px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${clipsOpen ? "border-sky-500 bg-sky-900/30 text-sky-300" : "border-zinc-700 text-zinc-200 hover:border-zinc-500"}`
            }
          >
            {clipsOpen ? "Hide clips ▲" : "Edit clips ▼"}
          </button>
        )}
        {!rendering && !failed && variant.output_url && (
          <button
            onClick={() =>
              downloadVideo(variant.output_url!, `kria-${variant.variant_id}.mp4`)
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
              disabled={rendering || sequenceSynced}
              title={sequenceSynced ? SEQUENCE_TEXT_LOCKED_HINT : undefined}
              onClick={() => {
                const next = prompt("New intro text:", variant.intro_text ?? "");
                if (next && next.trim()) run(() => onRetext(next.trim()));
              }}
              className={btnClass}
            >
              Edit text
            </button>
            <button
              disabled={rendering || sequenceSynced}
              title={sequenceSynced ? SEQUENCE_TEXT_LOCKED_HINT : undefined}
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
          // Post-render layout pick, shown as two visual preview cards on a dark
          // inner tile. The editorial word-cluster only works on short hooks
          // (server enforces 3-6 words; the card pre-disables with a hint so the
          // user isn't bounced by a 422). Sequence-synced variants render
          // Editorial as the active state and bypass the word-count gate (the
          // server does too) — Classic stays clickable as the opt-out of sync.
          const layout =
            sequenceSynced || variant.intro_layout === "cluster" ? "cluster" : "linear";
          const words = (variant.intro_text ?? "").trim().split(/\s+/).filter(Boolean).length;
          const clusterBlocked = !sequenceSynced && (words < 3 || words > 6);
          const hookText = variant.intro_text ?? "";
          return (
            <div className="w-full">
              <div
                role="radiogroup"
                aria-label="Intro text layout"
                className="flex gap-2"
              >
                <LayoutPreviewCard
                  kind="classic"
                  text={hookText}
                  selected={layout === "linear"}
                  disabled={rendering || layout === "linear"}
                  title="Classic centered text"
                  onSelect={() => run(() => onChangeLayout("linear"))}
                />
                <LayoutPreviewCard
                  kind="editorial"
                  text={hookText}
                  selected={layout === "cluster"}
                  disabled={rendering || layout === "cluster" || clusterBlocked}
                  title={
                    sequenceSynced
                      ? "Editorial — text synced to this edit"
                      : clusterBlocked
                        ? "Editorial layout needs a 3-6 word hook — shorten the text first"
                        : "Editorial word-cluster — mixed sizes, magazine-style"
                  }
                  onSelect={() => run(() => onChangeLayout("cluster"))}
                />
              </div>
              {clusterBlocked && layout === "linear" && (
                <p className="mt-1.5 text-xs text-[#a1a1aa]">
                  Editorial needs a 3-6 word hook — shorten the text to unlock it.
                </p>
              )}
            </div>
          );
        })()}
        {tracks.length > 0 && variant.music_track_id !== null && (
          <select
            disabled={rendering}
            value=""
            onChange={(e) => {
              if (!e.target.value) return;
              run(() => onSwap(e.target.value));
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
