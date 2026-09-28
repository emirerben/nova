import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { ModuleDetailPanel } from "@/components/architecture/ModuleDetailPanel";
import type { Module } from "@/lib/architecture-config";

// ---------------------------------------------------------------------------
// Mock the hooks so we control GitHub data
// ---------------------------------------------------------------------------
const mockIssuesData = { items: [], rateLimited: false };
const mockCommitsData = { items: [], rateLimited: false };

jest.mock("@/hooks/useArchitectureData", () => ({
  useModuleIssues: (label: string | null) => ({
    data: label ? mockIssuesData : null,
    isLoading: false,
  }),
  useModuleCommits: (path: string | null) => ({
    data: path ? mockCommitsData : null,
    isLoading: false,
  }),
}));

const testModule: Module = {
  id: "processing",
  name: "Processing",
  description: "Video analysis pipeline: probe, transcribe, scene detect, score",
  level: "L1",
  files: [
    "src/apps/api/app/tasks/orchestrate.py",
    "src/apps/api/app/pipeline/probe.py",
  ],
  githubLabel: "module:processing",
  dependsOn: ["upload"],
  produces: ["top 9 clip candidates (ranked)"],
  business: {
    userFacing: "AI watches the video",
    businessImpact: "Core AI magic",
    metric: "Processing time <8 min",
    status: "live",
  },
};

describe("ModuleDetailPanel", () => {

  test("file links point to correct GitHub blob URLs", () => {
    render(
      <ModuleDetailPanel module={testModule} onClose={jest.fn()} viewMode="technical" />
    );

    const link = screen.getByText("src/apps/api/app/tasks/orchestrate.py");
    expect(link).toHaveAttribute(
      "href",
      "https://github.com/emirerben/nova/blob/main/src/apps/api/app/tasks/orchestrate.py"
    );
    expect(link).toHaveAttribute("target", "_blank");
  });
});
