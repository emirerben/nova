"use client";

/**
 * Minimal collapsible JSON tree viewer. No external deps.
 *
 * Used by the admin job-debug page to render agent input/output, pipeline
 * trace events, and assorted JSONB columns. Collapses by default beyond
 * `defaultDepth` so a 50-key object doesn't blow up the page on first paint.
 */

import { useState } from "react";

interface JsonTreeViewProps {
  value: unknown;
  /** Levels of nesting expanded by default. Defaults to 2. */
  defaultDepth?: number;
  /** Optional indent in pixels per nesting level. */
  indentPx?: number;
}

// Hard cap on recursion. JSONB blobs from agents / assembly plans are
// shallow in practice (depth 5-8), but a malformed or pathologically nested
// payload shouldn't blow the React render stack. Past this depth, render
// a placeholder.
const _MAX_DEPTH = 32;

export function JsonTreeView({
  value,
  defaultDepth = 2,
  indentPx = 14,
}: JsonTreeViewProps): JSX.Element {
  return (
    <div className="font-mono text-xs leading-relaxed text-zinc-300">
      <JsonNode value={value} depth={0} defaultDepth={defaultDepth} indentPx={indentPx} />
    </div>
  );
}

interface JsonNodeProps {
  value: unknown;
  depth: number;
  defaultDepth: number;
  indentPx: number;
  keyLabel?: string;
}

function JsonNode({
  value,
  depth,
  defaultDepth,
  indentPx,
  keyLabel,
}: JsonNodeProps): JSX.Element {
  const [open, setOpen] = useState(depth < defaultDepth);

  if (depth > _MAX_DEPTH) {
    return (
      <Leaf
        keyLabel={keyLabel}
        render={<span className="text-zinc-500">…[depth cap reached]</span>}
      />
    );
  }

  if (value === null) {
    return <Leaf keyLabel={keyLabel} render={<span className="text-zinc-500">null</span>} />;
  }
  if (typeof value === "string") {
    return (
      <Leaf
        keyLabel={keyLabel}
        render={<StringLeaf value={value} />}
      />
    );
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return (
      <Leaf
        keyLabel={keyLabel}
        render={<span className="text-emerald-400">{String(value)}</span>}
      />
    );
  }

  const isArray = Array.isArray(value);
  const entries: [string, unknown][] = isArray
    ? (value as unknown[]).map((v, i) => [String(i), v])
    : Object.entries(value as Record<string, unknown>);

  if (entries.length === 0) {
    return (
      <Leaf
        keyLabel={keyLabel}
        render={<span className="text-zinc-500">{isArray ? "[]" : "{}"}</span>}
      />
    );
  }

  return (
    <div style={{ paddingLeft: depth === 0 ? 0 : indentPx }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-zinc-500 hover:text-zinc-300 select-none"
      >
        {open ? "▾" : "▸"}{" "}
        {keyLabel !== undefined && (
          <span className="text-sky-300">{keyLabel}: </span>
        )}
        <span className="text-zinc-500">
          {isArray ? `Array(${entries.length})` : `Object(${entries.length})`}
        </span>
      </button>
      {open && (
        <div>
          {entries.map(([k, v]) => (
            <JsonNode
              key={k}
              keyLabel={k}
              value={v}
              depth={depth + 1}
              defaultDepth={defaultDepth}
              indentPx={indentPx}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Leaf({
  keyLabel,
  render,
}: {
  keyLabel: string | undefined;
  render: JSX.Element;
}): JSX.Element {
  return (
    <div style={{ paddingLeft: 14 }}>
      {keyLabel !== undefined && <span className="text-sky-300">{keyLabel}: </span>}
      {render}
    </div>
  );
}

const _STRING_PREVIEW_MAX = 200;

function StringLeaf({ value }: { value: string }): JSX.Element {
  const [open, setOpen] = useState(false);
  if (value.length <= _STRING_PREVIEW_MAX) {
    return <span className="text-amber-300 break-all">&quot;{value}&quot;</span>;
  }
  return (
    <span className="text-amber-300 break-all">
      &quot;
      {open ? value : value.slice(0, _STRING_PREVIEW_MAX)}
      {!open && "…"}
      &quot;
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="ml-2 text-xs text-blue-400 underline"
      >
        {open ? "collapse" : `show ${value.length - _STRING_PREVIEW_MAX} more chars`}
      </button>
    </span>
  );
}
