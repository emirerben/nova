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
    console.error("[Nova] Unhandled error:", error);
  }, [error]);

  const isDev = process.env.NODE_ENV !== "production";

  return (
    <div className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4">
      <h2 className="text-xl font-semibold mb-2">Something went wrong</h2>
      <p className="text-zinc-400 text-sm mb-6 text-center max-w-md">
        An unexpected error occurred. Please try again.
      </p>
      <div className="flex gap-3">
        <button
          onClick={reset}
          className="px-4 py-2 bg-white text-black rounded-lg text-sm font-medium hover:bg-zinc-200 transition-colors"
        >
          Try again
        </button>
        <a
          href="/"
          className="px-4 py-2 border border-zinc-700 rounded-lg text-sm text-zinc-300 hover:border-zinc-500 transition-colors"
        >
          Back to home
        </a>
      </div>
      {error.digest && (
        <p className="text-zinc-600 text-xs mt-6 font-mono">ref: {error.digest}</p>
      )}
      {isDev && error.message && (
        <pre className="text-red-400 text-xs mt-4 font-mono max-w-2xl whitespace-pre-wrap text-center">
          {error.message}
        </pre>
      )}
    </div>
  );
}
