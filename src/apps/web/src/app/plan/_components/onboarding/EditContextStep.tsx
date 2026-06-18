"use client";

import { useState } from "react";

export function EditContextStep({
  onSubmit,
  onSkip,
}: {
  onSubmit: (topic: string) => void;
  onSkip: () => void;
}) {
  const [topic, setTopic] = useState("");

  return (
    <div className="flex flex-col gap-6 px-4 py-8 max-w-lg mx-auto animate-fade-up">
      {/* Editorial Playfair question with lime left-border */}
      <div className="border-l-4 border-lime-600 pl-4">
        <p className="font-display text-2xl text-[#0c0c0e] leading-snug">
          What&apos;s this footage about?
        </p>
      </div>

      <textarea
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        placeholder="e.g. hiking trip with friends last weekend"
        className="w-full rounded-xl border border-[#e4e4e7] bg-[#fafaf8] px-4 py-3 text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:outline-none focus:ring-2 focus:ring-lime-600 resize-none min-h-[80px]"
        rows={3}
      />

      <div className="flex gap-3">
        <button
          onClick={() => onSubmit(topic)}
          disabled={!topic.trim()}
          className="flex-1 rounded-xl bg-lime-700 text-white py-3 font-medium hover:bg-lime-800 disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
        >
          Make my edit →
        </button>
        <button
          onClick={onSkip}
          className="px-4 text-sm text-[#71717a] hover:text-[#0c0c0e] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded min-h-[44px]"
        >
          skip
        </button>
      </div>
    </div>
  );
}
