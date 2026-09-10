"use client";

/**
 * Editor for a mixed-media "slide post" (ordered images + videos — a TikTok
 * photo post or an Instagram carousel, plans/024).
 *
 * Deliberately NOT built on the video editor's tabs/lanes (UnifiedTimeline,
 * CaptionEditor, ...) — none of that applies to a slides variant, and the
 * backend closes every one of those lanes for it (`editor_capabilities`
 * reports them all inert; `require_editable_variant` 422s a direct call).
 * This panel is the ONE sanctioned way to touch a slide post after Generate.
 *
 * Reorder uses the house pattern: HTML5 drag-and-drop plus "Move earlier/
 * later" buttons for keyboard/a11y parity (see _editor/ToolDrawer.tsx and
 * _editor/InspectorPanel.tsx) — no new dependency; the repo has no DnD
 * library.
 *
 * v1 scope note: "Add" offers already-uploaded, ready pool assets not yet
 * in the post. Uploading a brand-new file from inside this panel is
 * deferred — attach it via the item's Assets pool first, then add it here.
 */

import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  type PlanItem,
  type PlanItemVariant,
  type PlatformProfile,
  type PoolAsset,
  type SlideEdits,
  type SlideRef,
  composeSlidePost,
  getSlidePostBundleUrl,
  listPoolAssets,
  putSlidePostDraft,
} from "@/lib/plan-api";
import SlideEditModal from "./SlideEditModal";

const PLATFORM_LABELS: Record<PlatformProfile, string> = {
  tiktok_photo: "TikTok photo post",
  instagram_carousel: "Instagram carousel",
};

const PLATFORM_HINTS: Record<PlatformProfile, string> = {
  tiktok_photo: "Images only · 1–35 slides",
  instagram_carousel: "Photos + videos · 2–20 slides",
};

export type SlidePostPanelProps = {
  item: PlanItem;
  /** The rendered "slides" variant, if Generate has already run once. */
  variant: PlanItemVariant | null;
  /** Called after any successful mutation — the parent's polled fetcher owns
   *  `item`/`variant`, so this panel never holds its own copy of server truth. */
  onRefetch: () => void | Promise<void>;
};

function slideKey(ref: SlideRef): string {
  return ref.id;
}

