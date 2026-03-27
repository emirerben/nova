"use client";

import { useCallback, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge as FlowEdge,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  modules,
  edges as configEdges,
  getDirectDependents,
  getChildCount,
  type Module,
} from "@/lib/architecture-config";
import { useModuleIssues, useActiveJobs } from "@/hooks/useArchitectureData";
import { ModuleNode, type ModuleNodeData } from "./ModuleNode";
import { DataFlowEdge } from "./DataFlowEdge";
import { ModuleDetailPanel } from "./ModuleDetailPanel";

// ---------------------------------------------------------------------------
// Node type registry
// ---------------------------------------------------------------------------

const nodeTypes = { moduleNode: ModuleNode };
const edgeTypes = { dataFlow: DataFlowEdge };

// ---------------------------------------------------------------------------
// Layout helpers — position L1 nodes in a pipeline flow
// ---------------------------------------------------------------------------

const L1_POSITIONS: Record<string, { x: number; y: number }> = {
  upload: { x: 0, y: 150 },
  processing: { x: 300, y: 150 },
  clips: { x: 600, y: 100 },
  templates: { x: 600, y: 250 },
  delivery: { x: 900, y: 150 },
  // Data stores at the bottom
  postgresql: { x: 200, y: 400 },
  redis: { x: 450, y: 400 },
  gcs: { x: 700, y: 400 },
};

