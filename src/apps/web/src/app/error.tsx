"use client";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
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
    </div>
  );
}
