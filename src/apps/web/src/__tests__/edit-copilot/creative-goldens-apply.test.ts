/**
 * Creative eval goldens must APPLY, not just parse (review OV-8).
 *
 * The backend eval harness proves each golden's op bundle survives server-side
 * validation. This test closes the other half of the loop: reconstruct the
 * draft state each golden's snapshot describes, rebuild the snapshot with
 * buildCopilotSnapshot, and run the golden's ops through applyCopilotOps —
 * a golden whose ops reference missing ids, stale indices, or unsupported
 * fields fails here instead of in a user's chat.
 */

import fs from "fs";
import path from "path";
import { describe, expect, it } from "@jest/globals";
import { applyCopilotOps } from "@/lib/edit-copilot/apply-ops";
import { buildCopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { SoundEffectSummary } from "@/lib/sfx-api";

const GOLDEN_DIR = path.join(
  __dirname,
  "../../../../api/tests/fixtures/agent_evals/edit_copilot/golden",
);

const CREATIVE_GOLDENS = [
  "ambiguous_make_it_pop.json",
  "creative_sync_text_to_beats.json",
  "creative_make_it_cool.json",
  "creative_sfx_on_moment.json",
];

interface GoldenSnapshot {
  text_bars?: Array<{ text: string; start_s: number; end_s: number; size_px?: number; effect?: string }>;
  slots?: Array<{ duration_s: number; moment?: string }>;
  sfx?: { catalog?: Array<{ id: string; name: string; duration_s: number | null }> };
  beat_marks?: number[];
}

interface Golden {
  input: { variant_snapshot: GoldenSnapshot };
  output: { ops: unknown[] };
}

function loadGolden(name: string): Golden {
  return JSON.parse(fs.readFileSync(path.join(GOLDEN_DIR, name), "utf8")) as Golden;
}

function ctxFromGolden(golden: Golden) {
  const snap = golden.input.variant_snapshot;
  const bars: TextElementBar[] = (snap.text_bars ?? []).map((b, i) => ({
    id: `bar-${i}`,
    text: b.text,
    start_s: b.start_s,
    end_s: b.end_s,
    role: "generative_intro",
    size_px: b.size_px,
    effect: b.effect,
  }));
  const slots: DraftSlot[] = (snap.slots ?? []).map((s, i) => ({
    key: `slot-${i}`,
    slotId: `slot-${i}`,
    clipIndex: i,
    inS: 0,
    durationS: s.duration_s,
    durationBeats: null,
    removed: false,
    momentDescription: s.moment ?? null,
  }));
  const clips = slots.map(() => ({ source_duration_s: 30 }));
  const sfxCatalog = (snap.sfx?.catalog ?? []).map(
    (effect) => ({ id: effect.id, name: effect.name, duration_s: effect.duration_s }) as SoundEffectSummary,
  );
  const capabilities = { text_elements: true, timeline: true, split_clips: true, sfx: true };
  const snapshot = buildCopilotSnapshot(bars, slots, clips, capabilities, [], {
    sfxEnabled: sfxCatalog.length > 0,
    sfxPlacements: [],
    sfxCatalog,
  });
  // The goldens' beat marks come from a grid we can't reconstruct from the
  // snapshot alone — inject them so applyCopilotOps exercises the real
  // snapping path against each golden's beat-timed values.
  if (snap.beat_marks?.length) {
    snapshot.beat_marks = snap.beat_marks;
  }
  return {
    bars,
    slots,
    snapshot,
    capabilities,
    sfx: [],
    sfxCatalog,
    makeTextBarId: () => "new-text",
    makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
    makeSfxPlacementId: () => "new-sfx",
  };
}

describe("creative goldens apply cleanly on the client", () => {
  it.each(CREATIVE_GOLDENS)("%s ops apply with zero rejections", (name) => {
    const golden = loadGolden(name);
    const result = applyCopilotOps(golden.output.ops, ctxFromGolden(golden));
    expect(result.rejected).toEqual([]);
    expect(result.applied.length).toBeGreaterThan(0);
  });

  it.each(CREATIVE_GOLDENS)("%s beat-timed ops copy times exactly from beat_marks", (name) => {
    // Deterministic guard for the v4 beat-fidelity contract: every timing a
    // creative golden emits must be a literal member of its beat_marks list —
    // a golden (or future regeneration) that invents or rounds beat times
    // fails here instead of shipping.
    const golden = loadGolden(name);
    const marks = golden.input.variant_snapshot.beat_marks;
    if (!marks?.length) return;
    for (const op of golden.output.ops as Array<Record<string, unknown>>) {
      for (const field of ["start_s", "end_s", "at_s"]) {
        const value = op[field];
        if (typeof value === "number") {
          expect(marks).toContain(value);
        }
      }
    }
  });
});
