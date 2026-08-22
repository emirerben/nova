/**
 * Raw-control ratchet (Kria Design System migration, DESIGN.md §15).
 *
 * Counts native `<button className=…>` / `<select>` / `<input className=…>`
 * (type not file/range/checkbox/radio/hidden/color) / `<textarea
 * className=…>` occurrences under `app/plan/**`, `app/generative/**`, and
 * `components/**` (excluding `components/ui/**` — the primitives themselves
 * — and `app/admin/**`, which keeps its own dark variant and migrates
 * later). Pairs with the `.eslintrc.json` `no-restricted-syntax` WARN rule
 * covering the same surfaces: this test is the numeric ratchet, the lint
 * rule is the inline nudge at write time.
 *
 * `RAW_CONTROL_BASELINE` is declared in per-lane blocks so each
 * implementation lane (A–F) only ever edits its own block — disjoint git
 * hunks, no merge conflicts between lanes touching different files. Rules:
 *   - A file present in the baseline may only go DOWN (lane replaced some/
 *     all of its raw controls with primitives) — never up.
 *   - A file absent from the baseline must have ZERO raw controls — i.e. a
 *     brand-new file must be written with primitives from the start.
 *   - The LAST lane to merge deletes this file entirely (DESIGN.md §15,
 *     "Sequencing + VERSION trap").
 */
import fs from "fs";
import path from "path";
import { describe, expect, it } from "@jest/globals";

const WEB_SRC = path.join(__dirname, "..", "..");

const SCAN_ROOTS = ["app/plan", "app/generative", "components"];
const EXCLUDE_DIR_PREFIXES = [
  path.join("components", "ui"),
  path.join("app", "admin"),
];
const EXCLUDED_INPUT_TYPES = new Set([
  "file",
  "range",
  "checkbox",
  "radio",
  "hidden",
  "color",
]);

/**
 * Return the element's opening tag only (see mobile-shell.test.tsx for the
 * same technique + rationale — non-self-closing tags like <textarea> need a
 * quote/brace-aware scan, not a naive `/>` or `>` search).
 */
function openingTag(src: string, from: number): string {
  let quote: string | null = null;
  let depth = 0;
  for (let i = from + 1; i < src.length; i++) {
    const ch = src[i];
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") quote = ch;
    else if (ch === "{") depth++;
    else if (ch === "}") depth--;
    else if (ch === ">" && depth === 0) return src.slice(from, i + 1);
  }
  return src.slice(from, from + 1400);
}

function countRawControls(src: string): number {
  let count = 0;

  for (const m of Array.from(src.matchAll(/<button[\s>]/g))) {
    if (/\bclassName=/.test(openingTag(src, m.index!))) count++;
  }

  count += Array.from(src.matchAll(/<select[\s>]/g)).length;

  for (const m of Array.from(src.matchAll(/<input[\s>]/g))) {
    const tag = openingTag(src, m.index!);
    if (!/\bclassName=/.test(tag)) continue;
    const typeMatch = tag.match(/\btype=["']([a-zA-Z]+)["']/);
    const type = typeMatch ? typeMatch[1] : "text";
    if (EXCLUDED_INPUT_TYPES.has(type)) continue;
    count++;
  }

  for (const m of Array.from(src.matchAll(/<textarea[\s>]/g))) {
    if (/\bclassName=/.test(openingTag(src, m.index!))) count++;
  }

  return count;
}

function isExcluded(relPath: string): boolean {
  return EXCLUDE_DIR_PREFIXES.some(
    (p) => relPath === p || relPath.startsWith(p + path.sep)
  );
}

function scan(): Record<string, number> {
  const results: Record<string, number> = {};

  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      const rel = path.relative(WEB_SRC, full);
      if (entry.isDirectory()) {
        if (isExcluded(rel)) continue;
        walk(full);
        continue;
      }
      if (!entry.name.endsWith(".tsx")) continue;
      if (isExcluded(rel)) continue;
      const src = fs.readFileSync(full, "utf8");
      const count = countRawControls(src);
      if (count > 0) results[rel] = count;
    }
  };

  for (const root of SCAN_ROOTS) {
    const full = path.join(WEB_SRC, root);
    if (fs.existsSync(full)) walk(full);
  }

  return results;
}

