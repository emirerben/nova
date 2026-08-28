/**
 * Shared text-control values for every editor presentation.
 *
 * Desktop and pocket surfaces must import these values instead of maintaining
 * their own font-size ranges or select options. The renderer accepts the full
 * 8–300px range; the select provides the same practical stops as desktop while
 * the fine slider covers every integer in the range.
 */
export const EDITOR_TEXT_SIZE_MIN = 8;
export const EDITOR_TEXT_SIZE_MAX = 300;

export const EDITOR_TEXT_SIZE_OPTIONS = (() => {
  const values: number[] = [];
  for (let size = 8; size <= 96; size += 8) values.push(size);
  values.push(120, 160, 220, 300);
  return values;
})();