export default function SlidePostPanel({ item, variant, onRefetch }: SlidePostPanelProps) {
  const draft = item.slide_post ?? null;
  const [slides, setSlides] = useState<SlideRef[]>(draft?.slides ?? []);
  const [coverIndex, setCoverIndex] = useState(draft?.cover_index ?? 0);
  const [caption, setCaption] = useState(draft?.caption ?? "");
  const [platformProfile, setPlatformProfile] = useState<PlatformProfile>(
    draft?.platform_profile ?? "tiktok_photo",
  );
  const [saving, setSaving] = useState(false);
  const [composing, setComposing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [poolAssets, setPoolAssets] = useState<PoolAsset[]>([]);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Re-sync local state when a fresh item lands (compose/rebuild finished).
  useEffect(() => {
    const next = item.slide_post ?? null;
    setSlides(next?.slides ?? []);
    setCoverIndex(next?.cover_index ?? 0);
    setCaption(next?.caption ?? "");
    setPlatformProfile(next?.platform_profile ?? "tiktok_photo");
  }, [item.id, item.slide_post]);

  useEffect(() => {
    listPoolAssets(item.id)
      .then((res) => setPoolAssets(res.assets))
      .catch(() => setPoolAssets([]));
  }, [item.id]);

  const inDraftIds = useMemo(() => new Set(slides.map((s) => s.asset_id)), [slides]);
  const availableAssets = poolAssets.filter(
    (a) => a.media_status === "ready" && !inDraftIds.has(a.id),
  );

  const isStale = draft != null && draft.rendered_version !== draft.version;
  const validation = variant?.slide_post?.validation ?? null;

  function previewUrlFor(slide: SlideRef): string | null {
    const rendered = variant?.slides?.find((s) => s.asset_id === slide.asset_id);
    const poolAsset = poolAssets.find((a) => a.id === slide.asset_id);
    return rendered?.preview_url ?? poolAsset?.display_url ?? null;
  }

  const editingSlide = editingIndex !== null ? slides[editingIndex] ?? null : null;

  async function save(next: {
    slides: SlideRef[];
    coverIndex: number;
    caption: string;
    platformProfile: PlatformProfile;
  }) {
    setSaving(true);
    setError(null);
    try {
      await putSlidePostDraft(item.id, {
        platform_profile: next.platformProfile,
        slides: next.slides,
        cover_index: next.coverIndex,
        caption: next.caption,
      });
      await onRefetch();
    } catch {
      setError("Couldn't save. Try again.");
    } finally {
      setSaving(false);
    }
  }

  function reorder(from: number, to: number) {
    if (from === to || from < 0 || to < 0 || from >= slides.length || to >= slides.length) return;
    const next = [...slides];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    const coverId = slides[coverIndex]?.id;
    const nextCoverIndex = coverId ? next.findIndex((s) => s.id === coverId) : 0;
    setSlides(next);
    setCoverIndex(Math.max(0, nextCoverIndex));
    void save({ slides: next, coverIndex: Math.max(0, nextCoverIndex), caption, platformProfile });
  }

  function removeSlide(index: number) {
    const next = slides.filter((_, i) => i !== index);
    const nextCoverIndex = Math.min(coverIndex, Math.max(0, next.length - 1));
    setSlides(next);
    setCoverIndex(nextCoverIndex);
    void save({ slides: next, coverIndex: nextCoverIndex, caption, platformProfile });
  }

  function addAsset(asset: PoolAsset) {
    const next = [...slides, { id: crypto.randomUUID(), asset_id: asset.id, kind: asset.kind }];
    setSlides(next);
    void save({ slides: next, coverIndex, caption, platformProfile });
  }

  async function saveSlideEdits(index: number, edits: SlideEdits | null) {
    const next = slides.map((s, i) => (i === index ? { ...s, edits } : s));
    setSlides(next);
    await save({ slides: next, coverIndex, caption, platformProfile });
  }

  function setCover(index: number) {
    setCoverIndex(index);
    void save({ slides, coverIndex: index, caption, platformProfile });
  }

  function commitCaption() {
    void save({ slides, coverIndex, caption, platformProfile });
  }

  function changeProfile(next: PlatformProfile) {
    setPlatformProfile(next);
    void save({ slides, coverIndex, caption, platformProfile: next });
  }

  async function compose() {
    setComposing(true);
    setError(null);
    try {
      await composeSlidePost(item.id, { platformProfile });
      await onRefetch();
    } catch {
      setError("Kria couldn't compose a draft. Try adding slides manually.");
    } finally {
      setComposing(false);
    }
  }

  async function exportBundle() {
    if (!variant) return;
    setError(null);
    try {
      const { url } = await getSlidePostBundleUrl(item.id, variant.variant_id);
      window.open(url, "_blank", "noopener,noreferrer");
    } catch {
      setError("Download isn't ready yet — fix the flagged slides or wait for the render to finish.");
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {(Object.keys(PLATFORM_LABELS) as PlatformProfile[]).map((profile) => (
            <Button
              key={profile}
              type="button"
              size="pill"
              variant={platformProfile === profile ? "secondary" : "outline"}
              onClick={() => changeProfile(profile)}
              disabled={saving}
              aria-pressed={platformProfile === profile}
              className="rounded-full"
              title={PLATFORM_HINTS[profile]}
            >
              {PLATFORM_LABELS[profile]}
            </Button>
          ))}
        </div>
        <Button type="button" variant="outline" onClick={compose} disabled={composing || saving}>
          {composing ? "Composing…" : "✨ Compose with Kria"}
        </Button>
      </div>

      {validation && validation.errors.length > 0 && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[13px] text-red-800">
          {validation.errors.map((e, i) => (
            <div key={i}>{e.message}</div>
          ))}
        </div>
      )}
      {isStale && (
        <p className="text-[12px] text-[#71717a]" role="status">
          Unsaved changes — the rendered post will update shortly.
        </p>
      )}
      {error && (
        <p className="text-[13px] text-red-700" role="alert">
          {error}
        </p>
      )}

      <div
        role="list"
        aria-label="Slides"
        className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4"
      >
        {slides.map((slide, index) => {
          const previewUrl = previewUrlFor(slide);
          const isCover = index === coverIndex;
          return (
            <div
              key={slideKey(slide)}
              role="listitem"
              draggable
              onDragStart={() => setDraggedId(slide.id)}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (!draggedId) return;
                const from = slides.findIndex((s) => s.id === draggedId);
                if (from >= 0) reorder(from, index);
                setDraggedId(null);
              }}
              onDragEnd={() => setDraggedId(null)}
              className={`group relative aspect-[3/4] overflow-hidden rounded-xl border bg-zinc-50 ${
                isCover ? "ring-2 ring-lime-600" : "border-zinc-200"
              }`}
            >
              {previewUrl ? (
                slide.kind === "video" ? (
                  // eslint-disable-next-line jsx-a11y/media-has-caption
                  <video
                    src={previewUrl}
                    muted
                    playsInline
                    className="absolute inset-0 h-full w-full object-cover"
                  />
                ) : (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={previewUrl}
                    alt={slide.alt ?? ""}
                    className="absolute inset-0 h-full w-full object-cover"
                  />
                )
              ) : (
                <div className="absolute inset-0 flex items-center justify-center text-[11px] text-[#a1a1aa]">
                  {slide.kind === "video" ? "Video" : "Photo"}
                </div>
              )}
              {isCover && (
                <span className="absolute left-1.5 top-1.5 rounded-full bg-lime-700 px-2 py-0.5 text-[10px] font-semibold text-white">
                  Cover
                </span>
              )}
              <Button
                type="button"
                size="sm"
                variant="secondary"
                aria-label={`Edit slide ${index + 1}`}
                disabled={saving}
                onClick={() => setEditingIndex(index)}
                className="absolute right-1.5 top-1.5 h-6 bg-white/90 px-2 text-[10px] font-semibold hover:bg-white"
              >
                Edit
              </Button>
              <div className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-1 bg-gradient-to-t from-black/70 to-transparent p-1.5">
                <div className="flex gap-1">
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="secondary"
                    aria-label={`Move slide ${index + 1} earlier`}
                    disabled={index === 0 || saving}
                    onClick={() => reorder(index, index - 1)}
                    className="h-6 w-6 bg-white/90 text-[11px] font-bold hover:bg-white"
                  >
                    ↑
                  </Button>
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="secondary"
                    aria-label={`Move slide ${index + 1} later`}
                    disabled={index === slides.length - 1 || saving}
                    onClick={() => reorder(index, index + 1)}
                    className="h-6 w-6 bg-white/90 text-[11px] font-bold hover:bg-white"
                  >
                    ↓
                  </Button>
                </div>
                <div className="flex gap-1">
                  {!isCover && (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      aria-label={`Set slide ${index + 1} as cover`}
                      disabled={saving}
                      onClick={() => setCover(index)}
                      className="h-6 bg-white/90 px-1.5 text-[10px] font-semibold hover:bg-white"
                    >
                      Cover
                    </Button>
                  )}
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="secondary"
                    aria-label={`Remove slide ${index + 1}`}
                    disabled={saving}
                    onClick={() => removeSlide(index)}
                    className="h-6 w-6 bg-white/90 text-[11px] font-bold text-red-700 hover:bg-white"
                  >
                    ✕
                  </Button>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {availableAssets.length > 0 && (
        <div>
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-[#71717a]">
            Add from your assets
          </p>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {availableAssets.map((asset) => (
              <Button
                key={asset.id}
                type="button"
                variant="ghost"
                onClick={() => addAsset(asset)}
                disabled={saving}
                className="relative h-20 w-16 shrink-0 overflow-hidden rounded-lg border border-zinc-200 p-0 hover:bg-transparent"
                aria-label="Add to post"
              >
                {asset.display_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={asset.display_url}
                    alt=""
                    className="absolute inset-0 h-full w-full object-cover"
                  />
                ) : null}
                <span className="absolute inset-0 flex items-center justify-center bg-black/20 text-lg font-bold text-white">
                  +
                </span>
              </Button>
            ))}
          </div>
        </div>
      )}

      <div>
        <label
          htmlFor="slide-post-caption"
          className="mb-1 block text-[11px] font-semibold uppercase tracking-[0.14em] text-[#71717a]"
        >
          Caption
        </label>
        <Textarea
          id="slide-post-caption"
          value={caption}
          onChange={(e) => setCaption(e.target.value)}
          onBlur={commitCaption}
          maxLength={2200}
          rows={3}
          placeholder="Write a caption for this post…"
        />
      </div>

      <div className="flex justify-end">
        <Button
          type="button"
          onClick={exportBundle}
          disabled={!variant || slides.length === 0 || (validation ? !validation.ok : false)}
        >
          Download
        </Button>
      </div>

      {editingSlide && editingIndex !== null && (
        <SlideEditModal
          open
          onOpenChange={(open) => {
            if (!open) setEditingIndex(null);
          }}
          slide={editingSlide}
          previewUrl={previewUrlFor(editingSlide)}
          onSave={(edits) => saveSlideEdits(editingIndex, edits)}
        />
      )}
    </div>
  );
}
