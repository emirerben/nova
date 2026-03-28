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

const emptyFilesModule: Module = {
  id: "posting",
  name: "1-Click Posting",
  description: "Platform posting (Phase 2)",
  level: "L2",
  files: [],
  githubLabel: "module:delivery",
  dependsOn: ["results_page"],
  produces: ["posted content on platforms"],
};

describe("ModuleDetailPanel", () => {
  test("shows module name, description, file list in technical view", () => {
    render(
      <ModuleDetailPanel module={testModule} onClose={jest.fn()} viewMode="technical" />
    );

    expect(screen.getByText("Processing")).toBeInTheDocument();
    expect(
      screen.getByText("Video analysis pipeline: probe, transcribe, scene detect, score")
    ).toBeInTheDocument();
    expect(
      screen.getByText("src/apps/api/app/tasks/orchestrate.py")
    ).toBeInTheDocument();
    expect(
      screen.getByText("src/apps/api/app/pipeline/probe.py")
    ).toBeInTheDocument();
  });

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

  test("shows 'No recent commits' when no commit data", () => {
    render(
      <ModuleDetailPanel module={testModule} onClose={jest.fn()} viewMode="technical" />
    );

    expect(screen.getByText("No recent commits")).toBeInTheDocument();
  });

  test("shows 'No open issues' with checkmark when issue count is 0", () => {
    render(
      <ModuleDetailPanel module={testModule} onClose={jest.fn()} viewMode="technical" />
    );

    expect(screen.getByText(/No open issues/)).toBeInTheDocument();
  });

  test("shows 'No files listed' for module with empty files array", () => {
    render(
      <ModuleDetailPanel module={emptyFilesModule} onClose={jest.fn()} viewMode="technical" />
    );

    expect(screen.getByText("No files listed")).toBeInTheDocument();
  });
});
