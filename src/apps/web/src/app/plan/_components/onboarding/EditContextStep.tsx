"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

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

      <Textarea
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        placeholder="e.g. hiking trip with friends last weekend"
        className="w-full rounded-xl border-[#e4e4e7] bg-[#ffffff] px-4 py-3 text-[#0c0c0e] placeholder:text-[#a1a1aa] focus-visible:ring-2 focus-visible:ring-lime-600 resize-none min-h-[80px]"
        rows={3}
      />

      <div className="flex gap-3">
        <Button
          type="button"
          onClick={() => onSubmit(topic)}
          disabled={!topic.trim()}
          className="h-auto min-h-[44px] flex-1 rounded-xl bg-lime-700 py-3 font-medium text-white hover:bg-lime-800 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:ring-lime-600"
        >
          Create video
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={onSkip}
          className="h-auto min-h-[44px] rounded px-4 text-sm text-[#71717a] hover:bg-transparent hover:text-[#0c0c0e] focus-visible:ring-lime-600"
        >
          Skip for now
        </Button>
      </div>
    </div>
  );
}
