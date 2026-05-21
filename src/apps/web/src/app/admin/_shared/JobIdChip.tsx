"use client";

import { useEffect, useRef, useState } from "react";

interface JobIdChipProps {
  jobId: string;
  truncateChars?: number;
}

export function JobIdChip({
  jobId,
  truncateChars = 8,
}: JobIdChipProps): JSX.Element {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const label = jobId.slice(0, truncateChars);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  async function copyJobId() {
    await navigator.clipboard.writeText(jobId);
    setCopied(true);
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setCopied(false), 1500);
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <span title={jobId} className="font-mono text-xs text-zinc-300">
        {label}
      </span>
      <button
        type="button"
        aria-label="Copy job ID"
        onClick={copyJobId}
        className="inline-flex h-5 w-5 items-center justify-center rounded border border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-zinc-100"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          className="h-3.5 w-3.5"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="2"
        >
          <rect x="8" y="8" width="11" height="11" rx="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1" />
        </svg>
      </button>
      {copied && <span className="text-[10px] text-emerald-300">Copied</span>}
    </span>
  );
}