// ── Lane 0 (seed) ───────────────────────────────────────────────────────
// Captured from origin/main @ 5553c582b (v0.46.0.1) via the exact
// `countRawControls` logic above. Lanes A–F each get their own block below
// this one, overriding only the files they touch.
const LANE_0_BASELINE: Record<string, number> = {
  "app/generative/VariantCard.tsx": 9,
  "app/generative/VoiceRecorder.tsx": 3,
  "app/plan/_components/AssetPool.tsx": 12,
  "app/plan/_components/CaptionEditor.tsx": 11,
  "app/plan/_components/CaptionStyleToggle.tsx": 1,
  "app/plan/_components/ChatInterview.tsx": 4,
  "app/plan/_components/ClipsLane.tsx": 2,
  "app/plan/_components/InlineClipsEditor.tsx": 19,
  "app/plan/_components/LiveOverlayCardsLayer.tsx": 1,
  "app/plan/_components/OnboardingShell.tsx": 2,
  "app/plan/_components/OverlayCardPopover.tsx": 7,
  "app/plan/_components/OverlayLane.tsx": 1,
  "app/plan/_components/PersonaEditor.tsx": 11,
  "app/plan/_components/PlanVariantEditor.tsx": 11,
  "app/plan/_components/SeedUploadCard.tsx": 2,
  "app/plan/_components/SfxLane.tsx": 7,
  "app/plan/_components/SignInPrompt.tsx": 1,
  "app/plan/_components/SongPicker.tsx": 3,
  "app/plan/_components/StyleAgentInterview.tsx": 5,
  "app/plan/_components/SuggestionRail.tsx": 8,
  "app/plan/_components/TextLane.tsx": 22,
  "app/plan/_components/TikTokPreScreen.tsx": 3,
  "app/plan/_components/UnifiedTimeline.tsx": 1,
  "app/plan/_components/onboarding/ClipGroupStep.tsx": 7,
  "app/plan/_components/onboarding/EditContextStep.tsx": 3,
  "app/plan/_components/onboarding/EditPayoff.tsx": 6,
  "app/plan/_components/onboarding/EditUploadStep.tsx": 5,
  "app/plan/_components/onboarding/ForkScreen.tsx": 3,
  "app/plan/_components/ui/StepRail.tsx": 2,
  "app/plan/_components/workspace/WorkspaceHome.tsx": 2,
  "app/plan/items/[id]/_editor/CaptionsDrawer.tsx": 14,
  "app/plan/items/[id]/_editor/CarouselPanel.tsx": 10,
  "app/plan/items/[id]/_editor/ContextStrip.tsx": 1,
  "app/plan/items/[id]/_editor/CopilotDrawer.tsx": 14,
  "app/plan/items/[id]/_editor/DirectorSuggestions.tsx": 6,
  "app/plan/items/[id]/_editor/EditorCanvas.tsx": 6,
  "app/plan/items/[id]/_editor/EditorShell.tsx": 35,
  "app/plan/items/[id]/_editor/EditorTimelineBody.tsx": 6,
  "app/plan/items/[id]/_editor/InspectorPanel.tsx": 34,
  "app/plan/items/[id]/_editor/InspectorRail.tsx": 1,
  "app/plan/items/[id]/_editor/MiniStrip.tsx": 2,
  "app/plan/items/[id]/_editor/MotionInspector.tsx": 10,
  "app/plan/items/[id]/_editor/OverlaySuggestions.tsx": 12,
  "app/plan/items/[id]/_editor/PresetGrid.tsx": 2,
  "app/plan/items/[id]/_editor/Sheet.tsx": 2,
  "app/plan/items/[id]/_editor/SongWindowSelector.tsx": 1,
  "app/plan/items/[id]/_editor/StylesDrawer.tsx": 1,
  "app/plan/items/[id]/_editor/ToolDock.tsx": 1,
  "app/plan/items/[id]/_editor/ToolDrawer.tsx": 42,
  "app/plan/items/[id]/_editor/ToolRail.tsx": 1,
  "app/plan/items/[id]/_editor/TransportBar.tsx": 6,
  "app/plan/items/[id]/_editor/inspector-fields.tsx": 3,
  "app/plan/items/[id]/components/AskKriaPanel.tsx": 6,
  "app/plan/items/[id]/components/EditProposalCard.tsx": 27,
  "app/plan/items/[id]/components/SetupPicker.tsx": 1,
  "app/plan/items/[id]/components/ShotSlotUploader.tsx": 17,
  "app/plan/items/[id]/page.tsx": 20,
  "app/plan/items/[id]/transcript/BriefStep.tsx": 1,
  "app/plan/items/[id]/transcript/QuestionsStep.tsx": 4,
  "app/plan/items/[id]/transcript/ReviewStep.tsx": 1,
  "app/plan/items/[id]/transcript/ScriptStep.tsx": 3,
  "app/plan/items/[id]/transcript/TeleprompterRecorder.tsx": 4,
  "app/plan/new/page.tsx": 2,
  "app/plan/page.tsx": 2,
  "components/Header.tsx": 6,
  "components/JsonTreeView.tsx": 2,
  "components/TikTokPublishDialog.tsx": 10,
  "components/TikTokReleaseRail.tsx": 13,
  "components/architecture/ArchitectureMap.tsx": 3,
  "components/library/FeedbackButtons.tsx": 4,
  "components/library/LibraryTile.tsx": 4,
  "components/library/TikTokConnectionCard.tsx": 4,
  "components/progress/NovaActivityFeed.tsx": 3,
  "components/progress/NovaStepRow.tsx": 1,
  "components/progress/ProgressTheater.tsx": 1,
  "components/progress/VariantRenderCard.tsx": 3,
  "components/text-motion/TextMotionControls.tsx": 5,
  "components/variant-editor/EditToolbar.tsx": 8,
  "components/variant-editor/LayoutPreviewCard.tsx": 1,
};

