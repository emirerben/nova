const EN_US_PLURAL_RULES = new Intl.PluralRules("en-US");

/** Format a count with an explicit noun while keeping plural logic in one place. */
export function formatCount(
  count: number,
  singular: string,
  plural = `${singular}s`,
): string {
  const noun = EN_US_PLURAL_RULES.select(count) === "one" ? singular : plural;
  return `${count} ${noun}`;
}

/** Format a rounded duration for helper copy without abbreviations or symbols. */
export function formatDurationWords(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  const parts: string[] = [];
  if (minutes > 0) parts.push(formatCount(minutes, "minute"));
  if (remainingSeconds > 0 || parts.length === 0) {
    parts.push(formatCount(remainingSeconds, "second"));
  }
  return parts.join(" ");
}
