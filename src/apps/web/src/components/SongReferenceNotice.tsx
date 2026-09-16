"use client";

import { useId, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { SongReference } from "@/lib/generative-api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export function SongReferenceNotice({ reference }: { reference?: SongReference | null }) {
  const [expanded, setExpanded] = useState(false);
  const [copyState, setCopyState] = useState("");
  const detailsId = useId();
  if (!reference || !Number.isFinite(reference.start_s) || !Number.isFinite(reference.end_s)
    || reference.start_s < 0 || reference.end_s <= reference.start_s) return null;
  const song = `${reference.title}${reference.artist ? ` — ${reference.artist}` : ""}`;
  const section = `${reference.start_s.toFixed(3)}–${reference.end_s.toFixed(3)} seconds`;
  const details = `Add ${song} in TikTok or Instagram. Use ${section}. Song audio is not included in this video.`;
  return (
    <div className="overflow-hidden rounded-lg border border-zinc-200 bg-white text-sm text-[#3f3f46]">
      <Button
        type="button"
        variant="ghost"
        className="flex min-h-11 w-full items-center justify-between gap-2 rounded-none px-3 py-2 text-left hover:bg-transparent"
        aria-expanded={expanded}
        aria-controls={detailsId}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="min-w-0 flex-1 truncate">
          <span className="font-medium">Add the song when posting</span>
          <span className="text-[#71717a]"> · {song}</span>
        </span>
        <ChevronDown
          aria-hidden="true"
          className={cn("size-4 shrink-0 text-[#71717a] transition-transform motion-reduce:transition-none", expanded && "rotate-180")}
        />
      </Button>
      {expanded && (
        <div id={detailsId} role="region" aria-label="Song details" className="border-t border-zinc-100 px-3 pb-2 pt-2">
          <p className="break-words">{song}</p>
          <p className="tabular-nums">Use {section} in TikTok or Instagram.</p>
          <p className="mt-1 text-xs text-[#71717a]">Song audio is not included in this video.</p>
          <Button type="button" variant="ghost" size="sm" className="mt-1" onClick={async () => {
            try {
              await navigator.clipboard.writeText(details);
              setCopyState("Copied");
            } catch {
              setCopyState("Couldn't copy. Select the song details above.");
            }
          }}>Copy song details</Button>
          <span className="ml-2 text-xs" role="status">{copyState}</span>
        </div>
      )}
    </div>
  );
}
