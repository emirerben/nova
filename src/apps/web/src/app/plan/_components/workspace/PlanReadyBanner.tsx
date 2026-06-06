"use client";
import { useEffect, useState } from "react";

interface PlanReadyBannerProps {
  horizonDays: number;
  /** Fire only once per session (parent passes true when in-session flip detected) */
  show: boolean;
  /** Called after the banner auto-dismisses so the parent can reset the flag */
  onDismiss?: () => void;
}

export function PlanReadyBanner({ horizonDays, show, onDismiss }: PlanReadyBannerProps) {
  const [visible, setVisible] = useState(show);
  useEffect(() => {
    if (!show) return;
    setVisible(true);
    const t = setTimeout(() => {
      setVisible(false);
      onDismiss?.();
    }, 4000);
    return () => clearTimeout(t);
  }, [show, onDismiss]);

  if (!visible) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="mb-4 flex items-center gap-3 rounded-2xl border border-lime-200 bg-lime-50 px-5 py-4"
    >
      <span className="text-lime-700">✓</span>
      <p className="font-display text-[15px] italic text-[#3f3f46]">
        Your {horizonDays} days are ready.
      </p>
    </div>
  );
}
