"use client";

import { useState } from "react";

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
        <p className="font-display text-2xl text-[#0c0c0e]">Group your clips</p>
        <p className="text-sm text-[#71717a] mt-1">
          Select clips that go together, add a topic. Ungrouped clips each get their own edit.
        </p>
      </div>

      {/* Selectable clip grid — unassigned only */}
      {unassignedIndices.length > 0 && (
        <div className="grid grid-cols-3 gap-2">
          {unassignedIndices.map((i) => {
            const isSelected = selected.has(i);
            return (
              <button
                key={i}
                onClick={() => toggleSelect(i)}
                aria-pressed={isSelected}
                aria-label={`Clip ${i + 1}`}
                className={`relative aspect-[9/16] rounded-lg overflow-hidden border-2 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 ${
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
              </button>
            );
          })}
        </div>
      )}

      {/* Group selected button */}
      {selectedUnassigned.length > 0 && !editingGroup && (
        <button
          onClick={() => setEditingGroup(true)}
          className="w-full rounded-xl border border-lime-600 text-lime-700 py-3 font-medium hover:bg-lime-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
        >
          Group {selectedUnassigned.length} selected
        </button>
      )}

      {/* Inline topic input */}
      {editingGroup && (
        <div className="flex gap-2">
          <input
            value={topicDraft}
            onChange={(e) => setTopicDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && confirmGroup()}
            placeholder="What's this group about? (optional)"
            autoFocus
            className="flex-1 rounded-xl border border-[#e4e4e7] bg-[#fafaf8] px-4 py-3 text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:outline-none focus:ring-2 focus:ring-lime-600"
          />
          <button
            onClick={confirmGroup}
            className="px-5 rounded-xl bg-lime-700 text-white font-medium hover:bg-lime-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
          >
            Add
          </button>
        </div>
      )}

      {/* Groups list */}
      {groups.length > 0 && (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-[#71717a] font-medium uppercase tracking-wide">Groups</p>
          {groups.map((group) => (
            <div
              key={group.id}
              className="rounded-xl border border-[#e4e4e7] bg-[#fafaf8] px-4 py-3 flex items-start gap-3"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm text-[#0c0c0e] font-medium truncate">
                  {group.topic || <span className="text-[#a1a1aa] font-normal">No topic</span>}
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
              <button
                onClick={() => setGroups((prev) => prev.filter((g) => g.id !== group.id))}
                aria-label="Remove group"
                className="text-[#a1a1aa] hover:text-[#0c0c0e] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded px-2 min-h-[44px] flex-shrink-0"
              >
                ×
              </button>
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
        <button
          onClick={onBack}
          className="px-4 text-sm text-[#71717a] hover:text-[#0c0c0e] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded min-h-[44px]"
        >
          ← back
        </button>
        <button
          onClick={handleSubmit}
          className="flex-1 rounded-xl bg-lime-700 text-white py-3 font-medium hover:bg-lime-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
        >
          Make my edits →
        </button>
      </div>
    </div>
  );
}
