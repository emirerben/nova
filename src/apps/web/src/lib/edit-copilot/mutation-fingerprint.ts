function stableMutationValue(value: unknown): unknown {
  if (value === undefined) return { __nova_undefined__: true };
  if (Array.isArray(value)) return value.map(stableMutationValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableMutationValue(item)]),
    );
  }
  return value;
}

/** Compact opaque fingerprint for stale-target protection. It deliberately
 * covers editor persistence fields that are too noisy or sensitive to put in
 * the model-facing prose, while remaining deterministic across object-key order. */
export function mutationFingerprint(parts: readonly unknown[]): string {
  const value = JSON.stringify(stableMutationValue(parts));
  let left = 2166136261;
  let right = 3339675911;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    left = Math.imul(left ^ code, 16777619);
    right = Math.imul(right ^ code, 2246822519);
  }
  return `m1-${(left >>> 0).toString(16).padStart(8, "0")}${(right >>> 0).toString(16).padStart(8, "0")}`;
}