// ── Lane A ──────────────────────────────────────────────────────────────
// (none yet)
const LANE_A_BASELINE: Record<string, number> = {};

// ── Lane B ──────────────────────────────────────────────────────────────
// (none yet)
const LANE_B_BASELINE: Record<string, number> = {};

// ── Lane C ──────────────────────────────────────────────────────────────
// (none yet)
const LANE_C_BASELINE: Record<string, number> = {};

// ── Lane D ──────────────────────────────────────────────────────────────
// (none yet)
const LANE_D_BASELINE: Record<string, number> = {};

// ── Lane E ──────────────────────────────────────────────────────────────
// E3 (`CopilotDrawer`, `CaptionsDrawer`, `MotionInspector`, `Sheet.tsx`):
// counts recaptured after the primitive swap (Button/Input/Label/Select).
// `StylesDrawer.tsx`'s one raw control (a role="radio" video-preview card)
// and the remaining raw controls in these files are intentionally left
// native — see the E3 PR description for why each doesn't map cleanly onto
// a shadcn primitive.
const LANE_E_BASELINE: Record<string, number> = {
  "app/plan/items/[id]/_editor/CopilotDrawer.tsx": 8,
  "app/plan/items/[id]/_editor/CaptionsDrawer.tsx": 13,
  "app/plan/items/[id]/_editor/MotionInspector.tsx": 5,
};

// ── Lane F ──────────────────────────────────────────────────────────────
// (none yet)
const LANE_F_BASELINE: Record<string, number> = {};

const RAW_CONTROL_BASELINE: Record<string, number> = {
  ...LANE_0_BASELINE,
  ...LANE_A_BASELINE,
  ...LANE_B_BASELINE,
  ...LANE_C_BASELINE,
  ...LANE_D_BASELINE,
  ...LANE_E_BASELINE,
  ...LANE_F_BASELINE,
};

describe("raw-control ratchet (DESIGN.md §15)", () => {
  it("no scanned file exceeds its baseline, and no new file introduces raw controls", () => {
    const actual = scan();
    const regressions: string[] = [];

    for (const [file, count] of Object.entries(actual)) {
      const allowed = RAW_CONTROL_BASELINE[file] ?? 0;
      if (count > allowed) {
        regressions.push(`${file}: ${count} raw control(s), baseline allows ${allowed}`);
      }
    }

    expect(regressions).toEqual([]);
  });
});
