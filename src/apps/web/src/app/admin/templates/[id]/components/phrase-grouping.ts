// Phrase grouping for the OverlaysTab editor.
//
// Layer-2 (post-PR #278) emits cumulative-reveal overlays where each row's
// sample_text holds the full line built up to and including that word:
//
//   overlay[0].sample_text = "the"
//   overlay[1].sample_text = "the work"
//   overlay[2].sample_text = "the work to"
//   overlay[3].sample_text = "the work to get there"
//
// One on-screen phrase becomes N overlays. The admin should edit the phrase
// once, not N times. This module groups those rows for the UI and expands an
// edited phrase back across the underlying overlays' sample_text values so
// the existing PATCH /admin/templates/{id}/overlays endpoint can persist
// without any backend change.

export interface OverlayRow {
  slot_index: number;
  overlay_index: number;
  original_sample_text: string;
  current_sample_text: string;
  start_s: number | null;
  end_s: number | null;
  role: string | null;
}

export type PhrasePattern = "cumulative" | "per_word" | "singleton";

export interface PhraseGroup {
  slot_index: number;
  // Indices into the input rows[] array, in order.
  member_row_indices: number[];
  pattern: PhrasePattern;
  // Joined human text the editor shows in the single input field.
  display_text: string;
  // Computed from members.
  start_s: number | null;
  end_s: number | null;
  role: string | null;
  dirty: boolean;
}

function tokenize(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean);
}

function isCumulativeContinuation(prev: string, curr: string): boolean {
  const p = prev.trim();
  const c = curr.trim();
  if (!p || !c) return false;
  if (c.length <= p.length) return false;
  return c.startsWith(p + " ");
}

function isPerWordContinuation(prev: string, curr: string): boolean {
  // Per-word atomized overlays: each row is a single short token, disjoint from
  // the prior row. We only group if both look like single tokens (no spaces).
  const p = prev.trim();
  const c = curr.trim();
  if (!p || !c) return false;
  if (p.includes(" ") || c.includes(" ")) return false;
  // And the per-word row should not be a cumulative extension — if it starts
  // with prev + " ", treat as cumulative (handled by the other branch).
  return !c.startsWith(p + " ");
}

// Build phrase groups within each slot. Rows are consumed in input order;
// the OverlaysTab already orders rows by (slot_index, overlay_index).
export function groupOverlayRowsIntoPhrases(rows: OverlayRow[]): PhraseGroup[] {
  const groups: PhraseGroup[] = [];
  if (rows.length === 0) return groups;

  let i = 0;
  while (i < rows.length) {
    const start = rows[i];
    const memberIndices: number[] = [i];
    let pattern: PhrasePattern = "singleton";

    // Look ahead to extend the run as long as same slot AND a known pattern
    // continues.
    while (i + 1 < rows.length) {
      const prevRow = rows[memberIndices[memberIndices.length - 1]];
      const nextRow = rows[i + 1];
      if (nextRow.slot_index !== start.slot_index) break;

      const prevText = prevRow.current_sample_text;
      const nextText = nextRow.current_sample_text;

      if (isCumulativeContinuation(prevText, nextText)) {
        if (pattern === "per_word") break; // pattern crossover — close here
        pattern = "cumulative";
        memberIndices.push(i + 1);
        i++;
        continue;
      }
      if (pattern !== "cumulative" && isPerWordContinuation(prevText, nextText)) {
        pattern = "per_word";
        memberIndices.push(i + 1);
        i++;
        continue;
      }
      break;
    }

    // Trailing-empty absorption: when the user shrinks a phrase (e.g. 4-stage
    // cumulative reveal → "Hello"), the freed overlays carry empty
    // sample_text. Keep them in the same phrase group so the editor still
    // shows one row, not 1 row + N ghost "single overlay" rows. We only
    // absorb empties at the tail — never leading or middle — so two
    // legitimately separate phrases in one slot can't fuse via a stray empty
    // between them.
    while (i + 1 < rows.length) {
      const nextRow = rows[i + 1];
      if (nextRow.slot_index !== start.slot_index) break;
      if (nextRow.current_sample_text.trim() !== "") break;
      memberIndices.push(i + 1);
      i++;
    }

    const lastMember = rows[memberIndices[memberIndices.length - 1]];
    // Display text ignores trailing-empty members so a phrase shrunk to one
    // word still shows that word in the input field, not "".
    const nonEmptyMembers = memberIndices
      .map((mi) => rows[mi])
      .filter((r) => r.current_sample_text.trim() !== "");
    const display_text =
      pattern === "cumulative"
        ? (nonEmptyMembers[nonEmptyMembers.length - 1]?.current_sample_text ?? "")
        : pattern === "per_word"
          ? nonEmptyMembers.map((r) => r.current_sample_text.trim()).join(" ")
          : nonEmptyMembers[0]?.current_sample_text ?? "";

    const dirty = memberIndices.some(
      (mi) => rows[mi].current_sample_text !== rows[mi].original_sample_text,
    );

    groups.push({
      slot_index: start.slot_index,
      member_row_indices: memberIndices,
      pattern,
      display_text,
      start_s: start.start_s,
      end_s: lastMember.end_s,
      role: start.role,
      dirty,
    });

    i++;
  }

  return groups;
}

// Given an existing phrase group and the user's edited text, produce the new
// sample_text for every underlying overlay row. Indices in the returned array
// align 1:1 with group.member_row_indices.
//
// Cumulative pattern with M members and N user-typed words:
//   N == 0           → all members "" (renderer hides empties).
//   1 <= N <  M      → stages[0..N-1] cumulative + stages[N..M-1] "".
//   N == M           → stages[0..M-1] cumulative 1..M words.
//   N >  M           → stages[0..M-2] cumulative 1..M-1; last member = full text
//                      (last reveal stage absorbs the surplus words).
// Per-word pattern: one word per member; pad with "" or compress the tail into
// the last member when N > M.
// Singleton: replace the single member's text verbatim.
export function expandPhraseEditToMemberTexts(
  group: PhraseGroup,
  newText: string,
): string[] {
  const memberCount = group.member_row_indices.length;
  const words = tokenize(newText);

  if (memberCount === 1) {
    return [words.join(" ")];
  }

  if (words.length === 0) {
    return new Array(memberCount).fill("");
  }

  if (group.pattern === "per_word") {
    const out: string[] = [];
    for (let k = 0; k < memberCount; k++) {
      if (k >= words.length) {
        out.push("");
      } else if (k === memberCount - 1 && words.length > memberCount) {
        out.push(words.slice(k).join(" "));
      } else {
        out.push(words[k]);
      }
    }
    return out;
  }

  // Default: cumulative (the dominant Layer-2 shape post-PR #278).
  const out: string[] = [];
  for (let k = 0; k < memberCount; k++) {
    if (k >= words.length) {
      out.push("");
    } else if (k === memberCount - 1) {
      out.push(words.join(" "));
    } else {
      out.push(words.slice(0, k + 1).join(" "));
    }
  }
  return out;
}
