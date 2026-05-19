/**
 * Tests for /admin/templates/[id] Debug tab.
 *
 * Mocks the typed client and asserts the rendered surface:
 *   - happy path: header chip + agent rows
 *   - failed status: error chip + error_detail visible
 *   - empty runs: empty-state hint
 */

import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";

import { DebugTab } from "@/app/admin/templates/[id]/components/DebugTab";
import * as adminApi from "@/lib/admin-api";
import type { TemplateDebugResponse } from "@/lib/admin-api";

jest.mock("@/lib/admin-api", () => ({
  __esModule: true,
  adminGetTemplateDebug: jest.fn(),
}));

const mockedGet = adminApi.adminGetTemplateDebug as jest.MockedFunction<
  typeof adminApi.adminGetTemplateDebug
>;

function makeRun(overrides: Partial<TemplateDebugResponse["template_agent_runs"][number]> = {}) {
  return {
    id: "r1",
    segment_idx: null,
    agent_name: "nova.compose.template_recipe",
    prompt_version: "3",
    model: "gemini-2.5-pro",
    outcome: "ok",
    attempts: 1,
    tokens_in: 1234,
    tokens_out: 567,
    cost_usd: 0.0123,
    latency_ms: 900,
    error_message: null,
    input_json: { k: "v" },
    output_json: { answer: "y" },
    raw_text: null,
    created_at: "2026-05-17T12:00:00Z",
    ...overrides,
  };
}

function makeResponse(
  overrides: Partial<TemplateDebugResponse> = {},
): TemplateDebugResponse {
  return {
    template: {
      id: "tpl_a",
      name: "Tiki Welcome",
      analysis_status: "ready",
      template_type: "standard",
      is_agentic: false,
      gcs_path: "templates/tiki.mp4",
      audio_gcs_path: null,
      music_track_id: null,
      error_detail: null,
      recipe_cached_at: "2026-05-17T11:50:00Z",
      created_at: "2026-05-17T11:00:00Z",
    },
    template_agent_runs: [makeRun()],
    recipe_cached: { slots: [{ i: 0 }] },
    ...overrides,
  };
}

beforeEach(() => {
  mockedGet.mockReset();
});

describe("DebugTab", () => {
  it("renders agent runs from the API", async () => {
    mockedGet.mockResolvedValueOnce(makeResponse());
    render(<DebugTab templateId="tpl_a" />);

    await waitFor(() =>
      expect(screen.getByText("nova.compose.template_recipe")).toBeInTheDocument(),
    );
    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(screen.getAllByText(/1 run/).length).toBeGreaterThan(0);
    expect(mockedGet).toHaveBeenCalledWith("tpl_a");
  });

  it("shows empty-state hint when there are no runs", async () => {
    mockedGet.mockResolvedValueOnce(
      makeResponse({ template_agent_runs: [], recipe_cached: null }),
    );
    render(<DebugTab templateId="tpl_a" />);

    await waitFor(() =>
      expect(
        screen.getByText(/No agent runs recorded/i),
      ).toBeInTheDocument(),
    );
  });

  it("shows analyzing hint when status is analyzing and no runs yet", async () => {
    mockedGet.mockResolvedValueOnce(
      makeResponse({
        template: {
          ...makeResponse().template,
          analysis_status: "analyzing",
        },
        template_agent_runs: [],
      }),
    );
    render(<DebugTab templateId="tpl_a" />);

    await waitFor(() =>
      expect(
        screen.getByText(/Analysis is still running/i),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("analyzing")).toBeInTheDocument();
  });

  it("renders error_detail when template failed", async () => {
    mockedGet.mockResolvedValueOnce(
      makeResponse({
        template: {
          ...makeResponse().template,
          analysis_status: "failed",
          error_detail: "Gemini rejected the prompt: invalid JSON.",
        },
      }),
    );
    render(<DebugTab templateId="tpl_a" />);

    await waitFor(() =>
      expect(screen.getByText("failed")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Gemini rejected the prompt: invalid JSON."),
    ).toBeInTheDocument();
  });

  it("surfaces fetch errors", async () => {
    mockedGet.mockRejectedValueOnce(new Error("Boom: 500"));
    render(<DebugTab templateId="tpl_a" />);

    await waitFor(() =>
      expect(screen.getByText(/Failed to load debug payload/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Boom: 500/)).toBeInTheDocument();
  });
});
