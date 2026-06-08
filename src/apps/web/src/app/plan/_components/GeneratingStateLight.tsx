"use client";
import { useEffect, useState } from "react";
import { formatElapsed } from "@/components/progress/logic";

interface GeneratingStateLightProps {
  /** "Building your {n} days" eyebrow line — pass the horizon number */
  horizonDays?: number;
  /** Label prefix override (default: "Building your {n} days") */
  label?: string;
}

export function GeneratingStateLight({
  horizonDays,
  label,
}: GeneratingStateLightProps) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const eyebrow = label ?? (horizonDays ? `Building your ${horizonDays} days` : "Setting up your persona");

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-col items-center gap-6 py-16 text-center"
    >
      {/* Lime pulse */}
      <span className="relative flex h-3 w-3">
        <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60" />
        <span className="relative inline-flex h-3 w-3 rounded-full bg-lime-700" />
      </span>

      {/* Eyebrow */}
      <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-lime-700">
        {eyebrow}
      </p>

      {/* Shimmer placeholder */}
      <div className="mt-4 w-full max-w-[320px] space-y-3">
        <div className="motion-safe:animate-pulse h-3 rounded-full bg-zinc-200" />
        <div className="motion-safe:animate-pulse h-3 w-4/5 rounded-full bg-zinc-200" />
        <div className="motion-safe:animate-pulse h-3 w-3/5 rounded-full bg-zinc-200" />
      </div>

      {/* Elapsed */}
      <p className="text-[13px] text-[#a1a1aa]">{formatElapsed(elapsed * 1000)}</p>
    </div>
  );
}
