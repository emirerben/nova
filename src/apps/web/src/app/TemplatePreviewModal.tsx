"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { TemplateListItem } from "@/lib/api";
import {
  activeTileStore,
  getCachedPlaybackUrl,
  invalidatePlaybackUrl,
} from "@/lib/template-playback";

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
    if (template) invalidatePlaybackUrl(template.id);
    setErrorMsg("Couldn't load preview.");
  }

  function onUseTemplate() {
    if (!template) return;
    router.push(`/template/${template.id}`);
  }

  if (!template) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Preview: ${template.name}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="relative flex flex-col items-center max-w-md w-full"
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
          {template.name} · {Math.round(template.total_duration_s)}s
        </p>
      </div>
    </div>
  );
}
