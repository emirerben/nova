/**
 * Tests for SeedProvenanceBadge.
 *
 * The badge is a pure function of item.source_idea_seed_id/text:
 *   - renders "From your idea: <text>" when both fields present
 *   - renders "From your idea" when only the id is present (text missing)
 *   - renders nothing (null) when source_idea_seed_id is absent/null
 *   - truncates long seed text at 36 chars with "…"
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import { SeedProvenanceBadge } from "@/app/plan/_components/ui/SeedProvenanceBadge";
import type { PlanItem } from "@/lib/plan-api";

function makeItem(overrides: Partial<PlanItem> = {}): PlanItem {
  return {
    id: "item-1",
    day_index: 1,
    theme: "Football",
    idea: "Fenerbahce match day highlights",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status: "idea",
    current_job_id: null,
    user_edited: false,
    ...overrides,
  };
}

describe("SeedProvenanceBadge", () => {
  it("renders label with seed text when both fields are present", () => {
    render(
      <SeedProvenanceBadge
        item={makeItem({
          source_idea_seed_id: "seed_fb",
          source_idea_seed_text: "Fenerbahce game",
        })}
      />,
    );
    expect(screen.getByText(/From your idea: Fenerbahce game/)).toBeTruthy();
  });

  it("renders fallback label when only the id is present", () => {
    render(
      <SeedProvenanceBadge
        item={makeItem({
          source_idea_seed_id: "seed_fb",
          source_idea_seed_text: null,
        })}
      />,
    );
    expect(screen.getByText("From your idea")).toBeTruthy();
  });

  it("renders nothing when source_idea_seed_id is null", () => {
    const { container } = render(
      <SeedProvenanceBadge
        item={makeItem({ source_idea_seed_id: null })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when source_idea_seed_id is absent", () => {
    const { container } = render(<SeedProvenanceBadge item={makeItem()} />);
    expect(container.firstChild).toBeNull();
  });

  it("truncates long seed text at 36 chars with ellipsis", () => {
    const longText = "a very long idea seed that exceeds thirty-six characters easily";
    render(
      <SeedProvenanceBadge
        item={makeItem({
          source_idea_seed_id: "seed_x",
          source_idea_seed_text: longText,
        })}
      />,
    );
    // The badge renders "✦ From your idea: <truncated>…" — verify truncation happened.
    expect(screen.getByText(/From your idea:/)).toBeTruthy();
    const badge = screen.getByText(/From your idea:/).closest("span");
    expect(badge?.textContent).toContain("…");
    expect(badge?.textContent?.includes(longText)).toBe(false);
  });
});
