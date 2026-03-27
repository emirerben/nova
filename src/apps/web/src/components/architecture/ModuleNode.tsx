"use client";

import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { Module } from "@/lib/architecture-config";

/**
 * Module card layout (left-aligned, not centered):
 * ┌─────────────────────────────────┐
 * │ Processing          ●3 issues   │
 * │ Video analysis pipeline         │
 * │ 5 files · 3 sub-modules        │
 * └─────────────────────────────────┘
 */

export interface ModuleNodeData {
  module: Module;
  issueCount: number | null; // null = loading/error
  isActive: boolean;
  activeVisual: "pulse" | "check" | "check-yellow" | "error" | null;
  isHighlighted: boolean;
  isSelected: boolean;
  childCount: number;
}

function IssueBadge({ count }: { count: number | null }) {
  if (count === null) {
    return (
      <span className="ml-auto text-xs font-medium bg-gray-800 text-gray-600 px-1.5 py-0.5 rounded">
        ...
      </span>
    );
  }
  if (count === 0) {
    return (
      <span className="ml-auto text-xs font-medium bg-gray-800 text-gray-600 px-1.5 py-0.5 rounded">
        0
      </span>
    );
  }
  return (
    <span className="ml-auto text-xs font-medium bg-amber-500/20 text-amber-400 px-1.5 py-0.5 rounded">
      {count}
    </span>
  );
}

function ActivityIndicator({
  visual,
}: {
  visual: "pulse" | "check" | "check-yellow" | "error" | null;
}) {
  if (!visual) return null;

  if (visual === "pulse") {
    return (
      <span className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-emerald-400 animate-[glow_2s_ease-in-out_infinite]" />
    );
  }
  if (visual === "check") {
    return (
      <span className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-emerald-500" />
    );
  }
  if (visual === "check-yellow") {
    return (
      <span className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-yellow-500" />
    );
  }
  if (visual === "error") {
    return (
      <span className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-red-500" />
    );
  }
  return null;
}

function ModuleNodeInner({ data }: NodeProps & { data: ModuleNodeData }) {
  const { module, issueCount, activeVisual, isHighlighted, isSelected, childCount } =
    data;
  const isDataStore = module.isDataStore;

  const baseBg = isDataStore
    ? "bg-blue-950 border-blue-800"
    : "bg-gray-900 border-gray-800";

  const highlightRing = isSelected
    ? "ring-2 ring-blue-500"
    : isHighlighted
    ? "ring-2 ring-blue-400 bg-blue-950/20"
    : "";

  const pulseStyle =
    activeVisual === "pulse"
      ? "shadow-lg shadow-emerald-400/20 ring-2 ring-emerald-400"
      : "";

  return (
    <div
      className={`
        relative border rounded-lg p-3 min-w-[200px] max-w-[220px]
        transition-all duration-200 cursor-pointer
        hover:border-gray-600
        ${baseBg} ${highlightRing} ${pulseStyle}
      `}
    >
      <ActivityIndicator visual={activeVisual} />

      <Handle type="target" position={Position.Left} className="!bg-gray-600 !w-2 !h-2 !border-0" />
      <Handle type="source" position={Position.Right} className="!bg-gray-600 !w-2 !h-2 !border-0" />

      {/* Row 1: Name + issue badge */}
      <div className="flex items-center gap-2">
        {isDataStore && (
          <span className="text-blue-400 text-xs">◆</span>
        )}
        <span className="text-sm font-semibold text-gray-100 truncate">
          {module.name}
        </span>
        <IssueBadge count={issueCount} />
      </div>

      {/* Row 2: Description */}
      <p className="text-xs text-gray-400 mt-1 truncate">
        {module.description}
      </p>

      {/* Row 3: Metadata */}
      <p className="text-xs text-gray-500 mt-1">
        {module.files.length} file{module.files.length !== 1 ? "s" : ""}
        {childCount > 0 && ` · ${childCount} sub-module${childCount !== 1 ? "s" : ""}`}
      </p>
    </div>
  );
}

export const ModuleNode = memo(ModuleNodeInner);