function buildL2Nodes(parentId: string, children: Record<string, Module>): Node[] {
  const parentPos = L1_POSITIONS[parentId] ?? { x: 0, y: 0 };
  const entries = Object.values(children);
  return entries.map((child, i) => ({
    id: child.id,
    type: "moduleNode",
    position: {
      x: parentPos.x + (i % 3) * 240,
      y: parentPos.y + Math.floor(i / 3) * 120,
    },
    data: {
      module: child,
      issueCount: null,
      isActive: false,
      activeVisual: null,
      isHighlighted: false,
      isSelected: false,
      childCount: 0,
    } satisfies ModuleNodeData,
  }));
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ArchitectureMap() {
  const [selectedModuleId, setSelectedModuleId] = useState<string | null>(null);
  const [highlightedIds, setHighlightedIds] = useState<Set<string>>(new Set());
  const [expandedL1, setExpandedL1] = useState<string | null>(null);
  const [detailModule, setDetailModule] = useState<Module | null>(null);

  // Active jobs for live overlay
  const { data: activeJobs } = useActiveJobs();
  const activeJobMap = useMemo(() => {
    const map = new Map<string, { visual: "pulse" | "check" | "check-yellow" | "error" }>();
    if (activeJobs) {
      for (const job of activeJobs) {
        // Keep the most "active" visual per module
        if (!map.has(job.moduleId) || job.visual === "pulse") {
          map.set(job.moduleId, { visual: job.visual });
        }
      }
    }
    return map;
  }, [activeJobs]);

  // Build nodes based on current zoom level
  const builtNodes = useMemo((): Node[] => {
    if (expandedL1) {
      // L2 view: show children of expanded module
      const parent = modules[expandedL1];
      if (!parent?.children) return [];
      return buildL2Nodes(expandedL1, parent.children);
    }

    // L1 view: show all top-level modules
    return Object.values(modules).map((mod) => ({
      id: mod.id,
      type: "moduleNode",
      position: L1_POSITIONS[mod.id] ?? { x: 0, y: 0 },
      data: {
        module: mod,
        issueCount: null,
        isActive: !!activeJobMap.get(mod.id),
        activeVisual: activeJobMap.get(mod.id)?.visual ?? null,
        isHighlighted: highlightedIds.has(mod.id),
        isSelected: selectedModuleId === mod.id,
        childCount: getChildCount(mod.id),
      } satisfies ModuleNodeData,
    }));
  }, [expandedL1, activeJobMap, highlightedIds, selectedModuleId]);

  const [nodes, setNodes, onNodesChange] = useNodesState(builtNodes);

  // Keep nodes in sync with computed values
  useMemo(() => {
    setNodes(builtNodes);
  }, [builtNodes, setNodes]);

  // Build edges
  const builtEdges = useMemo((): FlowEdge[] => {
    if (expandedL1) {
      // L2 edges: connections between children
      const parent = modules[expandedL1];
      if (!parent?.children) return [];
      const childIds = new Set(Object.keys(parent.children));
      const childEdges: FlowEdge[] = [];
      Object.values(parent.children).forEach((child) => {
        child.dependsOn.forEach((depId) => {
          if (childIds.has(depId)) {
            childEdges.push({
              id: `${depId}-${child.id}`,
              source: depId,
              target: child.id,
              type: "dataFlow",
              data: { label: "", isActive: false },
              markerEnd: { type: MarkerType.ArrowClosed, color: "#4b5563" },
            });
          }
        });
      });
      return childEdges;
    }

    // L1 edges from config
    return configEdges.map((e) => ({
      id: `${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      type: "dataFlow",
      data: {
        label: e.label,
        isActive:
          highlightedIds.has(e.source) || highlightedIds.has(e.target),
      },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#4b5563" },
    }));
  }, [expandedL1, highlightedIds]);

  const [edges, setEdges, onEdgesChange] = useEdgesState(builtEdges);

  useMemo(() => {
    setEdges(builtEdges);
  }, [builtEdges, setEdges]);

  // Click handler
  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      const mod = modules[node.id];

      if (expandedL1) {
        // We're in L2 view — open detail panel
        const parent = modules[expandedL1];
        const childMod = parent?.children?.[node.id];
        if (childMod) {
          setDetailModule(childMod);
        }
        return;
      }

      // L1 view
      if (mod?.children && Object.keys(mod.children).length > 0) {
        // Expand to L2
        setExpandedL1(mod.id);
        setSelectedModuleId(null);
        setHighlightedIds(new Set());
      } else if (mod) {
        // Data store or module with no children — open detail panel
        setDetailModule(mod);
      }
    },
    [expandedL1]
  );

  // Right-click or shift-click for impact highlighting
  const onNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: Node) => {
      event.preventDefault();
      if (selectedModuleId === node.id) {
        // Toggle off
        setSelectedModuleId(null);
        setHighlightedIds(new Set());
      } else {
        setSelectedModuleId(node.id);
        const dependents = getDirectDependents(node.id);
        setHighlightedIds(new Set(dependents.map((d) => d.id)));
      }
    },
    [selectedModuleId]
  );

  // Click on empty space — clear selection
  const onPaneClick = useCallback(() => {
    setSelectedModuleId(null);
    setHighlightedIds(new Set());
  }, []);

  // Breadcrumb
  const breadcrumb = expandedL1
    ? `L1 Pipeline > ${modules[expandedL1]?.name ?? expandedL1}`
    : "L1 Pipeline";

  return (
    <div className="w-full h-full flex flex-col bg-gray-950">
      {/* Header bar */}
      <div className="h-12 flex items-center px-4 bg-gray-900 border-b border-gray-800 shrink-0">
        <span className="text-sm text-gray-400">Nova</span>
        <span className="text-gray-600 mx-2">·</span>
        <span className="text-sm font-medium text-gray-200">Architecture Map</span>
        <span className="text-gray-600 mx-2">·</span>
        {expandedL1 ? (
          <button
            onClick={() => {
              setExpandedL1(null);
              setDetailModule(null);
              setSelectedModuleId(null);
              setHighlightedIds(new Set());
            }}
            className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
          >
            ← Back to L1
          </button>
        ) : null}
        <span className="text-xs text-gray-500 ml-1">{breadcrumb}</span>

        {/* Right side: help hint */}
        <div className="ml-auto text-xs text-gray-600">
          Click to drill in · Right-click to highlight dependents · Esc to close
        </div>
      </div>

      {/* react-flow canvas */}
      <div className="flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          onNodeContextMenu={onNodeContextMenu}
          onPaneClick={onPaneClick}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.5}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          defaultEdgeOptions={{
            type: "dataFlow",
          }}
        >
          <Background color="#1f2937" gap={20} />
          <Controls
            showInteractive={false}
            className="!bg-gray-900 !border-gray-800 [&>button]:!bg-gray-900 [&>button]:!border-gray-800 [&>button]:!text-gray-400 [&>button:hover]:!bg-gray-800"
          />
        </ReactFlow>
      </div>

      {/* Footer legend */}
      <div className="h-8 flex items-center px-4 bg-gray-900 border-t border-gray-800 shrink-0 gap-4">
        <span className="flex items-center gap-1.5 text-[10px] text-gray-500">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          active
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-gray-500">
          <span className="w-2 h-2 rounded-full bg-gray-600" />
          idle
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-gray-500">
          <span className="w-2 h-2 rounded-full bg-amber-400" />
          has issues
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-gray-500">
          <span className="w-2 h-2 rounded-full bg-blue-950 border border-blue-800" />
          data store
        </span>
      </div>

      {/* Detail panel */}
      <ModuleDetailPanel
        module={detailModule}
        onClose={() => setDetailModule(null)}
      />
    </div>
  );
}
