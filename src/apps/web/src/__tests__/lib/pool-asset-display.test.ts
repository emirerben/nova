import { poolAssetAnalysisLine } from "@/lib/pool-asset-display";
import type { PoolAsset } from "@/lib/plan-api";

function asset(overrides: Partial<PoolAsset> = {}): PoolAsset {
  return {
    id: "asset-1",
    kind: "video",
    status: "ready",
    source_filename: "sunset.mov",
    duration_s: 3,
    aspect: 1.78,
    subject: "sunset over a bay",
    user_context: "",
    nova_description: null,
    nova_on_screen_text: null,
    display_url: "https://signed/video.mov",
    deduped: false,
    gcs_path: "users/u/plan/i/pool/sunset.mov",
    ...overrides,
  };
}

describe("poolAssetAnalysisLine", () => {
  it("prefers a generated description", () => {
    expect(
      poolAssetAnalysisLine(asset({ nova_description: "Sunset over the bay" })),
    ).toBe("Sunset over the bay");
  });

  it("falls back to generated on-screen copy when the description is blank", () => {
    expect(
      poolAssetAnalysisLine(
        asset({
          nova_description: "  ",
          nova_on_screen_text: "Boats at golden hour",
        }),
      ),
    ).toBe("Boats at golden hour");
  });

  it("uses the authoritative active status when generated copy is absent", () => {
    expect(poolAssetAnalysisLine(asset({ status: "analyzing" }))).toBe(
      "Analysis pending",
    );
  });

  it("does not describe a failed analysis as pending", () => {
    expect(poolAssetAnalysisLine(asset({ status: "failed" }))).toBe(
      "Analysis failed",
    );
  });

  it("distinguishes a filename-only fallback from a full analysis", () => {
    expect(poolAssetAnalysisLine(asset({ source_type: "stub" }))).toBe(
      "Basic file details ready",
    );
    expect(poolAssetAnalysisLine(asset())).toBe("Analysis complete");
  });
});
