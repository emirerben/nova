export function normalizeEditableHex(value: string | null | undefined): string | null {
  if (!value) return null;
  const match = value.trim().match(/^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
  if (!match) return null;
  const raw = match[1];
  const expanded =
    raw.length === 3
      ? raw
          .split("")
          .map((char) => `${char}${char}`)
          .join("")
      : raw;
  return `#${expanded.toUpperCase()}`;
}
