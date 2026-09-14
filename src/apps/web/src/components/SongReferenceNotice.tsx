"use client";

import { useState } from "react";
import type { SongReference } from "@/lib/generative-api";
import { Button } from "@/components/ui/button";

export function SongReferenceNotice({ reference }: { reference?: SongReference | null }) {
  const [copyState, setCopyState] = useState("");
  if (!reference || !Number.isFinite(reference.start_s) || !Number.isFinite(reference.end_s)
    || reference.start_s < 0 || reference.end_s <= reference.start_s) return null;
  const song = `${reference.title}${reference.artist ? ` — ${reference.artist}` : ""}`;
  const section = `${reference.start_s.toFixed(3)}–${reference.end_s.toFixed(3)} seconds`;
  const details = `Add ${song} in TikTok or Instagram. Use ${section}. Song audio is not included in this video.`;
  return (
    <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-[#3f3f46]">
      <p className="font-medium">Add the song when posting</p>
      <p className="mt-1 break-words">{song}</p>
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
  );
}
