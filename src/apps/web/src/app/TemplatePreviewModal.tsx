"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { TemplateListItem } from "@/lib/api";
import {
  activeTileStore,
  getCachedPlaybackUrl,
  invalidatePlaybackUrl,
} from "@/lib/template-playback";

// Matches --modal-close-dur in globals.css. Keep in sync.
const MODAL_CLOSE_MS = 150;

interface Props {
  template: TemplateListItem | null;
  returnFocusTo: HTMLElement | null;
  onClose: () => void;
}

export default function TemplatePreviewModal({ template, returnFocusTo, onClose }: Props) {
  const router = useRouter();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // t-modal animation state. Driven by the `template` prop:
  //   null  → closed (or closing → then closed)
  //   non-null → open (after one rAF so CSS transition fires)
  const [animState, setAnimStateState] = useState<"closed" | "open" | "closing">("closed");
  const animStateRef = useRef<"closed" | "open" | "closing">("closed");
  function setAnimState(s: "closed" | "open" | "closing") {
    animStateRef.current = s;
    setAnimStateState(s);
  }

  // Snapshot of the last non-null template so we can still render content
  // while the close animation is playing (animState === "closing").
  const displayedTemplateRef = useRef<TemplateListItem | null>(null);
  if (template !== null) displayedTemplateRef.current = template;

  const closeTimerRef = useRef<ReturnType<typeof setTimeout>>();

  // Drive animState from the template prop.
  useEffect(() => {
    clearTimeout(closeTimerRef.current);
    if (template !== null) {
      // One rAF ensures the element is in the DOM before is-open flips,
      // which is what triggers the CSS scale+opacity transition.
      const raf = requestAnimationFrame(() => setAnimState("open"));
      return () => cancelAnimationFrame(raf);
    } else if (animStateRef.current === "open") {
      // Template cleared — play close animation, then unmount.
      setAnimState("closing");
      closeTimerRef.current = setTimeout(
        () => setAnimState("closed"),
        MODAL_CLOSE_MS,
      );
      return () => clearTimeout(closeTimerRef.current);
    }
  }, [template]);

  const isOpen = template !== null;

  // Lock scroll while the modal is open. Pause any active grid tile so the
  // poster underneath isn't fighting the modal video for bandwidth/audio.
  useEffect(() => {
    if (!isOpen) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    activeTileStore.setActive(null);
    return () => {
      document.body.style.overflow = prevOverflow;
    };
  }, [isOpen]);

  // Fetch the playback URL when the modal opens.
  useEffect(() => {
    if (!template) {
      setVideoUrl(null);
      setErrorMsg(null);
      return;
    }
    let cancelled = false;
    setIsLoading(true);
    setErrorMsg(null);
    getCachedPlaybackUrl(template.id)
      .then((url) => {
        if (!cancelled) {
          setVideoUrl(url);
          setIsLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setErrorMsg("Couldn't load preview.");
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [template]);

  // Keyboard: ESC closes; basic focus trap (Tab cycles within dialog).
  useEffect(() => {
    if (!isOpen) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "Tab" && dialogRef.current) {
        const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  // Move focus into the modal on open; restore to triggering tile on close.
  useEffect(() => {
    if (!isOpen) {
      returnFocusTo?.focus?.();
      return;
    }
    closeButtonRef.current?.focus();
  }, [isOpen, returnFocusTo]);

  function onVideoError() {
    const t = displayedTemplateRef.current;
    if (t) invalidatePlaybackUrl(t.id);
    setErrorMsg("Couldn't load preview.");
  }

  function onUseTemplate() {
    const t = displayedTemplateRef.current;
    if (!t) return;
    router.push(`/template/${t.id}`);
  }

  // Unmount entirely once the close animation is done.
  if (animState === "closed") return null;
  const displayedTemplate = displayedTemplateRef.current;
  if (!displayedTemplate) return null;

  const modalClass = animState === "open" ? "is-open" : animState === "closing" ? "is-closing" : "";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Preview: ${displayedTemplate.name}`}
      className={[
        "fixed inset-0 z-50 flex items-center justify-center backdrop-blur-sm p-4",
        "transition-[background-color,opacity] duration-[250ms]",
        animState === "open" ? "bg-black/80" : "bg-black/0",
      ].join(" ")}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {/* t-modal: scale-up from center on open, softer scale-down on close. */}
      <div
        ref={dialogRef}
        className={`t-modal ${modalClass} relative flex flex-col items-center max-w-md w-full`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="relative aspect-[9/16] w-full max-h-[85vh] bg-zinc-950 rounded-xl overflow-hidden shadow-2xl">
          {videoUrl && !errorMsg && (
            <video
              src={videoUrl}
              autoPlay
              controls
              loop
              playsInline
              onError={onVideoError}
              className="w-full h-full object-contain"
            />
          )}
          {isLoading && !errorMsg && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="w-8 h-8 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
            </div>
          )}
          {errorMsg && (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-zinc-400 text-sm gap-3 px-6 text-center">
              <p>{errorMsg}</p>
              <button
                type="button"
                onClick={onUseTemplate}
                className="px-4 py-2 bg-white text-black rounded-lg text-xs font-semibold hover:bg-zinc-200 transition-colors"
              >
                Use this template anyway
              </button>
            </div>
          )}
        </div>

        <div className="mt-4 flex items-center gap-2 w-full">
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            className="flex-1 py-2.5 rounded-lg border border-zinc-700 text-sm text-zinc-300 hover:border-zinc-500 hover:text-white transition-colors"
          >
            Close
          </button>
          <button
            type="button"
            onClick={onUseTemplate}
            className="flex-1 py-2.5 rounded-lg bg-white text-black text-sm font-semibold hover:bg-zinc-200 transition-colors"
          >
            Use this template
          </button>
        </div>

        <p className="mt-3 text-xs text-zinc-500 text-center">
          {displayedTemplate.name} · {Math.round(displayedTemplate.total_duration_s)}s
        </p>
      </div>
    </div>
  );
}
