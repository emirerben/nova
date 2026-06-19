/**
 * Failure-reason taxonomy (DESIGN.md §7-D10): backend `error_class` values →
 * plain language naming WHY it failed and the action that can actually help.
 * Raw error strings never reach users, and "try editing again" died here — it
 * suggested a retry that couldn't fix a language problem (the Chinese-lyrics
 * incident, dogfood feedback #6).
 */
/**
 * Plain-language explanation for a clip that couldn't be placed in a variant.
 * Returned as a sentence that slots into "Shot N – <copy>".
 */
export function unplacedShotCopy(reason: string | null | undefined): string {
  switch (reason) {
    case "song_too_short":
      return "didn't fit this song — the beat window was shorter than your shots. It's in the Original-audio take.";
    case "unusable_footage":
      return "couldn't be used here (the clip may have failed to process).";
    default:
      return "wasn't placed in this take. It's in the Original-audio take.";
  }
}

export function variantFailureCopy(errorClass?: string | null): string {
  switch (errorClass) {
    case "lyrics_unsupported_language":
      return "Lyrics aren't available for this song's language yet — try a different song.";
    case "lyric_alignment_error":
      return "The lyrics couldn't be timed to this song — try a different song.";
    case "timeout":
      return "This render ran out of time — generating again usually works.";
    case "storage_error":
      return "We couldn't fetch the footage for this one — try again.";
    case "encoder_error":
      return "Something went wrong while rendering — try again.";
    default:
      return "This one didn't render. Changing the song or style starts a fresh try.";
  }
}
