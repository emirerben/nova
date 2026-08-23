"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export interface ClipItem {
  gcsPath: string;
  objectUrl: string;
}

interface ClipGroup {
  id: string;
  clipIndices: number[];
  topic: string;
}

export function ClipGroupStep({
  clips,
  onSubmit,
  onBack,
}: {
  clips: ClipItem[];
  onSubmit: (groups: { clips: string[]; topic: string }[]) => void;
  onBack: () => void;
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [groups, setGroups] = useState<ClipGroup[]>([]);
  const [topicDraft, setTopicDraft] = useState("");
  const [editingGroup, setEditingGroup] = useState(false);

  const assignedIndices = new Set(groups.flatMap((g) => g.clipIndices));
  const unassignedIndices = clips.map((_, i) => i).filter((i) => !assignedIndices.has(i));
  const selectedUnassigned = Array.from(selected).filter((i) => !assignedIndices.has(i));

  function toggleSelect(idx: number) {
    if (assignedIndices.has(idx)) return;
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(idx) ? next.delete(idx) : next.add(idx);
      return next;
    });
  }

  function confirmGroup() {
    if (selectedUnassigned.length === 0) return;
    setGroups((prev) => [
      ...prev,
      { id: `g-${Date.now()}`, clipIndices: selectedUnassigned, topic: topicDraft },
    ]);
    setSelected(new Set());
    setTopicDraft("");
    setEditingGroup(false);
  }

  function handleSubmit() {
    const result: { clips: string[]; topic: string }[] = groups.map((g) => ({
      clips: g.clipIndices.map((i) => clips[i].gcsPath),
      topic: g.topic,
    }));
    // ungrouped clips each get their own solo edit
    for (const idx of unassignedIndices) {
      result.push({ clips: [clips[idx].gcsPath], topic: "" });
    }
    onSubmit(result);
  }

  return (
    <div className="flex flex-col gap-6 px-4 py-8 max-w-lg mx-auto animate-fade-up">
      <div className="border-l-4 border-lime-600 pl-4">
        <p className="font-display text-2xl text-[#0c0c0e]">Group clips by story</p>
        <p className="text-sm text-[#71717a] mt-1">
          Select clips that belong in the same video. Clips you leave ungrouped become separate videos.
        </p>
      </div>

      {/* Selectable clip grid — unassigned only */}
      {unassignedIndices.length > 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {unassignedIndices.map((i) => {
            const isSelected = selected.has(i);
            return (
              <Button
                key={i}
                type="button"
                variant="ghost"
                onClick={() => toggleSelect(i)}
                aria-pressed={isSelected}
                aria-label={`Clip ${i + 1}`}
                className={`relative h-auto w-full aspect-[9/16] rounded-lg overflow-hidden border-2 p-0 hover:bg-transparent focus-visible:ring-lime-600 ${
                  isSelected ? "border-lime-600" : "border-transparent"
                }`}
              >
                <video
                  src={clips[i].objectUrl}
                  className="w-full h-full object-cover"
                  muted
                  playsInline
                />
                {isSelected && (
                  <div className="absolute inset-0 bg-lime-600/20 flex items-end justify-end p-1.5">
                    <span className="w-5 h-5 rounded-full bg-lime-600 text-white text-xs flex items-center justify-center font-bold">
                      ✓
                    </span>
                  </div>
                )}
              </Button>
            );
          })}
        </div>
      )}

      {/* Group selected button */}
      {selectedUnassigned.length > 0 && !editingGroup && (
        <Button
          type="button"
          variant="outline"
          onClick={() => setEditingGroup(true)}
          className="h-auto min-h-[44px] w-full rounded-xl border-lime-600 py-3 font-medium text-lime-700 hover:bg-lime-50 focus-visible:ring-lime-600"
        >
          Group {selectedUnassigned.length} selected
        </Button>
      )}

      {/* Inline topic input */}
      {editingGroup && (
        <div className="flex gap-2">
          <Input
            value={topicDraft}
            onChange={(e) => setTopicDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && confirmGroup()}
            placeholder="What is this group about? (optional)"
            autoFocus
            className="h-auto flex-1 rounded-xl border-[#e4e4e7] bg-[#ffffff] px-4 py-3 text-[#0c0c0e] placeholder:text-[#a1a1aa] focus-visible:ring-2 focus-visible:ring-lime-600"
          />
          <Button
            type="button"
            onClick={confirmGroup}
            className="h-auto min-h-[44px] rounded-xl bg-lime-700 px-5 font-medium text-white hover:bg-lime-800 focus-visible:ring-lime-600"
          >
            Add
          </Button>
        </div>
      )}

      {/* Groups list */}
      {groups.length > 0 && (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-[#71717a] font-medium uppercase tracking-wide">Groups</p>
          {groups.map((group) => (
            <div
              key={group.id}
              className="rounded-xl border border-[#e4e4e7] bg-[#ffffff] px-4 py-3 flex items-start gap-3"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm text-[#0c0c0e] font-medium truncate">
                  {group.topic || <span className="text-[#a1a1aa] font-normal">No topic yet</span>}
                </p>
                <p className="text-xs text-[#71717a] mt-0.5">
                  {group.clipIndices.length} clip{group.clipIndices.length !== 1 ? "s" : ""}
                </p>
                {/* Thumbnail strip */}
                <div className="flex gap-1 mt-2">
                  {group.clipIndices.slice(0, 5).map((idx) => (
                    <div key={idx} className="w-7 aspect-[9/16] rounded overflow-hidden bg-[#e4e4e7] flex-shrink-0">
                      <video src={clips[idx].objectUrl} className="w-full h-full object-cover" muted playsInline />
                    </div>
                  ))}
                  {group.clipIndices.length > 5 && (
                    <div className="w-7 aspect-[9/16] rounded bg-[#e4e4e7] flex items-center justify-center text-xs text-[#71717a] flex-shrink-0">
                      +{group.clipIndices.length - 5}
                    </div>
                  )}
                </div>
              </div>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setGroups((prev) => prev.filter((g) => g.id !== group.id))}
                aria-label="Remove group"
                className="h-auto min-h-[44px] w-auto flex-shrink-0 rounded px-2 text-[#a1a1aa] hover:bg-transparent hover:text-[#0c0c0e] focus-visible:ring-lime-600"
              >
                ×
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Ungrouped indicator */}
      {unassignedIndices.length > 0 && (
        <div className="rounded-xl border border-dashed border-[#e4e4e7] px-4 py-3">
          <p className="text-xs text-[#71717a]">
            {unassignedIndices.length} ungrouped clip{unassignedIndices.length !== 1 ? "s" : ""} —
            each gets its own edit
          </p>
        </div>
      )}

      <div className="flex gap-3">
        <Button
          type="button"
          variant="ghost"
          onClick={onBack}
          className="h-auto min-h-[44px] rounded px-4 text-sm text-[#71717a] hover:bg-transparent hover:text-[#0c0c0e] focus-visible:ring-lime-600"
        >
          Back
        </Button>
        <Button
          type="button"
          onClick={handleSubmit}
          className="h-auto min-h-[44px] flex-1 rounded-xl bg-lime-700 py-3 font-medium text-white hover:bg-lime-800 focus-visible:ring-lime-600"
        >
          Create videos
        </Button>
      </div>
    </div>
  );
}
