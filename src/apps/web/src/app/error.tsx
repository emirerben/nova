"use client";

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // Always log so users with DevTools open can paste the trace to support.
  useEffect(() => {
    console.error("[Kria] Unhandled error:", error);
  }, [error]);

  return (
    <div className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4" role="alert">
      <h1 className="text-xl font-semibold mb-2">This page couldn&apos;t load</h1>
      <p className="text-zinc-400 text-sm mb-6 text-center max-w-md">
        Your saved videos are safe. Reload this page, or return to your videos.
      </p>
      <div className="flex gap-3">
        <button
          onClick={reset}
          className="px-4 py-2 bg-white text-black rounded-lg text-sm font-medium hover:bg-zinc-200 transition-colors"
        >
          Reload page
        </button>
        <a
          href="/plan"
          className="px-4 py-2 border border-zinc-700 rounded-lg text-sm text-zinc-300 hover:border-zinc-500 transition-colors"
        >
          Go to My videos
        </a>
      </div>
      {error.digest && (
        <p className="text-zinc-600 text-xs mt-6 font-mono">Support reference: {error.digest}</p>
      )}
    </div>
  );
}
