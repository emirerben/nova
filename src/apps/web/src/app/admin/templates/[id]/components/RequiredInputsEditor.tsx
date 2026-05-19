"use client";

import { useState, useCallback } from "react";
import type { RequiredInput } from "@/lib/admin-api";

/**
 * Typed editor for VideoTemplate.required_inputs (the keys/labels the upload UI
 * collects from end users). Wholesale-replaces the list on save — caller passes
 * `value` and gets the next array via `onChange`. Validation:
 *  - non-empty key + label (also enforced server-side)
 *  - unique keys (also enforced server-side; we surface inline)
 *  - max_length: 1..200
 */
export function RequiredInputsEditor({
  value,
  onChange,
  disabled,
}: {
  value: RequiredInput[];
  onChange: (next: RequiredInput[]) => void;
  disabled?: boolean;
}) {
  const [localError, setLocalError] = useState<string | null>(null);

  const update = useCallback(
    (next: RequiredInput[]) => {
      // Surface a duplicate-key warning inline so admins don't bounce off a
      // 422 at save time; backend still enforces this.
      const seen = new Set<string>();
      let dup: string | null = null;
      for (const entry of next) {
        const k = entry.key.trim();
        if (!k) continue;
        if (seen.has(k)) {
          dup = k;
          break;
        }
        seen.add(k);
      }
      setLocalError(dup ? `Duplicate key: ${dup}` : null);
      onChange(next);
    },
    [onChange],
  );

  const addRow = () => {
    update([
      ...value,
      { key: "", label: "", placeholder: "", max_length: 50, required: false },
    ]);
  };

  const removeRow = (idx: number) => {
    update(value.filter((_, i) => i !== idx));
  };

  const moveRow = (idx: number, direction: -1 | 1) => {
    const target = idx + direction;
    if (target < 0 || target >= value.length) return;
    const next = [...value];
    [next[idx], next[target]] = [next[target], next[idx]];
    update(next);
  };

  const patchRow = (idx: number, patch: Partial<RequiredInput>) => {
    update(value.map((entry, i) => (i === idx ? { ...entry, ...patch } : entry)));
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-white font-medium">Required inputs</p>
          <p className="text-xs text-zinc-500 mt-0.5">
            Fields the upload UI collects from end users (e.g. <code>location</code>,{" "}
            <code>subject</code>). Used by templates that substitute user text into overlays.
          </p>
        </div>
        <button
          type="button"
          onClick={addRow}
          disabled={disabled}
          className="px-3 py-1.5 text-xs bg-zinc-800 hover:bg-zinc-700 text-white rounded border border-zinc-700 disabled:opacity-50"
        >
          + Add input
        </button>
      </div>

      {value.length === 0 && (
        <div className="text-xs text-zinc-600 italic border border-dashed border-zinc-800 rounded px-3 py-4 text-center">
          No required inputs. Click <em>Add input</em> to create the first one.
        </div>
      )}

      {value.map((entry, idx) => (
        <div
          key={idx}
          data-testid={`required-input-row-${idx}`}
          className="border border-zinc-800 rounded p-3 space-y-2 bg-zinc-950/50"
        >
          <div className="grid grid-cols-2 gap-2">
            <label className="block text-xs text-zinc-400">
              Key
              <input
                value={entry.key}
                onChange={(e) => patchRow(idx, { key: e.target.value })}
                placeholder="location"
                disabled={disabled}
                className="mt-1 w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-white text-sm focus:outline-none focus:border-zinc-500 font-mono"
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Label
              <input
                value={entry.label}
                onChange={(e) => patchRow(idx, { label: e.target.value })}
                placeholder="Location"
                disabled={disabled}
                className="mt-1 w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-white text-sm focus:outline-none focus:border-zinc-500"
              />
            </label>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <label className="block text-xs text-zinc-400 col-span-2">
              Placeholder
              <input
                value={entry.placeholder ?? ""}
                onChange={(e) => patchRow(idx, { placeholder: e.target.value })}
                placeholder="e.g. Brazil"
                disabled={disabled}
                className="mt-1 w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-white text-sm focus:outline-none focus:border-zinc-500"
              />
            </label>
            <label className="block text-xs text-zinc-400">
              Max length
              <input
                type="number"
                value={entry.max_length ?? 50}
                onChange={(e) =>
                  patchRow(idx, {
                    max_length: Math.max(1, Math.min(200, Number(e.target.value) || 50)),
                  })
                }
                min={1}
                max={200}
                disabled={disabled}
                className="mt-1 w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-white text-sm focus:outline-none focus:border-zinc-500"
              />
            </label>
          </div>

          <div className="flex items-center justify-between pt-1">
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <input
                type="checkbox"
                checked={Boolean(entry.required)}
                onChange={(e) => patchRow(idx, { required: e.target.checked })}
                disabled={disabled}
                className="rounded"
              />
              Required
            </label>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => moveRow(idx, -1)}
                disabled={disabled || idx === 0}
                aria-label="Move up"
                className="px-2 py-1 text-xs text-zinc-400 hover:text-white disabled:opacity-30"
              >
                ↑
              </button>
              <button
                type="button"
                onClick={() => moveRow(idx, 1)}
                disabled={disabled || idx === value.length - 1}
                aria-label="Move down"
                className="px-2 py-1 text-xs text-zinc-400 hover:text-white disabled:opacity-30"
              >
                ↓
              </button>
              <button
                type="button"
                onClick={() => removeRow(idx)}
                disabled={disabled}
                aria-label="Remove"
                className="px-2 py-1 text-xs text-red-400 hover:text-red-300 disabled:opacity-30"
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      ))}

      {localError && (
        <p className="text-xs text-red-400">{localError}</p>
      )}
    </div>
  );
}
